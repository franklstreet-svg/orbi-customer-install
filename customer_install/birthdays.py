"""
birthdays.py — Birthday + anniversary reminder sweep for Orbi.

Walks every user's contacts.json looking for `birthday` / `anniversary`
fields (ISO date or MM-DD), surfaces upcoming ones for the owner
dashboard, drafts a short card message via the LLM that matches the
owner's tone, and (when run on a schedule) drops a reminder into the
owner's reminders.json a few days before the date so they have time
to act on it.

The reminders module already handles delivery. All we do is:
  1. detect upcoming dates
  2. draft text
  3. add(user_dir, drafted_text, due_iso, channel)

IDEMPOTENCY: We tag each contact with a "birthday_sent_<year>" marker
(via contacts.update) the first time we schedule for that year. Sweep
skips contacts that already have the current year's marker. New year
rolls over automatically.

CONTACT FIELDS (added, all optional — old contacts work unchanged):
  birthday    : "1985-06-12"  or  "06-12"     (year is optional)
  anniversary : "2018-09-30"  or  "09-30"
  birthday_sent_<YYYY>      : "2026-06-09T08:00:00Z"  (idempotency marker)
  anniversary_sent_<YYYY>   : "2026-09-27T08:00:00Z"

ROUTES (registered by orbi.py — leave a comment block, no Flask import here):

  GET  /api/owner/birthdays/upcoming?days_ahead=14
       Returns find_upcoming_dates(current_user_dir, days_ahead) as JSON.

  POST /api/owner/birthdays/draft
       Body: {contact_id, kind}
       Looks up contact, calls draft_card_text(CONFIG, contact, kind),
       returns {text}.

  POST /api/owner/birthdays/sweep_now
       Owner-triggered manual sweep. Calls run_sweep(CONFIG, DATA_DIR)
       and returns {created: <count>}.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("orbi.birthdays")

# How many days BEFORE the date to drop the reminder. 3 = enough time
# to grab a card / pick up flowers.
DEFAULT_LEAD_DAYS = 3
# Reminder hour-of-day (UTC) for the scheduled nudge.
DEFAULT_REMINDER_HOUR_UTC = 15  # 8am Pacific-ish, harmless if off

_DATE_FORMATS = ("%Y-%m-%d", "%m-%d", "%m/%d", "%Y/%m/%d")


# ── Date parsing ────────────────────────────────────────────────────────


def _parse_date_field(value: str) -> tuple[int, int] | None:
    """Parse a contact birthday/anniversary field. Year may be missing.
    Returns (month, day) or None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(v, fmt)
            return dt.month, dt.day
        except ValueError:
            continue
    return None


def _next_occurrence(month: int, day: int, today: date) -> date:
    """Find the next date this month/day will occur on or after `today`."""
    year = today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        # Feb 29 in a non-leap year — slide to Feb 28
        if month == 2 and day == 29:
            candidate = date(year, 2, 28)
        else:
            return today + timedelta(days=9999)  # effectively never
    if candidate < today:
        try:
            candidate = date(year + 1, month, day)
        except ValueError:
            candidate = date(year + 1, 2, 28)
    return candidate


# ── Public: find upcoming dates ─────────────────────────────────────────


def find_upcoming_dates(user_dir: Path, days_ahead: int = 14) -> list[dict]:
    """Walk this user's contacts, return any with birthday/anniversary
    occurring in the next `days_ahead` days (inclusive of today)."""
    from modules import contacts as mod_contacts  # lazy

    today = date.today()
    horizon = today + timedelta(days=max(0, int(days_ahead)))
    out: list[dict] = []

    for c in mod_contacts.list_all(user_dir):
        for kind, field in (("birthday", "birthday"),
                            ("anniversary", "anniversary")):
            md = _parse_date_field(c.get(field, ""))
            if not md:
                continue
            next_dt = _next_occurrence(md[0], md[1], today)
            if today <= next_dt <= horizon:
                out.append({
                    "contact_id": c.get("id", ""),
                    "name":       c.get("name", ""),
                    "kind":       kind,
                    "date":       next_dt.strftime("%Y-%m-%d"),
                    "days_until": (next_dt - today).days,
                })

    out.sort(key=lambda r: (r["days_until"], r["name"].lower()))
    return out


# ── Public: LLM-drafted card text ───────────────────────────────────────


