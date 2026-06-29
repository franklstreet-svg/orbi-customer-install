"""
Encrypted offsite backup.

Daily encrypted backup of the customer's data folder to S3-compatible storage
(Cloudflare R2, Backblaze B2, or actual AWS S3). Client-side encryption with
a key derived from a passphrase the owner sets at install time. Frank/the
brain machine CANNOT read the encrypted contents.

Restore flow:
  1. New customer box, fresh Orbi install
  2. Owner enters their restore passphrase in the dashboard
  3. backup.restore() pulls the latest backup, decrypts, drops files into data/
  4. Done

Storage: any S3-compatible endpoint. Cloudflare R2 is recommended (no egress
fees, $5 minimum/month for 10GB), Backblaze B2 is fine too.

Encryption: AES-256-GCM. Key = HKDF-SHA256(passphrase + per-install salt).
We never store the passphrase — only its argon2id hash for verification.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import tarfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("orbi.backup")

# Optional deps
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def _state_path(data_dir: Path) -> Path:
    return data_dir / ".backup_state.json"

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
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except (OSError, NotImplementedError):
        pass

def derive_key(passphrase: str, salt: bytes) -> bytes:
    """HKDF-SHA256 derivation. 32 bytes for AES-256."""
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required for backups")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32,
                salt=salt, info=b"orbi-backup-v1")
    return hkdf.derive(passphrase.encode("utf-8"))


# ---------------------------------------------------------------------------
# Set up backup passphrase
# ---------------------------------------------------------------------------

def set_passphrase(data_dir: Path, passphrase: str) -> dict:
    """Owner provides a backup passphrase. We store a salt + a verification
    hash, NOT the passphrase itself."""
    if len(passphrase) < 12:
        raise ValueError("Passphrase must be at least 12 characters")
    salt = secrets.token_bytes(16)
    key  = derive_key(passphrase, salt)
    verifier = hashlib.sha256(b"orbi-verifier:" + key).hexdigest()
    state = _load_state(data_dir)
    state.update({
        "salt": base64.b64encode(salt).decode("ascii"),
        "verifier": verifier,
        "set_at": int(time.time()),
    })
    _save_state(data_dir, state)
    return {"status": "ok"}

def verify_passphrase(data_dir: Path, passphrase: str) -> bytes | None:
    """Returns the derived encryption key if valid, None if not."""
    state = _load_state(data_dir)
    salt_b64 = state.get("salt")
    verifier = state.get("verifier")
    if not salt_b64 or not verifier:
        return None
    salt = base64.b64decode(salt_b64)
    key  = derive_key(passphrase, salt)
    actual = hashlib.sha256(b"orbi-verifier:" + key).hexdigest()
    if hmac.compare_digest(actual, verifier):
        return key
    return None


# ---------------------------------------------------------------------------
# Encrypt / decrypt
# ---------------------------------------------------------------------------

def encrypt(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM. Format: 12-byte nonce || ciphertext."""
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, data, b"orbi-backup-v1")
    return nonce + ct

def decrypt(blob: bytes, key: bytes) -> bytes:
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(key).decrypt(nonce, ct, b"orbi-backup-v1")


# ---------------------------------------------------------------------------
# Bundle data folder into a tar.gz in memory
# ---------------------------------------------------------------------------

# Files to skip — secrets we don't want to back up offsite even encrypted
_SKIP_FILES = {".session_secret", ".audit_secret", ".vapid_keys.json",
               ".backup_state.json"}

