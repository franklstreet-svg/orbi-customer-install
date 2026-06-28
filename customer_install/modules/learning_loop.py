"""
learning_loop module — Orbi's compounding-knowledge feature.

The flow (per Frank's spec, 2026-05-26):

  1. Visitor asks Orbi something.
  2. Orbi checks: learned_answers.json + business profile + catalog + workspace.
     If NO match (Orbi truly doesn't know), this module fires.
  3. Orbi politely tells the visitor she's not sure and asks for their
     name + best way to reach them (phone/email/text).
  4. The question + visitor contact info is saved as a PENDING question
     in pending_questions.json.
  5. The owner gets notified through their preferred channel (email, SMS,
     PWA push, or phone call). The notification includes a unique token
     so when the owner replies, we can link the answer back.
  6. Owner answers — either by replying to the email/SMS, or by entering
     the answer in their owner dashboard.
  7. The Q+A is saved to learned_answers.json with verified=True. Every
     future visitor with the same question gets the answer INSTANTLY.
  8. The original visitor gets the answer delivered via their preferred
     channel ("Hi Sarah, Mike got back to me — yes, we have left-handed
     flange wrenches, part #LH-FW-12, $8.95 each, 4 in stock.").

All data stays on the customer's machine inside the `Orby/` folder.
Never the cloud.

Storage:
  Orby/data/pending_questions.json   ← unanswered, waiting on the owner
  Orby/data/learned_answers.json     ← answered, verified, permanent

Per-question structure in pending_questions.json:

  {
    "token": "Q_abc12def34",
    "question": "Do you have left-handed flange wrenches?",
    "question_normalized": "do you have left handed flange wrenches",
    "asked_at": "2026-05-26T15:30:00Z",
    "asker": {
      "name": "Sarah Chen",
      "phone": "+17755550123",
      "email": null,
      "preferred_channel": "sms",
      "session_id": "visitor_abc123"
    },
    "owner_notified_at": "2026-05-26T15:30:02Z",
    "owner_notify_channel": "email",
    "status": "awaiting_answer",
    "asked_count": 1
  }
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_LOCK = threading.Lock()

PENDING_FILE = "pending_questions.json"
LEARNED_FILE = "learned_answers.json"


# ── Public API ───────────────────────────────────────────────────────────


def find_learned(data_dir: Path, question: str) -> dict | None:
    """Check learned_answers.json for a verified answer to this question.
    Returns the learned-answer dict if found, None otherwise. Called from
    /chat BEFORE the LLM — the never-guess pattern."""
    norm = _normalize(question)
    if not norm:
        return None
    items = _read(data_dir / LEARNED_FILE, [])
    for it in items:
        if it.get("verified") and it.get("question_normalized") == norm:
            # Bump usage counters (best-effort, atomic write)
            try:
                it["asked_count"] = (it.get("asked_count") or 0) + 1
                it["last_asked_at"] = _now_iso()
                _write(data_dir / LEARNED_FILE, items)
            except Exception:
                pass
            return it
    return None


def capture_pending(data_dir: Path, question: str, asker: dict,
                    session_id: str = "") -> dict:
    """Save a new pending question. Returns the saved record (with token).
    Idempotent for the same (question_normalized, asker.phone-or-email) —
    if the same person asks the same question twice while it's still
    pending, we bump asked_count instead of creating a duplicate."""
    if not question or not question.strip():
        raise ValueError("question is empty")
    norm = _normalize(question)
    asker = dict(asker or {})
    asker_key = (asker.get("phone") or asker.get("email") or "").strip().lower()

    with _LOCK:
        pending = _read(data_dir / PENDING_FILE, [])
        # Check for an existing pending question from the same asker
        for p in pending:
            if (p.get("question_normalized") == norm
                and _asker_key(p.get("asker") or {}) == asker_key
                and p.get("status") == "awaiting_answer"):
                p["asked_count"] = (p.get("asked_count") or 1) + 1
                p["last_asked_at"] = _now_iso()
                _write(data_dir / PENDING_FILE, pending)
                return p

        record = {
            "token": _new_token(),
            "question": question.strip(),
            "question_normalized": norm,
            "asked_at": _now_iso(),
            "asker": {
                "name":  (asker.get("name") or "").strip(),
                "phone": (asker.get("phone") or "").strip(),
                "email": (asker.get("email") or "").strip(),
                "preferred_channel": (asker.get("preferred_channel") or "").strip(),
                "session_id": session_id,
            },
            "owner_notified_at": None,
            "owner_notify_channel": None,
            "status": "awaiting_answer",
            "asked_count": 1,
        }
        pending.append(record)
        _write(data_dir / PENDING_FILE, pending)
        return record


def mark_owner_notified(data_dir: Path, token: str, channel: str) -> bool:
    """Called by the dispatcher after the owner notification has been sent."""
    with _LOCK:
        pending = _read(data_dir / PENDING_FILE, [])
        for p in pending:
            if p.get("token") == token:
                p["owner_notified_at"] = _now_iso()
                p["owner_notify_channel"] = channel
                _write(data_dir / PENDING_FILE, pending)
                return True
    return False


def answer_pending(data_dir: Path, token: str, answer: str,
                   answered_by: str = "owner") -> dict | None:
    """The owner has provided an answer. Move the question from
    pending_questions.json to learned_answers.json (verified=True),
    leave a 'delivery_pending' marker so the customer can be notified.
    Returns the updated learned-answer record (with .original_asker
    populated so the caller can dispatch the customer callback)."""
    answer = (answer or "").strip()
    if not answer:
        raise ValueError("answer is empty")

    with _LOCK:
        pending = _read(data_dir / PENDING_FILE, [])
        idx = next((i for i, p in enumerate(pending) if p.get("token") == token), -1)
        if idx < 0:
            return None
        record = pending.pop(idx)
        _write(data_dir / PENDING_FILE, pending)

        learned = _read(data_dir / LEARNED_FILE, [])
        # Replace any existing entry for the same normalized question
        # (an older unverified answer, or a previous question's record).
        learned = [l for l in learned
                   if l.get("question_normalized") != record["question_normalized"]]

        learned_record = {
            "token": record["token"],
            "question": record["question"],
            "question_normalized": record["question_normalized"],
            "answer": answer,
            "answered_at": _now_iso(),
            "answered_by": answered_by,
            "verified": True,
            "asked_count": record.get("asked_count", 1),
            "last_asked_at": record.get("last_asked_at") or record.get("asked_at"),
            "original_asker": record.get("asker") or {},
            "delivery_status": "pending",  # customer hasn't been notified yet
            "delivery_at": None,
        }
        learned.append(learned_record)
        _write(data_dir / LEARNED_FILE, learned)
        return learned_record


def mark_delivered(data_dir: Path, token: str, channel: str) -> bool:
    """Called by the dispatcher after the customer-callback message has
    been sent. Marks delivery_status=delivered so we don't re-deliver."""
    with _LOCK:
        learned = _read(data_dir / LEARNED_FILE, [])
        for l in learned:
            if l.get("token") == token:
                l["delivery_status"] = "delivered"
                l["delivery_at"] = _now_iso()
                l["delivery_channel"] = channel
                _write(data_dir / LEARNED_FILE, learned)
                return True
    return False


