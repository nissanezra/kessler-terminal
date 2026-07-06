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
    """Collect report items (id + title + date) from an unknown-shaped response."""
    if isinstance(obj, dict):
        pid = obj.get("id")
        title = obj.get("title") or obj.get("name")
        date = (obj.get("publicationDate") or obj.get("date")
                or obj.get("publishedAt") or obj.get("createdAt"))
        if isinstance(pid, str) and title and date:
            out.append({"id": pid, "title": title, "date": str(date)[:10]})
        for v in obj.values():
            _walk_publications(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_publications(v, out)
    return out


def list_recent(token, limit=40):
    body = {"semanticRatio": 0,
            "pagination": {"page": 1, "limit": limit},
            "filter": {"search": "", "hideLockedContent": False}}
    data = _api("/api/v3/publications/search_fast", token, "POST", body)
    items, seen = [], set()
    for it in _walk_publications(data, []):
        if it["id"] not in seen:
            seen.add(it["id"])
            items.append(it)
    # newest first, regardless of the API's default ordering, so a report published
    # today is always at the top and never falls outside the downloaded set.
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


def sync(token=None, quiet=False, limit=25):
    """Download any recent reports not already saved. Returns count downloaded."""
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
    pubs = list_recent(token, limit)
    say(f"  rosenberg: {len(pubs)} recent reports listed")
    got = 0
    for p in pubs:
        name = _safe_name(p["title"], p["date"])
        if name in existing:
            continue
        try:
            download_pdf(token, p["id"], os.path.join(RESEARCH_DIR, name))
            existing.add(name)
            got += 1
            say(f"    + {name}")
        except Exception as e:
            say(f"    x {p['title'][:48]} — {e}")
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
    """Register a weekday-morning download task once (Windows Task Scheduler)."""
    if os.name != "nt":
        return
    marker = os.path.join(HERE, ".rosenberg_sched")
    if os.path.exists(marker):
        return
    try:
        import subprocess
        cmd = '"%s" "%s" sync' % (sys.executable, os.path.join(HERE, "rosenberg.py"))
        subprocess.run(["schtasks", "/Create", "/SC", "WEEKLY",
                        "/D", "MON,TUE,WED,THU,FRI", "/TN", "KesslerRosenberg",
                        "/TR", cmd, "/ST", "07:30", "/F"],
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
    else:
        sync()
