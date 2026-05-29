"""
friend_checkin — proactive "hey, how's it going?" from Orby.

When tone == "friend" and the owner hasn't messaged in a while, send a
push notification reaching out like an actual friend would. The push
opens the dashboard chat with a pre-filled warm opener Orby chose.

Design rules:
- Don't be annoying: max one check-in per ~18 hours, never twice in
  the same calendar day.
- Quiet hours: never fire between 10pm and 7am local time. Friends
  text during the day, not at 2am.
- Tied to inactivity: only fire when last_owner_msg > 12h ago.
- Skip first 24h after install: don't ambush brand-new owners.
- Variety: rotate phrasings so it doesn't feel scripted.

State file: data/.friend_checkin_state.json
  { "last_checkin_ts": int, "owner_last_seen_ts": int }
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path

log = logging.getLogger("orbi.friend_checkin")

STATE_FILE = ".friend_checkin_state.json"

# Quiet hours — local time. Don't message between these.
QUIET_START = dtime(22, 0)     # 10pm
QUIET_END   = dtime(7, 0)      # 7am

CHECKIN_INTERVAL_SECONDS = 18 * 3600    # 18 hours min between check-ins
INACTIVITY_THRESHOLD     = 12 * 3600    # owner silent for 12h+ before reach-out
LOOP_INTERVAL            = 15 * 60      # check every 15 min
INSTALL_GRACE_SECONDS    = 24 * 3600    # skip first 24h after install


# Rotating openers — kept short, real-friend-tone. Owner's first name
# substituted in when available.
CHECKIN_OPENERS = [
    "hey {name} — quiet day on your end. how you doing?",
    "morning {name}. anything on your mind?",
    "haven't heard from you in a bit — everything good?",
    "hey {name}, just checking in. how's the week going?",
    "hi {name} — thinking of you. any wins or weight to share?",
    "you good? noticed it's been a minute.",
    "hey — how'd that thing you were stressed about turn out?",
    "morning {name}. coffee in hand yet?",
    "hi {name}. what's the move today?",
    "hey friend — how's everything with you?",
]


def _state_path(data_dir: Path) -> Path:
    return data_dir / STATE_FILE


def _load_state(data_dir: Path) -> dict:
    p = _state_path(data_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(data_dir: Path, state: dict) -> None:
    p = _state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def mark_owner_active(data_dir: Path) -> None:
    """Call this whenever the owner sends a chat message. Resets the
    inactivity clock so we don't reach out to someone who's mid-conversation."""
    state = _load_state(data_dir)
    state["owner_last_seen_ts"] = int(time.time())
    _save_state(data_dir, state)


def _is_quiet_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    t = now.time()
    if QUIET_START <= QUIET_END:
        return QUIET_START <= t < QUIET_END
    # Wrap-around (22:00 → 07:00)
    return t >= QUIET_START or t < QUIET_END


def _should_check_in(state: dict, install_ts: int) -> tuple[bool, str]:
    """Returns (should, reason). Reason is for logging only."""
    now = time.time()

    if _is_quiet_hours():
        return False, "quiet_hours"

    if (now - install_ts) < INSTALL_GRACE_SECONDS:
        return False, "install_grace"

    last_checkin = state.get("last_checkin_ts", 0)
    if (now - last_checkin) < CHECKIN_INTERVAL_SECONDS:
        return False, "recent_checkin"

    last_owner = state.get("owner_last_seen_ts", install_ts)
    if (now - last_owner) < INACTIVITY_THRESHOLD:
        return False, "owner_active_recently"

    return True, "ready"


def start_checkin_scheduler(config: dict, data_dir: Path,
                             business: dict, notify_callback) -> None:
    """Background thread. Calls notify_callback(title, body) when it's
    time to send a friendly check-in.

    Only runs when tone == 'friend'.
    """
    tone = ((business.get("personality") or {}).get("tone") or "friend").lower()
    if tone != "friend":
        log.info("friend check-ins disabled (tone=%s)", tone)
        return

    # Owner's first name for the opener
    owner_full = ((business.get("personality") or {}).get("owner_name")
                   or business.get("owner_name") or "")
    owner_first = owner_full.split()[0] if owner_full else "there"

    # Use install_ts from config or fall back to "now" if missing
    install_ts = int(config.get("installed_at") or time.time())

    def loop():
        time.sleep(60)   # short startup delay
        while True:
            try:
                state = _load_state(data_dir)
                should, reason = _should_check_in(state, install_ts)
                log.debug(f"friend check-in tick: should={should} reason={reason}")
                if should:
                    opener = random.choice(CHECKIN_OPENERS).format(name=owner_first)
                    try:
                        notify_callback(
                            title="Orby checking in",
                            body=opener,
                        )
                        state["last_checkin_ts"] = int(time.time())
                        _save_state(data_dir, state)
                        log.info(f"friend check-in sent: {opener!r}")
                    except Exception:    # noqa: BLE001
                        log.exception("friend check-in notify_callback crashed")
            except Exception:    # noqa: BLE001
                log.exception("friend check-in loop crashed")
            time.sleep(LOOP_INTERVAL)

    t = threading.Thread(target=loop, daemon=True, name="orbi-friend-checkin")
    t.start()
    log.info(f"friend check-in scheduler started for {owner_first} "
             f"(every {LOOP_INTERVAL}s, inactivity>{INACTIVITY_THRESHOLD}s, "
             f"min interval {CHECKIN_INTERVAL_SECONDS}s)")
