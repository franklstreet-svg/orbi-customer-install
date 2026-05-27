"""
safe_send.py — category-gated email sending for Orbi customer installs.

The default Orbi policy is "draft only" — every connector lands replies
in the owner's Drafts folder. That's the right default but it gets in
the way for routine, low-risk messages: thank-you notes, appointment
confirmations, follow-up nudges. The owner ends up rubber-stamping the
same 30 drafts a week.

safe_send.py is the opt-in escape hatch. The owner picks a small set of
CATEGORIES they're willing to let Orbi auto-send (the "whitelist"); for
those, send_email() goes straight to the wire. Everything else still
falls back to drafts.

CATEGORIES (free-form strings — these are conventions, not enforced):
    thank_you                — "thanks for stopping by", post-visit notes
    follow_up_nudge          — "haven't heard back, still interested?"
    appointment_confirmation — "see you Tuesday at 3 PM"
    review_response          — public-facing review reply text
    billing_reminder         — "your invoice is 5 days past due"

Preferences live at <user_dir>/safe_send_prefs.json:
    {
      "whitelist":         ["thank_you", "appointment_confirmation"],
      "require_approval":  false        # if true, NOTHING auto-sends
    }

DESIGN NOTES
------------
- We route through the existing connectors (connectors.gmail and
  connectors.outlook). Both gained a send_message() method as part of
  this feature — see those files for the OAuth scope additions.

- Preference of Gmail over Outlook when both are connected: this matches
  what Orbi already does (gcal scope was Google-first too). If only one
  is connected we use that one.

- Atomic state — same .tmp + .replace() pattern as the rest of Orbi.

ROUTE SURFACE — see bottom of file. orbi.py is NOT modified by this
module; the orchestrator wires the routes.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("orbi.safe_send")

_PREFS_FILE = "safe_send_prefs.json"
_LOCK = threading.Lock()

# Default categories shown in the UI dropdown. Owners can still pass
# arbitrary strings to send_email() — these are just the suggested set.
DEFAULT_CATEGORIES = [
    "thank_you",
    "follow_up_nudge",
    "appointment_confirmation",
    "review_response",
    "billing_reminder",
]

DEFAULT_PREFS = {
    "whitelist":        [],     # empty == nothing auto-sends; everything drafts
    "require_approval": False,
}


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

def _prefs_path(user_dir: Path) -> Path:
    return Path(user_dir) / _PREFS_FILE


def get_preferences(user_dir) -> dict:
    """Return the owner's safe-send preferences, with defaults filled in."""
    p = _prefs_path(Path(user_dir))
    if not p.exists():
        return dict(DEFAULT_PREFS, whitelist=list(DEFAULT_PREFS["whitelist"]),
                    available_categories=list(DEFAULT_CATEGORIES))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("safe_send: prefs read failed: %s", exc)
        data = {}
    out = dict(DEFAULT_PREFS)
    out["whitelist"] = list(data.get("whitelist") or [])
    out["require_approval"] = bool(data.get("require_approval", False))
    out["available_categories"] = list(DEFAULT_CATEGORIES)
    return out


def set_preferences(user_dir, prefs: dict) -> dict:
    """Persist preferences atomically. Returns the saved record."""
    user_dir = Path(user_dir)
    user_dir.mkdir(parents=True, exist_ok=True)

    whitelist = prefs.get("whitelist") or []
    if not isinstance(whitelist, list):
        whitelist = []
    # Normalise — strip + lowercase, dedupe, preserve order.
    seen: set[str] = set()
    clean_wl: list[str] = []
    for c in whitelist:
        if not isinstance(c, str):
            continue
        key = c.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        clean_wl.append(key)

    record = {
        "whitelist":        clean_wl,
        "require_approval": bool(prefs.get("require_approval", False)),
    }

    p = _prefs_path(user_dir)
    with _LOCK:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)

    log.info("safe_send: prefs updated (whitelist=%s, require_approval=%s)",
             clean_wl, record["require_approval"])
    return get_preferences(user_dir)


