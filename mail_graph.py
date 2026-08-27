"""Microsoft 365 / Outlook mail client for the terminal's MESSAGES panel.

Read + send for a single mailbox (e.g. robert@kesslercompanies.com) via Microsoft
Graph, using the OAuth **device-code** flow — no client secret, no hosted redirect,
and delegated (user-consented) scopes only. The token it holds can read and send
that user's mail and nothing else.

Credentials (never in the repo / chat):
  - App (client) ID + tenant ID: env MAIL_CLIENT_ID / MAIL_TENANT_ID, else the local
    dotfile `.mail_graph_creds` (line 1 = client id, line 2 = tenant id).
  - OAuth token cache: `.mail_graph_token.json` (access + refresh token). The refresh
    token is long-lived, so a one-time `login` keeps working for weeks.

One-time setup (see the terminal notes):
  1. Register an app in Entra (entra.microsoft.com) for the kesslercompanies.com tenant,
     delegated Graph scopes Mail.Read + Mail.Send + offline_access + User.Read, and turn
     ON "Allow public client flows".
  2. Put the client id + tenant id in `.mail_graph_creds` (or the env vars).
  3. Run `python mail_graph.py login` and follow the code prompt once.

CLI:
  python mail_graph.py login    # one-time device-code sign-in -> saves the token
  python mail_graph.py inbox    # print the latest inbox headers (needs a valid token)
  python mail_graph.py whoami   # show the signed-in mailbox address
"""
import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
CRED_FILE = HERE / ".mail_graph_creds"
TOKEN_FILE = HERE / ".mail_graph_token.json"

AUTHORITY = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
# Delegated, user-consentable scopes only. offline_access -> we get a refresh token.
SCOPES = "offline_access Mail.Read Mail.Send User.Read"
UA = {"User-Agent": "KesslerTerminal/1.0"}


# ---------------------------------------------------------------------------
# Credentials + token storage
# ---------------------------------------------------------------------------
def load_creds():
    """(client_id, tenant_id) from env or the local dotfile, else (None, None)."""
    cid = os.environ.get("MAIL_CLIENT_ID", "").strip()
    tid = os.environ.get("MAIL_TENANT_ID", "").strip()
    if cid and tid:
        return cid, tid
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


def _save_token(tok, keep_email=True):
    tok = dict(tok)
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 120
    if keep_email:
        old = _load_token() or {}
        if old.get("email") and "email" not in tok:
            tok["email"] = old["email"]
    TOKEN_FILE.write_text(json.dumps(tok))
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass


def is_configured():
    """True when we have creds AND a saved token, so the app can offer the panel."""
    return have_creds() and _load_token() is not None


def account_email():
    """The signed-in mailbox address, if known (cached at login)."""
    return (_load_token() or {}).get("email")


def _token_endpoint():
    _, tid = load_creds()
    return f"{AUTHORITY}/{tid}/oauth2/v2.0/token"


