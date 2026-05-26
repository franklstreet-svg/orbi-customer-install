#!/usr/bin/env python3
"""
Orbi Watchdog — keeps Orbi alive on the customer's box.

Runs as its OWN service (separate from orbi.py) so it can restart
Orbi when orbi.py crashes or hangs. The OS service manager
(systemd/launchctl/Windows SC) auto-starts BOTH on boot — if either
one dies, the other can still recover.

Loop (default 30s poll):
  1. HTTP GET http://127.0.0.1:<port>/health
     ✓ 200 in <5s → reset failure counter, continue
     ✗ timeout/non-200 → increment counter
  2. After 3 consecutive failures:
     - Take a snapshot of Orby/data/ → Orby/snapshots/local/<timestamp>/
       (so we can roll back if the restart makes things worse)
     - Restart Orbi via the service manager
     - Reset failure counter, increment restart counter
  3. After 3 consecutive RESTART failures (Orbi keeps crashing):
     - Pick the most recent good snapshot from Orby/snapshots/local/
     - Replace Orby/data/ with the snapshot's contents
     - Restart Orbi one more time
     - Log the rollback event
  4. After a failed rollback:
     - Notify the owner via every channel we have (PWA push + email + SMS)
     - Continue trying — eventually internet/whatever comes back

Also:
  - Daily snapshots at 3am local (separate from the failure snapshots)
  - Snapshots prune to 7 most recent automatically
  - Every state change written to Orby/data/watchdog.log

Standalone — does NOT import orbi.py. Reads config.json from ORBI_DIR.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("orbi.watchdog")

# Defaults — can be overridden via config.json under "watchdog"
DEFAULT_POLL_SECONDS         = 30
DEFAULT_HEALTH_TIMEOUT       = 5
DEFAULT_PING_FAILS_BEFORE_RESTART  = 3
DEFAULT_RESTART_FAILS_BEFORE_ROLLBACK = 3
DEFAULT_DAILY_SNAPSHOT_HOUR  = 3        # 3am local
DEFAULT_SNAPSHOT_RETAIN      = 7        # keep last 7 snapshots


# ── Top-level entry ──────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """The watchdog process. Reads ORBI_DIR from env, loads config,
    runs the watch loop forever."""
    argv = argv or sys.argv[1:]
    orbi_dir = _resolve_orbi_dir()
    config = _load_config(orbi_dir)
    data_dir = orbi_dir / "data"

    _setup_logging(data_dir)
    log.info("Orbi watchdog starting — orbi_dir=%s", orbi_dir)

    wd_cfg = (config.get("watchdog") or {})
    server_cfg = (config.get("server") or {})
    poll = int(wd_cfg.get("poll_seconds", DEFAULT_POLL_SECONDS))
    timeout = int(wd_cfg.get("health_timeout", DEFAULT_HEALTH_TIMEOUT))
    fails_before_restart = int(wd_cfg.get("fails_before_restart", DEFAULT_PING_FAILS_BEFORE_RESTART))
    restart_fails_before_rollback = int(wd_cfg.get("restart_fails_before_rollback", DEFAULT_RESTART_FAILS_BEFORE_ROLLBACK))
    snapshot_retain = int(wd_cfg.get("snapshot_retain", DEFAULT_SNAPSHOT_RETAIN))
    daily_hour = int(wd_cfg.get("daily_snapshot_hour", DEFAULT_DAILY_SNAPSHOT_HOUR))

    host = server_cfg.get("host", "127.0.0.1")
    port = int(server_cfg.get("port", 5050))
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"  # always ping locally
    health_url = f"http://{host}:{port}/health"

    log.info("watchdog config: poll=%ss timeout=%ss restart_after=%s rollback_after=%s",
             poll, timeout, fails_before_restart, restart_fails_before_rollback)

    consecutive_ping_fails = 0
    consecutive_restart_fails = 0
    last_daily_snapshot_day = None

    while True:
        try:
            # 1. Daily snapshot check (separate from failure snapshots)
            now = datetime.now()
            if now.hour >= daily_hour and now.date() != last_daily_snapshot_day:
                try:
                    take_snapshot(orbi_dir, label="daily")
                    prune_snapshots(orbi_dir, retain=snapshot_retain)
                    last_daily_snapshot_day = now.date()
                except Exception as e:
                    log.warning("daily snapshot failed: %s", e)

            # 2. Ping
            healthy = check_health(health_url, timeout=timeout)
            if healthy:
                if consecutive_ping_fails:
                    log.info("orbi recovered after %d failed pings", consecutive_ping_fails)
                consecutive_ping_fails = 0
                consecutive_restart_fails = 0
            else:
                consecutive_ping_fails += 1
                log.warning("health check failed (%d/%d) at %s",
                            consecutive_ping_fails, fails_before_restart, health_url)

                if consecutive_ping_fails >= fails_before_restart:
                    # 3a. Take a safety snapshot before we restart
                    try:
                        take_snapshot(orbi_dir, label="pre-restart")
                        prune_snapshots(orbi_dir, retain=snapshot_retain)
                    except Exception as e:
                        log.warning("pre-restart snapshot failed: %s", e)

                    # 3b. Restart
                    restart_ok = restart_orbi_service(config)
                    consecutive_ping_fails = 0
                    if restart_ok:
                        log.info("orbi restart succeeded — waiting for it to come back up")
                        # Give Orbi a few seconds to bind the port
                        time.sleep(8)
                        if check_health(health_url, timeout=timeout):
                            log.info("orbi is healthy again post-restart")
                            consecutive_restart_fails = 0
                        else:
                            consecutive_restart_fails += 1
                    else:
                        consecutive_restart_fails += 1
                        log.error("orbi restart attempt failed")

                    # 3c. Rollback if restarts keep failing
                    if consecutive_restart_fails >= restart_fails_before_rollback:
                        log.error("orbi has failed %d consecutive restarts — rolling back",
                                  consecutive_restart_fails)
                        if rollback_to_last_snapshot(orbi_dir):
                            log.info("rollback succeeded — restarting orbi from prior snapshot")
                            restart_orbi_service(config)
                            consecutive_restart_fails = 0
                        else:
                            log.error("ROLLBACK FAILED — notifying owner")
                            notify_owner_failure(config, data_dir,
                                                 reason="rollback failed after %d failed restarts" % consecutive_restart_fails)
                            # Don't loop-spam the owner — back off 5 minutes
                            time.sleep(300)
                            consecutive_restart_fails = 0
        except KeyboardInterrupt:
            log.info("watchdog stopped (KeyboardInterrupt)")
            return 0
        except Exception as e:
            log.warning("watchdog loop error: %s", e)

        time.sleep(poll)


# ── Health check ─────────────────────────────────────────────────────────


def check_health(url: str, timeout: int = 5) -> bool:
    """Return True if /health returns 200 with a JSON body containing
    status=ok. False on timeout / connection error / non-200."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return False
            body = r.read(2048)
            try:
                data = json.loads(body)
                return data.get("status") == "ok"
            except json.JSONDecodeError:
                return False
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
        return False
    except Exception as e:
        log.warning("check_health unexpected error: %s", e)
        return False


