"""
widget_install_wix — inject Orby's embed <script> via Wix Settings → Custom Code.

Wix actively resists browser automation (anti-bot JS, frequent UI changes).
This recipe tries the Settings → Custom Code path. If blocked, it returns
a BLOCKED reason so the manual instructions are shown instead.

params:
  site_url   — customer's Wix site URL (for reference)
  username   — Wix account email
  password   — Wix account password
  embed_code — the <script> tag to inject
"""

from __future__ import annotations

import logging

from . import register

log = logging.getLogger("orbi.web_agent.recipes.widget_install_wix")

_LOGIN_URL = "https://users.wix.com/signin"
_MANAGE_URL = "https://manage.wix.com/"


def run(page, *, goal: str, params: dict, on_confirm, workspace_dir) -> dict:
    username   = params.get("username", "")
    password   = params.get("password", "")
    embed_code = params.get("embed_code", "")

    if not all([username, password, embed_code]):
        return {"ok": False, "result": "BLOCKED: missing params", "downloaded": []}

    try:
        # ── Step 1: Log in ──────────────────────────────────────────────────
        page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(2_000)  # Wix loads slowly

        for sel in ("input[name='email']", "input[type='email']", "#email"):
            try:
                page.fill(sel, username, timeout=5_000)
                break
            except Exception:
                continue

        for sel in ("button[type='submit']", "text=Continue", "text=Next"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_timeout(2_000)

        for sel in ("input[name='password']", "input[type='password']", "#password"):
            try:
                page.fill(sel, password, timeout=5_000)
                break
            except Exception:
                continue

        if not on_confirm({"type": "submit", "reason": "Log in to Wix account"}):
            return {"ok": False, "result": "BLOCKED: owner declined login", "downloaded": []}

        for sel in ("button[type='submit']", "text=Log In", "text=Sign In"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_load_state("domcontentloaded", timeout=25_000)
        page.wait_for_timeout(3_000)

        # Wix may show a captcha or bot-detection block
        content = page.content().lower()
        if any(w in content for w in ("captcha", "robot", "verify you", "bot")):
            return {"ok": False,
                    "result": "BLOCKED: Wix is asking for captcha verification — manual install needed",
                    "downloaded": []}

        if "signin" in page.url or "login" in page.url:
            return {"ok": False,
                    "result": "BLOCKED: Wix login failed — check credentials or try manual install",
                    "downloaded": []}

        log.info("Wix login attempt reached: %s", page.url)

        # ── Step 2: Navigate to Settings → Custom Code ──────────────────────
        page.goto(_MANAGE_URL, wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(3_000)

        # Navigate to site dashboard first
        for sel in ("text=Settings", "[data-hook='settings']", "a[href*='settings']"):
            try:
                page.click(sel, timeout=5_000)
                page.wait_for_load_state("domcontentloaded", timeout=12_000)
                break
            except Exception:
                continue

        for sel in ("text=Custom Code", "a[href*='custom-code']", "[data-hook='custom-code']"):
            try:
                page.click(sel, timeout=5_000)
                page.wait_for_load_state("domcontentloaded", timeout=12_000)
                break
            except Exception:
                continue

        page.wait_for_timeout(2_000)

        # ── Step 3: Add new code snippet ────────────────────────────────────
        content = page.content()
        if "data-orby-key" in content:
            return {"ok": True, "result": "Already installed via Wix Custom Code", "downloaded": []}

        # Click "Add Custom Code" or "+"
        for sel in ("text=Add Custom Code", "text=+ Add Code", "button:has-text('Add')"):
            try:
                page.click(sel, timeout=5_000)
                page.wait_for_timeout(2_000)
                break
            except Exception:
                continue

        # Paste into the code textarea
        for sel in ("textarea", "input[type='text']", ".code-input"):
            try:
                page.wait_for_selector(sel, timeout=5_000)
                page.fill(sel, embed_code, timeout=5_000)
                break
            except Exception:
                continue

        # Set placement to "Head"
        for sel in ("text=Head", "option[value='head']", "[data-value='head']"):
            try:
                page.click(sel, timeout=3_000)
                break
            except Exception:
                continue

        if not on_confirm({"type": "submit", "reason": "Save Wix Custom Code with Orby widget"}):
            return {"ok": False, "result": "BLOCKED: owner declined save", "downloaded": []}

        for sel in ("text=Apply", "text=Save", "button[type='submit']"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_timeout(3_000)

        # Wix requires publishing for changes to go live
        for sel in ("text=Publish", "button:has-text('Publish')"):
            try:
                page.click(sel, timeout=5_000)
                page.wait_for_timeout(3_000)
                break
            except Exception:
                continue

        log.info("Wix Custom Code saved and published")
        return {"ok": True,
                "result": "Orby embed code added to Wix site via Custom Code (Head). Published.",
                "downloaded": []}

    except Exception as e:
        log.warning("Wix install error: %s", e)
        return {"ok": False,
                "result": f"BLOCKED: Wix automation hit an error — {e}. Manual install needed.",
                "downloaded": []}


register({
    "name": "widget_install_wix",
    "site": "wix",
    "run":  run,
})