def draft_card_text(config: dict, contact: dict, kind: str) -> str:
    """Ask the LLM for a short friendly card message that matches the
    owner's tone. 1–3 sentences. No stage directions, no '[insert name]'
    placeholders — drop the contact's name in directly."""
    name = (contact.get("name") or "").strip() or "friend"
    company = (contact.get("company") or "").strip()
    notes = (contact.get("notes") or "").strip()
    kind_norm = "anniversary" if (kind or "").lower().startswith("ann") else "birthday"

    biz = (config.get("business") or {})
    owner_name = (biz.get("owner_name") or biz.get("name") or "").strip()
    biz_tone_hint = (biz.get("tone") or "warm, casual, brief").strip()

    system = (
        "You write very short personal card messages on behalf of a small "
        "business owner. Match this tone: " + biz_tone_hint + ". "
        "Output ONLY the message body, 1 to 3 sentences. "
        "No 'Dear', no signature line, no stage directions, no brackets, "
        "no quotation marks around the message. Plain text only."
    )
    relation_bit = f" (notes about them: {notes[:160]})" if notes else ""
    company_bit = f" who works at {company}" if company else ""
    sender_bit = f" The sender is {owner_name}." if owner_name else ""
    user_msg = (
        f"Write a {kind_norm} card to {name}{company_bit}.{relation_bit}{sender_bit} "
        "Keep it warm and personal, not corporate. 1–3 sentences."
    )

    try:
        import llm_client  # lazy
        resp = llm_client.generate(config, system, [
            {"role": "user", "content": user_msg}
        ])
        text = (resp.text or "").strip() if resp else ""
    except Exception as e:
        log.warning(f"draft_card_text LLM call failed: {e}")
        text = ""

    if not text:
        # Deterministic fallback so the owner always sees SOMETHING.
        if kind_norm == "anniversary":
            text = f"Happy anniversary, {name} — wishing you both another wonderful year."
        else:
            text = f"Happy birthday, {name}! Hope you have a great day."
        return text

    # Strip stray surrounding quotes the LLM sometimes adds.
    if text.startswith(('"', "'")) and text.endswith(('"', "'")) and len(text) > 2:
        text = text[1:-1].strip()
    return text


# ── Public: gift suggestions ────────────────────────────────────────────


def suggest_gift(config: dict, contact: dict, kind: str,
                 budget_hint: str | None = None,
                 occasion: str | None = None) -> dict:
    """LLM-suggest 3 gift ideas for an upcoming birthday / anniversary /
    milestone. Budget-aware, relationship-aware.

    Returns:
        {
            "suggestions": [{"idea": "...", "rough_cost": "$X-Y", "why": "..."}, ...],
            "needs_budget": True/False,   # True if we should ask the owner
        }

    `budget_hint` examples: "$25-50", "tight", "no limit", "around $100",
                            None (then we may ask the owner via reminder text).
    `occasion` overrides `kind` for things like "graduation", "promotion",
              "first house" — non-birthday milestones.
    """
    name = (contact.get("name") or "").strip() or "them"
    notes = (contact.get("notes") or "").strip()
    relationship = (contact.get("relationship") or contact.get("relation")
                     or contact.get("tags", [""])[0] if contact.get("tags") else "").strip()
    company = (contact.get("company") or "").strip()
    occasion = occasion or kind or "birthday"

    biz = (config.get("business") or {})
    tone_hint = (biz.get("tone") or "warm, casual").strip()

    # If no budget hint, mark for asking the owner casually
    needs_budget = not bool(budget_hint)
    budget_line = (f"Budget: {budget_hint}." if budget_hint
                    else "No budget set yet — give a small/mid/big tier each.")

    system = (
        "You suggest thoughtful, NOT-cheesy gift ideas. You're suggesting "
        "to a friend (the owner) — speak like a friend who happens to "
        "know what good gifts look like. NOT like a corporate sales bot.\n\n"
        "Output STRICT JSON with this exact shape — no preamble, no fences:\n"
        "  {\n"
        "    \"suggestions\": [\n"
        "      {\"idea\": \"3-12 words, concrete\", \"rough_cost\": \"$X-Y\", \"why\": \"1 short sentence — why THIS person would like it\"},\n"
        "      ...3 total...\n"
        "    ]\n"
        "  }\n\n"
        "RULES:\n"
        "- Three suggestions. If budget is given, all three must respect it.\n"
        "- If no budget, give one small-tier (under $25), one mid-tier "
        "  ($25-100), one bigger-tier ($100+). Cover the range.\n"
        "- 'why' must reference SOMETHING about the person — their notes, "
        "  their relationship to the owner, the occasion. Never generic.\n"
        "- Avoid gift cards unless the person is genuinely hard to shop "
        "  for AND the notes show no specific interests.\n"
        "- For spouses/partners: lean experiential or sentimental, not just "
        "  material. For kids: age-appropriate. For coworkers: professional.\n"
        "- Tone: " + tone_hint + ". Don't be cheesy.\n"
    )

    notes_bit = f" Notes about them: {notes[:200]}." if notes else ""
    rel_bit = f" Relationship to the owner: {relationship}." if relationship else ""
    company_bit = f" Works at {company}." if company else ""
    user_msg = (
        f"Suggest gift ideas for {name}'s upcoming {occasion}.{rel_bit}"
        f"{company_bit}{notes_bit} {budget_line}"
    )

    import json as _json
    try:
        import llm_client  # lazy
        resp = llm_client.generate(config, system, [
            {"role": "user", "content": user_msg}
        ])
        raw = (resp.text or "").strip() if resp else ""
    except Exception as e:
        log.warning(f"suggest_gift LLM call failed: {e}")
        raw = ""

    # Parse the JSON — try to extract the {} block if there's filler
    suggestions = []
    if raw:
        import re as _re
        raw = _re.sub(r"^```(?:json)?\s*", "", raw)
        raw = _re.sub(r"\s*```\s*$", "", raw)
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if m:
            try:
                data = _json.loads(m.group(0))
                sug = data.get("suggestions") or []
                for s in sug[:3]:
                    if isinstance(s, dict) and s.get("idea"):
                        suggestions.append({
                            "idea": str(s.get("idea", "")).strip(),
                            "rough_cost": str(s.get("rough_cost", "")).strip(),
                            "why": str(s.get("why", "")).strip(),
                        })
            except _json.JSONDecodeError:
                pass

    if not suggestions:
        # Deterministic fallback so the reminder always has SOMETHING
        suggestions = [
            {"idea": f"A handwritten card with a small thoughtful gesture",
             "rough_cost": "$5-20",
             "why": f"Personal and never wrong, especially when you're unsure."},
        ]

    return {
        "suggestions": suggestions,
        "needs_budget": needs_budget,
    }