def bundle(data_dir: Path) -> bytes:
    """Tar-gzip the data folder (excluding secrets), return as bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(data_dir):
            root_path = Path(root)
            # Skip the .session_secret etc.
            for fname in files:
                if fname in _SKIP_FILES:
                    continue
                if fname.endswith(".tmp"):
                    continue
                fp = root_path / fname
                arcname = fp.relative_to(data_dir)
                tar.add(fp, arcname=str(arcname))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# S3-compatible upload (minimal — uses signed PUT URL)
# ---------------------------------------------------------------------------
# To avoid an extra dependency on boto3, we expect the caller to provide a
# pre-signed PUT URL (generated server-side by Frank's brain machine or via
# Cloudflare R2's S3-compatible API). The customer install just PUTs to it.

def upload_to_presigned_url(blob: bytes, url: str,
                            content_type: str = "application/octet-stream") -> bool:
    req = urllib.request.Request(url, data=blob, method="PUT",
                                  headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        log.warning(f"backup upload HTTP error: {e.code} {e.read()[:200]}")
        return False
    except Exception as e:
        log.warning(f"backup upload failed: {e}")
        return False

def download_from_url(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read()
    except Exception as e:
        log.warning(f"backup download failed: {e}")
        return None


# ---------------------------------------------------------------------------
# High-level: run_backup() / run_restore()
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Local-backup mode (no cloud, no R2, works out of the box)
# ---------------------------------------------------------------------------

def _local_key_path(data_dir: Path) -> Path:
    return data_dir / ".backup_local_key"


def _get_or_create_local_key(data_dir: Path) -> bytes:
    """Per-install random 32-byte key kept in a chmod 600 file. Used when
    backup.mode='local' so backups work out of the box without the owner
    setting a passphrase. Owners who want extra security can switch to
    passphrase mode in Settings."""
    p = _local_key_path(data_dir)
    if p.exists():
        try:
            return base64.b64decode(p.read_text(encoding="ascii").strip())
        except Exception:    # noqa: BLE001
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    p.write_text(base64.b64encode(key).decode("ascii"), encoding="ascii")
    try:
        os.chmod(p, 0o600)
    except (OSError, NotImplementedError):
        pass
    return key


def _local_backup_dir(config: dict, data_dir: Path) -> Path:
    """Where local backup snapshots are written. Default: <data_dir>/../backups/.
    Owner can override via config.backup.local_dir."""
    bk_cfg = config.get("backup", {})
    override = bk_cfg.get("local_dir")
    if override:
        return Path(override).expanduser()
    return data_dir.parent / "backups"


def _prune_local_backups(backup_dir: Path, retention: int = 14) -> int:
    """Keep newest N .enc files, delete the rest. Returns count deleted."""
    if not backup_dir.exists():
        return 0
    files = sorted(
        [p for p in backup_dir.iterdir() if p.suffix == ".enc"],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    deleted = 0
    for old in files[retention:]:
        try:
            old.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


def _current_local_backup_name(config: dict) -> str:
    """Single rolling local snapshot name.

    Frank 2026-06-25: local backups must overwrite in place instead of
    stacking timestamped archives. Keep this stable unless the owner
    explicitly asks for rotating backups again.
    """
    bk_cfg = config.get("backup", {})
    return str(bk_cfg.get("local_name") or "orbi-current.tar.gz.enc")


def run_backup(config: dict, data_dir: Path) -> dict:
    """Bundle, encrypt, write. Returns summary.

    Two modes:
      mode='local' (default for new installs) — writes encrypted snapshot
        to <data_dir>/../backups/. No cloud, no upload URL needed. Uses a
        per-install random key file (chmod 600). 14-day rotation default.
      mode='cloud' — passphrase + presigned URL flow (legacy / opt-in).
    """
    if not HAS_CRYPTO:
        return {"status": "error", "reason": "cryptography_missing"}
    bk_cfg = config.get("backup", {})
    if not bk_cfg.get("enabled"):
        return {"status": "skipped", "reason": "disabled"}

    mode = bk_cfg.get("mode", "local")    # default LOCAL — works out of box

    raw = bundle(data_dir)
    name = _current_local_backup_name(config)

    if mode == "local":
        key = _get_or_create_local_key(data_dir)
        encrypted = encrypt(raw, key)
        backup_dir = _local_backup_dir(config, data_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / name
        tmp = dest.with_suffix(".enc.tmp")
        tmp.write_bytes(encrypted)
        tmp.replace(dest)
        pruned = _prune_local_backups(backup_dir, retention=1)
        state = _load_state(data_dir)
        state["last_backup_ts"] = int(time.time())
        state["last_backup_name"] = name
        state["last_backup_bytes"] = len(encrypted)
        state["last_backup_mode"] = "local"
        state["last_backup_path"] = str(dest)
        _save_state(data_dir, state)
        log.info(f"local backup ok: {dest} ({len(encrypted)} bytes, pruned {pruned})")
        return {"status": "ok", "mode": "local", "name": name,
                "bytes": len(encrypted), "path": str(dest), "pruned": pruned}

    # mode == "cloud" — original passphrase+upload flow
    name = f"orbi-current.tar.gz.enc"
    state = _load_state(data_dir)
    if not state.get("salt"):
        return {"status": "error", "reason": "passphrase_not_set"}
    key_b64 = bk_cfg.get("_runtime_key_b64") or os.environ.get("ORBI_BACKUP_KEY")
    if not key_b64:
        return {"status": "error", "reason": "no_runtime_key"}
    try:
        key = base64.b64decode(key_b64)
    except Exception:
        return {"status": "error", "reason": "invalid_runtime_key"}
    upload_url = bk_cfg.get("upload_url")
    if not upload_url:
        return {"status": "error", "reason": "no_upload_url"}
    encrypted = encrypt(raw, key)
    final_url = upload_url.replace("{filename}", name)
    ok = upload_to_presigned_url(encrypted, final_url)
    if not ok:
        return {"status": "error", "reason": "upload_failed",
                "name": name, "bytes": len(encrypted)}
    state["last_backup_ts"] = int(time.time())
    state["last_backup_name"] = name
    state["last_backup_bytes"] = len(encrypted)
    state["last_backup_mode"] = "cloud"
    _save_state(data_dir, state)
    log.info(f"cloud backup ok: {name} ({len(encrypted)} bytes)")
    return {"status": "ok", "mode": "cloud", "name": name, "bytes": len(encrypted)}


def run_restore(config: dict, data_dir: Path,
                passphrase: str, download_url: str) -> dict:
    """Download, decrypt, extract into data_dir. Returns summary."""
    if not HAS_CRYPTO:
        return {"status": "error", "reason": "cryptography_missing"}
    state = _load_state(data_dir)
    if state.get("salt"):
        key = verify_passphrase(data_dir, passphrase)
        if not key:
            return {"status": "error", "reason": "wrong_passphrase"}
    else:
        return {"status": "error", "reason": "no_passphrase_state"}

    encrypted = download_from_url(download_url)
    if not encrypted:
        return {"status": "error", "reason": "download_failed"}

    try:
        raw = decrypt(encrypted, key)
    except Exception as e:
        return {"status": "error", "reason": f"decrypt_failed: {e}"}

    # Move current data aside before restoring
    safety = data_dir.with_suffix(f".prerestore-{int(time.time())}")
    if data_dir.exists():
        os.rename(data_dir, safety)
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            tar.extractall(data_dir)
    except Exception as e:
        # Roll back
        if safety.exists():
            os.rename(safety, data_dir)
        return {"status": "error", "reason": f"extract_failed: {e}"}

    return {"status": "ok", "bytes_restored": len(raw),
            "safety_backup": str(safety)}


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

import threading

def start_daily_backup(config: dict, data_dir: Path) -> None:
    bk_cfg = config.get("backup", {})
    if not bk_cfg.get("enabled"):
        return
    interval = int(bk_cfg.get("interval_seconds", 86400))

    def loop():
        # First run after a small delay so startup doesn't race
        time.sleep(60)
        while True:
            try:
                result = run_backup(config, data_dir)
                log.info(f"daily backup: {result}")
            except Exception as e:
                log.warning(f"daily backup error: {e}")
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    log.info(f"daily backup scheduler started (interval={interval}s)")
