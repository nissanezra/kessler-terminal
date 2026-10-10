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

# App-only (client-credentials) access token cache. When a client secret + target
# mailbox are configured the module runs app-only: the app authenticates as itself
# (no user sign-in, no MFA) and reads/sends that one mailbox via /users/{mailbox}.
_APP_TOKEN = {"token": "", "exp": 0.0}


# ---------------------------------------------------------------------------
# Credentials + token storage
# ---------------------------------------------------------------------------
# The client SECRET never lives in a file — it is stored in the OS keychain
# (macOS Keychain / Windows Credential Manager) under this service/account.
KEYRING_SERVICE = "KesslerTerminal"
KEYRING_ACCOUNT = "mail_client_secret"


def _cfg():
    """Non-secret config (client id, tenant, mailbox) from .mail_graph_creds.
    Preferred form is key=value lines; legacy positional lines are still read."""
    d = {}
    if not CRED_FILE.exists():
        return d
    lines = [ln.strip() for ln in CRED_FILE.read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if any("=" in ln for ln in lines):
        for ln in lines:
            if "=" in ln:
                k, v = ln.split("=", 1)
                d[k.strip().lower()] = v.strip()
    else:                                     # legacy: client id / tenant / secret / mailbox
        for key, val in zip(("client_id", "tenant", "secret", "mailbox"), lines):
            d[key] = val
    return d


def load_creds():
    """(client_id, tenant_id) from env or the config file, else (None, None)."""
    c = _cfg()
    cid = os.environ.get("MAIL_CLIENT_ID", "").strip() or c.get("client_id", "")
    tid = os.environ.get("MAIL_TENANT_ID", "").strip() or c.get("tenant", "")
    return (cid or None, tid or None)


def have_creds():
    return all(load_creds())


def keyring_set_secret(secret):
    """Store the client secret in the OS keychain."""
    import keyring
    keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, secret)


def keyring_clear_secret():
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except Exception:
        pass


def _app_secret():
    """Client secret for app-only mode: env override -> OS keychain -> config file (fallback)."""
    env = os.environ.get("MAIL_CLIENT_SECRET", "").strip()
    if env:
        return env
    try:
        import keyring
        kv = (keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) or "").strip()
        if kv:
            return kv
    except Exception:
        pass
    return _cfg().get("secret", "")


def _mailbox():
    """Target mailbox for app-only mode, e.g. rkessler@kesslercompanies.com."""
    return os.environ.get("MAIL_MAILBOX", "").strip() or _cfg().get("mailbox", "")


def app_only():
    """True when client id + tenant + secret + mailbox are all set -> app-only (no MFA)."""
    cid, tid = load_creds()
    return bool(cid and tid and _app_secret() and _mailbox())


def _base():
    """Graph path root: /me for delegated, /users/<mailbox> for app-only."""
    if app_only():
        from urllib.parse import quote
        return "/users/" + quote(_mailbox())
    return "/me"


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
    """True when the panel can go live: app-only creds present, OR delegated creds + a token."""
    return app_only() or (have_creds() and _load_token() is not None)


def account_email():
    """The mailbox address shown in the panel."""
    if app_only():
        return _mailbox()
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


async def _app_access_token(session):
    """Client-credentials token for app-only mode (cached until it nears expiry)."""
    now = time.time()
    if _APP_TOKEN["token"] and now < _APP_TOKEN["exp"]:
        return _APP_TOKEN["token"]
    cid, _ = load_creds()
    data = {"grant_type": "client_credentials", "client_id": cid,
            "client_secret": _app_secret(),
            "scope": "https://graph.microsoft.com/.default"}
    async with session.post(_token_endpoint(), data=data, headers=UA,
                            timeout=aiohttp.ClientTimeout(total=20)) as r:
        body = await r.json(content_type=None)
        if r.status != 200:
            raise RuntimeError(f"app token {r.status}: {str(body)[:200]}")
    _APP_TOKEN["token"] = body["access_token"]
    _APP_TOKEN["exp"] = now + int(body.get("expires_in", 3600)) - 120
    return _APP_TOKEN["token"]


async def _valid_access_token(session):
    if app_only():
        return await _app_access_token(session)
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
    if app_only():
        return _mailbox()
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
    data = await _get(session, f"{_base()}/mailFolders/{folder}/messages", params)
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
    """One full message: recipients + HTML/text body + file-attachment list (metadata only)."""
    params = {"$select": "subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments"}
    m = await _get(session, f"{_base()}/messages/{msg_id}", params)
    body = m.get("body", {})
    atts = []
    if m.get("hasAttachments"):
        try:
            ad = await _get(session, f"{_base()}/messages/{msg_id}/attachments",
                            {"$select": "id,name,contentType,size,isInline"})
            for a in ad.get("value", []):
                if a.get("isInline"):          # inline images belong to the body, not the clip list
                    continue
                atts.append({"id": a.get("id"), "name": a.get("name") or "attachment",
                             "contentType": a.get("contentType") or "", "size": a.get("size") or 0})
        except Exception:
            pass
    return {
        "id": m.get("id"),
        "subject": m.get("subject") or "(no subject)",
        "from": _addr(m.get("from")),
        "to": [_addr(x) for x in m.get("toRecipients", [])],
        "cc": [_addr(x) for x in m.get("ccRecipients", [])],
        "received": m.get("receivedDateTime"),
        "bodyType": body.get("contentType", "text"),
        "body": body.get("content", ""),
        "attachments": atts,
    }