def format_gift_line(suggestions_result: dict,
                     budget_unknown_addendum: str = "") -> str:
    """One-line summary suitable for embedding in a reminder text.
    Owner sees this in the push notification.

    If budget was unknown, append a short 'how much you looking to spend?'
    nudge so owner can answer back."""
    sugs = suggestions_result.get("suggestions") or []
    if not sugs:
        return ""
    lines = ["Gift ideas:"]
    for s in sugs[:3]:
        cost = f" ({s['rough_cost']})" if s.get("rough_cost") else ""
        lines.append(f"  · {s['idea']}{cost}")
    text = "\n".join(lines)
    if suggestions_result.get("needs_budget"):
        text += "\n\nWhat range you looking to spend? Tell me and I'll narrow it down."
    return text


# ── Public: scheduled sweep ─────────────────────────────────────────────


def run_sweep(config: dict, data_dir: Path) -> int:
    """Walk every user's contacts, schedule a reminder ~3 days before
    each upcoming birthday/anniversary. Idempotent: each contact gets
    a `birthday_sent_<YYYY>` or `anniversary_sent_<YYYY>` field once
    the reminder is scheduled — sweep skips contacts that already have
    the current-year marker.

    Returns total reminders created across all users.
    """
    from modules import contacts as mod_contacts  # lazy
    from modules import reminders as mod_reminders
    import users as user_registry

    today = date.today()
    lead_days = DEFAULT_LEAD_DAYS

    total = 0
    user_list = []
    try:
        user_list = user_registry.list_users(data_dir)
    except Exception as e:
        log.warning(f"sweep: list_users failed: {e}")
        return 0

    for u in user_list:
        username = u.get("username", "")
        if not username:
            continue
        user_dir = user_registry.get_user_dir(data_dir, username)
        if not user_dir.exists():
            continue

        for c in mod_contacts.list_all(user_dir):
            for kind, field in (("birthday", "birthday"),
                                ("anniversary", "anniversary")):
                md = _parse_date_field(c.get(field, ""))
                if not md:
                    continue
                next_dt = _next_occurrence(md[0], md[1], today)
                # We schedule when we're within (lead_days + 1) of the date.
                days_until = (next_dt - today).days
                if days_until < 0 or days_until > lead_days:
                    continue

                # Idempotency: skip if we already scheduled for this year.
                year_key = f"{kind}_sent_{next_dt.year}"
                if c.get(year_key):
                    continue

                # Draft the card text up-front and stash it in the reminder.
                try:
                    card = draft_card_text(config, c, kind)
                except Exception as e:
                    log.warning(f"sweep: draft failed for {c.get('name')}: {e}")
                    card = f"It's {c.get('name','their')}'s {kind} coming up."

                # Reminder fires the morning of the date (UTC).
                due_dt = datetime(next_dt.year, next_dt.month, next_dt.day,
                                  DEFAULT_REMINDER_HOUR_UTC, 0, 0,
                                  tzinfo=timezone.utc)
                due_iso = due_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                rem_text = (
                    f"{c.get('name','Someone')}'s {kind} — "
                    f"draft card: {card}"
                )

                try:
                    mod_reminders.add(user_dir, rem_text, due_iso, channel="in_app")
                    mod_contacts.update(user_dir, c.get("id", ""),
                                        **{year_key: _now_iso()})
                    total += 1
                    log.info(f"birthday sweep: scheduled {kind} for "
                             f"{c.get('name')} (user={username}, due={due_iso})")
                except Exception as e:
                    log.warning(f"sweep: add reminder failed: {e}")

    return total


# ── Helpers ─────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