def list_pending(data_dir: Path) -> list[dict]:
    """For the owner-dashboard view — what's waiting on an answer."""
    pending = _read(data_dir / PENDING_FILE, [])
    return sorted(pending, key=lambda p: p.get("asked_at", ""), reverse=True)


def list_learned(data_dir: Path, limit: int | None = None) -> list[dict]:
    """All verified Q+A. Newest first. Used by the dashboard + admin tools."""
    learned = _read(data_dir / LEARNED_FILE, [])
    learned.sort(key=lambda l: l.get("answered_at", ""), reverse=True)
    if limit:
        return learned[:limit]
    return learned


def list_undelivered(data_dir: Path) -> list[dict]:
    """Learned answers whose original asker hasn't been notified yet.
    Used by the customer-callback dispatcher.

    Status MUST be 'pending' AND we must have at least one usable
    contact channel (phone or email). The parens here matter — without
    them Python's `and` binds tighter than `or`, which let delivered
    records slip back into the list whenever they had an email."""
    out = []
    for l in _read(data_dir / LEARNED_FILE, []):
        if l.get("delivery_status") != "pending":
            continue
        asker = l.get("original_asker") or {}
        if not (asker.get("phone") or asker.get("email")):
            continue
        out.append(l)
    return out


def find_pending(data_dir: Path, token: str) -> dict | None:
    """Lookup a single pending question by token."""
    for p in _read(data_dir / PENDING_FILE, []):
        if p.get("token") == token:
            return p
    return None


