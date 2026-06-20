"""
actions — the structured command vocabulary the agent emits and executes.

Every browser interaction the controller decides to take is one of these
actions. The brain emits them as JSON (matching the same pattern as
site_scraper.llm_extract); the dispatcher executes them against the
shared Playwright BrowserContext.

Each action is a small dataclass-style dict so it's easy to log,
replay, or save into a recipe.

Action dict shape
-----------------
    {
        "type": "click" | "type" | "select" | "navigate" | "wait_for" |
                 "read" | "screenshot" | "scroll" | "submit" | "finish",
        # type-specific fields below:
        "selector":     str,    # CSS selector, ARIA selector, or accessibility ref
        "text":         str,    # for "type"
        "value":        str,    # for "select"
        "url":          str,    # for "navigate"
        "condition":    str,    # for "wait_for" — "visible" | "hidden" | "network_idle" | "url_contains:<frag>"
        "timeout_ms":   int,    # optional, default 10_000
        "scroll":       str,    # for "scroll" — "page_down" | "page_up" | "to_selector"
        "reason":       str,    # free-text explanation the brain gives
        "confirm":      bool,   # if True, surface to owner for approval before executing
    }
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("orbi.web_agent.actions")

DEFAULT_TIMEOUT_MS = 10_000

# Actions that require human confirmation by default, because executing
# them moves money, sends real messages, or dispatches real-world work.
# Recipes can override per-action via "confirm": false (e.g. for already
# customer-approved bulk operations).
_CONSEQUENTIAL_ACTION_TYPES = {
    "submit",       # form submission (could be payment, assign driver, etc.)
}

# Type-specific keywords inside `reason` that escalate any action to
# "needs confirmation" even if its type wouldn't normally require it.
_CONSEQUENTIAL_REASON_KEYWORDS = (
    "pay", "submit payment", "confirm order", "assign driver",
    "dispatch", "send message", "fire", "delete", "cancel order",
    "refund",
)


def needs_confirmation(action: dict) -> bool:
    """Decide whether this action should be gated behind owner approval
    before execution. Conservative — when in doubt, gate."""
    if action.get("confirm") is True:
        return True
    if action.get("confirm") is False:
        # Explicit recipe override — trust the recipe author.
        return False
    if action.get("type") in _CONSEQUENTIAL_ACTION_TYPES:
        return True
    reason = (action.get("reason") or "").lower()
    if any(kw in reason for kw in _CONSEQUENTIAL_REASON_KEYWORDS):
        return True
    return False


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class ActionError(Exception):
    """Raised when an action can't be executed (selector missing,
    timeout, navigation failure). The controller catches this and
    asks the brain to recover."""


def execute(page, action: dict, *, download_dir: Path | None = None) -> dict:
    """Execute one action against the live Playwright page.
    Returns a small result dict the controller adds to the action log.

    {
        "ok": bool,
        "type": str,
        "elapsed_ms": int,
        "result": Any,           # type-specific: text read, screenshot path, etc.
        "error": str | None,
        "downloaded": str | None,   # path to any file downloaded by this action
    }
    """
    t0 = time.time()
    atype = action.get("type")
    try:
        if atype == "navigate":
            return _do_navigate(page, action, t0)
        if atype == "click":
            return _do_click(page, action, t0)
        if atype == "type":
            return _do_type(page, action, t0)
        if atype == "select":
            return _do_select(page, action, t0)
        if atype == "wait_for":
            return _do_wait_for(page, action, t0)
        if atype == "read":
            return _do_read(page, action, t0)
        if atype == "screenshot":
            return _do_screenshot(page, action, t0, download_dir)
        if atype == "scroll":
            return _do_scroll(page, action, t0)
        if atype == "submit":
            return _do_submit(page, action, t0)
        if atype == "finish":
            # Marker action — controller stops the loop on this. Not an error.
            return _ok(atype, t0, result={"finished": True})
        raise ActionError(f"unknown action type: {atype!r}")
    except ActionError:
        raise
    except Exception as e:
        log.warning("action %s raised %s: %s", atype, type(e).__name__, e)
        raise ActionError(f"{atype} failed: {type(e).__name__}: {e}") from e


# ---------------------------------------------------------------------------
# Individual executors
# ---------------------------------------------------------------------------

def _ok(atype: str, t0: float, *, result: Any = None,
        downloaded: str | None = None) -> dict:
    return {
        "ok": True,
        "type": atype,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "result": result,
        "error": None,
        "downloaded": downloaded,
    }


def _timeout(action: dict) -> int:
    try:
        return int(action.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS


def _do_navigate(page, action, t0):
    url = (action.get("url") or "").strip()
    if not url:
        raise ActionError("navigate requires `url`")
    page.goto(url, timeout=_timeout(action), wait_until="domcontentloaded")
    return _ok("navigate", t0, result={"url": page.url})


def _do_click(page, action, t0):
    selector = (action.get("selector") or "").strip()
    if not selector:
        raise ActionError("click requires `selector`")
    page.click(selector, timeout=_timeout(action))
    return _ok("click", t0, result={"clicked": selector})


def _do_type(page, action, t0):
    selector = (action.get("selector") or "").strip()
    text = action.get("text", "")
    if not selector:
        raise ActionError("type requires `selector`")
    # `fill` clears the field first — that's almost always what you want
    # when an LLM emits a "type X here" action.
    page.fill(selector, str(text), timeout=_timeout(action))
    return _ok("type", t0, result={"selector": selector, "chars": len(str(text))})


def _do_select(page, action, t0):
    selector = (action.get("selector") or "").strip()
    value = action.get("value", "")
    if not selector:
        raise ActionError("select requires `selector`")
    page.select_option(selector, value=str(value), timeout=_timeout(action))
    return _ok("select", t0, result={"selector": selector, "value": value})


def _do_wait_for(page, action, t0):
    condition = (action.get("condition") or "").strip()
    if not condition:
        raise ActionError("wait_for requires `condition`")
    timeout = _timeout(action)
    if condition.startswith("url_contains:"):
        fragment = condition.split(":", 1)[1]
        page.wait_for_url(f"**{fragment}**", timeout=timeout)
    elif condition == "network_idle":
        page.wait_for_load_state("networkidle", timeout=timeout)
    elif condition in ("visible", "hidden"):
        selector = (action.get("selector") or "").strip()
        if not selector:
            raise ActionError("wait_for visible/hidden requires `selector`")
        page.wait_for_selector(selector, state=condition, timeout=timeout)
    else:
        raise ActionError(f"unknown wait_for condition: {condition}")
    return _ok("wait_for", t0, result={"condition": condition})


def _do_read(page, action, t0):
    selector = (action.get("selector") or "").strip()
    if not selector:
        # No selector → return the visible page text (capped) so the brain
        # can reason about what's on screen without a screenshot.
        text = page.inner_text("body", timeout=_timeout(action))
        return _ok("read", t0, result={"text": text[:10_000]})
    text = page.inner_text(selector, timeout=_timeout(action))
    return _ok("read", t0, result={"text": text[:10_000], "selector": selector})


def _do_screenshot(page, action, t0, download_dir):
    if download_dir is None:
        raise ActionError("screenshot requires download_dir")
    download_dir.mkdir(parents=True, exist_ok=True)
    path = download_dir / f"screenshot_{int(time.time() * 1000)}.png"
    page.screenshot(path=str(path), full_page=action.get("full_page", False))
    return _ok("screenshot", t0, result={"path": str(path)},
               downloaded=str(path))


def _do_scroll(page, action, t0):
    direction = (action.get("scroll") or "page_down").strip()
    if direction == "page_down":
        page.evaluate("window.scrollBy(0, window.innerHeight)")
    elif direction == "page_up":
        page.evaluate("window.scrollBy(0, -window.innerHeight)")
    elif direction == "to_selector":
        selector = (action.get("selector") or "").strip()
        if not selector:
            raise ActionError("scroll to_selector requires `selector`")
        page.locator(selector).scroll_into_view_if_needed(timeout=_timeout(action))
    else:
        raise ActionError(f"unknown scroll direction: {direction}")
    return _ok("scroll", t0, result={"direction": direction})


def _do_submit(page, action, t0):
    # Form submit. Owner confirmation handled by the controller — by the
    # time we reach this executor, the gate has already cleared.
    selector = (action.get("selector") or "form").strip()
    page.locator(selector).evaluate("el => el.submit()")
    page.wait_for_load_state("domcontentloaded", timeout=_timeout(action))
    return _ok("submit", t0, result={"selector": selector, "url_after": page.url})
