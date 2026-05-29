"""
updater — daily check for new Orbi releases on GitHub.

Strategy: NOTIFY THEN ASK, never auto-install.

  1. Background thread polls GitHub Releases API every 24h.
  2. Compares latest release tag to local config.version (semver-ish).
  3. If a newer release exists, sets a flag + sends a push notification
     to the owner: "Orbi v0.3.0 is available — say 'install update' in
     chat to apply it."
  4. Owner chat command (wired in orbi.py) downloads the appropriate
     platform asset to a staging directory and writes an `apply.sh` shim
     that systemd's `orbi-update.service` (created by install.sh) can run
     as root to do the actual file swap + restart.

Why notify-then-ask instead of fully automatic:
  - Customer-facing AI silently updating itself overnight is alarming
    if anything goes wrong.
  - Owners on metered connections might not want a 100MB download.
  - Some updates have breaking changes (e.g. a new schema field) — owner
    should know.

For dev / git-checkout installs (Frank's machine), the updater detects
the .git directory and falls back to `git pull` instead of downloading
the binary. Same chat command, different mechanism.

State:
  ~/Orbi/.updater_state.json
    { "last_check": ts, "available": "v0.3.0" or null, "channel": "stable" }
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("orbi.updater")

# Where customer installs pull releases from. Public repo so no auth needed.
GITHUB_REPO = "franklstreet-svg/orbi-customer-install"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CHECK_INTERVAL_SECONDS = 24 * 3600
STATE_FILENAME = ".updater_state.json"


def _state_path(data_dir: Path) -> Path:
    return data_dir / STATE_FILENAME


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


def _norm_version(v: str) -> tuple[int, ...]:
    """Best-effort semver tuple. Returns (0,) on garbage so unparseable
    'available' never beats a parseable 'current'."""
    s = (v or "").lstrip("vV")
    parts = []
    for chunk in s.split("."):
        m = re.match(r"^(\d+)", chunk)
        if not m:
            break
        parts.append(int(m.group(1)))
    return tuple(parts) if parts else (0,)


def is_newer(latest: str, current: str) -> bool:
    return _norm_version(latest) > _norm_version(current)


def _platform_asset_name() -> str | None:
    """Pick the right release asset for this OS. Returns the filename or
    None if we don't know what to pick (e.g. ARM Mac in the future)."""
    osname = platform.system().lower()
    if osname == "linux":
        return "orbi-installer"
    if osname == "darwin":
        return "orbi-installer.pkg"
    if osname == "windows":
        return "orbi-installer.exe"
    return None


def check_for_update(config: dict, data_dir: Path,
                     timeout: int = 15) -> dict | None:
    """Returns a release-info dict if a newer release is available, else None.
    Persists state so chat handler can answer 'is there an update?' quickly."""
    current = (config or {}).get("version", "0.0.0")
    try:
        req = urllib.request.Request(RELEASES_URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "orbi-updater/1",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log.warning(f"update check failed: {e}")
        return None

    latest_tag = data.get("tag_name", "")
    if not latest_tag:
        return None

    state = _load_state(data_dir)
    state["last_check"] = int(time.time())

    if not is_newer(latest_tag, current):
        state["available"] = None
        _save_state(data_dir, state)
        return None

    # Find the right asset for this platform
    target_name = _platform_asset_name()
    asset_url = None
    asset_size = 0
    for a in data.get("assets", []):
        if a.get("name") == target_name:
            asset_url = a.get("browser_download_url")
            asset_size = a.get("size", 0)
            break

    info = {
        "tag": latest_tag,
        "name": data.get("name") or latest_tag,
        "published_at": data.get("published_at"),
        "body": (data.get("body") or "")[:600],
        "asset_url": asset_url,
        "asset_size": asset_size,
        "asset_name": target_name,
        "current_version": current,
    }
    state["available"] = info
    _save_state(data_dir, state)
    log.info(f"update available: {current} → {latest_tag} "
             f"({asset_size} bytes for {target_name})")
    return info


def get_pending_update(data_dir: Path) -> dict | None:
    """Read cached 'is there an update' answer. No network call. Used by
    the chat handler so 'is there an update?' returns instantly."""
    state = _load_state(data_dir)
    info = state.get("available")
    return info if isinstance(info, dict) else None


def is_git_checkout(install_root: Path) -> bool:
    """Detect dev install (git clone) vs production binary install."""
    return (install_root / ".git").is_dir()


def download_update(info: dict, staging_dir: Path) -> Path | None:
    """Download the release asset into staging_dir. Returns the path.
    For git-checkout installs, runs git pull instead and returns the
    install_root path as a sentinel."""
    if not info or not info.get("asset_url"):
        return None
    staging_dir.mkdir(parents=True, exist_ok=True)
    dest = staging_dir / (info.get("asset_name") or "orbi-installer")
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        urllib.request.urlretrieve(info["asset_url"], tmp)
        tmp.replace(dest)
        try:
            os.chmod(dest, 0o755)
        except (OSError, NotImplementedError):
            pass
        log.info(f"downloaded update → {dest}")
        return dest
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError) as e:
        log.warning(f"update download failed: {e}")
        try:
            tmp.unlink()
        except OSError:
            pass
        return None


def git_pull_update(install_root: Path) -> dict:
    """For dev installs — `git pull` in the install root. Returns status."""
    try:
        result = subprocess.run(
            ["git", "-C", str(install_root), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=60)
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout.strip()[:500],
            "stderr": result.stderr.strip()[:500],
        }
    except (subprocess.SubprocessError, OSError) as e:
        return {"ok": False, "error": str(e)}


def start_update_check_scheduler(config: dict, data_dir: Path,
                                  notify_callback=None) -> None:
    """Background thread that checks for updates every 24h. When a new
    release lands, calls notify_callback(info) so orbi.py can push to the
    owner via the notifications system."""
    if not (config or {}).get("updater", {}).get("enabled", True):
        log.info("updater disabled in config")
        return

    def loop():
        # First check after a 5-minute delay so startup isn't blocked
        time.sleep(300)
        last_notified_tag = None
        while True:
            try:
                info = check_for_update(config, data_dir)
                if info and info["tag"] != last_notified_tag and notify_callback:
                    try:
                        notify_callback(info)
                        last_notified_tag = info["tag"]
                    except Exception:    # noqa: BLE001
                        log.exception("update notify_callback crashed")
            except Exception:    # noqa: BLE001
                log.exception("update check crashed")
            time.sleep(CHECK_INTERVAL_SECONDS)

    t = threading.Thread(target=loop, daemon=True, name="orbi-updater")
    t.start()
    log.info("update-check scheduler started (every 24h)")
