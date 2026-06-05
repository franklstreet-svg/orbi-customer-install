"""
phone_order.py — Phone-side order finalization.

When a call ends after an order conversation:
  1. extract_order_from_history() — LLM parses the transcript into structured JSON
  2. build_cart_for_purblum() — maps extracted items to the canonical menu and
     produces a cart in the shape web_driver.submit_purblum_order expects
  3. caller gets a verbal confirmation + SMS receipt (handled in voice.py)

The cart shape this produces is identical to what order.html's JS POSTs to
/api/public/purblum/order, so web_driver clicks through purblum.com using
EXACTLY the same data a normal customer would submit.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request

log = logging.getLogger("orbi.phone_order")


# ---------------------------------------------------------------------------
# Menu loading + flattening
# ---------------------------------------------------------------------------

def load_menu(menu_url_or_path: str) -> dict:
    """Load canonical menu. Accepts http(s) URL or local file path."""
    if menu_url_or_path.startswith(("http://", "https://")):
        req = Request(menu_url_or_path, headers={"User-Agent": "Orby/1.0"})
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    return json.loads(Path(menu_url_or_path).read_text(encoding="utf-8"))


def flatten_items(menu: dict) -> list[dict]:
    """Return [{..., _category: 'Signature Sandwiches'}] for every item."""
    out = []
    for cat in menu.get("categories", []) or []:
        for item in cat.get("items", []) or []:
            row = dict(item)
            row["_category"] = cat.get("name", "")
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Fuzzy matching helpers
# ---------------------------------------------------------------------------

_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]+")


def _norm(s: str) -> str:
    return _NORMALIZE_RE.sub("", (s or "").lower()).strip()


def match_item(speech_name: str, menu_items: list[dict]) -> Optional[dict]:
    """Find the menu item a phone-speech name refers to.
       Tries: exact substring → difflib close-match → word-overlap."""
    if not speech_name:
        return None
    target = _norm(speech_name)
    if not target:
        return None
    target_words = set(target.split())

    # 1. Exact / substring
    for item in menu_items:
        n = _norm(item.get("name", ""))
        if target == n or target in n or n in target:
            return item

    # 2. difflib on raw names
    names = [it.get("name", "") for it in menu_items]
    close = difflib.get_close_matches(speech_name, names, n=1, cutoff=0.55)
    if close:
        for it in menu_items:
            if it.get("name") == close[0]:
                return it

    # 3. Word overlap (handles "Italian" → "Truckee Italian")
    best, best_score = None, 0
    for item in menu_items:
        item_words = set(_norm(item.get("name", "")).split())
        if not item_words:
            continue
        overlap = len(target_words & item_words)
        if overlap > best_score:
            best, best_score = item, overlap
    return best if best_score >= 1 else None


def _match_option(speech: str, options: list[dict]) -> Optional[dict]:
    """Match a spoken option label (e.g. 'sourdough', 'extra meat') to a
    menu option entry. Returns the option dict or None."""
    if not speech or not options:
        return None
    s = _norm(speech)
    if not s:
        return None
    # Strip noise words callers add
    s_clean = re.sub(r"\b(add|put|extra|some|a|an|the|with|cheese|please)\b", " ", s).strip()
    for opt in options:
        n = _norm(opt.get("label", ""))
        if n == s or s in n or n in s:
            return opt
        if s_clean and (s_clean == n or s_clean in n or n in s_clean):
            return opt
    # difflib
    labels = [o.get("label", "") for o in options]
    close = difflib.get_close_matches(speech, labels, n=1, cutoff=0.55)
    if close:
        return next((o for o in options if o.get("label") == close[0]), None)
    return None


def _match_remove_default(speech: str, defaults: list[str]) -> Optional[str]:
    """Match a 'no onions' / 'no tomato' phrase to one of the defaults_on
    labels. Returns the canonical default label or None."""
    if not speech or not defaults:
        return None
    s = _norm(speech)
    for d in defaults:
        if _norm(d) in s or s in _norm(d):
            return d
    close = difflib.get_close_matches(speech, defaults, n=1, cutoff=0.55)
    return close[0] if close else None


# ---------------------------------------------------------------------------
# LLM-based extraction from the call transcript
# ---------------------------------------------------------------------------

def extract_order_from_history(history: list[dict],
                                  menu: dict,
                                  llm_client_module,
                                  config: dict) -> dict:
    """Pass the call history + menu names to the LLM. Returns:
        {ok: True, items: [...], customer: {name, pickup_time}, notes: str}
    or
        {ok: False, error: <reason>, raw: <llm output>}.
    """
    if not history:
        return {"ok": False, "error": "empty_history"}

    items_flat = flatten_items(menu)
    if not items_flat:
        return {"ok": False, "error": "no_menu_items"}

    # Build a compact menu summary by category for the LLM
    menu_lines = []
    for cat in menu.get("categories", []):
        names = [it.get("name", "") for it in cat.get("items", [])]
        if names:
            menu_lines.append(f"  {cat.get('name','')}: {', '.join(names)}")
    menu_text = "\n".join(menu_lines)

    # Compact the transcript
    transcript_lines = []
    for m in history:
        role = "CALLER" if m.get("role") == "user" else "AGENT"
        transcript_lines.append(f"{role}: {m.get('content','')}")
    transcript = "\n".join(transcript_lines)

    system = (
        "You are an order-extraction parser. Read the phone-call transcript "
        "between a CALLER and an order-taking AGENT, and output the structured "
        "order as STRICT JSON only — no prose, no markdown fences.\n\n"
        f"MENU AVAILABLE:\n{menu_text}\n\n"
        "OUTPUT SHAPE (omit unknown fields by setting them to null or []):\n"
        "{\n"
        '  "items": [\n'
        "    {\n"
        '      "name": "<exact menu item name from the list above>",\n'
        '      "qty": 1,\n'
        '      "size": "<full or half or 12-inch or 6-inch or null>",\n'
        '      "bread": "<sourdough, wheat, italian roll, gluten-free, or null>",\n'
        '      "toasted": <true or false or null>,\n'
        '      "removes": ["onion", "tomato", ...],\n'
        '      "adds": ["bacon", "extra meat", ...],\n'
        '      "cheese": "<provolone, swiss, cheddar, pepper jack, no cheese, or null>",\n'
        '      "notes": "<freeform like \'cut in half\' or null>"\n'
        "    }\n"
        "  ],\n"
        '  "customer": {\n'
        '    "name": "<as given by caller>",\n'
        '    "pickup_time": "<as given — e.g. \'in 15 minutes\' or \'6:30 PM\'>"\n'
        "  },\n"
        '  "sms_receipt_consent": <true UNLESS the caller affirmatively REFUSED '
        'a text receipt ("no don\'t text me", "no SMS please", "no thanks on '
        'the text"). Default to true — receipts are sent unless the caller '
        'explicitly opted out, matching industry standard (DoorDash/Uber). '
        'Only set to false if caller explicitly refused>,\n'
        '  "notes": "<freeform call notes>"\n'
        "}\n\n"
        "Rules:\n"
        "- ONLY include items the caller actually decided to order. Ignore items "
        "  they considered but cancelled.\n"
        "- Use EXACT menu names from the list above. Map nicknames: 'Italian' → "
        "  'Truckee Italian', etc.\n"
        "- DRINK MAPPING (CRITICAL): If the caller names a specific drink brand "
        "  that's NOT on the menu (Sprite, Coke, Diet Coke, Pepsi, Dr Pepper, "
        "  7Up, Mountain Dew, Fanta, lemonade, root beer, etc.), MAP IT to the "
        "  closest GENERIC drink that IS on the menu. Usually 'Fountain Drink' "
        "  (for soda from the dispenser) or 'Bottled Drink' (for bottled brands). "
        "  Note the specific brand in the 'notes' field of that line so the "
        "  kitchen knows which one. Example: caller says 'a Sprite' → emit "
        "  {\"name\": \"Fountain Drink\", \"notes\": \"Sprite\"}.\n"
        "- Same idea for any other off-menu drink the caller mentions — match it "
        "  to the closest on-menu item rather than leaving it unmatched.\n"
        "- If no order was placed (the caller just asked questions and hung up), "
        "  return {\"items\": [], \"customer\": {\"name\": null, \"pickup_time\": null}, \"notes\": null}.\n"
        "- Output JSON ONLY. No explanation."
    )
    user = f"TRANSCRIPT:\n{transcript}\n\nExtract the JSON order now."

    try:
        resp = llm_client_module.generate(
            config, system, [{"role": "user", "content": user}],
        )
        raw = (resp.text or "").strip()
    except Exception as e:
        log.warning(f"extract: LLM call failed: {e}")
        return {"ok": False, "error": f"llm_failed: {e}"}

    # Strip markdown fences if the LLM ignored the "JSON only" rule
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)

    try:
        data = json.loads(raw)
    except Exception as e:
        # Last resort: try to find the first {...} block
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            log.warning(f"extract: JSON parse failed: {e}; raw={raw[:300]!r}")
            return {"ok": False, "error": "json_parse_failed", "raw": raw[:400]}
        try:
            data = json.loads(m.group(0))
        except Exception as e2:
            log.warning(f"extract: JSON parse failed (regex retry): {e2}")
            return {"ok": False, "error": "json_parse_failed", "raw": raw[:400]}

    return {
        "ok": True,
        "items": data.get("items") or [],
        "customer": data.get("customer") or {},
        "sms_receipt_consent": data.get("sms_receipt_consent"),
        "notes": data.get("notes") or "",
    }


# ---------------------------------------------------------------------------
# Cart builder — extracted JSON → web_driver shape
# ---------------------------------------------------------------------------

def build_cart_for_purblum(extracted_items: list[dict],
                              menu: dict) -> tuple[list[dict], list[str]]:
    """Convert LLM-extracted items into the web_driver cart shape.
       Returns (cart, warnings). Cart shape:
         [{name, qty, modifiers: [{group, action, label, delta}]}]"""
    items_flat = flatten_items(menu)
    cart, warnings = [], []

    for ext in extracted_items or []:
        spoken_name = ext.get("name") or ""
        match = match_item(spoken_name, items_flat)
        if not match:
            warnings.append(f"could not match item: {spoken_name!r}")
            continue

        modifier_groups = match.get("modifier_groups", []) or []
        mods: list[dict] = []

        # --- BREAD (single-choice) -----------------------------------------
        if ext.get("bread"):
            grp = next((g for g in modifier_groups
                          if g.get("name", "").lower().startswith("bread")), None)
            if grp:
                opt = _match_option(ext["bread"], grp.get("options", []) or [])
                if opt:
                    mods.append({
                        "group": grp["name"],
                        "action": "choose",
                        "label": opt["label"],
                        "delta": opt.get("price_delta", 0),
                    })

        # --- SIZE (single-choice) ------------------------------------------
        if ext.get("size"):
            grp = next((g for g in modifier_groups
                          if "size" in g.get("name", "").lower()), None)
            if grp:
                # Map "12-inch" / "whole" / "full" / "12 in" → match
                size_hint = ext["size"].lower()
                opt = _match_option(size_hint, grp.get("options", []) or [])
                if not opt:
                    if any(k in size_hint for k in ("12", "whole", "full", "large")):
                        opt = next((o for o in grp.get("options", []) or []
                                     if "12" in o.get("label", "")
                                     or "whole" in o.get("label", "").lower()), None)
                    elif any(k in size_hint for k in ("6", "half", "small")):
                        opt = next((o for o in grp.get("options", []) or []
                                     if "6" in o.get("label", "")
                                     or "half" in o.get("label", "").lower()), None)
                if opt:
                    mods.append({
                        "group": grp["name"],
                        "action": "choose",
                        "label": opt["label"],
                        "delta": opt.get("price_delta", 0),
                    })

        # --- CHEESE (single-choice) ----------------------------------------
        if ext.get("cheese"):
            grp = next((g for g in modifier_groups
                          if g.get("name", "").lower() == "cheese"), None)
            if grp:
                opt = _match_option(ext["cheese"], grp.get("options", []) or [])
                if opt:
                    mods.append({
                        "group": grp["name"],
                        "action": "choose",
                        "label": opt["label"],
                        "delta": opt.get("price_delta", 0),
                    })

        # --- TOASTED (multi-add — handled like an "adds" item) -------------
        if ext.get("toasted") is True:
            grp = next((g for g in modifier_groups
                          if g.get("type") == "multi"), None)
            if grp:
                opt = _match_option("toasted", grp.get("options", []) or [])
                if opt:
                    mods.append({
                        "group": grp["name"],
                        "action": "add",
                        "label": opt["label"],
                        "delta": opt.get("price_delta", 0),
                    })

        # --- REMOVES (uncheck defaults) ------------------------------------
        if ext.get("removes"):
            grp = next((g for g in modifier_groups
                          if g.get("type") == "remove"), None)
            if grp:
                defaults = grp.get("defaults_on", []) or []
                for r in ext["removes"]:
                    label = _match_remove_default(r, defaults)
                    if label:
                        mods.append({
                            "group": grp["name"],
                            "action": "remove",
                            "label": label,
                            "delta": 0,
                        })

        # --- ADDS (multi) --------------------------------------------------
        if ext.get("adds"):
            grp = next((g for g in modifier_groups
                          if g.get("type") == "multi"), None)
            if grp:
                for a in ext["adds"]:
                    opt = _match_option(a, grp.get("options", []) or [])
                    if opt:
                        mods.append({
                            "group": grp["name"],
                            "action": "add",
                            "label": opt["label"],
                            "delta": opt.get("price_delta", 0),
                        })

        cart.append({
            "name": match["name"],
            "qty": int(ext.get("qty") or 1),
            "modifiers": mods,
        })

    return cart, warnings


# ---------------------------------------------------------------------------
# Order summary for verbal confirmation + SMS receipt
# ---------------------------------------------------------------------------

def compute_cart_total(cart: list[dict], tax_rate: float = 0.0) -> dict:
    """Deterministic dollar math — no LLM in the loop. Sums every base
    price + every modifier price_delta, applies tax_rate. Returns:
        {subtotal, tax, total, lines: [{name, qty, line_total}], tax_rate_pct}
    All values are float dollars. Cart shape is what build_cart_for_purblum
    produces."""
    lines = []
    subtotal = 0.0
    for ci in cart or []:
        qty = int(ci.get("qty") or 1)
        name = ci.get("name", "")
        # Item base price — we don't always have it on the cart entry, so
        # accept it from a base_price field OR a price field, OR fall back
        # to 0 (caller should have set it).
        base = float(ci.get("base_price") or ci.get("price") or 0)
        delta_sum = 0.0
        for m in (ci.get("modifiers") or []):
            try:
                delta_sum += float(m.get("delta") or 0)
            except Exception:
                pass
        line_total = (base + delta_sum) * qty
        subtotal += line_total
        lines.append({"name": name, "qty": qty, "line_total": round(line_total, 2)})
    subtotal = round(subtotal, 2)
    tax = round(subtotal * float(tax_rate or 0), 2)
    total = round(subtotal + tax, 2)
    return {
        "subtotal": subtotal, "tax": tax, "total": total,
        "lines": lines,
        "tax_rate_pct": round(float(tax_rate or 0) * 100, 2),
    }


def annotate_cart_with_menu_prices(cart: list[dict], menu: dict) -> list[dict]:
    """Walk the cart and fill in base_price for each item by matching the
    canonical menu. Mutates + returns the cart for convenience."""
    items_flat = flatten_items(menu)
    for ci in cart or []:
        if ci.get("base_price") is not None:
            continue
        match = match_item(ci.get("name", ""), items_flat)
        if match and match.get("base_price") is not None:
            ci["base_price"] = float(match["base_price"])
    return cart


def render_order_summary(cart: list[dict], customer: dict,
                            order_id: str = "", total: str = "") -> str:
    """Plain-language summary of an order — used both for verbal readback
    on the phone AND the SMS receipt body."""
    if not cart:
        return "No items on the order."
    lines = []
    for ci in cart:
        line = f"{ci.get('qty',1)}x {ci.get('name','')}"
        mods = ci.get("modifiers", []) or []
        mod_bits = []
        for m in mods:
            act = m.get("action", "")
            lbl = m.get("label", "")
            if act == "choose":
                mod_bits.append(lbl)
            elif act == "remove":
                mod_bits.append(f"no {lbl.lower()}")
            elif act == "add":
                mod_bits.append(f"+{lbl.lower()}")
        if mod_bits:
            line += " (" + ", ".join(mod_bits) + ")"
        lines.append(line)
    summary = "; ".join(lines)
    extras = []
    if customer.get("name"):
        extras.append(f"for {customer['name']}")
    if customer.get("pickup_time"):
        extras.append(f"pickup {customer['pickup_time']}")
    if extras:
        summary += ". " + " — ".join(extras)
    if order_id:
        summary += f". Order #{order_id}"
    if total:
        summary += f". Total: {total}"
    return summary
