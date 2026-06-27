#!/usr/bin/env python3
"""
A personal "Bloomberg-style" terminal built on Textual.

Home screen is the live market monitor (from dashboard.py). A command line at
the bottom drives functions, Bloomberg-style:

    AAPL            security detail (fundamentals + interactive chart + news)
    AAPL GP         full-screen price chart
    AAPL GP 6M      chart at a timeframe (1D 1W 1M 3M 6M 1Y 5Y 10Y ALL)
    AAPL GP 2024-01-01 2024-06-01   custom date range
    AAPL FA         company financials (SEC filings)
    AAPL N          news headlines
    BTC GP          crypto charts (Binance)
    MON             back to the live monitor   ·   HELP   ·   Q

In a chart: hover the mouse to read the value, click a timeframe to switch.

Run:  ~/markets-dashboard/.venv/bin/python ~/markets-dashboard/terminal.py
"""

import asyncio
import json
import os
import webbrowser
from datetime import datetime
from pathlib import Path

import aiohttp
from rich.console import Group
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.suggester import Suggester
from textual.widgets import Button, Footer, Header, Input, Static
import sys as _sys
from textual_image.widget import Image as _AutoTermImage, TGPImage as _TGPImage
from textual_image.widget import SixelImage as _SixelImage
from textual_image.widget import HalfcellImage as _HalfcellImage
from textual_image.widget import UnicodeImage as _UnicodeImage
# Pick the image-rendering protocol. WezTerm (Windows) needs the kitty graphics
# protocol; textual-image's auto-detection is unreliable over the Windows console
# and falls back to blocky blocks. Resolve from (1) a CLI arg — always reaches the
# program, unlike env vars through WezTerm — then (2) env, then (3) WezTerm detect.
_PROTO_MAP = {"tgp": _TGPImage, "sixel": _SixelImage, "auto": _AutoTermImage,
              "halfcell": _HalfcellImage, "unicode": _UnicodeImage}


def _pick_proto():
    for a in _sys.argv[1:]:
        al = a.lower().lstrip("-")
        if al.startswith("image="):
            return al.split("=", 1)[1]
        if al in _PROTO_MAP:
            return al
    p = os.environ.get("MKT_IMAGE_PROTOCOL", "").lower()
    if p:
        return p
    if (os.environ.get("WEZTERM_PANE") or os.environ.get("WEZTERM_EXECUTABLE")
            or os.environ.get("TERM_PROGRAM", "").lower() == "wezterm"):
        # WezTerm: sixel renders real images; its kitty (tgp) support in the
        # 20240203 stable build can't draw the Unicode placeholder glyph (blocks).
        return "sixel"
    return "auto"


TermImage = _PROTO_MAP.get(_pick_proto(), _AutoTermImage)

import chart_render
import dashboard as dash
import terminal_data as td

# Make TLS verification use certifi's CA bundle (the standalone Python ships an
# incomplete trust store — some gov hosts like treasurydirect.gov fail otherwise)
import ssl as _ssl
try:
    import certifi as _certifi
    _SSL_CTX = _ssl.create_default_context(cafile=_certifi.where())
except Exception:
    _SSL_CTX = None

AMBER = "orange1"
GREEN = "green3"
RED = "red3"
DIM = "grey50"
SRC = "sky_blue3"       # news source / publisher label
WHITE = "white"
AXIS = "grey37"
C_PRICE = "grey85"     # price line (soft white)
C_SMA50 = "yellow"
C_SMA100 = "green3"
C_SMA200 = "magenta"
C_RSI = "cyan"
C_REF = "grey35"       # RSI 30/70 reference lines

# Plot area fractions — MUST match chart_render.fig.subplots_adjust(left/right)
PLOT_LEFT, PLOT_RIGHT = 0.058, 0.992

# ---- printing: white-background export of the current view ------------------
# remap the dark-theme colours that are illegible on a white page
_PRINT_REMAP = {
    "#ffffff": "#000000",  # white text -> black
    "#dadada": "#333333",  # grey85
    "#cbcccd": "#333333",  # light grey / price
    "#808080": "#555555",  # grey50 -> darker
    "#00d700": "#0a8a0a",  # green -> darker green
    "#5fafd7": "#1565a0",  # sky blue -> darker
    "#ffaf00": "#a86b00",  # amber -> darker amber
}


def _light_terminal_theme():
    from rich.terminal_theme import TerminalTheme
    return TerminalTheme(
        (255, 255, 255), (0, 0, 0),
        [(0, 0, 0), (194, 54, 33), (37, 188, 36), (173, 173, 39),
         (73, 46, 225), (211, 56, 211), (51, 187, 200), (203, 204, 205)],
        [(129, 131, 131), (252, 57, 31), (49, 231, 34), (234, 236, 35),
         (88, 51, 255), (249, 53, 248), (20, 240, 240), (233, 235, 235)])


def _cell_px():
    """Pixel dimensions of one terminal cell (for sizing the chart image)."""
    try:
        from textual_image._terminal import get_cell_size
        cs = get_cell_size()
        return max(int(cs.width), 1), max(int(cs.height), 1)
    except Exception:
        return (9, 18)


def _timeframe_bar(active_tf, indicators=True, compare=False, index100=False):
    line = Text("  ")
    spans = []
    col = 2
    for tf in td.TF_ORDER:
        label = f" {tf} "
        line.append(label, style="bold black on orange1" if tf == active_tf
                    else "grey85 on grey23")
        line.append(" ")
        spans.append((col, col + len(label) - 1, tf))
        col += len(label) + 1
    clabel = " CUSTOM "                       # date-range box next to ALL
    line.append(clabel, style="bold black on orange1" if active_tf == "CUSTOM"
                else "bold black on cyan")
    spans.append((col, col + len(clabel) - 1, "CUSTOM"))
    col += len(clabel) + 1
    line.append("   ", style="")
    col += 3
    if compare:                                      # % return  <->  indexed to 100
        idx = f" {'INDEX 100' if index100 else '% RETURN'} "
        line.append(idx, style="bold black on green3")
        spans.append((col, col + len(idx) - 1, "__IDX100__"))
        col += len(idx) + 1
    else:
        ind = f" IND {'ON' if indicators else 'OFF'} "    # toggle SMA/RSI
        line.append(ind, style="bold black on green3" if indicators else "grey85 on grey23")
        spans.append((col, col + len(ind) - 1, "__IND__"))
        col += len(ind) + 1
    line.append(" ")
    col += 1
    cmpl = " +CMP "                                   # start / add to a comparison
    line.append(cmpl, style="bold black on cyan")
    spans.append((col, col + len(cmpl) - 1, "__CMP__"))
    if compare:
        line.append("   (toggle % return / indexed-to-100 · +CMP = add a ticker)", style=DIM)
    else:
        line.append("   (CUSTOM = dates · IND = SMA/RSI · +CMP = compare)", style=DIM)
    return line, spans


def _chart_header(symbol, tf, bars, mode, hover_idx, indicators=True):
    """Compact interactive header (timeframe bar + hover readout). The symbol,
    price, change and legend already live in the chart image itself."""
    closes = [b["c"] for b in bars]
    n = len(closes)
    full = (mode == "chart") and indicators

    if hover_idx is not None and 0 <= hover_idx < n:
        b = bars[hover_idx]
        i = hover_idx
        h = Text("  ◉ ", style=AMBER)
        h.append(f"{b['t']}   ", style="bold white")
        h.append(f"{b['c']:,.2f}", style="bold white")
        if "o" in b:
            h.append(f"   O {b['o']:,.2f}  H {b['h']:,.2f}  L {b['l']:,.2f}", style=DIM)
        if full:
            for series, lab, colr in ((td.sma(closes, 50), "50", C_SMA50),
                                      (td.sma(closes, 100), "100", C_SMA100),
                                      (td.sma(closes, 200), "200", C_SMA200)):
                if series[i] is not None:
                    h.append(f"   {lab}d {series[i]:,.1f}", style=colr)
            rsi = td.rsi(closes, 14)
            if rsi[i] is not None:
                h.append(f"   RSI {rsi[i]:,.0f}", style=C_RSI)
    else:
        h = Text("  hover the chart to read any point's value", style=DIM)

    bar, spans = _timeframe_bar(tf, indicators)
    return Group(bar, h), spans


# ===========================================================================
# Interactive chart widget (real image via matplotlib + terminal graphics)
# ===========================================================================


class ChartHeader(Static):
    """Text header with a clickable timeframe bar (3rd line)."""

    def __init__(self, **kw):
        super().__init__("", **kw)
        self._spans = []

    def set_content(self, renderable, spans):
        self._spans = spans
        self.update(renderable)

    def on_click(self, event: events.Click):
        if event.y == 0:                         # timeframe bar is the first header line
            for c0, c1, tf in self._spans:
                if c0 <= event.x <= c1:
                    if tf == "CUSTOM":
                        self.app.prompt_custom_range()
                    elif tf == "__IND__":
                        self.app.run_worker(self.app.toggle_indicators(), exclusive=True)
                    elif tf == "__IDX100__":
                        self.app.run_worker(self.app.toggle_index100(), exclusive=True)
                    elif tf == "__CMP__":
                        self.app.prompt_compare()
                    else:
                        self.app.run_worker(self.app.change_timeframe(tf), exclusive=True)
                    return


