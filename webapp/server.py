"""Web-app prototype for the markets terminal.

Reuses the existing data engine (dashboard.py + terminal_data.py) UNCHANGED as the
backend, and serves a single page that mirrors the terminal's design. The point of
the prototype: same look, but charts are now real interactive (browser) charts and
the UI is smooth DOM updates instead of terminal repaints.

Run:  cd ~/markets-dashboard && ./.venv/bin/python webapp/server.py
then open http://127.0.0.1:8787
"""
import asyncio
import calendar
import hashlib
import html
import json
import os
import re
import socket
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # import sibling modules

import dashboard as dash       # noqa: E402
import terminal_data as td     # noqa: E402


# ---- Gemini API key (AI summaries) -----------------------------------------
# Cloud (Fly) supplies it as the env secret GEMINI_API_KEY. Desktop builds read
# it from a local obfuscated dotfile so the shared key never lives in the public
# repo. The dotfile leading-dot keeps the auto-updater's _safe filter from
# touching it (same approach as the Rosenberg creds).
import base64, platform  # noqa: E402

GEMINI_KEY_FILE = HERE / ".gemini_key"


def _gk_xor(data):
    k = hashlib.sha256(("kkt-gemini::" + platform.node()).encode()).digest()
    return bytes(b ^ k[i % len(k)] for i, b in enumerate(data))


def save_gemini_key(key):
    GEMINI_KEY_FILE.write_bytes(base64.b64encode(_gk_xor(key.strip().encode())))


def _gemini_key():
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if k:
        return k
    try:
        return _gk_xor(base64.b64decode(GEMINI_KEY_FILE.read_bytes())).decode().strip()
    except Exception:
        return ""


# ---- research mirror (desktop shows the shared cloud app's reports) ---------
# A family desktop that isn't the subscription source can display the SAME
# Rosenberg reports the shared cloud app already holds, by MIRRORING the files
# from it (rather than the desktop independently logging into the subscription).
# Config = the app URL + its password; stored locally (obfuscated) or via env.
MIRROR_FILE = HERE / ".research_mirror"


def save_mirror_cfg(url, key):
    blob = json.dumps({"url": url.strip().rstrip("/"), "key": key.strip()}).encode()
    MIRROR_FILE.write_bytes(base64.b64encode(_gk_xor(blob)))


def _mirror_cfg():
    url = os.environ.get("RESEARCH_MIRROR_URL", "").strip()
    key = os.environ.get("RESEARCH_MIRROR_KEY", "").strip()
    if url and key:
        return url.rstrip("/"), key
    try:
        d = json.loads(_gk_xor(base64.b64decode(MIRROR_FILE.read_bytes())).decode())
        return d["url"].rstrip("/"), d["key"]
    except Exception:
        return None


def build_monitor():
    """Current monitor state, mirroring dashboard.render()'s layout."""
    ncols = max(c for c, *_ in dash.SECTIONS) + 1
    sections = []
    for col, title, prov, rows in dash.SECTIONS:
        rws = list(rows) + [(t, t) for t in dash.USER_ADDS.get(title, [])]
        rws = [r for r in rws if not dash._row_hidden(r)]
        out_rows = []
        for r in rws:
            q = dash.STATE.get(r[1])
            if not q:
                continue
            up = q.change is not None and q.change >= 0
            if q.unit == "pos":
                price = dash.fmt_pos(q.price)
                chg = dash.fmt_pos(q.change, signed=True)
                pct = "--"
            else:
                price = dash.fmt_num(q.price, q.decimals)
                chg = dash.fmt_num(q.change, q.decimals, signed=True)
                pct = (dash.fmt_num(q.pct, 2, signed=True) + "%") if q.pct is not None else "--"
            out_rows.append({
                "key": title + "|" + q.label,
                "label": q.label, "price": price, "raw": q.price, "chg": chg,
                "pct": pct, "time": q.tdisp, "up": up,
                "cmd": dash._click_cmd(r[1], prov),
            })
        if out_rows:
            sections.append({"col": col, "title": title, "rows": out_rows})
    return {"ncols": ncols, "sections": sections}


async def api_monitor(request):
    return web.json_response(build_monitor())


async def api_ws(request):
    """Live monitor stream: full snapshot on connect, then changed rows are pushed."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    if os.environ.get("MKT_PASSWORD"):     # security: log the active-monitor device too
        _record_seen(request.cookies.get("kkt_name"),
                     _client_ip(request), request.headers.get("User-Agent", ""))
    request.app["ws_clients"].add(ws)
    try:
        await ws.send_json({"type": "full", **build_monitor()})
        async for msg in ws:                       # we don't expect client messages
            if msg.type == web.WSMsgType.ERROR:
                break
    finally:
        request.app["ws_clients"].discard(ws)
    return ws


async def monitor_broadcast(app):
    """Every 0.5s, diff the monitor and push only the rows whose value changed."""
    last = {}
    try:
        while True:
            await asyncio.sleep(0.5)
            mon = build_monitor()
            changed, newlast = [], {}
            for s in mon["sections"]:
                for r in s["rows"]:
                    newlast[r["key"]] = r["raw"]
                    if last.get(r["key"], object()) != r["raw"]:
                        changed.append(r)
            last = newlast
            clients = app["ws_clients"]
            if not clients or not changed:
                continue
            for ws in list(clients):
                try:
                    await ws.send_json({"type": "update", "rows": changed})
                except Exception:
                    clients.discard(ws)
    except asyncio.CancelledError:
        return


async def api_sections(request):
    """Sectors a ticker can be filed under in the live dashboard, plus the ones it's
    already in (so the picker can mark them)."""
    ticker = request.query.get("ticker", "").upper().strip()
    added_in = [s for s, lst in dash.USER_ADDS.items() if ticker in lst]
    return web.json_response({"sections": dash.addable_sections(), "added_in": added_in})


async def api_add(request):
    """Add a ticker under a dashboard sector (persisted; live loops poll it next cycle)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    ticker = str(data.get("ticker", "")).upper().strip()
    section = str(data.get("section", "")).strip()
    if not ticker or not section:
        return web.json_response({"ok": False, "error": "ticker and section required"},
                                 status=400)
    added = dash.add_to_section(ticker, section)
    dash.track([ticker])                       # ensure it's polled even if loops cache
    return web.json_response({"ok": True, "added": added, "already": not added,
                              "ticker": ticker, "section": section})


async def api_chart(request):
    """OHLC/close history + SMA overlays for one ticker (for the browser chart)."""
    s = request.app["session"]
    ticker = request.query.get("ticker", "AAPL").upper()
    tf = request.query.get("tf", "1Y")
    frm, to = request.query.get("from"), request.query.get("to")
    if frm and to:                      # custom date range
        tf = "CUSTOM"
        bars = await td.fetch_history(s, ticker, custom=(frm, to)) or []
    else:
        bars = await td.fetch_history(s, ticker, tf) or []
    bars = [b for b in bars if b.get("t") and b.get("c") is not None]
    # Intraday (1D) bars carry clock-time labels ("4:00 AM"); the browser chart
    # needs numeric UNIX timestamps. Stamp them onto today's date (UTC epoch so the
    # chart renders the literal clock time) and drop any that don't parse.
    if tf == "1D":
        now = datetime.now()
        stamped = []
        for b in bars:
            try:
                t = datetime.strptime(str(b["t"]), "%I:%M %p")
                b["t"] = calendar.timegm((now.year, now.month, now.day,
                                          t.hour, t.minute, 0, 0, 0, 0))
                stamped.append(b)
            except (ValueError, TypeError):
                continue
        bars = stamped
    closes = [b["c"] for b in bars]
    # warmup: ~1yr of prior closes so SMA/RSI are fully formed across the chosen
    # window (not warming up mid-chart). Skipped for 1D intraday.
    warmup = []
    if bars and tf != "1D":
        try:
            d0 = datetime.fromisoformat(str(bars[0]["t"])[:10])
            wfrom = (d0 - timedelta(days=365)).strftime("%Y-%m-%d")
            wto = (d0 - timedelta(days=1)).strftime("%Y-%m-%d")
            wb = await td.fetch_history(s, ticker, custom=(wfrom, wto)) or []
            warmup = [b["c"] for b in wb if b.get("c") is not None]
        except Exception:
            warmup = []
    full = warmup + closes
    k = len(warmup)

    def line_ind(vals):                 # computed on warmup+window, sliced to window
        vs = vals[k:]
        return [{"time": b["t"], "value": round(v, 4)}
                for b, v in zip(bars, vs) if v is not None]

    price = [{"time": b["t"], "value": round(b["c"], 4)} for b in bars]
    sma50, sma100, sma200 = (line_ind(td.sma(full, n)) for n in (50, 100, 200))
    rsi = line_ind(td.rsi(full, 14))

    # Decimate very long series. lightweight-charts won't zoom out past ~0.5px/bar, so
    # thousands of daily bars (e.g. FEDFUNDS ALL ~26k, or any 10Y) can't fit and
    # fitContent() clips to the most recent slice. Thin to a width-friendly count,
    # always keeping the final point so the last price/% is exact.
    MAXPTS = 2500
    if len(price) > MAXPTS:
        stride = (len(price) + MAXPTS - 1) // MAXPTS

        def thin(a):
            if len(a) <= 2:
                return a
            out = a[::stride]
            if (len(a) - 1) % stride != 0:
                out.append(a[-1])
            return out

        price, sma50, sma100, sma200, rsi = map(
            thin, (price, sma50, sma100, sma200, rsi))

    idx = td.resolve_index(ticker)
    return web.json_response({
        "ticker": ticker, "tf": tf,
        "price": price, "sma50": sma50, "sma100": sma100, "sma200": sma200,
        "rsi": rsi, "display": idx[2] if idx else ticker,
        # FRED series (rates/yields/spreads/econ) are levels, not tradeable prices,
        # so the compare legend shows % only (no dollar value) for them.
        "rate": bool(td.resolve_fred(ticker)),
    })


# field display order for the fundamentals grid (matches the terminal)
FUND_ORDER = ["Exchange", "Market Cap", "P/E (ttm)", "P/E (fwd)", "EPS (ttm)",
              "EPS (fwd)", "Price/Sales", "Revenue (ttm)", "Gross Margin",
              "Net Margin", "ROE", "Debt/Equity", "EBITDA (ttm)", "Beta",
              "Dividend", "Div Yield", "Shares Out", "52wk High", "52wk Low",
              "Day High", "Day Low", "Volume"]


async def api_security(request):
    """Ticker detail: header quote, fundamentals grid, and P/E history."""
    ticker = request.query.get("ticker", "AAPL").upper()
    s = request.app["session"]
    fund = await td.fetch_fundamentals(s, ticker)
    idx = td.resolve_index(ticker)
    if idx:
        pe = await td.fetch_index_pe(s, ticker)
        pe_label = f"{idx[2].split('·')[0].strip()} P/E"
    else:
        pe = await td.fetch_pe_history(s, ticker, n=30)
        pe_label = "P/E (yr-end)"
    fields = [{"k": k, "v": fund[k]} for k in FUND_ORDER if fund and k in fund]
    return web.json_response({
        "ticker": ticker,
        "name": (fund.get("Name") if fund else "") or "",
        "display": idx[2] if idx else (fund.get("Name") if fund else ticker),
        "last": fund.get("Last") if fund else None,
        "change": fund.get("Change") if fund else None,
        "changePct": fund.get("Change %") if fund else None,
        "up": (fund.get("_changetype", "") != "DOWN") if fund else True,
        "fields": fields,
        "pe": [{"year": y, "pe": round(p, 1)} for y, p in pe],
        "pe_label": pe_label,
        "is_crypto": td.is_crypto(ticker),
    })


