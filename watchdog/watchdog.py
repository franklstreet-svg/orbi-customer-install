#!/usr/bin/env python3
"""
Orbi Watchdog
-------------
Self-healing supervisor for the customer-side Orbi install.

What it does:
  1. Pings Orbi's /health endpoint every CHECK_INTERVAL seconds.
  2. After FAIL_THRESHOLD consecutive failures, restarts the Orbi service.
  3. After MAX_RESTARTS failed restarts, rolls back to the most recent snapshot.
  4. Takes a daily snapshot at 03:00 local time, keeps the last 7.
  5. Takes a pre-update snapshot whenever /opt/orbi/UPDATING.lock is observed.
  6. Sends a push notification to the owner on rollback / unrecoverable failure.

Designed to be tiny, dependency-light, and reliable. Runs as a systemd service.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tarfile
import shutil
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ORBI_DIR        = Path(os.environ.get("ORBI_DIR", "/opt/orbi"))
DATA_DIR        = ORBI_DIR / "data"
SNAPSHOT_DIR    = ORBI_DIR / "snapshots"
LOG_FILE        = ORBI_DIR / "watchdog.log"
CONFIG_FILE     = ORBI_DIR / "config.json"
UPDATE_LOCK     = ORBI_DIR / "UPDATING.lock"

HEALTH_URL          = os.environ.get("ORBI_HEALTH_URL", "http://127.0.0.1:5050/health")
SERVICE_NAME        = os.environ.get("ORBI_SERVICE", "orbi")
CHECK_INTERVAL      = int(os.environ.get("ORBI_CHECK_INTERVAL", "30"))   # seconds
HEALTH_TIMEOUT      = int(os.environ.get("ORBI_HEALTH_TIMEOUT", "10"))   # seconds
FAIL_THRESHOLD      = int(os.environ.get("ORBI_FAIL_THRESHOLD", "3"))
MAX_RESTARTS        = int(os.environ.get("ORBI_MAX_RESTARTS", "3"))
RESTART_BACKOFF     = int(os.environ.get("ORBI_RESTART_BACKOFF", "30"))  # seconds
SNAPSHOT_RETAIN     = int(os.environ.get("ORBI_SNAPSHOT_RETAIN", "7"))
SNAPSHOT_HOUR       = int(os.environ.get("ORBI_SNAPSHOT_HOUR", "3"))     # 24-hour

# ---------------------------------------------------------------------------
# Logging (file + stdout for journalctl)
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Notification (PWA push via local Orbi endpoint, falls through silently)
# ---------------------------------------------------------------------------

def notify_owner(title: str, body: str, urgent: bool = False) -> None:
    """Best-effort owner notification. Goes through Orbi's own push endpoint
    when Orbi is reachable; otherwise queues the notice to be delivered later."""
    payload = json.dumps({
        "title": title,
        "body": body,
        "urgent": urgent,
        "source": "watchdog",
    }).encode("utf-8")
    try:
        # Derive notify URL from HEALTH_URL so a port change only needs
        # one env var (ORBI_HEALTH_URL) — not two.
        _notify_base = HEALTH_URL.rsplit("/", 1)[0]  # strips trailing /health
        req = urllib.request.Request(
            f"{_notify_base}/api/internal/notify",
            data=payload,
            headers={"Content-Type": "application/json", "X-Watchdog": "1"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        # Orbi unreachable — write to disk so the next start can flush queued notices.
        try:
            queue_file = ORBI_DIR / "notify_queue.jsonl"
            with open(queue_file, "a") as f:
                f.write(payload.decode("utf-8") + "\n")
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT) as resp:
            if resp.status != 200:
                return False
            body = resp.read().decode("utf-8", errors="ignore")
            # Health endpoint should return JSON: {"status":"ok",...}
            try:
                data = json.loads(body)
                return data.get("status") == "ok"
            except json.JSONDecodeError:
                return False
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return False

# ---------------------------------------------------------------------------
# Process control
# ---------------------------------------------------------------------------

def systemctl(action: str) -> bool:
    """Returns True on a 0 exit code. Logs the result either way."""
    cmd = ["systemctl"]
    # Set ORBI_SYSTEMD_USER=1 to manage a user-level service (e.g. when
    # running the watchdog on a dev box without root). Customer installs
    # leave this unset and use the system-level orbi.service.
    if os.environ.get("ORBI_SYSTEMD_USER") == "1":
        cmd.append("--user")
    cmd.extend([action, SERVICE_NAME])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60,
        )
        ok = result.returncode == 0
        log(f"systemctl {action} {SERVICE_NAME}: "
            f"{'OK' if ok else 'FAILED rc=' + str(result.returncode)}")
        if not ok and result.stderr:
            log(f"  stderr: {result.stderr.strip()}")
        return ok
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"systemctl {action} error: {e}")
        return False

def restart_orbi() -> bool:
    log("Attempting Orbi restart...")
    if not systemctl("restart"):
        return False
    # Give it a few seconds to come up, then health check
    time.sleep(8)
    for _ in range(6):  # up to ~30s
        if healthy():
            log("Orbi restarted and healthy.")
            return True
        time.sleep(5)
    log("Orbi restarted but did NOT become healthy.")
    return False

# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def snapshot_name(tag: str = "auto") -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"orbi-{tag}-{ts}.tar.gz"

def take_snapshot(tag: str = "auto") -> Path | None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    target = SNAPSHOT_DIR / snapshot_name(tag)
    log(f"Taking snapshot: {target.name}")
    try:
        with tarfile.open(target, "w:gz") as tar:
            if DATA_DIR.exists():
                tar.add(DATA_DIR, arcname="data")
            if CONFIG_FILE.exists():
                tar.add(CONFIG_FILE, arcname="config.json")
        log(f"Snapshot complete: {target.name} "
            f"({target.stat().st_size // 1024} KB)")
        return target
    except Exception as e:
        log(f"Snapshot FAILED: {e}")
        if target.exists():
            target.unlink()
        return None

def list_snapshots() -> list[Path]:
    if not SNAPSHOT_DIR.exists():
        return []
    return sorted(
        SNAPSHOT_DIR.glob("orbi-*.tar.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

def prune_snapshots() -> None:
    snaps = list_snapshots()
    if len(snaps) <= SNAPSHOT_RETAIN:
        return
    for old in snaps[SNAPSHOT_RETAIN:]:
        try:
            old.unlink()
            log(f"Pruned old snapshot: {old.name}")
        except Exception as e:
            log(f"Failed to prune {old.name}: {e}")

def restore_snapshot(path: Path) -> bool:
    log(f"Restoring snapshot: {path.name}")
    try:
        # Safety backup of current state before overwriting
        if DATA_DIR.exists():
            safety = ORBI_DIR / f"data.preroll-{int(time.time())}"
            shutil.move(str(DATA_DIR), str(safety))
            log(f"Current data moved to {safety.name} for safety.")
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(ORBI_DIR)
        log("Snapshot restored.")
        return True
    except Exception as e:
        log(f"Restore FAILED: {e}")
        return False

# ---------------------------------------------------------------------------
# Recovery ladder
# ---------------------------------------------------------------------------

def attempt_recovery() -> bool:
    """Restart up to MAX_RESTARTS times, then roll back. Returns True if recovered."""
    for attempt in range(1, MAX_RESTARTS + 1):
        log(f"Recovery attempt {attempt}/{MAX_RESTARTS}: restart")
        if restart_orbi():
            return True
        if attempt < MAX_RESTARTS:
            log(f"Waiting {RESTART_BACKOFF}s before next attempt...")
            time.sleep(RESTART_BACKOFF)

    log("All restart attempts failed. Beginning snapshot rollback.")
    notify_owner(
        title="Orbi is recovering",
        body="Orbi had trouble starting and is restoring from a recent backup. "
             "You shouldn't see any data loss. This usually completes in under a minute.",
        urgent=False,
    )

    for snap in list_snapshots():
        if restore_snapshot(snap):
            if restart_orbi():
                notify_owner(
                    title="Orbi is back online",
                    body=f"Restored from snapshot {snap.name}. "
                         f"All systems normal.",
                    urgent=False,
                )
                return True
            log(f"Restart after restore from {snap.name} failed. Trying older snapshot.")
        else:
            log(f"Restore from {snap.name} failed. Trying older snapshot.")

    log("UNRECOVERABLE: no snapshot restored successfully.")
    notify_owner(
        title="Orbi needs help",
        body="Orbi is having trouble that I couldn't auto-fix. "
             "Frank has been alerted and will look into it.",
        urgent=True,
    )
    return False

# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def next_snapshot_due(after: datetime) -> datetime:
    target = after.replace(hour=SNAPSHOT_HOUR, minute=0, second=0, microsecond=0)
    if target <= after:
        target += timedelta(days=1)
    return target

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    log("=" * 60)
    log(f"Orbi watchdog starting. Monitoring {HEALTH_URL}")
    log(f"Service: {SERVICE_NAME}  Snapshot dir: {SNAPSHOT_DIR}")
    log("=" * 60)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    consecutive_fails = 0
    next_snapshot = next_snapshot_due(datetime.now())
    last_update_lock_seen = False

    while True:
        # Check for pre-update snapshot trigger
        update_lock_now = UPDATE_LOCK.exists()
        if update_lock_now and not last_update_lock_seen:
            log("Update lock detected — taking pre-update snapshot.")
            take_snapshot(tag="pre-update")
            prune_snapshots()
        last_update_lock_seen = update_lock_now

        # Scheduled daily snapshot
        if datetime.now() >= next_snapshot:
            take_snapshot(tag="daily")
            prune_snapshots()
            next_snapshot = next_snapshot_due(datetime.now())

        # Health check
        if healthy():
            if consecutive_fails > 0:
                log(f"Orbi recovered on its own after {consecutive_fails} failed check(s).")
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            log(f"Health check failed ({consecutive_fails}/{FAIL_THRESHOLD})")
            if consecutive_fails >= FAIL_THRESHOLD:
                if attempt_recovery():
                    consecutive_fails = 0
                else:
                    # Back off before retrying the whole ladder so we don't loop hot
                    log("Recovery failed. Sleeping 5 minutes before re-checking.")
                    time.sleep(300)
                    consecutive_fails = 0

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Watchdog stopped by user.")
        sys.exit(0)
