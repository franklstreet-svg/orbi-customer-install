"""
orbi_window — native desktop window wrapper for the dashboard.

Replaces Chrome `--app=URL` mode (which still looks/behaves like a browser:
refresh shortcuts, right-click "Reload", URL hidden but everything else
present) with a real WebView2 window. The customer sees a Windows program
with the Orbi icon, no browser tells.

Implementation: pywebview wraps the bundled Edge WebView2 runtime that ships
with every Windows 10/11. Tiny dependency (~2 MB), no Chromium re-download.

Usage:
    python orbi_window.py [--port 5050]

What it does:
    1. Waits up to 30s for orbi.py to be listening on the given port.
    2. Opens a single native window pointed at http://localhost:<port>/owner/login
    3. Sets the window title to "Orbi" and the taskbar icon (when bundled).
    4. Blocks until the user closes the window. Orbi.py keeps running
       in the background — closing the window does NOT stop the service.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import urllib.request
import urllib.error

log = logging.getLogger("orbi.window")

DEFAULT_PORT = 5050
HEALTH_TIMEOUT_SECONDS = 30
WINDOW_TITLE = "Orbi"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 820


def _wait_for_health(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass
        time.sleep(1.0)
    return False


def _resolve_icon() -> Optional[str]:
    """Locate the bundled .ico for the window. install_runtime.py ships
    icons/orbi.ico (when present). Falls back to None — pywebview shows
    its default in that case."""
    candidates = [
        Path(__file__).resolve().parent / "icons" / "orbi.ico",
        Path(__file__).resolve().parent / "static" / "orbi.ico",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def open_window(port: int = DEFAULT_PORT,
                 path: str = "/owner/login") -> int:
    """Open the dashboard in a native window. Returns the process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("waiting up to %ds for orbi service on port %d",
              HEALTH_TIMEOUT_SECONDS, port)
    healthy = _wait_for_health(port, HEALTH_TIMEOUT_SECONDS)
    if not healthy:
        log.warning("orbi did not respond on /health in time — opening anyway "
                    "(user may see a refresh prompt)")
    url = f"http://127.0.0.1:{port}{path}"

    try:
        import webview
    except ImportError:
        # pywebview missing — fall back to opening in the default browser.
        # Better to land the customer SOMEWHERE than nowhere.
        log.warning("pywebview not installed — falling back to default browser")
        import webbrowser
        webbrowser.open(url, new=2)
        return 0

    icon = _resolve_icon()
    create_kwargs: dict = {
        "title":      WINDOW_TITLE,
        "url":        url,
        "width":      WINDOW_WIDTH,
        "height":     WINDOW_HEIGHT,
        "min_size":   (900, 600),
        "resizable":  True,
        "text_select": True,
    }
    # Newer pywebview on Windows uses gui="edgechromium" by default
    # (WebView2). On Mac it's WebKit. On Linux it's GTK/WebKit2. We let
    # pywebview pick — it's always the native option for the host OS.

    log.info("opening window: %s", url)
    window = webview.create_window(**create_kwargs)

    # Set the icon AFTER window creation when pywebview supports it.
    # If not supported on this platform, this is a no-op.
    start_kwargs: dict = {"debug": False}
    if icon and os.name == "nt":
        # On Windows we can pass icon via the start() call.
        start_kwargs["icon"] = icon

    try:
        webview.start(**start_kwargs)
    except TypeError:
        # Older pywebview signatures may not accept `icon` — retry without it.
        webview.start(debug=False)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Orbi native window launcher")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                         help="orbi.py port (default 5050)")
    parser.add_argument("--path", default="/owner/login",
                         help="initial path inside Orbi (default /owner/login)")
    args = parser.parse_args()
    return open_window(args.port, args.path)


if __name__ == "__main__":
    sys.exit(main())
