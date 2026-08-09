"""Rosenberg Research auto-download for Kessler Terminal.

Logs into the subscriber's OWN Rosenberg Research account (app.rosenbergresearch.com)
and downloads the latest report PDFs into this app's `research/` folder, where the
terminal's Research page already lists them. Runs on the subscriber's own machine
with their own credentials — nothing is shared and nothing leaves the machine.

Auth is AWS Cognito SRP (the same handshake the website's login uses), implemented
here with the Python standard library only — no extra packages, no browser.

Usage:
    python rosenberg.py setup     # enter your Rosenberg email + password once (stored locally)
    python rosenberg.py sync      # download any new reports into research/
    python rosenberg.py forget    # remove the stored credentials

The credential is kept only on THIS machine in a local file the updater never
touches. It is lightly obfuscated (not strong encryption) — treat this machine as
you would any that remembers your logins.
"""
import base64
import binascii
import datetime
import getpass
import hashlib
import hmac
import json
import os
import platform
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH_DIR = os.path.join(HERE, "research")
CRED_FILE = os.path.join(HERE, ".rosenberg_cred")     # dotfile: updater won't overwrite it
SKIP_FILE = os.path.join(HERE, ".rosenberg_skip")     # marker: user chose to skip setup

# ---- Cognito app (read from the Rosenberg web app's config) ----------------
COGNITO_REGION = "us-east-1"
USER_POOL_ID = "us-east-1_UOkJuvFrR"
CLIENT_ID = "4mrhq89c78hivorknhj7uvt7jv"
POOL_NAME = USER_POOL_ID.split("_")[1]                # SRP uses the pool id sans region
COGNITO_URL = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
API_BASE = "https://app.rosenbergresearch.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 3072-bit SRP group (RFC 5054), as used by Amazon Cognito.
_N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
    "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
    "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31"
    "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF")
_G_HEX = "2"
_INFO_BITS = b"Caldera Derived Key"

_WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---- SRP helpers (standard Cognito SRP math) -------------------------------
def _hash_sha256(buf):
    return hashlib.sha256(buf).hexdigest()


def _hex_hash(hex_string):
    return _hash_sha256(bytes.fromhex(hex_string))


def _hex_to_long(h):
    return int(h, 16)


def _long_to_hex(val):
    return "%x" % val


def _pad_hex(long_int):
    hash_str = long_int if isinstance(long_int, str) else _long_to_hex(long_int)
    if len(hash_str) % 2 == 1:
        hash_str = "0" + hash_str
    elif hash_str[0] in "89abcdefABCDEF":
        hash_str = "00" + hash_str
    return hash_str


def _calculate_u(big_a, big_b):
    return _hex_to_long(_hex_hash(_pad_hex(big_a) + _pad_hex(big_b)))


def _compute_hkdf(ikm, salt):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, _INFO_BITS + bytes([1]), hashlib.sha256).digest()[:16]


def _now_string():
    now = datetime.datetime.utcnow()
    return "%s %s %d %02d:%02d:%02d UTC %d" % (
        _WEEKDAYS[now.isoweekday() % 7], _MONTHS[now.month], now.day,
        now.hour, now.minute, now.second, now.year)


class _SRP:
    def __init__(self):
        self.big_n = _hex_to_long(_N_HEX)
        self.g = _hex_to_long(_G_HEX)
        self.k = _hex_to_long(_hex_hash("00" + _N_HEX + "0" + _G_HEX))
        self.small_a = self._gen_small_a()
        self.large_a = pow(self.g, self.small_a, self.big_n)
        if self.large_a % self.big_n == 0:
            raise ValueError("SRP A illegal (A mod N == 0)")

    def _gen_small_a(self):
        rnd = _hex_to_long(binascii.hexlify(os.urandom(128)).decode())
        return rnd % self.big_n

    def password_key(self, username, password, server_b, salt):
        u_value = _calculate_u(self.large_a, server_b)
        if u_value == 0:
            raise ValueError("SRP U == 0")
        up = "%s%s:%s" % (POOL_NAME, username, password)
        up_hash = _hash_sha256(up.encode("utf-8"))
        x_value = _hex_to_long(_hex_hash(_pad_hex(salt) + up_hash))
        g_mod = pow(self.g, x_value, self.big_n)
        int2 = server_b - self.k * g_mod
        s_value = pow(int2, self.small_a + u_value * x_value, self.big_n)
        return _compute_hkdf(bytearray.fromhex(_pad_hex(_long_to_hex(s_value))),
                             bytearray.fromhex(_pad_hex(_long_to_hex(u_value))))