# ---------------------------------------------------------------------------
# Whitelist check
# ---------------------------------------------------------------------------

def is_safe_to_send(category: str, owner_whitelist: list[str]) -> bool:
    """Return True iff `category` is in `owner_whitelist`. Case-insensitive."""
    if not category or not isinstance(category, str):
        return False
    cat = category.strip().lower()
    if not cat:
        return False
    wl = {c.strip().lower() for c in (owner_whitelist or []) if isinstance(c, str)}
    return cat in wl


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send_email(config: dict, user_dir, *,
               to_email: str, subject: str, body: str,
               category: str) -> dict:
    """
    Send an email through whichever connector is configured (Gmail
    preferred, Outlook fallback) — but ONLY if `category` is whitelisted
    in the owner's safe-send preferences and require_approval is False.
    Otherwise the message is saved to drafts.

    Returns one of:
        { "action": "sent",     "via": "gmail"|"outlook",
          "message_id": "...", "to": "...", "category": "..." }
        { "action": "drafted",  "via": "gmail"|"outlook",
          "draft_id": "...",   "to": "...", "category": "...",
          "reason": "category_not_whitelisted"|"require_approval"|
                    "no_connector"|"send_failed" }
    """
    user_dir = Path(user_dir)
    to_email = (to_email or "").strip()
    subject  = (subject or "").strip() or "(no subject)"
    body     = body or ""
    category = (category or "").strip().lower()

    if not to_email:
        return {"action": "error", "error": "to_email required"}

    prefs = get_preferences(user_dir)
    whitelist = prefs.get("whitelist") or []
    require_approval = prefs.get("require_approval", False)

    # Decide: send or draft?
    do_send = (not require_approval) and is_safe_to_send(category, whitelist)
    reason = ""
    if not do_send:
        reason = ("require_approval" if require_approval
                  else "category_not_whitelisted")

    # Pick connector. Lazy import — keeps the module bootable without
    # google libs / urllib config.
    conn, conn_id = _pick_connector(config, user_dir)
    if conn is None:
        log.warning("safe_send: no connected mail connector for user_dir=%s",
                    user_dir)
        return {"action": "error", "to": to_email, "category": category,
                "reason": "no_connector",
                "error": "no Gmail or Outlook connector is connected"}

    if do_send:
        try:
            res = conn.send_message(to_email=to_email,
                                    subject=subject, body=body)
            mid = (res or {}).get("message_id", "") if isinstance(res, dict) else ""
            log.info("safe_send: SENT via %s to %s (category=%s, id=%s)",
                     conn_id, to_email, category, mid)
            return {
                "action":     "sent",
                "via":        conn_id,
                "message_id": mid,
                "to":         to_email,
                "category":   category,
            }
        except Exception as exc:    # noqa: BLE001 — must fall back to draft
            log.warning("safe_send: send via %s failed: %s — falling back to draft",
                        conn_id, exc)
            reason = "send_failed"
            # fall through into the draft path below

    # Draft path. We use _draft_arbitrary_email() here because the
    # existing connector draft_reply() requires a parent message_id;
    # safe_send emails are net-new, not replies.
    try:
        draft_res = _draft_arbitrary_email(conn, conn_id, to_email, subject, body)
        log.info("safe_send: DRAFTED via %s to %s (category=%s, reason=%s)",
                 conn_id, to_email, category, reason or "fallback")
        return {
            "action":   "drafted",
            "via":      conn_id,
            "draft_id": draft_res.get("draft_id", ""),
            "to":       to_email,
            "category": category,
            "reason":   reason or "drafted",
        }
    except Exception as exc:    # noqa: BLE001
        log.warning("safe_send: draft via %s also failed: %s", conn_id, exc)
        return {
            "action": "error",
            "via":    conn_id,
            "to":     to_email,
            "category": category,
            "error":  f"both send and draft failed: {exc}",
        }


