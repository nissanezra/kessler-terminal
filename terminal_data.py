"""
Data engine for the terminal app: fundamentals, price history, company
financials, news, and technical indicators. All sources are free / no-key:

  * Fundamentals  -> CNBC quote webservice (fund=1)
  * Daily history -> StockAnalysis.com (equities/ETFs) · Binance (crypto)
  * Financials    -> SEC EDGAR XBRL company facts
  * News          -> free RSS feeds
"""

import asyncio
import html as _html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
SEC_UA = {"User-Agent": "markets-dashboard personal use contact@example.com"}

CRYPTO = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt", "XRP": "xrpusdt",
          "DOGE": "dogeusdt", "ADA": "adausdt", "AVAX": "avaxusdt", "LINK": "linkusdt",
          "BNB": "bnbusdt", "MATIC": "maticusdt", "DOT": "dotusdt", "LTC": "ltcusdt"}


def is_crypto(ticker):
    t = ticker.upper().replace("-USD", "").replace("USDT", "")
    return t in CRYPTO


def binance_pair(ticker):
    t = ticker.upper().replace("-USD", "").replace("USDT", "")
    return CRYPTO.get(t, t.lower() + "usdt")


# ---------------------------------------------------------------------------
# Fundamentals (CNBC)
# ---------------------------------------------------------------------------

CNBC_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"

# CNBC field -> friendly label, for the detail view
FUND_FIELDS = [
    ("name", "Name"), ("exchange", "Exchange"), ("last", "Last"),
    ("change", "Change"), ("change_pct", "Change %"),
    ("open", "Open"), ("high", "Day High"), ("low", "Day Low"),
    ("yrhiprice", "52wk High"), ("yrloprice", "52wk Low"),
    ("volume_alt", "Volume"), ("tendayavgvol", "10d Avg Vol"),
    ("mktcapView", "Market Cap"), ("pe", "P/E (ttm)"), ("fpe", "P/E (fwd)"),
    ("eps", "EPS (ttm)"), ("feps", "EPS (fwd)"), ("psales", "Price/Sales"),
    ("revenuettm", "Revenue (ttm)"), ("GROSMGNTTM", "Gross Margin"),
    ("NETPROFTTM", "Net Margin"), ("ROETTM", "ROE"), ("DEBTEQTYQ", "Debt/Equity"),
    ("TTMEBITD", "EBITDA (ttm)"), ("beta", "Beta"),
    ("dividend", "Dividend"), ("dividendyield", "Div Yield"),
    ("sharesout", "Shares Out"),
]


