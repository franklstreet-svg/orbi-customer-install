"""
modules/wins — owner's personal wins / accomplishments log.

The compounding emotional moat: when the owner shares a win, Orby quietly
logs it. Later, when the owner sounds stressed or down, Orby pulls the
right past win and reminds them: "Remember last March when you were
worried about losing the Kowalski deal? You ended up landing two bigger
ones in May. Same energy now — you've got this."

A real friend who has WATCHED you succeed, holds your track record, and
plays it back during current struggles. No other AI assistant does this.

File: <user_dir>/wins.json
  {
    "wins": [
      {
        "id":          "w_abc123",
        "logged_at":   1780000000,
        "text":        "Closed the Maxwell deal — $47k contract",
        "category":    "deal" | "milestone" | "feedback" | "personal" | "growth" | "",
        "month_year":  "2026-05",
        "source":      "auto" | "manual"
      }
    ]
  }

Categories help with retrieval — when the owner is stressed about a
deal, pull a past deal win, not a "kid's recital" win.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

log = logging.getLogger("orbi.wins")

_LOCK = threading.Lock()
WINS_FILE = "wins.json"


def _path(user_dir: Path) -> Path:
    return Path(user_dir) / WINS_FILE


def _load(user_dir: Path) -> dict:
    p = _path(user_dir)
    if not p.exists():
        return {"wins": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "wins" not in data:
            return {"wins": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"wins": []}


def _save(user_dir: Path, data: dict) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def record_win(user_dir: Path, text: str, category: str = "",
               source: str = "manual") -> dict:
    """Append a win to the user's log. Returns the stored entry."""
    text = (text or "").strip()
    if not text or len(text) < 5:
        raise ValueError("win text too short")
    with _LOCK:
        data = _load(user_dir)
        now = datetime.now()
        entry = {
            "id":         "w_" + uuid.uuid4().hex[:8],
            "logged_at":  int(time.time()),
            "text":       text[:500],
            "category":   (category or _auto_category(text)).strip(),
            "month_year": now.strftime("%Y-%m"),
            "source":     source,
        }
        data["wins"].append(entry)
        _save(user_dir, data)
    return entry


def list_all(user_dir: Path, limit: int = 100) -> list[dict]:
    data = _load(user_dir)
    wins = sorted(data["wins"], key=lambda w: w.get("logged_at", 0), reverse=True)
    return wins[:limit]


def list_by_category(user_dir: Path, category: str, limit: int = 5) -> list[dict]:
    wins = list_all(user_dir, limit=200)
    return [w for w in wins if w.get("category") == category][:limit]


# ── Auto-categorization ───────────────────────────────────────────────────

_CATEGORY_KEYWORDS = {
    "deal":      ("closed", "signed", "landed", "won the", "got the contract",
                   "deal", "client", "sale", "sold", "booked", "invoice",
                   "commission"),
    "milestone": ("first", "100th", "1000th", "anniversary", "milestone",
                   "opened", "launched", "shipped", "released", "went live",
                   "year", "month"),
    "feedback":  ("five star", "5 star", "great review", "thank you",
                   "loved it", "amazing", "best", "compliment", "praise",
                   "testimonial", "shoutout"),
    "growth":    ("revenue", "profit", "up %", "doubled", "tripled",
                   "record", "all time", "best month", "highest", "broke",
                   "fastest"),
    "personal":  ("kid", "daughter", "son", "wife", "husband", "partner",
                   "family", "vacation", "trip", "weekend", "got engaged",
                   "got married", "moved into"),
}


def _auto_category(text: str) -> str:
    t = (text or "").lower()
    scores = {cat: 0 for cat in _CATEGORY_KEYWORDS}
    for cat, kws in _CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else ""


# ── Detection from chat ───────────────────────────────────────────────────

# Owner shared a win — covers explicit ("we just closed the X deal") and
# the more conversational ("just had our best month ever", "got the
# Maxwell signed, finally")
_WIN_RE = re.compile(
    r"\b(?:just\s+)?(?:we\s+|i\s+|we'?ve\s+|i'?ve\s+)?"
    r"(?:closed|signed|landed|won|booked|sold|finalized|launched|shipped|"
    r"hit|broke|crushed|nailed|beat|got\s+the|got\s+a\s+contract|"
    r"finally\s+(?:got|landed|signed|closed))"
    r"\s+[\w\$\d\.\,\-\s]{3,100}",
    re.IGNORECASE,
)

