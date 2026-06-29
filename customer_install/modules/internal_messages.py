"""
modules/internal_messages — staff-to-staff messaging within one Orby install.

The CRM-style messages module (modules/messages.py) handles INBOUND customer
messages. This module handles INTERNAL employee-to-employee chat — "Cathi,
covering your shift tomorrow", "Joe, the order is ready", etc.

Two send paths from the dashboard:
  1. Direct compose: employee types in a form, picks recipient, hits send
  2. Tell Orby: "tell Cathi the meeting moved to 3pm" → Orby's chat detects
     the intent, resolves "Cathi" to her username, sends the message

Storage: single shared file (filtered on read so each user sees their own
inbox + outbox). Keeps everything in one place + auditable; if the install
ever scales to thousands of internal messages we can shard per-user.

File: data/internal_messages.json
  {
    "messages": [
      {
        "id":          "im_abc123",
        "from":        "frank",       (lowercase username)
        "from_name":   "Frank Street",(display name at send time — snapshot)
        "to":          "cathi",       (lowercase username)
        "to_name":     "Cathi Brown",
        "body":        "Order #4521 is ready for pickup",
        "via":         "manual" | "orby",  (typed vs Orby-sent on behalf)
        "created_at":  1780000000,
        "read_at":     null | 1780000060
      }
    ]
  }
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("orbi.internal_messages")

_LOCK = threading.Lock()
FILE = "internal_messages.json"


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE


def _load(data_dir: Path) -> dict:
    p = _path(data_dir)
    if not p.exists():
        return {"messages": [], "groups": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "messages" not in data:
            return {"messages": [], "groups": []}
        if "groups" not in data:
            data["groups"] = []
        return data
    except (json.JSONDecodeError, OSError):
        return {"messages": [], "groups": []}


def _save(data_dir: Path, data: dict) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def send(data_dir: Path, *,
         from_user: str, to_user: str,
         body: str, via: str = "manual",
         from_name: str = "", to_name: str = "") -> dict:
    """Send an internal message. Returns the stored entry."""
    from_user = (from_user or "").strip().lower()
    to_user = (to_user or "").strip().lower()
    body = (body or "").strip()
    if not from_user or not to_user:
        raise ValueError("from + to required")
    if not body:
        raise ValueError("body required")
    if from_user == to_user:
        raise ValueError("cannot message yourself")
    with _LOCK:
        data = _load(data_dir)
        entry = {
            "id":         "im_" + uuid.uuid4().hex[:10],
            "from":       from_user,
            "from_name":  from_name or from_user,
            "to":         to_user,
            "to_name":    to_name or to_user,
            "body":       body[:4000],
            "via":        via,
            "created_at": int(time.time()),
            "read_at":    None,
        }
        data["messages"].append(entry)
        _save(data_dir, data)
    log.info(f"internal msg: {from_user} → {to_user} ({via}, {len(body)} chars)")
    return entry


def list_for_user(data_dir: Path, username: str,
                  limit: int = 100, only_unread: bool = False,
                  active_usernames: list | None = None) -> list[dict]:
    """All messages where the user is sender OR recipient. Newest first.

    Includes group messages where the user is a current group member
    (for stored groups) or where the user is active (for the special
    __all__ "Whole Team" group). active_usernames is needed to resolve
    the __all__ virtual group; pass list of active staff usernames.
    """
    u = (username or "").strip().lower()
    if not u:
        return []
    data = _load(data_dir)
    # Build group membership map for fast lookup
    group_members = {g["id"]: set(g.get("members", [])) for g in data.get("groups", [])}
    active_set = {(a or "").strip().lower() for a in (active_usernames or [])}
    out = []
    for m in data["messages"]:
        gid = m.get("group_id")
        if gid:
            if gid == "__all__":
                in_group = u in active_set
            else:
                in_group = u in group_members.get(gid, set())
            if in_group:
                out.append(m)
        else:
            if m.get("to") == u or m.get("from") == u:
                out.append(m)
    if only_unread:
        def _is_unread(m):
            if m.get("group_id"):
                return u not in set(m.get("read_by", []))
            return m.get("to") == u and not m.get("read_at")
        out = [m for m in out if _is_unread(m)]
    out.sort(key=lambda m: m.get("created_at", 0), reverse=True)
    return out[:limit]


def unread_count(data_dir: Path, username: str,
                  active_usernames: list | None = None) -> int:
    return len(list_for_user(data_dir, username, only_unread=True,
                              limit=10000, active_usernames=active_usernames))


def mark_read(data_dir: Path, message_id: str, by_user: str) -> bool:
    """Mark a message as read. Only the RECIPIENT can mark their own.
    Returns True if updated, False if not found or unauthorized."""
    by = (by_user or "").strip().lower()
    with _LOCK:
        data = _load(data_dir)
        for m in data["messages"]:
            if m.get("id") == message_id and m.get("to") == by:
                if not m.get("read_at"):
                    m["read_at"] = int(time.time())
                _save(data_dir, data)
                return True
    return False


def mark_all_read(data_dir: Path, by_user: str,
                   active_usernames: list | None = None) -> int:
    """Mark all messages addressed to this user as read (1-on-1 + groups
    where they're a member). Returns count newly marked."""
    by = (by_user or "").strip().lower()
    now = int(time.time())
    n = 0
    active_set = {(a or "").strip().lower() for a in (active_usernames or [])}
    with _LOCK:
        data = _load(data_dir)
        group_members = {g["id"]: set(g.get("members", []))
                          for g in data.get("groups", [])}
        for m in data["messages"]:
            gid = m.get("group_id")
            if gid:
                if gid == ALL_GROUP_ID:
                    in_group = by in active_set
                else:
                    in_group = by in group_members.get(gid, set())
                if in_group:
                    rb = m.get("read_by") or []
                    if by not in rb:
                        rb.append(by)
                        m["read_by"] = rb
                        n += 1
            else:
                if m.get("to") == by and not m.get("read_at"):
                    m["read_at"] = now
                    n += 1
        if n:
            _save(data_dir, data)
    return n


def thread_with(data_dir: Path, username: str, other_user: str,
                 limit: int = 200) -> list[dict]:
    """Just the messages between username and other_user (both directions),
    oldest first so it reads like a chat transcript."""
    u = (username or "").strip().lower()
    o = (other_user or "").strip().lower()
    data = _load(data_dir)
    out = [m for m in data["messages"]
           if (m.get("from") == u and m.get("to") == o) or
              (m.get("from") == o and m.get("to") == u)]
    out.sort(key=lambda m: m.get("created_at", 0))
    return out[-limit:]


# ── Groups (multi-recipient threads, iMessage-style) ───────────────────────
# A group has an id, name, members (usernames), and a creator. Messages sent
# to a group carry group_id and to=="__group__" so they don't collide with
# 1-on-1 messages. Everyone in the group sees the thread; replies are also
# group messages, so everyone sees those too — like an iMessage group chat.
#
# The sentinel id "__all__" is the virtual "Whole Team" group — never stored.
# When a message goes to __all__, all active staff at send time can read it.

ALL_GROUP_ID = "__all__"
ALL_GROUP_NAME = "Whole Team"


def list_groups(data_dir: Path, for_user: str | None = None) -> list[dict]:
    """List stored groups. If for_user is set, only groups they're in."""
    data = _load(data_dir)
    groups = list(data.get("groups", []))
    if for_user:
        u = (for_user or "").strip().lower()
        groups = [g for g in groups if u in g.get("members", [])]
    groups.sort(key=lambda g: (g.get("name") or "").lower())
    return groups


def get_group(data_dir: Path, group_id: str) -> dict | None:
    if group_id == ALL_GROUP_ID:
        return {"id": ALL_GROUP_ID, "name": ALL_GROUP_NAME,
                "members": [], "virtual": True}
    data = _load(data_dir)
    for g in data.get("groups", []):
        if g.get("id") == group_id:
            return g
    return None


def create_group(data_dir: Path, *, name: str, members: list,
                  created_by: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("group name required")
    members = sorted({(m or "").strip().lower()
                       for m in (members or []) if (m or "").strip()})
    if len(members) < 2:
        raise ValueError("a group needs at least 2 members")
    with _LOCK:
        data = _load(data_dir)
        entry = {
            "id":         "g_" + uuid.uuid4().hex[:10],
            "name":       name[:80],
            "members":    members,
            "created_by": (created_by or "").strip().lower(),
            "created_at": int(time.time()),
        }
        data["groups"].append(entry)
        _save(data_dir, data)
    log.info(f"group created: {entry['id']} '{name}' ({len(members)} members)")
    return entry


def update_group(data_dir: Path, group_id: str, *,
                  name: str | None = None,
                  members: list | None = None) -> dict | None:
    with _LOCK:
        data = _load(data_dir)
        for g in data.get("groups", []):
            if g.get("id") == group_id:
                if name is not None:
                    new = (name or "").strip()[:80]
                    if new:
                        g["name"] = new
                if members is not None:
                    g["members"] = sorted({(m or "").strip().lower()
                                            for m in members if (m or "").strip()})
                _save(data_dir, data)
                return g
    return None


def delete_group(data_dir: Path, group_id: str) -> bool:
    with _LOCK:
        data = _load(data_dir)
        before = len(data.get("groups", []))
        data["groups"] = [g for g in data.get("groups", [])
                           if g.get("id") != group_id]
        if len(data["groups"]) != before:
            _save(data_dir, data)
            return True
    return False


def send_to_group(data_dir: Path, *,
                   group_id: str, group_name: str,
                   member_usernames: list,
                   from_user: str, from_name: str,
                   body: str, via: str = "manual") -> dict:
    """Send a message to a group. member_usernames is the resolved list at
    send time (for __all__ that's all active staff; for stored groups it's
    the group's member list). Stored on the message for audit/snapshot."""
    body = (body or "").strip()
    if not body:
        raise ValueError("body required")
    fu = (from_user or "").strip().lower()
    if not fu:
        raise ValueError("from required")
    members = sorted({(m or "").strip().lower()
                       for m in (member_usernames or []) if (m or "").strip()})
    with _LOCK:
        data = _load(data_dir)
        entry = {
            "id":         "im_" + uuid.uuid4().hex[:10],
            "from":       fu,
            "from_name":  from_name or fu,
            "to":         "__group__",
            "to_name":    group_name or "Group",
            "group_id":   group_id,
            "group_members_at_send": members,
            "body":       body[:4000],
            "via":        via,
            "created_at": int(time.time()),
            "read_by":    [fu],   # sender has auto-read their own message
        }
        data["messages"].append(entry)
        _save(data_dir, data)
    log.info(f"group msg: {fu} → {group_id} ({via}, {len(body)} chars, "
              f"{len(members)} recipients)")
    return entry


def group_thread(data_dir: Path, group_id: str,
                  limit: int = 200) -> list[dict]:
    """Return the chronological thread for a group (oldest first)."""
    data = _load(data_dir)
    out = [m for m in data["messages"] if m.get("group_id") == group_id]
    out.sort(key=lambda m: m.get("created_at", 0))
    return out[-limit:]


def mark_group_read(data_dir: Path, group_id: str, by_user: str) -> int:
    """Mark all group messages as read by this user. Returns count newly
    marked."""
    u = (by_user or "").strip().lower()
    if not u:
        return 0
    n = 0
    with _LOCK:
        data = _load(data_dir)
        for m in data["messages"]:
            if m.get("group_id") == group_id:
                rb = m.get("read_by") or []
                if u not in rb:
                    rb.append(u)
                    m["read_by"] = rb
                    n += 1
        if n:
            _save(data_dir, data)
    return n


# ── Natural-language detection ─────────────────────────────────────────────
# Catches phrasings like:
#   "tell Cathi I'll be 10 minutes late"
#   "let Joe know the order is ready"
#   "send Sarah a message that the meeting moved to 3pm"
#   "message Tom about the deli order"
#   "ping Maria — printer is down"

import re as _re

_INTENT_PATTERNS = [
    # "tell <name> <message>"
    _re.compile(r"^\s*(?:please\s+|can\s+you\s+(?:please\s+)?)?"
                 r"tell\s+(?P<name>[A-Za-z][a-zA-Z]{1,30})\s+(?P<body>.+)",
                 _re.IGNORECASE),
    # "let <name> know <message>"
    _re.compile(r"^\s*(?:please\s+|can\s+you\s+(?:please\s+)?)?"
                 r"let\s+(?P<name>[A-Za-z][a-zA-Z]{1,30})\s+know\s+"
                 r"(?:that\s+)?(?P<body>.+)",
                 _re.IGNORECASE),
    # "send <name> a message (that/about/saying/:) <body>"
    _re.compile(r"^\s*(?:please\s+|can\s+you\s+(?:please\s+)?)?"
                 r"send\s+(?P<name>[A-Za-z][a-zA-Z]{1,30})\s+(?:a\s+)?"
                 r"(?:message|note|text|dm|im)\s*"
                 r"(?:that\s+|about\s+|saying\s+|[:\-—]\s*)?(?P<body>.+)",
                 _re.IGNORECASE),
    # "message <name> (that/about) <body>"  /  "ping <name> — body"
    _re.compile(r"^\s*(?:please\s+|can\s+you\s+(?:please\s+)?)?"
                 r"(?:message|msg|ping|notify|dm|im)\s+"
                 r"(?P<name>[A-Za-z][a-zA-Z]{1,30})\s*"
                 r"(?:[:\-—]\s*|that\s+|about\s+|saying\s+)?(?P<body>.+)",
                 _re.IGNORECASE),
]


_NOT_A_STAFF_NAME = {
    # Pronouns
    "me", "myself", "us", "you", "yourself", "him", "her", "them", "it",
    "everyone", "everybody", "anyone", "anybody", "someone", "somebody",
    "ourselves", "themselves",
    # Articles / determiners — "tell the buyer", "tell a customer", etc.
    "the", "a", "an", "my", "your", "our", "their", "this", "that",
    "these", "those", "its", "his",
    # Common non-name nouns that follow "tell"
    "buyer", "customer", "client", "user", "owner", "visitor", "caller",
    "team", "staff", "everyone", "vendor", "partner", "prospect",
}


def detect_send_intent(message: str) -> dict | None:
    """Return {recipient_name, body} if message is an internal-send intent.
    Drops pronouns as recipients — "tell me about my wife" was being
    parsed as recipient='me' and producing "I don't see a staff member
    named 'me'". Pronouns are never staff identities."""
    msg = (message or "").strip()
    if not msg or len(msg) > 1000:
        return None
    for pat in _INTENT_PATTERNS:
        m = pat.match(msg)
        if m:
            name = m.group("name").strip()
            if name.lower() in _NOT_A_STAFF_NAME:
                continue  # pronoun — not a staff recipient
            body = m.group("body").strip().rstrip(".?!")
            if len(body) < 2:
                continue
            return {"recipient_name": name, "body": body}
    return None


# ── Group send detection (multi-recipient: "the sales team", "everyone") ──
# These patterns match BEFORE the individual-recipient ones in the caller,
# because "tell the sales team..." would otherwise parse as recipient="the"
# (and "sales team..." as the body — which it did before this was added).

_GROUP_QUALIFIER = (
    # Phrases that introduce a group reference:
    r"(?:everyone|everybody|all\s+staff|all\s+(?:my\s+)?(?:team|staff)|"
    r"whole\s+team|the\s+whole\s+team|"
    r"the\s+(?P<group_named>[A-Za-z][\w&'\- ]{1,40}?)\s+"
    r"(?:team|crew|group|staff|squad|department|dept))"
)

_GROUP_INTENT_PATTERNS = [
    # "tell <group> <body>"
    _re.compile(r"^\s*(?:please\s+|can\s+you\s+(?:please\s+)?)?"
                 r"tell\s+" + _GROUP_QUALIFIER + r"\s+(?P<body>.+)",
                 _re.IGNORECASE),
    # "let <group> know <body>"
    _re.compile(r"^\s*(?:please\s+|can\s+you\s+(?:please\s+)?)?"
                 r"let\s+" + _GROUP_QUALIFIER + r"\s+know\s+"
                 r"(?:that\s+)?(?P<body>.+)",
                 _re.IGNORECASE),
    # "send <group> a message ..."
    _re.compile(r"^\s*(?:please\s+|can\s+you\s+(?:please\s+)?)?"
                 r"send\s+" + _GROUP_QUALIFIER + r"\s+(?:a\s+)?"
                 r"(?:message|note|text|dm|im|update|announcement)\s*"
                 r"(?:that\s+|about\s+|saying\s+|[:\-—]\s*)?(?P<body>.+)",
                 _re.IGNORECASE),
    # "message <group> ..." / "ping <group> ..." / "notify <group> ..."
    _re.compile(r"^\s*(?:please\s+|can\s+you\s+(?:please\s+)?)?"
                 r"(?:message|msg|ping|notify|dm|im|announce(?:\s+to)?)\s+"
                 + _GROUP_QUALIFIER + r"\s*"
                 r"(?:[:\-—]\s*|that\s+|about\s+|saying\s+)?(?P<body>.+)",
                 _re.IGNORECASE),
    # "announce to <group>" + body — handled above via "announce to"
]


def detect_group_send_intent(message: str) -> dict | None:
    """Return {group_query, body} if message is a group-send intent.

    group_query is the raw group name from the message ("sales", "kitchen",
    "everyone", "whole team", etc.) — the caller resolves it against
    existing stored groups (fuzzy match) or to the virtual __all__ Whole
    Team group. Returns None if the message isn't a group send.

    IMPORTANT: callers must try this BEFORE detect_send_intent(), because
    the individual-recipient patterns would parse "tell the sales team..."
    as recipient="the" / body="sales team ..." (which is what bit Frank's
    test: "Message the sales team — 4:30 meeting" → "I don't see a staff
    member named 'the'").
    """
    msg = (message or "").strip()
    if not msg or len(msg) > 1000:
        return None
    for pat in _GROUP_INTENT_PATTERNS:
        m = pat.match(msg)
        if not m:
            continue
        # group_named is set only when the message used the
        # "the <X> team/crew/group/..." form; everyone/all/whole-team
        # collapses to the __all__ virtual group.
        named = m.groupdict().get("group_named")
        group_query = (named or "").strip().lower() if named else "__all__"
        body = m.group("body").strip().rstrip(".?!")
        if len(body) < 2:
            continue
        return {"group_query": group_query, "body": body}
    return None


def resolve_group(data_dir: Path, group_query: str) -> dict | None:
    """Fuzzy-match a group query string to a stored group, or return the
    virtual Whole Team group if the query is __all__ / everyone / etc.
    Returns the group dict (id, name, members) or None if no match."""
    q = (group_query or "").strip().lower()
    if not q or q in {"__all__", "everyone", "everybody", "all", "all staff",
                       "whole team", "the whole team"}:
        return {"id": ALL_GROUP_ID, "name": ALL_GROUP_NAME,
                "members": [], "virtual": True}
    groups = list_groups(data_dir)
    # Exact match first
    for g in groups:
        if g.get("name", "").lower() == q:
            return g
    # Substring match — "sales" matches "Sales Team", "Top Sales"
    matches = [g for g in groups if q in g.get("name", "").lower()]
    if len(matches) == 1:
        return matches[0]
    # Reverse — try "X" matches a group whose name's first word equals X
    for g in groups:
        first_word = (g.get("name", "").split() or [""])[0].lower()
        if first_word == q:
            return g
    return None
