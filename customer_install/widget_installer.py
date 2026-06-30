"""
widget_installer.py — Auto-install Orby's chat widget on a customer's website.

Flow:
  1. detect_platform(url, html) → "wordpress" | "wix" | "squarespace" |
                                   "shopify" | "webflow" | "weebly" | "unknown"
  2. generate_embed_code(config) → one-line <script> tag unique to this tenant
  3. Orby asks for admin credentials via chat
  4. run_install(platform, site_url, creds, embed_code, data_dir, ...) →
     uses web_agent to log in and inject the code automatically
  5. verify_installed(site_url, api_key) → True/False
  6. Fallback: get_manual_instructions(platform, embed_code) → plain-English steps

Credentials are NEVER written to disk — held only in memory during install.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("orbi.widget_installer")

# ── Platform fingerprints ──────────────────────────────────────────────────────

_PLATFORM_SIGS: list[tuple[str, list[str]]] = [
    ("wordpress",    ["/wp-content/", "/wp-includes/", "wp-login.php",
                      "wordpress.org", "wpengine"]),
    ("shopify",      ["cdn.shopify.com", "myshopify.com", "Shopify.theme",
                      "shopify-section"]),
    ("squarespace",  ["squarespace.com", "squarespace-cdn.com",
                      "static1.squarespace.com", "sqs-video"]),
    ("wix",          ["wix.com", "wixstatic.com", "wixsite.com",
                      "wix-bolt", "X-Powered-By: Wix"]),
    ("webflow",      ["webflow.com", "assets.website-files.com",
                      "wf-form-page", "w-dyn-"]),
    ("weebly",       ["weebly.com", "weebly-data.com", "editmysite.com"]),
    ("godaddy",      ["godaddy.com", "secureserver.net", "gdwp"]),
    ("squarespace",  ["sqsp.net"]),
]


def detect_platform(url: str, html: str) -> str:
    """Identify the CMS from the scraped homepage HTML.
    Returns a lowercase platform name or 'unknown'."""
    combined = (url + " " + (html or "")).lower()
    for platform, sigs in _PLATFORM_SIGS:
        if any(sig.lower() in combined for sig in sigs):
            return platform
    return "unknown"


# ── Embed code ─────────────────────────────────────────────────────────────────

def generate_embed_code(config: dict) -> str:
    """Build the one-line <script> tag for this tenant.
    config: the parsed config.json dict."""
    api_key = (config.get("api_key") or "").strip()
    brain_url = (
        config.get("brain", {}).get("url")
        or "https://billing.twickell.com"
    ).rstrip("/")
    if not api_key:
        return "<!-- Orby: api_key not set in config.json -->"
    return (
        f'<script src="{brain_url}/widget.js" '
        f'data-orby-key="{api_key}" async></script>'
    )


def embed_script_marker(api_key: str) -> str:
    """A short unique string we can grep for to verify installation."""
    return f'data-orby-key="{api_key}"'


# ── Auto-install via web_agent ─────────────────────────────────────────────────

def run_install(
    platform: str,
    site_url: str,
    username: str,
    password: str,
    embed_code: str,
    *,
    data_dir: Path,
    workspace_dir: Path,
    brain_call,
    on_confirm=None,
) -> dict:
    """Drive a browser to log into the customer's CMS and inject embed_code.

    Returns:
        {"ok": bool, "message": str, "needs_manual": bool}
    """
    recipe_map = {
        "wordpress":   "widget_install_wordpress",
        "squarespace": "widget_install_squarespace",
        "shopify":     "widget_install_shopify",
        "wix":         "widget_install_wix",
    }

    recipe_name = recipe_map.get(platform)
    if not recipe_name:
        return {
            "ok": False,
            "needs_manual": True,
            "message": (
                f"I don't have an automatic installer for {platform or 'this platform'} yet. "
                f"Here are the manual instructions."
            ),
        }

    try:
        from web_agent import controller as _ctrl
    except ImportError:
        return {
            "ok": False,
            "needs_manual": True,
            "message": "Browser automation isn't available on this server yet.",
        }

    result = _ctrl.run_task(
        goal=f"Install Orby chat widget on {site_url}",
        data_dir=data_dir,
        workspace_dir=workspace_dir,
        brain_call=brain_call,
        start_url=_admin_login_url(platform, site_url),
        recipe_name=recipe_name,
        recipe_params={
            "site_url":   site_url,
            "username":   username,
            "password":   password,
            "embed_code": embed_code,
        },
        on_confirm=on_confirm or (lambda _: True),
        headless=True,
    )

    # Credentials are in recipe_params which lives only on the stack.
    # The controller does NOT persist recipe_params to disk.

    ok = result.get("ok", False)
    reason = result.get("stopped_reason", "")
    blocked = "BLOCKED:" in reason

    if ok:
        return {
            "ok": True,
            "needs_manual": False,
            "message": "I installed my chat widget on your website successfully.",
        }
    if blocked:
        blocked_reason = reason.replace("BLOCKED:", "").strip()
        return {
            "ok": False,
            "needs_manual": True,
            "message": (
                f"I ran into a wall: {blocked_reason}. "
                f"Here are the manual steps instead."
            ),
        }
    return {
        "ok": False,
        "needs_manual": True,
        "message": "I wasn't able to complete the install automatically. Here's what to do manually.",
    }


def _admin_login_url(platform: str, site_url: str) -> str:
    base = site_url.rstrip("/")
    if platform == "wordpress":
        return f"{base}/wp-admin/"
    if platform == "squarespace":
        return "https://account.squarespace.com/"
    if platform == "shopify":
        # Shopify admin is at the myshopify.com subdomain
        domain = re.sub(r"https?://", "", base).rstrip("/")
        if "myshopify.com" not in domain:
            domain = domain.split(".")[0] + ".myshopify.com"
        return f"https://{domain}/admin"
    if platform == "wix":
        return "https://manage.wix.com/"
    return site_url


# ── Verification ───────────────────────────────────────────────────────────────

def verify_installed(site_url: str, api_key: str, timeout: int = 12) -> bool:
    """Fetch the live website and confirm the embed marker is present."""
    if not site_url or not api_key:
        return False
    marker = embed_script_marker(api_key)
    try:
        req = urllib.request.Request(
            site_url,
            headers={"User-Agent": "OrbyVerify/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(200_000).decode("utf-8", errors="ignore")
        return marker in html
    except Exception as e:
        log.debug(f"verify_installed fetch failed for {site_url}: {e}")
        return False


# ── Manual fallback instructions ───────────────────────────────────────────────

_MANUAL: dict[str, str] = {
    "wordpress": """\
