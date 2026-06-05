"""
email_inbox — unified inbox view across connected Gmail + Outlook.

The dashboard's Email tab calls this module. It:
  - Pulls recent messages from every connected email provider
  - Auto-tags each with auto_categorize (urgent / lead / etc.)
  - Applies the owner's custom keyword-flag rules
  - Caches results per-user so the dashboard isn't dog-slow on refresh
  - Surfaces per-message actions (draft reply, mark important, archive)
    via the underlying connector classes

Per-user settings live in <user_dir>/email_inbox_settings.json:
    {
      "flag_keywords":   ["urgent", "ASAP", "invoice", "lawsuit"],
      "fetch_limit":     50,
      "folders_gmail":   ["INBOX"],
      "folders_outlook": ["inbox"]
    }

Per-user cache lives in <user_dir>/email_inbox_cache.json (last fetched
batch, used to render the tab quickly while a fresh fetch streams in
background).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("orbi.email_inbox")

_LOCK = threading.Lock()
SETTINGS_FILE = "email_inbox_settings.json"
CACHE_FILE = "email_inbox_cache.json"

DEFAULT_SETTINGS = {
    "flag_keywords":   ["urgent", "asap", "invoice", "lawsuit", "refund",
                        "subpoena", "court", "deadline"],
    "fetch_limit":     50,
    "folders_gmail":   ["INBOX"],
    "folders_outlook": ["inbox"],
}


# ── Settings ────────────────────────────────────────────────────────────


def load_settings(user_dir: Path) -> dict:
    p = user_dir / SETTINGS_FILE
    if not p.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        s = json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        s = {}
    # Fill in any missing keys with defaults
    return {**DEFAULT_SETTINGS, **s}


def save_settings(user_dir: Path, settings: dict) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / SETTINGS_FILE
    with _LOCK:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)


# ── Fetch + categorize ─────────────────────────────────────────────────


def fetch_inbox(config: dict, user_dir: Path,
                source: str = "all", limit: int | None = None,
                query: str = "", force_refresh: bool = False) -> dict:
    """Pull recent messages from connected email providers.

    Args:
      source: 'all' | 'gmail' | 'outlook'
      query:  optional search string (Gmail/Outlook query syntax)
      force_refresh: if False, returns cached results when fresh (<2min)

    Returns:
      {
        "messages": [
          {
            "id":         "<provider>-<message_id>",
            "provider":   "gmail" | "outlook",
            "subject":    "...",
            "from":       "...",
            "snippet":    "...",
            "date":       "<iso>",
            "unread":     true,
            "tags":       ["lead", "urgent"],
            "flagged":    true,         # any flag_keyword matched
            "flag_reason": "matched 'lawsuit'",
          },
          ...
        ],
        "by_category": {urgent: N, lead: N, complaint: N, ...},
        "by_provider": {gmail: N, outlook: N},
        "total":       N,
        "errors":      {gmail: ".....", outlook: "...."},  # if any source failed
        "from_cache":  bool,
        "fetched_at":  "<iso>",
      }
    """
    settings = load_settings(user_dir)
    fetch_limit = limit or settings.get("fetch_limit", 50)

    # Cache check
    if not force_refresh and not query:
        cached = _read_cache(user_dir)
        if cached and (time.time() - cached.get("ts", 0)) < 120:
            cached["data"]["from_cache"] = True
            return cached["data"]

    messages = []
    errors = {}

    if source in ("all", "gmail"):
        try:
            messages.extend(_pull_gmail(config, user_dir, fetch_limit, query))
        except Exception as e:
            log.warning(f"gmail pull failed: {e}")
            errors["gmail"] = str(e)

    if source in ("all", "outlook"):
        try:
            messages.extend(_pull_outlook(config, user_dir, fetch_limit, query))
        except Exception as e:
            log.warning(f"outlook pull failed: {e}")
            errors["outlook"] = str(e)

    if source in ("all", "imap"):
        try:
            import imap_smtp
            messages.extend(imap_smtp.pull_inbox(user_dir, account_id=None,
                                                 limit=fetch_limit, query=query))
        except Exception as e:
            log.warning(f"imap pull failed: {e}")
            errors["imap"] = str(e)

    # Sort by date desc
    messages.sort(key=lambda m: m.get("date", ""), reverse=True)

    # Apply tagging + flag-keyword check
    _tag_and_flag(config, messages, settings)

    by_category, by_provider = _aggregate(messages)
    result = {
        "messages":    messages[:fetch_limit],
        "by_category": by_category,
        "by_provider": by_provider,
        "total":       len(messages),
        "errors":      errors,
        "from_cache":  False,
        "fetched_at":  _now_iso(),
    }
    if not query:
        _write_cache(user_dir, result)
    return result


def _pull_gmail(config: dict, user_dir: Path, limit: int, query: str) -> list[dict]:
    from connectors.base import get_instance
    inst = get_instance("gmail", config, user_dir)
    if inst is None or not inst.is_connected():
        return []
    if query:
        raw = inst.search(query, limit=limit)
    else:
        raw = inst.list_recent(limit=limit)
    out = []
    for m in raw:
        out.append({
            "id":       "gmail-" + str(m.get("id", "")),
            "provider": "gmail",
            "raw_id":   m.get("id", ""),
            "subject":  m.get("subject", ""),
            "from":     m.get("from", ""),
            "snippet":  m.get("snippet", ""),
            "date":     m.get("date", ""),
            "unread":   bool(m.get("unread")),
        })
    return out


def _pull_outlook(config: dict, user_dir: Path, limit: int, query: str) -> list[dict]:
    from connectors.base import get_instance
    inst = get_instance("outlook", config, user_dir)
    if inst is None or not inst.is_connected():
        return []
    if query:
        raw = inst.search(query, limit=limit)
    else:
        raw = inst.list_recent(limit=limit)
    out = []
    for m in raw:
        out.append({
            "id":       "outlook-" + str(m.get("id", "")),
            "provider": "outlook",
            "raw_id":   m.get("id", ""),
            "subject":  m.get("subject", ""),
            "from":     m.get("from", ""),
            "snippet":  m.get("snippet", ""),
            "date":     m.get("date", ""),
            "unread":   bool(m.get("unread")),
        })
    return out


def _tag_and_flag(config: dict, messages: list[dict], settings: dict) -> None:
    """Apply auto-categorizer tags AND flag-keyword match in-place."""
    keywords = [k.strip().lower() for k in settings.get("flag_keywords", []) if k]
    for m in messages:
        text = f"{m.get('subject','')} {m.get('snippet','')}".strip()
        try:
            import auto_categorize
            m["tags"] = auto_categorize.categorize(config, text, hints={
                "from_name": m.get("from"),
                "type":      "email",
            })
        except Exception:
            m["tags"] = ["other"]
        # Keyword flag
        text_lower = text.lower()
        hit = next((k for k in keywords if k and k in text_lower), None)
        if hit:
            m["flagged"] = True
            m["flag_reason"] = f"matched '{hit}'"
        else:
            m["flagged"] = False
            m["flag_reason"] = ""


def _aggregate(messages: list[dict]) -> tuple[dict, dict]:
    by_cat = {}
    by_prov = {}
    for m in messages:
        for t in m.get("tags") or []:
            by_cat[t] = by_cat.get(t, 0) + 1
        p = m.get("provider", "?")
        by_prov[p] = by_prov.get(p, 0) + 1
    return by_cat, by_prov


# ── Per-message actions (delegate to the connector classes) ─────────────


def get_message(config: dict, user_dir: Path, message_id: str) -> dict:
    """Fetch the full body of a specific message."""
    provider, raw_id = _split_id(message_id)
    # IMAP doesn't go through connectors.base — it lives in imap_smtp.
    # raw_id format: '<account_id>-<uid>' where account_id may itself
    # contain hyphens, so split on the LAST hyphen.
    if provider == "imap":
        import imap_smtp
        if "-" in raw_id:
            account_id, _, uid = raw_id.rpartition("-")
        else:
            account_id, uid = "", raw_id
        return imap_smtp.fetch_one_body(user_dir, account_id, uid)
    from connectors.base import get_instance
    inst = get_instance(provider, config, user_dir)
    if inst is None or not inst.is_connected():
        return {"error": f"{provider} not connected"}
    try:
        return inst.get_message(raw_id)
    except Exception as e:
        return {"error": str(e)}


def draft_reply(config: dict, user_dir: Path, message_id: str,
                reply_text: str) -> dict:
    """Save a draft reply to the provider's Drafts folder. Returns
    {draft_id, ok: True} or {error}."""
    provider, raw_id = _split_id(message_id)
    from connectors.base import get_instance
    inst = get_instance(provider, config, user_dir)
    if inst is None or not inst.is_connected():
        return {"error": f"{provider} not connected"}
    try:
        return inst.draft_reply(raw_id, reply_text)
    except Exception as e:
        return {"error": str(e)}


def mark_read(config: dict, user_dir: Path, message_id: str) -> dict:
    """Mark a message as read in the provider. Gmail and Outlook both
    support label manipulation; we'll try each provider's idiomatic API."""
    provider, raw_id = _split_id(message_id)
    from connectors.base import get_instance
    inst = get_instance(provider, config, user_dir)
    if inst is None or not inst.is_connected():
        return {"error": f"{provider} not connected"}
    try:
        if provider == "gmail":
            # Remove UNREAD label
            svc = inst._get_service()
            svc.users().messages().modify(
                userId="me", id=raw_id,
                body={"removeLabelIds": ["UNREAD"]}
            ).execute()
            return {"ok": True}
        elif provider == "outlook":
            # PATCH isRead=true
            inst._graph("PATCH", f"/me/messages/{raw_id}", json={"isRead": True})
            return {"ok": True}
    except Exception as e:
        return {"error": str(e)}
    return {"error": "unsupported provider"}


def archive_message(config: dict, user_dir: Path, message_id: str) -> dict:
    """Move out of inbox. Gmail: remove INBOX label. Outlook: move to Archive."""
    provider, raw_id = _split_id(message_id)
    from connectors.base import get_instance
    inst = get_instance(provider, config, user_dir)
    if inst is None or not inst.is_connected():
        return {"error": f"{provider} not connected"}
    try:
        if provider == "gmail":
            svc = inst._get_service()
            svc.users().messages().modify(
                userId="me", id=raw_id,
                body={"removeLabelIds": ["INBOX"]}
            ).execute()
            return {"ok": True}
        elif provider == "outlook":
            # Find the Archive folder and move
            inst._graph("POST", f"/me/messages/{raw_id}/move",
                        json={"destinationId": "archive"})
            return {"ok": True}
    except Exception as e:
        return {"error": str(e)}
    return {"error": "unsupported provider"}


def flag_message(config: dict, user_dir: Path, message_id: str,
                 flagged: bool = True) -> dict:
    """Star/flag a message visually in the provider for owner's own view."""
    provider, raw_id = _split_id(message_id)
    from connectors.base import get_instance
    inst = get_instance(provider, config, user_dir)
    if inst is None or not inst.is_connected():
        return {"error": f"{provider} not connected"}
    try:
        if provider == "gmail":
            svc = inst._get_service()
            label_change = {"addLabelIds": ["STARRED"]} if flagged else {"removeLabelIds": ["STARRED"]}
            svc.users().messages().modify(userId="me", id=raw_id, body=label_change).execute()
            return {"ok": True, "flagged": flagged}
        elif provider == "outlook":
            inst._graph("PATCH", f"/me/messages/{raw_id}", json={
                "flag": {"flagStatus": "flagged" if flagged else "notFlagged"}
            })
            return {"ok": True, "flagged": flagged}
    except Exception as e:
        return {"error": str(e)}
    return {"error": "unsupported provider"}


# ── Internal helpers ───────────────────────────────────────────────────


def _split_id(message_id: str) -> tuple[str, str]:
    """email_inbox IDs are '<provider>-<raw_id>'."""
    if "-" in message_id:
        provider, raw = message_id.split("-", 1)
        return provider.lower(), raw
    return "", message_id


def _read_cache(user_dir: Path) -> dict | None:
    p = user_dir / CACHE_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(user_dir: Path, data: dict) -> None:
    with _LOCK:
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
            payload = {"ts": time.time(), "data": data}
            tmp = (user_dir / CACHE_FILE).with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(user_dir / CACHE_FILE)
        except OSError as e:
            log.warning(f"cache write failed: {e}")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