def _parse_mdy(s):
    """Parse a user date (MM-DD-YYYY and friends) -> 'YYYY-MM-DD' or None."""
    s = (s or "").strip().replace("/", "-").replace(".", "-")
    for fmt in ("%m-%d-%Y", "%m-%d-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


POPULAR_TICKERS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "AVGO", "AMD",
    "NFLX", "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "UNH", "JNJ", "LLY", "PFE",
    "XOM", "CVX", "WMT", "HD", "COST", "PG", "KO", "PEP", "MCD", "DIS", "INTC", "CRM",
    "ORCL", "ADBE", "QCOM", "TXN", "PYPL", "UBER", "BABA", "PLTR", "COIN", "MSTR",
    "SPY", "QQQ", "DIA", "IWM", "VOO", "VTI", "TLT", "IEF", "SHY", "GOVT", "BIL", "LQD",
    "HYG", "GLD", "SLV", "VNQ", "SCHH", "MORT", "XLF", "XLK", "XLE", "XLV", "SMH", "ARKK",
    "KWEB", "MAA", "AVB", "CPT", "ABR", "STWD", "LADR", "O", "SPG", "PLD", "AMT",
    "DOW", "SPX", "NASDAQ", "NDX", "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA",
]
SUGGEST_COMMANDS = ["MON", "HOME", "HELP", "NEWS", "PORT", "AUCTIONS", "CMP"]


class TickerSuggester(Suggester):
    """Inline ghost-text autocomplete for the first token of a command."""

    def __init__(self, app):
        super().__init__(use_cache=True, case_sensitive=False)
        self._app = app

    async def get_suggestion(self, value):
        if not value or " " in value:        # only the leading ticker token
            return None
        v = value.upper()
        for cand in self._app.suggest_pool:
            if cand.startswith(v) and cand != v:
                return cand
        return None


class ChartView(Vertical):
    def compose(self) -> ComposeResult:
        yield ChartHeader(id="chdr")
        with Horizontal(id="daterow"):
            yield Static("RANGE", id="rangelbl")
            yield Input(placeholder="MM-DD-YYYY", id="dfrom", max_length=10)
            yield Static("–", id="dsep")
            yield Input(placeholder="MM-DD-YYYY", id="dto", max_length=10)
            yield Button("Go", id="dgo")
        yield TermImage(id="chimg")
        yield Static(id="chtext")           # text/braille chart (image-less terminals)

    def on_mouse_move(self, event: events.MouseMove):
        # event bubbles up from the image; map screen x to the image region
        try:
            img = self.query_one("#chimg", TermImage)
        except Exception:
            return
        region = img.region
        if region.width and region.contains(event.screen_x, event.screen_y):
            self.app.chart_hover(event.screen_x - region.x, region.width)


# ===========================================================================
# Renderers for the text panels
# ===========================================================================


def render_fundamentals(fund):
    sym = fund.get("_symbol", "?")
    up = fund.get("_changetype", "") != "DOWN"
    col = GREEN if up else RED
    head = Text()
    head.append(f" {sym}  ", style=f"bold black on {AMBER}")
    head.append(f" {fund.get('Name','')}", style="bold white")
    head.append(f"    {fund.get('Last','')} ", style="bold white")
    head.append(f"{fund.get('Change','')} ({fund.get('Change %','')})", style=col)

    order = ["Exchange", "Market Cap", "P/E (ttm)", "P/E (fwd)", "EPS (ttm)",
             "EPS (fwd)", "Price/Sales", "Revenue (ttm)", "Gross Margin",
             "Net Margin", "ROE", "Debt/Equity", "EBITDA (ttm)", "Beta",
             "Dividend", "Div Yield", "Shares Out", "52wk High", "52wk Low",
             "Day High", "Day Low", "Volume"]
    grid = Table.grid(padding=(0, 2))
    for _ in range(2):
        grid.add_column(justify="left"); grid.add_column(justify="right")
    pairs = [(k, fund[k]) for k in order if k in fund]
    for i in range(0, len(pairs), 2):
        row = []
        for k, v in pairs[i:i + 2]:
            row += [Text(k, style=AMBER), Text(str(v), style="white")]
        if len(row) == 2:
            row += [Text(""), Text("")]
        grid.add_row(*row)
    return Group(head, Text(""), grid)


def _fmt_money(v):
    if v is None:
        return "--"
    a = abs(v)
    if a >= 1e9:
        return f"{v/1e9:,.1f}B"
    if a >= 1e6:
        return f"{v/1e6:,.1f}M"
    return f"{v:,.2f}"


def render_etf_holdings(sym, holdings):
    t = Table(box=None, padding=(0, 2), expand=False)
    for c, j in [("#", "right"), ("HOLDING", "left"), ("TICKER", "left"), ("WEIGHT", "right")]:
        t.add_column(c, justify=j)
    for i, h in enumerate(holdings, 1):
        t.add_row(Text(str(i), style=DIM), Text(h.get("name") or "?", style="white"),
                  Text(h.get("symbol") or "", style=AMBER),
                  Text(str(h.get("weight") or ""), style="bold white"))
    return Group(Text(f" {sym}  —  Top ETF Holdings", style=f"bold {AMBER}"), Text(""), t,
                 Text("\n  Source: StockAnalysis  ·  weights by portfolio allocation", style=DIM))


def render_institutional_holders(sym, data):
    hdr = Text(f" {sym}  —  Top Institutional Holders", style=f"bold {AMBER}")
    if data.get("inst_pct"):
        hdr.append(f"   ({data['inst_pct']} institutional)", style=DIM)
    t = Table(box=None, padding=(0, 2), expand=False)
    for c, j in [("#", "right"), ("HOLDER", "left"), ("SHARES", "right"),
                 ("VALUE ($K)", "right"), ("AS OF", "left")]:
        t.add_column(c, justify=j)
    for i, h in enumerate(data["holders"], 1):
        t.add_row(Text(str(i), style=DIM), Text(h.get("name") or "?", style="white"),
                  Text(str(h.get("shares") or ""), justify="right"),
                  Text(str(h.get("value") or ""), style="bold white"),
                  Text(str(h.get("date") or ""), style=DIM))
    return Group(hdr, Text(""), t, Text("  Source: Nasdaq (13F filings)", style=DIM))


def render_financials(sym, fin):
    if not fin:
        return Text(f"No SEC financials found for {sym}.", style=RED)
    statements = fin["statements"]
    pcol = fin.get("partial_year")
    pnq = fin.get("partial_nq")
    # shared year columns across all statements
    years = sorted({y for _name, m in statements for s in m.values() for y, _ in s})

    def col_label(y):
        if y == pcol:
            return f"{y}·{pnq}Q" if pnq else f"{y}*"
        return str(y)

    blocks = [Text(f" {sym}  —  Financials (SEC 10-K / 20-F)", style=f"bold {AMBER}"),
              Text("")]
    for name, metrics in statements:
        t = Table(title=name, title_style=f"bold {AMBER}", title_justify="left",
                  header_style=f"bold {AMBER}", expand=False, padding=(0, 1))
        t.add_column("", style="white", no_wrap=True)
        for y in years:
            t.add_column(col_label(y), justify="right",
                         header_style="bold cyan" if y == pcol else f"bold {AMBER}")
        for label, series in metrics.items():
            d = dict(series)
            emph = label == "Free Cash Flow"
            pct = label == "Payout Ratio"
            cells = [Text("--" if d.get(y) is None else
                          (f"{d[y]:.0f}%" if pct else _fmt_money(d[y])),
                          style="bold cyan" if emph else "white") for y in years]
            t.add_row(Text(label, style="bold cyan" if emph else AMBER), *cells)
        blocks += [t, Text("")]
    src = "  Source: SEC EDGAR XBRL · Free Cash Flow = Operating CF − CapEx"
    if pcol:
        src += (f" · {pcol}·{pnq}Q = current year, {pnq} quarter(s) filed "
                "(flows year-to-date; balance-sheet as of latest quarter)")
    blocks.append(Text(src, style=DIM))
    return Group(*blocks)


def _link_style(url, links, base="white", meta=None, headline="", source=""):
    """Return a clickable Rich style that opens the in-terminal reader.
    Registers `url` in `links` (and the headline/source in `meta`, kept in sync)
    and references it by index — avoids quoting URLs into the action string."""
    if not url or links is None:
        return base
    links.append(url)
    if meta is not None:
        meta.append((headline, source))
    return Style.parse(base) + Style(meta={"@click": f"app.read({len(links) - 1})"})


def render_news(items, header, links=None, meta=None):
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(justify="left", ratio=1)
    for it in items:
        line = Text()
        line.append("• ", style=AMBER)
        line.append(it["title"], style=_link_style(it.get("link"), links, meta=meta,
                    headline=it["title"], source=it.get("source", "")))
        if it.get("source"):
            line.append(f"   {it['source']}", style=SRC)
        if it.get("pub"):
            line.append(f"   {it['pub'][:16]}", style=DIM)
        t.add_row(line)
    return Group(Text(f" {header}", style=f"bold {AMBER}"), Text(""), t)


