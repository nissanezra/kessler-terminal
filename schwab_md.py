"""Schwab Market Data client — READ-ONLY (no account access, no trading).

Uses the "Market Data Production" product only: quotes + option chains. The app is
registered WITHOUT "Accounts and Trading", so the token this obtains is incapable of
seeing balances/positions or placing orders — by design.

Credentials (never in the repo / chat):
  - App Key + Secret: env SCHWAB_APP_KEY / SCHWAB_APP_SECRET, else the local dotfile
    `.schwab_md_creds` (two lines: key, then secret).
  - OAuth token cache: `.schwab_md_token.json` (access + refresh token). Refresh token
    lasts ~7 days (Schwab), so a quick re-login is needed weekly.

CLI:
  python schwab_md.py login     # one-time browser OAuth -> saves the token
  python schwab_md.py chain GDX # print a quick GDX chain summary (needs a valid token)
"""
import asyncio
import base64
import json
import os
import time
import urllib.parse
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
CRED_FILE = HERE / ".schwab_md_creds"
TOKEN_FILE = HERE / ".schwab_md_token.json"

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
MD_BASE = "https://api.schwabapi.com/marketdata/v1"
REDIRECT_URI = "https://127.0.0.1:8182"


# ---------------------------------------------------------------------------
# Credentials + token storage
# ---------------------------------------------------------------------------
def load_creds():
    """(app_key, app_secret) from env or the local dotfile, else (None, None)."""
    key = os.environ.get("SCHWAB_APP_KEY", "").strip()
    sec = os.environ.get("SCHWAB_APP_SECRET", "").strip()
    if key and sec:
        return key, sec
    if CRED_FILE.exists():
        lines = [ln.strip() for ln in CRED_FILE.read_text().splitlines() if ln.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]
    return None, None


def have_creds():
    return all(load_creds())


def _load_token():
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return None


def _save_token(tok):
    tok = dict(tok)
    # stamp an absolute expiry so we know when to refresh (access token ~30 min)
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 1800)) - 60
    TOKEN_FILE.write_text(json.dumps(tok))
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass


def _basic_auth():
    key, sec = load_creds()
    return base64.b64encode(f"{key}:{sec}".encode()).decode()


def is_configured():
    """True when we have creds AND a saved token (so the app can offer the view)."""
    return have_creds() and _load_token() is not None


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------
def authorize_url():
    key, _ = load_creds()
    q = urllib.parse.urlencode({"client_id": key, "redirect_uri": REDIRECT_URI})
    return f"{AUTH_URL}?{q}"


async def _post_token(session, data):
    headers = {"Authorization": f"Basic {_basic_auth()}",
               "Content-Type": "application/x-www-form-urlencoded"}
    async with session.post(TOKEN_URL, data=data, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=20)) as r:
        body = await r.json(content_type=None)
        if r.status != 200:
            raise RuntimeError(f"token {r.status}: {body}")
        return body


async def exchange_code(session, redirected_url):
    """Exchange the ?code=... from the post-login redirect URL for tokens."""
    parsed = urllib.parse.urlparse(redirected_url)
    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        raise RuntimeError("no ?code= found in the pasted URL")
    tok = await _post_token(session, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})
    _save_token(tok)
    return tok


async def _valid_access_token(session):
    tok = _load_token()
    if not tok:
        raise RuntimeError("not logged in — run `python schwab_md.py login`")
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    # refresh
    tok = await _post_token(session, {
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    # Schwab returns a fresh refresh_token too; keep whatever it sends
    _save_token(tok)
    return tok["access_token"]


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
async def _get(session, path, params):
    token = await _valid_access_token(session)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with session.get(f"{MD_BASE}{path}", params=params, headers=headers,
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        body = await r.json(content_type=None)
        if r.status != 200:
            raise RuntimeError(f"{path} {r.status}: {str(body)[:200]}")
        return body


def _contract(c):
    """One option contract row, only the fields the terminal shows."""
    def num(x):
        try:
            v = float(x)
            return None if v in (-999.0, -999, 0.0) and x in (-999.0, -999) else v
        except (TypeError, ValueError):
            return None
    return {
        "symbol": c.get("symbol"),
        "strike": c.get("strikePrice"),
        "bid": num(c.get("bid")), "ask": num(c.get("ask")),
        "last": num(c.get("last")), "mark": num(c.get("mark")),
        "delta": num(c.get("delta")), "gamma": num(c.get("gamma")),
        "theta": num(c.get("theta")), "vega": num(c.get("vega")),
        "iv": num(c.get("volatility")),            # implied vol (%)
        "oi": c.get("openInterest"), "volume": c.get("totalVolume"),
        "dte": c.get("daysToExpiration"),
    }


def parse_chain(d):
    """Flatten Schwab's call/put ExpDateMap into per-expiration call/put strike rows."""
    exps = {}
    for side, key in (("calls", "callExpDateMap"), ("puts", "putExpDateMap")):
        for exp_key, strikes in (d.get(key) or {}).items():
            exp = exp_key.split(":")[0]                # "2026-09-19:30" -> "2026-09-19"
            row = exps.setdefault(exp, {"exp": exp, "calls": [], "puts": []})
            for _strike, contracts in sorted(strikes.items(), key=lambda kv: float(kv[0])):
                for c in contracts:
                    row[side].append(_contract(c))
    return {
        "symbol": d.get("symbol"),
        "underlyingPrice": d.get("underlyingPrice"),
        "isDelayed": d.get("isDelayed"),
        "expirations": [exps[k] for k in sorted(exps)],
    }


async def get_option_chain(session, symbol, strike_count=None, contract_type="ALL"):
    """Full option chain for `symbol` (calls + puts, all expirations)."""
    params = {"symbol": symbol.upper(), "contractType": contract_type,
              "includeUnderlyingQuote": "true"}
    if strike_count:
        params["strikeCount"] = str(strike_count)
    return parse_chain(await _get(session, "/chains", params))


async def get_quotes(session, symbols):
    return await _get(session, "/quotes", {"symbols": ",".join(symbols)})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _cli():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    async with aiohttp.ClientSession() as s:
        if cmd == "login":
            if not have_creds():
                print("Missing App Key/Secret. Put them in", CRED_FILE,
                      "(line 1 = key, line 2 = secret) or set SCHWAB_APP_KEY/SECRET.")
                return
            print("\n1) Open this URL in your browser and log in to Schwab:\n")
            print("   " + authorize_url())
            print("\n2) After approving, the browser will try to load a 127.0.0.1 page that"
                  " fails —\n   that's expected. Copy the FULL URL from the address bar"
                  " (it has ?code=...).\n")
            pasted = input("Paste the redirected URL here: ").strip()
            await exchange_code(s, pasted)
            print("✓ token saved to", TOKEN_FILE)
        elif cmd == "chain":
            sym = (sys.argv[2] if len(sys.argv) > 2 else "GDX").upper()
            ch = await get_option_chain(s, sym, strike_count=6)
            print(f"{ch['symbol']}  underlying={ch['underlyingPrice']}  "
                  f"delayed={ch['isDelayed']}  expirations={len(ch['expirations'])}")
            if ch["expirations"]:
                e = ch["expirations"][0]
                print(f"  nearest exp {e['exp']}: {len(e['calls'])} calls / {len(e['puts'])} puts")
                for c in e["calls"][:4]:
                    print(f"    C {c['strike']}: bid {c['bid']} ask {c['ask']} "
                          f"IV {c['iv']} delta {c['delta']} OI {c['oi']}")
        else:
            print(__doc__)


if __name__ == "__main__":
    asyncio.run(_cli())
