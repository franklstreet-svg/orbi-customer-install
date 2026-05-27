"""
booking.py — public "book a time with me" widget backend.

Visitors land on /book?u=<username> (or click an embedded widget on the
customer's own site), see the owner's open slots, pick one, and submit
their contact info. We:

    1. Re-validate the slot is still free (race-condition guard — see
       _slot_is_still_free() below; this is the only thing standing
       between us and double-bookings since the booking page sits in
       front of a polling cache of slots).
    2. Create a calendar event in the owner's per-user calendar.json.
    3. Capture a "callback"-typed message so the owner sees the booking
       in the unified Messages inbox alongside chat leads.
    4. Push to Google Calendar if the owner has connected it.
    5. Fire notifications.send() so the owner gets push/email/SMS ping
       on the new booking immediately.

DESIGN NOTES
------------
- This module is a public-surface wrapper around scheduler.find_open_slots
  / modules.calendar.add. The wrapper exists for one reason: visitors must
  NEVER see other people's busy event titles, locations, or notes.
  scheduler.find_open_slots already returns only free slots (not the busy
  blocks), so the filtering boundary is naturally clean — but we still
  strip any unexpected keys defensively before returning to the visitor.

- Config lives at <user_dir>/booking_config.json. Sensible defaults if
  missing so that turning on bookings is a single click from the
  dashboard. Owners can disable bookings entirely by setting
  enabled=False (the public endpoints then return 404, not "disabled"
  — we don't want to leak that the user even exists).

- Race condition: between the visitor loading the slot grid and clicking
  submit, the owner (or another visitor) might fill the slot. We re-run
  find_open_slots() at book time and only proceed if our chosen
  start_iso is still present. This is cheap (~1 file read of
  calendar.json) and avoids the complexity of a reservation/expiry
  table for what is, in practice, a low-throughput operation.

ROUTE SURFACE — see bottom of file. The orchestrator wires these into
orbi.py; this module does NOT touch orbi.py itself.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("orbi.booking")

_CONFIG_LOCK = threading.Lock()
_CONFIG_FILE = "booking_config.json"

# Defaults applied when the owner hasn't written a booking_config.json yet.
# These intentionally match scheduler.find_open_slots defaults so the
# behaviour is consistent between the owner-side scheduler and the
# visitor-side widget.
_DEFAULTS = {
    "enabled":           False,         # off by default — owner opts in
    "duration_minutes":  30,
    "days_ahead":        14,
    "working_hours":     [9, 17],
    "tz":                "UTC",         # display only — slots are stored UTC
    "require_phone":     False,
    "custom_intro_text": "",
}


# ---------------------------------------------------------------------------
# Public API: config
# ---------------------------------------------------------------------------

def get_booking_config(config, user_dir) -> dict:
    """Return the public widget config the owner has set. Always returns
    a fully-populated dict — missing keys are filled with _DEFAULTS so
    callers never need to None-check fields."""
    user_dir = Path(user_dir)
    p = user_dir / _CONFIG_FILE
    cfg = dict(_DEFAULTS)
    if p.exists():
        try:
            stored = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                cfg.update(stored)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("booking: read config failed for %s: %s", user_dir, exc)

    # Normalize/clamp values
    cfg["duration_minutes"] = max(5, min(240, int(cfg.get("duration_minutes") or 30)))
    cfg["days_ahead"]       = max(1, min(60, int(cfg.get("days_ahead") or 14)))
    wh = cfg.get("working_hours") or [9, 17]
    try:
        wh_start = max(0, min(23, int(wh[0])))
        wh_end   = max(wh_start + 1, min(24, int(wh[1])))
    except (ValueError, TypeError, IndexError):
        wh_start, wh_end = 9, 17
    cfg["working_hours"] = [wh_start, wh_end]
    cfg["enabled"]       = bool(cfg.get("enabled"))
    cfg["require_phone"] = bool(cfg.get("require_phone"))
    cfg["tz"]            = (cfg.get("tz") or "UTC").strip() or "UTC"
    cfg["custom_intro_text"] = (cfg.get("custom_intro_text") or "").strip()
    return cfg


def set_booking_config(user_dir, cfg: dict) -> dict:
    """Owner-only — write the booking config atomically. Returns the
    normalised config that was actually stored."""
    user_dir = Path(user_dir)
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / _CONFIG_FILE

    merged = dict(_DEFAULTS)
    if isinstance(cfg, dict):
        # Only accept the fields we know about — ignore stray keys so a
        # malicious owner-side dashboard bug can't poison the file.
        for key in _DEFAULTS:
            if key in cfg:
                merged[key] = cfg[key]

    # Re-run through the same normalisation as get_booking_config().
    normalized = _normalize_for_store(merged)

    with _CONFIG_LOCK:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
        tmp.replace(p)
    log.info("booking: config updated for %s (enabled=%s, dur=%dm)",
             user_dir, normalized["enabled"], normalized["duration_minutes"])
    return normalized


def _normalize_for_store(cfg: dict) -> dict:
    """Same clamps as get_booking_config but on an explicit input dict —
    used by set_booking_config so the file we write is already valid."""
    out = dict(_DEFAULTS)
    out.update(cfg or {})
    out["duration_minutes"] = max(5, min(240, int(out.get("duration_minutes") or 30)))
    out["days_ahead"]       = max(1, min(60, int(out.get("days_ahead") or 14)))
    wh = out.get("working_hours") or [9, 17]
    try:
        wh_start = max(0, min(23, int(wh[0])))
        wh_end   = max(wh_start + 1, min(24, int(wh[1])))
    except (ValueError, TypeError, IndexError):
        wh_start, wh_end = 9, 17
    out["working_hours"] = [wh_start, wh_end]
    out["enabled"]       = bool(out.get("enabled"))
    out["require_phone"] = bool(out.get("require_phone"))
    out["tz"]            = (out.get("tz") or "UTC").strip() or "UTC"
    out["custom_intro_text"] = (out.get("custom_intro_text") or "").strip()
    return out


# ---------------------------------------------------------------------------
# Public API: list open slots (visitor-safe)
# ---------------------------------------------------------------------------

def get_public_availability(config, data_dir, user_dir,
                            duration_minutes: int = 30,
                            days_ahead: int = 14) -> list[dict]:
    """Visitor-safe wrapper around scheduler.find_open_slots. Returns
    only what's safe to expose publicly — no busy event titles,
    locations, notes, or attendee lists.

    Each slot dict is:
        {
          "start_iso":  "2026-05-28T14:00:00Z",
          "end_iso":    "2026-05-28T14:30:00Z",
          "day_label":  "Thursday May 28",
          "time_label": "2:00 PM",
          "available":  True
        }
    """
    user_dir = Path(user_dir)
    cfg = get_booking_config(config, user_dir)
    # Caller-supplied values win, but fall back to the owner's config.
    dur  = max(5, int(duration_minutes or cfg["duration_minutes"]))
    days = max(1, int(days_ahead or cfg["days_ahead"]))
    wh   = tuple(cfg["working_hours"])

    try:
        import scheduler
    except Exception as exc:    # noqa: BLE001
        log.warning("booking.get_public_availability: scheduler import failed: %s", exc)
        return []

    raw = scheduler.find_open_slots(
        user_dir,
        duration_minutes=dur,
        days_ahead=days,
        working_hours=wh,
    )

    # Defensive filter — only expose the four well-known keys. Any
    # future scheduler change that adds a "busy_title" or similar
    # field can't accidentally leak through this boundary.
    public_slots: list[dict] = []
    for s in raw or []:
        public_slots.append({
            "start_iso":  s.get("start_iso", ""),
            "end_iso":    s.get("end_iso", ""),
            "day_label":  s.get("day_label", ""),
            "time_label": s.get("time_label", ""),
            "available":  True,
        })
    return public_slots


# ---------------------------------------------------------------------------
# Public API: book a slot (visitor-initiated)
# ---------------------------------------------------------------------------

def book_public_slot(config, data_dir, user_dir, *,
                     visitor_name: str,
                     visitor_email: str,
                     visitor_phone: str = "",
                     start_iso: str,
                     end_iso: str,
                     duration_minutes: int = 30,
                     notes: str = "") -> dict:
    """Book a slot for a visitor. See module docstring for the flow.

    Returns:
        {
          "event_id":         "<12-char hex>",
          "confirmation":     "ok",
          "calendar_synced":  bool,        # True if Google Calendar push succeeded
        }

    Raises:
        ValueError — if required fields are missing, the slot is no
            longer available, or bookings are disabled for this owner.
    """
    user_dir = Path(user_dir)
    data_dir = Path(data_dir)

    name  = (visitor_name or "").strip()
    email = (visitor_email or "").strip()
    phone = (visitor_phone or "").strip()
    notes = (notes or "").strip()

    # ── Validate required fields ────────────────────────────────────
    cfg = get_booking_config(config, user_dir)
    if not cfg.get("enabled"):
        raise ValueError("bookings are not enabled for this owner")
    if not name:
        raise ValueError("visitor_name is required")
    if not email or "@" not in email:
        raise ValueError("a valid visitor_email is required")
    if cfg.get("require_phone") and not phone:
        raise ValueError("visitor_phone is required for this owner")
    if not start_iso or not end_iso:
        raise ValueError("start_iso and end_iso are required")

    # ── Race-condition guard ────────────────────────────────────────
    # Re-fetch live availability and confirm our slot is still free.
    # See module docstring for why we do this here instead of locking.
    if not _slot_is_still_free(config, data_dir, user_dir,
                               start_iso=start_iso,
                               duration_minutes=duration_minutes,
                               days_ahead=cfg["days_ahead"]):
        log.info("booking: slot %s already taken (visitor %s / %s)",
                 start_iso, name, email)
        raise ValueError("that time was just taken — please pick another")

    # ── 1. Create the calendar event ────────────────────────────────
    try:
        import modules.calendar as cal
    except Exception as exc:    # noqa: BLE001
        log.warning("booking.book_public_slot: calendar import failed: %s", exc)
        raise ValueError(f"calendar unavailable: {exc}") from exc

    title = f"[Booking] {name}"
    attendees = [a for a in (name, email, phone) if a]
    event_notes = _format_notes(name, email, phone, notes)

    event = cal.add(
        user_dir,
        title=title,
        start=start_iso,
        end=end_iso,
        notes=event_notes,
        with_=attendees,
    )
    event_id = event.get("id", "")
    log.info("booking: created event %s (%s -> %s) for %s",
             event_id, start_iso, end_iso, email or name)

    # ── 2. Capture as a "callback" message for the inbox ────────────
    try:
        import modules.messages as messages
        body = (
            f"New booking: {name} — {_human_time(start_iso, cfg.get('tz'))}\n"
            f"Duration: {duration_minutes} min\n"
            f"Email: {email}\n"
            + (f"Phone: {phone}\n" if phone else "")
            + (f"\nNotes from visitor:\n{notes}" if notes else "")
        )
        messages.capture(
            data_dir,
            msg_type="callback",
            from_name=name,
            from_phone=phone or None,
            from_email=email,
            body=body,
            source="booking",
            meta={
                "event_id":         event_id,
                "start_iso":        start_iso,
                "end_iso":          end_iso,
                "duration_minutes": duration_minutes,
            },
            config=config,
        )
    except Exception as exc:    # noqa: BLE001
        log.warning("booking: messages.capture failed (non-fatal): %s", exc)

    # ── 3. Push to Google Calendar if connected ─────────────────────
    calendar_synced = False
    try:
        import gcal
        if gcal.is_connected(user_dir):
            res = gcal.push_to_google(user_dir, event_id) or {}
            calendar_synced = bool(res.get("pushed"))
            if calendar_synced:
                log.info("booking: pushed event %s to Google", event_id)
            else:
                log.warning("booking: gcal push failed for %s: %s",
                            event_id, res.get("error", ""))
    except Exception as exc:    # noqa: BLE001 — gcal is optional
        log.info("booking: gcal unavailable, local-only booking: %s", exc)

    # ── 4. Notify the owner ─────────────────────────────────────────
    try:
        import notifications as notify
        notify.send(
            config, data_dir,
            event="new_lead",
            title="New booking",
            body=f"{name} booked you at {_human_time(start_iso, cfg.get('tz'))}",
            url="/owner/messages",
        )
    except Exception as exc:    # noqa: BLE001
        log.warning("booking: notify.send failed (non-fatal): %s", exc)

    return {
        "event_id":        event_id,
        "confirmation":    "ok",
        "calendar_synced": calendar_synced,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _slot_is_still_free(config, data_dir, user_dir, *,
                        start_iso: str,
                        duration_minutes: int,
                        days_ahead: int) -> bool:
    """Re-fetch live availability and check if start_iso is still in the
    list. This is the race-condition guard — two visitors could load the
    booking page at the same time, but only one can win the slot."""
    slots = get_public_availability(
        config, data_dir, user_dir,
        duration_minutes=duration_minutes,
        days_ahead=days_ahead,
    )
    return any(s.get("start_iso") == start_iso for s in slots)


def _format_notes(name: str, email: str, phone: str, notes: str) -> str:
    """Build the calendar-event notes block. Keeps the same shape across
    bookings so the owner can scan it quickly."""
    lines = [f"Booked via Orbi public widget."]
    lines.append(f"Visitor: {name}")
    lines.append(f"Email:   {email}")
    if phone:
        lines.append(f"Phone:   {phone}")
    if notes:
        lines.append("")
        lines.append("Notes from visitor:")
        lines.append(notes)
    return "\n".join(lines)


def _human_time(iso: str, tz_label: str | None = None) -> str:
    """Render '2026-05-28T14:00:00Z' as 'Thu May 28, 2:00 PM' for
    notification bodies. Falls back to the raw ISO on parse failure."""
    if not iso:
        return ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")) \
            .astimezone(timezone.utc)
        h24 = dt.hour
        h12 = h24 % 12 or 12
        ampm = "AM" if h24 < 12 else "PM"
        suffix = f" {tz_label}" if tz_label and tz_label.upper() != "UTC" else ""
        return f"{dt.strftime('%a %b %-d')}, {h12}:{dt.minute:02d} {ampm}{suffix}"
    except (ValueError, AttributeError):
        return iso


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# PUBLIC routes (no auth — must validate `u` is an existing, active user
# whose booking config has enabled=True; otherwise return 404, NOT a
# "disabled" message — we don't want to leak which usernames exist):
#
#   GET /book?u=<username>
#       static — serves /static/booking.html. JS on the page reads the
#       `u` query param and calls the two endpoints below.
#
#   GET /api/public/booking/slots?u=<username>&duration=30
#       calls:   booking.get_public_availability(CONFIG, DATA_DIR,
#                    users.get_user_dir(DATA_DIR, u),
#                    duration_minutes=duration,
#                    days_ahead=<from owner's config>)
#       returns: {
#                  "business_name":      "...",
#                  "owner_name":         "...",
#                  "intro_text":         "...",
#                  "duration_minutes":   30,
#                  "require_phone":      false,
#                  "tz":                 "UTC",
#                  "slots":              [ { start_iso, end_iso,
#                                            day_label, time_label,
#                                            available }, ... ]
#                }
#       404 if user doesn't exist OR booking_config.enabled is false.
#
#   POST /api/public/booking/book
#       body:    {
#                  "u":             "<username>",
#                  "visitor_name":  "...",
#                  "visitor_email": "...",
#                  "visitor_phone": "...",       # optional
#                  "start_iso":     "...",
#                  "end_iso":       "...",
#                  "duration_minutes": 30,
#                  "notes":         "..."        # optional
#                }
#       calls:   booking.book_public_slot(CONFIG, DATA_DIR,
#                    users.get_user_dir(DATA_DIR, u), ...)
#       returns: { "event_id": "...", "confirmation": "ok",
#                  "calendar_synced": bool }
#       400 on validation failure (race-condition loss, missing fields,
#       bookings disabled). 404 if user missing.
#
# OWNER routes (require_owner cookie auth):
#
#   GET /api/owner/booking/config
#       calls:   booking.get_booking_config(CONFIG, OWNER_USER_DIR)
#       returns: full normalised config dict
#
#   PUT /api/owner/booking/config
#       body:    partial or full config dict
#       calls:   booking.set_booking_config(OWNER_USER_DIR, body)
#       returns: the normalised config that was actually stored
#
# ---------------------------------------------------------------------------