def _clip(s, n):
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def render_news_dashboard(sections, when="", links=None, meta=None):
    """Two-column daily market-news board: CNBC sections side by side."""
    from itertools import zip_longest
    blocks = []
    for sec in sections:
        body = [Text(f" ▌ {sec['heading']}", style=f"bold {AMBER}")]
        for it in sec["items"]:
            line = Text()
            line.append("  • ", style=AMBER)
            line.append(_clip(it["title"], 54), style=_link_style(it.get("link"), links,
                        meta=meta, headline=it["title"], source=it.get("source", "")))
            if it.get("source"):
                line.append(f"  {_clip(it['source'], 16)}", style=SRC)
            if it.get("age"):
                line.append(f"  {it['age']}", style=DIM)
            body.append(line)
        body.append(Text(""))
        blocks.append(Group(*body))
    left, right = blocks[0::2], blocks[1::2]   # alternate sections across columns
    grid = Table.grid(padding=(0, 3), expand=True)
    grid.add_column(ratio=1); grid.add_column(ratio=1)
    for a, b in zip_longest(left, right, fillvalue=Text("")):
        grid.add_row(a, b)
    head = Text(" MARKET NEWS", style=f"bold {AMBER}")
    if when:
        head.append(f"   {when}", style=DIM)
    head.append("   ·  CNBC + Google News  ·  auto-refresh 90s  ·  click a headline to read it here",
                style=DIM)
    return Group(head, Text(""), grid)


def render_article(art, headline, source, browser_links=None):
    """Reader view: title, source, body paragraphs, and a clickable nav bar."""
    nav = Text()
    nav.append(" ← BACK ", style=Style(color="black", bgcolor="orange1",
               meta={"@click": "app.reader_back"}))
    nav.append("   ")
    if browser_links is not None and art.get("url"):
        browser_links.append(art["url"])
        nav.append(" OPEN IN BROWSER ↗ ", style=Style(color="white", bgcolor="grey23",
                   meta={"@click": f"app.open_url({len(browser_links) - 1})"}))
    nav.append("    (Esc = monitor)", style=DIM)

    title = art.get("title") or headline
    out = [nav, Text(""), Text(title, style=f"bold {AMBER}")]
    src = source or ""
    if src:
        out.append(Text(src, style=SRC))
    out.append(Text(""))
    if art.get("paywalled") or not art.get("paragraphs"):
        out.append(Text("  This article appears to be paywalled or could not be "
                        "extracted.", style=RED))
        out.append(Text("  Use OPEN IN BROWSER ↗ above to read the full piece.",
                        style=DIM))
    else:
        for p in art["paragraphs"]:
            out.append(Text(p, style="grey85"))
            out.append(Text(""))
    return Group(*out)


def render_auctions(auctions):
    t = Table(box=None, padding=(0, 2), expand=False, header_style=f"bold {AMBER}")
    t.add_column("DATE", style="white", no_wrap=True)
    t.add_column("TYPE")
    t.add_column("TERM", no_wrap=True)
    t.add_column("HIGH YIELD", justify="right")
    t.add_column("BID/COVER", justify="right")
    for a in auctions:
        try:
            y = f"{float(a['yield']):.3f}%"
        except (ValueError, TypeError):
            y = a.get("yield") or "—"
        try:
            bc_v = float(a["btc"])
            bc = Text(f"{bc_v:.2f}x", style=GREEN if bc_v >= 2.6 else
                      (RED if bc_v < 2.2 else "white"))
        except (ValueError, TypeError):
            bc = Text("—", style=DIM)
        t.add_row(a["date"], Text(a["type"], style=AMBER), a["term"],
                  Text(y, style="bold white"), bc)
    return Group(Text(" US TREASURY AUCTIONS  (most recent)", style=f"bold {AMBER}"),
                 Text(""), t,
                 Text("\n  Source: TreasuryDirect · high yield = clearing rate · "
                      "bid/cover = demand (higher = stronger; <2.2 = weak)", style=DIM))


PORT_FILE = Path(__file__).resolve().parent / "portfolio.json"
TX_FILE = Path(__file__).resolve().parent / "transactions.json"


def load_portfolio():
    try:
        return json.loads(PORT_FILE.read_text())
    except Exception:
        return {"name": "Portfolio", "positions": []}


def load_transactions():
    try:
        return json.loads(TX_FILE.read_text()).get("transactions", [])
    except Exception:
        return []


async def _price_on(session, ticker, date):
    """Closing price on/just after a date, to derive shares from a $ amount."""
    from datetime import timedelta
    end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=10)).strftime("%Y-%m-%d")
    try:
        bars = await td.fetch_history(session, ticker, "1Y", custom=(date, end))
        return bars[0]["c"] if bars else None
    except Exception:
        return None


async def build_positions(session):
    """Aggregate transactions into per-ticker positions (shares + cost basis)."""
    agg = {}
    for tx in load_transactions():
        try:
            tk = tx["ticker"].upper()
        except (KeyError, AttributeError):
            continue
        date = tx.get("date")
        amount = tx.get("amount")
        shares = tx.get("shares")
        price = tx.get("price")
        if shares is None:
            if price is None and date:
                price = await _price_on(session, tk, date)
            if price and amount is not None:
                shares = amount / price
        if shares is None:
            continue
        cost = amount if amount is not None else shares * (price or 0)
        sign = -1 if str(tx.get("action", "buy")).lower().startswith("sell") else 1
        a = agg.setdefault(tk, {"shares": 0.0, "cost": 0.0, "first": date, "n": 0})
        a["shares"] += sign * shares
        a["cost"] += sign * cost
        a["n"] += 1
        if date and (not a["first"] or date < a["first"]):
            a["first"] = date
    positions = [{"ticker": tk, **v} for tk, v in agg.items() if abs(v["shares"]) > 1e-6]
    dash.track([p["ticker"] for p in positions])
    return positions


def _signed(v, money=True, pct=False):
    sign = "+" if v >= 0 else "−"
    body = f"${abs(v):,.0f}" if money else f"{abs(v):,.2f}{'%' if pct else ''}"
    return Text(f"{sign}{body}", style=GREEN if v >= 0 else RED)