async def get_attachment(session, msg_id, att_id):
    """Download one file attachment -> (filename, content_type, raw_bytes)."""
    import base64
    a = await _get(session, f"{_base()}/messages/{msg_id}/attachments/{att_id}")
    return (a.get("name") or "attachment",
            a.get("contentType") or "application/octet-stream",
            base64.b64decode(a.get("contentBytes") or ""))


async def mark_read(session, msg_id, read=True):
    """Flag a message read/unread (Graph GET doesn't auto-mark-read like Outlook does)."""
    token = await _valid_access_token(session)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", **UA}
    async with session.patch(f"{GRAPH}{_base()}/messages/{msg_id}", json={"isRead": bool(read)},
                             headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            raise RuntimeError(f"mark_read {r.status}: {(await r.text())[:120]}")
    return True


async def unread_count(session):
    """Count of unread inbox messages (for the nav badge)."""
    data = await _get(session, f"{_base()}/mailFolders/inbox",
                      {"$select": "unreadItemCount"})
    return int(data.get("unreadItemCount") or 0)


async def forward_message(session, msg_id, to, comment=""):
    """Forward a message (Graph's native forward re-attaches the original's files)."""
    def rcpts(v):
        if isinstance(v, str):
            v = [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
        return [{"emailAddress": {"address": x}} for x in (v or [])]
    payload = {"comment": comment or "", "toRecipients": rcpts(to)}
    token = await _valid_access_token(session)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", **UA}
    async with session.post(f"{GRAPH}{_base()}/messages/{msg_id}/forward", json=payload,
                            headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status not in (200, 202):
            raise RuntimeError(f"forward {r.status}: {(await r.text())[:200]}")
    return True


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
    async with session.post(f"{GRAPH}{_base()}/sendMail", json=payload, headers=headers,
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
        if cmd == "set-config":
            cid = input("Client (app) ID  [the GUID, e.g. 1234abcd-...]: ").strip()
            tid = (input("Tenant [kesslercompanies.com]: ").strip()
                   or "kesslercompanies.com")
            mbx = (input("Mailbox [rkessler@kesslercompanies.com]: ").strip()
                   or "rkessler@kesslercompanies.com")
            if "~" in cid or "@" in cid:
                print("\n⚠ That doesn't look like a client ID — it should be a GUID "
                      "(no '~', no '@'). The '~' string is the client SECRET; set that "
                      "with `set-secret`, not here. Aborting.")
                return
            CRED_FILE.write_text(f"client_id={cid}\ntenant={tid}\nmailbox={mbx}\n")
            try:
                os.chmod(CRED_FILE, 0o600)
            except Exception:
                pass
            print("✓ wrote config to", CRED_FILE, "(no secret in here)")
            return
        if cmd == "set-secret":
            import getpass
            sec = getpass.getpass("Paste the client SECRET VALUE (hidden input): ").strip()
            if not sec:
                print("nothing entered — aborted.")
                return
            try:
                keyring_set_secret(sec)
                print("✓ secret stored in the OS keychain (service", KEYRING_SERVICE + ")")
            except Exception as e:
                print("✗ keychain store failed:", e)
                print("  Install the keychain support: pip install keyring pywin32-ctypes")
            return
        if cmd == "clear-secret":
            keyring_clear_secret()
            print("✓ secret removed from the OS keychain")
            return
        if cmd == "login":
            if app_only():
                print("App-only mode is configured (client secret + mailbox) — no sign-in needed.")
                print("Target mailbox:", _mailbox())
                print("Verifying access…")
                try:
                    msgs = await list_messages(s, top=3)
                    print(f"✓ connected — {len(msgs)} recent message(s) readable.")
                    for m in msgs:
                        frm = (m["from"]["name"] or m["from"]["email"] or "?")[:28]
                        print(f"   {m['received'][:16]}  {frm:28}  {m['subject'][:46]}")
                except Exception as e:
                    print("✗ could not read the mailbox:", e)
                    print("  If this says 'Access denied'/insufficient privileges, IT must grant")
                    print("  APPLICATION permissions Mail.Read + Mail.Send (not delegated) and")
                    print("  admin-consent them for this app.")
                return
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
