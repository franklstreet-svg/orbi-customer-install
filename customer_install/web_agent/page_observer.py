"""
page_observer — Orbi's "eyes" for the web agent.

Reads what's on the current page in a form the LLM can reason over:

  1. Accessibility tree (default) — Playwright exposes a structured map
     of every clickable/interactive element with its role, name, and
     value. Text-only, no pixels, no vision model required. This is
     the same thing screen readers use, and it's how modern browser
     agents stay reliable.
  2. DOM scrape fallback — when the a11y tree is missing labels (custom
     web components, broken markup), pull <input>/<button>/<a> elements
     directly with their attributes.
  3. Visual screenshot (last resort) — for the LLM to use as a sanity
     check when the textual observations don't add up. Saved to
     DATA_DIR/web_agent_screenshots/ and referenced by path.

The observation is intentionally compact — token budget per step matters.
We trim the a11y tree to interactive elements only, cap labels at ~120
chars, and skip decorative roles (img, presentation, none).
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("orbi.web_agent.observer")


# Roles we KEEP in the trimmed observation — anything a user could interact
# with. Other roles (img, presentation, banner) drop out to keep the brain's
# prompt short.
_INTERACTIVE_ROLES = {
    "button", "link", "textbox", "searchbox", "combobox", "checkbox",
    "radio", "switch", "menuitem", "menuitemcheckbox", "menuitemradio",
    "tab", "treeitem", "option", "spinbutton", "slider",
}

# Roles that introduce useful structure (so the brain knows what section
# it's in) but don't get a clickable ref.
_STRUCTURAL_ROLES = {
    "heading", "navigation", "main", "form", "dialog", "alert",
}

_MAX_LABEL_CHARS = 120
_MAX_NODES = 200  # safety cap — most pages fit in <100 interactive elements


def observe(page) -> dict:
    """Return a compact observation of the current page.

    {
        "url":          str,
        "title":        str,
        "elements":     list[dict],   # interactive + structural, capped at _MAX_NODES
        "page_text":    str,          # readable text on the page, capped 4k chars
        "truncated":    bool,
    }

    Each element dict:
        {"ref": "e3", "role": "button", "name": "Sign in",
         "value": "", "disabled": False}

    The `ref` is a stable label the brain emits back in its actions — the
    executor maps it to a real selector via accessibility ID.
    """
    obs: dict[str, Any] = {
        "url":   page.url,
        "title": _safe(lambda: page.title()) or "",
        "elements": [],
        "page_text": "",
        "truncated": False,
    }

    # 1. Accessibility tree — the primary observation
    try:
        a11y = page.accessibility.snapshot(interesting_only=True)
        if a11y:
            flat: list[dict] = []
            _walk_a11y(a11y, flat)
            if len(flat) > _MAX_NODES:
                obs["truncated"] = True
                flat = flat[:_MAX_NODES]
            obs["elements"] = flat
    except Exception as e:
        log.warning("accessibility snapshot failed: %s", e)

    # 2. DOM fallback — only if a11y returned nothing useful
    if not obs["elements"]:
        try:
            obs["elements"] = _dom_fallback(page)
        except Exception as e:
            log.warning("DOM fallback failed: %s", e)

    # 3. Page text — capped, for the brain's general sense of what's here
    try:
        text = page.inner_text("body", timeout=2000) or ""
        obs["page_text"] = " ".join(text.split())[:4000]
    except Exception:
        pass

    return obs


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _safe(fn):
    """Call fn(), return None on any exception. For non-essential reads
    where we'd rather have a blank than crash the observation."""
    try:
        return fn()
    except Exception:
        return None


def _walk_a11y(node: dict, out: list[dict], depth: int = 0) -> None:
    """Flatten the accessibility tree into a list of interactive +
    structural elements with monotonically-increasing `ref` IDs."""
    role = (node.get("role") or "").lower()
    if role in _INTERACTIVE_ROLES or role in _STRUCTURAL_ROLES:
        name = (node.get("name") or "").strip()
        if len(name) > _MAX_LABEL_CHARS:
            name = name[:_MAX_LABEL_CHARS - 1] + "…"
        entry = {
            "ref": f"e{len(out)}",
            "role": role,
            "name": name,
            "value": (node.get("value") or "")[:80],
            "disabled": bool(node.get("disabled")),
        }
        if depth > 0:
            entry["depth"] = depth
        out.append(entry)
        if len(out) >= _MAX_NODES:
            return
    for child in node.get("children") or []:
        _walk_a11y(child, out, depth + 1)
        if len(out) >= _MAX_NODES:
            return


def _dom_fallback(page) -> list[dict]:
    """When the a11y tree is empty (custom shadow-DOM components, frames,
    weird sites), pull interactive elements directly from the DOM. Less
    semantic but better than blind."""
    js = """
    () => {
      const out = [];
      const sel = 'a, button, input, select, textarea, [role=button], [role=link]';
      document.querySelectorAll(sel).forEach((el, i) => {
        if (i >= 200) return;
        const r = el.tagName.toLowerCase();
        out.push({
          ref: `e${i}`,
          role: r === 'a' ? 'link' : r === 'input' ? (el.type || 'textbox') : r,
          name: (el.innerText || el.value || el.placeholder ||
                  el.getAttribute('aria-label') || '').trim().slice(0, 120),
          value: (el.value || '').toString().slice(0, 80),
          disabled: !!el.disabled,
        });
      });
      return out;
    }
    """
    return page.evaluate(js) or []


# ---------------------------------------------------------------------------
# Public helper for the controller — resolve a brain-emitted ref to the
# actual Playwright selector. The brain says "click e7"; we look up
# what e7 was on the most recent observation and translate.
# ---------------------------------------------------------------------------

def selector_for_ref(observation: dict, ref: str) -> str | None:
    """Given an observation and a `ref` the brain emitted, return a CSS
    selector Playwright can act on. Falls back to role+name lookup if
    the page has changed since the observation was captured."""
    elements = observation.get("elements") or []
    for el in elements:
        if el.get("ref") == ref:
            role = el.get("role") or ""
            name = (el.get("name") or "").replace("'", r"\'")
            if name:
                # ARIA role+name is what Playwright's get_by_role uses.
                # Returning a selector string the executor can pass to
                # page.click / page.fill etc.
                return f"role={role}[name='{name}']"
            # No name — fall back to first-of-role.
            return f"role={role}"
    return None