def _account_section(acct, positions, rate, indent=""):
    """Render one account's holdings (equities, treasuries, cash, futures).
    Returns (blocks, totals_dict). `positions` = equity/ETF list with shares+cost."""
    blocks = []
    sleeves = acct.get("cash", acct.get("positions", []))

    # ---- equity / ETF positions (live P&L) ----
    pos_val = pos_cost = 0.0
    if positions:
        pt = Table(box=None, padding=(0, 2), expand=False)
        for c, j in [("TICKER", "left"), ("SHARES", "right"), ("COST BASIS", "right"),
                     ("LAST", "right"), ("MKT VALUE", "right"), ("UNREAL P&L", "right"),
                     ("RETURN", "right"), ("SINCE", "left")]:
            pt.add_column(c, justify=j, style="white" if c == "TICKER" else None)
        for p in sorted(positions, key=lambda x: -(x["shares"] * _live(x["ticker"]))):
            tk = p["ticker"]
            last = _live(tk)
            cost = p["cost"]
            avg = cost / p["shares"] if p["shares"] else 0
            mv = p["shares"] * last
            pnl = mv - cost
            ret = (pnl / cost * 100) if cost else 0
            pos_val += mv
            pos_cost += cost
            pt.add_row(Text(tk, style=AMBER), Text(f"{p['shares']:,.2f}"),
                       Text(f"${avg:,.2f}"), Text(f"${last:,.2f}" if last else "—"),
                       Text(f"${mv:,.0f}", style="bold white"),
                       _signed(pnl), _signed(ret, money=False, pct=True),
                       Text(p.get("first") or "", style=DIM))
        tot_pnl = pos_val - pos_cost
        tot_ret = (tot_pnl / pos_cost * 100) if pos_cost else 0
        pt.add_row(Text("POSITIONS", style="bold white"), Text(""), Text(f"${pos_cost:,.0f}", style=DIM),
                   Text(""), Text(f"${pos_val:,.0f}", style="bold white"),
                   _signed(tot_pnl), _signed(tot_ret, money=False, pct=True), Text(""))
        blocks += [Text(f"{indent} EQUITY / ETF POSITIONS", style=f"bold {AMBER}"), pt, Text("")]

    # ---- US Treasuries, marked LIVE to current yields ----
    tre = acct.get("treasuries", [])
    tre_mv = tre_daily = tre_coupon = 0.0
    if tre:
        tt = Table(box=None, padding=(0, 2), expand=False)
        for c, j in [("NOTE", "left"), ("YTM", "right"), ("PRICE", "right"),
                     ("MKT VALUE", "right"), ("Δ TODAY", "right")]:
            tt.add_column(c, justify=j)
        now = datetime.now()
        for nt in tre:
            face = nt.get("face", 0)
            mat = datetime.strptime(nt["maturity"], "%Y-%m-%d")
            years = max((mat - now).days / 365.25, 0)
            y, dchg = _yield(_tenor_sym(years))
            tre_coupon += face * nt.get("coupon", 0) / 100
            if y is None:
                px, mv, dval, ytxt = 100.0, face, 0.0, "—"
            else:
                px = _bond_price(nt.get("coupon", 0), years, y)
                mv = px / 100 * face
                dval = (px - _bond_price(nt.get("coupon", 0), years, y - dchg)) / 100 * face
                ytxt = f"{y:.2f}%"
            tre_mv += mv
            tre_daily += dval
            tt.add_row(Text(nt.get("name", "?"), style=AMBER), Text(ytxt),
                       Text(f"{px:.2f}"), Text(f"${mv:,.0f}", style="bold white"), _signed(dval))
        tt.add_row(Text("TREASURIES", style="bold white"), Text(""), Text(""),
                   Text(f"${tre_mv:,.0f}", style="bold white"), _signed(tre_daily))
        blocks += [Text(f"{indent} US TREASURIES  (live, marked to yields)", style=f"bold {AMBER}"),
                   tt, Text("")]

    # ---- cash / money-market sleeves ----
    income = tre_coupon
    if sleeves:
        st = Table(box=None, padding=(0, 2), expand=False)
        for c, j in [("SLEEVE", "left"), ("VALUE", "right"), ("YIELD", "right"),
                     ("EST. ANNUAL", "right")]:
            st.add_column(c, justify=j)
        for p in sleeves:
            amt = p.get("amount", 0)
            prate = p.get("rate", rate)        # fixed coupon if given, else live 3M
            if p.get("yields") and prate is not None:
                ann = amt * prate / 100
                income += ann
                yc, ac = Text(f"{prate:.2f}%", style=GREEN), Text(f"${ann:,.0f}", style=GREEN)
            else:
                yc, ac = Text("--", style=DIM), Text("--", style=DIM)
            st.add_row(Text(p.get("name", "?"), style=AMBER),
                       Text(f"${amt:,.0f}", style="bold white"), yc, ac)
        blocks += [Text(f"{indent} CASH / MONEY MARKET", style=f"bold {AMBER}"), st, Text("")]

    # ---- futures account, marked LIVE to the contract price ----
    fut = acct.get("futures")
    fut_eq = fut_daily = 0.0
    if fut:
        funded = fut.get("net_funded", 0)
        base_eq = fut.get("equity_baseline", fut.get("equity", 0))
        contract = fut.get("contract")
        base_px = fut.get("baseline_price")
        qty = fut.get("qty", 0)
        mult = fut.get("mult", 1000)
        sign = 1 if fut.get("side", "long") == "long" else -1
        live = _live(contract) if contract else 0
        move = (live - base_px) * mult * qty * sign if (live and base_px) else 0
        fut_eq = base_eq + move
        fut_daily = _live_change(contract) * mult * qty * sign if contract else 0.0
        pnl = fut_eq - funded
        ret = (pnl / funded * 100) if funded else 0
        ft = Table(box=None, padding=(0, 2), expand=False)
        for c, j in [("POSITION", "left"), ("LIVE PRICE", "right"), ("Δ vs ANCHOR", "right"),
                     ("EQUITY NOW", "right"), ("P&L SINCE START", "right"), ("RETURN", "right")]:
            ft.add_column(c, justify=j)
        desc = f"{qty} {fut.get('side','long').upper()}  {fut.get('name', contract)}"
        ft.add_row(Text(desc, style=AMBER),
                   Text(f"{live:,.3f}" if live else "—", style="bold white"),
                   _signed(move), Text(f"${fut_eq:,.0f}", style="bold white"),
                   _signed(pnl), _signed(ret, money=False, pct=True))
        blocks += [Text(f"{indent} FUTURES  (live)", style=f"bold {AMBER}"), ft, Text("")]

    totals = {"pos_val": pos_val, "pos_cost": pos_cost, "tre_mv": tre_mv,
              "tre_daily": tre_daily, "income": income,
              "cash_total": sum(p.get("amount", 0) for p in sleeves),
              "fut_eq": fut_eq, "fut_daily": fut_daily,
              "fut_funded": (fut.get("net_funded", 0) if fut else 0), "has_fut": bool(fut)}
    return blocks, totals


def _acct_positions(acct):
    """Equity/ETF holdings declared inline on an account -> position dicts."""
    return [{"ticker": h["ticker"], "shares": h.get("shares", 0),
             "cost": h.get("cost", 0), "first": h.get("since")}
            for h in acct.get("holdings", [])]


def _portfolio_footer(pf, totals_list, subtotals=None):
    """Grand NET LIQUID / income / since-inception / TODAY across all accounts.
    Returns (today_line, footer_blocks)."""
    agg = {k: sum(t[k] for t in totals_list) for k in
           ("pos_val", "tre_mv", "tre_daily", "income", "cash_total",
            "fut_eq", "fut_daily", "fut_funded")}
    has_fut = any(t["has_fut"] for t in totals_list)
    grand = agg["cash_total"] + agg["pos_val"] + agg["fut_eq"] + agg["tre_mv"]
    income = agg["income"]

    gt = Text()
    gt.append("  NET LIQUID   ", style="bold white")
    gt.append(f"${grand:,.2f}", style="bold white")
    if income:
        gt.append("      Treasury/cash income  ", style=DIM)
        gt.append(f"${income:,.0f}/yr (${income/365:,.0f}/day)", style=GREEN)
    if has_fut:
        pnl = agg["fut_eq"] - agg["fut_funded"]
        gt.append("      futures P&L  ", style=DIM)
        gt.append(f"{'+' if pnl>=0 else '−'}${abs(pnl):,.0f}", style=GREEN if pnl >= 0 else RED)
    extra = [gt]

    if subtotals:
        sl = Text("  ACCOUNTS   ", style="bold white")
        for i, (nm, v) in enumerate(subtotals):
            if i:
                sl.append("     ", style=DIM)
            sl.append(f"{nm} ", style=DIM)
            sl.append(f"${v:,.0f}", style="white")
        extra.append(sl)

    dep = pf.get("deposited")
    if dep:
        chg = grand - dep
        r = chg / dep * 100
        since = Text()
        since.append("  SINCE INCEPTION   ", style="bold white")
        since.append(f"deposited ${dep:,.0f}  →  net liquid ${grand:,.0f}    ", style=DIM)
        since.append(f"{'+' if chg>=0 else '−'}${abs(chg):,.0f} ({r:+.1f}%)",
                     style=GREEN if chg >= 0 else RED)
        extra.append(since)
    extra.append(Text("\n  Treasuries marked live to current yields · cash at live 3M "
                      "rate (or stated).  Edit holdings in portfolio.json", style=DIM))

    day_income = income / 365 if income else 0.0
    day_total = agg["fut_daily"] + agg["tre_daily"] + day_income
    today = Text("  TODAY  ", style="bold white")
    today.append_text(_signed(day_total))
    today.append("    futures ", style=DIM); today.append_text(_signed(agg["fut_daily"]))
    today.append("    treasuries ", style=DIM); today.append_text(_signed(agg["tre_daily"]))
    today.append("    income ", style=DIM); today.append_text(_signed(day_income))
    return today, extra


def render_portfolio(positions=None):
    pf = load_portfolio()
    q = dash.STATE.get("US3M")
    rate = q.price if q and q.price else None

    head = Text()
    head.append(f"  {pf.get('name', 'Portfolio')}  ", style=f"bold black on {AMBER}")
    head.append("  PORTFOLIO", style="bold white")
    blocks = [head, Text("")]

    accounts = pf.get("accounts")
    if accounts:
        totals_list, subtotals = [], []
        for acct in accounts:
            sec, T = _account_section(acct, _acct_positions(acct), rate, indent=" ")
            nl = T["cash_total"] + T["pos_val"] + T["fut_eq"] + T["tre_mv"]
            label = Text()
            label.append(f"  ▌ {acct.get('name', 'Account')}   ", style="bold white")
            label.append(f"${nl:,.0f}", style=AMBER)
            blocks += [label, Text("")] + sec
            totals_list.append(T)
            subtotals.append((acct.get("name", "Account"), nl))
        today, footer = _portfolio_footer(pf, totals_list, subtotals)
    else:
        # legacy single-account file (equities come from transactions.json)
        sec, T = _account_section(pf, positions, rate)
        blocks += sec
        today, footer = _portfolio_footer(pf, [T])

    blocks += footer
    blocks.insert(1, today)
    return Group(*blocks)


def _app_title():
    """App/title-bar name; override per-install via an appname.txt file."""
    try:
        t = (Path(__file__).resolve().parent / "appname.txt").read_text().strip()
        if t:
            return t
    except Exception:
        pass
    return "KATZNELSON COMPANY TERMINAL"


def _live(ticker):
    q = dash.STATE.get(ticker.upper())
    return q.price if q and q.price else 0.0


def _live_change(ticker):
    q = dash.STATE.get(ticker.upper())
    return q.change if q and q.change is not None else 0.0


def _yield(sym):
    q = dash.STATE.get(sym)
    if q and q.price:
        return q.price, (q.change or 0.0)
    return None, 0.0


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
    c = coupon / freq
    y = ytm / 100 / freq
    if y == 0:
        return c * n + 100
    return sum(c / (1 + y) ** t for t in range(1, n + 1)) + 100 / (1 + y) ** n


