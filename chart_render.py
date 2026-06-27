"""Render Bloomberg-style market charts as images (matplotlib, dark theme).

Used by the terminal when running in an image-capable terminal (iTerm2/kitty),
to draw genuinely smooth anti-aliased lines instead of text characters.
"""

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import os
from PIL import Image, ImageDraw, ImageFont

import terminal_data as td

BG = "#0a0a0a"
GRID = "#1e1e1e"
FG = "#888888"
C_PRICE = "#f5f5f5"
C_SMA50 = "#ffd400"
C_SMA100 = "#27d17e"
C_SMA200 = "#ff3b9a"
C_RSI = "#34c3d6"
C_UP = "#21c97a"
C_DOWN = "#ff5a5a"
C_AMBER = "#ffa500"


def _nan(seq):
    return np.array([np.nan if v is None else v for v in seq], dtype=float)


def render_chart_png(symbol, tf, bars, mode="chart", w_px=1600, h_px=900,
                     indicators=True, light=False):
    """Return a PIL.Image of the chart. indicators=False -> clean price-only.
    light=True renders a white-background (printer-friendly) version."""
    # local palette shadows the module dark theme; light theme for printing
    BG, GRID, FG, C_PRICE, C_SMA50, C_SMA100, C_SMA200, C_RSI, C_AMBER, C_UP, C_DOWN = (
        ("#ffffff", "#d9d9d9", "#555555", "#111111", "#b38f00", "#1a8f54",
         "#cc0066", "#1a8a99", "#cc7a00", "#0a8a0a", "#cc0000") if light else
        ("#0a0a0a", "#1e1e1e", "#888888", "#f5f5f5", "#ffd400", "#27d17e",
         "#ff3b9a", "#34c3d6", "#ffa500", "#21c97a", "#ff5a5a"))
    title_fg = "black" if light else "white"
    closes = [b["c"] for b in bars]
    n = len(closes)
    if n < 2:
        img = Image.new("RGB", (w_px, h_px), BG)
        return img, None
    x = np.arange(n)
    last, first = closes[-1], closes[0]
    chg = (last / first - 1) * 100 if first else 0
    full = (mode == "chart") and indicators

    dpi = 100
    fig = plt.figure(figsize=(w_px / dpi, h_px / dpi), dpi=dpi, facecolor=BG)
    if full:
        gs = fig.add_gridspec(4, 1, hspace=0.08)
        ax = fig.add_subplot(gs[0:3, 0])
        axr = fig.add_subplot(gs[3, 0], sharex=ax)
    else:
        ax = fig.add_subplot(1, 1, 1)
        axr = None

    for a in filter(None, [ax, axr]):
        a.set_facecolor(BG)
        a.grid(True, color=GRID, lw=0.6)
        for s in a.spines.values():
            s.set_color(GRID)
        a.tick_params(colors=FG, labelsize=11)

    # price + moving averages
    ax.plot(x, closes, color=C_PRICE, lw=1.4, label="Price")
    if full:
        ax.plot(x, _nan(td.sma(closes, 50)), color=C_SMA50, lw=1.1, label="SMA 50")
        ax.plot(x, _nan(td.sma(closes, 100)), color=C_SMA100, lw=1.1, label="SMA 100")
        ax.plot(x, _nan(td.sma(closes, 200)), color=C_SMA200, lw=1.1, label="SMA 200")
        leg = ax.legend(loc="upper left", facecolor=BG, edgecolor=GRID,
                        labelcolor="#cccccc", fontsize=10, framealpha=0.6)
    ax.margins(x=0.005)

    # title — laid out left-to-right from the badge width so long names
    # (e.g. "DOW JONES · DIA") don't overlap the price/change text.
    color = C_UP if chg >= 0 else C_DOWN

    def _cwf(fs):                       # monospace char width as a figure fraction
        return fs * 0.6 * (100 / 72) / w_px

    last_s = f"{last:,.2f}"
    chg_s = f"{chg:+.2f}%   {tf}"
    rng_s = f"{bars[0]['t']}  →  {bars[-1]['t']}"
    x = 0.012
    fig.text(x, 0.965, symbol, color="black", fontsize=15, weight="bold",
             family="monospace", bbox=dict(boxstyle="square,pad=0.3", fc=C_AMBER, ec="none"))
    x += (len(symbol) + 3) * _cwf(15)
    fig.text(x, 0.96, last_s, color=title_fg, fontsize=15, weight="bold", family="monospace")
    x += (len(last_s) + 2) * _cwf(15)
    fig.text(x, 0.96, chg_s, color=color, fontsize=14, family="monospace")
    x += (len(chg_s) + 2) * _cwf(14)
    fig.text(x, 0.962, rng_s, color=FG, fontsize=11, family="monospace")

    # x ticks: ~8 evenly spaced date labels
    step = max(n // 8, 1)
    ticks = list(range(0, n, step))
    ax_target = axr if full else ax
    ax_target.set_xticks(ticks)
    ax_target.set_xticklabels([bars[i]["t"] for i in ticks], fontsize=10)
    if full:
        plt.setp(ax.get_xticklabels(), visible=False)

    # RSI panel
    if full:
        rsi = _nan(td.rsi(closes, 14))
        axr.plot(x, rsi, color=C_RSI, lw=1.2)
        axr.axhline(70, color="#555", lw=0.8, ls="--")
        axr.axhline(30, color="#555", lw=0.8, ls="--")
        axr.set_ylim(0, 100)
        axr.set_yticks([30, 50, 70])
        axr.text(0.004, 0.86, "RSI(14)", transform=axr.transAxes, color=C_RSI,
                 fontsize=10, family="monospace")

    # StrMethodFormatter uses str.format, which supports comma grouping ("{x:,.2f}").
    # FormatStrFormatter ("%,.2f") does NOT — it raises on the comma for prices >= 1000.
    ax.yaxis.set_major_formatter(mticker.StrMethodFormatter(
        "{x:,.2f}" if max(closes) >= 1000 else "{x:.2f}"))
    fig.subplots_adjust(left=0.058, right=0.992, top=0.93, bottom=0.06)

    # capture the price-axes geometry so the caller can place a crosshair
    fig.canvas.draw()
    pos = ax.get_position()
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    geom = {"x0f": pos.x0, "x1f": pos.x1, "y0f": pos.y0, "y1f": pos.y1,
            "xmin": xmin, "xmax": xmax, "ymin": ymin, "ymax": ymax, "n": n}

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB"), geom


_FONT_PATH = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans.ttf")
COMPARE_COLORS = ["#f5f5f5", "#ffd400", "#34c3d6", "#ff3b9a", "#27d17e", "#ffa500"]


def _resample_raw(closes, N):
    """Linearly resample the raw price series to N evenly-spaced points."""
    n = len(closes)
    if n < 2:
        return [closes[0] if closes else 0.0] * N
    out = []
    for c in range(N):
        pos = c / (N - 1) * (n - 1)
        i = int(pos)
        out.append(closes[-1] if i >= n - 1 else
                   closes[i] * (1 - (pos - i)) + closes[i + 1] * (pos - i))
    return out


def _resample_pct(closes, N):
    out = _resample_raw(closes, N)
    base = out[0] or 1
    return [(v / base - 1) * 100 for v in out]


def render_compare_png(items, w_px=1600, h_px=900, index100=False, level_syms=None):
    """items = [(symbol, bars), ...]. Overlays each ticker's return, ALIGNED on a
    shared real date axis (union of trading days, forward-filled) and REBASED to a
    common start date — so every x is the same calendar day for every line.
    index100=False -> cumulative % return (rebased to 0%); True -> indexed to 100.
    level_syms: symbols (rates/yields) drawn on a SECOND right axis at their actual
    level instead of rebased — so a rate overlays sensibly on price indexes.
    Returns (PIL.Image, geom)."""
    items = [(s, b) for s, b in items if b and len(b) >= 2]
    if len(items) < 2:
        return Image.new("RGB", (w_px, h_px), BG), None
    level_syms = {s.upper() for s in (level_syms or [])}

    # --- align all series on a common, real date axis -------------------------
    maps = [{bar["t"]: bar["c"] for bar in b} for _, b in items]
    base_date = max(b[0]["t"] for _, b in items)     # common start = latest first day
    dates = [d for d in sorted(set().union(*maps)) if d >= base_date]
    if len(dates) < 2:
        return Image.new("RGB", (w_px, h_px), BG), None
    N = len(dates)

    dpi = 100
    fig = plt.figure(figsize=(w_px / dpi, h_px / dpi), dpi=dpi, facecolor=BG)
    ax = fig.add_subplot(1, 1, 1)
    ax.set_facecolor(BG); ax.grid(True, color=GRID, lw=0.6)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.tick_params(colors=FG, labelsize=11)
    has_level = any(sym.upper() in level_syms for sym, _ in items)
    ax2 = ax.twinx() if has_level else None
    if ax2 is not None:
        ax2.set_facecolor("none")
        ax2.tick_params(colors=FG, labelsize=11)
        for sp in ax2.spines.values():
            sp.set_color(GRID)

    series, handles = [], []
    for (sym, _bars), m, color in zip(items, maps, COMPARE_COLORS):
        prior = [c for d, c in sorted(m.items()) if d <= base_date]
        last = prior[-1] if prior else next(iter(m.values()))
        vals = []
        for d in dates:                              # forward-fill onto the axis
            if d in m:
                last = m[d]
            vals.append(last)
        if sym.upper() in level_syms:                # rate / yield -> right axis, actual level
            plot = vals[:]
            (ln,) = ax2.plot(range(N), plot, color=color, lw=1.4,
                             label=f"{sym}  {plot[-1]:.2f}%")
            series.append({"sym": sym, "color": color, "plot": plot, "pct": plot,
                           "vals": vals, "axis": "right", "unit": "level"})
        else:
            base = vals[0] or 1
            if index100:
                plot = [v / base * 100 for v in vals]
                label = f"{sym}  {plot[-1]:.1f}"
            else:
                plot = [(v / base - 1) * 100 for v in vals]
                label = f"{sym}  {plot[-1]:+.1f}%"
            (ln,) = ax.plot(range(N), plot, color=color, lw=1.4, label=label)
            series.append({"sym": sym, "color": color, "plot": plot, "pct": plot,
                           "vals": vals, "axis": "left", "unit": "pct"})
        handles.append(ln)

    ax.axhline(100 if index100 else 0, color="#666", lw=0.8)
    ax.legend(handles=handles, loc="upper left", facecolor=BG, edgecolor=GRID,
              labelcolor="#ccc", fontsize=12, framealpha=0.6)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter(
        "%g" if index100 else "%+g%%"))
    if ax2 is not None:
        ax2.yaxis.set_major_formatter(mticker.FormatStrFormatter("%g%%"))
    fig.text(0.012, 0.955, "COMPARE  " + "  vs  ".join(s for s, _ in items),
             color=C_AMBER, fontsize=14, weight="bold", family="monospace")
    mode_lbl = "indexed to 100" if index100 else "% return"
    if has_level:
        mode_lbl += " · rate on right axis"
    fig.text(0.45, 0.957, f"{dates[0]}  →  {dates[-1]}    ({mode_lbl}, common start)",
             color=FG, fontsize=11, family="monospace")
    step = max(N // 8, 1)
    ticks = list(range(0, N, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([dates[t] for t in ticks], fontsize=10)
    rgt = 0.945 if ax2 is not None else 0.99
    fig.subplots_adjust(left=0.052, right=rgt, top=0.92, bottom=0.07)
    fig.canvas.draw()
    pos = ax.get_position()
    lmin, lmax = ax.get_ylim()
    geom = {"x0f": pos.x0, "x1f": pos.x1, "y0f": pos.y0, "y1f": pos.y1, "n": N,
            "ymin": lmin, "ymax": lmax, "compare": True, "series": series,
            "dates": dates, "index100": index100,
            "axis_left": {"ymin": lmin, "ymax": lmax},
            "axis_right": ({"ymin": ax2.get_ylim()[0], "ymax": ax2.get_ylim()[1]}
                           if ax2 is not None else None)}
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB"), geom


def _hex_to_rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def draw_compare_markers(base, geom, idx, date_label=None):
    """Small cross on EACH compared line at column idx, a value tag in the line's
    colour beside each cross, and a little date tag up top."""
    img = base.copy()
    w, h = img.size
    d = ImageDraw.Draw(img)
    x0, x1 = geom["x0f"] * w, geom["x1f"] * w
    ytop, ybot = (1 - geom["y1f"]) * h, (1 - geom["y0f"]) * h
    al = geom.get("axis_left") or {"ymin": geom.get("ymin", 0.0), "ymax": geom.get("ymax", 1.0)}
    ar = geom.get("axis_right")
    px = x0 + idx / max(geom["n"] - 1, 1) * (x1 - x0)
    R = 10
    try:
        vfont = ImageFont.truetype(_FONT_PATH, 17)
    except Exception:
        vfont = ImageFont.load_default()
    # collect each line's point (on its own axis), then place value tags
    pts = []
    for ss in geom["series"]:
        axinfo = ar if (ss.get("axis") == "right" and ar) else al
        ymin, ymax = axinfo["ymin"], axinfo["ymax"]
        yr = (ymax - ymin) or 1
        v = ss.get("plot", ss["pct"])[idx]       # plotted value on this line's axis
        raw = ss.get("vals", ss["pct"])[idx]     # raw value for the label
        py = ytop + (ymax - v) / yr * (ybot - ytop)
        col = _hex_to_rgb(ss["color"])
        d.line([(px - R, py), (px + R, py)], fill=col, width=1)
        d.line([(px, py - R), (px, py + R)], fill=col, width=1)
        d.ellipse([px - 3, py - 3, px + 3, py + 3], outline=col, width=2)
        pts.append((py, raw, ss.get("unit"), col))
    near_right = px > (x0 + x1) / 2
    placed = []                                  # y-centres already used
    for py, price, unit, col in sorted(pts, key=lambda p: p[0]):
        if unit == "level":
            label = f"{price:.2f}%"
        else:
            label = f"{price:,.2f}" if abs(price) < 1000 else f"{price:,.0f}"
        tb = d.textbbox((0, 0), label, font=vfont)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        lx = px - R - 10 - tw if near_right else px + R + 10
        lx = min(max(lx, x0), x1 - tw)
        ly = py - th // 2 - 2
        while any(abs(ly - p) < th + 8 for p in placed):   # nudge down off overlaps
            ly += th + 8
        placed.append(ly)
        d.rectangle([lx - 5, ly - 3, lx + tw + 5, ly + th + 5], fill=(12, 12, 12),
                    outline=col)
        d.text((lx, ly), label, fill=col, font=vfont)
    if date_label:
        try:
            font = ImageFont.truetype(_FONT_PATH, 20)
        except Exception:
            font = ImageFont.load_default()
        tb = d.textbbox((0, 0), date_label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        lx = min(max(px - tw / 2, x0), x1 - tw)
        ly = ytop + 2
        d.rectangle([lx - 5, ly - 3, lx + tw + 5, ly + th + 5], fill=(18, 18, 18),
                    outline=(255, 176, 0))
        d.text((lx, ly), date_label, fill=(245, 245, 245), font=font)
    return img


def draw_crosshair(base, geom, idx, value, label=None):
    """Return a copy of `base` with a SMALL cross marker at (idx, value) and a
    little date/price tag beside it (instead of full-width axis lines)."""
    img = base.copy()
    w, h = img.size
    d = ImageDraw.Draw(img)
    x0, x1 = geom["x0f"] * w, geom["x1f"] * w
    ytop, ybot = (1 - geom["y1f"]) * h, (1 - geom["y0f"]) * h
    xr = geom["xmax"] - geom["xmin"] or 1
    yr = geom["ymax"] - geom["ymin"] or 1
    px = x0 + (idx - geom["xmin"]) / xr * (x1 - x0)
    py = ytop + (geom["ymax"] - value) / yr * (ybot - ytop)
    col = (255, 176, 0)
    R = 13                                   # small cross arm length
    d.line([(px - R, py), (px + R, py)], fill=col, width=1)
    d.line([(px, py - R), (px, py + R)], fill=col, width=1)
    d.ellipse([px - 3, py - 3, px + 3, py + 3], outline=col, width=2)

    if label:
        try:
            font = ImageFont.truetype(_FONT_PATH, 20)
        except Exception:
            font = ImageFont.load_default()
        tb = d.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        lx, ly = px + R + 8, py - th - 14
        if lx + tw + 10 > w:                 # flip to the left near the right edge
            lx = px - R - 8 - tw - 10
        if ly < ytop + 2:                    # drop below if too high
            ly = py + R + 8
        d.rectangle([lx - 5, ly - 4, lx + tw + 5, ly + th + 6], fill=(18, 18, 18), outline=col)
        d.text((lx, ly), label, fill=(245, 245, 245), font=font)
    return img


if __name__ == "__main__":
    import asyncio
    import aiohttp

    async def _demo():
        async with aiohttp.ClientSession() as s:
            bars = await td.fetch_history(s, "AAPL", "1Y")
        img, geom = render_chart_png("AAPL", "1Y", bars, "chart")
        img = draw_crosshair(img, geom, geom["n"] // 2, bars[geom["n"] // 2]["c"])
        img.save("/tmp/chart_demo.png")
        print("saved /tmp/chart_demo.png", img.size, geom)

    asyncio.run(_demo())
