"""
users — multi-user registry + per-user data folders + archive lifecycle.

Roles:
  owner  — full access, manages staff, sees archive, can transfer items
  staff  — scoped to own personal-assistant data + shared business data
  (visitor is not in users.json — they're unauthenticated public chat)

Storage:
  data/users.json                   — registry (username → meta + pw_hash)
  data/users/<username>/            — per-user folder (calendar.json, etc.)
  data/_archived/<username>/        — deactivated user's data
  data/_archived/<username>/_meta.json — when archived, purge date, holds

On deactivation we DO NOT delete. We move to _archived/ and start a 90-day
purge timer. Owner can transfer items out before purge, or place a hold.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auth import hash_password, verify_password

log = logging.getLogger("orbi.users")

USERS_FILE = "users.json"
ARCHIVE_DIR_NAME = "_archived"
USERS_DIR_NAME = "users"
DEFAULT_PURGE_DAYS = 90

_LOCK = threading.Lock()


# ── Public registry API ─────────────────────────────────────────────────


def load_users(data_dir: Path) -> dict:
    """Return the full users.json contents as a dict {username: record}."""
    path = data_dir / USERS_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"users.json read failed: {e}")
        return {}


def save_users(data_dir: Path, users: dict) -> None:
    """Atomic write of the registry."""
    path = data_dir / USERS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def get_user(data_dir: Path, username: str) -> dict | None:
    """Lookup one user by username (case-insensitive). Returns None if missing
    or archived (archived users can't be looked up through normal channels)."""
    if not username:
        return None
    users = load_users(data_dir)
    key = username.lower()
    rec = users.get(key)
    if rec and rec.get("status") != "active":
        return None
    return rec


def list_users(data_dir: Path, include_archived: bool = False) -> list[dict]:
    """Return all users as a list, sorted by username. Each dict has
    username/role/status/display_name/created_at — no pw_hash."""
    users = load_users(data_dir)
    out = []
    for username, rec in sorted(users.items()):
        if not include_archived and rec.get("status") != "active":
            continue
        out.append({
            "username":     username,
            "role":         rec.get("role", "staff"),
            "status":       rec.get("status", "active"),
            "display_name": rec.get("display_name", username),
            "created_at":   rec.get("created_at", ""),
            "archived_at":  rec.get("archived_at", ""),
        })
    return out


def add_user(data_dir: Path, username: str, password: str,
             role: str = "staff", display_name: str | None = None,
             email: str | None = None) -> dict:
    """Create a new user. Username is lowercased. Role must be 'owner' or 'staff'.
    Raises ValueError on duplicate or bad input. Returns the new record (without pw_hash)."""
    if not username or not username.strip():
        raise ValueError("username required")
    if not password or len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    if role not in ("owner", "staff"):
        raise ValueError(f"role must be owner or staff, got {role!r}")

    key = username.strip().lower()
    with _LOCK:
        users = load_users(data_dir)
        if key in users:
            raise ValueError(f"user {key!r} already exists")
        rec = {
            "username":     key,
            "role":         role,
            "status":       "active",
            "display_name": display_name or username.strip(),
            "email":        (email or "").strip().lower(),
            "pw_hash":      hash_password(password),
            "created_at":   _now_iso(),
        }
        users[key] = rec
        save_users(data_dir, users)
        # Pre-create the user's data folder so modules can write to it immediately
        (get_user_dir(data_dir, key)).mkdir(parents=True, exist_ok=True)
        log.info(f"user created: {key} (role={role})")
        return {k: v for k, v in rec.items() if k != "pw_hash"}


def verify_user(data_dir: Path, username: str, password: str) -> dict | None:
    """Check username + password. Returns user dict (no pw_hash) on success,
    None on failure. Archived users always fail (status check)."""
    if not username or not password:
        return None
    key = username.strip().lower()
    users = load_users(data_dir)
    rec = users.get(key)
    if not rec or rec.get("status") != "active":
        return None
    if not verify_password(password, rec.get("pw_hash", "")):
        return None
    return {k: v for k, v in rec.items() if k != "pw_hash"}


def change_password(data_dir: Path, username: str, new_password: str) -> bool:
    """Update a user's password. Returns True if updated, False if user missing."""
    if not new_password or len(new_password) < 6:
        raise ValueError("password must be at least 6 characters")
    key = username.strip().lower()
    with _LOCK:
        users = load_users(data_dir)
        if key not in users:
            return False
        users[key]["pw_hash"] = hash_password(new_password)
        users[key]["password_changed_at"] = _now_iso()
        save_users(data_dir, users)
        return True


# ── Per-user folder helpers ─────────────────────────────────────────────


def get_user_dir(data_dir: Path, username: str) -> Path:
    """Return the path to a user's per-user folder (creates parents lazily on write)."""
    return data_dir / USERS_DIR_NAME / username.strip().lower()


def get_archive_dir(data_dir: Path, username: str) -> Path:
    """Return the path to an archived user's folder."""
    return data_dir / ARCHIVE_DIR_NAME / username.strip().lower()


# ── Archive lifecycle ───────────────────────────────────────────────────


def deactivate_user(data_dir: Path, username: str,
                    purge_days: int = DEFAULT_PURGE_DAYS) -> dict:
    """Deactivate a user:
      1. Move data/users/<username>/ to data/_archived/<username>/
      2. Flip status=archived in users.json (record stays for audit)
      3. Write _meta.json with archived_at + purge_after dates
    Returns the meta dict. Raises ValueError if user missing or already archived.
    Cannot deactivate the last active owner (safety guard)."""
    key = username.strip().lower()
    with _LOCK:
        users = load_users(data_dir)
        if key not in users:
            raise ValueError(f"user {key!r} not found")
        rec = users[key]
        if rec.get("status") != "active":
            raise ValueError(f"user {key!r} is already {rec.get('status')}")

        if rec.get("role") == "owner":
            other_active_owners = [
                u for u, r in users.items()
                if u != key and r.get("role") == "owner" and r.get("status") == "active"
            ]
            if not other_active_owners:
                raise ValueError("cannot deactivate the only active owner")

        user_dir = get_user_dir(data_dir, key)
        archive_dir = get_archive_dir(data_dir, key)
        archive_dir.parent.mkdir(parents=True, exist_ok=True)

        if user_dir.exists():
            if archive_dir.exists():
                # Shouldn't happen, but if it does merge by suffixing timestamp
                archive_dir = archive_dir.with_name(archive_dir.name + "_" + _now_compact())
            shutil.move(str(user_dir), str(archive_dir))
        else:
            archive_dir.mkdir(parents=True, exist_ok=True)

        archived_at = datetime.now(timezone.utc)
        purge_after = archived_at + timedelta(days=purge_days)
        meta = {
            "username":     key,
            "archived_at":  archived_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "purge_after":  purge_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "purge_days":   purge_days,
            "hold":         False,
            "role_at_archive": rec.get("role"),
        }
        (archive_dir / "_meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        rec["status"]      = "archived"
        rec["archived_at"] = meta["archived_at"]
        users[key] = rec
        save_users(data_dir, users)
        log.info(f"user archived: {key} (purge after {meta['purge_after']})")
        return meta


def reactivate_user(data_dir: Path, username: str) -> dict:
    """Reverse of deactivate_user:
      1. Move data/_archived/<username>/ back to data/users/<username>/
      2. Flip status=active in users.json
      3. Drop the _meta.json + purge fields
    Returns the restored user record. Raises ValueError if no archive
    exists OR an active user folder is already in the way.
    """
    key = username.strip().lower()
    with _LOCK:
        users = load_users(data_dir)
        rec = users.get(key)
        if not rec:
            raise ValueError(f"user {key!r} not found")
        if rec.get("status") == "active":
            raise ValueError(f"user {key!r} is already active")

        user_dir = get_user_dir(data_dir, key)
        archive_dir = get_archive_dir(data_dir, key)
        if not archive_dir.exists():
            raise ValueError(f"no archive folder for {key!r}")
        if user_dir.exists():
            raise ValueError(
                f"user folder for {key!r} already exists — "
                "won't overwrite. Move or delete it first.")

        shutil.move(str(archive_dir), str(user_dir))
        meta_path = user_dir / "_meta.json"
        if meta_path.exists():
            try:
                meta_path.unlink()
            except OSError:
                pass

        rec["status"] = "active"
        rec.pop("archived_at", None)
        rec.pop("purge_hold", None)
        users[key] = rec
        save_users(data_dir, users)
        log.info(f"user reactivated: {key}")
        return rec


def list_archived(data_dir: Path) -> list[dict]:
    """Owner-dashboard helper. Returns archived users with their meta and a
    folder-summary count of items per source."""
    archive_root = data_dir / ARCHIVE_DIR_NAME
    if not archive_root.exists():
        return []
    out = []
    for entry in sorted(archive_root.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / "_meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        meta["username"] = meta.get("username", entry.name)
        meta["folder"]   = str(entry)
        meta["summary"]  = _summarize_user_folder(entry)
        out.append(meta)
    return out


def set_purge_hold(data_dir: Path, username: str, hold: bool) -> bool:
    """Owner can place a hold on auto-purge (e.g. legal retention)."""
    archive_dir = get_archive_dir(data_dir, username)
    meta_path = archive_dir / "_meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    meta["hold"] = bool(hold)
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def purge_expired_archives(data_dir: Path) -> list[str]:
    """Background sweeper. Deletes archived user folders whose purge_after
    has passed and have hold=False. Returns list of purged usernames."""
    archived = list_archived(data_dir)
    now = datetime.now(timezone.utc)
    purged = []
    for entry in archived:
        if entry.get("hold"):
            continue
        purge_after_str = entry.get("purge_after", "")
        if not purge_after_str:
            continue
        try:
            purge_after = datetime.strptime(purge_after_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now >= purge_after:
            try:
                shutil.rmtree(entry["folder"])
                purged.append(entry["username"])
                log.info(f"archive purged: {entry['username']}")
            except Exception as e:
                log.warning(f"purge failed for {entry['username']}: {e}")
    return purged


# ── Item transfer (owner reclaims valuable items from archived users) ───


def transfer_items(data_dir: Path, from_username: str, to_username: str,
                   source_file: str, item_ids: list) -> int:
    """Move named items (by id) from an archived user's source file to a live
    user's same source file. Used for contacts, notes, calendar entries etc.

    source_file is the JSON filename inside the user folder, e.g. "contacts.json".
    item_ids is a list of item id values to transfer.

    Returns the number of items actually transferred. Items are deep-copied
    then removed from the source. Atomic on both ends."""
    src_dir = get_archive_dir(data_dir, from_username)
    dst_dir = get_user_dir(data_dir, to_username)
    src_path = src_dir / source_file
    dst_path = dst_dir / source_file
    if not src_path.exists():
        return 0
    dst_dir.mkdir(parents=True, exist_ok=True)

    with _LOCK:
        try:
            src_data = json.loads(src_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0
        try:
            dst_data = json.loads(dst_path.read_text(encoding="utf-8")) if dst_path.exists() else []
        except (json.JSONDecodeError, OSError):
            dst_data = []

        if not isinstance(src_data, list) or not isinstance(dst_data, list):
            log.warning(f"transfer_items only supports list-shaped data; got src={type(src_data).__name__}")
            return 0

        wanted = set(str(i) for i in item_ids)
        moving = [x for x in src_data if str(x.get("id", "")) in wanted]
        keeping = [x for x in src_data if str(x.get("id", "")) not in wanted]

        if not moving:
            return 0

        for item in moving:
            item["_transferred_from"] = from_username
            item["_transferred_at"]   = _now_iso()

        dst_data.extend(moving)

        for path, data in ((src_path, keeping), (dst_path, dst_data)):
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)

        log.info(f"transferred {len(moving)} item(s) from {from_username}:{source_file} to {to_username}")
        return len(moving)


# ── Helpers ─────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _summarize_user_folder(folder: Path) -> dict:
    """Count items in each known JSON list-file so the owner can see what's there."""
    summary = {}
    for json_file in folder.glob("*.json"):
        if json_file.name.startswith("_"):
            continue
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                summary[json_file.stem] = len(data)
            elif isinstance(data, dict):
                summary[json_file.stem] = len(data)
        except (json.JSONDecodeError, OSError):
            summary[json_file.stem] = "?"
    return summary