def _cognito(target, payload):
    req = urllib.request.Request(
        COGNITO_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/x-amz-json-1.1",
                 "X-Amz-Target": "AWSCognitoIdentityProviderService." + target,
                 "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("message", body)
        except Exception:
            msg = body
        raise RuntimeError(f"Cognito {target} failed: {msg}")


def login(email, password):
    """Return (id_token, access_token) via the Cognito USER_SRP_AUTH flow."""
    srp = _SRP()
    init = _cognito("InitiateAuth", {
        "AuthFlow": "USER_SRP_AUTH", "ClientId": CLIENT_ID,
        "AuthParameters": {"USERNAME": email, "SRP_A": _long_to_hex(srp.large_a)}})
    cp = init.get("ChallengeParameters", {})
    salt, srp_b = cp["SALT"], cp["SRP_B"]
    secret_block, user_id = cp["SECRET_BLOCK"], cp["USER_ID_FOR_SRP"]
    timestamp = _now_string()
    hkdf = srp.password_key(user_id, password, _hex_to_long(srp_b), salt)
    msg = (POOL_NAME.encode("utf-8") + user_id.encode("utf-8")
           + base64.standard_b64decode(secret_block) + timestamp.encode("utf-8"))
    signature = base64.standard_b64encode(
        hmac.new(hkdf, msg, hashlib.sha256).digest()).decode("utf-8")
    resp = _cognito("RespondToAuthChallenge", {
        "ClientId": CLIENT_ID, "ChallengeName": "PASSWORD_VERIFIER",
        "ChallengeResponses": {"USERNAME": user_id,
                               "PASSWORD_CLAIM_SECRET_BLOCK": secret_block,
                               "TIMESTAMP": timestamp,
                               "PASSWORD_CLAIM_SIGNATURE": signature}})
    res = resp.get("AuthenticationResult")
    if not res:
        raise RuntimeError(f"Login returned no tokens (challenge: {resp.get('ChallengeName')})")
    return res["IdToken"], res["AccessToken"]


# ---- Rosenberg REST API ----------------------------------------------------
def _api(path, token, method="GET", body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API_BASE + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + token,
                                          "User-Agent": UA, "Accept": "*/*"})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        blob = r.read()
        ctype = r.headers.get("Content-Type", "")
    return (blob, ctype) if raw else json.loads(blob)