# ---------------------------------------------------------------------------
# Internal: connector picker
# ---------------------------------------------------------------------------

def _pick_connector(config: dict, user_dir: Path):
    """Return (connector_instance, id_str) for the first connected mail
    integration. Prefer Gmail. Return (None, '') if nothing is connected."""
    try:
        from connectors.base import get_instance
    except Exception as exc:    # noqa: BLE001
        log.warning("safe_send: connectors.base import failed: %s", exc)
        return None, ""

    for cid in ("gmail", "outlook"):
        try:
            inst = get_instance(cid, config, user_dir)
        except Exception as exc:    # noqa: BLE001
            log.warning("safe_send: %s instance build failed: %s", cid, exc)
            continue
        if inst is None:
            continue
        try:
            if inst.is_connected():
                return inst, cid
        except Exception as exc:    # noqa: BLE001
            log.warning("safe_send: %s is_connected check failed: %s", cid, exc)
            continue
    return None, ""


def _draft_arbitrary_email(conn, conn_id: str,
                            to_email: str, subject: str, body: str) -> dict:
    """Create a brand-new draft (not a reply) on whichever connector. Both
    connectors have native draft creation we can call directly without
    needing a parent message_id."""
    if conn_id == "gmail":
        return _gmail_draft_new(conn, to_email, subject, body)
    if conn_id == "outlook":
        return _outlook_draft_new(conn, to_email, subject, body)
    raise RuntimeError(f"unsupported connector id={conn_id!r}")


def _gmail_draft_new(conn, to_email: str, subject: str, body: str) -> dict:
    import base64
    from email.message import EmailMessage
    em = EmailMessage()
    em["To"]      = to_email
    em["Subject"] = subject
    em.set_content(body or "")
    raw = base64.urlsafe_b64encode(em.as_bytes()).decode("ascii")
    svc = conn._get_service()
    draft = svc.users().drafts().create(
        userId="me", body={"message": {"raw": raw}},
    ).execute()
    return {"draft_id": draft.get("id", "")}


def _outlook_draft_new(conn, to_email: str, subject: str, body: str) -> dict:
    status, resp = conn._graph(
        "POST", "/me/messages",
        json_body={
            "subject":      subject,
            "body":         {"contentType": "Text", "content": body or ""},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
    )
    if status not in (200, 201) or not isinstance(resp, dict):
        raise RuntimeError(f"outlook draft create failed (status={status}): {resp}")
    return {"draft_id": resp.get("id", "")}


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir is the logged-in owner's
# per-user data folder. `config` is the loaded config.json.
#
#   GET  /api/owner/safe_send/preferences
#       calls:   get_preferences(user_dir)
#       returns: { "whitelist": [...], "require_approval": bool,
#                  "available_categories": [...] }
#
#   PUT  /api/owner/safe_send/preferences
#       body:    { "whitelist": ["thank_you", ...], "require_approval": false }
#       calls:   set_preferences(user_dir, body)
#       returns: same shape as GET
#
#   POST /api/owner/safe_send/send
#       body:    { "to_email": "...", "subject": "...", "body": "...",
#                  "category": "thank_you" }
#       calls:   send_email(config, user_dir, to_email=..., subject=...,
#                           body=..., category=...)
#       returns: { "action": "sent"|"drafted", "via": "gmail"|"outlook",
#                  "message_id"|"draft_id": "...", "to": "...",
#                  "category": "...", "reason": "..."? }
#
# NOTE — ONE-TIME OWNER ACTION REQUIRED:
# This feature added new OAuth scopes to BOTH mail connectors:
#   - Gmail: added  https://www.googleapis.com/auth/gmail.send
#            (existing gmail.compose does NOT include send permission)
#   - Outlook: added  Mail.Send
# Existing owners must RE-CONNECT Gmail / Outlook (disconnect → connect
# again) to grant the new permission. Until they do, send_email() will
# silently fall back to drafts.
#
# ---------------------------------------------------------------------------
