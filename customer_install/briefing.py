"""
briefing — daily morning brief for an Orbi owner / staff user.

Pulls from local modules (calendar, reminders, tasks, messages, learning_loop)
plus any connected external sources (Gmail, Stripe, Google Reviews, Yelp) and
assembles a personalized "good morning" digest. The brief is delivered via the
shared notifications.send() pipeline (web push + email + SMS, whichever
channels the owner has enabled).

DESIGN NOTES
------------
- Per-user state is stored under the user's own folder:
    <user_dir>/briefing_prefs.json   — {enabled, hour, channels, last_sent_iso}
    <user_dir>/briefing_state.json   — {last_sent_date, last_brief_summary, ...}
- Connectors are looked up lazily through connectors.base.get_instance(). If
  the connector module never loaded (missing pip dep) or the user never
  connected the service, we skip silently — the brief still goes out.
- Connector calls are individually try/except-wrapped. One broken integration
  cannot break the whole brief. Failures are recorded as a short note in the
  returned `stats.errors` list and a one-line entry in `items`.
- Atomic writes (tmp + replace) under a module-level threading.Lock for
  state files — same convention as users.py / modules/calendar.py.
- The background scheduler is described at the bottom; orbi.py owns the
  thread (so we don't double-start it on import).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("orbi.briefing")

PREFS_FILE = "briefing_prefs.json"
STATE_FILE = "briefing_state.json"

DEFAULT_PREFS = {
    "enabled":  True,
    "hour":     7,                    # local hour (24h) to send brief
    "channels": ["push", "email"],    # subset of ["push", "email", "sms"]
}

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_briefing(config: dict, data_dir: Path, username: str) -> dict:
    """Assemble the morning brief for one user.

    Pulls from local modules + connected external sources. Each source is
    isolated so one failing integration does not break the brief. Returns
    a dict with three keys:

        {
            "summary_text": "Good morning Frank. You have 2 meetings today...",
            "stats":        {"events_today": 2, "unread_emails": 8, ...,
                             "errors": ["gmail: not connected"]},
            "items":        [{"kind": "event", "title": "...", ...}, ...],
        }
    """
    data_dir = Path(data_dir)
    user_dir = _user_dir(data_dir, username)

    display_name = _display_name(data_dir, username)
    today_iso = _today_local_iso(config)

    stats: dict = {
        "events_today":          0,
        "reminders_due":         0,
        "open_tasks":            0,
        "unread_emails":         0,
        "yesterday_revenue":     None,
        "new_reviews":           0,
        "unread_messages":       0,
        "pending_questions":     0,
        "errors":                [],
    }
    items: list[dict] = []

    # ── Calendar (local) ────────────────────────────────────────────────
    try:
        from modules import calendar as mod_calendar
        events = mod_calendar.today(user_dir) or []
        stats["events_today"] = len(events)
        for e in events:
            items.append({
                "kind":     "event",
                "title":    e.get("title", ""),
                "when":     (e.get("start", "") or "")[:16].replace("T", " "),
                "location": e.get("location", "") or "",
            })
    except Exception as exc:    # noqa: BLE001
        log.warning("briefing[%s] calendar failed: %s", username, exc)
        stats["errors"].append(f"calendar: {exc}")

    # ── Reminders (local) due today ─────────────────────────────────────
    try:
        from modules import reminders as mod_reminders
        all_rem = mod_reminders.list_all(user_dir) or []
        due_today = [r for r in all_rem
                     if _due_date(r.get("due", "")) == today_iso]
        stats["reminders_due"] = len(due_today)
        for r in due_today:
            items.append({
                "kind":  "reminder",
                "text":  r.get("text", ""),
                "when":  (r.get("due", "") or "")[:16].replace("T", " "),
            })
    except Exception as exc:    # noqa: BLE001
        log.warning("briefing[%s] reminders failed: %s", username, exc)
        stats["errors"].append(f"reminders: {exc}")

    # ── Tasks (local) ───────────────────────────────────────────────────
    try:
        from modules import tasks as mod_tasks
        open_tasks = mod_tasks.list_all(user_dir, include_done=False) or []
        stats["open_tasks"] = len(open_tasks)
        # Surface the first few only — full list lives in the dashboard.
        for t in open_tasks[:3]:
            items.append({"kind": "task", "text": t.get("text", "")})
    except Exception as exc:    # noqa: BLE001
        log.warning("briefing[%s] tasks failed: %s", username, exc)
        stats["errors"].append(f"tasks: {exc}")

    # ── Unread customer messages (shared data_dir, not per-user) ────────
    try:
        from modules import messages as mod_messages
        unread = [m for m in mod_messages.list_all(data_dir, limit=200)
                  if not m.get("read")]
        stats["unread_messages"] = len(unread)
        for m in unread[:5]:
            items.append({
                "kind":    "message",
                "from":    m.get("from_name") or m.get("from_phone") or m.get("from_email") or "Anonymous",
                "type":    m.get("type", "message"),
                "snippet": (m.get("body") or "")[:120],
            })
    except Exception as exc:    # noqa: BLE001
        log.warning("briefing[%s] messages failed: %s", username, exc)
        stats["errors"].append(f"messages: {exc}")

    # ── Learning-loop pending questions ─────────────────────────────────
    try:
        from modules import learning_loop as mod_learning
        pending = mod_learning.list_pending(data_dir) or []
        stats["pending_questions"] = len(pending)
        for p in pending[:3]:
            items.append({
                "kind":     "question",
                "question": p.get("question", ""),
                "asker":    (p.get("asker") or {}).get("name", "") or "anonymous visitor",
                "token":    p.get("token", ""),
            })
    except Exception as exc:    # noqa: BLE001
        log.warning("briefing[%s] learning_loop failed: %s", username, exc)
        stats["errors"].append(f"learning_loop: {exc}")

    # ── Gmail (connected only) ──────────────────────────────────────────
    gmail = _connector(config, user_dir, "gmail")
    if gmail and _is_connected(gmail):
        try:
            unread_msgs = gmail.list_recent(limit=20)
            # list_recent uses in:inbox; filter to unread.
            unread_msgs = [m for m in unread_msgs if m.get("unread")]
            stats["unread_emails"] = len(unread_msgs)
            for m in unread_msgs[:5]:
                items.append({
                    "kind":    "email",
                    "from":    m.get("from", ""),
                    "subject": m.get("subject", ""),
                    "snippet": (m.get("snippet") or "")[:120],
                })
        except Exception as exc:    # noqa: BLE001
            log.warning("briefing[%s] gmail failed: %s", username, exc)
            stats["errors"].append("Gmail check failed")
            items.append({"kind": "note", "text": "Gmail check failed — try again later."})

    # ── Stripe — yesterday's revenue (connected only) ───────────────────
    stripe = _connector(config, user_dir, "stripe")
    if stripe and _is_connected(stripe):
        try:
            # daily_summary(days=2) gives yesterday + today. We want yesterday only.
            summary = stripe.daily_summary(days=2) or []
            yesterday_iso = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
            y_row = next((r for r in summary if r.get("date") == yesterday_iso), None)
            if y_row:
                stats["yesterday_revenue"] = {
                    "gross":   y_row.get("gross_dollars", 0.0),
                    "net":     y_row.get("net_dollars", 0.0),
                    "count":   y_row.get("count", 0),
                    "refunds": y_row.get("refunds", 0.0),
                }
                items.append({
                    "kind":    "revenue",
                    "date":    yesterday_iso,
                    "gross":   y_row.get("gross_dollars", 0.0),
                    "count":   y_row.get("count", 0),
                })
        except Exception as exc:    # noqa: BLE001
            log.warning("briefing[%s] stripe failed: %s", username, exc)
            stats["errors"].append("Stripe check failed")
            items.append({"kind": "note", "text": "Stripe check failed — try again later."})

    # ── New reviews since last brief (Google + Yelp, connected only) ────
    last_sent = _last_sent_iso(user_dir)
    new_reviews = []
    for conn_id in ("google_reviews", "yelp"):
        conn = _connector(config, user_dir, conn_id)
        if not conn or not _is_connected(conn):
            continue
        try:
            if conn_id == "yelp":
                resp = conn.list_reviews(limit=3) or {}
                reviews = resp.get("reviews") or []
            else:
                # google_reviews needs a location_id; read it from the saved
                # token meta (set during first list_locations() call).
                tok = getattr(conn, "_read_tokens", lambda: {})() or {}
                loc_id = tok.get("default_location_id") or ""
                if not loc_id:
                    continue
                resp = conn.list_reviews(loc_id, limit=10) or {}
                reviews = resp.get("reviews") or []
            for r in reviews:
                if _is_after(r.get("time", ""), last_sent):
                    r = dict(r)
                    r["source"] = conn_id
                    new_reviews.append(r)
        except Exception as exc:    # noqa: BLE001
            log.warning("briefing[%s] %s reviews failed: %s", username, conn_id, exc)
            stats["errors"].append(f"{conn_id} check failed")
            items.append({"kind": "note", "text": f"{conn_id} check failed — try again later."})

    stats["new_reviews"] = len(new_reviews)
    for r in new_reviews[:5]:
        items.append({
            "kind":   "review",
            "source": r.get("source", ""),
            "author": r.get("author", ""),
            "rating": r.get("rating", 0),
            "text":   (r.get("text") or "")[:140],
        })

    # ── Build the human-readable summary ────────────────────────────────
    summary_text = _build_summary(display_name, stats)

    return {
        "summary_text": summary_text,
        "stats":        stats,
        "items":        items,
        "username":     username,
        "built_at":     _now_iso(),
    }


def format_brief_for_speech(brief: dict) -> str:
    """Short TTS-friendly version of the brief — one paragraph, no list markers."""
    stats = brief.get("stats") or {}
    summary = brief.get("summary_text") or ""

    parts = [summary]

    rev = stats.get("yesterday_revenue") or {}
    if rev and rev.get("gross"):
        parts.append(
            f"Yesterday you brought in ${rev['gross']:.0f} "
            f"across {rev.get('count', 0)} payment{'s' if rev.get('count', 0) != 1 else ''}."
        )

    if stats.get("new_reviews"):
        n = stats["new_reviews"]
        parts.append(f"You have {n} new review{'s' if n != 1 else ''} to look at.")

    if stats.get("pending_questions"):
        n = stats["pending_questions"]
        parts.append(
            f"And {n} customer question{'s' if n != 1 else ''} "
            f"{'are' if n != 1 else 'is'} waiting on your answer."
        )

    if stats.get("errors"):
        # Don't read the error list aloud — just a soft heads-up.
        parts.append("Heads-up — one or more checks couldn't run; see the dashboard.")

    return " ".join(p.strip() for p in parts if p.strip())


def send_morning_brief(config: dict, data_dir: Path, username: str) -> dict:
    """Build the brief and dispatch it via notify.send(). Records the send
    in <user_dir>/briefing_state.json so should_send_today() returns False
    for the rest of the day. Always returns the brief dict (with a
    `delivery` field added)."""
    import notifications as notify   # local import — avoids circular at boot

    brief = build_briefing(config, data_dir, username)
    user_dir = _user_dir(data_dir, username)

    title = "Your morning brief"
    body  = brief.get("summary_text") or "Good morning."

    try:
        result = notify.send(
            config, Path(data_dir),
            event="new_message",   # mapped flag; brief is owner-facing info
            title=title,
            body=body,
            url="/owner#briefing",
        )
    except Exception as exc:    # noqa: BLE001
        log.warning("briefing[%s] notify.send failed: %s", username, exc)
        result = {"queued": False, "error": str(exc)}

    brief["delivery"] = result
    _record_sent(user_dir, brief)
    return brief


def should_send_today(user_dir: Path) -> bool:
    """True if the brief has not yet been sent today (UTC date).
    Returns True if state file is missing (first run)."""
    state = _read_state(user_dir)
    last = state.get("last_sent_date", "")
    today = datetime.now(timezone.utc).date().isoformat()
    return last != today


def get_preferences(user_dir: Path) -> dict:
    """Return the user's briefing prefs, merged over defaults."""
    user_dir = Path(user_dir)
    path = user_dir / PREFS_FILE
    if not path.exists():
        return dict(DEFAULT_PREFS)
    try:
        prefs = json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("briefing prefs read failed: %s", exc)
        prefs = {}
    merged = dict(DEFAULT_PREFS)
    merged.update({k: v for k, v in prefs.items() if v is not None})
    return merged


