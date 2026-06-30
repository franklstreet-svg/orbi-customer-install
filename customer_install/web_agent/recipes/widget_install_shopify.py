"""
widget_install_shopify — inject Orby's embed <script> into a Shopify theme.

Strategy:
  Online Store → Themes → Edit Code → Layout/theme.liquid → find </head> → inject

params:
  site_url   — customer's store URL (e.g. "https://mybusiness.com" or mystore.myshopify.com)
  username   — Shopify admin email
  password   — Shopify admin password
  embed_code — the <script> tag to inject
"""

from __future__ import annotations

import logging
import re

from . import register

log = logging.getLogger("orbi.web_agent.recipes.widget_install_shopify")


def _shopify_admin_url(site_url: str) -> str:
    domain = re.sub(r"https?://", "", site_url).rstrip("/")
    if "myshopify.com" not in domain:
        shop_handle = domain.split(".")[0]
        domain = f"{shop_handle}.myshopify.com"
    return f"https://{domain}/admin"


def run(page, *, goal: str, params: dict, on_confirm, workspace_dir) -> dict:
    site_url   = params.get("site_url", "")
    username   = params.get("username", "")
    password   = params.get("password", "")
    embed_code = params.get("embed_code", "")

    if not all([site_url, username, password, embed_code]):
        return {"ok": False, "result": "BLOCKED: missing params", "downloaded": []}

    admin_url = _shopify_admin_url(site_url)

    try:
        # ── Step 1: Log in ──────────────────────────────────────────────────
        page.goto(admin_url, wait_until="domcontentloaded", timeout=20_000)

        for sel in ("input[name='account[email]']", "input[type='email']", "#account_email"):
            try:
                page.fill(sel, username, timeout=3_000)
                break
            except Exception:
                continue

        for sel in ("button[type='submit']", "input[type='submit']"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_load_state("domcontentloaded", timeout=15_000)

        for sel in ("input[name='account[password]']", "input[type='password']"):
            try:
                page.fill(sel, password, timeout=4_000)
                break
            except Exception:
                continue

        if not on_confirm({"type": "submit", "reason": "Log in to Shopify admin"}):
            return {"ok": False, "result": "BLOCKED: owner declined login", "downloaded": []}

        for sel in ("button[type='submit']", "input[type='submit']"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_load_state("domcontentloaded", timeout=20_000)

        if "login" in page.url and "invalid" in page.content().lower():
            return {"ok": False,
                    "result": "BLOCKED: Shopify login failed — check credentials",
                    "downloaded": []}

        log.info("Shopify login succeeded")

        # ── Step 2: Navigate to Online Store → Themes ───────────────────────
        themes_url = f"{admin_url}/themes"
        page.goto(themes_url, wait_until="domcontentloaded", timeout=15_000)

        # Click "Edit Code" on the active (published) theme
        for label in ("Edit code", "Edit Code", "Actions", "Customize"):
            try:
                page.click(f"text={label}", timeout=4_000)
                page.wait_for_load_state("domcontentloaded", timeout=12_000)
                if "code" in page.url:
                    break
            except Exception:
                continue

        # ── Step 3: Open theme.liquid ───────────────────────────────────────
        for sel in (
            "text=theme.liquid",
            "a[href*='theme.liquid']",
            "[data-file='layout/theme.liquid']",
        ):
            try:
                page.click(sel, timeout=5_000)
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
                break
            except Exception:
                continue

        # ── Step 4: Edit the file ───────────────────────────────────────────
        # Shopify's code editor uses CodeMirror — we interact via textarea fallback
        editor_sel = None
        for sel in (".CodeMirror textarea", "textarea.editor", "textarea"):
            try:
                page.wait_for_selector(sel, timeout=5_000)
                editor_sel = sel
                break
            except Exception:
                continue

        if not editor_sel:
            return {"ok": False,
                    "result": "BLOCKED: could not find Shopify code editor",
                    "downloaded": []}

        current_code = page.eval_on_selector(editor_sel, "el => el.value") or ""

        if "data-orby-key" in current_code:
            return {"ok": True, "result": "Already installed in theme.liquid", "downloaded": []}

        if "</head>" not in current_code.lower():
            return {"ok": False,
                    "result": "BLOCKED: </head> not found in theme.liquid",
                    "downloaded": []}

        new_code = re.sub(
            r"(</head>)",
            embed_code + "\n\\1",
            current_code,
            count=1,
            flags=re.IGNORECASE,
        )

        page.eval_on_selector(
            editor_sel,
            "([el, val]) => { el.value = val; el.dispatchEvent(new Event('input', {bubbles:true})); }",
            [new_code],
        )

        if not on_confirm({"type": "submit", "reason": "Save theme.liquid with Orby embed code"}):
            return {"ok": False, "result": "BLOCKED: owner declined save", "downloaded": []}

        for sel in ("button[name='commit']", "button[type='submit']", ".action-button.primary"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_timeout(3_000)
        log.info("Shopify theme.liquid saved")
        return {"ok": True,
                "result": "Orby embed code added to Shopify theme.liquid",
                "downloaded": []}

    except Exception as e:
        log.warning("Shopify install error: %s", e)
        return {"ok": False, "result": f"BLOCKED: {e}", "downloaded": []}


register({
    "name": "widget_install_shopify",
    "site": "shopify",
    "run":  run,
})