Here's how to add me to your WordPress site:
1. Log into your WordPress dashboard (yourdomain.com/wp-admin)
2. Go to **Appearance → Theme File Editor**
3. Select **header.php** on the right
4. Find the line that says `</head>` and paste the code on the line just above it
5. Click **Update File**

That's it — I'll appear on every page of your site.""",

    "squarespace": """\
Here's how to add me to your Squarespace site:
1. Log into your Squarespace account at squarespace.com
2. Go to **Settings → Advanced → Code Injection**
3. Paste the code into the **Header** box
4. Click **Save**

I'll appear on every page automatically.""",

    "shopify": """\
Here's how to add me to your Shopify store:
1. Log into your Shopify admin
2. Go to **Online Store → Themes**
3. Click **Actions → Edit Code** on your active theme
4. Open **Layout → theme.liquid**
5. Find `</head>` and paste the code on the line just above it
6. Click **Save**""",

    "wix": """\
Here's how to add me to your Wix site:
1. Log into Wix and open your site editor
2. Click the **+** (Add) menu → **Embed Code → Embed HTML**
3. Paste the code into the HTML box
4. Position the element anywhere (it'll show as a chat button, not a box)
5. Publish your site

Or go to **Settings → Custom Code → Add Code** and paste it in the Head section.""",

    "webflow": """\
Here's how to add me to your Webflow site:
1. Go to your Webflow project settings
2. Click **Custom Code**
3. Paste the code in the **Head Code** section
4. Save and publish your site""",

    "unknown": """\
Here's how to add me to your website:
1. Open your website's theme or template files
2. Find the file that contains `</head>` (often called header.php, base.html, layout.html, or theme.liquid)
3. Paste the code on the line just above `</head>`
4. Save and publish

If you're not sure how, just forward this to your web developer — it's one line of code and takes them about 2 minutes.""",
}

_MANUAL["weebly"] = """\
Here's how to add me to your Weebly site:
1. Log into Weebly and open your site editor
2. Go to **Settings → SEO**
3. Paste the code into the **Header Code** box
4. Save and publish"""