# ── Snapshots ────────────────────────────────────────────────────────────


def _snapshots_dir(orbi_dir: Path) -> Path:
    return orbi_dir / "snapshots" / "local"


def take_snapshot(orbi_dir: Path, label: str = "manual") -> Path:
    """Copy orbi_dir/data/ into orbi_dir/snapshots/local/<YYYYMMDD-HHMMSS-label>/
    Returns the snapshot path. Uses shutil.copytree which is atomic per-file
    but not per-directory; we accept that — a partial snapshot just means
    the rollback might be from one step earlier."""
    src = orbi_dir / "data"
    if not src.exists():
        raise FileNotFoundError(f"data dir does not exist: {src}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}__{_safe_label(label)}"
    dest = _snapshots_dir(orbi_dir) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Use a temp name first so half-written snapshots aren't picked up by
    # rollback if we get killed mid-copy.
    tmp = dest.with_suffix(".incomplete")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns(
        "*.tmp", "*.lock", "__pycache__", ".history",
    ))
    tmp.rename(dest)
    log.info("snapshot taken: %s (%d bytes)", dest.name, _dir_size(dest))
    return dest


def _list_snapshots_sorted(orbi_dir: Path) -> list[Path]:
    """All snapshots, newest first. Sorts by FILENAME (which is
    timestamp-prefixed YYYYMMDD-HHMMSS__label) rather than mtime,
    because shutil.copytree preserves the source's mtime and would
    make all snapshots look the same age."""
    sdir = _snapshots_dir(orbi_dir)
    if not sdir.exists():
        return []
    snaps = [p for p in sdir.iterdir()
             if p.is_dir() and not p.name.endswith(".incomplete")]
    snaps.sort(key=lambda p: p.name, reverse=True)
    return snaps