def _walk_publications(obj, out):
    """Collect report items (id + title + date + bucketId) from an unknown-shaped response."""
    if isinstance(obj, dict):
        pid = obj.get("id")
        title = obj.get("title") or obj.get("name")
        date = (obj.get("publicationDate") or obj.get("date")
                or obj.get("publishedAt") or obj.get("createdAt"))
        if isinstance(pid, str) and title and date:
            out.append({"id": pid, "title": title, "date": str(date)[:10],
                        "bucketId": obj.get("bucketId")})
        for v in obj.values():
            _walk_publications(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_publications(v, out)
    return out


# Rosenberg's publication sections — bucketId + featureIds taken straight from each
# portal "publications-list" URL. `code` prefixes the saved filenames so the terminal
# can group them into one section per category; `keep` = how many newest to hold.
ROSENBERG_FEEDS = [
    {"code": "daily", "name": "Daily Reports",
     "bucket": "08dcb156-1d29-4e4b-8e18-7640bf04f20d",
     "features": ["18e77d4b-7399-4453-b978-4b500f1deb55",     # Breakfast with Dave
                  "08dce2fe-d146-478f-83eb-17ed9552eb64"],    # Early Morning with Dave
     "keep": 10},
    {"code": "macro", "name": "Macro Research",
     "bucket": "08dcb188-f40e-40bf-8e74-af88ab237f7c",
     "features": ["2290f675-fc5d-452e-aca0-acd6455fa76c",
                  "59b8bb4e-9c55-4474-8be7-b1b8075107dc",
                  "08dce95d-8707-4b36-815f-bd78a65b0923",
                  "08dcb185-f704-442d-8ea4-562138074ae6"], "keep": 6},
    {"code": "strategy", "name": "Market Strategy",
     "bucket": "08dcb189-327b-48ab-8243-f1c8b4f306f3",
     "features": ["08dd4ba9-a87e-428f-8ca8-7a1fc39ced79",
                  "08dd4ba9-dd15-4120-8611-351228d688a4",
                  "7f7ca3f4-d4a9-4b56-9a74-f724ddf9a6ba"], "keep": 6},
    {"code": "strategizer", "name": "Strategizer Report",
     "bucket": "08dcb189-edd2-435c-86f2-7a9aa080a0c4", "features": [], "keep": 4},
    {"code": "special", "name": "Special Reports",
     "bucket": "08dcb189-1348-4872-83d0-3160af93cac8", "features": [], "keep": 5},
    {"code": "webcasts", "name": "Webcasts & Multimedia",
     "bucket": "08dcb189-c1f7-4d14-83b1-c7be65a3368a",
     "features": ["1507dad1-6f8e-4f63-a728-45e224ddcd1d"], "time": "Past", "keep": 4},
    {"code": "chartroom", "name": "Investor Chartroom",
     "bucket": "08dcb189-d662-4057-8339-e3fd5575a943", "features": [], "keep": 4},
    {"code": "models", "name": "Proprietary Models",
     "bucket": "08dcb18a-09c6-4761-8021-0282eed31fb3", "features": [], "keep": 6},
]


def list_section(token, feed, limit=15):
    """Newest publications in one section via the portal's details/search endpoint
    (keyset pagination, date-sorted). Filters by bucketId + featureIds (+ time)."""
    filt = {"bucketId": feed["bucket"], "search": "", "hideLockedContent": False}
    if feed.get("features"):
        filt["featureIds"] = feed["features"]
    if feed.get("time"):
        filt["time"] = feed["time"]
    # /search (not /details/search): the plain endpoint returns PDF-only publications
    # too (e.g. Breakfast with Dave), which details/search silently omits.
    data = _api("/api/v3/publications/search", token, "POST",
                {"pagination": {"limit": limit}, "filter": filt})
    items, seen = [], set()
    for it in _walk_publications(data, []):
        if it["id"] not in seen:
            seen.add(it["id"])
            items.append(it)
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


def download_pdf(token, pub_id, dest_path):
    blob, ctype = _api(f"/api/v3/publications/{pub_id}/pdf", token, raw=True)
    if blob[:4] != b"%PDF":                       # endpoint may return a signed URL instead
        url = None
        if "json" in ctype.lower():
            try:
                url = (json.loads(blob).get("url") or json.loads(blob).get("downloadUrl"))
            except Exception:
                url = None
        if not url:
            m = re.search(rb'https?://[^"\']+', blob[:2000])
            url = m.group(0).decode() if m else None
        if not url:
            raise RuntimeError("PDF endpoint returned neither a PDF nor a URL")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                    timeout=60) as r:
            blob = r.read()
    tmp = dest_path + ".part"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, dest_path)


# ---- credential storage (local only, obfuscated) ---------------------------
def _machine_key():
    return hashlib.sha256(("kkt-rr::" + platform.node()).encode()).digest()


