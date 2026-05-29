"""
review_responder — Tier-2 feature: auto-draft polite owner replies for new
customer reviews (Google Business Profile + Yelp) and surface them for the
owner to approve before posting.

The flow:
    1.  scan_recent_reviews() walks every connected review connector
        (google_reviews, yelp), pulls the most recent reviews, and compares
        them to <user_dir>/seen_reviews.json. Anything not in that state file
        is "new" and gets a draft.
    2.  draft_response() takes a single review dict (author, rating, text)
        plus the loaded business_info and asks the LLM (via llm_client) for
        a 2-4 sentence, human-sounding reply tuned to the rating and to the
        business's configured personality.tone. The prompt explicitly forbids
        (parenthetical stage directions), apologetics that argue back, and
        marketing fluff.
    3.  mark_reviewed() flips the per-review "approved" bit in the same state
        file once the owner either posts the draft or dismisses it. That stops
        the dashboard from re-surfacing it on the next scan.

ROUTE SURFACE — see bottom of file. Routes are owner-authed (cookie); the
orchestrator passes config + user_dir.

CONVENTIONS
-----------
* logging.getLogger("orbi.review_responder")
* atomic state writes (tmp + .replace)
* lazy imports for connectors / llm_client so importing this module from a
  thin context (cron, tests) does NOT pull the whole Google client stack
* the LLM call is wrapped in try/except so a tier-3 outage falls through to
  a deterministic template — the owner is never left without a draft.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("orbi.review_responder")

STATE_FILE = "seen_reviews.json"
_LOCK = threading.Lock()

# Connectors we know how to pull reviews from. The string id matches the
# Connector.id field in connectors/gcal_reviews.py and connectors/yelp.py.
REVIEW_CONNECTORS = ("google_reviews", "yelp")

# Tones supported by personality.tone. Anything else falls back to
# friendly_professional.
TONE_GUIDANCE = {
    "friendly_professional": (
        "Warm but professional. Sound like a competent business owner "
        "who genuinely appreciates feedback. No slang."
    ),
    "warm_casual": (
        "Friendly and personal, like you'd talk to a regular. Light touch is "
        "fine. Still respectful, never corny."
    ),
    "formal": (
        "Polite and businesslike. Complete sentences. Sound like a hotel "
        "manager or a doctor's office responding."
    ),
    "playful": (
        "A bit of warmth and personality is great. One small touch of humor "
        "is OK on positive reviews. Never sarcastic on negative ones."
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def draft_response(config: dict, business_info: dict, review: dict) -> str:
    """
    Draft a polite owner reply to a single review.

    `review` shape:  {author, rating (1-5), text}
    Returns a single string, 2-4 sentences, no parentheticals, no signature.

    Tries the LLM (llm_client.generate). If every tier fails, falls back to
    a deterministic template tuned to the rating so the owner ALWAYS has
    something to start from.
    """
    if not isinstance(review, dict):
        return ""
    rating  = _coerce_rating(review.get("rating"))
    author  = (review.get("author") or "").strip() or "there"
    text    = (review.get("text") or "").strip()

    tone_key  = _tone_key(business_info)
    biz_name  = (business_info.get("name") or "").strip() or "our business"
    contact_email = ((business_info.get("contact") or {}).get("email") or "").strip()

    system = _build_system_prompt(tone_key, biz_name, contact_email)
    user   = _build_user_prompt(author, rating, text)

    # Try the LLM
    try:
        import llm_client
        resp = llm_client.generate(config, system,
                                   [{"role": "user", "content": user}])
        out = (getattr(resp, "text", "") or "").strip()
        out = _scrub(out)
        if out:
            return out
        log.info("llm returned empty content; using template fallback")
    except Exception as exc:    # noqa: BLE001 — fall through to template
        log.warning("llm draft failed, using template: %s", exc)

    return _template_reply(rating, author, biz_name, contact_email)


def scan_recent_reviews(config: dict, user_dir) -> dict:
    """
    For every connected review connector, fetch recent reviews, diff against
    the per-user seen_reviews.json, and return only the NEW ones plus drafts.

    Returns:
        {
            "new_reviews": [ {connector, id, author, rating, text, time,
                              location_id?, business_id?}, ...],
            "drafts":      [ {review: {...}, draft: "..."}, ...],
            "errors":      { "google_reviews": "...", "yelp": "..." }
        }

    State file <user_dir>/seen_reviews.json shape:
        {
          "google_reviews": { "<review_id>": {"first_seen": ISO,
                                              "reviewed": false,
                                              "rating": 5,
                                              "author": "..."}, ... },
          "yelp":           { ... }
        }
    """
    user_dir = Path(user_dir)
    user_dir.mkdir(parents=True, exist_ok=True)
    state = _read_state(user_dir)
    biz_info = _load_business_info(user_dir)

    new_reviews: list[dict] = []
    drafts:      list[dict] = []
    errors:      dict       = {}

    for cid in REVIEW_CONNECTORS:
        try:
            reviews = _fetch_for_connector(config, user_dir, cid)
        except Exception as exc:    # noqa: BLE001 — defensive: don't block other connectors
            log.warning("scan: connector %s crashed: %s", cid, exc)
            errors[cid] = f"{type(exc).__name__}: {exc}"
            continue

        if not isinstance(reviews, list):
            continue

        seen_for_conn = state.setdefault(cid, {})
        for r in reviews:
            rid = str(r.get("id") or "").strip()
            if not rid:
                # Synthesize a stable id from author+time+text so we don't
                # spam the owner with the same review every poll.
                rid = _synthetic_id(r)
            if rid in seen_for_conn:
                continue  # already surfaced — skip
            entry = {
                "first_seen": _now_iso(),
                "reviewed":   False,
                "rating":     _coerce_rating(r.get("rating")),
                "author":     (r.get("author") or "").strip(),
            }
            seen_for_conn[rid] = entry

            normalized = {
                "connector": cid,
                "id":        rid,
                "author":    r.get("author") or "",
                "rating":    _coerce_rating(r.get("rating")),
                "text":      r.get("text") or "",
                "time":      r.get("time") or "",
            }
            # Carry the platform-specific id forward so the orchestrator can
            # post the reply through the right connector.
            if cid == "google_reviews":
                # google reviews need (location_id, review_id). We didn't
                # capture location_id in list_reviews; surface what we have.
                normalized["location_id"] = r.get("location_id") or ""
            elif cid == "yelp":
                normalized["url"] = r.get("url") or ""
            new_reviews.append(normalized)

    # Draft replies for new ones
    for r in new_reviews:
        try:
            draft = draft_response(config, biz_info, r)
        except Exception as exc:    # noqa: BLE001 — never block scan on a draft
            log.warning("draft failed for %s/%s: %s", r["connector"], r["id"], exc)
            draft = _template_reply(_coerce_rating(r["rating"]),
                                    r.get("author") or "there",
                                    (biz_info.get("name") or "").strip() or "our business",
                                    ((biz_info.get("contact") or {}).get("email") or "").strip())
        drafts.append({"review": r, "draft": draft})

    _write_state(user_dir, state)

    log.info("scan: %d new review(s) across %d connector(s)",
             len(new_reviews), len(REVIEW_CONNECTORS))
    return {
        "new_reviews": new_reviews,
        "drafts":      drafts,
        "errors":      errors,
    }


def mark_reviewed(user_dir, review_id: str) -> bool:
    """
    Flip the 'reviewed' bit on a single review. Returns True if a record was
    found and updated, False otherwise. The orchestrator should call this
    whether the owner posted, edited-then-posted, or dismissed the draft —
    either way it shouldn't pop back up.
    """
    review_id = str(review_id or "").strip()
    if not review_id:
        return False
    user_dir = Path(user_dir)
    with _LOCK:
        state = _read_state(user_dir)
        hit = False
        for cid, items in (state or {}).items():
            if not isinstance(items, dict):
                continue
            entry = items.get(review_id)
            if entry is None:
                continue
            entry["reviewed"]    = True
            entry["reviewed_at"] = _now_iso()
            hit = True
            log.info("marked reviewed: %s/%s", cid, review_id)
            break
        if hit:
            _write_state_locked(user_dir, state)
        return hit


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_system_prompt(tone_key: str, biz_name: str, contact_email: str) -> str:
    tone_line = TONE_GUIDANCE.get(tone_key, TONE_GUIDANCE["friendly_professional"])
    contact_line = (
        f"If the rating is 1 or 2, suggest the customer email {contact_email} "
        f"so the owner can make it right offline."
        if contact_email else
        "If the rating is 1 or 2, invite the customer to contact the owner "
        "directly to make it right (do not invent an email address)."
    )
    return (
        "You write short, human-sounding owner replies to customer reviews "
        f"for {biz_name}. Tone: {tone_line}\n\n"
        "Rules — non-negotiable:\n"
        "  - Output ONLY the reply text, no preamble, no quotes, no labels.\n"
        "  - NEVER include (parenthetical) stage directions, asides, or notes.\n"
        "  - NEVER argue with a negative review. Never say the customer is wrong.\n"
        "  - 2 to 4 sentences. No bullet lists. No emojis unless the tone is 'playful'.\n"
        "  - Use the customer's first name if their author name is a real name.\n"
        "  - If the customer mentioned a specific thing they liked or disliked, "
        "acknowledge that specific thing — don't be generic.\n"
        f"  - {contact_line}\n"
        "  - 5-star: warm thank-you, brief, reference what they liked if any.\n"
        "  - 4-star: thank them and invite them to share what would have made it 5.\n"
        "  - 3-star: acknowledge the mixed feedback seriously, offer to make it right.\n"
        "  - 1-2 star: apologetic, take it offline, never defensive.\n"
        "  - Sign-off is just the owner's first name OR no sign-off at all. "
        "Don't sign with the business name. Don't add '— Best, Owner'."
    )


def _build_user_prompt(author: str, rating: int, text: str) -> str:
    text_block = text if text else "(no review text — just a star rating)"
    return (
        f"Customer name: {author}\n"
        f"Rating: {rating}/5\n"
        f"Review text:\n{text_block}\n\n"
        "Write the reply now."
    )


# ---------------------------------------------------------------------------
# Template fallback (deterministic, always works)
# ---------------------------------------------------------------------------


def _template_reply(rating: int, author: str, biz_name: str,
                    contact_email: str) -> str:
    first = _first_name(author)
    contact_clause = (
        f" Please email us at {contact_email} so we can make this right."
        if contact_email else
        " Please reach out so we can make this right."
    )
    if rating >= 5:
        return (
            f"Thanks so much, {first} — we really appreciate you taking the "
            f"time to share this. It means a lot to the whole team at "
            f"{biz_name}. Hope to see you again soon."
        )
    if rating == 4:
        return (
            f"Thank you for the kind words, {first}. We'd love to know what "
            f"would have made the visit a 5-star one — your feedback helps "
            f"us get better. Hope to see you again at {biz_name}."
        )
    if rating == 3:
        return (
            f"Thanks for the honest feedback, {first}. We take mixed reviews "
            f"seriously — there's clearly something we can do better. We'd "
            f"love a chance to make your next visit to {biz_name} a great one."
        )
    # 1-2 stars
    return (
        f"I'm sorry your experience with {biz_name} wasn't what it should "
        f"have been, {first}.{contact_clause} We want to hear what happened "
        f"and do better."
    )


# ---------------------------------------------------------------------------
# Connector glue
# ---------------------------------------------------------------------------


def _fetch_for_connector(config: dict, user_dir: Path, cid: str) -> list[dict]:
    """Return a flat list of recent reviews from connector `cid`, or []."""
    try:
        from connectors.base import get_instance, import_all
        import_all()
        conn = get_instance(cid, config, user_dir)
    except Exception as exc:    # noqa: BLE001
        log.info("connector %s unavailable: %s", cid, exc)
        return []

    if conn is None or not _is_connected(conn):
        return []

    if cid == "yelp":
        try:
            payload = conn.list_reviews(limit=3)
        except Exception as exc:    # noqa: BLE001
            log.warning("yelp list_reviews failed: %s", exc)
            return []
        return list(payload.get("reviews") or [])

    if cid == "google_reviews":
        # GBP is per-location. Pull locations, then reviews per location.
        out: list[dict] = []
        try:
            locs_payload = conn.list_locations()
        except Exception as exc:    # noqa: BLE001
            log.warning("google_reviews list_locations failed: %s", exc)
            return []
        if locs_payload.get("error"):
            # API likely restricted — surface 0 reviews quietly.
            log.info("google_reviews not currently returning data: %s",
                     locs_payload.get("error"))
            return []
        for loc in (locs_payload.get("locations") or []):
            location_id = loc.get("id") or ""
            if not location_id:
                continue
            try:
                rp = conn.list_reviews(location_id, limit=10)
            except Exception as exc:    # noqa: BLE001
                log.warning("google_reviews list_reviews %s failed: %s",
                            location_id, exc)
                continue
            for r in (rp.get("reviews") or []):
                r = dict(r)
                r["location_id"] = location_id
                out.append(r)
        return out

    return []


def _is_connected(conn) -> bool:
    try:
        return bool(conn.is_connected())
    except Exception:   # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Business-info loader (lazy)
# ---------------------------------------------------------------------------


def _load_business_info(user_dir: Path) -> dict:
    """Lazy-load business_info from the user's data folder. Returns {} on
    any failure — draft prompts will still be usable, just slightly less
    personalized."""
    try:
        from modules import business_info as biz_mod
        return biz_mod.load(Path(user_dir)) or {}
    except Exception as exc:    # noqa: BLE001
        log.info("business_info unavailable, drafts will be generic: %s", exc)
        return {}


def _tone_key(business_info: dict) -> str:
    personality = (business_info or {}).get("personality") or {}
    tone = (personality.get("tone") or "").strip().lower()
    if tone in TONE_GUIDANCE:
        return tone
    return "friendly_professional"


# ---------------------------------------------------------------------------
# State I/O — atomic writes
# ---------------------------------------------------------------------------


def _read_state(user_dir: Path) -> dict:
    p = Path(user_dir) / STATE_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("seen_reviews read failed: %s", exc)
        return {}


def _write_state(user_dir: Path, state: dict) -> None:
    with _LOCK:
        _write_state_locked(user_dir, state)


def _write_state_locked(user_dir: Path, state: dict) -> None:
    user_dir = Path(user_dir)
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / STATE_FILE
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scrub(text: str) -> str:
    """Strip artifacts the LLM tends to add: surrounding quotes, leading
    'Reply:' labels, and parenthetical stage directions."""
    if not text:
        return ""
    s = text.strip()
    # Drop wrapping quotes
    if len(s) >= 2 and s[0] in "\"'" and s[-1] in "\"'":
        s = s[1:-1].strip()
    # Drop leading labels like "Reply:" or "Response:"
    for label in ("Reply:", "Response:", "Owner reply:", "Draft:"):
        if s.lower().startswith(label.lower()):
            s = s[len(label):].lstrip()
    # Strip (parenthetical) stage directions — defensive, the prompt forbids
    # them but small models sometimes ignore that.
    import re
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def _first_name(author: str) -> str:
    a = (author or "").strip()
    if not a or a.lower() in ("anonymous", "a google user", "yelp user"):
        return "there"
    first = a.split()[0]
    # Strip honorifics
    if first.lower().rstrip(".") in ("mr", "mrs", "ms", "dr", "prof"):
        parts = a.split()
        if len(parts) > 1:
            first = parts[1]
    return first or "there"


def _coerce_rating(r) -> int:
    try:
        v = int(r)
    except (TypeError, ValueError):
        try:
            v = int(float(r))
        except (TypeError, ValueError):
            v = 0
    return max(0, min(5, v))


def _synthetic_id(review: dict) -> str:
    """Stable hash for reviews without a platform id."""
    src = f"{review.get('author','')}|{review.get('time','')}|{review.get('text','')}"
    return "syn_" + hashlib.sha1(src.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir is the logged-in owner's
# per-user data folder; config is the global config dict.
#
#   GET  /api/owner/reviews/new
#       calls:  scan_recent_reviews(config, user_dir)
#       returns:{ "new_reviews": [...], "drafts": [...], "errors": {...} }
#
#   POST /api/owner/reviews/<review_id>/mark_reviewed
#       body:   {} (the review_id is in the path)
#       calls:  mark_reviewed(user_dir, review_id)
#       returns:{ "ok": true|false }
#
#   POST /api/owner/reviews/<review_id>/draft
#       body:   {}  — re-draft if the owner didn't like the first one
#       implementation:
#           1. read seen_reviews.json for the review_id (recover author/rating)
#           2. re-pull the review text from the connector (or cache the body
#              in seen_reviews.json on first scan — implementer's choice)
#           3. call draft_response(config, business_info.load(user_dir), review)
#           4. return { "draft": "..." }
#       NOTE: this route is intentionally idempotent — calling it twice
#       returns two different drafts (the LLM has temperature > 0), which is
#       exactly what the owner wants when they hit "try again".
#
# ---------------------------------------------------------------------------
