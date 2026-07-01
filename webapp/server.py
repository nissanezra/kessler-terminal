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
import html
import json
import os
import re
import socket
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # import sibling modules

import dashboard as dash       # noqa: E402
import terminal_data as td     # noqa: E402


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
    return out


async def _research_feed(session, feed):
    try:
        async with session.get(feed["url"], headers=td.UA,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            raw = await r.text()
    except Exception:
        return None
    items = []
    for it in _parse_research_feed(raw, 12):
        entry = {"title": it["title"], "meta": td._rss_age(it["pub"])}
        if feed.get("author"):
            entry["author"] = feed["author"]
        if feed["readable"]:
            entry["kind"], entry["link"] = "web", it["link"]
        else:
            entry["kind"], entry["body"] = "blurb", _clean_html(it["desc"])
            if feed.get("link_base"):
                entry["link"] = feed["link_base"] + _slug(it["title"]) + feed.get("link_suffix", "")
        items.append(entry)
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


async def api_research(request):
    """Research view: saved files (drop into research/) + public feeds."""
    RESEARCH_DIR.mkdir(exist_ok=True)
    files = []
    for p in RESEARCH_DIR.iterdir():
        if p.is_file() and p.suffix.lower() in _RESEARCH_EXT and not p.name.startswith("."):
            st = p.stat()
            files.append({"kind": "file", "file": p.name, "title": p.stem,
                          "ext": p.suffix.lower().lstrip("."), "_m": st.st_mtime})
    files.sort(key=lambda x: x["_m"], reverse=True)
    for it in files:
        it["meta"] = datetime.fromtimestamp(it.pop("_m")).strftime("%b %d, %Y")

    sections = []
    if files:
        sections.append({"name": "Rosenberg Research", "items": files})
    session = request.app["session"]
    # Own session with raised header limits: some gov sites send oversized CSP headers
    # that trip aiohttp's default 8190-byte cap (same issue BMO's sitemap has).
    async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as econ:
        # Order: Rosenberg (above) → BMO → Adam Taggart → Fed → Data&Budget →
        # Treasury auctions → Academic → Global.
        econ_tasks = [_econ_group_section(econ, g) for g in ECON_GROUPS]
        tasks = [_bmo_insights_section()]
        tasks += [_research_feed(session, f) for f in RESEARCH_FEEDS]
        tasks += econ_tasks[:2]                      # FEDERAL RESERVE, U.S. DATA & BUDGET
        tasks.append(_treasury_auctions_section(econ))
        tasks += econ_tasks[2:]                      # ACADEMIC RESEARCH, GLOBAL
        feeds = await asyncio.gather(*tasks)
    for fs in feeds:
        if fs and fs["items"]:
            sections.append(fs)
    return web.json_response({"sections": sections})


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


async def api_research_read(request):
    """Extract one report's text for the in-app reader."""
    name = request.query.get("file", "")
    p = RESEARCH_DIR / name
    if (not name or "/" in name or "\\" in name or ".." in name
            or not p.is_file() or p.suffix.lower() not in _RESEARCH_EXT):
        return web.json_response({"title": name, "paragraphs": [], "error": "not found"}, status=404)
    try:
        if p.suffix.lower() == ".pdf":
            paras = _pdf_paragraphs(p)
        else:
            paras = [b.strip() for b in p.read_text(encoding="utf-8", errors="replace").split("\n\n")
                     if b.strip()]
    except Exception as e:
        return web.json_response({"title": p.stem, "paragraphs": [], "error": str(e)})
    return web.json_response({"title": p.stem, "paragraphs": paras})


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


async def index(request):
    html = (HERE / "static" / "index.html").read_text(encoding="utf-8")
    return web.Response(text=html.replace("{{GREETING_NAME}}", _greeting_name()),
                        content_type="text/html")


async def on_start(app):
    app["session"] = aiohttp.ClientSession()
    for fn in (dash.cnbc_loop, dash.binance_loop, dash.fred_loop, dash.cftc_loop):
        app["tasks"].append(asyncio.create_task(fn()))
    app["tasks"].append(asyncio.create_task(monitor_broadcast(app)))


async def on_cleanup(app):
    for ws in list(app.get("ws_clients", ())):
        await ws.close()
    for t in app["tasks"]:
        t.cancel()
    await app["session"].close()


def make_app():
    app = web.Application()
    app["tasks"] = []
    app["ws_clients"] = set()
    app.router.add_get("/", index)
    app.router.add_get("/ws", api_ws)
    app.router.add_get("/api/monitor", api_monitor)
    app.router.add_get("/api/sections", api_sections)
    app.router.add_post("/api/add", api_add)
    app.router.add_get("/api/chart", api_chart)
    app.router.add_get("/api/security", api_security)
    app.router.add_get("/api/financials", api_financials)
    app.router.add_get("/api/portfolio", api_portfolio)
    app.router.add_get("/api/news", api_news)
    app.router.add_get("/api/news_board", api_news_board)
    app.router.add_get("/api/research", api_research)
    app.router.add_get("/api/research/read", api_research_read)
    app.router.add_get("/api/article", api_article)
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
