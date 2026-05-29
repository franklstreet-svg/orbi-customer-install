"""
modules/gifts — per-user gift history + taste learning.

Stores every gift the owner has given (auto-detected from chat OR
explicitly logged) along with the recipient, occasion, rough cost, and
eventual outcome ("loved", "fine", "missed"). Over time this becomes
the owner's gift TASTE profile that suggest_gift() pulls into its LLM
context — six months in, Orby knows the owner prefers experiences over
objects, leans $50-100 for friends, $200+ for spouse, and never wins
with kitchen gadgets.

File: <user_dir>/gifts.json
  {
    "gifts": [
      {
        "id":          "g_abc123def4",
        "recipient":   "Kathy",
        "recipient_id": "contact_xyz" or null,
        "relationship": "spouse" | "kid" | "friend" | "coworker" | "parent" | "",
        "occasion":    "birthday" | "anniversary" | "christmas" | "graduation" | "...",
        "item":        "concert tickets",
        "rough_cost":  "$150",
        "year":        2026,
        "given_at":    1780000000,
        "outcome":     "loved" | "fine" | "missed" | null,
        "outcome_note": "She talked about it for weeks." (optional)
      }
    ]
  }
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path


_LOCK = threading.Lock()
GIFTS_FILE = "gifts.json"


def _path(user_dir: Path) -> Path:
    return Path(user_dir) / GIFTS_FILE


def _load(user_dir: Path) -> dict:
    p = _path(user_dir)
    if not p.exists():
        return {"gifts": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "gifts" not in data:
            return {"gifts": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"gifts": []}


def _save(user_dir: Path, data: dict) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def record_gift(user_dir: Path, *, recipient: str, occasion: str,
                item: str, rough_cost: str = "",
                relationship: str = "", recipient_id: str = "",
                outcome: str = "", outcome_note: str = "") -> dict:
    """Append a gift to the user's history. Returns the stored entry."""
    if not recipient or not item:
        raise ValueError("recipient + item required")
    with _LOCK:
        data = _load(user_dir)
        from datetime import datetime
        entry = {
            "id":           uuid.uuid4().hex[:10],
            "recipient":    recipient.strip(),
            "recipient_id": recipient_id.strip(),
            "relationship": relationship.strip(),
            "occasion":     (occasion or "").strip(),
            "item":         item.strip(),
            "rough_cost":   rough_cost.strip(),
            "year":         datetime.now().year,
            "given_at":     int(time.time()),
            "outcome":      (outcome or "").strip() or None,
            "outcome_note": (outcome_note or "").strip(),
        }
        data["gifts"].append(entry)
        _save(user_dir, data)
    return entry


def record_outcome(user_dir: Path, gift_id: str,
                   outcome: str, note: str = "") -> bool:
    """Mark how a previously-given gift went. Returns True if found + updated."""
    with _LOCK:
        data = _load(user_dir)
        for g in data["gifts"]:
            if g.get("id") == gift_id:
                g["outcome"] = (outcome or "").strip() or None
                if note:
                    g["outcome_note"] = note.strip()
                _save(user_dir, data)
                return True
    return False


def list_all(user_dir: Path, limit: int = 50) -> list[dict]:
    data = _load(user_dir)
    gifts = sorted(data["gifts"], key=lambda g: g.get("given_at", 0), reverse=True)
    return gifts[:limit]


def list_for_recipient(user_dir: Path, recipient: str,
                       limit: int = 10) -> list[dict]:
    """Past gifts to a specific person. Case-insensitive name match."""
    if not recipient:
        return []
    target = recipient.strip().lower()
    data = _load(user_dir)
    matching = [g for g in data["gifts"]
                if g.get("recipient", "").lower() == target]
    matching.sort(key=lambda g: g.get("given_at", 0), reverse=True)
    return matching[:limit]


def taste_summary(user_dir: Path) -> str:
    """Build a short LLM-friendly summary of the owner's gift patterns
    for injection into suggest_gift system prompts. Returns "" if no
    history."""
    gifts = list_all(user_dir, limit=30)
    if not gifts:
        return ""

    lines = ["OWNER'S PAST GIFT PATTERNS (use these to weight suggestions):"]

    # Wins (loved) vs misses
    wins = [g for g in gifts if g.get("outcome") == "loved"]
    misses = [g for g in gifts if g.get("outcome") == "missed"]
    if wins:
        lines.append(f"- {len(wins)} gift(s) labeled 'loved':")
        for g in wins[:5]:
            cost = f" ({g['rough_cost']})" if g.get("rough_cost") else ""
            lines.append(f"    · '{g['item']}' for {g['recipient']} "
                          f"({g.get('occasion','')}){cost}")
    if misses:
        lines.append(f"- {len(misses)} gift(s) labeled 'missed' — AVOID similar:")
        for g in misses[:5]:
            lines.append(f"    · '{g['item']}' for {g['recipient']}")

    # Common categories the owner tends to give
    items = [g.get("item", "").lower() for g in gifts if g.get("item")]
    keywords = {
        "experience":  ["concert", "tickets", "trip", "weekend", "dinner",
                         "spa", "show", "event", "class", "lesson"],
        "object":      ["watch", "necklace", "ring", "earrings", "bracelet",
                         "book", "tools", "gadget", "device"],
        "consumable":  ["wine", "whiskey", "chocolates", "flowers", "candle"],
        "handmade":    ["handmade", "handwritten", "letter", "card", "photo album"],
    }
    leanings = []
    for cat, kws in keywords.items():
        count = sum(1 for it in items if any(kw in it for kw in kws))
        if count >= 2:
            leanings.append(f"{cat}({count})")
    if leanings:
        lines.append(f"- Tends toward: {', '.join(leanings)}")

    return "\n".join(lines)


