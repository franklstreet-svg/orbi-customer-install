"""
widget_install_wordpress — inject Orby's embed <script> into a WordPress site.

Strategy:
  1. Log in at /wp-admin/
  2. Navigate to Appearance → Theme File Editor
  3. Open header.php
  4. Find </head> and insert the embed code just above it
  5. Save
  6. Fall back to Appearance → Customize → Custom HTML widget if editor is disabled

params:
  site_url   — e.g. "https://mybusiness.com"
  username   — WordPress admin username or email
  password   — WordPress admin password
  embed_code — the <script> tag to inject
"""

from __future__ import annotations

import logging
import re

from . import register

log = logging.getLogger("orbi.web_agent.recipes.widget_install_wordpress")

_EDITOR_PATH  = "/wp-admin/theme-editor.php"
_EDITOR_FILE  = "?file=header.php"


def run(page, *, goal: str, params: dict, on_confirm, workspace_dir) -> dict:
    site_url   = params.get("site_url", "").rstrip("/")
    username   = params.get("username", "")
    password   = params.get("password", "")
    embed_code = params.get("embed_code", "")

    if not all([site_url, username, password, embed_code]):
        return {"ok": False, "result": "BLOCKED: missing required params", "downloaded": []}

    try:
        # ── Step 1: Log in ──────────────────────────────────────────────────
        login_url = f"{site_url}/wp-login.php"
        page.goto(login_url, wait_until="domcontentloaded", timeout=20_000)

        # Fill credentials
        for sel in ("#user_login", "input[name='log']"):
            try:
                page.fill(sel, username, timeout=3_000)
                break
            except Exception:
                continue

        for sel in ("#user_pass", "input[name='pwd']"):
            try:
                page.fill(sel, password, timeout=3_000)
                break
            except Exception:
                continue

        if not on_confirm({"type": "submit", "reason": "Log in to WordPress admin"}):
            return {"ok": False, "result": "BLOCKED: owner declined login", "downloaded": []}

        for sel in ("#wp-submit", "input[type='submit']"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_load_state("domcontentloaded", timeout=15_000)

        # Detect login failure
        if "wp-login.php" in page.url and "incorrect" in page.content().lower():
            return {"ok": False,
                    "result": "BLOCKED: WordPress login failed — check credentials",
                    "downloaded": []}

        log.info("WordPress login succeeded at %s", page.url)

        # ── Step 2: Try Theme File Editor ───────────────────────────────────
        editor_url = f"{site_url}{_EDITOR_PATH}{_EDITOR_FILE}"
        page.goto(editor_url, wait_until="domcontentloaded", timeout=15_000)
        content = page.content()

        if "not allowed" in content.lower() or "disabled" in content.lower() or "error" in content.lower():
            log.info("Theme editor disabled — falling back to Customizer")
            return _try_customizer(page, site_url, embed_code, on_confirm)

        # ── Step 3: Edit header.php ─────────────────────────────────────────
        # The theme editor shows the file content in a <textarea id="newcontent">
        try:
            page.wait_for_selector("#newcontent", timeout=8_000)
        except Exception:
            log.info("No textarea found in theme editor — falling back")
            return _try_customizer(page, site_url, embed_code, on_confirm)

        current_code = page.eval_on_selector("#newcontent", "el => el.value") or ""

        if not current_code.strip():
            return _try_customizer(page, site_url, embed_code, on_confirm)

        # Already installed?
        if "data-orby-key" in current_code:
            return {"ok": True,
                    "result": "Orby widget is already in header.php",
                    "downloaded": []}

        # Insert just above </head>
        if "</head>" not in current_code.lower():
            return _try_customizer(page, site_url, embed_code, on_confirm)

        new_code = re.sub(
            r"(</head>)",
            embed_code + "\n\\1",
            current_code,
            count=1,
            flags=re.IGNORECASE,
        )

        # Write into the textarea via JS (fill() can be slow on large files)
        page.eval_on_selector(
            "#newcontent",
            "([el, val]) => { el.value = val; el.dispatchEvent(new Event('input')); }",
            [new_code],
        )

        if not on_confirm({"type": "submit",
                           "reason": "Save header.php with Orby embed code"}):
            return {"ok": False, "result": "BLOCKED: owner declined save", "downloaded": []}

        # Click the Update File button
        for sel in ("#submit", "input[name='submit']", "button[type='submit']"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_load_state("domcontentloaded", timeout=15_000)

        if "updated" in page.content().lower() or "data-orby-key" in page.content():
            log.info("header.php saved successfully")
            return {"ok": True,
                    "result": "Orby embed code added to WordPress header.php",
                    "downloaded": []}

        return _try_customizer(page, site_url, embed_code, on_confirm)

    except Exception as e:
        log.warning("WordPress install recipe error: %s", e)
        return {"ok": False,
                "result": f"BLOCKED: unexpected error — {e}",
                "downloaded": []}


def _try_customizer(page, site_url: str, embed_code: str, on_confirm) -> dict:
    """Fallback: use WP Customizer → Additional CSS area.
    Note: this only works if the theme supports a header scripts area.
    If not, we return BLOCKED so manual instructions are shown."""
    try:
        customizer_url = f"{site_url}/wp-admin/customize.php"
        page.goto(customizer_url, wait_until="domcontentloaded", timeout=15_000)

        # Look for "Additional CSS" or "Custom CSS" panel
        for label in ("Additional CSS", "Custom CSS", "Custom Scripts"):
            try:
                page.click(f"text={label}", timeout=3_000)
                page.wait_for_load_state("domcontentloaded", timeout=8_000)
                break
            except Exception:
                continue
        else:
            return {"ok": False,
                    "result": "BLOCKED: Theme Editor disabled and Customizer has no script area",
                    "downloaded": []}

        # Find a textarea for custom code
        try:
            page.wait_for_selector("textarea", timeout=5_000)
        except Exception:
            return {"ok": False,
                    "result": "BLOCKED: no editable area found in Customizer",
                    "downloaded": []}

        existing = page.eval_on_selector("textarea", "el => el.value") or ""
        if "data-orby-key" in existing:
            return {"ok": True, "result": "Already installed", "downloaded": []}

        page.eval_on_selector(
            "textarea",
            "([el, v]) => { el.value = el.value + '\\n' + v; el.dispatchEvent(new Event('input')); }",
            [embed_code],
        )

        if not on_confirm({"type": "submit", "reason": "Save Customizer with Orby embed code"}):
            return {"ok": False, "result": "BLOCKED: owner declined", "downloaded": []}

        for sel in ("#save", "button.save", "[data-action='save']"):
            try:
                page.click(sel, timeout=5_000)
                break
            except Exception:
                continue

        page.wait_for_timeout(3_000)
        return {"ok": True,
                "result": "Orby embed code saved via WordPress Customizer",
                "downloaded": []}

    except Exception as e:
        return {"ok": False,
                "result": f"BLOCKED: Customizer fallback failed — {e}",
                "downloaded": []}


register({
    "name": "widget_install_wordpress",
    "site": "wordpress",
    "run":  run,
})