HELP = """\
 COMMANDS  (type below, then Enter)

   AAPL                     security detail — fundamentals, chart, news
   AAPL 6M                  security detail at a timeframe
   AAPL GP                  full chart: white price + SMA 50/100/200 + RSI
   CMP AAPL MSFT NVDA       compare tickers (% return) · also: AAPL VS MSFT · +CMP button
   AAPL GP 3M               chart at a timeframe
   AAPL GP 2024-01-01 2024-06-01    custom date range
   AAPL FA                  financials: income, balance sheet & cash flow (full history)
   AAPL FA 10               financials limited to the last 10 years
   AAPL N                   news headlines
   BTC GP                   crypto works too (BTC ETH SOL XRP DOGE ADA ...)
   DOW GP · SPX GP · NASDAQ GP   indices (Dow/S&P via ETF, Nasdaq at real level)
   FEDFUNDS GP · US10Y GP · US2Y GP · 2S10S GP   rates & yields (FRED, decades back)
   DELINQ-CRE GP · DELINQ-CC GP · CHARGEOFF-CC GP   loan delinquency & charge-off rates
   CPI GP · CORECPI GP · UNRATE GP · PAYROLLS GP · WAGES GP   BLS economic data
   AUCTIONS                 recent US Treasury auction results (yield, bid/cover)

   TIMEFRAMES   1D 1W 1M 3M 6M 1Y 5Y 10Y ALL   (click the bar, or type as above)
   IN A CHART   hover the mouse to read the value at that point

   ADD NVDA                 add a ticker — then pick which section it goes in
   ADD NVDA STOCKS          add straight into a named section (skip the prompt)
   DEL NVDA                 remove a ticker from the monitor

   PORT                     your portfolio: capital, allocation, live income
   MON / HOME               live market monitor      Esc   back to monitor
   NEWS                     daily market news board — click a headline to read it in-terminal
   (also: WIRE / HEADLINES)
   POPOUT                   open charts live in a browser tab (if your terminal can't show images)
                            type POPOUT again (or POPOUT OFF) to go back to inline   ·   Q  quit
"""


# ===========================================================================
# App
# ===========================================================================


class MarketTerminal(App):
    CSS = """
    Screen { background: #0a0a0a; }
    /* clickable tickers/headlines: keep their colour, drop the link underline
       so the monitor reads as uniform amber. hover stays bold (no underline). */
    #top, #bottom, #readerbody {
        link-style: not underline;
        link-style-hover: bold not underline;
    }
    #top { padding: 0 1; height: auto; }
    #chart { height: 1fr; width: 1fr; }
    #chdr { height: 2; padding: 0 1; }
    #rangelbl { width: 7; height: 1; color: orange; content-align: left middle; }
    #dfrom, #dto { width: 14; height: 1; border: none; background: #1a1a1a; color: white; }
    #dsep { width: 3; height: 1; content-align: center middle; color: grey; }
    #dgo { width: 6; height: 1; min-width: 4; background: #1d4d2a; color: white; border: none; }
    #chimg { height: 1fr; width: 1fr; padding: 0 1; align: center middle; }
    #daterow { display: none; height: 1; padding: 0 1; }
    #bottom { padding: 0 1; height: auto; }
    #reader { height: 1fr; padding: 0 2; background: #0a0a0a; display: none; }
    #readerbody { height: auto; width: 1fr; }
    #chtext { height: 1fr; width: 1fr; padding: 0 1; display: none; }
    #cmdbar { height: 3; }
    #addbtn { width: 11; height: 3; min-width: 9; background: #1d1d1d;
              color: orange; border: tall #444; }
    #addbtn:hover { background: #2a2a2a; }
    #printbtn { width: 11; height: 3; min-width: 9; background: #1d1d1d;
                color: orange; border: tall #444; }
    #printbtn:hover { background: #2a2a2a; }
    #cmd { width: 1fr; border: tall #333; background: #111; color: white; }
    """
    BINDINGS = [("escape", "go_monitor", "Monitor"), ("ctrl+c", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.mode = "monitor"
        self.session = None
        self.cur_symbol = ""
        self.cur_display = ""
        self.tf = "1Y"
        self.cur_bars = []
        self.cur_mode = "des"
        self.cur_tf_label = "1Y"
        self.cur_hover = None
        self.show_indicators = False   # charts open clean; IND button adds SMA/RSI
        self.cmp_symbols = []
        self._cmp_items = []
        self._cmp_levels = []          # compare symbols drawn on the right (rate) axis
        self.cmp_index100 = False      # compare view: % return (False) vs indexed-to-100
        self._pending_add = None       # ticker awaiting a section choice
        self._pending_sections = []
        # pop-out mode: open charts as crisp PNGs in the browser instead of inline
        # (for terminals/GPUs that can't render images). Enable via env var or a
        # ".popout" file next to this script.
        self._chart_popout = bool(os.environ.get("MKT_CHART_POPOUT")) or os.path.exists(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".popout"))
        self._popout_opened = False
        # text/braille charts for terminals that can't show images. Windows
        # Terminal CAN (sets WT_SESSION) -> use crisp image charts there; other
        # Windows terminals -> text fallback. Override with MKT_CHART_TEXT=0/1.
        _tc = os.environ.get("MKT_CHART_TEXT", "")
        if _tc in ("0", "1"):
            self._chart_text = (_tc == "1")
        elif os.environ.get("WT_SESSION"):          # Windows Terminal -> images work
            self._chart_text = False
        else:
            self._chart_text = _sys.platform.startswith("win")
        # draw the hover cross + date/value on the chart. Repainting the sixel
        # image blinks slightly in Windows Terminal; toggle off with HOVER OFF
        # (then the read-out shows flicker-free in the header instead).
        self._hover_redraw = os.environ.get("MKT_HOVER", "1") != "0"
        self.cur_custom = None
        self.positions = []
        self._wire_on = False          # True only while the WIRE news board is showing
        self._link_urls = []           # article URLs for clickable headlines (by index)
        self._news_meta = []           # (headline, source) parallel to _link_urls
        self._reader_back = None       # coroutine fn: how to return from the reader
        self._browser_links = []       # URLs for the reader's OPEN IN BROWSER link
        self.suggester = TickerSuggester(self)
        self._alias_tickers = sorted(set(td.FRED_CHART) | set(td.INDEX_ALIAS))
        self._ticker_universe = []
        self.suggest_pool = []
        self._rebuild_pool()
        self.cur_base_img = None
        self.cur_geom = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="cmdbar"):
            yield Button("＋ Add", id="addbtn")
            yield Input(placeholder="Command  (e.g. AAPL · AAPL GP 6M · AAPL FA · ADD NVDA · type a letter for suggestions)",
                        id="cmd", suggester=self.suggester)
            yield Button("🖨 Print", id="printbtn")
        yield Static(id="top")
        yield ChartView(id="chart")
        yield Static(id="bottom")
        with VerticalScroll(id="reader"):
            yield Static(id="readerbody")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "addbtn":
            inp = self.query_one("#cmd", Input)
            inp.value = "ADD "
            inp.focus()
        elif event.button.id == "dgo":
            self.run_worker(self._apply_date_inputs(), exclusive=True)
        elif event.button.id == "printbtn":
            self.run_worker(self._print_screen(), exclusive=False)

    # ---- print the current screen (white background) ----------------------

    def _render_html(self, renderable):
        """Render a Rich renderable to a white-background HTML fragment."""
        import io
        import re
        from rich.console import Console
        con = Console(record=True, file=io.StringIO(), width=240)
        con.print(renderable)
        html = con.export_html(
            theme=_light_terminal_theme(), inline_styles=True,
            code_format="<pre style=\"font-family:Menlo,Consolas,monospace;"
                        "font-size:11px;line-height:1.18;white-space:pre;\">{code}</pre>")
        # drop all cell backgrounds so everything sits on the white page,
        # then darken the foreground colours that are illegible on white.
        html = re.sub(r"background-color: #[0-9a-f]{6};?\s*", "", html)
        for a, b in _PRINT_REMAP.items():
            html = html.replace(a, b)
        return html

    async def _light_chart_png(self):
        """Re-render the current chart on a white background, return PNG bytes."""
        if not self.cur_bars or self.cur_mode == "compare":
            return None
        import io
        w_px, h_px = self._chart_px(self.cur_mode)
        img, _ = await asyncio.to_thread(
            chart_render.render_chart_png, self.cur_display, self.cur_tf_label,
            self.cur_bars, self.cur_mode, w_px, h_px, self.show_indicators, True)
        buf = io.BytesIO(); img.save(buf, format="PNG")
        return buf.getvalue()

    async def _print_screen(self):
        import base64
        import tempfile
        parts = []
        # chart pane (if a chart is on screen) -> light-rendered image
        try:
            if self.chart().display and self.cur_mode in ("chart", "des", "compare"):
                png = await self._light_chart_png()
                if png:
                    b64 = base64.b64encode(png).decode()
                    parts.append(f'<img src="data:image/png;base64,{b64}" '
                                 f'style="max-width:100%;height:auto;">')
        except Exception:
            pass
        # visible text panes -> white-bg HTML
        for wid in ("top", "bottom", "readerbody"):
            try:
                w = self.query_one(f"#{wid}", Static)
            except Exception:
                continue
            if not w.display:
                continue
            rend = getattr(w, "_Static__content", None)
            if rend is None or rend == "":
                continue
            try:
                parts.append(self._render_html(rend))
            except Exception:
                pass
        if not parts:
            self.notify("Nothing to print on this view", severity="warning"); return
        title = _app_title()
        when = datetime.now().strftime("%a %b %d %Y  %H:%M")
        doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
