"""Self-updater for Kessler Terminal.

Runs (by the launcher) BEFORE the app starts. Checks the public repo for a newer
code version and, if found, downloads the updated code files into this folder.
It only ever writes the bare filenames listed in version.json — it never touches
local data (portfolio.json, .fred_key, transactions.json) or anything outside
this folder. Fails silent/offline-safe: if the check fails, the app just runs
the code it already has.
"""
import json
import os
import ssl
import sys
import urllib.request

REPO = "nissanezra/kessler-terminal"
RAW_MAIN = f"https://raw.githubusercontent.com/{REPO}/main/"
API_COMMIT = f"https://api.github.com/repos/{REPO}/commits/main"
HERE = os.path.dirname(os.path.abspath(__file__))
VFILE = os.path.join(HERE, ".appversion")


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "kessler-terminal-updater"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        return r.read()


def _local_version():
    try:
        return int(open(VFILE).read().strip())
    except Exception:
        return 0


def _resolve_base():
    """Freshness-safe base URL for fetching repo files.

    GitHub's raw CDN caches the branch HEAD for ~5 min, so a just-pushed update
    can keep looking 'up to date' for several minutes (raw ignores cache-busting
    query strings and no-cache headers). Resolve the current commit SHA via the
    API and pin raw URLs to it — SHA URLs are immutable, so they're never stale.
    Falls back to the (possibly cached) branch URL if the API is unreachable."""
    try:
        sha = json.loads(_get(API_COMMIT)).get("sha")
        if sha:
            return f"https://raw.githubusercontent.com/{REPO}/{sha}/"
    except Exception as e:
        print(f"  update: SHA resolve failed, using branch — {e}")
    return RAW_MAIN


def _ensure_wezterm_config():
    """Force WezTerm to software rendering so chart images composite reliably on
    machines whose GPU/driver renders text but leaves graphics (sixel) blank.
    Writes ~/.wezterm.lua only if absent or previously written by us."""
    cfg = os.path.join(os.path.expanduser("~"), ".wezterm.lua")
    marker = "-- kessler-terminal auto-config (do not remove this line)"
    body = (marker + "\nreturn {\n"
            "  front_end = 'Software',\n"
            "  enable_kitty_graphics = true,\n"
            "  max_fps = 30,\n}\n")
    try:
        if os.path.exists(cfg):
            cur = open(cfg, encoding="utf-8", errors="ignore").read()
            if marker not in cur:
                return                       # user's own config — leave it alone
            if cur.strip() == body.strip():
                return                       # already current
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(body)
        print("  update: applied WezTerm software-rendering config")
    except Exception as e:
        print(f"  update: wezterm config skipped — {e}")


def _ensure_deps():
    """Make sure newer dependencies are present (auto-deploy without a reinstall)."""
    try:
        import plotext  # noqa: F401
    except Exception:
        print("  update: installing chart library (plotext)…")
        try:
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "plotext"],
                           timeout=120)
        except Exception as e:
            print(f"  update: plotext install skipped — {e}")
    # Web terminal: a native window on Windows needs pywebview (Edge WebView2).
    # Best-effort — if it won't install, the web app just opens in the browser.
    if os.name == "nt":
        try:
            import webview  # noqa: F401
        except Exception:
            print("  update: installing native-window support (pywebview)…")
            try:
                import subprocess
                subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pywebview"],
                               timeout=180)
            except Exception as e:
                print(f"  update: pywebview install skipped — {e}")


def _ensure_greeting():
    """Set the splash greeting name once on Windows (Robert's machines). Never
    overwrites a name already set locally (webapp/greeting.txt or MKT_USER)."""
    if os.name != "nt":
        return
    path = os.path.join(HERE, "webapp", "greeting.txt")
    if os.path.exists(path):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("Robert")
    except Exception:
        pass


def main():
    _ensure_wezterm_config()                 # every launch: keep the render config in place
    _ensure_deps()
    _ensure_greeting()
    base = _resolve_base()                    # SHA-pinned when possible, never stale
    try:
        manifest = json.loads(_get(base + "version.json"))
    except Exception as e:
        print(f"  update: skipped (offline?) — {e}")
        return
    remote = int(manifest.get("version", 0))
    local = _local_version()

    def _safe(fn):
        # relative paths only — no absolute, no ".." traversal, no hidden
        # files/dirs (protects .fred_key/.appversion/portfolio.json). Subfolders OK.
        parts = fn.replace("\\", "/").split("/")
        return not (not fn or fn.startswith(("/", "\\")) or ".." in parts
                    or "" in parts or any(p.startswith(".") for p in parts))

    safe = [fn for fn in manifest.get("files", []) if _safe(fn)]
    missing = [fn for fn in safe
               if not os.path.exists(os.path.join(HERE, *fn.split("/")))]

    if remote > local:
        targets, bump = safe, True                # new version: refresh everything
        print(f"  update: v{local} -> v{remote}, downloading…")
    elif missing:
        # same version, but files an older updater couldn't fetch (e.g. a new
        # subfolder) are missing — self-heal without bumping the version.
        targets, bump = missing, False
        print(f"  update: fetching {len(missing)} missing file(s)…")
    else:
        print(f"  update: up to date (v{local})")
        return

    ok = True
    for fn in targets:
        dst = os.path.join(HERE, *fn.split("/"))
        try:
            data = _get(base + "/".join(fn.split("/")))
            os.makedirs(os.path.dirname(dst) or HERE, exist_ok=True)
            tmp = dst + ".new"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)   # atomic swap
            print(f"    ✓ {fn}")
        except Exception as e:
            print(f"    ✗ {fn} — {e}")
            ok = False
    if ok and bump:
        with open(VFILE, "w") as f:
            f.write(str(remote))
    print("  update: done." if ok else "  update: some failed — will retry next launch.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"  update: error — {e}", file=sys.stderr)