def set_preferences(user_dir: Path, prefs: dict) -> dict:
    """Update prefs (partial — only the keys provided). Returns the merged
    prefs after the write. Validates hour (0-23) and channels."""
    user_dir = Path(user_dir)
    user_dir.mkdir(parents=True, exist_ok=True)
    current = get_preferences(user_dir)

    incoming = dict(prefs or {})
    if "enabled" in incoming:
        current["enabled"] = bool(incoming["enabled"])
    if "hour" in incoming:
        try:
            h = int(incoming["hour"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"hour must be int 0-23: {exc}") from exc
        if not 0 <= h <= 23:
            raise ValueError(f"hour out of range: {h}")
        current["hour"] = h
    if "channels" in incoming:
        chans = incoming["channels"] or []
        if not isinstance(chans, list):
            raise ValueError("channels must be a list")
        valid = {"push", "email", "sms"}
        bad = [c for c in chans if c not in valid]
        if bad:
            raise ValueError(f"invalid channel(s): {bad}")
        current["channels"] = list(chans)

    with _LOCK:
        path = user_dir / PREFS_FILE
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
    return current


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _user_dir(data_dir: Path, username: str) -> Path:
    """Resolve the user's data folder. Falls back to a direct join if the
    users module is not importable (defensive — should never happen in
    normal operation)."""
    try:
        from users import get_user_dir
        return get_user_dir(Path(data_dir), username)
    except Exception:    # noqa: BLE001
        return Path(data_dir) / "users" / (username or "").strip().lower()


def _display_name(data_dir: Path, username: str) -> str:
    try:
        from users import get_user
        rec = get_user(Path(data_dir), username) or {}
        return (rec.get("display_name") or username or "").strip() or username
    except Exception:    # noqa: BLE001
        return username


def _connector(config: dict, user_dir: Path, connector_id: str):
    """Return a connector instance (no validity check) or None if the
    connector module isn't registered. Lazy imports the registry so a
    missing optional dep can't take down the brief."""
    try:
        from connectors.base import get_instance, import_all
        import_all()    # idempotent — populates the registry on first call
        return get_instance(connector_id, config, user_dir)
    except Exception as exc:    # noqa: BLE001
        log.info("briefing: connector %r unavailable: %s", connector_id, exc)
        return None


def _is_connected(conn) -> bool:
    try:
        return bool(conn.is_connected())
    except Exception:    # noqa: BLE001
        return False


def _due_date(due_iso: str) -> str:
    """Return the YYYY-MM-DD prefix of an ISO due string, or ''."""
    if not due_iso or len(due_iso) < 10:
        return ""
    return due_iso[:10]


def _today_local_iso(config: dict) -> str:
    """Today's date in the configured local timezone offset. We don't pull
    pytz in to avoid a dep — UTC is good enough for an all-day match."""
    return datetime.now(timezone.utc).date().isoformat()


def _is_after(time_str: str, ref_iso: str) -> bool:
    """Best-effort comparison: returns True if time_str is later than
    ref_iso. Empty ref_iso means 'always count as new'."""
    if not ref_iso:
        return True
    if not time_str:
        return False
    a = (time_str or "")[:19]
    b = (ref_iso or "")[:19]
    return a > b


def _last_sent_iso(user_dir: Path) -> str:
    return _read_state(user_dir).get("last_sent_iso", "")


def _read_state(user_dir: Path) -> dict:
    path = Path(user_dir) / STATE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("briefing state read failed: %s", exc)
        return {}


def _record_sent(user_dir: Path, brief: dict) -> None:
    """Atomic write of briefing_state.json with last-sent timestamps and a
    one-line summary for the dashboard 'last brief' card."""
    user_dir = Path(user_dir)
    user_dir.mkdir(parents=True, exist_ok=True)
    state = _read_state(user_dir)
    now = datetime.now(timezone.utc)
    state["last_sent_iso"]    = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state["last_sent_date"]   = now.date().isoformat()
    state["last_summary"]     = (brief.get("summary_text") or "")[:500]
    state["last_stats"]       = brief.get("stats") or {}
    with _LOCK:
        path = user_dir / STATE_FILE
        tmp  = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)


