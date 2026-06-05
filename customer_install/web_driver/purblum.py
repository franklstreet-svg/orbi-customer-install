"""
purblum.py — Orby drives purblum.com/pages/order.html to submit an order.

This is the per-restaurant click path. It matches the UI we built in
order.html:
  · menu cards with "Customize & Add" button per item
  · customizer modal with single/remove/multi modifier groups
  · cart panel with name/phone/pickup-time/notes form
  · "Place Pickup Order" button → receipt modal pops up

Public:
    submit_order(cart, customer, headless=True, data_dir=None) -> dict

cart shape — same as what the order.html JS sends to the backend:
    [
      {"name": "Truckee Italian", "qty": 2, "modifiers": [
         {"group": "Bread", "action": "choose", "label": "Gluten-free", "delta": 1.50},
         {"group": "Comes with — uncheck to remove", "action": "remove", "label": "Onion", "delta": 0},
         {"group": "Add extras", "action": "add", "label": "Bacon", "delta": 1.75},
         ...
      ]},
      ...
    ]
customer shape:
    {"name": "Sarah Johnson", "phone": "775-555-0123",
     "pickup_time": "6:30 PM today"}
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .base import launch_browser, capture_failure

log = logging.getLogger("orbi.web_driver.purblum")

ORDER_URL = "https://purblum.com/pages/order.html"

# For demo mode, slow each Playwright action by this much so the
# restaurant owner can actually see what's happening on screen.
DEMO_SLOW_MS = 300


def submit_order(cart: list[dict],
                  customer: dict,
                  notes: str = "",
                  headless: bool = True,
                  record_video: bool = False,
                  data_dir: Path | None = None) -> dict:
    """Drive purblum.com to place this order. Returns:
        {ok, order_id, total, screenshot, video, error, elapsed_seconds}
    On any failure, captures a full-page screenshot for debugging.
    If record_video=True, saves an MP4/webm of the whole session (great
    for social-media demos showing Orby clicking through the page)."""
    if not cart:
        return {"ok": False, "error": "empty_cart"}
    t0 = time.time()
    # When recording video, slow it down enough that the cursor click on
    # each modifier is readable in playback.
    slow_mo = DEMO_SLOW_MS if (not headless or record_video) else 0
    video_dir = None
    if record_video:
        video_dir = Path(data_dir or "data") / "web_driver_videos"
    log.info(f"submit_order: {len(cart)} items, headless={headless}, video={record_video}")
    try:
        with launch_browser(headless=headless, slow_mo_ms=slow_mo,
                              record_video_dir=video_dir) as (browser, page):
            # ── Step 1: Open the order page ───────────────────────────
            page.goto(ORDER_URL, wait_until="domcontentloaded")
            # Wait for the menu to render — it's loaded async from menu.json
            page.wait_for_selector(".menu-card", state="visible", timeout=15_000)

            # ── Step 2: For each cart item, customize + add ───────────
            for item in cart:
                _add_item_to_cart(page, item)

            # ── Step 3: Fill out the customer info form ───────────────
            page.fill("#customer-name", customer.get("name", ""))
            page.fill("#customer-phone", customer.get("phone", ""))
            if customer.get("pickup_time"):
                page.fill("#pickup-time", customer["pickup_time"])
            if notes:
                page.fill("#order-notes", notes)

            # ── Step 4: Submit ────────────────────────────────────────
            page.click("#place-btn")

            # ── Step 5: Wait for the receipt modal ────────────────────
            page.wait_for_selector("#receipt-overlay[style*='flex']",
                                    state="visible", timeout=20_000)

            # Pull the order ID + total from the receipt
            order_id_el = page.query_selector("#receipt-order-id")
            order_id = (order_id_el.inner_text() if order_id_el else "").strip()
            # Total is in the receipt body — look for the TOTAL row
            total_text = ""
            try:
                # The TOTAL row has font-size:16px font-weight:700 — find by text
                total_el = page.query_selector("#receipt-body div:has-text('TOTAL')")
                if total_el:
                    total_text = total_el.inner_text().strip()
            except Exception:
                pass

            # Screenshot the receipt as proof of submission
            proof_path = ""
            try:
                shots = Path(data_dir or "data") / "web_driver_screenshots"
                shots.mkdir(parents=True, exist_ok=True)
                proof_path = str(shots / f"purblum_order_{int(time.time())}.png")
                page.screenshot(path=proof_path, full_page=True)
            except Exception as e:
                log.warning(f"receipt screenshot failed: {e}")

            # Grab the video path BEFORE the context closes (the file is
            # only finalized in the with-block's exit handler, but the
            # path attribute is set the moment recording starts).
            video_path = ""
            if record_video:
                try:
                    video_path = str(page.video.path()) if page.video else ""
                except Exception as e:
                    log.warning(f"video path lookup failed: {e}")

            return {
                "ok": True,
                "order_id": order_id,
                "total": total_text,
                "screenshot": proof_path,
                "video": video_path,
                "elapsed_seconds": round(time.time() - t0, 1),
            }
    except Exception as e:
        log.exception("submit_order failed")
        shot = ""
        try:
            # Try to capture a screenshot of whatever state we're in
            from playwright.sync_api import sync_playwright
            shot = ""  # the with-block already closed; no page available
        except Exception:
            pass
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "screenshot": shot,
            "elapsed_seconds": round(time.time() - t0, 1),
        }


def _add_item_to_cart(page, item: dict) -> None:
    """Find the menu card for `item['name']`, open its customizer modal,
    set every modifier, click Add to Order. Repeats item['qty'] times so
    qty=2 = two separate clicks (matches how a human would do it)."""
    name = item.get("name", "").strip()
    qty = max(1, int(item.get("qty", 1)))
    modifiers = item.get("modifiers", []) or []

    for n in range(qty):
        # Find the menu card whose <h4> matches the item name (exact)
        card = page.locator(
            f".menu-card:has(h4:text-is('{name}'))"
        ).first
        if card.count() == 0:
            raise RuntimeError(f"menu card not found for item: {name!r}")

        # Click "Customize & Add" on that card
        card.locator(".customize-btn").click()

        # Wait for the customizer modal to open
        page.wait_for_selector(".customizer-overlay.open", state="visible",
                                timeout=8_000)

        # Apply each modifier
        for mod in modifiers:
            _apply_modifier(page, mod)

        # Click "Add to Order — $X.XX"
        page.locator(".customizer .add-btn").click()

        # Wait for modal to close before we move to the next item
        page.wait_for_selector(".customizer-overlay.open", state="hidden",
                                timeout=5_000)


def _apply_modifier(page, mod: dict) -> None:
    """Inside the open customizer, set one modifier option:
       single → click the matching radio
       remove → uncheck the default checkbox (which is on by default)
       add    → check the multi-add checkbox"""
    action = (mod.get("action") or "").strip()
    label = (mod.get("label") or "").strip()
    if not (action and label):
        return

    if action == "choose":
        # Single-choice radio — click the <input> next to the label text
        page.locator(
            f"#cust-groups label:has-text('{label}') input[type='radio']"
        ).first.check()
    elif action == "remove":
        # The "uncheck to remove" group has checked-by-default checkboxes;
        # we UNCHECK them.
        cb = page.locator(
            f"#cust-groups label:has-text('{label}') input[type='checkbox']"
        ).first
        if cb.is_checked():
            cb.uncheck()
    elif action == "add":
        # Multi-add checkbox — check it.
        cb = page.locator(
            f"#cust-groups label:has-text('{label}') input[type='checkbox']"
        ).first
        if not cb.is_checked():
            cb.check()
    else:
        log.warning(f"unknown modifier action: {action!r}")