# ── Confidence detection — "does Orbi actually know this?" ───────────────


# Patterns that strongly indicate Orbi is bluffing / declining / hedging.
# When the LLM produces a reply matching any of these, we treat the
# question as UNKNOWN and route into the learning loop.
_BLUFF_PATTERNS = [
    r"\bi['']?m\s+not\s+(?:sure|certain|positive)\b",
    r"\bi\s+don['']?t\s+(?:have|know|see)\b",
    r"\bi\s+couldn['']?t\s+find\b",
    r"\bnot\s+sure\b",
    r"\bunable\s+to\s+(?:find|locate|determine)\b",
    r"\bdon['']?t\s+have\s+(?:the\s+)?(?:exact|specific|complete)\b",
    r"\bcan['']?t\s+say\s+for\s+sure\b",
    r"\bi['']?d\s+(?:have\s+to\s+)?check\s+with\b",
    r"\blet\s+me\s+check\s+with\s+(?:mike|the\s+owner|the\s+team)\b",
    r"\bi['']?ll\s+(?:have\s+to\s+)?get\s+back\b",
]
_BLUFF_RE = re.compile("|".join(_BLUFF_PATTERNS), re.IGNORECASE)


def reply_indicates_unknown(reply: str) -> bool:
    """Heuristic — does this LLM reply read like 'I don't know'? If yes,
    we should kick into the learning loop instead of letting Orbi guess."""
    if not reply or len(reply) < 5:
        return True  # empty reply == she definitely doesn't know
    return bool(_BLUFF_RE.search(reply))


def is_question_form(message: str) -> bool:
    """Is the visitor actually asking something (vs greeting / chit-chat)?
    We only escalate to the owner for real questions."""
    if not message or len(message.strip()) < 3:
        return False
    msg = message.strip().lower()
    if msg.endswith("?"):
        return True
    starters = ("do you", "does ", "is ", "are ", "can ", "could ", "would ",
                "will ", "what", "when", "where", "why", "how", "who",
                "i need", "looking for", "looking at", "have any",
                "carry any", "stock any", "available")
    return any(msg.startswith(s) for s in starters)


_GENERAL_KNOWLEDGE_KEYWORDS = (
    "weather", "forecast", "temperature", "rain", "snow", "sunny", "cloudy",
    "hot", "cold", "humid", "wind",
    "recipe", "how to cook", "how do i cook", "how do i make",
    "what is ", "what's a ", "what are ", "who is ", "who was ",
    "when did ", "when was ", "where is ", "where was ",
    "why does ", "why do ", "why is ", "why was ",
    "how does ", "how do ", "how is ",
    "define ", "definition", "meaning of", "explain ",
    "history of", "capital of", "population of",
    "math", "calculate", "formula",
    "sports", "score", "game", "team",
    "movie", "book", "song", "artist", "actor",
    "joke", "riddle", "fun fact",
    "translate", "language",
    "science", "biology", "chemistry", "physics",
    "planet", "space", "star", "galaxy",
    "country", "continent", "ocean", "river", "mountain",
)


def is_general_knowledge_question(message: str) -> bool:
    """Return True if the message is asking about general world knowledge
    (weather, facts, how-to, geography, science, etc.) rather than something
    specific to the business.  These should NEVER trigger the learning loop —
    the LLM can answer them directly."""
    if not message:
        return False
    m = message.strip().lower()
    return any(kw in m for kw in _GENERAL_KNOWLEDGE_KEYWORDS)


# ── Internals ────────────────────────────────────────────────────────────


# ── Customer-callback dispatcher ─────────────────────────────────────────


_DISPATCHER_THREAD: threading.Thread | None = None
_DISPATCHER_STOP = threading.Event()


