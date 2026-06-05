"""Shared Playwright helpers — browser lifecycle, error screenshots,
slow-motion for demo mode."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("orbi.web_driver")

# Where error screenshots land (for post-mortem when a driver fails)
SCREENSHOT_DIR_NAME = "web_driver_screenshots"


def screenshot_dir(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        data_dir = Path("data")
    p = Path(data_dir) / SCREENSHOT_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def launch_browser(headless: bool = True, slow_mo_ms: int = 0,
                    viewport: dict | None = None,
                    record_video_dir: Path | None = None):
    """Yield (browser, page) + tracks the video path on the context if
    recording is enabled. Cleans up on exit.

    headless=True for production phone-order flow (invisible).
    headless=False for live demo (visible cursor — needs a display).
    slow_mo_ms slows each action by N ms — used for both visible demo
    AND recorded video so the cursor click is readable in the playback.
    record_video_dir → Playwright records the session as a webm/mp4
    inside this folder; the actual filename is on page.video.path()
    after the context closes."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            slow_mo=slow_mo_ms,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx_kwargs = {
            "viewport": viewport or {"width": 1280, "height": 900},
            "user_agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
        }
        if record_video_dir is not None:
            Path(record_video_dir).mkdir(parents=True, exist_ok=True)
            ctx_kwargs["record_video_dir"] = str(record_video_dir)
            ctx_kwargs["record_video_size"] = ctx_kwargs["viewport"]
        try:
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            page.set_default_timeout(15_000)
            yield browser, page
            # Close the context BEFORE the browser so the video file
            # finalizes — Playwright only writes the .webm on context.close()
            try:
                context.close()
            except Exception:
                pass
        finally:
            try:
                browser.close()
            except Exception:
                pass


def capture_failure(page, data_dir: Path | None, label: str) -> str:
    """Save a screenshot on failure so we can see what went wrong.
    Returns the path string for the SMS / API response."""
    try:
        out = screenshot_dir(data_dir) / f"{label}_{int(time.time())}.png"
        page.screenshot(path=str(out), full_page=True)
        return str(out)
    except Exception as e:
        log.warning(f"failure screenshot failed: {e}")
        return ""