def _xor(data):
    key = _machine_key()
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def save_creds(email, password):
    blob = json.dumps({"e": email, "p": password}).encode()
    with open(CRED_FILE, "wb") as f:
        f.write(base64.b64encode(_xor(blob)))


def load_creds():
    # cloud (Fly) supplies the login as env secrets; the desktop build uses the local file
    env_e, env_p = os.environ.get("ROSENBERG_EMAIL"), os.environ.get("ROSENBERG_PASSWORD")
    if env_e and env_p:
        return env_e, env_p
    if not os.path.exists(CRED_FILE):
        return None
    try:
        raw = base64.b64decode(open(CRED_FILE, "rb").read())
        d = json.loads(_xor(raw).decode())
        return d["e"], d["p"]
    except Exception:
        return None


def has_creds():
    return load_creds() is not None


# ---- filename + sync -------------------------------------------------------
def _safe_name(title, date):
    base = re.sub(r"[^\w .-]", "", title).strip().replace(" ", "_")[:80] or "report"
    return f"{base}_{date}.pdf"


# Sidecar mapping saved filename -> the report's real headline (with punctuation the
# filename can't hold), so the terminal can show clean titles instead of the filename.
TITLES_FILE = os.path.join(RESEARCH_DIR, ".rr_titles.json")


def _load_titles():
    try:
        return json.load(open(TITLES_FILE, encoding="utf-8"))
    except Exception:
        return {}


def _save_titles(titles):
    try:
        with open(TITLES_FILE, "w", encoding="utf-8") as f:
            json.dump(titles, f, ensure_ascii=False)
    except Exception:
        pass


def _export_originals(titles, quiet=False):
    """Windows only (Robert): mirror the downloaded report PDFs into a friendly
    'Rosenberg Reports' folder on the Desktop, organized by section with readable
    names, so the ORIGINAL files can be opened/printed outside the terminal."""
    if os.name != "nt":
        return
    import shutil, re as _re
    section_names = {f["code"]: f["name"] for f in ROSENBERG_FEEDS}
    home = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")
    outroot = os.path.join(desktop if os.path.isdir(desktop) else home, "Rosenberg Reports")
    n = 0
    try:
        files = os.listdir(RESEARCH_DIR)
    except Exception:
        return
    for fn in files:
        if not fn.startswith("rr_") or not fn.lower().endswith(".pdf"):
            continue
        m = _re.match(r"rr_([a-z]+)__", fn)
        code = m.group(1) if m else ""
        section = section_names.get(code, "Reports")
        dm = _re.search(r"(\d{4}-\d{2}-\d{2})", fn)
        date = dm.group(1) if dm else ""
        title = titles.get(fn) or fn[len(f"rr_{code}__"):-4].replace("_", " ")
        title = _re.sub(r'[<>:"/\\|?*]', " ", title).strip()[:120]
        outname = (title + (f" {date}" if date and date not in title else "") + ".pdf").strip()
        outdir = os.path.join(outroot, section)
        dst = os.path.join(outdir, outname)
        if os.path.exists(dst):
            continue
        try:
            os.makedirs(outdir, exist_ok=True)
            shutil.copy2(os.path.join(RESEARCH_DIR, fn), dst)
            n += 1
        except Exception:
            pass
    if n and not quiet:
        print(f"  rosenberg: exported {n} original PDF(s) to Desktop\\Rosenberg Reports")