# "Best [X] [ever / this year / so far]"
_BEST_EVER_RE = re.compile(
    r"\b(?:best|biggest|highest|record|all[- ]time)\s+"
    r"(?:month|week|day|year|quarter|sale|deal|score|review|payday|paycheck)\s+"
    r"(?:ever|so\s+far|this\s+year|in\s+\w+|yet)?\b",
    re.IGNORECASE,
)


def detect_win_mention(message: str) -> str | None:
    """If the owner shared a win in this message, return the relevant
    substring to log. Returns None if no clear win signal."""
    msg = (message or "").strip()
    if not msg or len(msg) > 600:    # don't auto-log essays
        return None
    # Skip negations: "didn't close the deal", "lost the contract"
    if re.search(r"\b(didn'?t|didn'?t|never|lost|missed|failed|"
                 r"couldn'?t|wasn'?t able)\b", msg, re.IGNORECASE):
        return None
    m = _WIN_RE.search(msg) or _BEST_EVER_RE.search(msg)
    if not m:
        return None
    # Return a clean snippet — the matched span plus a few words around it,
    # capped at 200 chars
    start = max(0, m.start() - 20)
    end = min(len(msg), m.end() + 40)
    snippet = msg[start:end].strip()
    return snippet[:200]


# ── Retrieval for stressed-mode replies ───────────────────────────────────

# Owner sounds stressed / down — sentiment cues that should trigger pulling
# a relevant past win to inject into the LLM context.
_STRESS_CUES = re.compile(
    r"\b(stressed|burned\s*out|burnt\s*out|exhausted|drained|overwhelmed|"
    r"worried|anxious|scared|nervous|losing\s+sleep|can'?t\s+sleep|"
    r"falling\s+apart|breaking\s+down|don'?t\s+know\s+(?:if|how)|"
    r"might\s+lose|going\s+to\s+lose|worried\s+about\s+(?:losing|the)|"
    r"struggling|sucking|failing|tanking|hate\s+this|fed\s+up|"
    r"want\s+to\s+quit|thinking\s+about\s+(?:quitting|giving\s+up))\b",
    re.IGNORECASE,
)


def detect_stress(message: str) -> bool:
    """True if owner's message has stress/struggle cues that should
    trigger pulling a past win for emotional context."""
    return bool(_STRESS_CUES.search(message or ""))


def relevant_win_for_message(user_dir: Path, message: str,
                              max_wins: int = 2) -> list[dict]:
    """Pick the most relevant past wins to surface given the owner's
    current message. Heuristic: try to match category from message,
    fall back to most-recent wins if no category match."""
    wins = list_all(user_dir, limit=50)
    if not wins:
        return []

    msg_lower = (message or "").lower()
    # Try to detect category from the message
    target_cat = None
    if any(w in msg_lower for w in ("deal", "client", "sale", "contract", "customer")):
        target_cat = "deal"
    elif any(w in msg_lower for w in ("revenue", "money", "income", "profit", "broke")):
        target_cat = "growth"
    elif any(w in msg_lower for w in ("review", "feedback", "complaint", "rating")):
        target_cat = "feedback"
    elif any(w in msg_lower for w in ("family", "kid", "wife", "husband", "partner")):
        target_cat = "personal"

    if target_cat:
        same_cat = [w for w in wins if w.get("category") == target_cat]
        if same_cat:
            return same_cat[:max_wins]

    # Fallback — most recent wins regardless of category
    return wins[:max_wins]


def context_block(user_dir: Path, message: str) -> str:
    """When owner sounds stressed AND we have past wins, return a context
    block to prepend to the LLM prompt. Empty string otherwise."""
    if not detect_stress(message):
        return ""
    relevant = relevant_win_for_message(user_dir, message, max_wins=2)
    if not relevant:
        return ""
    lines = ["PAST WIN(S) WORTH REMINDING THEM ABOUT — they sound stressed "
             "right now; weave ONE of these in if it lands naturally, don't "
             "force it. Reference the actual story, not the date:"]
    for w in relevant:
        when = w.get("month_year", "")
        lines.append(f"  · ({when}) {w.get('text', '')}")
    return "\n".join(lines)
