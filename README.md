# Kessler Terminal

Code for a personal Bloomberg-style market terminal (Python/Textual). This repo
hosts the application code so installed copies can **auto-update** on launch.

- `terminal.py` — the TUI app (command line, charts, compare, portfolio, news, print)
- `dashboard.py` — the live market monitor + data poll loops
- `terminal_data.py` — data fetching (history, fundamentals, financials, FRED, etc.)
- `chart_render.py` — chart images (price/SMA/RSI, compare overlay, crosshair)
- `update.py` — self-updater (pulls newer code from this repo)
- `version.json` — version manifest the updater checks

No secrets, API keys, or personal/portfolio data live here — those stay only on
each installed machine. Data sources are all free/no-key (CNBC, Binance, FRED,
StockAnalysis, SEC EDGAR, TreasuryDirect, Google News).