# ---- symbol search: company name -> ticker suggestions -------------------
# SEC's company_tickers.json covers ~10k US-listed stocks (name -> ticker). We
# add the ETFs / indices / crypto the terminal supports but SEC doesn't list.
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SYM_CACHE = {"t": None, "rows": []}      # [(symbol, name, type)]; refreshed daily
SYMBOL_SUPPLEMENT = [
    # major ETFs
    ("SPY", "SPDR S&P 500 ETF", "etf"), ("QQQ", "Invesco QQQ (Nasdaq 100)", "etf"),
    ("DIA", "SPDR Dow Jones ETF", "etf"), ("IWM", "iShares Russell 2000 ETF", "etf"),
    ("VTI", "Vanguard Total Stock Market ETF", "etf"), ("VOO", "Vanguard S&P 500 ETF", "etf"),
    ("GLD", "SPDR Gold Shares", "etf"), ("SLV", "iShares Silver Trust", "etf"),
    ("TLT", "iShares 20+ Year Treasury ETF", "etf"), ("HYG", "iShares High Yield Bond ETF", "etf"),
    ("XLF", "Financials Sector ETF", "etf"), ("XLE", "Energy Sector ETF", "etf"),
    ("XLK", "Technology Sector ETF", "etf"), ("USO", "US Oil Fund", "etf"),
    ("GDX", "VanEck Gold Miners ETF", "etf"), ("ARKK", "ARK Innovation ETF", "etf"),
    # indices (as the terminal keys them)
    ("SPX", "S&P 500 Index", "index"), ("NDX", "Nasdaq 100 Index", "index"),
    ("DJI", "Dow Jones Industrial Average", "index"), ("INDU", "Dow Jones Industrial Average", "index"),
    ("RUT", "Russell 2000 Index", "index"), ("VIX", "CBOE Volatility Index", "index"),
    # crypto
    ("BTC", "Bitcoin", "crypto"), ("ETH", "Ethereum", "crypto"), ("SOL", "Solana", "crypto"),
    ("XRP", "XRP", "crypto"), ("DOGE", "Dogecoin", "crypto"), ("ADA", "Cardano", "crypto"),
    # rates (FRED-charted)
    ("FDTR", "Fed Funds Rate", "rate"), ("US10Y", "US 10-Year Yield", "rate"),
    ("US2Y", "US 2-Year Yield", "rate"), ("CPI", "US CPI Inflation", "rate"),
]


async def _symbol_rows(session):
    now = datetime.now()
    if _SYM_CACHE["t"] and (now - _SYM_CACHE["t"]).total_seconds() < 86400:
        return _SYM_CACHE["rows"]
    rows = list(SYMBOL_SUPPLEMENT)
    try:
        async with session.get(SEC_TICKERS_URL, headers=td.SEC_UA,
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json(content_type=None)
        for v in data.values():
            sym, title = v.get("ticker"), v.get("title")
            if sym and title:
                rows.append((sym.upper(), title.title(), "stock"))
    except Exception:
        if _SYM_CACHE["rows"]:
            return _SYM_CACHE["rows"]          # keep the last good list on a blip
    _SYM_CACHE.update(t=now, rows=rows)
    return rows


_NAME_SUFFIX = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
                "limited", "plc", "sa", "ag", "nv", "group", "holdings", "holding",
                "the", "lp", "llc", "trust", "class", "common", "stock", "&"}


def _name_core(name):
    """Company name minus corporate suffixes: 'Apple Inc.' -> 'apple'."""
    return " ".join(w for w in re.split(r"[^\w]+", name.lower())
                    if w and w not in _NAME_SUFFIX)


def _rank_symbol(q, sym, name):
    """Higher is better; None to drop. q is lowercase."""
    s, n = sym.lower(), name.lower()
    if s == q:
        return 100                             # exact ticker
    if _name_core(name) == q:
        return 88                              # exact company name (ignoring Inc/Corp/…)
    if s.startswith(q):
        return 80 - len(sym)                   # prefer the shortest matching ticker
    if n.startswith(q):
        return 70
    if any(w.startswith(q) for w in re.split(r"[^\w]+", n)):
        return 60
    if q in n:
        return 45
    return None


async def api_symsearch(request):
    """Company name (or partial ticker) -> best-matching ticker suggestions."""
    q = request.query.get("q", "").strip().lower()
    if len(q) < 2:
        return web.json_response({"results": []})
    rows = await _symbol_rows(request.app["session"])
    scored = []
    for sym, name, typ in rows:
        sc = _rank_symbol(q, sym, name)
        if sc is not None:
            # tie-break: shorter company name (the real one beats obscure lookalikes),
            # then shorter ticker, then alphabetical.
            scored.append((sc, len(name), len(sym), sym, name, typ))
    scored.sort(key=lambda x: (-x[0], x[1], x[2], x[3]))
    seen, out = set(), []
    for _, _, _, sym, name, typ in scored:
        if sym in seen:
            continue
        seen.add(sym)
        out.append({"symbol": sym, "name": name, "type": typ})
        if len(out) >= 8:
            break
    return web.json_response({"results": out})


async def api_news(request):
    """Headlines for a ticker (or general market news if no ticker)."""
    ticker = request.query.get("ticker") or None
    items = await td.fetch_news(request.app["session"], ticker, 14)
    return web.json_response({"items": [{
        "title": it["title"], "source": it.get("source", ""),
        "link": it.get("link", ""), "age": td._rss_age(it.get("pub", "")),
    } for it in items]})


async def api_news_board(request):
    """Multi-section market-news board (CNBC + Google feeds), mirroring the terminal."""
    secs = await td.fetch_news_dashboard(request.app["session"], per=7)
    return web.json_response({"sections": [{
        "heading": s["heading"],
        "items": [{"title": it["title"], "source": it.get("source", ""),
                   "link": it.get("link", ""), "age": it.get("age", "")}
                  for it in s["items"]],
    } for s in secs]})


RESEARCH_DIR = HERE.parent / "research"
_RESEARCH_EXT = {".pdf", ".txt", ".md"}

# Public research feeds shown alongside the folder. `readable`=True means the post's
# full text is fetchable (opened via the article reader); False shows the feed blurb.
RESEARCH_FEEDS = [
    {"name": "Adam Taggart · Thoughtful Money",
     "url": "https://adamtaggart.substack.com/feed", "readable": True},
    {"name": "Henry Tapper · AgeWage",
     "url": "https://henrytapper.com/feed/", "readable": True},
    # YouTube Atom feed (their substack is dormant). Interviews only — Shorts are
    # dropped. Items open as a blurb (video description) + link out to YouTube.
    {"name": "Wealthion",
     "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCKMeK-HGHfUFFArZ91rzv5A",
     "readable": False, "drop": "/shorts/"},
    # BMO is handled separately by _bmo_insights_section() below: the whole Insights
    # library (all authors, not just Macro Horizons) is pulled from BMO's sitemap and
    # filtered to the last N days. The pages are server-rendered, so they read in-app.
]

# All BMO Capital Markets "Insights" articles come from the sitemap (there's no RSS);
# each <url> carries a real publish/refresh date in <lastmod>, which we use to keep the
# last BMO_INSIGHTS_DAYS days across every author.
BMO_SITEMAP = "https://capitalmarkets.bmo.com/sitemap.xml"
BMO_INSIGHTS_PREFIX = "https://capitalmarkets.bmo.com/en/insights/"
BMO_INSIGHTS_DAYS = 14
# Slugs are lowercase; keep these tokens uppercase when rebuilding a headline.
_BMO_ACRONYMS = {
    "bmo", "us", "usmca", "ai", "esg", "ceo", "cfo", "cio", "reit", "reits", "svb",
    "ccus", "cop27", "ev", "evs", "gdp", "ecb", "boc", "uk", "eu", "cad",
    "usd", "q1", "q2", "q3", "q4", "etf", "etfs", "ipo", "sp", "tsx", "llm", "llms",
    "esr", "ipos", "eps", "fx",
}


def _slug_to_title(slug):
    words = []
    for w in slug.split("-"):
        if not w:
            continue
        words.append(w.upper() if w.lower() in _BMO_ACRONYMS else w.capitalize())
    return " ".join(words)


