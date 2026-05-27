"""
follow_up — surfaces "you haven't replied to X in N days" items so leads
and customer questions don't fall through the cracks.

Approach: scan every data source that represents an incoming-to-owner
interaction (captured messages, learning_loop pending questions,
unread Gmail/Outlook threads if connected), compute days-since-touched,
and rank by staleness × importance.

Each "stale item" carries:
    - source       (messages / learning_loop / gmail / outlook / contacts)
    - id           (id within that source)
    - title        (short label for the dashboard row)
    - from         (who needs replying)
    - days_stale   (int)
    - urgency_score (0-100, see _score for weighting)
    - suggested_action  ("draft reply", "call back", "follow up on quote")
    - one_line_draft   (LLM-drafted nudge text the owner can send with one tap)

Surfaces in the dashboard as a "Needs Follow-Up" widget AND the morning
briefing pulls from this.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("orbi.follow_up")

DEFAULT_STALE_THRESHOLD_DAYS = 2
URGENT_TAG_BONUS  = 30
LEAD_TAG_BONUS    = 25
COMPLAINT_BONUS   = 35
UNREAD_BONUS      = 10


# ── Core stale-item finder ─────────────────────────────────────────────


def find_stale_items(config: dict, data_dir: Path, user_dir: Path | None = None,
                     min_days: int = DEFAULT_STALE_THRESHOLD_DAYS,
                     limit: int = 20) -> list[dict]:
    """Walk every reachable inbox and return items the owner hasn't replied
    to in at least `min_days` days. Sorted by urgency_score desc."""
    items = []
    items.extend(_from_messages(data_dir, min_days))
    items.extend(_from_learning_loop(data_dir, min_days))
    if user_dir:
        items.extend(_from_gmail(config, user_dir, min_days))
        items.extend(_from_outlook(config, user_dir, min_days))
    items.sort(key=lambda i: i.get("urgency_score", 0), reverse=True)
    return items[:limit]


def draft_nudge(config: dict, item: dict) -> str:
    """LLM-drafted short, friendly follow-up the owner can send (or edit
    + send) in one click. Returns plain text, no markdown."""
    if not item:
        return ""
    try:
        import llm_client
        system = (
            "You write short, warm follow-up nudges for a small-business owner. "
            "Output ONLY the nudge text, no preamble, no signature line, no "
            "subject — just the body of the message. Keep it 2-4 sentences. "
            "Sound human and friendly. Reference what they originally asked or "
            "what you promised. End with a clear next step."
        )
        prompt = _build_prompt(item)
        resp = llm_client.generate(config, system,
                                   [{"role": "user", "content": prompt}])
        return (resp.text or "").strip()
    except Exception as e:
        log.warning(f"nudge draft failed: {e}")
        name = item.get("from") or "there"
        return (f"Hi {name}, just following up on your message from "
                f"{item.get('days_stale','a few')} days ago. Did you still "
                f"want me to help with that? — let me know.")


# ── Internal: per-source scanners ───────────────────────────────────────


def _from_messages(data_dir: Path, min_days: int) -> list[dict]:
    try:
        from modules import messages as mod_messages
        msgs = mod_messages.list_all(data_dir, limit=500)
    except Exception as e:
        log.warning(f"messages source failed: {e}")
        return []
    out = []
    now = time.time()
    for m in msgs:
        if m.get("read"):
            continue
        age_secs = now - float(m.get("timestamp", 0) or 0)
        days = int(age_secs / 86400)
        if days < min_days:
            continue
        out.append({
            "source":     "messages",
            "id":         m.get("id"),
            "title":      _msg_title(m),
            "from":       m.get("from_name") or m.get("from_phone") or m.get("from_email") or "Unknown",
            "from_email": m.get("from_email"),
            "from_phone": m.get("from_phone"),
            "body":       m.get("body", "")[:300],
            "tags":       m.get("tags") or [],
            "days_stale": days,
            "urgency_score":     _score(days, m.get("tags") or [], unread=True),
            "suggested_action":  _suggest_action(m),
        })
    return out


def _from_learning_loop(data_dir: Path, min_days: int) -> list[dict]:
    try:
        from modules import learning_loop as mod_learning
        if not hasattr(mod_learning, "list_pending"):
            return []
        pending = mod_learning.list_pending(data_dir, limit=200)
    except Exception as e:
        log.warning(f"learning_loop source failed: {e}")
        return []
    out = []
    now = time.time()
    for q in pending:
        ts = float(q.get("asked_at_ts") or q.get("ts") or 0)
        if not ts:
            continue
        days = int((now - ts) / 86400)
        if days < min_days:
            continue
        out.append({
            "source":     "learning_loop",
            "id":         q.get("id"),
            "title":      f"Pending question: {(q.get('question') or '')[:60]}",
            "from":       (q.get("asker") or {}).get("name") or "a visitor",
            "from_email": (q.get("asker") or {}).get("email"),
            "from_phone": (q.get("asker") or {}).get("phone"),
            "body":       q.get("question", "")[:300],
            "tags":       ["question"],
            "days_stale": days,
            "urgency_score":     _score(days, ["question"], unread=True) + 5,
            "suggested_action":  "answer the question",
        })
    return out


def _from_gmail(config: dict, user_dir: Path, min_days: int) -> list[dict]:
    try:
        from connectors.base import get_instance
        inst = get_instance("gmail", config, user_dir)
        if inst is None or not inst.is_connected():
            return []
        # Pull unread inbox messages — Gmail's date math is server-side.
        # We can't easily query "unread for N+ days" without per-message
        # math; we'll fetch unread + check age client-side.
        msgs = inst.list_recent(limit=30)
    except Exception as e:
        log.debug(f"gmail source skipped: {e}")
        return []
    out = []
    for m in msgs:
        if not m.get("unread"):
            continue
        days = _days_since_email_date(m.get("date", ""))
        if days < min_days:
            continue
        out.append({
            "source":     "gmail",
            "id":         m.get("id"),
            "title":      m.get("subject") or "(no subject)",
            "from":       m.get("from") or "",
            "body":       m.get("snippet", "")[:300],
            "tags":       [],
            "days_stale": days,
            "urgency_score":     _score(days, [], unread=True),
            "suggested_action":  "reply",
        })
    return out


def _from_outlook(config: dict, user_dir: Path, min_days: int) -> list[dict]:
    try:
        from connectors.base import get_instance
        inst = get_instance("outlook", config, user_dir)
        if inst is None or not inst.is_connected():
            return []
        msgs = inst.list_recent(limit=30)
    except Exception as e:
        log.debug(f"outlook source skipped: {e}")
        return []
    out = []
    for m in msgs:
        if not m.get("unread"):
            continue
        days = _days_since_email_date(m.get("date", ""))
        if days < min_days:
            continue
        out.append({
            "source":     "outlook",
            "id":         m.get("id"),
            "title":      m.get("subject") or "(no subject)",
            "from":       m.get("from") or "",
            "body":       m.get("snippet", "")[:300],
            "tags":       [],
            "days_stale": days,
            "urgency_score":     _score(days, [], unread=True),
            "suggested_action":  "reply",
        })
    return out


# ── Scoring / formatting helpers ───────────────────────────────────────


def _score(days_stale: int, tags: list[str], unread: bool = False) -> int:
    """Higher = more important to follow up on. Caps at 100."""
    s = min(days_stale * 6, 60)  # staleness contributes up to 60
    if "urgent"    in tags: s += URGENT_TAG_BONUS
    if "lead"      in tags: s += LEAD_TAG_BONUS
    if "complaint" in tags: s += COMPLAINT_BONUS
    if unread:              s += UNREAD_BONUS
    return min(s, 100)


def _msg_title(m: dict) -> str:
    body = (m.get("body") or "").strip()
    if len(body) > 60:
        body = body[:57] + "..."
    return body or f"{(m.get('type') or 'message').title()} from {m.get('from_name') or '?'}"


def _suggest_action(m: dict) -> str:
    t = (m.get("type") or "").lower()
    return {
        "lead":      "send estimate / nudge",
        "callback":  "call them back",
        "voicemail": "return the call",
        "order":     "confirm or fulfill",
        "question":  "answer the question",
    }.get(t, "reply")


def _days_since_email_date(date_str: str) -> int:
    """Parse RFC 2822 / ISO email date strings and return days since now."""
    if not date_str:
        return 0
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() / 86400))
    except Exception:
        # Try ISO
        try:
            iso = re.sub(r"Z$", "+00:00", date_str)
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() / 86400))
        except Exception:
            return 0


def _build_prompt(item: dict) -> str:
    bits = [
        f"Source: {item.get('source')}",
        f"They wrote / asked: {item.get('body', '')[:400]}",
        f"It's been {item.get('days_stale')} days since.",
        f"Their name: {item.get('from')}",
    ]
    if item.get("tags"):
        bits.append(f"Categorized as: {', '.join(item['tags'])}")
    bits.append("Write a friendly 2-4 sentence follow-up nudge from the owner.")
    return "\n".join(bits)