def notify_visitor(config: dict, data_dir: Path, learned_record: dict) -> str:
    """Send a learned-answer reply to the original asker via their
    preferred channel. Returns the channel used, or '' if delivery
    failed (in which case the record stays in 'delivery_status=pending'
    and the dispatcher will retry on the next poll)."""
    import notifications as notify  # local import to avoid circular dep

    asker = learned_record.get("original_asker") or {}
    name = (asker.get("name") or "there").strip()
    answer = learned_record.get("answer") or ""
    question = learned_record.get("question") or ""
    business_name = (config.get("business") or {}).get("name") or "the team"
    pref = (asker.get("preferred_channel") or "").lower().strip()
    phone = (asker.get("phone") or "").strip()
    email = (asker.get("email") or "").strip()

    # Build the message. Two flavors: SMS (short) and email (full).
    sms_body = (
        f"Hi {name}, this is Orbi from {business_name}. You asked: "
        f"\"{question[:80]}\" — got the answer for you: {answer}"
    )[:600]
    email_subject = f"Got your answer about \"{question[:60]}\""
    email_body = (
        f"Hi {name},\n\n"
        f"You asked us: {question}\n\n"
        f"Here's the answer from {business_name}:\n\n"
        f"  {answer}\n\n"
        f"Anything else? Just reply or visit us anytime.\n\n"
        f"— Orbi, on behalf of {business_name}"
    )

    # Route: try the preferred channel first, then fall back to the
    # other one if we have it.
    tried = []
    if pref == "sms" and phone:
        if notify.send_sms_to(config, phone, sms_body):
            return "sms"
        tried.append("sms")
        if email and notify.send_email_to(config, email, email_subject, email_body):
            return "email"
        tried.append("email")
    elif pref == "email" and email:
        if notify.send_email_to(config, email, email_subject, email_body):
            return "email"
        tried.append("email")
        if phone and notify.send_sms_to(config, phone, sms_body):
            return "sms"
        tried.append("sms")
    else:
        # No preference — prefer SMS if we have a phone, else email
        if phone:
            if notify.send_sms_to(config, phone, sms_body):
                return "sms"
            tried.append("sms")
        if email:
            if notify.send_email_to(config, email, email_subject, email_body):
                return "email"
            tried.append("email")

    log.warning("notify_visitor failed for token=%s (tried %s, phone=%s, email=%s)",
                learned_record.get("token"), ",".join(tried) or "none",
                bool(phone), bool(email))
    return ""


def start_delivery_dispatcher(config: dict, data_dir: Path,
                              poll_seconds: int = 30) -> None:
    """Start the background thread that delivers verified learned-answer
    replies back to the original askers. Polls list_undelivered() every
    poll_seconds; for each undelivered answer, calls notify_visitor()
    and marks delivered on success. Idempotent — safe to call twice."""
    global _DISPATCHER_THREAD
    if _DISPATCHER_THREAD and _DISPATCHER_THREAD.is_alive():
        return
    _DISPATCHER_STOP.clear()
    _DISPATCHER_THREAD = threading.Thread(
        target=_dispatcher_loop,
        args=(config, data_dir, poll_seconds),
        daemon=True,
        name="learning-loop-dispatcher",
    )
    _DISPATCHER_THREAD.start()
    log.info("learning-loop dispatcher started (poll=%ss)", poll_seconds)


def stop_delivery_dispatcher() -> None:
    _DISPATCHER_STOP.set()


def _dispatcher_loop(config: dict, data_dir: Path, poll_seconds: int) -> None:
    """Background loop — check undelivered list, deliver, mark, repeat.
    Crash-safe: any exception is logged and the loop continues."""
    while not _DISPATCHER_STOP.is_set():
        try:
            for rec in list_undelivered(data_dir):
                channel = notify_visitor(config, data_dir, rec)
                if channel:
                    mark_delivered(data_dir, rec["token"], channel)
                    log.info("learning-loop delivered token=%s via %s to %s",
                             rec["token"], channel,
                             (rec.get("original_asker") or {}).get("phone")
                             or (rec.get("original_asker") or {}).get("email"))
        except Exception as e:
            log.warning("learning-loop dispatcher error: %s", e)
        for _ in range(poll_seconds):
            if _DISPATCHER_STOP.is_set():
                break
            time.sleep(1)


# Make time importable at module load (we use it in the dispatcher loop)
import time  # noqa: E402  (kept here to keep public API clean above)


# ── Internals ────────────────────────────────────────────────────────────


def _normalize(s: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace. Two visitors
    asking the same question slightly differently must collapse to the
    same normalized key — that's how learned answers compound."""
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _asker_key(asker: dict) -> str:
    return ((asker or {}).get("phone") or (asker or {}).get("email") or "").strip().lower()


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_token() -> str:
    return "Q_" + secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