def _build_summary(display_name: str, stats: dict) -> str:
    """Render `stats` as a human-readable greeting paragraph."""
    name = (display_name or "").strip() or "there"
    parts = [f"Good morning {name}."]

    n_events = stats.get("events_today", 0) or 0
    n_rem    = stats.get("reminders_due", 0) or 0
    n_tasks  = stats.get("open_tasks", 0) or 0
    n_email  = stats.get("unread_emails", 0) or 0
    n_msgs   = stats.get("unread_messages", 0) or 0
    n_qs     = stats.get("pending_questions", 0) or 0
    n_revs   = stats.get("new_reviews", 0) or 0

    sched_bits = []
    if n_events:
        sched_bits.append(f"{n_events} meeting{'s' if n_events != 1 else ''} today")
    if n_rem:
        sched_bits.append(f"{n_rem} reminder{'s' if n_rem != 1 else ''} due")
    if n_tasks:
        sched_bits.append(f"{n_tasks} open task{'s' if n_tasks != 1 else ''}")
    if sched_bits:
        parts.append("You have " + ", ".join(sched_bits) + ".")

    inbox_bits = []
    if n_email:
        inbox_bits.append(f"{n_email} unread email{'s' if n_email != 1 else ''}")
    if n_msgs:
        inbox_bits.append(f"{n_msgs} new customer message{'s' if n_msgs != 1 else ''}")
    if inbox_bits:
        parts.append(("Inbox: " + ", ".join(inbox_bits) + ".").capitalize())

    rev = stats.get("yesterday_revenue") or {}
    if rev and rev.get("gross"):
        parts.append(
            f"Yesterday's revenue: ${rev['gross']:.2f} "
            f"({rev.get('count', 0)} payment{'s' if rev.get('count', 0) != 1 else ''})."
        )

    if n_revs:
        parts.append(f"{n_revs} new review{'s' if n_revs != 1 else ''} since your last brief.")

    if n_qs:
        parts.append(
            f"{n_qs} customer question{'s' if n_qs != 1 else ''} "
            f"waiting for your answer."
        )

    if not (n_events or n_rem or n_tasks or n_email or n_msgs or n_revs or n_qs or rev):
        parts.append("Nothing pressing — a quiet start to the day.")

    return " ".join(parts)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). `user_dir` comes from the logged-in
