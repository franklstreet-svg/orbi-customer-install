"""
modules/line_items — parse natural-language line item lists.

Foreman in the field will say things like:
  "3 plugs in the garage and 3 more lights in the kitchen"
  "2 outlets, one switch, and a ceiling fan"
  "add a recessed light over the island and rerun the dryer vent"

This module pulls those into structured rows:
  [{qty: 3, item: "plug", location: "garage", description: "3 plugs in the garage"},
   {qty: 3, item: "light", location: "kitchen", description: "3 lights in the kitchen"}]

If a pricing catalog is provided, each row gets unit_price + line_total
attached when a match is found.
"""
from __future__ import annotations

import re
from pathlib import Path

# Number-word conversions for "two outlets", "a ceiling fan"
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}

# Conjunctions that split items
_SPLIT_RE = re.compile(
    r"\s+(?:and|plus|,|;|&|/)\s+(?:also\s+)?(?:add\s+)?(?:another\s+)?(?:more\s+)?",
    re.IGNORECASE,
)

# Each item clause: "3 plugs in the garage" / "two outlets" / "a recessed light over the island"
_ITEM_RE = re.compile(
    r"^\s*(?:(\d+)|(" + "|".join(_NUMBER_WORDS.keys()) + r"))\s+"
    r"(?:more\s+|additional\s+|extra\s+|new\s+)?"
    r"(.+?)"
    r"(?:\s+(?:in|on|at|over|under|behind|near|by|for|to)\s+(?:the\s+)?(.+))?"
    r"\s*$",
    re.IGNORECASE,
)
# Fallback: clause with no number — assume qty=1 ("a recessed light over the island")
_FALLBACK_RE = re.compile(
    r"^\s*(?:add\s+|put\s+in\s+|install\s+)?"
    r"(?:a\s+|an\s+|the\s+)?"
    r"(.+?)"
    r"(?:\s+(?:in|on|at|over|under|behind|near|by|for|to)\s+(?:the\s+)?(.+))?"
    r"\s*$",
    re.IGNORECASE,
)


def parse(text: str, *, data_dir: Path | None = None) -> list[dict]:
    """Parse free-text into structured line items. If data_dir is given,
    each item is looked up against the pricing catalog and gets
    unit_price + line_total when a match is found."""
    if not text or not text.strip():
        return []
    # Split on conjunctions to get individual clauses
    clauses = [c.strip(" .,") for c in _SPLIT_RE.split(text.strip())
                if c.strip(" .,")]
    rows: list[dict] = []
    for clause in clauses:
        row = _parse_clause(clause)
        if row:
            rows.append(row)
    # Pricing lookup
    if data_dir is not None and rows:
        try:
            from modules import pricing as mod_pricing
            for r in rows:
                match = mod_pricing.find_price(data_dir, r["item"])
                if match:
                    r["catalog_id"]  = match["id"]
                    r["catalog_label"] = match["label"]
                    r["unit_price"]  = float(match["unit_price"])
                    r["unit"]        = match.get("unit", "each")
                    r["line_total"]  = round(r["qty"] * r["unit_price"], 2)
        except Exception:
            pass
    return rows


def _parse_clause(clause: str) -> dict | None:
    clause = clause.strip()
    if not clause:
        return None
    # Strip leading garbage like "Johnson —" / "for Mr. Smith —" / "$200 worth of"
    # — anything up to the first quantity word OR the first item-noun.
    # Looks for: first digit, OR first known number-word, OR first letter
    # of an article (a/an/the/some) that precedes an item.
    pre = re.match(
        r"^\s*(?:.+?[—–\-:])\s*"   # any text ended by a dash/colon
        r"(?=\d|\b(?:" + "|".join(_NUMBER_WORDS.keys()) + r")\b)",
        clause, re.IGNORECASE)
    if pre:
        clause = clause[pre.end():].strip()

    # Try main pattern with explicit quantity
    m = _ITEM_RE.match(clause)
    if m:
        qty_digit = m.group(1)
        qty_word = m.group(2)
        item = (m.group(3) or "").strip(" .,")
        location = (m.group(4) or "").strip(" .,") if m.group(4) else ""
        if not item:
            return None
        if qty_digit:
            qty = int(qty_digit)
        else:
            qty = _NUMBER_WORDS.get((qty_word or "").lower(), 1)
        return {
            "qty":         qty,
            "item":        _normalize_item(item, qty),
            "location":    location,
            "description": _build_description(qty, item, location),
        }

    # Fallback — no number, assume qty=1
    m = _FALLBACK_RE.match(clause)
    if m:
        item = (m.group(1) or "").strip(" .,")
        location = (m.group(2) or "").strip(" .,") if m.group(2) else ""
        if not item or len(item) < 2:
            return None
        # Filter out clauses that are pure conjunctions or noise
        if item.lower() in {"more", "add", "put in", "install", "another", "extra"}:
            return None
        return {
            "qty":         1,
            "item":        _normalize_item(item, 1),
            "location":    location,
            "description": _build_description(1, item, location),
        }
    return None


def _normalize_item(item: str, qty: int) -> str:
    """Strip plural 's' if qty=1, lowercase. Used for catalog lookup."""
    s = item.strip().lower()
    # Common short-form normalizations
    s = re.sub(r"^(?:additional|extra|new)\s+", "", s)
    if qty == 1:
        # Singularize crude — only strip 's' if word ends in non-s+s and doesn't look already-singular
        if len(s) > 3 and s.endswith("s") and not s.endswith("ss"):
            # Special case 'es' → strip 'es' for words like 'boxes', 'fixtures' → keep as 'fixture'? skip for now
            s = s.rstrip("s")
    return s


def _build_description(qty: int, item: str, location: str) -> str:
    """Build a human-readable line description for the CO."""
    item = item.strip()
    if qty == 1:
        # Re-singularize trailing s for display
        if len(item) > 3 and item.endswith("s") and not item.endswith("ss"):
            item = item.rstrip("s")
        article = "an" if item[:1].lower() in "aeiou" else "a"
        bits = [f"{article} {item}"]
    else:
        bits = [f"{qty} {item}"]
    if location:
        bits.append(f"in the {location}" if not location.lower().startswith(("the ", "in ", "on ", "at "))
                    else location)
    return " ".join(bits)


def format_for_co_description(rows: list[dict]) -> str:
    """Pretty multi-line CO description with totals if pricing was found."""
    if not rows:
        return ""
    lines = []
    grand = 0.0
    any_priced = any(r.get("line_total") is not None for r in rows)
    for r in rows:
        if r.get("line_total") is not None:
            lines.append(f"  · {r['qty']} × {r.get('catalog_label') or r['item']} "
                          f"@ ${r['unit_price']:,.2f} = ${r['line_total']:,.2f}"
                          + (f"  ({r['location']})" if r.get("location") else ""))
            grand += r["line_total"]
        else:
            lines.append(f"  · {r['description']}"
                          + " (no price on file)")
    if any_priced and grand > 0:
        lines.append(f"  Subtotal: ${grand:,.2f}")
    return "\n".join(lines)


def total(rows: list[dict]) -> float:
    """Sum line totals where pricing was found. Returns 0 if nothing priced."""
    return round(sum(float(r.get("line_total") or 0) for r in rows), 2)
