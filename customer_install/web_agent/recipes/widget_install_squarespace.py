"""
widget_install_squarespace — inject Orby's embed <script> via Squarespace
Code Injection (Settings → Advanced → Code Injection → Header).

params:
  site_url   — customer's Squarespace site URL (used for verification only)
  username   — Squarespace account email
  password   — Squarespace account password
  embed_code — the <script> tag to inject
"""

from __future__ import annotations

import logging

from . import register

log = logging.getLogger("orbi.web_agent.recipes.widget_install_squarespace")

_LOGIN_URL        = "https://account.squarespace.com/password"
_DASHBOARD_URL    = "https://account.squarespace.com/"


def run(page, *, goal: str, params: dict, on_confirm, workspace_dir) -> dict:
    username   = params.get("username", "")
    password   = params.get("password", "")
    embed_code = params.get("embed_code", "")
    site_url   = params.get("site_url", "")

    if not all([username, password, embed_code]):
        return {"ok": False, "result": "BLOCKED: missing params", "downloaded": []}

    try:
        # ── Step 1: Log in ──────────────────────────────────────────────────
        page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)

        for sel in ("input[name='email']", "input[type='email']", "#email"):
            try:
                page.fill(sel, username, timeout=3_000)
                break
            except Exception:
                continue

        for sel in ("input[name='password']", "input[type='password']", "#password"):
            try:
                page.fill(sel, password, timeout=3_000)
                break
            except Exception:
                continue

        if not on_confirm({"type": "submit", "reason": "Log in to Squarespace"}):
            return {"ok": False, "result": "BLOCKED: owner declined login", "downloaded": []}

        for sel in ("button[type='submit']", "input[type='submit']", ".submit-btn"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_load_state("domcontentloaded", timeout=20_000)

        if "password" in page.url and "incorrect" in page.content().lower():
            return {"ok": False,
                    "result": "BLOCKED: Squarespace login failed — check credentials",
                    "downloaded": []}

        log.info("Squarespace login succeeded, at %s", page.url)

        # ── Step 2: Open the correct site ──────────────────────────────────
        # If user has multiple sites, find the right one.
        content = page.content()
        if site_url:
            domain = site_url.replace("https://", "").replace("http://", "").rstrip("/")
            # Click site card matching domain
            try:
                page.click(f"text={domain}", timeout=4_000)
                page.wait_for_load_state("domcontentloaded", timeout=12_000)
            except Exception:
                pass  # Single-site accounts go straight to the editor

        # ── Step 3: Navigate to Settings → Advanced → Code Injection ───────
        # Squarespace backend URL pattern: /{site-id}/settings/advanced/code-injection
        # Try direct URL navigation into settings
        current = page.url
        if "squarespace.com" not in current:
            return {"ok": False,
                    "result": "BLOCKED: did not reach Squarespace dashboard",
                    "downloaded": []}

        # Try clicking through the nav
        for nav_label in ("Settings", "settings"):
            try:
                page.click(f"text={nav_label}", timeout=4_000)
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
                break
            except Exception:
                continue

        for adv_label in ("Advanced", "advanced"):
            try:
                page.click(f"text={adv_label}", timeout=4_000)
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
                break
            except Exception:
                continue

        for ci_label in ("Code Injection", "code-injection"):
            try:
                page.click(f"text={ci_label}", timeout=4_000)
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
                break
            except Exception:
                continue

        # ── Step 4: Paste into the Header field ────────────────────────────
        # Code Injection page has labeled textareas: Header, Footer, Lock Page, etc.
        header_sel = None
        for sel in (
            "textarea[data-field='header']",
            "#header-injection",
            "textarea:near(:text('Header'))",
            "textarea",
        ):
            try:
                page.wait_for_selector(sel, timeout=5_000)
                header_sel = sel
                break
            except Exception:
                continue

        if not header_sel:
            return {"ok": False,
                    "result": "BLOCKED: could not find Code Injection textarea",
                    "downloaded": []}

        existing = page.eval_on_selector(header_sel, "el => el.value") or ""
        if "data-orby-key" in existing:
            return {"ok": True, "result": "Already installed", "downloaded": []}

        page.eval_on_selector(
            header_sel,
            "([el, v]) => { el.value = el.value + '\\n' + v; el.dispatchEvent(new Event('input')); }",
            [embed_code],
        )

        if not on_confirm({"type": "submit", "reason": "Save Squarespace Code Injection"}):
            return {"ok": False, "result": "BLOCKED: owner declined save", "downloaded": []}

        for sel in ("button[type='submit']", "input[type='submit']", ".save-btn", "text=Save"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_timeout(3_000)
        log.info("Squarespace Code Injection saved")
        return {"ok": True,
                "result": "Orby embed code added to Squarespace header via Code Injection",
                "downloaded": []}

    except Exception as e:
        log.warning("Squarespace install error: %s", e)
        return {"ok": False, "result": f"BLOCKED: {e}", "downloaded": []}


register({
    "name": "widget_install_squarespace",
    "site": "squarespace",
    "run":  run,
})
