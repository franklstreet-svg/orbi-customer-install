"""
business_info module — hours, services, menu, FAQ, contact info.

The single most important module. This is what Orbi needs to answer
"are you open Sunday?", "how much for an oil change?", "do you deliver?".
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

_LOCK = threading.Lock()

def _path(data_dir: Path) -> Path:
    return data_dir / "business_info.json"

def load(data_dir: Path) -> dict:
    p = _path(data_dir)
    if not p.exists():
        return _empty()
    with _LOCK:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _empty()

def save(data_dir: Path, info: dict) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        # Atomic write: temp file + rename
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(info, indent=2), encoding="utf-8")
        tmp.replace(p)

def _empty() -> dict:
    return {
        "name": "",
        "tagline": "",
        "description": "",
        "address": {"street": "", "city": "", "state": "", "zip": ""},
        "contact": {"phone": "", "email": "", "website": ""},
        "hours": {},
        "services": [],
        "menu": [],
        "faq": [],
        "policies": {"cancellation": "", "returns": "", "payment": ""},
        "personality": {"name": "Orbi", "tone": "friendly_professional"},
    }

# ---------------------------------------------------------------------------
# Look-ups Orbi can call directly
# ---------------------------------------------------------------------------

def hours_today(data_dir: Path) -> str | None:
    """Returns 'Open 9am-5pm' / 'Closed today' / None if hours not set."""
    import datetime
    info = load(data_dir)
    h = info.get("hours", {})
    if not h:
        return None
    today_name = datetime.datetime.now().strftime("%A").lower()
    today = h.get(today_name)
    if not today:
        return None
    if today.get("closed"):
        return "Closed today"
    return f"Open {today.get('open','?')} - {today.get('close','?')}"

def is_open_now(data_dir: Path) -> bool | None:
    """Returns True / False / None if hours not set or unclear."""
    import datetime
    info = load(data_dir)
    h = info.get("hours", {})
    if not h:
        return None
    now = datetime.datetime.now()
    today_name = now.strftime("%A").lower()
    today = h.get(today_name)
    if not today or today.get("closed"):
        return False
    try:
        open_h, open_m = [int(x) for x in today["open"].split(":")]
        close_h, close_m = [int(x) for x in today["close"].split(":")]
    except (ValueError, KeyError):
        return None
    open_minutes  = open_h * 60 + open_m
    close_minutes = close_h * 60 + close_m
    now_minutes   = now.hour * 60 + now.minute
    return open_minutes <= now_minutes < close_minutes

def find_service_or_menu_item(data_dir: Path, query: str) -> list[dict]:
    """Loose search across services + menu items by name. Returns matching entries."""
    info = load(data_dir)
    q = query.lower().strip()
    if not q:
        return []
    results = []
    for s in info.get("services", []) or []:
        if not isinstance(s, dict):
            continue
        if q in (s.get("name", "") or "").lower():
            results.append({"kind": "service", **s})
    for section in info.get("menu", []) or []:
        if not isinstance(section, dict):
            continue
        for item in section.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            if q in (item.get("name", "") or "").lower():
                results.append({"kind": "menu", "section": section.get("section"), **item})
    return results
