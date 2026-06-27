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

REPO_BASE = "https://raw.githubusercontent.com/nissanezra/kessler-terminal/main/"
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


def main():
    try:
        manifest = json.loads(_get(REPO_BASE + "version.json"))
    except Exception as e:
        print(f"  update: skipped (offline?) — {e}")
        return
    remote = int(manifest.get("version", 0))
    local = _local_version()
    if remote <= local:
        print(f"  update: up to date (v{local})")
        return
    print(f"  update: v{local} -> v{remote}, downloading…")
    ok = True
    for fn in manifest.get("files", []):
        if not fn or "/" in fn or "\\" in fn or fn.startswith("."):  # safety: bare names only
            continue
        try:
            data = _get(REPO_BASE + fn)
            tmp = os.path.join(HERE, fn + ".new")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(HERE, fn))   # atomic swap
            print(f"    ✓ {fn}")
        except Exception as e:
            print(f"    ✗ {fn} — {e}")
            ok = False
    if ok:
        with open(VFILE, "w") as f:
            f.write(str(remote))
        print(f"  update: done (now v{remote}).")
    else:
        print("  update: some files failed — keeping current version.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"  update: error — {e}", file=sys.stderr)
