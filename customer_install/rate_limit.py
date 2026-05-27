"""
rate_limit — per-user daily caps on LLM calls.

Protects the owner's HuggingFace/brain budget from runaway loops (buggy
client code, voice mode stuck in a feedback loop, malicious visitor,
etc). Caps live in-memory and reset at midnight UTC. Hits are recorded
per-(user, day) so each user has their own bucket.

Defaults are deliberately generous — won't bother any normal user:
  - Owner:    1000 LLM calls / day
  - Staff:    500 LLM calls / day
  - Visitor:  200 LLM calls / day per IP

When over the limit, the caller (orbi.py) should return a friendly
"slow down" message without hitting the LLM. The caller picks the
identity key (username for logged-in, IP for visitors).
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger("orbi.rate_limit")

# default caps per role
LIMITS = {
    "owner":   1000,
    "staff":    500,
    "visitor":  200,
}

_LOCK = threading.Lock()
_COUNTS: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
# _COUNTS[utc_date_str][identity_key] = count_today
_LAST_DATE: str = ""


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _maybe_roll_day():
    """If the UTC date has changed since last write, clear yesterday's counts."""
    global _LAST_DATE
    today = _today_utc()
    if _LAST_DATE and _LAST_DATE != today:
        # Drop everything that isn't today
        for d in list(_COUNTS.keys()):
            if d != today:
                _COUNTS.pop(d, None)
        log.info(f"rate_limit: rolled day to {today}")
    _LAST_DATE = today


def check_and_increment(identity: str, role: str = "visitor") -> tuple[bool, int, int]:
    """Record one LLM call for this identity. Returns:
      (allowed: bool, used_today: int, limit: int)

    allowed=False means the caller should NOT make the LLM call this time.
    The count is still incremented so blocked attempts also count (prevents
    a hot loop from skirting the limit by retrying)."""
    if not identity:
        identity = "anonymous"
    cap = LIMITS.get(role, LIMITS["visitor"])
    with _LOCK:
        _maybe_roll_day()
        today = _today_utc()
        used = _COUNTS[today][identity] + 1
        _COUNTS[today][identity] = used
        allowed = used <= cap
        if not allowed:
            log.warning(f"rate_limit: BLOCKED {identity} ({role}) — {used}/{cap} today")
        return (allowed, used, cap)


def usage(identity: str) -> int:
    """How many LLM calls this identity made today."""
    with _LOCK:
        _maybe_roll_day()
        return _COUNTS[_today_utc()].get(identity, 0)


def snapshot() -> dict:
    """Owner-dashboard view: who's used what today."""
    with _LOCK:
        _maybe_roll_day()
        return {
            "date":   _today_utc(),
            "limits": dict(LIMITS),
            "by_identity": dict(_COUNTS[_today_utc()]),
        }
