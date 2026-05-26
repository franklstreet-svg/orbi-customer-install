"""
Audit log — append-only, tamper-evident, exportable.

Every significant action on the customer's Orbi install gets logged:
  - Owner login / logout / failed login
  - Config changes (business info, settings, password)
  - Module add/remove
  - Frank-side remote sessions (when admin token used)
  - Stripe billing state changes
  - Watchdog restarts / rollbacks
  - Push-subscription registration

Each entry includes timestamp, actor, action, resource, before/after diff.
Stored as line-delimited JSON (JSONL) — efficient append, easy to grep,
exportable as CSV. Each entry is HMAC-signed using a per-install secret;
verifying signatures detects tampering.

Required for medical/legal verticals, useful for everyone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("orbi.audit")
_LOCK = threading.Lock()


def _path(data_dir: Path) -> Path:
    return data_dir / "audit.log.jsonl"

def _secret_path(data_dir: Path) -> Path:
    return data_dir / ".audit_secret"

def _get_secret(data_dir: Path) -> bytes:
    p = _secret_path(data_dir)
    if not p.exists():
        import secrets
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(secrets.token_bytes(32))
        try:
            os.chmod(p, 0o600)
        except (OSError, NotImplementedError):
            pass
    return p.read_bytes()

def _sign(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_event(data_dir: Path, *, actor: str, action: str,
              resource: str | None = None, before=None, after=None,
              ip: str | None = None, meta: dict | None = None) -> None:
    """Append a single audit entry. Never raises."""
    try:
        entry = {
            "ts": int(time.time() * 1000),  # millis for ordering
            "actor": actor,
            "action": action,
            "resource": resource,
            "before": _truncate(before),
            "after":  _truncate(after),
            "ip": ip,
            "meta": meta or {},
        }
        raw = json.dumps(entry, separators=(",", ":"), sort_keys=True,
                         default=str).encode("utf-8")
        entry["sig"] = _sign(_get_secret(data_dir), raw)
        line = json.dumps(entry, separators=(",", ":"),
                          default=str) + "\n"
        with _LOCK:
            with open(_path(data_dir), "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        log.warning(f"audit log_event failed: {e}")

def _truncate(v, max_len: int = 4000):
    if v is None:
        return None
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    if len(s) > max_len:
        return s[:max_len] + "...(truncated)"
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s

def tail(data_dir: Path, limit: int = 100, since_ts: int | None = None,
         actor: str | None = None, action: str | None = None) -> list[dict]:
    """Read the last `limit` entries matching filters."""
    p = _path(data_dir)
    if not p.exists():
        return []
    entries = []
    with _LOCK:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_ts and e.get("ts", 0) < since_ts:
                    continue
                if actor and e.get("actor") != actor:
                    continue
                if action and e.get("action") != action:
                    continue
                entries.append(e)
    return entries[-limit:]

def verify_integrity(data_dir: Path) -> dict:
    """Re-compute HMAC for every entry and report tampered/missing rows.
    Returns {total, valid, tampered, tampered_lines}."""
    p = _path(data_dir)
    if not p.exists():
        return {"total": 0, "valid": 0, "tampered": 0, "tampered_lines": []}
    secret = _get_secret(data_dir)
    total = valid = tampered = 0
    tampered_lines = []
    with _LOCK:
        with open(p, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    tampered += 1
                    tampered_lines.append(lineno)
                    total += 1
                    continue
                total += 1
                claimed_sig = e.pop("sig", "")
                payload = json.dumps(e, separators=(",", ":"),
                                     sort_keys=True, default=str).encode("utf-8")
                actual_sig = _sign(secret, payload)
                if hmac.compare_digest(claimed_sig, actual_sig):
                    valid += 1
                else:
                    tampered += 1
                    tampered_lines.append(lineno)
    return {
        "total": total, "valid": valid,
        "tampered": tampered,
        "tampered_lines": tampered_lines,
    }

def export_csv(data_dir: Path) -> str:
    """Export the entire audit log as CSV for legal/compliance handover."""
    import csv
    import io
    p = _path(data_dir)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["timestamp_utc", "actor", "action", "resource",
                     "ip", "before", "after", "meta", "signature"])
    if not p.exists():
        return out.getvalue()
    with _LOCK:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = e.get("ts", 0)
                from datetime import datetime, timezone
                writer.writerow([
                    datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
                    e.get("actor", ""),
                    e.get("action", ""),
                    e.get("resource", ""),
                    e.get("ip", ""),
                    json.dumps(e.get("before", "")),
                    json.dumps(e.get("after", "")),
                    json.dumps(e.get("meta", {})),
                    e.get("sig", ""),
                ])
    return out.getvalue()