@page {{ margin: 12mm; }}
body {{ background:#fff; color:#000; font-family:Menlo,Consolas,monospace; margin:18px; }}
h1 {{ font-size:15px; margin:0 0 2px 0; letter-spacing:1px; }}
.sub {{ color:#666; font-size:11px; margin-bottom:12px; }}
pre {{ margin:0 0 10px 0; }}
img {{ display:block; margin:6px 0 12px 0; border:1px solid #ddd; }}
@media print {{ .noprint {{ display:none; }} }}
</style></head><body>
<h1>{title}</h1>
<div class="sub">{when} &middot; printed from the terminal</div>
<div class="noprint" style="margin-bottom:12px;">
  <button onclick="window.print()" style="font-size:13px;padding:6px 16px;cursor:pointer;">🖨 Print</button>
</div>
{''.join(parts)}
</body></html>"""
        path = os.path.join(tempfile.gettempdir(), "kessler_print.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
        webbrowser.open("file://" + path)
        self.notify("Opened a printable (white) view in your browser — press "
                    "Cmd/Ctrl-P to print")

    async def _apply_date_inputs(self):
        fa = _parse_mdy(self.query_one("#dfrom", Input).value)
        ta = _parse_mdy(self.query_one("#dto", Input).value)
        if not fa or not ta:
            self.notify("Enter both dates as MM-DD-YYYY", severity="error"); return
        if fa > ta:
            fa, ta = ta, fa
        if self.cur_mode == "compare":
            if not self.cmp_symbols:
                self.notify("Start a comparison first (e.g. CMP AAPL MSFT)"); return
            await self.show_compare(self.cmp_symbols, custom=(fa, ta))
        elif self.cur_symbol:
            await self._load_chart(self.cur_symbol, self.cur_mode or "chart", custom=(fa, ta))
        else:
            self.notify("Open a chart first (type a ticker)")

    async def on_mount(self):
        self.title = _app_title()
        conn = aiohttp.TCPConnector(ssl=_SSL_CTX) if _SSL_CTX else None
        self.session = aiohttp.ClientSession(connector=conn)
        for fn in (dash.cnbc_loop, dash.binance_loop, dash.fred_loop, dash.cftc_loop):
            asyncio.create_task(fn())
        self.set_interval(0.4, self.tick)
        self.set_interval(90, self._wire_tick)     # refresh WIRE news board if open
        self.query_one("#cmd", Input).focus()
        self._show_only("top")
        self.tick()
        asyncio.create_task(self._load_ticker_universe())

    async def _load_ticker_universe(self):
        try:
            cmap = await td._load_cik_map(self.session)
            self._ticker_universe = sorted(cmap.keys())
            self._rebuild_pool()
        except Exception:
            pass

    def _rebuild_pool(self):
        seen, pool = set(), []
        for c in (SUGGEST_COMMANDS + self._alias_tickers + POPULAR_TICKERS
                  + dash.all_added() + self._ticker_universe):
            u = c.upper()
            if u not in seen:
                seen.add(u); pool.append(u)
        self.suggest_pool = pool

    async def on_unmount(self):
        if self.session:
            await self.session.close()

    # ---- helpers -----------------------------------------------------------

    def top(self):
        return self.query_one("#top", Static)

    def chart(self):
        return self.query_one("#chart", ChartView)

    def bottom(self):
        return self.query_one("#bottom", Static)

    def _show_only(self, *ids):
        self._wire_on = False          # any view switch stops the news auto-refresh
        for wid in ("top", "chart", "bottom", "reader"):
            self.query_one(f"#{wid}").display = wid in ids

    def tick(self):
        if self.mode == "monitor":
            self.top().update(dash.render())
        elif self.mode == "port":
            self.top().update(render_portfolio(self.positions))

    async def show_portfolio(self):
        self.mode = "port"
        self._show_only("top")
        self.top().update(Text("  building portfolio …", style=DIM))
        self.positions = await build_positions(self.session)
        # multi-account portfolios declare holdings inline — register them for
        # live CNBC polling so their market value / gain updates in real time.
        pf = load_portfolio()
        held = [h["ticker"] for a in pf.get("accounts", []) for h in a.get("holdings", [])]
        if held:
            dash.track(held)
        self.top().update(render_portfolio(self.positions))

    def action_go_monitor(self):
        self._pending_add = None       # cancel any pending "add to section" prompt
        self.mode = "monitor"
        self._show_only("top")
        self.tick()

    def action_open_url(self, idx):
        """Open a clicked headline in the default browser (fired by @click meta)."""
        try:
            url = self._link_urls[int(idx)]
        except (IndexError, ValueError, TypeError):
            return
        if url:
            webbrowser.open(url)

    # ---- command handling --------------------------------------------------

    async def action_open_ticker(self, cmd):
        """Run a command from a clicked monitor row (fired by @click meta)."""
        if cmd:
            await self._run_command(str(cmd))

    async def on_input_submitted(self, event: Input.Submitted):
        if event.input.id in ("dfrom", "dto"):   # date boxes -> apply range, keep values
            await self._apply_date_inputs()
            return
        raw = event.value.strip()
        event.input.value = ""
        if raw:
            await self._run_command(raw)

    async def _run_command(self, raw):
        parts = raw.upper().split()
        cmd = parts[0]

        if self._pending_add is not None:               # awaiting a section choice
            if cmd in ("ESC", "CANCEL", "X", "Q"):
                self._pending_add = None; self.action_go_monitor(); return
            sec = self._match_section(raw.strip())
            if sec:
                self._file_ticker(self._pending_add, sec)
            else:
                self.notify("Type the section number or name (or X to cancel)")
            return

        if cmd in ("Q", "EXIT", "QUIT"):
            await self.action_quit(); return
        if cmd in ("MON", "HOME"):
            self.action_go_monitor(); return
        if cmd == "HELP":
            self.mode = "help"; self._show_only("top")
            self.top().update(Text(HELP, style="white")); return
        if cmd in ("NEWS", "WIRE", "TOPNEWS", "NEWSWIRE", "HEADLINES"):
            await self.show_news_dashboard(); return
        if cmd in ("AUCTIONS", "AUCTION", "AUCT", "TD"):
            await self.show_auctions(); return
        if cmd in ("POPOUT", "POP", "BROWSER"):
            on = (parts[1] not in ("OFF", "0", "NO")) if len(parts) > 1 else not self._chart_popout
            self._set_popout(on); return
        if cmd in ("HOVER", "CROSS"):
            self._hover_redraw = (parts[1] not in ("OFF", "0", "NO")) if len(parts) > 1 \
                else not self._hover_redraw
            self.notify("Hover cross ON (blinks a little in Windows Terminal)"
                        if self._hover_redraw else
                        "Hover cross OFF — date/value shows in the top bar, no blink")
            return
        if cmd in ("PORT", "PORTFOLIO", "HOLDINGS", "PNL"):
            await self.show_portfolio(); return
        if cmd in ("CMP", "COMPARE") or "VS" in parts:
            toks = [p for p in parts if p not in ("CMP", "COMPARE", "VS")]
            dates = [t for t in toks if t.count("-") == 2]
            custom = (dates[0], dates[1]) if len(dates) >= 2 else None
            tfs = [t for t in toks if t in td.TF_ORDER]
            self.tf = tfs[0] if tfs else self.tf
            syms = [t for t in toks if t not in dates and t not in tfs]
            if len(syms) >= 2:
                await self.show_compare(syms, custom)
            else:
                self.notify("Compare needs 2+ tickers — e.g.  CMP AAPL MSFT")
            return
        if cmd in ("ADD", "+", "WATCH"):
            await self._add_ticker(parts[1:]); return
        if cmd in ("DEL", "RM", "REMOVE", "-", "UNWATCH"):
            for tk in parts[1:]:
                dash.remove_ticker(tk)
            self._rebuild_pool(); self.action_go_monitor(); return

        ticker = cmd
        rest = parts[1:]
        func = "DES"
        tf = self.tf
        custom = None
        if rest:
            if rest[0] in ("GP", "CHART", "G", "FA", "FIN", "N", "NEWS", "DES"):
                func = rest[0]; rest = rest[1:]
            if len(rest) >= 2 and "-" in rest[0] and "-" in rest[1]:
                custom = (rest[0], rest[1])
            elif rest and rest[0] in td.TF_ORDER:
                tf = rest[0]
        self.tf = tf
        self.cur_symbol = ticker

        try:
            if func in ("FA", "FIN"):
                fa_years = None       # default: all available history
                if rest and rest[0].isdigit():
                    fa_years = int(rest[0])
                await self.show_financials(ticker, fa_years)
            elif func in ("N", "NEWS"):
                await self.show_news(ticker)
            elif func in ("GP", "CHART", "G"):
                await self.show_chart(ticker, custom)
            else:
                await self.show_security(ticker, custom)
        except Exception as e:
            self.mode = "detail"; self._show_only("top")
            self.top().update(Text(f"  Error loading {ticker}: {e}", style=RED))

    # ---- function handlers -------------------------------------------------

    async def show_security(self, ticker, custom=None):
        self.mode = "des"
        self._show_only("top", "chart", "bottom")
        self.top().update(Text(f"  Loading {ticker} …", style=DIM))
        fund = await td.fetch_fundamentals(self.session, ticker)
        if not fund:
            # rates/indices/credit/crypto have no stock fundamentals — chart them
            if td.resolve_fred(ticker) or td.resolve_index(ticker) or td.is_crypto(ticker):
                await self.show_chart(ticker, custom); return
            self._show_only("top")
            self.top().update(Text(f"  No data for '{ticker}'.", style=RED)); return
        self.top().update(render_fundamentals(fund))
        await self._load_chart(ticker, "des", custom)
        try:
            news = await td.fetch_news(self.session, ticker, 6)
            self._link_urls, self._news_meta = [], []
            self._reader_back = (lambda t=ticker, c=custom: self.show_security(t, c))
            self.bottom().update(render_news(news, "NEWS", self._link_urls,
                                             self._news_meta))
        except Exception:
            self.bottom().update(Text(""))

    async def _add_ticker(self, parts):
        if not parts:
            self.notify("Usage: ADD <TICKER>  (then pick a section)")
            return
        tk = parts[0].upper()
        section_arg = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        # validate the ticker first
        fund = await td.fetch_fundamentals(self.session, tk)
        if not fund and not td.is_crypto(tk):
            self.notify(f"Unknown ticker: {tk}", severity="error"); return
        if section_arg:                                  # ADD NVDA STOCKS  (direct)
            sec = self._match_section(section_arg)
            if not sec:
                self.notify(f"No section matching '{section_arg}'", severity="error"); return
            self._file_ticker(tk, sec); return
        # no section given -> ask which one
        self._pending_add = tk
        self._show_section_picker(tk)

    def _match_section(self, arg):
        secs = dash.addable_sections()
        a = arg.strip().upper()
        if a.isdigit():
            i = int(a) - 1
            return secs[i] if 0 <= i < len(secs) else None
        for s in secs:                                   # exact (case-insensitive)
            if s.upper() == a:
                return s
        hits = [s for s in secs if a in s.upper()]       # unique substring
        return hits[0] if len(hits) == 1 else None

    def _show_section_picker(self, ticker):
        self.mode = "addpick"; self._show_only("top")
        secs = dash.addable_sections()
        self._pending_sections = secs
        t = Text()
        t.append(f"  Add  {ticker}  to which section?\n\n", style=f"bold {AMBER}")
        for i, s in enumerate(secs, 1):
            line = Text()
            line.append(f"   {i:>2}.  ", style=DIM)
            line.append(s + "\n", style=Style(color="orange1",
                        meta={"@click": f"app.file_ticker({i - 1})"}))
            t.append_text(line)
        t.append("\n   click a section, or type its number / name  ·  Esc cancels",
                 style=DIM)
        self.top().update(t)

    def action_file_ticker(self, idx):
        if self._pending_add is None:
            return
        try:
            sec = self._pending_sections[int(idx)]
        except (IndexError, ValueError, TypeError):
            return
        self._file_ticker(self._pending_add, sec)

    def _file_ticker(self, ticker, section):
        dash.add_to_section(ticker, section)
        dash.track([ticker])
        self._pending_add = None
        self._rebuild_pool()
        self.action_go_monitor()
        self.notify(f"Added {ticker} to {section}")

    async def show_chart(self, ticker, custom=None):
        self.mode = "chart"
        self.cur_mode = "chart"
        self._show_only("chart")
        await self._load_chart(ticker, "chart", custom)

    async def show_compare(self, symbols, custom=None):
        self.mode = "chart"
        self.cur_mode = "compare"
        self._show_only("chart")
        self.cmp_symbols = [s.upper() for s in symbols]
        self.cur_custom = custom
        self.cur_tf_label = "CUSTOM" if custom else self.tf
        self.cur_hover = None
        hdr = self.chart().query_one("#chdr", ChartHeader)
        hdr.set_content(Text(f"  comparing {', '.join(self.cmp_symbols)} …", style=DIM), [])
        items = []
        for s in self.cmp_symbols:
            bars = await td.fetch_history(self.session, s, self.tf, custom)
            if bars:
                items.append((s, bars))
        if len(items) < 2:
            hdr.set_content(Text("  need 2+ valid tickers to compare", style=RED), [])
            return
        self._cmp_items = items
        # rates/yields (FRED levels) plot on a 2nd axis at their actual level
        self._cmp_levels = [s for s, _ in items if td.resolve_fred(s)]
        await self._render_compare_image()

    def _chart_cells(self):
        """Chart area size in CHARACTER cells (for text/plotext charts)."""
        cols = max(self.app.size.width - 4, 60)
        rows = max(self.app.size.height - 9, 12)
        return cols, rows

    def _show_chart_text(self, ansi):
        """Display a text/braille chart (hide the image widget)."""
        self.chart().query_one("#chimg", TermImage).display = False
        w = self.chart().query_one("#chtext", Static)
        w.display = True
        w.update(Text.from_ansi(ansi) if ansi else Text("  no chart data", style=RED))

    async def _render_compare_image(self):
        """(Re)render the compare overlay from cached items — used on load & toggle."""
        if not self._cmp_items:
            return
        if self._chart_text:
            cols, rows = self._chart_cells()
            txt = await asyncio.to_thread(chart_render.render_compare_text,
                                          self._cmp_items, cols, rows, self.cmp_index100)
            self.cur_base_img, self.cur_geom = None, None
            self._show_chart_text(txt)
            self._refresh_compare_header()
            return
        w_px, h_px = self._chart_px("chart")
        img, geom = await asyncio.to_thread(
            chart_render.render_compare_png, self._cmp_items, w_px, h_px,
            self.cmp_index100, self._cmp_levels)
        self.cur_base_img, self.cur_geom = img, geom
        self._publish_chart(img)
        self._refresh_compare_header()

    async def toggle_index100(self):
        if self.cur_mode != "compare":
            return
        self.cmp_index100 = not self.cmp_index100
        self.cur_hover = None
        await self._render_compare_image()

    def _refresh_compare_header(self):
        g = self.cur_geom
        if self.cur_hover is not None and g and g.get("series"):
            i = max(0, min(self.cur_hover, g["n"] - 1))
            idx100 = g.get("index100")
            h = Text("  ◉ ", style=AMBER)
            h.append(f"{g['dates'][i]}", style="bold white")
            for ss in g["series"]:
                if ss.get("unit") == "level":            # rate/yield: actual level
                    txt = f"   {ss['sym']} {ss['vals'][i]:.2f}%"
                else:
                    v = ss["pct"][i]
                    txt = f"   {ss['sym']} {v:.1f}" if idx100 else f"   {ss['sym']} {v:+.1f}%"
                h.append(txt, style=ss["color"])
        else:
            h = Text("  hover to read each return at a date · click a timeframe", style=DIM)
        bar, spans = _timeframe_bar(self.cur_tf_label, self.show_indicators,
                                    compare=True, index100=self.cmp_index100)
        self.chart().query_one("#chdr", ChartHeader).set_content(Group(bar, h), spans)

    def _chart_px(self, mode):
        """Pixel size matching the image pane, so the chart fills the window."""
        cw, ch = _cell_px()
        cols = max(self.app.size.width - 2, 60)
        # rows available for the image: screen - header(1) - input(3) - footer(1) - chdr(3)
        rows = max(self.app.size.height - 8, 12)
        w, h = cols * cw, rows * ch
        if h > 1000:                       # cap for fast redraws, keep aspect
            w = round(w * 1000 / h); h = 1000
        return int(w), int(h)

    async def _load_chart(self, ticker, mode, custom=None):
        bars = await td.fetch_history(self.session, ticker, self.tf, custom)
        tf_label = "CUSTOM" if custom else self.tf
        if (not bars or len(bars) < 2) and not custom and self.tf != "1D":
            # thin daily history (e.g. a stock that IPO'd today) -> use intraday
            intra = await td.fetch_history(self.session, ticker, "1D")
            if intra and len(intra) >= 2:
                bars, tf_label = intra, "1D"
        if not bars or len(bars) < 2:
            self.chart().query_one("#chdr", ChartHeader).set_content(
                Text(f"  not enough chart data for {ticker} yet", style=RED), [])
            return
        fr = td.resolve_fred(ticker)
        idx = td.resolve_index(ticker)
        self.cur_symbol = ticker          # raw, used for re-fetch on timeframe change
        self.cur_display = fr[1] if fr else (idx[2] if idx else ticker.upper())
        self.cur_bars = bars
        self.cur_mode = mode
        self.cur_hover = None
        self.cur_tf_label = tf_label
        await self._render_chart_image()

    def _chart_to_browser(self, img):
        """Pop-out mode: write the chart PNG into an auto-refreshing HTML page and
        open it once in the browser. Subsequent renders overwrite it -> tab updates."""
        import base64
        import io
        import tempfile
        buf = io.BytesIO(); img.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        html = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<meta http-equiv='refresh' content='2'><title>Kessler Chart</title>"
                "<style>html,body{margin:0;background:#0a0a0a;}"
                "img{width:100%;height:auto;display:block;}</style></head>"
                f"<body><img src='data:image/png;base64,{b64}'></body></html>")
        path = os.path.join(tempfile.gettempdir(), "kessler_chart.html")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            if not self._popout_opened:
                webbrowser.open("file://" + path)
                self._popout_opened = True
                self.notify("Chart opened in your browser (auto-updates as you "
                            "change ticker/timeframe). Keep that tab open beside the terminal.")
        except Exception:
            pass

    def _publish_chart(self, img):
        if self._chart_popout:
            self._chart_to_browser(img)
        self.chart().query_one("#chtext", Static).display = False
        imgw = self.chart().query_one("#chimg", TermImage)
        imgw.display = True
        imgw.image = img

    def _set_popout(self, on):
        """Toggle chart pop-out (charts open live in the browser). Persisted via a
        .popout flag file so it survives restarts and auto-updates."""
        self._chart_popout = bool(on)
        flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".popout")
        try:
            if on:
                open(flag, "w").write("1")
            elif os.path.exists(flag):
                os.remove(flag)
        except Exception:
            pass
        if on:
            self.notify("Pop-out ON — charts open live in a browser tab "
                        "(auto-updates). Keep it open beside the terminal.")
            if self.cur_base_img is not None:           # push the current chart now
                self._popout_opened = False
                self._chart_to_browser(self.cur_base_img)
        else:
            self.notify("Pop-out OFF — charts render inline in the terminal.")
            self._popout_opened = False

    async def _render_chart_image(self):
        """(Re)render the chart from cached bars — image or text per terminal."""
        if self._chart_text:
            cols, rows = self._chart_cells()
            txt = await asyncio.to_thread(chart_render.render_chart_text,
                                          self.cur_display, self.cur_tf_label,
                                          self.cur_bars, self.show_indicators, cols, rows)
            self.cur_base_img, self.cur_geom = None, None
            self._show_chart_text(txt)
            self._refresh_chart_header()
            return
        w_px, h_px = self._chart_px(self.cur_mode)
        img, geom = await asyncio.to_thread(
            chart_render.render_chart_png, self.cur_display, self.cur_tf_label,
            self.cur_bars, self.cur_mode, w_px, h_px, self.show_indicators)
        self.cur_base_img = img
        self.cur_geom = geom
        self._publish_chart(img)
        self._refresh_chart_header()

    async def toggle_indicators(self):
        if self.cur_mode == "compare":
            return                       # no SMA/RSI in compare mode
        self.show_indicators = not self.show_indicators
        if self.cur_bars:
            await self._render_chart_image()

    def _refresh_chart_header(self):
        g, spans = _chart_header(self.cur_display, self.cur_tf_label, self.cur_bars,
                                 self.cur_mode, self.cur_hover, self.show_indicators)
        self.chart().query_one("#chdr", ChartHeader).set_content(g, spans)

    def chart_hover(self, x_cell, widget_w):
        if not widget_w or self.cur_geom is None:
            return
        g = self.cur_geom
        frac = x_cell / widget_w
        axspan = g["x1f"] - g["x0f"] or 1
        data_f = (frac - g["x0f"]) / axspan
        if self.cur_mode == "compare":
            n = g["n"]
            idx = max(0, min(n - 1, round(data_f * (n - 1))))
            if idx == self.cur_hover:
                return
            self.cur_hover = idx
            if self.cur_base_img is not None and self._hover_redraw:
                date = g["dates"][idx] if idx < len(g.get("dates", [])) else None
                self.chart().query_one("#chimg", TermImage).image = \
                    chart_render.draw_compare_markers(self.cur_base_img, g, idx, date)
            self._refresh_compare_header()
            return
        if not self.cur_bars:
            return
        n = len(self.cur_bars)
        idx = max(0, min(n - 1, round(g["xmin"] + data_f * (g["xmax"] - g["xmin"]))))
        if idx == self.cur_hover:
            return
        self.cur_hover = idx
        if self.cur_base_img is not None and self._hover_redraw:
            b = self.cur_bars[idx]
            label = f"{b['t']}   {b['c']:,.2f}"
            crossed = chart_render.draw_crosshair(self.cur_base_img, g, idx, b["c"], label)
            self.chart().query_one("#chimg", TermImage).image = crossed
        self._refresh_chart_header()

    def prompt_custom_range(self):
        try:
            self.query_one("#daterow").display = True
            self.query_one("#dfrom", Input).focus()
            self.notify("Fill the two date boxes (MM-DD-YYYY), then press Go or Enter")
        except Exception:
            pass

    def prompt_compare(self):
        base = self.cmp_symbols[0] if self.cur_mode == "compare" and self.cmp_symbols \
            else (self.cur_symbol or "")
        inp = self.query_one("#cmd", Input)
        inp.value = f"CMP {base} ".lstrip()
        if not base:
            inp.value = "CMP "
        inp.focus()
        self.notify("Type 2+ tickers to overlay (% return), e.g.  CMP AAPL MSFT")

    async def change_timeframe(self, tf):
        if tf not in td.TF_ORDER:
            return
        try:
            self.query_one("#daterow").display = False
        except Exception:
            pass
        self.tf = tf
        if self.cur_mode == "compare":
            await self.show_compare(self.cmp_symbols, None)
        elif self.cur_symbol:
            await self._load_chart(self.cur_symbol, self.cur_mode, None)

    async def show_financials(self, ticker, years=None):
        self.mode = "fa"; self._show_only("top")
        self.top().update(Text(f"  Loading {ticker} …", style=DIM))
        # ETF? show its top holdings instead of (non-existent) financials
        etf = await td.fetch_etf_holdings(self.session, ticker)
        if etf:
            self.top().update(render_etf_holdings(ticker, etf)); return
        fin = await td.fetch_financials(self.session, ticker, years)
        holders = await td.fetch_institutional_holders(self.session, ticker)
        blocks = [render_financials(ticker, fin)]
        if holders:
            blocks += [Text(""), render_institutional_holders(ticker, holders)]
        self.top().update(Group(*blocks))

    async def show_auctions(self):
        self.mode = "auct"; self._show_only("top")
        self.top().update(Text("  loading Treasury auctions …", style=DIM))
        try:
            au = await td.fetch_auctions(self.session, days=120, limit=30)
            self.top().update(render_auctions(au))
        except Exception as e:
            self.top().update(Text(f"  auctions unavailable: {e}", style=RED))

    async def show_news(self, ticker):
        self.mode = "news"; self._show_only("top")
        self.top().update(Text("  Loading news …", style=DIM))
        items = await td.fetch_news(self.session, ticker, 25)
        header = f"NEWS — {ticker}" if ticker else "MARKET NEWS"
        self._link_urls, self._news_meta = [], []
        self._reader_back = (lambda: self.show_news(ticker))
        self.top().update(render_news(items, header, self._link_urls, self._news_meta))

    async def show_news_dashboard(self):
        self.mode = "news"; self._show_only("top")
        self.top().update(Text("  Loading market news …", style=DIM))
        await self._render_wire()
        self._wire_on = True           # enable auto-refresh (cleared on any view switch)

    async def _render_wire(self):
        sections = await td.fetch_news_dashboard(self.session, per=7)
        if self.mode != "news":        # user navigated away while fetching
            return
        if not sections:
            self.top().update(Text("  news feed unavailable right now", style=RED))
            return
        when = datetime.now().strftime("%a %b %d  %H:%M")
        self._link_urls, self._news_meta = [], []
        self._reader_back = self.show_news_dashboard
        self.top().update(render_news_dashboard(sections, when, self._link_urls,
                                                self._news_meta))

    async def _wire_tick(self):
        if self._wire_on and self.mode == "news":
            await self._render_wire()

    # ---- in-terminal article reader ---------------------------------------

    async def action_read(self, idx):
        """Open the clicked headline in the in-terminal reader (fired by @click)."""
        try:
            i = int(idx)
            url = self._link_urls[i]
            headline, source = self._news_meta[i] if i < len(self._news_meta) else ("", "")
        except (IndexError, ValueError, TypeError):
            return
        self._wire_on = False          # pause board auto-refresh while reading
        self.mode = "reader"; self._show_only("reader")
        body = self.query_one("#readerbody", Static)
        body.update(Group(Text(f"  Loading “{_clip(headline, 70)}” …", style=DIM)))
        self.query_one("#reader", VerticalScroll).scroll_home(animate=False)
        art = await td.fetch_article(self.session, url)
        if self.mode != "reader":      # user navigated away mid-fetch
            return
        self._browser_links = []
        body.update(render_article(art, headline, source, self._browser_links))
        reader = self.query_one("#reader", VerticalScroll)
        reader.scroll_home(animate=False)
        reader.focus()                 # arrow/PageUp-Down scroll the article

    async def action_reader_back(self):
        if self._reader_back is not None:
            await self._reader_back()

    def action_open_url(self, idx):
        """Open a URL in the default browser (the reader's OPEN IN BROWSER link)."""
        try:
            url = self._browser_links[int(idx)]
        except (IndexError, ValueError, TypeError, AttributeError):
            return
        if url:
            webbrowser.open(url)


if __name__ == "__main__":
    MarketTerminal().run()