async def fetch_fundamentals(session, ticker):
    params = {"symbols": ticker.upper(), "requestMethod": "itv", "noform": "1",
              "fund": "1", "exthrs": "1", "output": "json"}
    async with session.get(CNBC_URL, params=params, headers=UA,
                           timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json(content_type=None)
    items = data.get("FormattedQuoteResult", {}).get("FormattedQuote", [])
    if not items or items[0].get("last") is None:
        return None
    q = items[0]
    out = {"_symbol": q.get("symbol", ticker.upper()),
           "_changetype": q.get("changetype", "")}
    for key, label in FUND_FIELDS:
        if key in q and q[key] not in (None, ""):
            out[label] = str(q[key])
    return out


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

SA_URL = "https://stockanalysis.com/api/symbol/s/{t}/history"
BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines"
NASDAQ_CHART = "https://api.nasdaq.com/api/quote/{t}/chart"
NASDAQ_HDR = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
              "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}

TF_ORDER = ["1D", "1W", "1M", "3M", "6M", "1Y", "5Y", "10Y", "ALL"]

# crypto (Binance): timeframe -> (interval, limit)
BINANCE_TF = {"1D": ("5m", 288), "1W": ("1h", 168), "1M": ("4h", 180),
              "3M": ("1d", 90), "6M": ("1d", 180), "1Y": ("1d", 365),
              "5Y": ("1w", 260), "10Y": ("1w", 520), "ALL": ("1w", 1000)}
# stocks (StockAnalysis): timeframe -> (api_range, tail) ; tail slices the result
SA_TF = {"1W": ("3M", 5), "1M": ("3M", 22), "3M": ("3M", None), "6M": ("6M", None),
         "1Y": ("1Y", None), "5Y": ("5Y", None), "10Y": ("10Y", None),
         "ALL": ("10Y", None)}


def _intraday_label(interval):
    return ("m" in interval or "h" in interval)


async def _binance_hist(session, ticker, tf):
    interval, limit = BINANCE_TF.get(tf, ("1d", 365))
    params = {"symbol": binance_pair(ticker).upper(), "interval": interval, "limit": limit}
    async with session.get(BINANCE_KLINES, params=params,
                           timeout=aiohttp.ClientTimeout(total=10)) as r:
        rows = await r.json(content_type=None)
    fmt = "%m-%d %H:%M" if _intraday_label(interval) else "%Y-%m-%d"
    bars = []
    for k in rows:
        t = datetime.utcfromtimestamp(k[0] / 1000).strftime(fmt)
        bars.append({"t": t, "o": float(k[1]), "h": float(k[2]),
                     "l": float(k[3]), "c": float(k[4])})
    return bars


async def _binance_custom(session, ticker, fromdate, todate):
    start = int(datetime.strptime(fromdate, "%Y-%m-%d").timestamp() * 1000)
    end = int(datetime.strptime(todate, "%Y-%m-%d").timestamp() * 1000)
    interval = "1d" if (end - start) / 86_400_000 <= 1000 else "1w"
    params = {"symbol": binance_pair(ticker).upper(), "interval": interval,
              "startTime": start, "endTime": end, "limit": 1000}
    async with session.get(BINANCE_KLINES, params=params,
                           timeout=aiohttp.ClientTimeout(total=10)) as r:
        rows = await r.json(content_type=None)
    return [{"t": datetime.utcfromtimestamp(k[0] / 1000).strftime("%Y-%m-%d"),
             "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])}
            for k in rows]


async def _sa_hist(session, ticker, sarange, tail=None):
    params = {"range": sarange, "period": "Daily"}
    async with session.get(SA_URL.format(t=ticker.upper()), params=params,
                           headers=UA, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json(content_type=None)
    out = []
    for b in data.get("data", []):
        try:
            out.append({"t": b["t"][:10], "o": float(b["o"]), "h": float(b["h"]),
                        "l": float(b["l"]), "c": float(b["c"])})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda b: b["t"])   # StockAnalysis returns newest-first
    return out[-tail:] if tail else out


async def _nasdaq_chart(session, ticker, params):
    """GET the Nasdaq chart, trying stock then ETF asset class. Returns raw points."""
    for ac in ("stocks", "etf"):
        try:
            async with session.get(NASDAQ_CHART.format(t=ticker.upper()),
                                   params={**params, "assetclass": ac}, headers=NASDAQ_HDR,
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json(content_type=None)
        except Exception:
            continue
        pts = ((data or {}).get("data") or {}).get("chart") or []
        if pts:
            return pts
    return []


async def _nasdaq_intraday(session, ticker):
    bars = []
    for p in await _nasdaq_chart(session, ticker, {}):
        try:
            t = p["z"]["dateTime"].replace(" ET", "")
            bars.append({"t": t, "c": float(p["y"])})
        except (KeyError, TypeError, ValueError):
            continue
    return bars


async def _nasdaq_daily(session, ticker, fromdate, todate):
    bars = []
    for p in await _nasdaq_chart(session, ticker, {"fromdate": fromdate, "todate": todate}):
        try:
            z = p.get("z", {})
            raw = p["z"]["dateTime"]
            try:                                  # normalise M/D/YYYY -> YYYY-MM-DD
                label = datetime.strptime(raw, "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                label = raw
            bars.append({"t": label, "c": float(p["y"]),
                         "o": float(z.get("open", p["y"])), "h": float(z.get("high", p["y"])),
                         "l": float(z.get("low", p["y"]))})
        except (KeyError, TypeError, ValueError):
            continue
    return bars


# Index aliases. kind "nasdaq" = real index level via Nasdaq; "proxy" = an ETF
# that tracks it (Dow/S&P/Russell have no free index-level feed). label is shown.
INDEX_ALIAS = {
    "DOW": ("proxy", "DIA", "DOW JONES · DIA"), "DJIA": ("proxy", "DIA", "DOW JONES · DIA"),
    "DJI": ("proxy", "DIA", "DOW JONES · DIA"), ".DJI": ("proxy", "DIA", "DOW JONES · DIA"),
    "INDU": ("proxy", "DIA", "DOW JONES · DIA"),
    "SPX": ("proxy", "SPY", "S&P 500 · SPY"), "SP500": ("proxy", "SPY", "S&P 500 · SPY"),
    "GSPC": ("proxy", "SPY", "S&P 500 · SPY"), ".SPX": ("proxy", "SPY", "S&P 500 · SPY"),
    "SPX500": ("proxy", "SPY", "S&P 500 · SPY"),
    "RUT": ("proxy", "IWM", "RUSSELL 2000 · IWM"), ".RUT": ("proxy", "IWM", "RUSSELL 2000 · IWM"),
    "RUSSELL": ("proxy", "IWM", "RUSSELL 2000 · IWM"),
    "NASDAQ": ("nasdaq", "COMP", "NASDAQ COMPOSITE"),
    "COMP": ("nasdaq", "COMP", "NASDAQ COMPOSITE"),
    "IXIC": ("nasdaq", "COMP", "NASDAQ COMPOSITE"), ".IXIC": ("nasdaq", "COMP", "NASDAQ COMPOSITE"),
    "NASDAQCOMP": ("nasdaq", "COMP", "NASDAQ COMPOSITE"),
    "NDX": ("nasdaq", "NDX", "NASDAQ 100"), "NASDAQ100": ("nasdaq", "NDX", "NASDAQ 100"),
    "SOX": ("nasdaq", "SOX", "PHLX SEMICONDUCTOR"), "SOXX": ("nasdaq", "SOX", "PHLX SEMICONDUCTOR"),
}

_TF_DAYS = {"1D": 14, "1W": 14, "1M": 35, "3M": 100, "6M": 190, "1Y": 380,
            "5Y": 1900, "10Y": 3800}


def resolve_index(ticker):
    return INDEX_ALIAS.get(ticker.upper())


# ---- FRED rate / yield charting (Fed funds, Treasury yields, curve spreads) ----
FRED_OBS = "https://api.stlouisfed.org/fred/series/observations"
_FRED_KEY = None


def _fred_key():
    global _FRED_KEY
    if _FRED_KEY is None:
        import os
        from pathlib import Path
        _FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
        if not _FRED_KEY:
            f = Path(__file__).resolve().parent / ".fred_key"
            _FRED_KEY = f.read_text().strip() if f.exists() else ""
    return _FRED_KEY


FRED_CHART = {
    "FEDFUNDS": ("DFF", "FED FUNDS RATE %"), "FFR": ("DFF", "FED FUNDS RATE %"),
    "DFF": ("DFF", "FED FUNDS RATE %"), "FED": ("DFF", "FED FUNDS RATE %"),
    "SOFR": ("SOFR", "SOFR %"),
    "US1M": ("DGS1MO", "US 1M YIELD %"), "US3M": ("DGS3MO", "US 3M YIELD %"),
    "US6M": ("DGS6MO", "US 6M YIELD %"), "US1Y": ("DGS1", "US 1Y YIELD %"),
    "US2Y": ("DGS2", "US 2Y YIELD %"), "US3Y": ("DGS3", "US 3Y YIELD %"),
    "US5Y": ("DGS5", "US 5Y YIELD %"), "US7Y": ("DGS7", "US 7Y YIELD %"),
    "US10Y": ("DGS10", "US 10Y YIELD %"), "US20Y": ("DGS20", "US 20Y YIELD %"),
    "US30Y": ("DGS30", "US 30Y YIELD %"),
    "2S10S": ("T10Y2Y", "2s10s SPREAD %"), "10Y2Y": ("T10Y2Y", "2s10s SPREAD %"),
    "10Y3M": ("T10Y3M", "10Y-3M SPREAD %"),
    # --- credit spreads (ICE BofA OAS, shown in bps) ---
    "HY": ("BAMLH0A0HYM2", "HY OAS (bps)"), "HYOAS": ("BAMLH0A0HYM2", "HY OAS (bps)"),
    "IG": ("BAMLC0A0CM", "IG OAS (bps)"), "IGOAS": ("BAMLC0A0CM", "IG OAS (bps)"),
    "CCC": ("BAMLH0A3HYC", "CCC OAS (bps)"), "CCCOAS": ("BAMLH0A3HYC", "CCC OAS (bps)"),
    "EMOAS": ("BAMLEMCBPIOAS", "EM OAS (bps)"),
    # deep-history credit spread (Moody's Baa − 10Y, since 1986, no license limit)
    "CREDIT": ("BAA10Y", "Baa CREDIT SPREAD (bps)"),
    "BAASPREAD": ("BAA10Y", "Baa CREDIT SPREAD (bps)"),
    "AAASPREAD": ("AAA10Y", "Aaa SPREAD (bps)"),
    # --- credit stress: loan delinquency & charge-off rates (Fed, quarterly) ---
    "DELINQ": ("DRALACBS", "DELINQUENCY · ALL LOANS %"),
    "DELINQ-ALL": ("DRALACBS", "DELINQUENCY · ALL LOANS %"),
    "DELINQ-CRE": ("DRCRELEXFACBS", "DELINQUENCY · CRE %"),
    "CRE-DELINQ": ("DRCRELEXFACBS", "DELINQUENCY · CRE %"),
    "DELINQ-MORT": ("DRSFRMACBS", "DELINQUENCY · SF MORTGAGE %"),
    "DELINQ-MTG": ("DRSFRMACBS", "DELINQUENCY · SF MORTGAGE %"),
    "DELINQ-CC": ("DRCCLACBS", "DELINQUENCY · CREDIT CARD %"),
    "DELINQ-CONS": ("DRCLACBS", "DELINQUENCY · CONSUMER %"),
    "DELINQ-BIZ": ("DRBLACBS", "DELINQUENCY · BUSINESS %"),
    "CHARGEOFF": ("CORALACBS", "CHARGE-OFF · ALL LOANS %"),
    "CHARGEOFF-ALL": ("CORALACBS", "CHARGE-OFF · ALL LOANS %"),
    "CHARGEOFF-CC": ("CORCCACBS", "CHARGE-OFF · CREDIT CARD %"),
    "CHARGEOFF-CRE": ("CORCREXFACBS", "CHARGE-OFF · CRE %"),
    # --- BLS economic data (via FRED; CPI/PCE/wages YoY %, payrolls MoM Δ) ---
    "CPI": ("CPIAUCSL", "CPI YoY %"), "INFLATION": ("CPIAUCSL", "CPI YoY %"),
    "CORECPI": ("CPILFESL", "CORE CPI YoY %"),
    "PCE": ("PCEPI", "PCE YoY %"), "COREPCE": ("PCEPILFE", "CORE PCE YoY %"),
    "WAGES": ("CES0500000003", "AVG HOURLY EARNINGS YoY %"),
    "UNEMPLOYMENT": ("UNRATE", "UNEMPLOYMENT %"), "UNRATE": ("UNRATE", "UNEMPLOYMENT %"),
    "PAYROLLS": ("PAYEMS", "NONFARM PAYROLLS MoM (k)"),
    "NFP": ("PAYEMS", "NONFARM PAYROLLS MoM (k)"), "JOBS": ("PAYEMS", "NONFARM PAYROLLS MoM (k)"),
}

FRED_UNITS = {
    "CPIAUCSL": "pc1", "CPILFESL": "pc1", "PCEPI": "pc1", "PCEPILFE": "pc1",
    "CES0500000003": "pc1", "PAYEMS": "chg",
}


def resolve_fred(ticker):
    return FRED_CHART.get(ticker.upper())


async def _fred_hist(session, series_id, tf, custom=None):
    key = _fred_key()
    if not key:
        return []
    if custom:
        start, end = custom
    else:
        start = ("1950-01-01" if tf == "ALL"
                 else (datetime.now() - timedelta(days=_TF_DAYS.get(tf, 380))).strftime("%Y-%m-%d"))
        end = datetime.now().strftime("%Y-%m-%d")
    params = {"series_id": series_id, "api_key": key, "file_type": "json",
              "observation_start": start, "observation_end": end, "sort_order": "asc",
              "units": FRED_UNITS.get(series_id, "lin")}   # pc1 = YoY%, chg = MoM change
    async with session.get(FRED_OBS, params=params,
                           timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json(content_type=None)
    mult = 100.0 if series_id.startswith("BAML") or series_id in ("BAA10Y", "AAA10Y") else 1.0
    bars = []
    for o in data.get("observations", []):
        v = o.get("value")
        if v in (".", "", None):
            continue
        try:
            bars.append({"t": o["date"], "c": float(v) * mult})
        except (ValueError, KeyError):
            continue
    return bars


# ---- TreasuryDirect auction results (free, public) ----
TD_AUCTIONS = "https://www.treasurydirect.gov/TA_WS/securities/auctioned"


async def fetch_auctions(session, days=150, limit=25):
    """Recent US Treasury auction results (most recent first)."""
    async with session.get(TD_AUCTIONS, params={"days": str(days)},
                           timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json(content_type=None)
    out = []
    for a in data:
        y = (a.get("highYield") or a.get("highInvestmentRate")
             or a.get("highDiscountRate") or a.get("interestRate") or "")
        out.append({
            "date": (a.get("auctionDate") or "")[:10],
            "type": a.get("securityType", ""),
            "term": a.get("securityTerm", ""),
            "yield": y,
            "btc": a.get("bidToCoverRatio", ""),
            "offering": a.get("offeringAmount", ""),
            "cusip": a.get("cusip", ""),
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:limit]


async def _nasdaq_index_hist(session, symbol, tf, custom=None):
    if custom:
        fromdate, today = custom
    else:
        fromdate = ("1970-01-01" if tf == "ALL"
                    else (datetime.now() - timedelta(days=_TF_DAYS.get(tf, 380))).strftime("%Y-%m-%d"))
        today = datetime.now().strftime("%Y-%m-%d")
    async with session.get(NASDAQ_CHART.format(t=symbol),
                           params={"assetclass": "index", "fromdate": fromdate, "todate": today},
                           headers=NASDAQ_HDR, timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json(content_type=None)
    bars = []
    for p in ((data or {}).get("data", {}) or {}).get("chart") or []:
        try:
            raw = p["z"]["dateTime"]
            try:
                label = datetime.strptime(raw, "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                label = raw
            bars.append({"t": label, "c": float(p["y"])})
        except (KeyError, TypeError, ValueError):
            continue
    return bars


async def fetch_history(session, ticker, tf="1Y", custom=None):
    """Return list of bars (dicts with t label and c close; o/h/l when available).

    tf is one of TF_ORDER. `custom=(fromdate, todate)` overrides tf with a daily
    range (YYYY-MM-DD)."""
    fr = resolve_fred(ticker)
    if fr:
        return await _fred_hist(session, fr[0], tf, custom)
    idx = resolve_index(ticker)
    if idx:
        kind, sym, _label = idx
        if kind == "nasdaq":
            return await _nasdaq_index_hist(session, sym, tf, custom)
        ticker = sym   # proxy ETF -> fetch like a normal stock below
    if custom:
        # ticker is already index-resolved above (nasdaq returned, proxy -> ETF symbol)
        if is_crypto(ticker):
            return await _binance_custom(session, ticker, custom[0], custom[1])
        return await _nasdaq_daily(session, ticker, custom[0], custom[1])
    if is_crypto(ticker):
        return await _binance_hist(session, ticker, tf)
    if tf == "1D":
        return await _nasdaq_intraday(session, ticker)
    if tf == "ALL":
        # full available history (StockAnalysis caps at 10Y; Nasdaq goes back decades)
        today = datetime.now().strftime("%Y-%m-%d")
        bars = await _nasdaq_daily(session, ticker, "1970-01-01", today)
        if bars:
            return bars
        # fall back to StockAnalysis 10Y if Nasdaq has nothing (e.g. some indices)
    sarange, tail = SA_TF.get(tf, ("1Y", None))
    return await _sa_hist(session, ticker, sarange, tail)


# ---------------------------------------------------------------------------
# Company financials (SEC EDGAR)
# ---------------------------------------------------------------------------

_CIK_CACHE = {}
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{tax}/{c}.json"
ANNUAL_FORMS = ("10-K", "20-F", "40-F")   # US, foreign-private-issuer, Canadian

# Grouped into the three financial statements (rendered as separate tables).
STATEMENTS = [
    # Each line lists US-GAAP candidates first, then IFRS ("ifrs-full:") fallbacks
    # for foreign filers (20-F). First concept with data wins.
    ("INCOME STATEMENT", {
        "Revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                    "Revenues", "SalesRevenueNet", "ifrs-full:Revenue"],
        "Gross Profit": ["GrossProfit", "ifrs-full:GrossProfit"],
        "Op. Income": ["OperatingIncomeLoss",
                       "ifrs-full:ProfitLossFromOperatingActivities"],
        "Net Income": ["NetIncomeLoss", "ifrs-full:ProfitLoss"],
        "Dil. EPS": ["EarningsPerShareDiluted", "ifrs-full:DilutedEarningsLossPerShare"],
    }),
    ("BALANCE SHEET", {
        "Assets": ["Assets", "ifrs-full:Assets"],
        "Liabilities": ["Liabilities", "ifrs-full:Liabilities"],
        "Equity": ["StockholdersEquity", "ifrs-full:Equity"],
        "Cash & Equiv.": ["CashAndCashEquivalentsAtCarryingValue",
                          "ifrs-full:CashAndCashEquivalents"],
        "Long-Term Debt": ["LongTermDebtNoncurrent", "LongTermDebt",
                           "ifrs-full:NoncurrentBorrowings"],
    }),
    ("CASH FLOW", {
        "Operating CF": ["NetCashProvidedByUsedInOperatingActivities",
                         "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
                         "ifrs-full:CashFlowsFromUsedInOperatingActivities"],
        "Investing CF": ["NetCashProvidedByUsedInInvestingActivities",
                         "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
                         "ifrs-full:CashFlowsFromUsedInInvestingActivities"],
        "Financing CF": ["NetCashProvidedByUsedInFinancingActivities",
                         "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
                         "ifrs-full:CashFlowsFromUsedInFinancingActivities"],
        "CapEx": ["PaymentsToAcquirePropertyPlantAndEquipment",
                  "PaymentsToAcquireProductiveAssets",
                  "ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
        "Dividends Paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends",
                           "ifrs-full:DividendsPaidClassifiedAsFinancingActivities"],
        "Buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    }),
]


async def _load_cik_map(session):
    if _CIK_CACHE:
        return _CIK_CACHE
    async with session.get(SEC_TICKERS, headers=SEC_UA,
                           timeout=aiohttp.ClientTimeout(total=12)) as r:
        data = await r.json(content_type=None)
    for row in data.values():
        _CIK_CACHE[row["ticker"].upper()] = row["cik_str"]
    return _CIK_CACHE


def _annual_year(it):
    """Return the data period's fiscal year for an annual fact, or None.

    SEC's `fy` is the FILING's fiscal year (comparatives share it), so we key by
    the period END date instead. Flows must span ~1 year; instants (balance
    sheet) are taken as-is.
    """
    end = it.get("end")
    if it.get("form") not in ANNUAL_FORMS or not end:
        return None
    start = it.get("start")
    if start and start != end:
        try:
            d0 = datetime.fromisoformat(start); d1 = datetime.fromisoformat(end)
        except ValueError:
            return None
        if not (350 <= (d1 - d0).days <= 380):   # annual flow only
            return None
    return int(end[:4])


def _partial_from_entries(entries, annual):
    """The current (in-progress) fiscal year from filed quarters, or None.
    Flows -> year-to-date cumulative from the latest 10-Q; instants (balance
    sheet) -> latest quarter-end value. Returns (year, value, n_quarters, is_flow)."""
    last_annual = max((y for y, _ in annual), default=None)
    flows = [e for e in entries if e.get("start")]
    if flows:
        qd = [e for e in flows if str(e.get("form", "")).startswith("10-Q")
              and e.get("fy") and e.get("end") and e.get("start")]
        if not qd:
            return None
        maxfy = max(e["fy"] for e in qd)

        def span(e):
            try:
                return (datetime.fromisoformat(e["end"]) - datetime.fromisoformat(e["start"])).days
            except ValueError:
                return 0
        e = max((x for x in qd if x["fy"] == maxfy), key=lambda x: (x["end"], span(x)))
        yr = int(e["end"][:4])
        if last_annual and yr <= last_annual:
            return None
        nq = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}.get(e.get("fp"))
        return (yr, e["val"], nq, True)
    ent = [e for e in entries if e.get("end")]
    if not ent:
        return None
    e = max(ent, key=lambda x: x["end"])
    yr = int(e["end"][:4])
    if last_annual and yr <= last_annual:
        return None
    return (yr, e["val"], None, False)


async def _concept_data(session, cik, candidates):
    """Return (annual_series, partial), MERGED across all candidate tags.
    annual_series = sorted [(end_year, value)] (latest filing wins on collision);
    partial = the most-recent in-progress-year tuple across tags."""
    best = {}            # end_year -> (filing_fy, value)
    best_partial = None  # (year, value, n_quarters, is_flow)
    for c in candidates:
        tax, concept = c.split(":", 1) if ":" in c else ("us-gaap", c)
        try:
            async with session.get(SEC_CONCEPT.format(cik=cik, tax=tax, c=concept),
                                   headers=SEC_UA,
                                   timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status != 200:
                    continue
                data = await r.json(content_type=None)
        except Exception:
            continue
        units = data.get("units", {})
        key = next(iter(units), None)
        if not key:
            continue
        entries = units[key]
        for it in entries:
            yr = _annual_year(it)
            if yr is None:
                continue
            ffy = it.get("fy") or yr
            if yr not in best or ffy > best[yr][0]:
                best[yr] = (ffy, it["val"])
        p = _partial_from_entries(entries, [])   # validity checked after merge
        if p and (best_partial is None or p[0] > best_partial[0]
                  or (p[0] == best_partial[0] and (p[2] or 0) > (best_partial[2] or 0))):
            best_partial = p
    annual = sorted((yr, v) for yr, (ffy, v) in best.items())
    if best_partial and annual and best_partial[0] <= max(y for y, _ in annual):
        best_partial = None      # not actually a new in-progress year
    return annual, best_partial


SA_ETF_HOLD = "https://stockanalysis.com/api/symbol/e/{t}/holdings"
NASDAQ_INST = "https://api.nasdaq.com/api/company/{t}/institutional-holdings"


async def fetch_etf_holdings(session, ticker, top=15):
    """Top constituent holdings of an ETF (name, symbol, weight%). None if not an ETF."""
    try:
        async with session.get(SA_ETF_HOLD.format(t=ticker.upper()), headers=UA,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status != 200:
                return None
            d = await r.json(content_type=None)
        hold = (d.get("data") or {}).get("holdings")
        if not hold:
            return None
        return [{"name": h.get("n"), "symbol": (h.get("s") or "").lstrip("$!").split("/")[-1],
                 "weight": h.get("as")} for h in hold[:top]]
    except Exception:
        return None


async def fetch_institutional_holders(session, ticker, top=15):
    """Top institutional holders of a stock + % institutional ownership."""
    try:
        async with session.get(
            NASDAQ_INST.format(t=ticker.upper()),
            params={"limit": str(top), "type": "TOTAL",
                    "sortColumn": "marketValue", "sortOrder": "DESC"},
            headers=NASDAQ_HDR, timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status != 200:
                return None
            d = await r.json(content_type=None)
        data = d.get("data") or {}
        summ = data.get("ownershipSummary") or {}
        rows = ((data.get("holdingsTransactions") or {}).get("table") or {}).get("rows") or []
        holders = [{"name": h.get("ownerName"), "shares": h.get("sharesHeld"),
                    "value": h.get("marketValue"), "date": h.get("date")} for h in rows[:top]]
        if not holders:
            return None
        return {"inst_pct": (summ.get("SharesOutstandingPCT") or {}).get("value"),
                "holders": holders}
    except Exception:
        return None


async def fetch_financials(session, ticker, years=None):
    cik_map = await _load_cik_map(session)
    cik = cik_map.get(ticker.upper())
    if not cik:
        return None
    # fetch every concept once: annual history + current partial year
    raw, partials = {}, {}
    all_years = set()
    for name, concepts in STATEMENTS:
        for label, cands in concepts.items():
            annual, partial = await _concept_data(session, cik, cands)
            if annual:
                raw[(name, label)] = annual
                all_years.update(y for y, _ in annual)
            if partial:
                partials[(name, label)] = partial
    if not all_years and not partials:
        return None
    # `years=None` -> all available history; otherwise the most recent N years
    window = set(all_years) if not years else set(sorted(all_years)[-years:])

    # the in-progress year column (max partial year) + how many quarters are in it
    pcol = max((p[0] for p in partials.values()), default=None)
    pnq = None
    if pcol is not None:
        for p in partials.values():
            if p[0] == pcol and p[3] and p[2]:
                pnq = p[2]
                break

    def with_partial(name, label, series):
        vals = [(y, v) for y, v in (series or []) if y in window]
        p = partials.get((name, label))
        if p and pcol is not None and p[0] == pcol:
            vals.append((pcol, p[1]))
        return vals

    statements = []
    for name, concepts in STATEMENTS:
        metrics = {}
        for label in concepts:
            vals = with_partial(name, label, raw.get((name, label)))
            if vals:
                metrics[label] = vals
        if name == "CASH FLOW":
            if "Operating CF" in metrics and "CapEx" in metrics:
                capex = dict(metrics["CapEx"])
                fcf = [(yr, ocf - capex[yr]) for yr, ocf in metrics["Operating CF"]
                       if yr in capex]
                if fcf:
                    metrics["Free Cash Flow"] = fcf
            ni = dict(with_partial("INCOME STATEMENT", "Net Income",
                                   raw.get(("INCOME STATEMENT", "Net Income"))))
            if "Dividends Paid" in metrics and ni:
                pr = [(yr, div / ni[yr] * 100) for yr, div in metrics["Dividends Paid"]
                      if ni.get(yr, 0) > 0]
                if pr:
                    metrics["Payout Ratio"] = pr
        if metrics:
            statements.append((name, metrics))
    return ({"cik": cik, "statements": statements, "partial_year": pcol,
             "partial_nq": pnq} if statements else None)


# ---------------------------------------------------------------------------
# News (RSS)
# ---------------------------------------------------------------------------

GOOGLE_NEWS = "https://news.google.com/rss/search"
CNBC_RSS = "https://www.cnbc.com/id/100003114/device/rss/rss.html"
CRYPTO_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP",
                "DOGE": "Dogecoin", "ADA": "Cardano", "AVAX": "Avalanche",
                "LINK": "Chainlink", "BNB": "Binance Coin", "LTC": "Litecoin",
                "DOT": "Polkadot", "MATIC": "Polygon crypto"}


def _parse_rss(text, limit=20):
    items = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        pub = it.findtext("pubDate") or ""
        link = it.findtext("link") or ""
        source = (it.findtext("source") or "").strip()   # Google News: publisher
        if title:
            items.append({"title": title.strip(), "pub": pub.strip(),
                          "link": link.strip(), "source": source})
        if len(items) >= limit:
            break
    return items


def _label_sources(items):
    """Strip the trailing ' - Publisher' Google adds, and default source to CNBC."""
    for it in items:
        src = it.get("source", "")
        if src and it["title"].endswith(f" - {src}"):
            it["title"] = it["title"][: -(len(src) + 3)]
        it["source"] = src.removesuffix(".com") if src else "CNBC"
    return items


async def fetch_news(session, ticker=None, limit=20):
    if ticker:
        if is_crypto(ticker):
            t = ticker.upper().replace("-USD", "").replace("USDT", "")
            query = CRYPTO_NAMES.get(t, t) + " crypto"
        else:
            query = ticker.upper() + " stock"
        url = GOOGLE_NEWS
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    else:
        url, params = CNBC_RSS, {}
    try:
        async with session.get(url, params=params, headers=UA,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            text = await r.text()
        items = _parse_rss(text, limit)
        if items:
            return _label_sources(items)
    except Exception:
        pass
    # fallback to market news
    async with session.get(CNBC_RSS, headers=UA,
                           timeout=aiohttp.ClientTimeout(total=10)) as r:
        return _label_sources(_parse_rss(await r.text(), limit))


# ---- daily market-news dashboard (several CNBC sections at once) -----------

CNBC_RSS_FMT = "https://www.cnbc.com/id/{id}/device/rss/rss.html"
# (heading, source, ref) — source "cnbc" -> section id · "google" -> search query.
# order = display order (alternates left/right column in the board).
NEWS_SECTIONS = [
    ("TOP NEWS", "cnbc", "100003114"),
    ("BLOOMBERG", "google", "site:bloomberg.com markets OR economy OR stocks"),
    ("MARKETS", "cnbc", "15839135"),
    ("ECONOMY", "cnbc", "20910258"),
    ("FINANCE", "cnbc", "10000664"),
    ("TECHNOLOGY", "cnbc", "19854910"),
    ("ENERGY", "cnbc", "19836768"),
    ("CRYPTO", "google", "cryptocurrency markets"),
    ("REAL ESTATE", "google", "real estate market REIT mortgage rates"),
]


def _rss_dt(pub):
    """Parse an RSS pubDate into a tz-aware UTC datetime, or None."""
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(pub, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            continue
    return None


def _rss_age(pub):
    """Turn an RSS pubDate into a compact age like '5m', '3h', '2d' (UTC)."""
    if not pub:
        return ""
    dt = _rss_dt(pub)
    if dt is None:
        return pub[:16]
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        return "now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


async def _fetch_section(session, heading, source, ref, per):
    if source == "google":
        url = GOOGLE_NEWS                 # when:7d keeps the board to the last week
        params = {"q": f"{ref} when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
        limit = 40                        # over-fetch, then keep the newest `per`
    else:
        url, params, limit = CNBC_RSS_FMT.format(id=ref), {}, per
    try:
        async with session.get(url, params=params, headers=UA,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            items = _parse_rss(await r.text(), limit)
    except Exception:
        items = []
    if source == "google":
        items.sort(key=lambda it: _rss_dt(it.get("pub", "")) or datetime.min.replace(
            tzinfo=timezone.utc), reverse=True)             # newest first
        items = items[:per]
    _label_sources(items)                                   # strip " - Publisher" / default CNBC
    for it in items:
        it["age"] = _rss_age(it.get("pub", ""))
    return {"heading": heading, "items": items}


async def fetch_news_dashboard(session, per=7):
    """Fetch every NEWS_SECTIONS feed concurrently. Returns an ordered list of
    {heading, items:[{title, link, pub, age}]} — sections with no items dropped."""
    secs = await asyncio.gather(*[
        _fetch_section(session, h, src, ref, per) for h, src, ref in NEWS_SECTIONS])
    return [s for s in secs if s["items"]]


# ---- in-terminal article reader -------------------------------------------

GNEWS_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


async def _resolve_google_news(session, url):
    """Turn a news.google.com/rss/articles/… redirect into the real publisher URL
    (via Google's batchexecute endpoint). Returns the original url on failure."""
    if "news.google.com" not in url:
        return url
    try:
        async with session.get(url, headers=UA,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            page = await r.text()
        aid = re.search(r'data-n-a-id="([^"]+)"', page).group(1)
        sig = re.search(r'data-n-a-sg="([^"]+)"', page).group(1)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page).group(1)
        inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1,
                "US:en", None, 1, None, None, None, None, None, 0, 1], "X", "X", 1,
                [1, 1, 1], 1, 1, None, 0, 0, None, 0], aid, int(ts), sig])
        body = "f.req=" + quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
        hdr = {**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
        async with session.post(GNEWS_BATCH, data=body, headers=hdr,
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            txt = await r.text()
        m = re.search(r'(https?://(?!news\.google)[^"\\]+)', txt)
        return m.group(1) if m else url
    except Exception:
        return url


def _extract_article(html_text):
    """Pull a readable (title, paragraphs) out of an article page with stdlib only."""
    title = ""
    m = (re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html_text)
         or re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.S)
         or re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S))
    if m:
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        title = re.split(r"\s+[|]\s+", title)[0].strip()   # drop " | Publisher" suffix
    paras, seen = [], set()
    for raw in re.findall(r"<p[^>]*>(.*?)</p>", html_text, re.S):
        txt = _html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        txt = re.sub(r"\s+", " ", txt)
        if len(txt) < 45 or txt in seen:               # too short / duplicate
            continue
        if max((len(w) for w in txt.split()), default=99) > 30:
            continue                                   # glued nav text (no real spaces)
        seen.add(txt)
        paras.append(txt)
        if len(paras) >= 80:
            break
    return title, paras


async def fetch_article(session, url):
    """Resolve (if needed), fetch and extract an article. Returns
    {url, title, paragraphs, paywalled}. paragraphs is [] if nothing readable."""
    real = await _resolve_google_news(session, url)
    try:
        async with session.get(real, headers=UA, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            html_text = await r.text()
            real = str(r.url)
    except Exception:
        return {"url": real, "title": "", "paragraphs": [], "paywalled": False}
    title, paras = _extract_article(html_text)
    paywalled = len(paras) < 3 or sum(len(p) for p in paras) < 600
    return {"url": real, "title": title, "paragraphs": paras, "paywalled": paywalled}


# ---------------------------------------------------------------------------
# Technical indicators (pure python, operate on close-price lists)
# ---------------------------------------------------------------------------


def sma(values, n):
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values, n):
    out = [None] * len(values)
    k = 2 / (n + 1)
    e = None
    for i, v in enumerate(values):
        e = v if e is None else v * k + e * (1 - k)
        out[i] = e
    return out


def rsi(values, n=14):
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100 - 100 / (1 + (ag / al if al else 999))
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100 - 100 / (1 + (ag / al if al else 999))
    return out


def macd(values, fast=12, slow=26, signal=9):
    ef, es = ema(values, fast), ema(values, slow)
    line = [(a - b) if a is not None and b is not None else None for a, b in zip(ef, es)]
    sig = ema([v if v is not None else 0 for v in line], signal)
    hist = [(l - s) if l is not None and s is not None else None for l, s in zip(line, sig)]
    return line, sig, hist


def bollinger(values, n=20, k=2):
    mid = sma(values, n)
    up, lo = [None] * len(values), [None] * len(values)
    for i in range(len(values)):
        if i >= n - 1:
            window = values[i - n + 1:i + 1]
            m = mid[i]
            sd = (sum((x - m) ** 2 for x in window) / n) ** 0.5
            up[i], lo[i] = m + k * sd, m - k * sd
    return up, mid, lo
