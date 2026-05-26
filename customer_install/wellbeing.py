"""
wellbeing — crisis-signal detection on visitor and caller messages.

When a customer of the business sends a message (or calls and speaks)
that contains crisis or distress language, this module:

  1. Returns a level flag ('crisis' / 'distress' / 'ok') to /chat and /voice
  2. Provides a system-prompt injection that tells Orbi to slow down, be
     warm, and naturally surface crisis resources (988, 741741, 911)
  3. Logs the event to data_dir/wellbeing_flags.json so the owner sees
     it in their dashboard and can follow up

Why this matters for B2B Orbi:
  - Any business with public-facing chat will eventually get a visitor
    in distress (medical practices, salons, schools, daycares, fitness,
    therapy-adjacent — all of them).
  - The owner has BOTH a legal and a moral obligation to handle it well.
  - This module makes sure Orbi doesn't blow past warning signs in
    pursuit of "answer the question, capture the lead."

Ported from /home/frank/orby_5050/engine/wellbeing.py (2026-05-26).
Adapted to customer_install conventions (self-contained, atomic writes,
data_dir parameter instead of engine.storage import).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("orbi.wellbeing")

_LOCK = threading.Lock()
FLAGS_FILE = "wellbeing_flags.json"


# Phrases that indicate an active crisis (immediate self-harm / suicide ideation).
# Conservative list — false positives are tolerable; false negatives are not.
CRISIS_SIGNALS = [
    "want to die", "want to kill myself", "kill myself", "end my life",
    "don't want to be here", "dont want to be here",
    "don't want to live", "dont want to live", "no reason to live",
    "better off without me", "everyone would be better off",
    "won't have to deal with this", "won't have to worry",
    "giving up", "can't go on", "cant go on",
    "can't do this anymore", "cant do this anymore",
    "thinking about suicide", "thinking about ending",
    "hurt myself", "cutting myself", "self harm", "self-harm",
    "overdose", "take all my pills",
    "say goodbye", "final goodbye", "last message",
    "nobody cares", "nobody would miss me", "no one would miss me",
]

# Phrases that indicate emotional distress (not immediate crisis but warrants care).
DISTRESS_SIGNALS = [
    "feeling hopeless", "completely hopeless", "no hope left",
    "so depressed", "really depressed", "deeply depressed",
    "can't stop crying", "cant stop crying", "crying all the time",
    "feel empty", "feel nothing", "feel numb",
    "totally alone", "completely alone", "so lonely", "so alone",
    "worthless", "i'm worthless", "im worthless", "feel worthless",
    "exhausted", "so tired of everything",
]

# Resources surfaced when crisis level fires.
CRISIS_RESOURCES = (
    "988 Suicide & Crisis Lifeline — call or text 988 (available 24/7)\n"
    "Crisis Text Line — text HOME to 741741\n"
    "Emergency — 911"
)


def check_message(text: str) -> dict:
    """Scan a single message for crisis or distress signals.
    Returns {'level': 'crisis' | 'distress' | 'ok', 'signal': str | None}.
    Crisis takes precedence over distress. Never raises."""
    if not text:
        return {"level": "ok", "signal": None}
    lower = text.lower()
    for signal in CRISIS_SIGNALS:
        if signal in lower:
            return {"level": "crisis", "signal": signal}
    for signal in DISTRESS_SIGNALS:
        if signal in lower:
            return {"level": "distress", "signal": signal}
    return {"level": "ok", "signal": None}


def get_crisis_context() -> str:
    """System-prompt addendum to inject when crisis signals fire. Tells
    Orbi to slow down, stay present, and surface resources naturally."""
    return (
        "WELLBEING ALERT — CRISIS SIGNALS DETECTED IN THIS MESSAGE.\n"
        "Stay warm and present. Do NOT change tone dramatically. Do NOT end the conversation.\n"
        "Respond with genuine care. Gently introduce crisis resources naturally in conversation.\n"
        "Do NOT ask 'are you thinking about suicide' in a cold clinical way.\n"
        "Do NOT minimize their feelings. Do NOT route them straight to the business owner.\n"
        f"Resources you can mention naturally:\n{CRISIS_RESOURCES}\n"
        "After acknowledging them, you can also offer to take a message for the owner — "
        "but only AFTER you've been present with what they're going through."
    )


def get_distress_context() -> str:
    """System-prompt addendum for distress (less acute than crisis).
    Asks Orbi to slow down, listen, mention 988 as an option."""
    return (
        "WELLBEING NOTE — This person seems to be struggling emotionally.\n"
        "Be extra warm, slow down, listen first. Ask open caring questions.\n"
        "Match their energy — if they're low, be gentle and present.\n"
        "You can mention: 'Sometimes talking to someone who specializes in this helps — "
        "988 is always there if you ever need it.'"
    )


def log_flag(data_dir: Path, level: str, signal: str | None,
             message_preview: str, source: str = "chat") -> None:
    """Append a wellbeing flag to data_dir/wellbeing_flags.json so the
    owner sees it in their dashboard. Atomic write. Never raises.

    source is 'chat' / 'phone' so the owner knows the surface."""
    if not level or level == "ok":
        return
    record = {
        "level":   level,
        "signal":  signal,
        "preview": (message_preview or "")[:200],
        "source":  source,
        "at":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = data_dir / FLAGS_FILE
    with _LOCK:
        try:
            existing = []
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8")) or []
                except (json.JSONDecodeError, OSError):
                    existing = []
            existing.append(record)
            # Keep only the last 500 flags to bound file size
            if len(existing) > 500:
                existing = existing[-500:]
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)
            log.info("wellbeing flag logged: level=%s source=%s", level, source)
        except Exception as e:
            log.warning("could not log wellbeing flag: %s", e)


def list_flags(data_dir: Path, limit: int = 50) -> list[dict]:
    """Owner-dashboard helper. Newest first."""
    path = data_dir / FLAGS_FILE
    if not path.exists():
        return []
    try:
        flags = json.loads(path.read_text(encoding="utf-8")) or []
    except (json.JSONDecodeError, OSError):
        return []
    flags.sort(key=lambda f: f.get("at", ""), reverse=True)
    return flags[:limit]