def prune_snapshots(orbi_dir: Path, retain: int = 7) -> None:
    """Keep only the `retain` most recent snapshots."""
    snaps = _list_snapshots_sorted(orbi_dir)
    for old in snaps[retain:]:
        try:
            shutil.rmtree(old)
            log.info("pruned old snapshot: %s", old.name)
        except Exception as e:
            log.warning("could not prune %s: %s", old.name, e)


def latest_snapshot(orbi_dir: Path) -> Path | None:
    snaps = _list_snapshots_sorted(orbi_dir)
    return snaps[0] if snaps else None


def rollback_to_last_snapshot(orbi_dir: Path) -> bool:
    """Replace orbi_dir/data/ with the contents of the most recent snapshot.
    Saves the current (broken) data as data.failed-<timestamp> for
    forensics in case the rollback's source was also bad. Returns True
    on success."""
    src_snap = latest_snapshot(orbi_dir)
    if not src_snap:
        log.error("rollback: no snapshot available to roll back to")
        return False
    data_dir = orbi_dir / "data"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    failed_aside = orbi_dir / f"data.failed-{stamp}"

    try:
        if data_dir.exists():
            shutil.move(str(data_dir), str(failed_aside))
        shutil.copytree(src_snap, data_dir, ignore=shutil.ignore_patterns(
            "*.tmp", "*.lock", "__pycache__",
        ))
        log.info("rollback complete: restored from %s (broken data set aside at %s)",
                 src_snap.name, failed_aside.name)
        return True
    except Exception as e:
        log.error("rollback failed: %s", e)
        # Try to undo — put the broken data back rather than leaving
        # the customer with no data at all
        if failed_aside.exists() and not data_dir.exists():
            try:
                shutil.move(str(failed_aside), str(data_dir))
            except Exception as e2:
                log.error("could not restore broken data either: %s", e2)
        return False


# ── Restart Orbi via the OS service manager ──────────────────────────────


def restart_orbi_service(config: dict) -> bool:
    """Use the per-OS service manager to restart the 'orbi' service.
    Returns True on success. Safe-fails (returns False, logs warning)
    on any error including unsupported OS."""
    try:
        # Lazy import so the watchdog can still run if service_manager
        # has a problem (it just won't be able to restart Orbi)
        from service_manager import get_service_manager
    except ImportError as e:
        log.warning("service_manager not importable: %s", e)
        return False
    try:
        mgr = get_service_manager()
        service_name = (config.get("service_name") or "orbi")
        return bool(mgr.restart(service_name))
    except Exception as e:
        log.warning("restart_orbi_service failed: %s", e)
        return False


# ── Owner notifications (best-effort) ────────────────────────────────────


def notify_owner_failure(config: dict, data_dir: Path, reason: str) -> None:
    """Send a high-urgency notification through every available channel.
    Best-effort — if notifications.py isn't importable or any channel
    fails, log and continue. We do NOT want the watchdog itself to
    crash when the owner-notification path is broken."""
    try:
        import notifications as notify
    except ImportError as e:
        log.warning("notifications.py not importable: %s", e)
        return
    try:
        notify.send(
            config, data_dir,
            event="watchdog_failed",
            title="🚨 Orbi watchdog: manual help needed",
            body=("The watchdog tried to restart Orbi and roll back to a "
                  "snapshot, but both failed. Reason: " + reason),
            url="/owner",
        )
    except Exception as e:
        log.warning("notify_owner_failure failed: %s", e)


# ── Plumbing ─────────────────────────────────────────────────────────────


def _resolve_orbi_dir() -> Path:
    """ORBI_DIR env var, else /opt/orbi, else ~/Orby."""
    env = os.environ.get("ORBI_DIR")
    if env:
        return Path(env)
    if Path("/opt/orbi").exists():
        return Path("/opt/orbi")
    return Path.home() / "Orby"


def _load_config(orbi_dir: Path) -> dict:
    p = orbi_dir / "config.json"
    if not p.exists():
        log.warning("config.json not found at %s — using defaults", p)
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("config.json is malformed: %s", e)
        return {}


def _setup_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "watchdog.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_path)),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _safe_label(label: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (label or "manual"))[:32]


def _dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            total += f.stat().st_size
        except (FileNotFoundError, PermissionError):
            pass
    return total


if __name__ == "__main__":
    sys.exit(main())
