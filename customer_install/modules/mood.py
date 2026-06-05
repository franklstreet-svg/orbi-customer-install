"""
mood — emotional signal detection + per-user mood log.

The infrastructure layer for personal-Orby's emotional intelligence.
Runs on every owner_chat turn (when companion_mode == "personal"),
detects emotional signals in the owner's message, and logs them to
mood_log.json under the user's data folder.

Two responsibilities:

  1. detect(message) — fast keyword/regex classifier, returns a signal
     dict {kind, intensity, snippet} or None. No LLM, sub-millisecond.

  2. log(user_dir, signal, source_msg) — append to mood_log.json. Used
     by recall() to surface emotional context to the LLM and by
     check_in_due() to identify when to proactively follow up.

Design notes:
- Stays out of the way when off (companion_mode != "personal" → never
  runs).
- Customer-facing paths (public chat, voice receptionist) NEVER touch
  this. EI is owner-only by architecture, not just by setting.
- Storage is per-user under <user_dir>/mood_log.json. No global mood
  data. Atomic writes via .tmp + replace.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.Lock()
MOOD_FILE = "mood_log.json"

# Negative signals — listed in roughly increasing intensity. The first
# match wins, so longer/more specific phrases come first within each
# intensity tier.
_NEG_PATTERNS: list[tuple[str, str, float]] = [
    # (regex, label, intensity 0..1)
    (r"\b(?:want to (?:kill|end) myself|don'?t want to live|suicid)", "crisis", 1.0),
    (r"\b(?:can'?t take (?:this|it) anymore|at the end of my rope)", "crisis", 0.95),
    (r"\b(?:completely overwhelmed|drowning|breaking down|losing it)", "overwhelmed", 0.9),
    (r"\b(?:burnt out|burned out|burnout)", "burnt_out", 0.85),
    (r"\b(?:hate (?:this|my|everything)|fed up|done with this)", "fed_up", 0.8),
    (r"\b(?:exhausted|wiped out|spent|wrecked)", "exhausted", 0.75),
    (r"\b(?:so (?:tired|stressed|frustrated|annoyed))", "stressed", 0.75),
    (r"\b(?:freaking out|panic|anxiety attack)", "anxious", 0.75),
    (r"\b(?:rough day|tough day|bad day|long day|brutal day)", "rough_day", 0.7),
    (r"\b(?:worried about|concerned about|nervous about|stressing about)", "worried", 0.65),
    (r"\b(?:stressed|frustrated|annoyed|fed up|down)", "stressed", 0.6),
    (r"\b(?:tired|drained|beat|worn out)", "tired", 0.55),
    (r"\b(?:not great|not good|kinda rough|kind of rough|meh)", "low", 0.4),
]

# Positive signals
_POS_PATTERNS: list[tuple[str, str, float]] = [
    (r"\b(?:best day|amazing day|crushed it|killed it|on fire)", "huge_win", 0.9),
    (r"\b(?:first \$\d+|biggest day|record day|milestone)", "milestone", 0.85),
    (r"\b(?:so (?:happy|excited|proud|stoked|pumped))", "elated", 0.8),
    (r"\b(?:huge|massive|fantastic|incredible|awesome)\b", "great", 0.7),
    (r"\b(?:happy|excited|proud|stoked|pumped|grateful)", "happy", 0.65),
    (r"\b(?:good day|great day|productive day|nice day)", "good_day", 0.6),
    (r"\b(?:feeling good|doing well|going well|all good)", "doing_well", 0.55),
]


def detect(message: str) -> dict | None:
    """Lightweight classifier. Returns {kind, intensity, valence,
    snippet} or None when no signal is detected.

    valence: "neg" | "pos" — so callers can branch (concerned response
    vs celebratory response) without re-checking the kind list.
    """
    if not message:
        return None
    low = " ".join(message.lower().split())
    if len(low) > 2000:
        low = low[:2000]
    # Negative first — crisis cues take priority over any positive coincidence
    for pat, kind, intensity in _NEG_PATTERNS:
        m = re.search(pat, low)
        if m:
            return {"kind": kind, "intensity": intensity,
                    "valence": "neg", "snippet": m.group(0)}
    for pat, kind, intensity in _POS_PATTERNS:
        m = re.search(pat, low)
        if m:
            return {"kind": kind, "intensity": intensity,
                    "valence": "pos", "snippet": m.group(0)}
    return None


def _path(user_dir: Path) -> Path:
    return Path(user_dir) / MOOD_FILE


def _load(user_dir: Path) -> list[dict]:
    p = _path(user_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(user_dir: Path, entries: list[dict]) -> None:
    p = _path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    tmp.replace(p)


def log(user_dir: Path, signal: dict, source_msg: str = "") -> dict:
    """Append a mood entry. Returns the saved entry.
    Caller has already gated on companion_mode == 'personal'."""
    entry = {
        "id":        uuid.uuid4().hex[:12],
        "ts":        time.time(),
        "kind":      signal.get("kind", ""),
        "intensity": float(signal.get("intensity", 0.0)),
        "valence":   signal.get("valence", ""),
        "snippet":   (signal.get("snippet") or "")[:200],
        "source":    (source_msg or "")[:400],
    }
    with _LOCK:
        entries = _load(user_dir)
        entries.append(entry)
        # Cap at 500 entries to keep the file small. Rolling window of
        # the most recent emotional context — older stuff falls off.
        if len(entries) > 500:
            entries = entries[-500:]
        _save(user_dir, entries)
    return entry


def recent(user_dir: Path, days: int = 14,
            valence: str | None = None) -> list[dict]:
    """Pull mood entries from the last `days`. Newest first."""
    cutoff = time.time() - (days * 86400)
    entries = [e for e in _load(user_dir) if e.get("ts", 0) >= cutoff]
    if valence:
        entries = [e for e in entries if e.get("valence") == valence]
    return sorted(entries, key=lambda e: e.get("ts", 0), reverse=True)


def context_block(user_dir: Path, max_lines: int = 5) -> str:
    """Format recent emotional context as a small LLM system-prompt
    addendum. Returns "" if nothing notable in the last two weeks.
    Caller has already gated on companion_mode == 'personal'."""
    from datetime import datetime, timezone
    entries = recent(user_dir, days=14)
    if not entries:
        return ""
    # Surface meaningful entries — drop the noise (intensity < 0.5)
    notable = [e for e in entries if e.get("intensity", 0) >= 0.5]
    if not notable:
        return ""
    # Group by kind so we can say "they've mentioned 'stressed' 3 times
    # this week" instead of listing every instance.
    from collections import Counter
    kinds = Counter(e.get("kind") for e in notable)

    lines = ["RECENT EMOTIONAL CONTEXT (use sparingly, only when relevant):"]
    # Most recent significant
    e = notable[0]
    when_ts = e.get("ts", 0)
    try:
        when = datetime.fromtimestamp(when_ts, tz=timezone.utc).astimezone().strftime("%a %b %-d")
    except (ValueError, OSError):
        when = "recently"
    snippet = e.get("snippet") or e.get("kind", "")
    valence_word = "rough" if e.get("valence") == "neg" else "good"
    lines.append(
        f"- Most recent: on {when} you noted them saying \"{snippet}\" "
        f"(a {valence_word} signal)."
    )
    # Patterns
    repeats = [(k, n) for k, n in kinds.items() if n >= 2 and k]
    if repeats:
        repeats.sort(key=lambda x: -x[1])
        for k, n in repeats[:max_lines - 1]:
            lines.append(
                f"- They've expressed \"{k}\" {n} times in the last 14 days."
            )
    lines.append(
        "Use this to inform your tone (gentler if recent rough signals; "
        "celebratory follow-up if recent wins). Do NOT recite this back; "
        "weave it into a single natural reference at most."
    )
    return "\n".join(lines)


def check_in_due(user_dir: Path) -> dict | None:
    """If the user had a strong negative signal 1-3 days ago AND nothing
    since, return that entry — caller can use it to seed a check-in
    ("hey, you mentioned that rough thing on Tuesday — how's it now?")."""
    entries = _load(user_dir)
    if not entries:
        return None
    now = time.time()
    # Find a strong neg signal in the 1-3 day window
    one_day  = now - 86400
    three_day = now - (3 * 86400)
    candidate = None
    for e in reversed(entries):  # newest first
        ts = e.get("ts", 0)
        if ts > one_day:
            # Too recent — they're probably still in that state
            continue
        if ts < three_day:
            break
        if e.get("valence") == "neg" and e.get("intensity", 0) >= 0.7:
            candidate = e
            break
    if not candidate:
        return None
    # Skip if there's been a more recent positive signal — they've
    # already moved on.
    for e in reversed(entries):
        if e.get("ts", 0) > candidate.get("ts", 0):
            if e.get("valence") == "pos" and e.get("intensity", 0) >= 0.6:
                return None  # they bounced back, no need to check in
    return candidate