# owner's per-user data folder. `CONFIG` and `DATA_DIR` are the globals in
# orbi.py.
#
#   GET  /api/owner/briefing/now
#       calls:   build_briefing(CONFIG, DATA_DIR, user["username"])
#       returns: full brief dict (summary_text, stats, items, built_at)
#       purpose: dashboard "preview" — render today's brief without
#                marking it sent.
#
#   POST /api/owner/briefing/send_now
#       calls:   send_morning_brief(CONFIG, DATA_DIR, user["username"])
#       returns: brief dict with `delivery` field populated.
#       purpose: force-send (e.g. owner taps "Send me my brief now").
#                Records the send → should_send_today() will return False
#                for the rest of the day.
#
#   GET  /api/owner/briefing/preferences
#       calls:   get_preferences(user_dir)
#       returns: {enabled, hour, channels}
#
#   PUT  /api/owner/briefing/preferences
#       body:    partial dict — any subset of {enabled, hour, channels}
#       calls:   set_preferences(user_dir, body)
#       returns: full merged prefs after the write.
#       errors:  400 on ValueError (bad hour / bad channel name).
#
# Background scheduler (lives in orbi.py — NOT started from this module so
# the daemon doesn't double-start when briefing.py is imported by route
# handlers). Sketch:
#
#     def briefing_scheduler_loop():
#         time.sleep(30)   # let the rest of boot settle
#         while True:
#             try:
#                 _briefing_tick()
#             except Exception as e:
#                 log.warning(f"briefing scheduler error: {e}")
#             time.sleep(60)
#
#     def _briefing_tick():
#         now_hour = datetime.now(timezone.utc).hour   # or local-tz aware
#         for u in users_mod.list_users(DATA_DIR):
#             user_dir = users_mod.get_user_dir(DATA_DIR, u["username"])
#             if not user_dir.exists():
#                 continue
#             prefs = briefing.get_preferences(user_dir)
#             if not prefs.get("enabled", True):
#                 continue
#             if now_hour < int(prefs.get("hour", 7)):
#                 continue
#             if not briefing.should_send_today(user_dir):
#                 continue
#             try:
#                 briefing.send_morning_brief(CONFIG, DATA_DIR, u["username"])
#                 log.info(f"morning brief sent: {u['username']}")
#             except Exception as e:
#                 log.warning(
#                     f"morning brief failed for {u['username']}: {e}"
#                 )
#
#     threading.Thread(target=briefing_scheduler_loop, daemon=True).start()
#
# ---------------------------------------------------------------------------
