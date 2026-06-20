"""
error_reporter — fire-and-forget customer error reporting back to the
central brain server. Closes the last fleet-health gap so Frank sees
customer crashes from his admin dashboard instead of waiting for the
customer to file a support ticket.

DESIGN:
- Fully optional: if the brain URL or api_key isn't configured, this
  module silently no-ops. Never raise from a reporting path — the
  reporter must never become the source of new crashes.
- Throttled: same exception class within 60s is dropped, so a runaway
  loop doesn't hammer the brain server.
- Truncated: each payload caps at 8KB. Big tracebacks get tail-truncated.
- Background thread: the actual HTTP POST runs in a daemon thread so
  the calling code (often a request handler) doesn't pay the latency.

USE:
    from error_reporter import report_exception
    try:
        ...
    except Exception as e:
        report_exception(e, context={"feature": "calendar_add"})
        raise
"""

from __future__ import annotations

import json
import logging
import platform
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("orbi.error_reporter")

# Per-process throttle: (error_class, last_sent_ts). Same class within
# 60s gets dropped to avoid flooding the brain server in a loop.
_THROTTLE: dict[str, float] = {}
_THROTTLE_WINDOW_SEC = 60.0
_THROTTLE_LOCK = threading.Lock()

# Cached config — populated on first use; orbi.py imports this module
# before its CONFIG dict is built, so we resolve lazily.
_CACHED_BRAIN_URL: str | None = None
_CACHED_API_KEY: str | None = None
_VERSION: str | None = None


def _resolve_config() -> tuple[str, str, str]:
    """Lazy-load brain URL + api_key from orbi.py's CONFIG. Returns
    ('', '', '') when not configured (caller will no-op)."""
    global _CACHED_BRAIN_URL, _CACHED_API_KEY, _VERSION
    if _CACHED_BRAIN_URL is not None:
        return _CACHED_BRAIN_URL, _CACHED_API_KEY or "", _VERSION or ""
    try:
        import orbi  # type: ignore
        cfg = getattr(orbi, "CONFIG", {}) or {}
    except Exception:
        cfg = {}
    brain = (cfg.get("brain") or {})
    _CACHED_BRAIN_URL = (brain.get("url") or "").rstrip("/")
    _CACHED_API_KEY = brain.get("api_key") or ""
    _VERSION = str(cfg.get("version", "")) or ""
    return _CACHED_BRAIN_URL, _CACHED_API_KEY, _VERSION


def _post_async(url: str, payload: dict) -> None:
    """Fire-and-forget POST in a daemon thread."""
    def _go():
        try:
            data = json.dumps(payload, default=str).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json",
                         "User-Agent": "Orby-ErrorReporter/0.1"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError):
            pass  # brain unreachable → swallow silently
        except Exception:
            pass  # never let the reporter raise
    threading.Thread(target=_go, daemon=True).start()


def report_exception(exc: BaseException, *,
                      context: dict | None = None,
                      request_url: str = "") -> None:
    """Report an exception to the brain. Safe to call from anywhere;
    silently no-ops if reporting isn't configured or the throttle
    matches. NEVER raises."""
    try:
        brain_url, api_key, version = _resolve_config()
        if not brain_url or not api_key:
            return  # not configured — silent no-op
        if "placeholder" in api_key.lower() or len(api_key) < 12:
            return
        err_class = type(exc).__name__
        now = time.time()
        with _THROTTLE_LOCK:
            last = _THROTTLE.get(err_class, 0)
            if now - last < _THROTTLE_WINDOW_SEC:
                return  # same class too recently — drop
            _THROTTLE[err_class] = now
        tb = "".join(traceback.format_exception(type(exc), exc,
                                                  exc.__traceback__))
        payload = {
            "error_class": err_class,
            "message":     str(exc)[:400],
            "traceback":   tb[-8000:],   # tail-truncate
            "context":     context or {},
            "version":     version,
            "platform":    f"{platform.system().lower()} "
                            f"{platform.release()}",
            "url":         request_url[:300],
        }
        _post_async(f"{brain_url}/api/error_report/{api_key}", payload)
    except Exception:
        # Reporter must NEVER raise. Swallow any new exception here.
        pass


def report_message(message: str, *,
                    error_class: str = "ManualReport",
                    context: dict | None = None) -> None:
    """Report a non-exception event (e.g., a deliberate warning the
    owner wants Frank to see). Same throttle + no-op rules."""
    try:
        brain_url, api_key, version = _resolve_config()
        if not brain_url or not api_key:
            return
        if "placeholder" in api_key.lower() or len(api_key) < 12:
            return
        now = time.time()
        with _THROTTLE_LOCK:
            last = _THROTTLE.get(error_class, 0)
            if now - last < _THROTTLE_WINDOW_SEC:
                return
            _THROTTLE[error_class] = now
        payload = {
            "error_class": error_class,
            "message":     (message or "")[:400],
            "traceback":   "",
            "context":     context or {},
            "version":     version,
            "platform":    f"{platform.system().lower()} "
                            f"{platform.release()}",
        }
        _post_async(f"{brain_url}/api/error_report/{api_key}", payload)
    except Exception:
        pass
