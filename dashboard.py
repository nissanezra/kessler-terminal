#!/usr/bin/env python3
"""
Live markets dashboard — a Bloomberg-style terminal monitor.

  * Crypto  -> Binance websocket (data.binance.vision) — true real-time ticks
  * Stocks, indices, sector ETFs, FX, commodities, treasury yields
            -> CNBC quote webservice, polled every few seconds

Both feeds are free and need no API key. Edit the SECTIONS config below to
add/remove rows. Run with:

    ~/markets-dashboard/.venv/bin/python ~/markets-dashboard/dashboard.py
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import websockets
from rich.console import Group
from rich.live import Live

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("SSL_CERT_DIR", os.path.dirname(certifi.where()))
except Exception:
    pass
from rich.style import Style
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# CONFIG — edit freely. Rows are (label, symbol[, decimals[, unit]]).
#
#   Providers (set per section): "cnbc", "binance", "fred", "cftc".
#   CNBC symbols: indices ".DJI", futures "@CL.1", FX "EUR=", yields "US10Y".
#   Binance symbols: lowercase pair, e.g. "btcusdt".
#   FRED symbols: FRED series id (e.g. "BAMLH0A0HYM2"); needs a free API key.
#   CFTC symbols: exact CoT contract name; shows leveraged-fund net positioning.
#   unit "pos" formats large position counts as 1.23M / 788k.
# ---------------------------------------------------------------------------

POLL_SECONDS = 5          # how often to refresh CNBC-sourced rows
FRED_SECONDS = 600        # FRED series are daily; poll every 10 min
CFTC_SECONDS = 3600       # CoT positioning is weekly; poll hourly
FLASH_SECONDS = 0.8       # how long a cell stays highlighted after a price tick

SECTIONS = [
    # ----- COLUMN 0 -----
    (0, "STOCKS", "cnbc", [
        ("DOW", ".DJI"), ("DOW FUT", "@DJ.1"), ("S&P 500", ".SPX"),
        ("S&P FUT", "@SP.1"), ("NASDAQ", ".IXIC"), ("NASDAQ FUT", "@ND.1"),
        ("TRANSPORTS", ".TRAN"), ("RUSSELL 2K", ".RUT"),
    ]),
    (0, "STOCK SECTORS", "cnbc", [
        ("FINANCIALS", "XLF"), ("HEALTHCARE", "XLV"), ("MATERIALS", "XLB"),
        ("TECHNOLOGY", "XLK"), ("UTILITIES", "XLU"), ("CONS DISC", "XLY"),
        ("CONS STAP", "XLP"), ("ENERGY", "XLE"), ("INDUSTRIAL", "XLI"),
    ]),
    (0, "CURRENCIES", "cnbc", [
        ("USD INDEX", ".DXY", 3), ("EUR/USD", "EUR=", 4), ("USD/JPY", "JPY=", 2),
        ("GBP/USD", "GBP=", 4), ("USD/CNY", "CNY=", 4), ("USD/RUB", "RUB=", 3),
        ("USD/CAD", "CAD=", 4), ("AUD/USD", "AUD=", 4),
    ]),
    (0, "TREASURY FUTURES", "cnbc", [
        ("2Y NOTE", "@TU.1", 3), ("5Y NOTE", "@FV.1", 3), ("10Y NOTE", "@TY.1", 3),
        ("ULTRA 10Y", "@TN.1", 3), ("T-BOND", "@US.1", 3),
    ]),

    # ----- COLUMN 1 -----
    (1, "ENERGY & METALS", "cnbc", [
        ("CRUDE WTI", "@CL.1", 2), ("BRENT", "@BZ.1", 2), ("NAT GAS", "@NG.1", 3),
        ("GASOLINE", "@RB.1", 3), ("GOLD", "@GC.1", 2), ("SILVER", "@SI.1", 3),
        ("PLATINUM", "@PL.1", 2), ("PALLADIUM", "@PA.1", 2), ("COPPER", "@HG.1", 3),
    ]),
    (1, "COMMODITY INDEX", "cnbc", [
        ("BBG CMDTY", ".BCOM", 2), ("S&P GSCI", ".SPGSCI", 2),
    ]),
    (1, "AGRICULTURE", "cnbc", [
        ("CORN", "@C.1", 2), ("WHEAT", "@W.1", 2), ("SOYBEANS", "@S.1", 2),
        ("SUGAR", "@SB.1", 2), ("COFFEE", "@KC.1", 2), ("COTTON", "@CT.1", 2),
        ("COCOA", "@CC.1", 0),
    ]),
    (1, "TREASURY YIELDS", "cnbc", [
        ("3 MONTH", "US3M", 3), ("2 YEAR", "US2Y", 3), ("3 YEAR", "US3Y", 3),
        ("5 YEAR", "US5Y", 3), ("7 YEAR", "US7Y", 3), ("10 YEAR", "US10Y", 3),
        ("20 YEAR", "US20Y", 3), ("30 YEAR", "US30Y", 3),
    ]),
    (1, "VOLATILITY", "cnbc", [
        ("VIX", ".VIX", 2),
    ]),

    # ----- COLUMN 2 -----
    (2, "WORLD STOCKS", "cnbc", [
        ("FTSE 100", ".FTSE"), ("DAX", ".GDAXI"), ("CAC 40", ".FCHI"),
        ("STOXX 50", ".STOXX50E"), ("IBEX 35", ".IBEX"), ("FTSE MIB", ".FTMIB"),
        ("SWISS SMI", ".SSMI"), ("NIKKEI", ".N225"), ("HANG SENG", ".HSI"),
        ("SHANGHAI", ".SSEC"), ("KOSPI", ".KS11"),
    ]),
    (2, "THEMATIC ETFs", "cnbc", [
        ("SBIO BIOTECH", "SBIO"), ("EUAD DEFENSE", "EUAD"), ("URA URANIUM", "URA"),
        ("AAXJ ASIA", "AAXJ"), ("CRAK REFINERS", "CRAK"), ("COPX COPPER", "COPX"),
        ("SHLD DEFENSE", "SHLD"), ("ICLN CLEAN EN", "ICLN"), ("IBAT BATTERY", "IBAT"),
        ("BUG CYBER", "BUG"), ("REMX RARE ERTH", "REMX"),
    ]),
    (0, "CRYPTO  (live)", "binance", [
        ("BITCOIN", "btcusdt", 2), ("ETHEREUM", "ethusdt", 2),
        ("SOLANA", "solusdt", 2), ("XRP", "xrpusdt", 4),
        ("DOGECOIN", "dogeusdt", 5), ("CARDANO", "adausdt", 4),
        ("AVALANCHE", "avaxusdt", 3), ("CHAINLINK", "linkusdt", 3),
    ]),

    # ----- CREDIT / MACRO (FRED, free key) + POSITIONING (CFTC, free) -----
    (2, "CREDIT SPREADS  (bps, daily)", "fred", [
        ("HY OAS", "BAMLH0A0HYM2", 1), ("IG OAS", "BAMLC0A0CM", 1),
        ("CCC OAS", "BAMLH0A3HYC", 1), ("EM OAS", "BAMLEMCBPIOAS", 1),
    ]),
    (2, "INFLATION & CURVE  (%, daily)", "fred", [
        ("5Y BREAKEVEN", "T5YIE", 2), ("10Y BREAKEVEN", "T10YIE", 2),
        ("5Y5Y FWD", "T5YIFR", 2), ("2s10s", "T10Y2Y", 2), ("10Y-3M", "T10Y3M", 2),
    ]),
    (2, "LEV FUND NET  (CoT, weekly)", "cftc", [
        ("UST 2Y", "UST 2Y NOTE - CHICAGO BOARD OF TRADE", 0, "pos"),
        ("UST 5Y", "UST 5Y NOTE - CHICAGO BOARD OF TRADE", 0, "pos"),
        ("UST 10Y", "UST 10Y NOTE - CHICAGO BOARD OF TRADE", 0, "pos"),
        ("UST BOND", "UST BOND - CHICAGO BOARD OF TRADE", 0, "pos"),
    ]),
    (2, "ECONOMY  (BLS, monthly)", "fred", [
        ("CPI YoY %", "CPIAUCSL", 2), ("CORE CPI %", "CPILFESL", 2),
        ("UNEMPLOY %", "UNRATE", 2), ("PAYROLLS Δk", "PAYEMS", 0),
        ("WAGES YoY %", "CES0500000003", 2),
    ]),

    # ----- COLUMN 3: REAL ESTATE -----
    (3, "APARTMENT OWNERS", "cnbc", [
        ("MAA", "MAA"), ("AVALONBAY", "AVB"), ("CAMDEN", "CPT"),
    ]),
    (3, "LENDERS", "cnbc", [
        ("ARBOR RLTY", "ABR"), ("STARWOOD", "STWD"), ("LADDER CAP", "LADR"),
    ]),
    (3, "REIT ETFs", "cnbc", [
        ("VNQ", "VNQ"), ("SCHWAB REIT", "SCHH"), ("MORT REIT", "MORT"),
    ]),
    (3, "CREDIT STRESS  (delinq %, qtrly)", "fred", [
        ("CRE LOANS", "DRCRELEXFACBS", 2), ("SF MORTGAGE", "DRSFRMACBS", 2),
        ("CREDIT CARD", "DRCCLACBS", 2), ("CONSUMER", "DRCLACBS", 2),
        ("ALL LOANS", "DRALACBS", 2), ("CC CHARGE-OFF", "CORCCACBS", 2),
    ]),
]

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class Quote:
    label: str
    decimals: int
    unit: str = ""              # "" = number, "pos" = position count (M/k)
    price: float = None
    change: float = None
    pct: float = None
    tdisp: str = "--"           # display string for the time column
    flash_until: float = 0.0    # wall-clock time the flash highlight expires
    flash_up: bool = True
    _last_price: float = None


STATE: dict[str, Quote] = {}
STATUS = {"cnbc": "starting", "binance": "starting", "fred": "off", "cftc": "starting"}

for _col, _title, _prov, _rows in SECTIONS:
    for _r in _rows:
        STATE[_r[1]] = Quote(
            label=_r[0],
            decimals=_r[2] if len(_r) > 2 else 2,
            unit=_r[3] if len(_r) > 3 else "",
        )


# Packaged/distributed builds carry an appname.txt (e.g. Robert's "KESSLER
# TERMINAL"); the Mac dev copy has none. In packaged builds the old per-section
# watchlist + ADD/pick flow is hidden — it's being replaced by a new sector-based
# add. The dev copy keeps the watchlist working.
PACKAGED_BUILD = (Path(__file__).resolve().parent / "appname.txt").exists()


# ---- user-added tickers, filed PER SECTION (persisted, editable) -----------
WATCH_FILE = Path(__file__).resolve().parent / "watchlist.json"


def _load_adds():
    """{section_title: [tickers]}. Migrates a legacy flat list to a WATCHLIST."""
    try:
        d = json.loads(WATCH_FILE.read_text())
        if isinstance(d, list):                       # legacy flat watchlist
            return {"★ WATCHLIST": [str(t).upper() for t in d]} if d else {}
        return {k: [str(t).upper() for t in v] for k, v in d.items() if v}
    except Exception:
        return {}


USER_ADDS = _load_adds()
for _sec, _ts in USER_ADDS.items():
    for _t in _ts:
        STATE.setdefault(_t, Quote(label=_t, decimals=2))


def _save_adds():
    try:
        WATCH_FILE.write_text(json.dumps(USER_ADDS))
    except Exception:
        pass


def addable_sections():
    """Section titles a stock/ETF/crypto can be filed under (ticker sections)."""
    out = []
    for _col, title, prov, _rows in SECTIONS:
        if prov in ("cnbc", "binance") and title not in out:
            out.append(title)
    for extra in USER_ADDS:                           # keep any custom sections
        if extra not in out:
            out.append(extra)
    return out


def add_to_section(ticker, section):
    t = ticker.upper().strip()
    lst = USER_ADDS.setdefault(section, [])
    if not t or t in lst:
        return False
    lst.append(t)
    STATE.setdefault(t, Quote(label=t, decimals=2))
    _save_adds()
    return True


def remove_ticker(ticker):
    """Remove a ticker from whichever user section(s) it was added to."""
    t = ticker.upper().strip()
    removed = False
    for sec, lst in list(USER_ADDS.items()):
        if t in lst:
            lst.remove(t); removed = True
        if not lst:
            del USER_ADDS[sec]
    if removed:
        _save_adds()
    return removed


def all_added():
    return [t for ts in USER_ADDS.values() for t in ts]


TRACKED = set()   # extra tickers polled for live quotes (e.g. portfolio positions)


def track(tickers):
    for t in tickers:
        t = t.upper()
        TRACKED.add(t)
        STATE.setdefault(t, Quote(label=t, decimals=2))


def cnbc_symbols():
    syms = [r[1] for _, _, prov, rows in SECTIONS if prov == "cnbc" for r in rows]
    extra = [t for t in (all_added() + sorted(TRACKED)) if t not in syms]
    return syms + extra


def update_quote(sym, price, change, pct, tdisp):
    q = STATE.get(sym)
    if q is None or price is None:
        return
    if q._last_price is not None and price != q._last_price:
        q.flash_up = price > q._last_price
        q.flash_until = time.time() + FLASH_SECONDS
    q._last_price = price
    q.price, q.change, q.pct, q.tdisp = price, change, pct, tdisp


# ---------------------------------------------------------------------------
# CNBC quote poller (free, no key) — one batched request for all symbols.
# ---------------------------------------------------------------------------

CNBC_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def to_float(s):
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("%", "").replace("+", "")
    if t in ("UNCH", "unch"):
        return 0.0
    try:
        return float(t)
    except ValueError:
        return None


def fmt_quote_time(raw):
    """CNBC last_time is either 'YYYY-MM-DD' (market closed) or ISO w/ 'T'."""
    if not raw:
        return "--"
    raw = str(raw)
    if "T" in raw:
        return raw.split("T", 1)[1][:8]      # HH:MM:SS
    return raw[5:].replace("-", "/")          # MM/DD


CNBC_CHUNK = 30   # CNBC can drop symbols from very large batches; chunk to be safe


async def cnbc_fetch_chunk(session, batch):
    params = {
        "symbols": "|".join(batch),
        "requestMethod": "itv", "noform": "1", "fund": "1",
        "exthrs": "1", "output": "json",
    }
    async with session.get(
        CNBC_URL, params=params, timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json(content_type=None)
    for q in data["FormattedQuoteResult"]["FormattedQuote"]:
        price = to_float(q.get("last"))
        if price is None:
            continue
        chg = to_float(q.get("change"))
        chg = 0.0 if chg is None else chg
        prev = price - chg
        pct = (chg / prev * 100) if prev else 0.0
        update_quote(q.get("symbol"), price, chg, pct, fmt_quote_time(q.get("last_time")))


async def cnbc_loop():
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            while True:
                syms = cnbc_symbols()          # re-read so added tickers are polled
                chunks = [syms[i:i + CNBC_CHUNK] for i in range(0, len(syms), CNBC_CHUNK)]
                try:
                    results = await asyncio.gather(
                        *(cnbc_fetch_chunk(session, c) for c in chunks),
                        return_exceptions=True,
                    )
                    errs = [r for r in results if isinstance(r, Exception)]
                    STATUS["cnbc"] = "ok" if not errs else f"partial({len(errs)})"
                except Exception as e:
                    STATUS["cnbc"] = f"err:{type(e).__name__}"
                await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# Binance websocket (free, no key) — real-time 24h ticker stream.
# data.binance.vision is the market-data host (not geo-blocked in the US).
# ---------------------------------------------------------------------------


async def binance_loop():
    syms = [r[1] for _, _, prov, rows in SECTIONS if prov == "binance" for r in rows]
    if not syms:
        return
    streams = "/".join(f"{s}@ticker" for s in syms)
    url = f"wss://data-stream.binance.vision/stream?streams={streams}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                STATUS["binance"] = "ok"
                async for raw in ws:
                    d = json.loads(raw).get("data", {})
                    sym = d.get("s", "").lower()
                    if not sym or "c" not in d:
                        continue
                    tdisp = time.strftime("%H:%M:%S",
                                          time.localtime(d.get("E", 0) / 1000))
                    update_quote(sym, float(d["c"]), float(d["p"]), float(d["P"]), tdisp)
        except asyncio.CancelledError:
            return
        except Exception as e:
            STATUS["binance"] = f"reconnecting({type(e).__name__})"
            await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# FRED (St. Louis Fed) — credit spreads, breakevens, curve. Free API key:
#   https://fredaccount.stlouisfed.org/apikeys  (instant, no payment)
# Put it in env FRED_API_KEY, or in a file ~/markets-dashboard/.fred_key
# ---------------------------------------------------------------------------

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def load_fred_key():
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        return key
    f = Path(__file__).resolve().parent / ".fred_key"
    if f.exists():
        return f.read_text().strip()
    return None


FRED_UNITS = {"CPIAUCSL": "pc1", "CPILFESL": "pc1", "PCEPI": "pc1",
              "PCEPILFE": "pc1", "CES0500000003": "pc1", "PAYEMS": "chg"}


async def fred_fetch_one(session, sid, key):
    params = {"series_id": sid, "api_key": key, "file_type": "json",
              "sort_order": "desc", "limit": "5", "units": FRED_UNITS.get(sid, "lin")}
    async with session.get(FRED_URL, params=params,
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        data = await resp.json(content_type=None)
    obs = [(o["date"], float(o["value"])) for o in data.get("observations", [])
           if o.get("value") not in (".", "", None)]
    if not obs:
        return
    mult = 100.0 if sid.startswith("BAML") else 1.0   # OAS percent -> bps
    date, val = obs[0]
    prev = obs[1][1] if len(obs) > 1 else val
    price = val * mult
    chg = (val - prev) * mult
    pct = (chg / (prev * mult) * 100) if prev else 0.0
    update_quote(sid, price, chg, pct, date[5:].replace("-", "/"))


async def fred_loop():
    sids = [r[1] for _, _, prov, rows in SECTIONS if prov == "fred" for r in rows]
    if not sids:
        return
    key = load_fred_key()
    if not key:
        STATUS["fred"] = "NO KEY"
        for sid in sids:
            STATE[sid].tdisp = "need key"
        return
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            while True:
                try:
                    res = await asyncio.gather(
                        *(fred_fetch_one(session, s, key) for s in sids),
                        return_exceptions=True)
                    errs = [r for r in res if isinstance(r, Exception)]
                    STATUS["fred"] = "ok" if not errs else f"partial({len(errs)})"
                except Exception as e:
                    STATUS["fred"] = f"err:{type(e).__name__}"
                await asyncio.sleep(FRED_SECONDS)
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# CFTC Commitments of Traders — leveraged-fund net positioning (free, weekly).
# ---------------------------------------------------------------------------

CFTC_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"


async def cftc_fetch_one(session, contract):
    params = {
        "$where": f"market_and_exchange_names = '{contract}'",
        "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": "2",
        "$select": "report_date_as_yyyy_mm_dd,lev_money_positions_long,"
                   "lev_money_positions_short",
    }
    async with session.get(CFTC_URL, params=params,
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        rows = await resp.json(content_type=None)
    if not rows:
        return

    def net(r):
        return int(r["lev_money_positions_long"]) - int(r["lev_money_positions_short"])

    cur = net(rows[0])
    prev = net(rows[1]) if len(rows) > 1 else cur
    date = rows[0]["report_date_as_yyyy_mm_dd"][:10]
    update_quote(contract, cur, cur - prev, 0.0, date[5:].replace("-", "/"))


async def cftc_loop():
    contracts = [r[1] for _, _, prov, rows in SECTIONS if prov == "cftc" for r in rows]
    if not contracts:
        return
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            while True:
                try:
                    res = await asyncio.gather(
                        *(cftc_fetch_one(session, c) for c in contracts),
                        return_exceptions=True)
                    errs = [r for r in res if isinstance(r, Exception)]
                    STATUS["cftc"] = "ok" if not errs else f"partial({len(errs)})"
                except Exception as e:
                    STATUS["cftc"] = f"err:{type(e).__name__}"
                await asyncio.sleep(CFTC_SECONDS)
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

AMBER = "orange1"
YELLOW = "#ffd400"        # ticker-label colour on the monitor (clickable + not)
DIM = "grey50"


def fmt_num(v, decimals, signed=False):
    if v is None:
        return "--"
    return format(v, f"{'+' if signed else ''},.{decimals}f")


def fmt_pos(v, signed=False):
    if v is None:
        return "--"
    sign = "+" if signed and v >= 0 else ("-" if v < 0 else "")
    a = abs(v)
    if a >= 1_000_000:
        return f"{sign}{a / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"{sign}{a / 1_000:.0f}k"
    return f"{sign}{a:.0f}"


# monitor symbol -> a command the terminal can open on click (chart "GP")
_INDEX_CLICK = {".DJI": "DOW", ".SPX": "SPX", ".IXIC": "NASDAQ", ".RUT": "RUT"}

# Futures / FX / world-index monitor symbols -> a tracking ETF the chart engine
# CAN plot. The free feeds don't chart futures directly (they return no data), so
# clicking a commodity/FX/world-index row opens its closest liquid ETF proxy
# instead (e.g. GOLD @GC.1 -> GLD, COPPER @HG.1 -> CPER, DAX .GDAXI -> EWG).
# (Only symbols whose proxy actually returns chart data are listed; coffee/cotton/
# cocoa/CNY/RUB have no live free-chart ETF, so they stay non-clickable.)
_PROXY_CLICK = {
    "@CL.1": "USO", "@BZ.1": "BNO", "@NG.1": "UNG", "@RB.1": "UGA", "@GC.1": "GLD",
    "@SI.1": "SLV", "@PL.1": "PPLT", "@PA.1": "PALL", "@HG.1": "CPER",
    "@C.1": "CORN", "@W.1": "WEAT", "@S.1": "SOYB", "@SB.1": "CANE",
    "@DJ.1": "DIA", "@SP.1": "SPY", "@ND.1": "QQQ",
    "@TU.1": "SHY", "@FV.1": "IEI", "@TY.1": "IEF", "@TN.1": "IEF", "@US.1": "TLT",
    ".BCOM": "DJP", ".SPGSCI": "GSG", ".VIX": "VIXY", ".TRAN": "IYT",
    ".FTSE": "EWU", ".GDAXI": "EWG", ".FCHI": "EWQ", ".STOXX50E": "FEZ",
    ".IBEX": "EWP", ".FTMIB": "EWI", ".SSMI": "EWL", ".N225": "EWJ",
    ".HSI": "EWH", ".SSEC": "MCHI", ".KS11": "EWY",
    ".DXY": "UUP", "EUR=": "FXE", "JPY=": "FXY", "GBP=": "FXB",
    "CAD=": "FXC", "AUD=": "FXA",
}

# reverse the FRED alias table (series id -> a chartable alias), so clicking a
# credit/inflation/economy row opens the right chart instead of the raw series id.
import terminal_data as _td
_FRED_REV = {}
for _alias, _v in _td.FRED_CHART.items():
    _FRED_REV.setdefault(_v[0], _alias)


def _click_cmd(sym, provider="cnbc"):
    """Map a monitor symbol to a clickable command, or None if not openable."""
    if provider == "binance" or sym.endswith("usdt"):       # crypto -> BTC GP
        return sym.upper().replace("USDT", "") + " GP"
    if provider == "cftc":                                  # CoT positioning: no chart
        return None
    if provider == "fred":                                  # HY OAS -> HY GP, etc.
        a = _FRED_REV.get(sym)
        return (a + " GP") if a else None
    if sym in _INDEX_CLICK:                                 # US index -> DOW GP
        return _INDEX_CLICK[sym] + " GP"
    if sym in _PROXY_CLICK:                                 # futures/FX/world idx -> ETF proxy
        return _PROXY_CLICK[sym] + " GP"
    if sym.startswith("US") and sym[2:-1].isdigit() and sym[-1] in "MY":
        return sym + " GP"                                  # treasury yield -> US10Y GP
    if sym[:1] in ".@" or sym.endswith("="):                # unmapped FX/futures/world idx
        return None
    return sym                                              # stock / ETF -> detail page


def render_section(title, rows, provider="cnbc"):
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(justify="left", no_wrap=True, ratio=4)    # label
    t.add_column(justify="right", no_wrap=True, ratio=4)   # price
    t.add_column(justify="right", no_wrap=True, ratio=3)   # change
    t.add_column(justify="right", no_wrap=True, ratio=3)   # pct
    t.add_column(justify="right", no_wrap=True, ratio=3)   # time

    now = time.time()
    for r in rows:
        q = STATE[r[1]]
        up = q.change is not None and q.change >= 0
        col = "green3" if up else "red3"

        if q.unit == "pos":
            price_str = fmt_pos(q.price)
            chg_str = fmt_pos(q.change, signed=True)
            pct_str = "--"
        else:
            price_str = fmt_num(q.price, q.decimals)
            chg_str = fmt_num(q.change, q.decimals, signed=True)
            pct_str = (fmt_num(q.pct, 2, signed=True) + "%") if q.pct is not None else "--"

        price_txt = Text(price_str)
        if now < q.flash_until:
            price_txt.stylize("bold black on green3" if q.flash_up else "bold white on red3")
        else:
            price_txt.stylize("bold white")

        cmd = _click_cmd(r[1], provider)
        if cmd:
            lbl = Text(q.label, style=Style(color=YELLOW, bold=False,
                       meta={"@click": f"app.open_ticker({cmd!r})"}))
        else:
            lbl = Text(q.label, style=YELLOW)

        t.add_row(
            lbl,
            price_txt,
            Text(chg_str, style=col),
            Text(pct_str, style=col),
            Text(q.tdisp, style=DIM),
        )

    header = Text(f" {title}", style=f"bold {AMBER} on grey15")
    header.pad_right(200)
    return Group(header, t, Text(""))


# ---- Bloomberg headlines (packaged build only, read-only, lower-right) ------
BLOOMBERG_HEADLINES = []        # list of {"title", "age"} kept fresh by bloomberg_loop
BLOOMBERG_QUERY = "site:bloomberg.com markets OR economy OR stocks"


def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def render_bloomberg(headlines):
    """Read-only Bloomberg headline ticker (no @click meta -> not clickable)."""
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(justify="left", no_wrap=True)
    for h in headlines:
        line = Text()
        line.append("• ", style=AMBER)
        line.append(_clip(h.get("title", ""), 46), style="grey85")
        if h.get("age"):
            line.append(f"  {h['age']}", style=DIM)
        t.add_row(line)
    header = Text(" BLOOMBERG", style=f"bold {AMBER} on grey15")
    header.pad_right(200)
    return Group(header, t, Text(""))


async def bloomberg_loop():
    """Refresh Bloomberg headlines for the monitor (shipped builds only)."""
    if not PACKAGED_BUILD:
        return
    try:
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    sec = await _td._fetch_section(
                        session, "BLOOMBERG", "google", BLOOMBERG_QUERY, 12)
                    items = sec.get("items") or []
                    if items:
                        BLOOMBERG_HEADLINES[:] = [
                            {"title": it.get("title", ""), "age": it.get("age", "")}
                            for it in items]
                except Exception:
                    pass
                await asyncio.sleep(120)        # headlines refresh every 2 min
    except asyncio.CancelledError:
        return


def render():
    ncols = max(col for col, *_ in SECTIONS) + 1
    columns = {i: [] for i in range(ncols)}
    used = set()
    show_adds = not PACKAGED_BUILD                          # watchlist hidden in shipped builds
    for col, title, _prov, rows in SECTIONS:
        extra = [(t, t) for t in USER_ADDS.get(title, [])] if show_adds else []
        used.add(title)
        columns[col].append(render_section(title, list(rows) + extra, _prov))
    # any custom sections that aren't part of the built-in layout -> column 0 top
    if show_adds:
        for title, ts in USER_ADDS.items():
            if title not in used and ts:
                columns[0].insert(0, render_section(title, [(t, t) for t in ts], "cnbc"))

    # Bloomberg headlines (shipped builds only) fill the lower-right free area
    if PACKAGED_BUILD and BLOOMBERG_HEADLINES:
        columns[ncols - 1].append(render_bloomberg(BLOOMBERG_HEADLINES))

    grid = Table.grid(expand=True, padding=(0, 2))
    for _ in range(ncols):
        grid.add_column(ratio=1)
    grid.add_row(*[Group(*columns[i]) for i in range(ncols)])

    clock = time.strftime("%a %d %b %Y  %H:%M:%S", time.localtime())
    title_bar = Text()
    title_bar.append("  MARKET MONITOR  ", style="bold black on orange1")
    title_bar.append(f"   {clock}", style="bold white")
    title_bar.append(
        f"      cnbc:{STATUS['cnbc']}  binance:{STATUS['binance']}  "
        f"fred:{STATUS['fred']}  cftc:{STATUS['cftc']}", style=DIM)

    legend = Text(
        f"  click a ticker to open its chart · live crypto (Binance ws) · "
        f"stocks/fx/commodities/yields {POLL_SECONDS}s (CNBC) · "
        f"credit/inflation daily (FRED) · positioning weekly (CFTC) · Ctrl-C to quit",
        style=DIM,
    )
    return Group(title_bar, Text(""), grid, legend)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    asyncio.create_task(cnbc_loop())
    asyncio.create_task(binance_loop())
    asyncio.create_task(fred_loop())
    asyncio.create_task(cftc_loop())
    with Live(render(), refresh_per_second=8, screen=True) as live:
        while True:
            live.update(render())
            await asyncio.sleep(0.2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
