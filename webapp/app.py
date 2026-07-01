"""Desktop launcher for the Kessler-Katznelson web terminal.

Runs the aiohttp web app on a background thread and shows it in a native window
(pywebview). If no native webview is available — e.g. an old Windows box with no
Edge WebView2 runtime — it falls back to the default browser, so the same
launcher works everywhere.

    ./.venv/bin/python webapp/app.py
"""
import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))            # so `import server` works

import server                            # noqa: E402
from aiohttp import web                  # noqa: E402

HOST, PREFERRED_PORT = "127.0.0.1", 8787
_ready = threading.Event()
_port = {"value": PREFERRED_PORT}


def _pick_port():
    """Prefer 8787; if it's taken, let the OS hand us a free one."""
    for port in (PREFERRED_PORT, 0):
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((HOST, port))
            p = s.getsockname()[1]
            s.close()
            return p
        except OSError:
            continue
    return PREFERRED_PORT


def _serve():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    port = _pick_port()
    _port["value"] = port
    runner = web.AppRunner(server.make_app())
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, HOST, port).start())
    _ready.set()
    loop.run_forever()


def main():
    threading.Thread(target=_serve, daemon=True).start()
    _ready.wait(timeout=15)
    url = f"http://{HOST}:{_port['value']}"
    try:
        import webview
        webview.create_window("Kessler-Katznelson Terminal", url,
                              width=1600, height=1000, min_size=(1100, 700))
        if sys.platform.startswith("win"):
            webview.start(gui="edgechromium")  # require modern WebView2; raises if absent
        else:
            webview.start()              # blocks until the window is closed
    except Exception as e:               # no modern webview -> use the default browser
        print(f"native window unavailable ({e}); opening browser at {url}", flush=True)
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