def sync(token=None, quiet=False):
    """Download the newest reports from every Rosenberg section not already saved.
    Filenames are prefixed `rr_<code>__` so the terminal groups them by section.
    Returns the number of new files downloaded."""
    def say(m):
        if not quiet:
            print(m)
    if token is None:
        creds = load_creds()
        if not creds:
            say("  rosenberg: no login saved — run `python rosenberg.py setup`")
            return 0
        say("  rosenberg: signing in…")
        token, _ = login(*creds)
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    existing = set(os.listdir(RESEARCH_DIR))
    titles = _load_titles()                          # filename -> original clean title
    got = 0
    for feed in ROSENBERG_FEEDS:
        try:
            pubs = list_section(token, feed)[:feed.get("keep", 6)]
        except Exception as e:
            say(f"  rosenberg: {feed['name']} — list failed: {e}")
            continue
        say(f"  rosenberg: {feed['name']}: {len(pubs)} newest")
        for p in pubs:
            name = f"rr_{feed['code']}__" + _safe_name(p["title"], p["date"])
            titles[name] = p["title"]                # keep the real headline for display
            if name in existing:
                continue
            try:
                download_pdf(token, p["id"], os.path.join(RESEARCH_DIR, name))
                existing.add(name)
                got += 1
                say(f"    + {name}")
            except Exception as e:
                say(f"    x {p['title'][:44]} — {e}")
    _save_titles(titles)
    _export_originals(titles, quiet=quiet)           # Windows: copy originals to the Desktop
    say(f"  rosenberg: {got} new report(s) downloaded" if got
        else "  rosenberg: up to date")
    return got


def interactive_setup():
    """Prompt once for the Rosenberg login, verify it, and save it locally."""
    print("\n  Rosenberg Research — connect your account (one time).")
    print("  Reports will then auto-download into your terminal each morning.")
    print("  Leave the email blank to skip.\n")
    try:
        email = input("  Rosenberg email: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not email:
        open(SKIP_FILE, "w").close()
        print("  Skipped. Delete .rosenberg_skip to be asked again.\n")
        return
    password = getpass.getpass("  Rosenberg password (hidden): ")
    print("  Checking…")
    try:
        token, _ = login(email, password)
    except Exception as e:
        print(f"  Login failed: {e}\n  Not saved — try again next launch.\n")
        return
    save_creds(email, password)
    _ensure_schedule()
    print("  Connected. Downloading your latest reports…")
    try:
        sync(token=token)
    except Exception as e:
        print(f"  (download will retry next launch — {e})")
    print()


def _ensure_schedule():
    """Register the download task. Runs every 3h from 09:00 so the ~8am reports are
    pulled shortly after release (and again through the day) even if the app is closed."""
    if os.name != "nt":
        return
    # marker bumped to _sched3 so existing installs re-create with the new time/cadence
    marker = os.path.join(HERE, ".rosenberg_sched3")
    if os.path.exists(marker):
        return
    try:
        import subprocess
        cmd = '"%s" "%s" sync' % (sys.executable, os.path.join(HERE, "rosenberg.py"))
        subprocess.run(["schtasks", "/Create", "/SC", "HOURLY", "/MO", "3",
                        "/TN", "KesslerRosenberg",
                        "/TR", cmd, "/ST", "09:00", "/F"],
                       capture_output=True, timeout=30)
        open(marker, "w").close()
    except Exception:
        pass


def auto():
    """Called by the updater at launch: prompt once, then keep reports fresh."""
    if os.name != "nt":            # only on the packaged (Windows) build
        return
    if has_creds():
        _ensure_schedule()         # self-heals if the task was never created
        try:
            sync(quiet=True)
        except Exception as e:
            print(f"  rosenberg: skipped — {e}")
    elif not os.path.exists(SKIP_FILE):
        interactive_setup()


def sync_cloud():
    """Cloud (Fly) entry point: login from env secrets, sync every section."""
    creds = load_creds()
    if not creds:
        return 0
    token, _ = login(*creds)
    return sync(token=token, quiet=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "setup":
        if os.path.exists(SKIP_FILE):
            os.remove(SKIP_FILE)
        interactive_setup()
    elif cmd == "forget":
        for f in (CRED_FILE, SKIP_FILE):
            if os.path.exists(f):
                os.remove(f)
        print("  rosenberg: stored login removed.")
    elif cmd == "diag":                              # newest per section (sanity check)
        tok, _ = login(*load_creds())
        for feed in ROSENBERG_FEEDS:
            s = list_section(tok, feed, 6)
            print(feed["name"], "->", [(p["date"], p["title"][:26]) for p in s[:3]])
    else:
        sync()