# ---------------------------------------------------------------------------
# OAuth — device-code flow
# ---------------------------------------------------------------------------
async def start_device_code(session):
    """Kick off device-code login; returns the dict with user_code + verification_uri."""
    cid, tid = load_creds()
    if not (cid and tid):
        raise RuntimeError("missing MAIL_CLIENT_ID / MAIL_TENANT_ID")
    url = f"{AUTHORITY}/{tid}/oauth2/v2.0/devicecode"
    async with session.post(url, data={"client_id": cid, "scope": SCOPES},
                            headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as r:
        body = await r.json(content_type=None)
        if r.status != 200:
            raise RuntimeError(f"devicecode {r.status}: {body}")
        return body


async def poll_device_code(session, device_code, interval=5, expires_in=900):
    """Poll until the user finishes signing in, then save the token."""
    cid, _ = load_creds()
    data = {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": cid, "device_code": device_code}
    deadline = time.time() + expires_in
    while time.time() < deadline:
        await asyncio.sleep(interval)
        async with session.post(_token_endpoint(), data=data, headers=UA,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            body = await r.json(content_type=None)
        if r.status == 200:
            _save_token(body)
            await _cache_email(session)
            return body
        err = body.get("error")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                interval += 5
            continue
        raise RuntimeError(f"device login failed: {body}")
    raise RuntimeError("device login timed out")


async def _valid_access_token(session):
    tok = _load_token()
    if not tok:
        raise RuntimeError("not signed in — run `python mail_graph.py login`")
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    cid, _ = load_creds()
    data = {"grant_type": "refresh_token", "client_id": cid,
            "scope": SCOPES, "refresh_token": tok["refresh_token"]}
    async with session.post(_token_endpoint(), data=data, headers=UA,
                            timeout=aiohttp.ClientTimeout(total=20)) as r:
        body = await r.json(content_type=None)
        if r.status != 200:
            raise RuntimeError(f"refresh {r.status}: {body}")
    _save_token(body)
    return body["access_token"]


# ---------------------------------------------------------------------------
# Graph calls
# ---------------------------------------------------------------------------
async def _get(session, path, params=None):
    token = await _valid_access_token(session)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", **UA}
    async with session.get(f"{GRAPH}{path}", params=params, headers=headers,
                           timeout=aiohttp.ClientTimeout(total=25)) as r:
        body = await r.json(content_type=None)
        if r.status != 200:
            raise RuntimeError(f"{path} {r.status}: {str(body)[:200]}")
        return body


async def _cache_email(session):
    try:
        me = await _get(session, "/me", {"$select": "mail,userPrincipalName,displayName"})
        email = me.get("mail") or me.get("userPrincipalName")
        if email:
            tok = _load_token() or {}
            tok["email"] = email
            tok["display_name"] = me.get("displayName")
            _save_token(tok, keep_email=False)
        return email
    except Exception:
        return None


def _addr(recipient):
    ea = (recipient or {}).get("emailAddress", {})
    return {"name": ea.get("name"), "email": ea.get("address")}


async def list_messages(session, folder="inbox", top=25):
    """Latest message headers from a well-known folder (inbox / sentitems / drafts)."""
    params = {"$top": str(top), "$orderby": "receivedDateTime desc",
              "$select": "subject,from,receivedDateTime,isRead,bodyPreview,hasAttachments"}
    data = await _get(session, f"/me/mailFolders/{folder}/messages", params)
    out = []
    for m in data.get("value", []):
        out.append({
            "id": m.get("id"),
            "subject": m.get("subject") or "(no subject)",
            "from": _addr(m.get("from")),
            "received": m.get("receivedDateTime"),
            "unread": not m.get("isRead", True),
            "preview": (m.get("bodyPreview") or "").strip(),
            "hasAttachments": m.get("hasAttachments", False),
        })
    return out


async def get_message(session, msg_id):
    """One full message: recipients + HTML/text body."""
    params = {"$select": "subject,from,toRecipients,ccRecipients,receivedDateTime,body"}
    m = await _get(session, f"/me/messages/{msg_id}", params)
    body = m.get("body", {})
    return {
        "id": m.get("id"),
        "subject": m.get("subject") or "(no subject)",
        "from": _addr(m.get("from")),
        "to": [_addr(x) for x in m.get("toRecipients", [])],
        "cc": [_addr(x) for x in m.get("ccRecipients", [])],
        "received": m.get("receivedDateTime"),
        "bodyType": body.get("contentType", "text"),
        "body": body.get("content", ""),
    }


async def send_message(session, to, subject, text, cc=None):
    """Send a plain-text message. `to`/`cc` are lists of addresses (or comma strings)."""
    def rcpts(v):
        if isinstance(v, str):
            v = [a.strip() for a in v.replace(";", ",").split(",") if a.strip()]
        return [{"emailAddress": {"address": a}} for a in (v or [])]
    payload = {"message": {
        "subject": subject or "(no subject)",
        "body": {"contentType": "Text", "content": text or ""},
        "toRecipients": rcpts(to),
        "ccRecipients": rcpts(cc),
    }, "saveToSentItems": True}
    token = await _valid_access_token(session)
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json", **UA}
    async with session.post(f"{GRAPH}/me/sendMail", json=payload, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=25)) as r:
        if r.status not in (200, 202):
            raise RuntimeError(f"sendMail {r.status}: {(await r.text())[:200]}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _cli():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    async with aiohttp.ClientSession() as s:
        if cmd == "login":
            if not have_creds():
                print("Missing client/tenant id. Put them in", CRED_FILE,
                      "(line 1 = client id, line 2 = tenant id) or set "
                      "MAIL_CLIENT_ID / MAIL_TENANT_ID.")
                return
            dc = await start_device_code(s)
            print("\n" + dc.get("message", ""))
            print(f"\n  URL : {dc.get('verification_uri')}")
            print(f"  CODE: {dc.get('user_code')}\n")
            print("Waiting for you to finish signing in…")
            await poll_device_code(s, dc["device_code"],
                                   int(dc.get("interval", 5)), int(dc.get("expires_in", 900)))
            print("\n✓ signed in as", account_email(), "— token saved to", TOKEN_FILE)
        elif cmd == "whoami":
            print(await _cache_email(s) or "not signed in")
        elif cmd == "inbox":
            for m in await list_messages(s, top=15):
                flag = "•" if m["unread"] else " "
                frm = (m["from"]["name"] or m["from"]["email"] or "?")[:24]
                print(f" {flag} {m['received'][:16]}  {frm:24}  {m['subject'][:50]}")
        else:
            print(__doc__)


if __name__ == "__main__":
    asyncio.run(_cli())