async def _bmo_insights_section(days=BMO_INSIGHTS_DAYS, limit=50):
    """All BMO Insights articles from the last `days` days, newest first (from sitemap).

    Uses its own session: BMO sends a very large Content-Security-Policy header that
    exceeds aiohttp's default 8190-byte header limit, so the shared app session 400s.
    """
    try:
        async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as s:
            async with s.get(BMO_SITEMAP, headers=td.UA,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                raw = await r.text()
    except Exception:
        return None
    raw = re.sub(r'\sxmlns="[^"]+"', "", raw, count=1)   # drop default ns for easy parsing
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    now = datetime.now(timezone.utc)
    rows = []
    for u in root.iter("url"):
        loc = (u.findtext("loc") or "").strip()
        if not loc.startswith(BMO_INSIGHTS_PREFIX):
            continue
        try:
            dt = datetime.fromisoformat((u.findtext("lastmod") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        age = (now - dt).days
        if age < 0 or age > days:
            continue
        slug = loc[len(BMO_INSIGHTS_PREFIX):].strip("/")
        meta = dt.strftime("%b %d") + (" · today" if age == 0
                                       else " · 1d" if age == 1 else f" · {age}d")
        rows.append((dt, {"kind": "web", "title": _slug_to_title(slug),
                          "link": loc, "meta": meta}))
    rows.sort(key=lambda x: x[0], reverse=True)
    items = [it for _, it in rows[:limit]]
    if not items:
        return None
    return {"name": f"BMO INSIGHTS · LAST {days} DAYS", "items": items}


# ---- Wix-hosted letters (no RSS; per-publication sitemap chunks) ----
# Free letters only. Pages are server-rendered, so they open in the in-app reader.
# Each site: sitemap index URL + (sitemap-chunk marker -> author tag) pairs.
WIX_LETTER_SITES = [
    {"name": "Mauldin Economics",
     "sitemap": "https://www.mauldineconomics.com/sitemap.xml",
     "pubs": [("dynamic-frontlinethoughts", "Thoughts from the Frontline"),
              ("dynamic-global-macro-update", "Global Macro Update")],
     "limit": 10},
    {"name": "Jared Dillian Money",
     "sitemap": "https://www.jareddillianmoney.com/sitemap.xml",
     "pubs": [("dynamic-weekly", "The Weekly Letter")],
     "limit": 10},
]


async def _wix_letters_section(site):
    """Latest letters from one Wix site, merged across publications, newest first."""
    rows = []
    try:
        async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as s:
            async with s.get(site["sitemap"], headers=td.UA,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                chunks = re.findall(r"<loc>([^<]+)</loc>", await r.text())
            for frag, tag in site["pubs"]:
                url = next((c for c in chunks if frag in c), None)
                if not url:
                    continue
                try:
                    async with s.get(url, headers=td.UA,
                                     timeout=aiohttp.ClientTimeout(total=15)) as r:
                        raw = await r.text()
                except Exception:
                    continue
                rows += [(lastmod, loc, tag) for loc, lastmod in re.findall(
                    r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", raw)]
    except Exception:
        return None
    rows.sort(reverse=True)                        # ISO dates — string sort is fine
    now = datetime.now(timezone.utc)
    items = []
    for lastmod, loc, tag in rows[:site["limit"]]:
        try:
            dt = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (now - dt).days
            meta = dt.strftime("%b %d") + (" · today" if age <= 0 else f" · {age}d")
        except ValueError:
            meta = lastmod
        slug = loc.rstrip("/").rsplit("/", 1)[-1]
        items.append({"kind": "web", "title": _slug_to_title(slug), "link": loc,
                      "meta": meta, "author": tag})
    if not items:
        return None
    return {"name": site["name"], "items": items}


# ---- Tracked people: news / interviews / articles (Google News queries) ----
# These voices publish rarely or behind sites with no feed, so each section
# tracks press coverage instead: anything mentioning them in the last year,
# newest first. Google News links resolve to the real publisher in the reader.
PERSON_FEEDS = [
    ("Jeremy Grantham", '"Jeremy Grantham"'),
    ("Lacy Hunt", '"Lacy Hunt"'),
]


async def _person_section(session, name, query):
    params = {"q": f"{query} when:1y", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    try:
        async with session.get(td.GOOGLE_NEWS, params=params, headers=td.UA,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            items = td._parse_rss(await r.text(), 40)
    except Exception:
        return None
    items.sort(key=lambda it: td._rss_dt(it.get("pub", ""))
               or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    items = td._label_sources(items[:10])
    out = [{"kind": "web", "title": it["title"], "link": it["link"],
            "meta": td._rss_age(it.get("pub", "")), "author": it.get("source", "")}
           for it in items if it.get("link")]
    if not out:
        return None
    return {"name": name, "items": out}


def _clean_html(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def _slug(title):
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", title.lower()).strip())


def _parse_research_feed(text, limit=12):
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        out.append({"title": title, "link": (it.findtext("link") or "").strip(),
                    "pub": (it.findtext("pubDate") or "").strip(),
                    "desc": it.findtext("description") or ""})
        if len(out) >= limit:
            break
    if not out:                       # Atom feed (e.g. YouTube channels), not RSS
        A, M = "{http://www.w3.org/2005/Atom}", "{http://search.yahoo.com/mrss/}"
        for e in root.iter(A + "entry"):
            title = (e.findtext(A + "title") or "").strip()
            if not title:
                continue
            ln, grp = e.find(A + "link"), e.find(M + "group")
            out.append({"title": title,
                        "link": (ln.get("href") if ln is not None else "").strip(),
                        "pub": (e.findtext(A + "published") or "").strip(),
                        "desc": (grp.findtext(M + "description") if grp is not None else "") or ""})
            if len(out) >= limit:
                break
    return out


async def _research_feed(session, feed):
    try:
        async with session.get(feed["url"], headers=td.UA,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            raw = await r.text()
    except Exception:
        return None
    items = []
    for it in _parse_research_feed(raw, 30):
        if feed.get("drop") and feed["drop"] in (it.get("link") or ""):
            continue                       # e.g. YouTube Shorts — interviews only
        entry = {"title": it["title"], "meta": td._rss_age(it["pub"])}
        if feed.get("author"):
            entry["author"] = feed["author"]
        if feed["readable"]:
            entry["kind"], entry["link"] = "web", it["link"]
        else:
            entry["kind"], entry["body"] = "blurb", _clean_html(it["desc"])
            if feed.get("link_base"):
                entry["link"] = feed["link_base"] + _slug(it["title"]) + feed.get("link_suffix", "")
            elif it.get("link"):
                entry["link"] = it["link"]
        items.append(entry)
        if len(items) >= 12:
            break
    return {"name": feed["name"], "items": items}


# Official / academic economic feeds, grouped under one heading each. Items from every
# feed in a group are merged and shown newest-first, tagged with their `src` label.
ECON_GROUPS = [
    {"heading": "FEDERAL RESERVE", "limit": 10, "feeds": [
        {"src": "Fed · Press", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
        {"src": "Fed · Speeches", "url": "https://www.federalreserve.gov/feeds/speeches.xml"},
        {"src": "Fed · FEDS Notes", "url": "https://www.federalreserve.gov/feeds/feds_notes.xml"},
        {"src": "NY Fed · Liberty St", "url": "https://libertystreeteconomics.newyorkfed.org/feed/"},
        {"src": "Atlanta Fed · macroblog", "url": "https://www.atlantafed.org/rss/macroblog"},
        {"src": "SF Fed", "url": "https://www.frbsf.org/feed/"},
    ]},
    {"heading": "U.S. DATA & BUDGET", "limit": 12, "feeds": [
        {"src": "BEA", "url": "https://apps.bea.gov/rss/rss.xml"},
        {"src": "CBO", "url": "https://www.cbo.gov/publications/all/rss.xml"},
        {"src": "GAO · Fiscal", "url": "https://www.gao.gov/rss/topic/budget-and-spending"},
    ]},
    {"heading": "ACADEMIC RESEARCH", "limit": 8, "feeds": [
        {"src": "NBER", "url": "https://back.nber.org/rss/new.xml"},
    ]},
    {"heading": "GLOBAL CENTRAL BANKS", "limit": 8, "feeds": [
        {"src": "ECB", "url": "https://www.ecb.europa.eu/rss/press.xml"},
    ]},
]


def _feed_ts(pub):
    """Best-effort parse of an RSS/Atom date string to a sortable epoch (0.0 if unknown)."""
    if not pub:
        return 0.0
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(pub).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


async def _fetch_feed_items(session, feed, per=6):
    try:
        async with session.get(feed["url"], headers=td.UA,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            raw = await r.text()
    except Exception:
        return []
    out = []
    for it in _parse_research_feed(raw, per):
        out.append({"title": it["title"], "meta": td._rss_age(it["pub"]),
                    "author": feed["src"], "kind": "web", "link": it["link"],
                    # feed summary — shown if the page itself can't be extracted
                    # (many gov sites, e.g. CBO, bot-block their article pages).
                    "body": _clean_html(it.get("desc", "")),
                    "_ts": _feed_ts(it["pub"])})
    return out


async def _econ_group_section(session, group):
    """One heading merging every feed in the group, newest-first."""
    subs = await asyncio.gather(*[_fetch_feed_items(session, f) for f in group["feeds"]])
    items = [it for sub in subs for it in sub]
    items.sort(key=lambda x: x["_ts"], reverse=True)
    items = items[:group.get("limit", 8)]
    for it in items:
        it.pop("_ts", None)
    if not items:
        return None
    return {"name": group["heading"], "items": items}


# US Treasury auctions — official TreasuryDirect JSON API (no key, no bot-wall).
TD_UPCOMING = "https://www.treasurydirect.gov/TA_WS/securities/upcoming?format=json"
TD_RESULTS = "https://www.treasurydirect.gov/TA_WS/securities/auctioned?format=json&days=7"


def _td_amt(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return f"${v/1e9:.0f}B" if v >= 1e9 else f"${v/1e6:.0f}M"


def _td_bil(s):
    """Money with one-decimal billions for the detail view (keeps e.g. $74.7B)."""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


def _td_kv(label, val, width=16):
    return f"{label:<{width}}: {val}" if val not in (None, "") else None


def _td_day(s):
    try:
        return datetime.fromisoformat((s or "").split("T")[0])
    except ValueError:
        return None


def _td_num(s, suffix):
    try:
        return f"{float(s):.3f}{suffix}"
    except (TypeError, ValueError):
        return None


async def _treasury_auctions_section(session):
    """Upcoming Treasury auctions (schedule) + recent auction results, as data lines."""
    async def get(url):
        try:
            # ssl=False: treasurydirect.gov serves a cert chain that certifi rejects
            # (verifies fine in the macOS Keychain / curl). Safe here — public,
            # read-only auction data, nothing sensitive is sent.
            async with session.get(url, headers=td.UA, ssl=False,
                                   timeout=aiohttp.ClientTimeout(total=12)) as r:
                return await r.json(content_type=None)
        except Exception:
            return []
    up, res = await asyncio.gather(get(TD_UPCOMING), get(TD_RESULTS))

    def D(s):
        return (s or "")[:10]

    items = []
    for x in sorted(up or [], key=lambda z: z.get("auctionDate", ""))[:7]:
        term, typ = x.get("securityTerm", "").strip(), x.get("securityType", "").strip()
        amt = _td_amt(x.get("offeringAmount"))
        dt = _td_day(x.get("auctionDate"))
        title = f"{term} {typ}" + (f" · {amt} offered" if amt else "")
        body = "\n".join(filter(None, [
            f"Upcoming auction — {term} {typ}",
            _td_kv("CUSIP", x.get("cusip")),
            _td_kv("Announced", D(x.get("announcementDate"))),
            _td_kv("Auction date", D(x.get("auctionDate"))),
            _td_kv("Issue date", D(x.get("issueDate"))),
            _td_kv("Maturity", D(x.get("maturityDate"))),
            _td_kv("Offering", _td_bil(x.get("offeringAmount"))),
            _td_kv("Reopening", x.get("reopening")),
            _td_kv("TIPS", x.get("tips") if x.get("tips") == "Yes" else None)]))
        items.append({"title": title, "author": "Upcoming", "kind": "blurb", "body": body,
                      "meta": dt.strftime("%b %d") if dt else "",
                      "link": "https://www.treasurydirect.gov/auctions/upcoming/"})

    for x in sorted(res or [], key=lambda z: z.get("auctionDate", ""), reverse=True)[:6]:
        term, typ = x.get("securityTerm", "").strip(), x.get("securityType", "").strip()
        yld = x.get("highYield")
        # bills quote a discount rate; notes/bonds/TIPS quote a yield
        hi = _td_num(yld, "%") or _td_num(x.get("highDiscountRate"), "%") or "n/a"
        med = _td_num(x.get("averageMedianYield"), "%") or _td_num(x.get("averageMedianDiscountRate"), "%")
        low = _td_num(x.get("lowYield"), "%") or _td_num(x.get("lowDiscountRate"), "%")
        try:
            btc = f"{float(x.get('bidToCoverRatio')):.2f}x"   # b/c convention: 2 decimals
        except (TypeError, ValueError):
            btc = "n/a"
        dt = _td_day(x.get("auctionDate"))
        rate_word = "High yield" if yld else "High rate"
        title = f"{term} {typ} · {hi} · b/c {btc}"
        body = "\n".join(ln for ln in [   # keep "" blank separators, drop missing (None) fields
            f"Auction result — {term} {typ}",
            _td_kv("CUSIP", x.get("cusip")),
            _td_kv("Auction date", D(x.get("auctionDate"))),
            _td_kv("Issue date", D(x.get("issueDate"))),
            _td_kv("Maturity", D(x.get("maturityDate"))),
            _td_kv("Format", x.get("auctionFormat")),
            _td_kv("Reopening", x.get("reopening")),
            "",
            _td_kv("Offering", _td_bil(x.get("offeringAmount"))),
            _td_kv("Total tendered", _td_bil(x.get("totalTendered"))),
            _td_kv("Total accepted", _td_bil(x.get("totalAccepted"))),
            _td_kv("Bid-to-cover", btc),
            "",
            _td_kv("Coupon", _td_num(x.get("interestRate"), "%")),
            _td_kv(rate_word, hi + (f"  (inv. {_td_num(x.get('highInvestmentRate'), '%')})"
                                    if x.get("highInvestmentRate") else "")),
            _td_kv("Median", med),
            _td_kv("Low", low),
            _td_kv("Price /100", x.get("pricePer100")),
            _td_kv("Allotted at high", _td_num(x.get("allocationPercentage"), "%")),
            "",
            _td_kv("Primary dealers", _td_bil(x.get("primaryDealerAccepted"))),
            _td_kv("Direct bidders", _td_bil(x.get("directBidderAccepted"))),
            _td_kv("Indirect bidders", _td_bil(x.get("indirectBidderAccepted"))),
            _td_kv("Noncompetitive", _td_bil(x.get("noncompetitiveAccepted")))] if ln is not None)
        items.append({"title": title, "author": "Result", "kind": "blurb", "body": body,
                      "meta": dt.strftime("%b %d") if dt else "",
                      "link": "https://www.treasurydirect.gov/auctions/auction-query/"})

    if not items:
        return None
    return {"name": "US TREASURY AUCTIONS", "items": items}


# ---- U.S. economic releases (CPI, jobs report, JOLTS, ADP, PPI, PCE, ...) ----
# BLS and ADP bot-wall their sites AND their RSS feeds outright (Akamai 403s even
# with full browser headers), so the numbers come from FRED instead: the latest
# observations of each report's headline series, plus the report's real release
# dates (past and scheduled) from FRED's release calendar. Items are blurbs — the
# reader shows the data block in-app instead of fetching a bot-walled page.
FRED_RELEASE_DATES = "https://api.stlouisfed.org/fred/release/dates"


def _pct(v, signed=False):
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def _jobs_k(v):
    return f"{v:+.0f}k"


# Per report: FRED release_id (for the release-date calendar), headline series
# (key -> (series_id, units); pc1 = YoY %, pch = period % change, chg = change),
# a compact title, detail rows for the reader, and which key's trend to show.
ECON_RELEASES = [
    {"long": "Consumer Price Index", "src": "BLS", "release_id": 10,
     "link": "https://www.bls.gov/news.release/cpi.nr0.htm",
     "series": {"head": ("CPIAUCSL", "pc1"), "core": ("CPILFESL", "pc1"),
                "mom": ("CPIAUCSL", "pch")},
     "title": lambda v, ref: (f"CPI · {ref}: {_pct(v['head'])} y/y · core "
                              f"{_pct(v['core'])} · {_pct(v['mom'], True)} m/m"),
     "rows": [("Headline CPI", "head", lambda v: _pct(v) + " y/y"),
              ("Core CPI (ex food & energy)", "core", lambda v: _pct(v) + " y/y"),
              ("Month-on-month", "mom", lambda v: _pct(v, True))],
     "hist": "head", "hist_label": "Headline y/y trend"},
    {"long": "Employment Situation (jobs report)", "src": "BLS", "release_id": 50,
     "link": "https://www.bls.gov/news.release/empsit.nr0.htm",
     "series": {"nfp": ("PAYEMS", "chg"), "unrate": ("UNRATE", "lin"),
                "wages": ("CES0500000003", "pc1")},
     "title": lambda v, ref: (f"Jobs Report · {ref}: {_jobs_k(v['nfp'])} payrolls · "
                              f"{_pct(v['unrate'])} unemployment"),
     "rows": [("Nonfarm payrolls", "nfp", lambda v: _jobs_k(v) + " m/m"),
              ("Unemployment rate", "unrate", _pct),
              ("Avg hourly earnings", "wages", lambda v: _pct(v) + " y/y")],
     "hist": "nfp", "hist_label": "Payrolls m/m trend", "hist_fmt": _jobs_k},
    # NOTE: the ADP series is in persons (PAYEMS-style series are in thousands).
    {"long": "ADP National Employment Report", "src": "ADP", "release_id": 194,
     "link": "https://adpemploymentreport.com/",
     "series": {"adp": ("ADPMNUSNERSA", "chg")},
     "title": lambda v, ref: f"ADP Employment · {ref}: {_jobs_k(v['adp'] / 1000)} private payrolls",
     "rows": [("Private payrolls", "adp", lambda v: _jobs_k(v / 1000) + " m/m")],
     "hist": "adp", "hist_label": "Private payrolls m/m trend",
     "hist_fmt": lambda v: _jobs_k(v / 1000)},
    {"long": "Job Openings and Labor Turnover Survey", "src": "BLS", "release_id": 192,
     "link": "https://www.bls.gov/news.release/jolts.nr0.htm",
     "series": {"open": ("JTSJOL", "lin"), "quits": ("JTSQUR", "lin")},
     "title": lambda v, ref: (f"JOLTS · {ref}: {v['open'] / 1000:.1f}M job openings · "
                              f"quits rate {_pct(v['quits'])}"),
     "rows": [("Job openings", "open", lambda v: f"{v / 1000:.2f}M"),
              ("Quits rate", "quits", _pct)],
     "hist": "open", "hist_label": "Openings (M) trend",
     "hist_fmt": lambda v: f"{v / 1000:.1f}"},
    {"long": "Producer Price Index (final demand)", "src": "BLS", "release_id": 46,
     "link": "https://www.bls.gov/news.release/ppi.nr0.htm",
     "series": {"head": ("PPIFIS", "pc1"), "mom": ("PPIFIS", "pch")},
     "title": lambda v, ref: (f"PPI · {ref}: {_pct(v['head'])} y/y · "
                              f"{_pct(v['mom'], True)} m/m"),
     "rows": [("PPI final demand", "head", lambda v: _pct(v) + " y/y"),
              ("Month-on-month", "mom", lambda v: _pct(v, True))],
     "hist": "head", "hist_label": "PPI y/y trend"},
    {"long": "PCE inflation (Personal Income & Outlays)", "src": "BEA", "release_id": 54,
     "link": "https://www.bea.gov/data/income-saving/personal-income",
     "series": {"head": ("PCEPI", "pc1"), "core": ("PCEPILFE", "pc1")},
     "title": lambda v, ref: (f"PCE Inflation · {ref}: {_pct(v['head'])} y/y · core "
                              f"{_pct(v['core'])}"),
     "rows": [("Headline PCE", "head", lambda v: _pct(v) + " y/y"),
              ("Core PCE (Fed's gauge)", "core", lambda v: _pct(v) + " y/y")],
     "hist": "core", "hist_label": "Core y/y trend"},
    {"long": "Advance Retail Sales", "src": "Census", "release_id": 9,
     "link": "https://www.census.gov/retail/sales.html",
     "series": {"mom": ("RSAFS", "pch"), "yoy": ("RSAFS", "pc1")},
     "title": lambda v, ref: (f"Retail Sales · {ref}: {_pct(v['mom'], True)} m/m · "
                              f"{_pct(v['yoy'])} y/y"),
     "rows": [("Month-on-month", "mom", lambda v: _pct(v, True)),
              ("Year-on-year", "yoy", _pct)],
     "hist": "mom", "hist_label": "m/m trend", "hist_fmt": lambda v: _pct(v, True)},
    {"long": "Gross Domestic Product", "src": "BEA", "release_id": 53, "q": True,
     "link": "https://www.bea.gov/data/gdp/gross-domestic-product",
     "series": {"gdp": ("A191RL1Q225SBEA", "lin")},
     "title": lambda v, ref: f"GDP · {ref}: {_pct(v['gdp'], True)} q/q annualized",
     "rows": [("Real GDP (q/q SAAR)", "gdp", lambda v: _pct(v, True))],
     "hist": "gdp", "hist_label": "q/q SAAR trend", "hist_fmt": lambda v: _pct(v, True)},
]


def _ref_label(date_str, quarterly=False):
    dt = datetime.fromisoformat(date_str)
    if quarterly:
        return f"Q{(dt.month - 1) // 3 + 1} {dt.year}"
    return dt.strftime("%b %Y")


async def _fred_latest(session, series_id, units="lin", n=8):
    """Newest n observations of a FRED series as (date, value), newest first."""
    key = td._fred_key()
    if not key:
        return []
    params = {"series_id": series_id, "api_key": key, "file_type": "json",
              "sort_order": "desc", "limit": n, "units": units}
    try:
        async with session.get(td.FRED_OBS, params=params,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            data = await r.json(content_type=None)
    except Exception:
        return []
    out = []
    for o in data.get("observations", []):
        v = o.get("value")
        if v in (".", "", None):
            continue
        try:
            out.append((o["date"], float(v)))
        except (ValueError, KeyError):
            continue
    return out


async def _fred_release_dates(session, release_id):
    """(most_recent_past, next_scheduled) dates for a FRED release; either may be None."""
    key = td._fred_key()
    if not key:
        return None, None
    params = {"release_id": release_id, "api_key": key, "file_type": "json",
              "sort_order": "desc", "limit": 30,
              "include_release_dates_with_no_data": "true"}
    try:
        async with session.get(FRED_RELEASE_DATES, params=params,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            data = await r.json(content_type=None)
    except Exception:
        return None, None
    today = datetime.now().date().isoformat()
    past = nxt = None
    for d in data.get("release_dates", []):     # newest first
        if d["date"] > today:
            nxt = d["date"]                     # keeps shrinking toward the soonest
        else:
            past = d["date"]
            break
    return past, nxt


_ECON_REL_CACHE = {"t": None, "sec": None}      # data moves daily at most; 15-min TTL


async def _econ_releases_section(session):
    """One section: the latest headline numbers of each major U.S. economic report."""
    if not td._fred_key():
        return None
    now = datetime.now()
    if _ECON_REL_CACHE["t"] and (now - _ECON_REL_CACHE["t"]).total_seconds() < 900:
        return _ECON_REL_CACHE["sec"]
    pairs = sorted({sw for rep in ECON_RELEASES for sw in rep["series"].values()})
    results = await asyncio.gather(
        *[_fred_latest(session, sid, units) for sid, units in pairs],
        *[_fred_release_dates(session, rep["release_id"]) for rep in ECON_RELEASES])
    obs = dict(zip(pairs, results[:len(pairs)]))
    rel = {rep["release_id"]: results[len(pairs) + i]
           for i, rep in enumerate(ECON_RELEASES)}
    items = []
    for rep in ECON_RELEASES:
        vals, dates = {}, {}
        for k, sw in rep["series"].items():
            series = obs.get(sw) or []
            if series:
                dates[k], vals[k] = series[0]
        if rep["hist"] not in vals:             # headline series unavailable — skip
            continue
        ref = _ref_label(dates[rep["hist"]], rep.get("q", False))
        try:
            title = rep["title"](vals, ref)
        except (KeyError, TypeError):
            title = f"{rep['long']} · {ref}"
        lines = [f"{rep['long']} ({rep['src']})", f"Reference period: {ref}", ""]
        for label, k, fmt in rep["rows"]:
            if k in vals:
                lines.append(f"{label:<28}{fmt(vals[k])}")
        hseries = obs.get(rep["series"][rep["hist"]]) or []
        if len(hseries) > 1:
            hfmt = rep.get("hist_fmt", lambda v: f"{v:.1f}")
            trail = []
            for d, v in reversed(hseries[:7]):
                dt = datetime.fromisoformat(d)
                lbl = (f"Q{(dt.month - 1) // 3 + 1}" if rep.get("q")
                       else dt.strftime("%b"))
                trail.append(f"{lbl} {hfmt(v)}")
            lines += ["", f"{rep['hist_label']}:  " + " · ".join(trail)]
        released, nxt = rel.get(rep["release_id"]) or (None, None)
        meta = ""
        if released:
            rd = datetime.fromisoformat(released).date()
            dd = (now.date() - rd).days
            meta = rd.strftime("%b %d") + (" · today" if dd == 0 else f" · {dd}d")
            lines += ["", f"Released: {rd.strftime('%b %d, %Y')}"]
        if nxt:
            lines.append(f"Next release: {datetime.fromisoformat(nxt).strftime('%b %d, %Y')}")
        lines += ["", "Data via FRED (St. Louis Fed)."]
        items.append((released or "", {"kind": "blurb", "title": title, "meta": meta,
                                       "author": rep["src"], "body": "\n".join(lines),
                                       "link": rep["link"]}))
    if not items:
        return None                             # transient failure — don't cache
    items.sort(key=lambda x: x[0], reverse=True)
    sec = {"name": "U.S. ECONOMIC RELEASES", "items": [it for _, it in items]}
    _ECON_REL_CACHE.update(t=now, sec=sec)
    return sec


# Auto-downloaded Rosenberg files are named `rr_<code>__<title>.pdf`; group them into
# one section per category, in this order. Anything else in research/ (manual drops) is
# shown under a generic "Research Reports" section.
ROSENBERG_SECTIONS = [
    ("daily", "Rosenberg · Daily Reports"), ("macro", "Rosenberg · Macro Research"),
    ("strategy", "Rosenberg · Market Strategy"), ("strategizer", "Rosenberg · Strategizer"),
    ("special", "Rosenberg · Special Reports"), ("webcasts", "Rosenberg · Webcasts"),
    ("chartroom", "Rosenberg · Investor Chartroom"),
    ("models", "Rosenberg · Proprietary Models"),
]


def _clean_report_title(t):
    """Tidy a report headline for display: no filename underscores, no ' -- '."""
    t = t.replace("_", " ")
    t = re.sub(r"\s*--\s*", " · ", t)             # 'Breakfast with Dave -- July 6' -> ' · '
    return re.sub(r"\s+", " ", t).strip()


async def api_research(request):
    """Research view: saved files (drop into research/) + public feeds."""
    RESEARCH_DIR.mkdir(exist_ok=True)
    try:                                          # real headlines the downloader saved
        titles = json.loads((RESEARCH_DIR / ".rr_titles.json").read_text(encoding="utf-8"))
    except Exception:
        titles = {}
    groups, other = {}, []                        # groups: code -> [items]
    for p in RESEARCH_DIR.iterdir():
        if not (p.is_file() and p.suffix.lower() in _RESEARCH_EXT
                and not p.name.startswith(".")):
            continue
        m = re.match(r"rr_([a-z]+)__(.+)", p.stem)
        stem_title = re.sub(r"_?\d{4}-\d{2}-\d{2}$", "", m.group(2) if m else p.stem)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})$", (m.group(2) if m else p.stem))
        item = {"kind": "file", "file": p.name,
                "title": _clean_report_title(titles.get(p.name) or stem_title),
                "ext": p.suffix.lower().lstrip("."),
                "_m": p.stat().st_mtime, "_d": dm.group(1) if dm else ""}
        (groups.setdefault(m.group(1), []) if m else other).append(item)

    def _finish(items, cap):
        items.sort(key=lambda x: (x["_d"], x["_m"]), reverse=True)   # newest report first
        items = items[:cap]
        for it in items:
            d, mt = it.pop("_d"), it.pop("_m")
            try:
                it["meta"] = (datetime.strptime(d, "%Y-%m-%d") if d
                              else datetime.fromtimestamp(mt)).strftime("%b %d, %Y")
            except ValueError:
                it["meta"] = d or ""
        return items

    sections = []
    for code, name in ROSENBERG_SECTIONS:
        if groups.get(code):
            sections.append({"name": name, "items": _finish(groups[code], 12)})
    if other:
        sections.append({"name": "Research Reports", "items": _finish(other, 12)})
    session = request.app["session"]
    # Own session with raised header limits: some gov sites send oversized CSP headers
    # that trip aiohttp's default 8190-byte cap (same issue BMO's sitemap has).
    async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as econ:
        # Order: Rosenberg (above) → Adam Taggart → U.S. economic releases →
        # Fed → Data&Budget → Treasury auctions → Academic → Global.
        # NOTE: BMO Insights was removed — it's proprietary bank content with no official
        # feed, they now block automated access (requests time out), and scraping/reproducing
        # it risked trouble. Only public-sector feeds + a public RSS (Taggart) remain.
        econ_tasks = [_econ_group_section(econ, g) for g in ECON_GROUPS]
        tasks = []
        tasks += [_research_feed(session, f) for f in RESEARCH_FEEDS]
        tasks += [_wix_letters_section(s) for s in WIX_LETTER_SITES]  # Mauldin, Dillian (sitemap, no RSS)
        tasks += [_person_section(session, n, q) for n, q in PERSON_FEEDS]
        tasks.append(_econ_releases_section(econ))   # CPI/jobs/JOLTS/ADP/... via FRED
        tasks += econ_tasks[:2]                      # FEDERAL RESERVE, U.S. DATA & BUDGET
        tasks.append(_treasury_auctions_section(econ))
        tasks += econ_tasks[2:]                      # ACADEMIC RESEARCH, GLOBAL
        feeds = await asyncio.gather(*tasks)
    for fs in feeds:
        if fs and fs["items"]:
            sections.append(fs)
    return web.json_response({"sections": sections})


# Report filename prefixes rendered as page IMAGES (charts/tables intact) on the shared
# cloud app. Everything else there reads as text — cheaper (no server-side render) and
# fine for long letters, keeping the memory-limited Fly box small.
_IMAGE_REPORT_PREFIXES = ("rr_models__", "rr_chartroom__")


def _renders_as_images(name):
    """Which reports render as page images. On the shared cloud app (MKT_PASSWORD set,
    memory-limited) only chart-heavy reports do; on a LOCAL terminal (Ezra's Mac /
    Robert's Windows build — real computers, no memory worry) ALL reports render as
    images, keeping the original full-fidelity format."""
    if os.environ.get("MKT_PASSWORD"):          # shared cloud Fly app
        return name.startswith(_IMAGE_REPORT_PREFIXES)
    return True                                 # local build: everything as images


def _pdf_page_count(path):
    """Number of pages, or 0 if PyMuPDF is missing / the file won't open. 0 => the
    reader falls back to text (keeps the desktop build working without the dep)."""
    try:
        import fitz  # PyMuPDF
    except Exception:
        return 0
    try:
        doc = fitz.open(str(path))
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        return 0


def _pdf_page_image(path, n, zoom=2.0, quality=80):
    """Render ONE page to JPEG bytes (served individually so the reader loads pages
    lazily instead of shipping a whole report as one huge JSON blob). None on failure."""
    try:
        import fitz  # PyMuPDF
    except Exception:
        return None
    try:
        doc = fitz.open(str(path))
        if n < 0 or n >= doc.page_count:
            doc.close(); return None
        jpg = doc[n].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("jpg", jpg_quality=quality)
        doc.close()
        return jpg
    except Exception:
        return None


def _pdf_paragraphs(path):
    import pypdf
    reader = pypdf.PdfReader(str(path))
    paras, buf = [], ""
    for page in reader.pages:
        for line in (page.extract_text() or "").split("\n"):
            line = line.rstrip()
            if not line:
                if buf:
                    paras.append(buf.strip()); buf = ""
                continue
            buf = (buf + " " + line).strip() if buf else line
            if line.endswith((".", "!", "?", ":", ";", "”", '"')) or len(line) < 42:
                paras.append(buf.strip()); buf = ""
    if buf:
        paras.append(buf.strip())
    return [x for x in paras if len(x.strip(".•·–—- ")) > 1]


def _safe_research_path(name):
    """Resolve a research filename to a Path inside RESEARCH_DIR, or None if unsafe.
    Blocks path traversal via separators / parent-dir escape, but allows a literal
    ".." inside a filename (e.g. a title ending in "...") and "--" (auto-pull titles)."""
    if not name or "/" in name or "\\" in name:
        return None
    p = RESEARCH_DIR / name
    if p.resolve().parent != RESEARCH_DIR.resolve() or not p.is_file() \
       or p.suffix.lower() not in _RESEARCH_EXT:
        return None
    return p


async def api_research_read(request):
    """One report's text + page count for the reader. Text is used for Summarize and
    as the fallback view; PDF page IMAGES are fetched one at a time via /api/research/page
    so a long report doesn't ship megabytes of base64 in a single response (was timing
    out on phones for 20+ page daily notes)."""
    name = request.query.get("file", "")
    p = _safe_research_path(name)
    if p is None:
        return web.json_response({"title": name, "paragraphs": [], "page_count": 0,
                                  "error": "not found"}, status=404)
    page_count = 0
    try:
        if p.suffix.lower() == ".pdf":
            paras = _pdf_paragraphs(p)
            # Cloud app: only chart-heavy reports render as images (rest text, keeps the
            # Fly box small). Local terminals render everything as images (original format).
            if _renders_as_images(name):
                page_count = await asyncio.to_thread(_pdf_page_count, p)
        else:
            paras = [b.strip() for b in p.read_text(encoding="utf-8", errors="replace").split("\n\n")
                     if b.strip()]
    except Exception as e:
        return web.json_response({"title": p.stem, "paragraphs": [], "page_count": 0, "error": str(e)})
    return web.json_response({"title": p.stem, "paragraphs": paras, "page_count": page_count})


_PAGECACHE_DIR = RESEARCH_DIR / ".pagecache"
# Render ONE page at a time. The Fly box is 256-512MB / shared-cpu-1x; concurrent
# PyMuPDF renders spike memory and OOM-crash the whole app (502s). This serializes
# the heavy work; cached pages skip it entirely (just a file read).
_PAGE_SEM = asyncio.Semaphore(1)


async def api_research_page(request):
    """Serve one PDF page as JPEG, rendered on first request and cached to disk so it
    only renders once. Renders are serialized (semaphore) so a tiny box never OOMs."""
    name = request.query.get("file", "")
    p = _safe_research_path(name)
    if p is None or p.suffix.lower() != ".pdf" or not _renders_as_images(name):
        return web.Response(status=404, text="not found")   # cloud: only chart reports render
    try:
        n = int(request.query.get("n", "0"))
    except ValueError:
        n = 0
    hdrs = {"Cache-Control": "private, max-age=604800"}
    cache = _PAGECACHE_DIR / f"{p.stem}__p{n}.jpg"
    try:
        if cache.is_file():
            return web.Response(body=cache.read_bytes(), content_type="image/jpeg", headers=hdrs)
    except Exception:
        pass
    async with _PAGE_SEM:                       # only one render in flight at a time
        try:                                    # double-check: another request may have just cached it
            if cache.is_file():
                return web.Response(body=cache.read_bytes(), content_type="image/jpeg", headers=hdrs)
        except Exception:
            pass
        img = await asyncio.to_thread(_pdf_page_image, p, n)
        if img is None:
            return web.Response(status=404, text="no page")
        try:
            _PAGECACHE_DIR.mkdir(exist_ok=True)
            cache.write_bytes(img)
        except Exception:
            pass
    return web.Response(body=img, content_type="image/jpeg", headers=hdrs)


# Auto-pulled Rosenberg report filenames look like  rr_<code>__<title>_<date>.pdf
_RR_RE = re.compile(r"^rr_[a-z]+__.+\.pdf$", re.I)


async def api_research_manifest(request):
    """List the auto-pulled Rosenberg report PDFs + their real titles, so a family
    desktop can mirror the same files this shared app already holds. Behind auth."""
    RESEARCH_DIR.mkdir(exist_ok=True)
    files = {p.name: int(p.stat().st_size) for p in RESEARCH_DIR.iterdir()
             if p.is_file() and _RR_RE.match(p.name)}
    try:
        titles = json.loads((RESEARCH_DIR / ".rr_titles.json").read_text(encoding="utf-8"))
    except Exception:
        titles = {}
    return web.json_response({"files": files, "titles": titles})


async def api_research_raw(request):
    """Serve one report PDF by name for the mirror client (report files only)."""
    name = request.query.get("file", "")
    p = RESEARCH_DIR / name
    safe = bool(name) and "/" not in name and "\\" not in name and _RR_RE.match(name) \
        and p.resolve().parent == RESEARCH_DIR.resolve()
    if not (safe and p.is_file()):
        return web.Response(status=404, text="not found")
    return web.FileResponse(p)


# Phrases that mark a bot-wall / paywall / consent shell rather than real article text.
# STRONG markers are unambiguous (a real article won't contain them); WEAK markers only
# count when the page is short or errored (they can appear inside legitimate prose).
_WALL_STRONG = (
    "you're not a robot", "you are not a robot", "let us know you're not a robot",
    "click the box below", "are you a robot", "verify you are a human",
    "supports javascript and cookies", "enable javascript and cookies",
    "checking your browser before", "attention required", "please enable js",
    "access to this page has been denied",
)
_WALL_WEAK = (
    "subscribe to continue", "subscriber-only", "subscribers only", "not a robot",
    "enable javascript", "disable any ad blocker", "captcha", "access denied",
    "reference id", "please enable cookies",
)


def _looks_walled(text, short_or_error=False):
    t = (text or "").lower()
    if any(m in t for m in _WALL_STRONG):
        return True
    return short_or_error and any(m in t for m in _WALL_WEAK)


async def api_article(request):
    """Resolve + fetch an article so it can be read inside the app (no new tab)."""
    url = request.query.get("url", "")
    if not url:
        return web.json_response({"title": "", "paragraphs": [], "paywalled": True})
    art = await td.fetch_article(request.app["session"], url)
    # The extractor sometimes returns a bot-wall / paywall shell (e.g. Bloomberg's
    # "click the box … you're not a robot") as if it were the article. Reject it so the
    # reader falls back to the embed (which shows a clean message) instead of the shell.
    paras = art.get("paragraphs") or []
    if paras and _looks_walled(" ".join(paras), short_or_error=len(" ".join(paras)) < 1500):
        art["paragraphs"] = []
        art["paywalled"] = True
    return web.json_response(art)


# ---- AI summarize (Google Gemini, free tier) -------------------------------
# flash-lite first: much higher free-tier limits, fast, and plenty good for
# summaries. Full flash is the fallback if lite is momentarily busy.
_GEMINI_MODELS = ("gemini-2.5-flash-lite", "gemini-2.5-flash")
_GEMINI_TRIES = 2          # attempts per model (503 "high demand" is intermittent)


def _gemini_url(model):
    return ("https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent")


async def _gemini_generate(session, key, prompt, max_out, models):
    """Call Gemini with retry + model fallback. Returns (text, error_message);
    exactly one is non-None. thinkingBudget 0 keeps the whole budget for output."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_out,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    hdrs = {"x-goog-api-key": key, "Content-Type": "application/json"}
    for model in models:
        for attempt in range(_GEMINI_TRIES):
            try:
                async with session.post(
                    _gemini_url(model), headers=hdrs, json=payload,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as r:
                    data = await r.json()
                if r.status == 200:
                    try:
                        txt = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    except Exception:
                        txt = ""
                    if txt:
                        return txt, None
                    break                                  # empty -> next model
                err = (((data or {}).get("error") or {}).get("message")
                       or f"Gemini error {r.status}")
                if r.status not in (429, 500, 502, 503):
                    return None, err
            except Exception as e:
                _ = e
            if attempt < _GEMINI_TRIES - 1:
                await asyncio.sleep(1.0 + attempt)
    return None, "Gemini is busy right now. Give it a few seconds and try again."


async def api_summarize(request):
    """Summarize the currently-open article / report text via Gemini.

    The reader POSTs the visible text so this works uniformly for news and
    saved Rosenberg reports without re-fetching or re-extracting server-side.
    """
    key = _gemini_key()
    if not key:
        return web.json_response(
            {"error": "AI summaries aren't set up on this terminal yet."}, status=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = (body.get("text") or "").strip()
    title = (body.get("title") or "").strip()
    if len(text) < 200:
        return web.json_response(
            {"error": "Not enough text to summarize."}, status=400)
    is_report = body.get("kind") == "report"
    if is_report:
        # Research reports are long and dense — feed much more in, let it write a
        # proper briefing, and prefer the stronger model (reports are read rarely,
        # so quota isn't a concern). flash-lite stays as the fallback.
        text = text[:90000]
        prompt = (
            "You are a senior markets strategist briefing a portfolio manager who "
            "will not read the full research report below but needs its full "
            "substance. Write a thorough, structured briefing.\n\n"
            "Use these exact section headers, each on its own line:\n"
            "Thesis: 1 to 2 sentences on the core argument or call.\n"
            "Key points: 6 to 10 bullets covering the main arguments and evidence, "
            "with the specific data, numbers, price levels, and dates the report "
            "cites.\n"
            "Markets and positioning: what it implies for rates, equities, credit, "
            "FX, or commodities, plus any specific trades or positioning the report "
            "mentions.\n"
            "Risks and caveats: what the author flags as risks or what could go "
            "wrong.\n"
            "Bottom line: 1 to 2 sentences with the actionable takeaway.\n\n"
            "Be substantive and specific, not vague. Preserve every important "
            "figure. Do not use em dashes anywhere; use commas or periods instead. "
            "Do not add any preamble before the first header.\n\n"
            f"TITLE: {title}\n\nREPORT:\n{text}"
        )
        max_out = 3200
        models = ("gemini-2.5-flash", "gemini-2.5-flash-lite")
    else:
        text = text[:30000]
        prompt = (
            "You are a sharp financial-markets analyst. Summarize the article "
            "below for a busy reader who wants the gist fast.\n\n"
            "Format:\n"
            "- One short overview sentence.\n"
            "- Then 3 to 6 bullet points of the key takeaways.\n"
            "Keep specific numbers, names, and dates. Be concise and neutral. "
            "Do not use em dashes anywhere; use commas or periods instead. "
            "Do not add any preamble, title, or closing remark.\n\n"
            f"TITLE: {title}\n\nTEXT:\n{text}"
        )
        max_out = 1400
        models = _GEMINI_MODELS
    text, err = await _gemini_generate(request.app["session"], key, prompt, max_out, models)
    if err:
        return web.json_response({"error": err}, status=502)
    return web.json_response({"summary": text})


async def _ticker_ai_context(session, ticker):
    """A compact, factual digest of a ticker (live quote + fundamentals + price-history
    peaks/lows with dates) so Gemini answers from REAL terminal data, not guesses."""
    lines = [f"Symbol: {ticker.upper()}"]
    try:
        fund = await td.fetch_fundamentals(session, ticker)
    except Exception:
        fund = None
    if fund:
        for k, v in fund.items():
            if k.startswith("_") or v in (None, ""):
                continue
            lines.append(f"{k}: {v}")
    # Price-history digest (close-based). StockAnalysis caps stocks at ~10y; indices go
    # further. Label the window so the model doesn't call a 10y high an all-time high.
    try:
        bars = await td.fetch_history(session, ticker, tf="ALL")
    except Exception:
        bars = None
    rows = [(b.get("t"), b.get("c")) for b in (bars or []) if b.get("t") and b.get("c") is not None]
    if rows:
        d0, d1 = rows[0][0], rows[-1][0]
        cur_d, cur_c = rows[-1]
        hi_d, hi_c = max(rows, key=lambda r: r[1])
        lo_d, lo_c = min(rows, key=lambda r: r[1])
        lines.append(f"Price history available: {d0} to {d1} (daily closes)")
        lines.append(f"Latest close: {cur_c} on {cur_d}")
        lines.append(f"Highest close in window: {hi_c} on {hi_d}")
        lines.append(f"Lowest close in window: {lo_c} on {lo_d}")
        # last 52 weeks
        try:
            cutoff = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
            yr = [r for r in rows if r[0] >= cutoff]
            if yr:
                yh = max(yr, key=lambda r: r[1]); yl = min(yr, key=lambda r: r[1])
                lines.append(f"52-week high close: {yh[1]} on {yh[0]}; low: {yl[1]} on {yl[0]}")
        except Exception:
            pass
        # highest AND lowest close per calendar year (peaks and troughs over time)
        hi_year, lo_year = {}, {}
        for d, c in rows:
            y = d[:4]
            if y not in hi_year or c > hi_year[y][1]:
                hi_year[y] = (d, c)
            if y not in lo_year or c < lo_year[y][1]:
                lo_year[y] = (d, c)
        if len(hi_year) > 1:
            yrs = "; ".join(
                f"{y}: high {hi_year[y][1]} ({hi_year[y][0]}), low {lo_year[y][1]} ({lo_year[y][0]})"
                for y in sorted(hi_year))
            lines.append(f"Yearly high and low close by year: {yrs}")
    return "\n".join(lines)


async def api_ask_ticker(request):
    """Free-form Q&A about a ticker via Gemini, grounded in the terminal's live data."""
    key = _gemini_key()
    if not key:
        return web.json_response(
            {"error": "AI isn't set up on this terminal yet. Open a report and tap "
                      "Summarize once to add your Gemini key."}, status=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ticker = (body.get("ticker") or "").strip()
    question = (body.get("question") or "").strip()
    if not ticker or len(question) < 2:
        return web.json_response({"error": "Ask a question about the ticker."}, status=400)
    context = await _ticker_ai_context(request.app["session"], ticker)
    prompt = (
        "You are a precise financial-markets analyst answering a question about a specific "
        "security inside a trading terminal. Use the LIVE DATA below for anything factual or "
        "numeric (current price, dates, highs, lows, peaks, ranges); it comes from the "
        "terminal and is authoritative. The price history is limited to the window shown, so "
        "if the question needs data outside that window, say so plainly instead of guessing. "
        "You may add brief general context from your own knowledge, but keep it clearly "
        "separate and note that it may be out of date. Do NOT give personalized investment "
        "advice or buy and sell recommendations. Do not use em dashes; use commas or periods. "
        "Be concise, specific, and answer the actual question.\n\n"
        f"LIVE DATA for {ticker.upper()}:\n{context}\n\nQUESTION: {question}"
    )
    text, err = await _gemini_generate(request.app["session"], key, prompt, 1200, _GEMINI_MODELS)
    if err:
        return web.json_response({"error": err}, status=502)
    return web.json_response({"answer": text})


async def api_set_gemini(request):
    """Store a Gemini API key on THIS terminal (desktop builds) so summaries work
    without touching the repo. No-op on the cloud app, where the env secret wins."""
    if os.environ.get("GEMINI_API_KEY", "").strip():
        return web.json_response({"ok": True, "note": "already active"})
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = (body.get("key") or "").strip()
    if len(key) < 20:
        return web.json_response({"error": "That doesn't look like a valid key."}, status=400)
    try:
        save_gemini_key(key)
    except Exception as e:
        return web.json_response({"error": f"Couldn't save the key: {e}"}, status=500)
    return web.json_response({"ok": True})


# ---- Daily Brief: Gemini reads the monitor + news + research -> what happened today
def _fmt_market_snapshot():
    lines = []
    for sec in build_monitor()["sections"]:
        rows = [f"{r['label']} {r['price']} {r['chg']} ({r['pct']})"
                for r in sec["rows"] if r.get("price") not in ("--", "", None)]
        if rows:
            lines.append(f"[{sec['title']}] " + "; ".join(rows))
    return "\n".join(lines)


async def _gather_brief_data(session):
    market = _fmt_market_snapshot()
    heads = []
    try:
        for s in await td.fetch_news_dashboard(session, per=8):
            for it in s["items"]:
                heads.append(f"- {it['title']} ({it.get('source', '')})")
    except Exception:
        pass
    news = "\n".join(heads[:60])
    # Rosenberg reports from roughly the last 2 days (today's desk research)
    reports, cutoff = [], (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        titles = json.loads((RESEARCH_DIR / ".rr_titles.json").read_text(encoding="utf-8"))
    except Exception:
        titles = {}
    daily = []                                        # (date, path) for the daily notes
    if RESEARCH_DIR.exists():
        for p in sorted(RESEARCH_DIR.glob("rr_*.pdf")):
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", p.stem)
            d = dm.group(1) if dm else ""
            if d and d >= cutoff:
                reports.append(f"- {_clean_report_title(titles.get(p.name) or p.stem)} ({d})")
                if p.name.startswith("rr_daily__"):
                    daily.append((d, p))
    # Full text of the 2 most recent daily notes (Breakfast/Early Morning with Dave)
    # so the brief can tie Rosenberg's ACTUAL views to today's tape, not just titles.
    daily.sort(reverse=True)
    detail = []
    for d, p in daily[:2]:
        try:
            paras = await asyncio.to_thread(_pdf_paragraphs, p)
            body = " ".join(paras)[:9000]
            detail.append(f"### {_clean_report_title(titles.get(p.name) or p.stem)} ({d})\n{body}")
        except Exception:
            pass
    return market, news, "\n".join(reports[:20]), "\n\n".join(detail)


async def api_daily_brief(request):
    """Gemini reads the live monitor, today's news, and recent research and writes a
    concise what-happened-today desk brief."""
    key = _gemini_key()
    if not key:
        return web.json_response(
            {"error": "AI isn't set up on this terminal yet. Open a report and tap "
                      "Summarize once to add your Gemini key."}, status=503)
    session = request.app["session"]
    market, news, reports, detail = await _gather_brief_data(session)
    if not (market or news):
        return web.json_response({"error": "No market or news data available right now."},
                                 status=502)
    today = datetime.now().strftime("%A, %B %d, %Y")
    prompt = (
        f"You are the chief markets strategist writing the end-of-day desk brief for {today}. "
        "Your job is to CONNECT THE DOTS across the data below, not just list it. Explain WHY "
        "markets moved by tying each notable move to its likely driver in the news or research, "
        "and cross-check what Rosenberg Research argues against what markets actually did today.\n\n"
        "Use these exact section headers, each on its own line:\n"
        "Markets: how the major assets moved today (equity indices, rates, FX, commodities, "
        "crypto), with the actual numbers. For each significant move, explain the likely driver "
        "by pointing to a specific news item or research view below, for example 'the 10Y yield "
        "fell 4bp, likely on the softer than expected jobs report'. Explain the moves, do not "
        "just report them.\n"
        "Market drivers: the 3 to 5 developments (news or data) that most moved markets today, "
        "each with which assets or tickers they moved and in what direction.\n"
        "Rosenberg view: what Rosenberg's latest notes actually argue, and whether today's "
        "market action confirms or contradicts their calls. Reference their specific points and "
        "tie them to the relevant tickers or yields. Skip this header entirely if there is no "
        "research below.\n"
        "Bottom line: the through-line connecting today's moves, news, and Rosenberg's views, "
        "plus what to watch next.\n\n"
        "Draw explicit cause-and-effect links wherever the data supports them. Be specific with "
        "numbers, names, and tickers. Do not invent a driver you cannot ground in the data below; "
        "if a move has no clear cause in the data, say the driver is unclear rather than guessing. "
        "Do not use em dashes anywhere; use commas or periods instead. Do not add any preamble "
        "before the first header.\n\n"
        f"MARKET SNAPSHOT:\n{market or 'unavailable'}\n\n"
        f"NEWS HEADLINES:\n{news or 'unavailable'}\n\n"
        f"RECENT RESEARCH PUBLISHED:\n{reports or 'none'}\n\n"
        f"ROSENBERG DAILY NOTES (full text, for tying their views to the tape):\n{detail or 'none'}"
    )
    text, err = await _gemini_generate(
        session, key, prompt, 3200, ("gemini-2.5-flash", "gemini-2.5-flash-lite"))
    if err:
        return web.json_response({"error": err}, status=502)
    return web.json_response(
        {"brief": text, "generated_at": datetime.now().strftime("%b %d, %Y  %I:%M %p")})


# Hosts we refuse to proxy — the embed endpoint fetches arbitrary URLs, so keep it from
# being used to reach this machine / the LAN from a phone on the same Wi-Fi.
_EMBED_BLOCK = re.compile(
    r"^(localhost|127\.|0\.0\.0\.0|10\.|192\.168\.|169\.254\.|::1|"
    r"172\.(1[6-9]|2\d|3[01])\.)")

# Shown inside the reader iframe when the target page is a bot-wall / JS challenge.
_EMBED_BLOCKED_HTML = (
    "<!doctype html><meta charset='utf-8'>"
    "<div style=\"font:14px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;"
    "color:#bbb;background:#111;height:100%;margin:0;display:flex;align-items:center;"
    "justify-content:center;text-align:center;padding:24px;box-sizing:border-box\">"
    "<div>This page can't be shown here — the site requires a live browser "
    "(bot protection).<br>Use <b style='color:#e0a63c'>“Open original ↗”</b> above "
    "to read it.</div></div>")


async def api_embed(request):
    """Proxy a page so it renders INSIDE the reader's iframe instead of a new tab.

    Sites that block framing do so with an X-Frame-Options / CSP `frame-ancestors`
    response header; because we serve the fetched HTML from our own origin those
    headers never reach the browser, so the iframe is allowed. A <base> tag is injected
    so the page's relative CSS/images still resolve back to the original site.
    """
    url = request.query.get("url", "")
    if not url.startswith(("http://", "https://")):
        return web.Response(text="bad url", status=400)
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
    if _EMBED_BLOCK.match(host):
        return web.Response(text="blocked host", status=403)
    try:
        async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as s:
            async with s.get(url, headers=td.UA,
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
                ctype = r.headers.get("Content-Type", "text/html")
                status = r.status
                raw = await r.read()
    except Exception as e:
        return web.Response(text=f"Couldn't load the page ({e}).", status=502)

    if "html" in ctype.lower():
        doc = raw.decode("utf-8", "ignore")
        # Bot-wall / JS-gate detection: some gov & news sites (e.g. CBO) return a tiny
        # "please enable JS" challenge to non-browsers. Embedding that is useless, so
        # show a clean message pointing at "Open original" instead of a blank white box.
        _vis = " ".join(re.sub(r"<[^>]+>", " ",
                        re.sub(r"<script.*?</script>|<style.*?</style>", "", doc,
                               flags=re.S | re.I)).split())
        if _looks_walled(_vis, short_or_error=(status >= 400 or len(_vis) < 240)):
            return web.Response(text=_EMBED_BLOCKED_HTML, content_type="text/html",
                                charset="utf-8")
        # Render a STATIC snapshot: drop scripts so client-side apps (Next.js etc.) don't
        # try to hydrate under our proxy origin and throw — the server-rendered article
        # text stays. This also disarms most paywall/consent overlays that JS injects.
        doc = re.sub(r"<script\b[^>]*>.*?</script>", "", doc, flags=re.I | re.S)
        doc = re.sub(r"<script\b[^>]*/>", "", doc, flags=re.I)
        # drop any in-document CSP meta that would re-impose frame-ancestors
        doc = re.sub(r'<meta[^>]+http-equiv=["\']?content-security-policy["\'][^>]*>',
                     "", doc, flags=re.I)
        base = f'<base href="{html.escape(url, quote=True)}">'
        if re.search(r"<head[^>]*>", doc, re.I):
            doc = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + base, doc, count=1, flags=re.I)
        else:
            doc = base + doc
        return web.Response(text=doc, content_type="text/html", charset="utf-8")
    return web.Response(body=raw, content_type=ctype.split(";")[0].strip() or "application/octet-stream")


def _fmt_money(v):
    if v is None:
        return "--"
    a = abs(v)
    if a >= 1e9:
        return f"{v/1e9:,.1f}B"
    if a >= 1e6:
        return f"{v/1e6:,.1f}M"
    return f"{v:,.2f}"


async def api_financials(request):
    """SEC financials (income / balance sheet / cash flow) for a stock, mirroring the
    terminal's FA view; ETFs return top holdings instead. Cells are pre-formatted."""
    s = request.app["session"]
    ticker = request.query.get("ticker", "AAPL").upper().strip()
    etf = await td.fetch_etf_holdings(s, ticker)
    if etf:
        return web.json_response({"ticker": ticker, "type": "etf", "etf_holdings": etf})
    fin = await td.fetch_financials(s, ticker)
    if not fin:
        return web.json_response({"ticker": ticker, "type": "stock", "statements": []})
    pcol, pnq = fin.get("partial_year"), fin.get("partial_nq")
    statements = fin["statements"]
    years = sorted({y for _n, m in statements for ser in m.values() for y, _ in ser})

    def col_label(y):
        return (f"{y}·{pnq}Q" if pnq else f"{y}*") if y == pcol else str(y)

    out = []
    for name, metrics in statements:
        rows = []
        for label, series in metrics.items():
            d = dict(series)
            kind = ("fcf" if label == "Free Cash Flow" else
                    "pct" if label == "Payout Ratio" else
                    "pe" if label == "P/E (yr-end)" else "money")
            cells = []
            for y in years:
                v = d.get(y)
                cells.append("--" if v is None else
                             f"{v:.0f}%" if kind == "pct" else
                             f"{v:.1f}x" if kind == "pe" else _fmt_money(v))
            rows.append({"label": label, "kind": kind, "cells": cells})
        out.append({"name": name, "rows": rows})

    holders = await td.fetch_institutional_holders(s, ticker)
    return web.json_response({
        "ticker": ticker, "type": "stock",
        "years": [{"label": col_label(y), "partial": y == pcol} for y in years],
        "partial_year": pcol, "partial_nq": pnq,
        "statements": out,
        "holders": ({"inst_pct": holders.get("inst_pct"),
                     "rows": holders.get("holders")} if holders else None),
    })


# --- portfolio (PORT): treasuries marked live to yields, futures to contract ---
PORT_FILE = HERE.parent / "portfolio.json"
_TENORS = [(0.75, "US3M"), (2.5, "US2Y"), (4, "US3Y"), (6, "US5Y"),
           (8.5, "US7Y"), (15, "US10Y"), (25, "US20Y"), (99, "US30Y")]


def _tenor_sym(years):
    for thr, sym in _TENORS:
        if years < thr:
            return sym
    return "US30Y"


def _bond_price(coupon, years, ytm, freq=2):
    """Clean price per 100 face for a coupon bond at a given yield-to-maturity %."""
    if years <= 0:
        return 100.0
    n = max(int(round(years * freq)), 1)
    c, y = coupon / freq, ytm / 100 / freq
    if y == 0:
        return c * n + 100
    return sum(c / (1 + y) ** t for t in range(1, n + 1)) + 100 / (1 + y) ** n


def _live(sym):
    q = dash.STATE.get(sym)
    return q.price if q and q.price else 0.0


def _live_change(sym):
    q = dash.STATE.get(sym)
    return q.change if q and q.change is not None else 0.0


def _yield(sym):
    q = dash.STATE.get(sym)
    return (q.price, q.change or 0.0) if q and q.price else (None, 0.0)


async def api_portfolio(request):
    if os.environ.get("MKT_NO_PORT"):              # portfolio disabled in this deploy
        return web.json_response({"error": "portfolio disabled"}, status=404)
    try:
        pf = json.loads(PORT_FILE.read_text())
    except Exception:
        return web.json_response({"error": "no portfolio.json found"}, status=404)
    q = dash.STATE.get("US3M")
    rate = q.price if q and q.price else None
    now = datetime.now()

    tre_out, tre_mv, tre_daily, tre_coupon = [], 0.0, 0.0, 0.0
    for nt in pf.get("treasuries", []):
        face = nt.get("face", 0)
        mat = datetime.strptime(nt["maturity"], "%Y-%m-%d")
        years = max((mat - now).days / 365.25, 0)
        y, dchg = _yield(_tenor_sym(years))
        tre_coupon += face * nt.get("coupon", 0) / 100
        if y is None:
            px, mv, dval, ytxt = 100.0, face, 0.0, None
        else:
            px = _bond_price(nt.get("coupon", 0), years, y)
            mv = px / 100 * face
            dval = (px - _bond_price(nt.get("coupon", 0), years, y - dchg)) / 100 * face
            ytxt = y
        tre_mv += mv
        tre_daily += dval
        tre_out.append({"name": nt.get("name", "?"), "ytm": ytxt,
                        "price": px, "mv": mv, "dtoday": dval})

    sleeves = pf.get("cash", pf.get("positions", []))
    sl_out, income, cash_total = [], tre_coupon, 0.0
    for p in sleeves:
        amt = p.get("amount", 0)
        cash_total += amt
        prate = p.get("rate", rate)
        if p.get("yields") and prate is not None:
            ann = amt * prate / 100
            income += ann
            sl_out.append({"name": p.get("name", "?"), "amount": amt, "rate": prate, "annual": ann})
        else:
            sl_out.append({"name": p.get("name", "?"), "amount": amt, "rate": None, "annual": None})

    fut = pf.get("futures")
    fut_out, fut_eq, fut_funded = None, 0.0, 0
    if fut:
        fut_funded = fut.get("net_funded", 0)
        base_eq = fut.get("equity_baseline", fut.get("equity", 0))
        contract, base_px = fut.get("contract"), fut.get("baseline_price")
        qty, mult = fut.get("qty", 0), fut.get("mult", 1000)
        sign = 1 if fut.get("side", "long") == "long" else -1
        live = _live(contract) if contract else 0
        move = (live - base_px) * mult * qty * sign if (live and base_px) else 0
        fut_eq = base_eq + move
        pnl = fut_eq - fut_funded
        ret = (pnl / fut_funded * 100) if fut_funded else 0
        fut_out = {"desc": f"{qty} {fut.get('side', 'long').upper()}  {fut.get('name', contract)}",
                   "live": live, "move": move, "equity": fut_eq, "pnl": pnl, "ret": ret,
                   "funded": fut_funded}

    net_liquid = cash_total + fut_eq + tre_mv
    dep = pf.get("deposited")
    since = None
    if dep:
        chg = net_liquid - dep
        since = {"deposited": dep, "net_liquid": net_liquid, "change": chg, "pct": chg / dep * 100}
    return web.json_response({
        "name": pf.get("name", "Portfolio"),
        "treasuries": tre_out, "tre_mv": tre_mv, "tre_daily": tre_daily,
        "sleeves": sl_out, "cash_total": cash_total, "futures": fut_out,
        "net_liquid": net_liquid, "income": income, "income_day": income / 365,
        "fut_pnl": (fut_eq - fut_funded) if fut else 0, "since": since,
    })


# --- access / "last seen" tracker (security): record EVERY authenticated device that
# opens the shared app, so we can spot anyone who isn't Robert or Ezra. Each distinct
# device (name + browser) is one entry with its IPs, first/last seen, and hit count.
# Stored on the Fly volume so it survives redeploys.
SEEN_FILE = RESEARCH_DIR / ".last_seen.json"
_seen_lastwrite = {}


def _client_ip(request):
    return ((request.headers.get("Fly-Client-IP")
             or request.headers.get("X-Forwarded-For", "").split(",")[0]
             or (request.remote or "")).strip())[:60]


def _record_seen(name, ip="", ua=""):
    name = (name or "unknown").strip()[:40] or "unknown"
    ua = (ua or "")[:200]
    key = hashlib.sha1((name + "|" + ua).encode()).hexdigest()[:12]
    now = datetime.now(timezone.utc)
    last = _seen_lastwrite.get(key)
    if last and (now - last).total_seconds() < 60:     # throttle rapid reloads
        return
    _seen_lastwrite[key] = now
    try:
        RESEARCH_DIR.mkdir(exist_ok=True)
        try:
            d = json.loads(SEEN_FILE.read_text())
        except Exception:
            d = {}
        dev = d.get("devices") or {}
        rec = dev.get(key) or {"name": name, "ua": ua, "first": now.isoformat(), "ips": []}
        rec["name"] = name
        rec["last"] = now.isoformat()
        rec["hits"] = int(rec.get("hits", 0)) + 1
        if ip and ip not in rec["ips"]:
            rec["ips"] = ([ip] + rec["ips"])[:6]
        dev[key] = rec
        if len(dev) > 80:                              # cap; keep most recent
            dev = dict(sorted(dev.items(), key=lambda kv: kv[1].get("last", ""),
                              reverse=True)[:80])
        d["devices"] = dev
        SEEN_FILE.write_text(json.dumps(d))
    except Exception:
        pass


async def api_last_seen(request):
    try:
        d = json.loads(SEEN_FILE.read_text())
    except Exception:
        d = {}
    return web.json_response(d)


def _greeting_name():
    """Whom to greet on the splash. Per-install (never shipped via the updater):
    env MKT_USER, else a local webapp/greeting.txt, else no name."""
    import os
    name = os.environ.get("MKT_USER", "").strip()
    if not name:
        try:
            name = (HERE / "greeting.txt").read_text(encoding="utf-8").strip()
        except Exception:
            name = ""
    return name.split()[0] if name else ""


# --- optional password gate (enabled only when MKT_PASSWORD is set, i.e. in the
# cloud). Local runs leave it unset, so nothing changes there. ---
def _auth_token():
    pw = os.environ.get("MKT_PASSWORD", "")
    return hashlib.sha256(("kkt:" + pw).encode()).hexdigest() if pw else None


_LOGIN_HTML = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kessler-Katznelson Terminal</title>
<style>html,body{height:100%;margin:0;background:#0a0a0a;color:#e0a63c;
font:15px/1.5 ui-monospace,Menlo,monospace;display:flex;align-items:center;justify-content:center}
form{text-align:center;padding:28px 30px;border:1px solid #4a3a15;border-radius:8px;background:#111}
h1{font-size:16px;letter-spacing:.08em;margin:0 0 18px}
input{background:#1a1a1a;border:1px solid #2a2a2a;color:#fff;font:15px ui-monospace,monospace;
padding:10px 12px;width:220px;outline:none;border-radius:4px}
button{margin-top:12px;display:block;width:100%;background:#e0a63c;color:#000;border:0;font-weight:700;
padding:10px;border-radius:4px;cursor:pointer;font-family:inherit}
.err{color:#ff6b6b;font-size:12px;margin-top:10px;min-height:14px}</style>
<form method="post" action="/login">
<h1>KESSLER-KATZNELSON TERMINAL</h1>
<input type="password" name="password" placeholder="password" autofocus autocomplete="current-password">
<button type="submit">Enter</button>
<div class="err">{{ERR}}</div>
</form>"""


_COOKIE_MAXAGE = 10 * 365 * 24 * 3600           # ~10 years: log in once, stay in


def _set_auth(resp, token):
    resp.set_cookie("kkt_auth", token, max_age=_COOKIE_MAXAGE, httponly=True, samesite="Lax")
    return resp


@web.middleware
async def auth_mw(request, handler):
    token = _auth_token()
    if not token:                                  # no password set -> open (local)
        return await handler(request)
    # PWA icons + manifest must load WITHOUT the login cookie — iOS fetches the
    # home-screen icon in a cookieless context, so gating /static/ breaks the K logo.
    # There's no data under /static/ (it's just the shell + assets); /api stays gated.
    # The dynamic manifest handler decides for itself what to reveal based on cookies.
    if request.path.startswith("/static/") or request.path == "/app.webmanifest":
        return await handler(request)
    pw = os.environ.get("MKT_PASSWORD", "")
    # already-remembered device
    if request.cookies.get("kkt_auth") == token:
        return await handler(request)
    # magic link: any URL with ?k=<password> remembers this device forever, then
    # redirects to a clean URL. Bookmark it once and never type the password again.
    if request.query.get("k", "") == pw:
        resp = _set_auth(web.HTTPFound(request.path or "/"), token)
        if request.query.get("u"):     # ?u=<name> on the magic link also sets the greeting
            resp.set_cookie("kkt_name", request.query.get("u"),
                            max_age=_COOKIE_MAXAGE, samesite="Lax")
        return resp
    if request.path == "/login":
        if request.method == "POST" and (await request.post()).get("password", "") == pw:
            return _set_auth(web.HTTPFound("/"), token)
        err = "Wrong password." if request.method == "POST" else ""
        return web.Response(text=_LOGIN_HTML.replace("{{ERR}}", err),
                            content_type="text/html", status=401 if err else 200)
    if request.path.startswith(("/api", "/ws")):   # don't redirect data/socket calls
        return web.Response(status=401, text="auth required")
    return web.HTTPFound("/login")


async def index(request):
    page = (HERE / "static" / "index.html").read_text(encoding="utf-8")
    # tell the frontend whether the portfolio (PORT) view is disabled in this deploy,
    # and whether local-only tools (Investment Calculator) are enabled. Local tools are
    # ON only on Ezra's Mac (Darwin) — Fly is Linux, Robert's build is Windows — so the
    # button never shows on the shared app or Robert's terminal. Env can force it.
    import platform
    local_tools = (os.environ.get("MKT_LOCAL_TOOLS") == "1"
                   or (platform.system() == "Darwin" and not os.environ.get("MKT_PASSWORD")))
    cfg = "<script>window.NO_PORT=%s;window.LOCAL_TOOLS=%s;</script>" % (
        "true" if os.environ.get("MKT_NO_PORT") else "false",
        "true" if local_tools else "false")
    # per-device greeting: ?u=<name> sets & remembers it, else the saved cookie, else env/file
    who = request.query.get("u") or request.cookies.get("kkt_name") or _greeting_name()
    # security: on the gated cloud app, log every authenticated open (name if known,
    # else "unknown") with its browser + IP, so any device that isn't Robert/Ezra shows up
    if os.environ.get("MKT_PASSWORD"):
        _record_seen(request.query.get("u") or request.cookies.get("kkt_name"),
                     _client_ip(request), request.headers.get("User-Agent", ""))
    page = page.replace("{{GREETING_NAME}}", html.escape(who or "", quote=True)) \
               .replace("{{APP_CONFIG}}", cfg)
    resp = web.Response(text=page, content_type="text/html")
    if request.query.get("u"):
        resp.set_cookie("kkt_name", request.query.get("u"),
                        max_age=_COOKIE_MAXAGE, samesite="Lax")
    return resp


_MANIFEST_BASE = {
    "name": "Kessler-Katznelson Terminal",
    "short_name": "Kessler-Katznelson",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#000000",
    "theme_color": "#000000",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    ],
}


async def app_manifest(request):
    """Dynamic web-app manifest. For an ALREADY-authenticated request we bake an
    auto-login magic link into start_url, so the iOS home-screen app (which gets its
    own cookie jar, separate from Safari) logs itself in and greets by name — no
    password ever. Anonymous fetches get a plain start_url, so the token never leaks.
    Requires crossorigin="use-credentials" on the <link rel="manifest"> so the browser
    sends the cookie when fetching this."""
    m = dict(_MANIFEST_BASE)
    token = _auth_token()
    authed = (not token) or (request.cookies.get("kkt_auth") == token)
    if token and authed:
        pw = os.environ.get("MKT_PASSWORD", "")
        name = request.query.get("u") or request.cookies.get("kkt_name") or _greeting_name()
        m["start_url"] = "/?k=" + quote(pw) + (("&u=" + quote(name)) if name else "")
    else:
        m["start_url"] = "/"
    return web.json_response(m, content_type="application/manifest+json")


async def _mirror_once(session, url, key):
    """Pull any report PDFs the shared app has that we don't, into local research/."""
    RESEARCH_DIR.mkdir(exist_ok=True)
    # ?k=<pw> makes auth_mw set the cookie via a 302; aiohttp follows it and keeps
    # the cookie for the rest of this session, so later /raw calls are authed too.
    async with session.get(f"{url}/api/research/manifest", params={"k": key},
                           timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status != 200:
            raise RuntimeError(f"manifest HTTP {r.status}")
        data = await r.json()
    remote = data.get("files") or {}
    new = 0
    for name, size in remote.items():
        if not _RR_RE.match(name):
            continue
        dst = RESEARCH_DIR / name
        if dst.exists() and dst.stat().st_size == size:
            continue
        async with session.get(f"{url}/api/research/raw", params={"file": name},
                               timeout=aiohttp.ClientTimeout(total=60)) as fr:
            if fr.status != 200:
                continue
            body = await fr.read()
        tmp = dst.with_suffix(dst.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(dst)
        new += 1
    # merge the real-headline sidecar so titles show cleanly, not filenames
    tf = RESEARCH_DIR / ".rr_titles.json"
    try:
        cur = json.loads(tf.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.update(data.get("titles") or {})
    tf.write_text(json.dumps(cur), encoding="utf-8")
    return new


async def _research_mirror_loop(app):
    """Desktop only: mirror the shared cloud app's Rosenberg reports into local
    research/ so this terminal shows the same reports the shared app already holds.
    No-op on the cloud source (its own ROSENBERG creds) and when unconfigured."""
    if os.environ.get("ROSENBERG_EMAIL"):         # this IS the source — don't mirror
        return
    cfg = _mirror_cfg()
    if not cfg:
        return
    url, key = cfg
    import traceback
    while True:
        try:
            n = await _mirror_once(app["session"], url, key)
            print(f"  research mirror: {n} new report(s) from {url}", flush=True)
        except Exception:
            print("  research mirror ERROR:\n" + traceback.format_exc(), flush=True)
        await asyncio.sleep(2 * 3600)             # check every 2h


async def _rosenberg_cloud_loop():
    """Cloud copy only: if Rosenberg creds are set (Fly secrets), download the daily
    Breakfast/Early Morning notes into research/ on startup and every 6h, so the
    hosted app (desktop + phone) shows them. No-op when the secrets aren't set."""
    if not (os.environ.get("ROSENBERG_EMAIL") and os.environ.get("ROSENBERG_PASSWORD")):
        print("  rosenberg cloud: no creds set — skipping", flush=True)
        return
    import rosenberg, traceback                   # sibling module (pure stdlib)
    while True:
        try:
            n = await asyncio.to_thread(rosenberg.sync_cloud)
            print(f"  rosenberg cloud: sync done — {n} new report(s)", flush=True)
        except Exception:
            print("  rosenberg cloud ERROR:\n" + traceback.format_exc(), flush=True)
        await asyncio.sleep(2 * 3600)             # check every 2h so morning reports arrive fast


async def on_start(app):
    app["session"] = aiohttp.ClientSession()
    for fn in (dash.cnbc_loop, dash.binance_loop, dash.fred_loop, dash.cftc_loop):
        app["tasks"].append(asyncio.create_task(fn()))
    app["tasks"].append(asyncio.create_task(monitor_broadcast(app)))
    app["tasks"].append(asyncio.create_task(_rosenberg_cloud_loop()))
    app["tasks"].append(asyncio.create_task(_research_mirror_loop(app)))


async def on_cleanup(app):
    for ws in list(app.get("ws_clients", ())):
        await ws.close()
    for t in app["tasks"]:
        t.cancel()
    await app["session"].close()


def make_app():
    app = web.Application(middlewares=[auth_mw])
    app["tasks"] = []
    app["ws_clients"] = set()
    app.router.add_get("/", index)
    app.router.add_get("/app.webmanifest", app_manifest)
    app.router.add_get("/ws", api_ws)
    app.router.add_get("/api/monitor", api_monitor)
    app.router.add_get("/api/sections", api_sections)
    app.router.add_post("/api/add", api_add)
    app.router.add_get("/api/chart", api_chart)
    app.router.add_get("/api/security", api_security)
    app.router.add_get("/api/symsearch", api_symsearch)
    app.router.add_get("/api/financials", api_financials)
    app.router.add_get("/api/portfolio", api_portfolio)
    app.router.add_get("/api/news", api_news)
    app.router.add_get("/api/news_board", api_news_board)
    app.router.add_get("/api/research", api_research)
    app.router.add_get("/api/research/read", api_research_read)
    app.router.add_get("/api/research/page", api_research_page)
    app.router.add_get("/api/research/manifest", api_research_manifest)
    app.router.add_get("/api/research/raw", api_research_raw)
    app.router.add_get("/api/article", api_article)
    app.router.add_post("/api/summarize", api_summarize)
    app.router.add_post("/api/ask_ticker", api_ask_ticker)
    app.router.add_get("/api/daily_brief", api_daily_brief)
    app.router.add_get("/api/last_seen", api_last_seen)
    app.router.add_post("/api/set_gemini", api_set_gemini)
    app.router.add_get("/api/embed", api_embed)
    app.router.add_static("/static/", HERE / "static")
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    return app


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "set-gemini":
        import getpass
        k = getpass.getpass("  Paste your Gemini API key (hidden): ").strip()
        if k:
            save_gemini_key(k)
            print("  Saved. AI summaries are now on — restart the terminal.")
        else:
            print("  Nothing entered; no change.")
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "set-mirror":
        import getpass
        u = (sys.argv[2] if len(sys.argv) > 2 else
             input("  Shared app URL [https://kessler-terminal.fly.dev]: ").strip()
             or "https://kessler-terminal.fly.dev")
        k = getpass.getpass("  Shared app password (hidden): ").strip()
        if k:
            save_mirror_cfg(u, k)
            print(f"  Saved. This terminal will mirror reports from {u.rstrip('/')} — restart it.")
        else:
            print("  Nothing entered; no change.")
        sys.exit(0)
    host = os.environ.get("MKT_HOST", "127.0.0.1")   # 0.0.0.0 = LAN/phone access
    port = int(os.environ.get("MKT_PORT") or os.environ.get("PORT") or "8787")
    print(f"  Kessler-Katznelson web  ->  http://127.0.0.1:{port}", flush=True)
    if host == "0.0.0.0":
        ip = _lan_ip()
        if ip:
            print(f"  iPhone (same Wi-Fi)     ->  http://{ip}:{port}"
                  f"   (Safari > Share > Add to Home Screen)", flush=True)
    print("  Keep this window open while using the phone. Ctrl-C to stop.", flush=True)
    web.run_app(make_app(), host=host, port=port, print=None)