def detect_logged_gift(message: str) -> dict | None:
    """Best-effort regex auto-detector for chat messages like:
        "I got Kathy a necklace for her birthday, around $80"
        "Bought my mom flowers for Mother's Day"
        "Picked up concert tickets for Joe's anniversary"
    Returns {recipient, item, occasion, rough_cost} or None.
    """
    import re
    msg = (message or "").strip()
    if not msg:
        return None

    OCCASION = (r"(?P<occasion>birthday|anniversary|christmas|graduation|"
                r"mother'?s\s+day|father'?s\s+day|valentine'?s\s+day|"
                r"wedding|baby\s+shower)")
    # Recipient = either a Proper Name (case-sensitive, no IGNORECASE on this
    # group) OR "my <relation>" (compound noun, captured as a single unit).
    REL = (r"my\s+(?:wife|husband|spouse|mom|dad|mother|father|son|"
            r"daughter|partner|girlfriend|boyfriend|brother|sister|kid|"
            r"in[- ]?law|grandma|grandpa|grandmother|grandfather|nephew|niece|"
            r"aunt|uncle|cousin|friend|coworker|boss)")
    NAME = r"[A-Z][a-zA-Z]{1,30}"   # case-sensitive — pronouns ('she') excluded
    VERB = r"(?:got|gave|bought|picked\s+up|grabbed|ordered|sent|made)"

    # Pattern A: verb + RECIPIENT + (a/an/the/some) + ITEM + for + occasion
    pat_a = re.compile(
        r"\b(?:i\s+)?" + VERB + r"\s+"
        r"(?P<recipient>" + REL + r"|" + NAME + r")\s+"
        r"(?:a\s+|an\s+|the\s+|some\s+)?"
        r"(?P<item>[a-z][a-z' \-]{2,40}?)\s+"
        r"for\s+(?:her\s+|his\s+|their\s+)?" + OCCASION,
        re.IGNORECASE)

    # Pattern B: verb + (a/an/the/some) + ITEM + for + RECIPIENT('s) + occasion
    # Handles "picked up concert tickets for Joe's anniversary"
    pat_b = re.compile(
        r"\b(?:i\s+)?" + VERB + r"\s+"
        r"(?:a\s+|an\s+|the\s+|some\s+)?"
        r"(?P<item>[a-z][a-z' \-]{2,40}?)\s+"
        r"for\s+(?P<recipient>" + REL + r"|" + NAME + r")(?:'s|\s)\s*"
        + OCCASION,
        re.IGNORECASE)

    m = pat_a.search(msg) or pat_b.search(msg)
    if not m:
        return None
    out = {
        "recipient": m.group("recipient").strip(),
        "item":      m.group("item").strip().rstrip(".,;:"),
        "occasion":  m.group("occasion").lower(),
    }
    # Optional cost capture: "around $80", "$50", "about 100 bucks"
    cost_m = re.search(
        r"(?:around\s+|about\s+|roughly\s+)?\$\s*(\d+(?:\.\d{1,2})?)|(\d+)\s+bucks?",
        msg, re.IGNORECASE)
    if cost_m:
        amount = cost_m.group(1) or cost_m.group(2)
        out["rough_cost"] = f"${amount}"
    return out


def detect_outcome_mention(message: str) -> dict | None:
    """Detect when owner mentions how a previous gift went:
        "Kathy loved the necklace"
        "Joe didn't really get into the book I gave him"
    Returns {recipient, outcome} or None. Recipient must be a proper
    name — pronouns (she/he/they) are excluded since they don't tie
    back to a specific stored gift.
    """
    import re
    msg = (message or "").strip()
    if not msg:
        return None
    # Recipient = proper name only (case-sensitive — NO ignore flag on this
    # group). This is achieved with a Unicode character class without the
    # IGNORECASE-affected [A-Z].
    pat = re.compile(
        r"\b(?P<recipient>[A-Z][a-zA-Z]{1,30})\s+"
        r"(?P<verb>loved|hated|adored|liked|disliked|didn'?t\s+(?:really\s+)?like|"
        r"didn'?t\s+(?:really\s+)?get\s+into|threw\s+(?:it\s+|that\s+)?out|"
        r"returned|never\s+used)\b"
    )    # NO re.IGNORECASE — keeps the leading capital requirement strict
    m = pat.search(msg)
    if not m:
        return None
    verb = m.group("verb").lower()
    if any(w in verb for w in ("loved", "adored")):
        outcome = "loved"
    elif verb in ("liked",):
        outcome = "fine"
    else:
        outcome = "missed"
    return {"recipient": m.group("recipient"), "outcome": outcome}