_MANUAL["godaddy"] = """\
Here's how to add me to your GoDaddy Website Builder:
1. Log into your GoDaddy account and open your website editor
2. Go to **Settings → Website Settings**
3. Find the **Header** or **Custom Code** section
4. Paste the code there and save"""


def get_manual_instructions(platform: str, embed_code: str) -> str:
    """Return human-readable installation steps + the embed code block."""
    instructions = _MANUAL.get(platform, _MANUAL["unknown"])
    return f"{instructions}\n\nHere's your code:\n```\n{embed_code}\n```"


# ── Orby's chat script ────────────────────────────────────────────────────────

def install_prompt(platform: str, site_url: str) -> str:
    """What Orby says when she's ready to auto-install."""
    platform_labels = {
        "wordpress":   "WordPress",
        "squarespace": "Squarespace",
        "shopify":     "Shopify",
        "wix":         "Wix",
        "webflow":     "Webflow",
        "weebly":      "Weebly",
        "godaddy":     "GoDaddy Website Builder",
        "unknown":     "your website platform",
    }
    label = platform_labels.get(platform, "your website platform")
    can_auto = platform in ("wordpress", "squarespace", "shopify", "wix")

    if can_auto:
        return (
            f"Your website runs on {label}. I can install my chat widget on it "
            f"automatically — I just need your {label} login email and password. "
            f"I'll use them only for this one task and won't store them anywhere. "
            f"What are your credentials? (Email first, then password on the next line, "
            f"or together like: email / password)"
        )
    return (
        f"Your website runs on {label}. I'll give you the code and exact steps "
        f"to add me — it takes about 2 minutes. Ready?"
    )


def developer_email(
    platform: str,
    site_url: str,
    biz_name: str,
    owner_name: str,
    embed_code: str,
) -> tuple[str, str]:
    """Return (subject, body) for a professional email to the web developer."""
    platform_labels = {
        "wordpress":   "WordPress",
        "squarespace": "Squarespace",
        "shopify":     "Shopify",
        "wix":         "Wix",
        "webflow":     "Webflow",
        "weebly":      "Weebly",
        "godaddy":     "GoDaddy Website Builder",
        "unknown":     "the website",
    }
    label = platform_labels.get(platform, "the website")

    where_map = {
        "wordpress":   "Paste it in **Appearance → Theme File Editor → header.php**, just above the closing `</head>` tag.",
        "squarespace": "Go to **Settings → Advanced → Code Injection** and paste it in the **Header** field.",
        "shopify":     "Go to **Online Store → Themes → Actions → Edit Code**, open **Layout/theme.liquid**, and paste it just above the closing `</head>` tag.",
        "wix":         "Go to **Settings → Custom Code → Add Code**, paste it in the **Head** section, then publish the site.",
        "webflow":     "Go to **Project Settings → Custom Code** and paste it in the **Head Code** section, then publish.",
        "weebly":      "Go to **Settings → SEO → Header Code** and paste it there, then save and publish.",
        "godaddy":     "Go to **Settings → Website Settings → Header** or **Custom Code** and paste it there.",
        "unknown":     "Paste it just above the closing `</head>` tag in your site's main template or header file.",
    }
    where = where_map.get(platform, where_map["unknown"])

    subject = f"Please add one line of code to {site_url} — from {owner_name}"

    body = f"""\
Hi,

{owner_name} at {biz_name or site_url} has set up an AI chat assistant called Orby \
for their website ({site_url}). They asked me to send you everything you need to get it live.

It's one line of code. Here it is:

{embed_code}

Where to put it ({label}):
{where}

That's the whole job. Once it's added and published, Orby's chat button will appear \
on every page of the site automatically — no further configuration needed.

If you run into any questions, {owner_name} can reach out to support@orbi.ai.

Thanks for your help,
Orby (on behalf of {owner_name})
"""
    return subject, body


def webdev_ask_prompt(biz_name: str) -> str:
    """What Orby says when pivoting to the web-developer email path."""
    return (
        "No problem at all. What's your web developer's email address? "
        "I'll send them everything they need — the code, where to put it, "
        "and a clear explanation. They'll be done in about 2 minutes."
    )


def blocked_prompt(platform: str, embed_code: str) -> str:
    """What Orby says when auto-install fails."""
    return (
        f"I ran into a security wall on your {platform or 'website'} admin — "
        f"some platforms block automated logins. No problem, here's how to do it manually:\n\n"
        + get_manual_instructions(platform, embed_code)
    )
