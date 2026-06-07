#!/usr/bin/env python3
"""
install_runtime — the actual logic that runs on a paying customer's machine
when they double-click the Orbi installer.

The PyInstaller bundle produced by build_orbi_installer.py wraps THIS module
as its entry point. It performs the install flow end-to-end:

  1. Prompts for the install-token Stripe gave the customer.
  2. Calls brain.twickell.com to verify the token and pull
     {customer_id, api_key, tier, owner_email}.
  3. Creates the install directory layout (/opt/orbi on Linux/Mac,
     C:\\Program Files\\Orbi on Windows).
  4. Writes a populated config.json from the template.
  5. Bootstraps the owner user via users.add_user(...) with a random
     12-char password.
  6. Installs the OS service:
        Linux   → systemd unit
        macOS   → launchd plist (LaunchDaemon)
        Windows → SC-created service via nssm/sc.exe
  7. Verifies the install with a /health probe.
  8. Opens a browser tab on http://localhost:5050/owner/login with the
     temporary password pre-filled (via ?bootstrap=<one-shot-token>).

Pure stdlib wherever possible. The bundle has no internet dependency
beyond the single HTTPS call to verify the install token.

Conventions:
  log = logging.getLogger("orbi.installer")
  every user-supplied string is sanitized before being written to disk
  every file write is atomic
  every shared-state mutation is guarded by threading.Lock
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

log = logging.getLogger("orbi.installer")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BILLING_BASE_URL = os.environ.get(
    "ORBI_BILLING_URL", "https://billing.twickell.com"
)
# Brain proxy URL — separate env var so Frank can move the LLM brain to a
# different host (a self-owned 70B box, an alternate Cloudflare tunnel)
# without disturbing the billing host. Defaults to the same hostname
# because today both run on the same machine.
BRAIN_BASE_URL = os.environ.get(
    "ORBI_BRAIN_URL", BILLING_BASE_URL
)
VERIFY_TIMEOUT_SECONDS = 15
HEALTH_TIMEOUT_SECONDS = 30
HEALTH_PORT = 5050

# Where to install based on platform
_SYS = sys.platform


def default_install_dir() -> Path:
    if _SYS.startswith("win"):
        return Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Orbi"
    # Linux + macOS both use /opt/orbi by convention
    return Path("/opt/orbi")


# ---------------------------------------------------------------------------
# Sanitization helpers (defensive — installer can't trust ANY input)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_BIZNAME_RE = re.compile(r"^[A-Za-z0-9 .,'&\-]{1,80}$")


def sanitize_token(raw: str) -> str:
    """Strip whitespace and verify shape of an install token."""
    raw = (raw or "").strip()
    if not _TOKEN_RE.match(raw):
        raise ValueError("token must be 16-128 chars of [A-Za-z0-9_-]")
    return raw


def _normalize_install_token(raw: str) -> str | None:
    """Be forgiving about how customers paste an install token. Handles:
      - leading/trailing whitespace, newlines, tabs
      - wrapping single or double quotes
      - the customer pasting an entire URL (extracts the token segment)
      - extra angle brackets <inst_...> from copy from email
      - the customer pasting WITHOUT the `inst_` prefix (we add it)
    Returns the normalized token, or None if no valid token was found."""
    import re as _re
    s = (raw or "").strip().strip("'\"<>` ")
    s = s.replace("\n", "").replace("\r", "").replace("\t", "")
    # If it looks like a URL, pull out the last path segment that
    # matches the token shape.
    if "://" in s or "/" in s:
        # Find any inst_XXX...XXX substring
        m = _re.search(r"(inst_[A-Za-z0-9_\-]{16,128})", s)
        if m:
            return m.group(1)
        # Or the last path-y segment
        tail = s.rstrip("/").rsplit("/", 1)[-1]
        s = tail
    # Allow customers who copied without the prefix
    if not s.startswith("inst_") and _re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", s):
        s = "inst_" + s
    # Final shape check — must start with inst_ and be 21-133 total
    if _re.fullmatch(r"inst_[A-Za-z0-9_\-]{16,128}", s):
        return s
    return None


def sanitize_email(raw: str) -> str:
    raw = (raw or "").strip().lower()
    if not _EMAIL_RE.match(raw):
        raise ValueError(f"not a valid email address: {raw!r}")
    return raw


def sanitize_business_name(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not _BIZNAME_RE.match(raw):
        # Best-effort scrub: keep letters, numbers, spaces, common punctuation
        raw = re.sub(r"[^A-Za-z0-9 .,'&\-]", "", raw)[:80].strip()
    return raw


# ---------------------------------------------------------------------------
# Atomic file write helper
# ---------------------------------------------------------------------------

_FILE_LOCK = threading.Lock()


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Write content to path via a temp file + rename. Best-effort chmod."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FILE_LOCK:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        try:
            os.chmod(tmp_path, mode)
        except (OSError, NotImplementedError):
            pass
        os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# 1. verify_token
# ---------------------------------------------------------------------------

def verify_token(token: str) -> dict | None:
    """Call the billing service to verify an install token.

    Returns the dict {customer_id, api_key, tier, owner_email} on success,
    or None on any verification failure. Network errors → None (log only).
    """
    try:
        token = sanitize_token(token)
    except ValueError as e:
        log.warning("token rejected at client: %s", e)
        return None

    url = f"{BILLING_BASE_URL.rstrip('/')}/api/verify/{token}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "orbi-installer/0.1"}
    )
    try:
        with urllib.request.urlopen(req, timeout=VERIFY_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                log.warning("verify_token: HTTP %s", resp.status)
                return None
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        log.warning("verify_token HTTPError: %s", e.code)
        return None
    except urllib.error.URLError as e:
        log.warning("verify_token URLError: %s", e.reason)
        return None
    except (TimeoutError, OSError) as e:
        log.warning("verify_token network error: %s", e)
        return None

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("verify_token: server returned non-JSON")
        return None

    required = {"customer_id", "api_key", "tier", "owner_email"}
    if not required.issubset(data):
        log.warning("verify_token: missing fields in response %s", data.keys())
        return None
    # Trust but sanitize what we just got
    try:
        data["owner_email"] = sanitize_email(data["owner_email"])
    except ValueError:
        log.warning("verify_token: bad owner_email from server")
        return None
    return data


# ---------------------------------------------------------------------------
# 2. setup_directories
# ---------------------------------------------------------------------------

def setup_directories(install_dir: Path) -> None:
    """Create /opt/orbi/{data,snapshots,llm_local,tunnel,bin,tts_models}
    (or platform-equiv). Idempotent — won't clobber existing data."""
    install_dir = Path(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("data", "snapshots", "llm_local", "tunnel", "bin", "tts_models"):
        (install_dir / sub).mkdir(parents=True, exist_ok=True)
    # Lock down data dir on POSIX
    if not _SYS.startswith("win"):
        try:
            os.chmod(install_dir / "data", 0o700)
        except OSError as e:
            log.warning("chmod 700 on data/ failed: %s", e)
    # Move any bundled binaries (ffmpeg, cloudflared, piper) from the
    # PyInstaller temp dir into install_dir/bin/ so they survive the
    # installer exiting.
    extract_bundled_binaries(install_dir / "bin")
    # Voice models for Piper TTS go in a separate dir.
    extract_bundled_voice_models(install_dir / "tts_models")
    log.info("install dirs ready under %s", install_dir)


def extract_bundled_voice_models(models_dir: Path) -> list[Path]:
    """Copy Piper voice models (.onnx + .onnx.json) from the PyInstaller
    bundle's tts_models/ into the persistent install_dir/tts_models/."""
    models_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return out
    src = Path(meipass) / "tts_models"
    if not src.exists() or not src.is_dir():
        log.info("no tts_models/ in bundle — Piper TTS will fall back to edge_tts")
        return out
    import shutil
    for f in src.iterdir():
        if not f.is_file():
            continue
        dst = models_dir / f.name
        try:
            shutil.copy2(f, dst)
            out.append(dst)
            log.info("installed voice model: %s (%.1f MB)",
                     dst.name, dst.stat().st_size / 1e6)
        except OSError as e:
            log.warning("could not install voice model %s: %s", f.name, e)
    return out


def extract_bundled_binaries(bin_dir: Path) -> list[Path]:
    """When run from a PyInstaller bundle, sys._MEIPASS/bin/ holds the
    ffmpeg + cloudflared binaries we shipped at build time. Copy them
    into the persistent install_dir/bin/ so the running Orbi process
    can find them after the installer exits.

    Returns the list of binaries successfully installed (so the caller
    can write their paths into config.json)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        log.info("not running from PyInstaller bundle — skipping binary extraction")
        return out
    src = Path(meipass) / "bin"
    if not src.exists() or not src.is_dir():
        log.info("no bin/ subdirectory in bundle — no bundled binaries to install")
        return out
    for f in src.iterdir():
        if not f.is_file():
            continue
        dst = bin_dir / f.name
        try:
            import shutil
            shutil.copy2(f, dst)
            if not _SYS.startswith("win"):
                dst.chmod(0o755)
            out.append(dst)
            log.info("installed bundled binary: %s", dst)
        except OSError as e:
            log.warning("could not install %s: %s", f.name, e)
    return out


# ---------------------------------------------------------------------------
# 2b. Extract the Orby app source from the PyInstaller bundle into the
# install dir and install Python dependencies. Without this step the
# systemd service would point at a non-existent /opt/orbi/orbi.py.
# ---------------------------------------------------------------------------

APP_TARBALL_NAME = "orbi_app.tar.gz"


def extract_app_source(install_dir: Path) -> int:
    """Extract the bundled orbi_app.tar.gz into install_dir. Returns the
    number of files written. Safe to re-run — overwrites existing files
    but doesn't touch data/, snapshots/, or .session_secret.

    The tarball was built by build_orbi_installer.build_app_tarball() and
    embedded by PyInstaller --add-data. It contains orbi.py, modules/,
    owner_dashboard/, static/, pwa/, requirements.txt, and every other
    file the running Orby process needs (excluding per-install state).
    """
    import tarfile
    install_dir = Path(install_dir)
    meipass = getattr(sys, "_MEIPASS", None)
    candidates = []
    if meipass:
        candidates.append(Path(meipass) / APP_TARBALL_NAME)
    here = Path(__file__).resolve().parent
    candidates.append(here / "build" / APP_TARBALL_NAME)   # source-tree dev
    candidates.append(here / APP_TARBALL_NAME)              # alongside script
    src = next((c for c in candidates if c.is_file()), None)
    if not src:
        raise FileNotFoundError(
            "Cannot find orbi_app.tar.gz — installer bundle is incomplete. "
            "Tried: " + ", ".join(str(c) for c in candidates)
        )
    # Protect per-install state — never overwrite these from the tarball.
    protected = {"data", "backups", "snapshots", "_archive",
                  "config.json", ".session_secret"}
    count = 0
    with tarfile.open(src, "r:gz") as tar:
        for member in tar.getmembers():
            top = member.name.split("/", 1)[0]
            if top in protected:
                continue
            tar.extract(member, install_dir, filter="data")
            count += 1
    log.info("extracted %d app files into %s", count, install_dir)
    return count


def _find_system_python() -> str:
    """Find a real Python interpreter on the customer's system. Inside a
    PyInstaller bundle, sys.executable points at the bundle binary itself,
    NOT at a Python interpreter — so we can't use sys.executable to spawn
    `python -m venv`. We have to find python3 on the host PATH.

    Raises RuntimeError if no Python ≥3.10 is found, with a clear hint
    to install one. (apt install python3 / brew install python / the
    python.org installer on Windows.)
    """
    candidates = []
    if _SYS.startswith("win"):
        candidates = ["py", "python", "python3"]
    else:
        candidates = ["python3.12", "python3.11", "python3.10", "python3"]
    for name in candidates:
        path = shutil.which(name)
        if not path:
            continue
        # Quick sanity check — must be ≥3.10 (Flask + edge-tts both want it)
        try:
            r = subprocess.run(
                [path, "-c", "import sys; print(sys.version_info[:2])"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and "(3," in r.stdout:
                major_minor = r.stdout.strip()
                # Parse "(3, 11)" → minor=11
                try:
                    minor = int(major_minor.split(",")[1].strip().rstrip(")"))
                    if minor >= 10:
                        log.info("using system python: %s (3.%d)", path, minor)
                        return path
                except ValueError:
                    continue
        except (subprocess.SubprocessError, OSError):
            continue
    raise RuntimeError(
        "No Python ≥3.10 found on PATH. Install python3 (apt install python3 "
        "on Ubuntu, brew install python3 on macOS, or python.org installer "
        "on Windows) and re-run this installer."
    )


def install_python_deps(install_dir: Path) -> None:
    """pip install -r requirements.txt into install_dir/.venv so the
    systemd service runs with all packages available. Creates the venv
    if it doesn't exist. Uses the host system's python3, NOT
    sys.executable (which inside a PyInstaller bundle points to the
    bundle binary itself)."""
    install_dir = Path(install_dir)
    req = install_dir / "requirements.txt"
    if not req.is_file():
        log.warning("no requirements.txt in %s — skipping pip install", install_dir)
        return
    venv_dir = install_dir / ".venv"
    if not venv_dir.exists():
        log.info("creating venv at %s", venv_dir)
        system_python = _find_system_python()
        subprocess.run([system_python, "-m", "venv", str(venv_dir)],
                        check=True)
    pip = venv_dir / ("Scripts" if _SYS.startswith("win") else "bin") / "pip"
    if not pip.exists():
        # Fallback to using the venv's python -m pip
        py = venv_dir / ("Scripts" if _SYS.startswith("win") else "bin") / "python"
        pip_cmd = [str(py), "-m", "pip"]
    else:
        pip_cmd = [str(pip)]
    log.info("installing Python deps from %s into %s", req, venv_dir)
    subprocess.run(pip_cmd + ["install", "--upgrade", "pip"],
                    check=False)
    subprocess.run(pip_cmd + ["install", "-r", str(req)], check=True)
    log.info("Python deps installed")


# ---------------------------------------------------------------------------
# 3. write_config
# ---------------------------------------------------------------------------

def _load_template() -> dict:
    """Locate config.json.template — works both when running from source
    and when running inside a PyInstaller bundle (sys._MEIPASS)."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "config.json.template")
    here = Path(__file__).resolve().parent
    candidates.append(here.parent / "config.json.template")
    candidates.append(here / "config.json.template")
    for c in candidates:
        if c.is_file():
            return json.loads(c.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        "config.json.template not found in any of: "
        + ", ".join(str(c) for c in candidates)
    )


def write_config(install_dir: Path, billing_data: dict,
                 business_name: str = "") -> Path:
    """Populate config.json from the template + verified billing data.
    Returns the path to the config file."""
    install_dir = Path(install_dir)
    cfg = _load_template()

    owner_email = sanitize_email(billing_data["owner_email"])
    api_key = billing_data["api_key"]
    tier = billing_data.get("tier", "standard")
    biz = sanitize_business_name(business_name)

    cfg.setdefault("owner", {})["email"] = owner_email
    cfg["owner"]["name"] = owner_email.split("@", 1)[0].title()
    # password_hash is filled in by bootstrap_owner() via users.add_user
    cfg["owner"].pop("_password_hash", None)

    cfg.setdefault("brain", {})["api_key"] = api_key
    cfg["brain"]["url"] = BRAIN_BASE_URL.rstrip("/")
    cfg["brain"].setdefault("timeout_seconds", 30)
    # Auto-start a cloudflared trycloudflare tunnel on boot so the
    # brain server can forward Twilio webhooks + visitor traffic back
    # to this machine. URL is reported via heartbeat. Zero-config —
    # customer never touches Cloudflare.
    cfg.setdefault("tunnel", {})["enabled"] = True
    # TTS: prefer Piper (self-hosted, MIT, commercially clean). orbi.py
    # automatically falls back to edge_tts if the bundled Piper binary
    # or voice model isn't present, so customer installs survive a
    # voice-model download failure during the build.
    cfg.setdefault("tts", {})["engine"] = "piper"
    cfg["tts"].setdefault("voice_model", "en_US-amy-medium")
    cfg.setdefault("billing", {})["check_url"] = (
        f"{BILLING_BASE_URL.rstrip('/')}/api/active"
    )

    if biz:
        cfg.setdefault("business", {})["name"] = biz

    cfg["tier"] = tier
    cfg["active"] = True
    # Paid add-on modules (Contractor Orby, Legal Orby, etc.). The Stripe
    # webhook computes this list based on which prices the customer
    # bought. Empty list = base Orby; lists like ['contractor'] or
    # ['legal','paralegal'] unlock the corresponding chat handlers.
    cfg["enabled_modules"] = list(billing_data.get("enabled_modules", []))
    cfg["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%SZ")
    cfg["_customer_id"] = billing_data.get("customer_id", "")

    config_path = install_dir / "config.json"
    atomic_write_text(
        config_path,
        json.dumps(cfg, indent=2, ensure_ascii=False),
        mode=0o600,
    )
    log.info("config.json written at %s", config_path)
    return config_path


# ---------------------------------------------------------------------------
# 4. bootstrap_owner
# ---------------------------------------------------------------------------

def _random_password(length: int = 12) -> str:
    """Cryptographically-random 12-char password. Avoids ambiguous chars."""
    alphabet = (string.ascii_letters + string.digits).translate(
        str.maketrans("", "", "Il1O0")
    )
    return "".join(secrets.choice(alphabet) for _ in range(length))


def bootstrap_owner(install_dir: Path, owner_email: str) -> str:
    """Create the bootstrap owner user inside install_dir/data/users.json.
    Returns the plaintext password so the caller can display it once.

    Imports users.py from the install dir (the same module the running
    service will use) so the password hashing format is identical.
    """
    install_dir = Path(install_dir)
    owner_email = sanitize_email(owner_email)
    data_dir = install_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Make sure we can import users.py from the install dir
    install_str = str(install_dir)
    added = False
    if install_str not in sys.path:
        sys.path.insert(0, install_str)
        added = True
    try:
        try:
            import users as _users_mod  # type: ignore
        except ImportError as e:
            # Tolerate running from /home/frank/orbi_web/customer_install (dev mode)
            log.warning("could not import users from %s: %s", install_str, e)
            fallback = Path(__file__).resolve().parent.parent
            if str(fallback) not in sys.path:
                sys.path.insert(0, str(fallback))
            import users as _users_mod  # type: ignore

        username = owner_email.split("@", 1)[0].lower()
        # If a user with that key already exists, append a digit
        suffix = 0
        original = username
        while True:
            existing = _users_mod.load_users(data_dir).get(username)
            if not existing:
                break
            suffix += 1
            username = f"{original}{suffix}"
            if suffix > 99:
                raise RuntimeError("cannot find unique owner username")

        password = _random_password(12)
        _users_mod.add_user(
            data_dir=data_dir,
            username=username,
            password=password,
            role="owner",
            display_name=owner_email.split("@", 1)[0].title(),
        )
        log.info("bootstrap owner created: %s", username)
        # Persist the username back into config.json for the wizard
        cfg_path = install_dir / "config.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg.setdefault("owner", {})["username"] = username
                atomic_write_text(
                    cfg_path,
                    json.dumps(cfg, indent=2, ensure_ascii=False),
                    mode=0o600,
                )
            except (json.JSONDecodeError, OSError) as e:
                log.warning("could not stamp username into config: %s", e)
        return password
    finally:
        if added:
            try:
                sys.path.remove(install_str)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# 5. install_service — platform-specific
# ---------------------------------------------------------------------------

def install_service(install_dir: Path) -> None:
    """Register the OS service that runs orbi.py on boot."""
    system = platform.system().lower()
    if system == "linux":
        _install_service_linux(install_dir)
    elif system == "darwin":
        _install_service_mac(install_dir)
    elif system == "windows":
        _install_service_windows(install_dir)
    else:
        raise RuntimeError(f"Unsupported platform: {system}")


_LINUX_UNIT = """\
[Unit]
Description=Orbi customer service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=orbi
WorkingDirectory={install_dir}
Environment=PYTHONUNBUFFERED=1
Environment=ORBI_DIR={install_dir}
Environment=PATH={install_dir}/bin:/usr/local/bin:/usr/bin:/bin
ExecStart={python} {install_dir}/orbi.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def _install_service_linux(install_dir: Path) -> None:
    install_dir = Path(install_dir)
    unit_path = Path("/etc/systemd/system/orbi.service")
    # Prefer the venv's python (which has Flask + edge-tts + the rest
    # installed by install_python_deps). Fall back to system python if
    # the venv wasn't set up.
    venv_py = install_dir / ".venv" / "bin" / "python"
    if venv_py.exists():
        python_bin = str(venv_py)
    else:
        try:
            python_bin = _find_system_python()
        except RuntimeError:
            python_bin = shutil.which("python3") or "/usr/bin/python3"
    content = _LINUX_UNIT.format(install_dir=install_dir, python=python_bin)
    try:
        atomic_write_text(unit_path, content, mode=0o644)
    except PermissionError:
        raise RuntimeError(
            "Need root to write /etc/systemd/system/orbi.service — "
            "re-run installer with sudo."
        )
    # Make sure the 'orbi' user exists; install.sh already handles this
    # for the bash path but we double-check here for the GUI installer.
    try:
        subprocess.run(["id", "orbi"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        subprocess.run(
            ["useradd", "-r", "-s", "/usr/sbin/nologin", "-d",
             str(install_dir), "orbi"],
            check=False,
        )
    # chown install_dir to orbi
    try:
        subprocess.run(
            ["chown", "-R", "orbi:orbi", str(install_dir)], check=False
        )
    except FileNotFoundError:
        pass
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    subprocess.run(
        ["systemctl", "enable", "--now", "orbi.service"], check=False
    )
    log.info("systemd unit installed at %s", unit_path)


_MAC_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.orbi.customer</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{install_dir}/orbi.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{install_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ORBI_DIR</key>
        <string>{install_dir}</string>
        <key>PATH</key>
        <string>{install_dir}/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{install_dir}/data/orbi.out.log</string>
    <key>StandardErrorPath</key>
    <string>{install_dir}/data/orbi.err.log</string>
</dict>
</plist>
"""


def _install_service_mac(install_dir: Path) -> None:
    install_dir = Path(install_dir)
    plist_path = Path("/Library/LaunchDaemons/com.orbi.customer.plist")
    python_bin = shutil.which("python3") or "/usr/bin/python3"
    content = _MAC_PLIST.format(install_dir=install_dir, python=python_bin)
    try:
        atomic_write_text(plist_path, content, mode=0o644)
    except PermissionError:
        raise RuntimeError(
            "Need root to write /Library/LaunchDaemons/com.orbi.customer.plist"
            " — re-run installer with sudo."
        )
    # Load the daemon (best-effort)
    subprocess.run(["launchctl", "unload", str(plist_path)], check=False,
                   capture_output=True)
    rc = subprocess.run(
        ["launchctl", "load", "-w", str(plist_path)], check=False
    )
    if rc.returncode != 0:
        log.warning("launchctl load returned %s", rc.returncode)
    log.info("launchd plist installed at %s", plist_path)


def _install_service_windows(install_dir: Path) -> None:
    """Register a Windows service via sc.exe. Prefers nssm if available
    because Python scripts as native services are awkward without a wrapper."""
    install_dir = Path(install_dir)
    python_bin = shutil.which("python") or shutil.which("py") or "python"
    nssm = shutil.which("nssm")

    if nssm:
        # nssm is the cleanest option
        subprocess.run([nssm, "install", "Orbi", python_bin,
                        str(install_dir / "orbi.py")], check=False)
        subprocess.run([nssm, "set", "Orbi", "AppDirectory",
                        str(install_dir)], check=False)
        subprocess.run([nssm, "set", "Orbi", "Start", "SERVICE_AUTO_START"],
                       check=False)
        subprocess.run([nssm, "start", "Orbi"], check=False)
        log.info("Windows service registered via nssm")
        return

    # Fallback: native sc.exe. The customer-side Python interpreter must
    # be on PATH at boot time, which is normally the case after PyInstaller.
    binpath = f'"{python_bin}" "{install_dir / "orbi.py"}"'
    subprocess.run(
        ["sc.exe", "create", "Orbi", "binPath=", binpath,
         "start=", "auto", "DisplayName=", "Orbi Customer Service"],
        check=False,
    )
    subprocess.run(["sc.exe", "start", "Orbi"], check=False)
    log.info("Windows service registered via sc.exe (no nssm)")


# ---------------------------------------------------------------------------
# 6. verify_install
# ---------------------------------------------------------------------------

def verify_install(install_dir: Path) -> bool:
    """Sanity-check: config.json + users.json exist, owner user is present."""
    install_dir = Path(install_dir)
    cfg = install_dir / "config.json"
    users = install_dir / "data" / "users.json"
    if not cfg.exists():
        log.error("verify_install: config.json missing at %s", cfg)
        return False
    if not users.exists():
        log.error("verify_install: users.json missing at %s", users)
        return False
    try:
        c = json.loads(cfg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error("verify_install: config.json unreadable: %s", e)
        return False
    if not c.get("brain", {}).get("api_key"):
        log.error("verify_install: api_key not set in config.json")
        return False
    try:
        u = json.loads(users.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error("verify_install: users.json unreadable: %s", e)
        return False
    if not any(rec.get("role") == "owner" for rec in u.values()):
        log.error("verify_install: no owner user in users.json")
        return False
    log.info("verify_install: PASS")
    return True


# ---------------------------------------------------------------------------
# 6b. create_desktop_shortcut — gives the customer an icon they can find
# ---------------------------------------------------------------------------

def create_desktop_shortcut(install_dir: Path) -> None:
    """Drop a desktop icon that opens the Orbi dashboard.

    Windows: writes a launcher.cmd in install_dir that starts the service
    (idempotent — sc start does nothing if already running) and opens
    the default browser. Then drops an Orbi.lnk on the user's Desktop
    pointing at the .cmd. Created via PowerShell since it's always
    available on Windows 10/11 and avoids needing pywin32 in the bundle.

    Linux: writes ~/Desktop/Orbi.desktop (the standard XDG entry).

    Mac: skipped for now — Mac users typically use Spotlight or
    /Applications, not desktop icons. Future work.
    """
    install_dir = Path(install_dir)
    home = Path.home()
    desktop = home / "Desktop"
    if not desktop.exists():
        # Some Linux distros + headless installs don't have a Desktop dir.
        log.info("no ~/Desktop directory — skipping desktop shortcut")
        return

    if _SYS.startswith("win"):
        # 1) Write the launcher .cmd. It tries Chrome's `--app=URL` mode
        #    first (no URL bar, no tabs — looks like a real desktop app),
        #    then Edge, then falls back to the default browser. Result is
        #    a window that feels native instead of "just another website."
        launcher = install_dir / "launcher.cmd"
        url = f"http://localhost:{HEALTH_PORT}/owner/login"
        launcher_body = (
            "@echo off\r\n"
            "REM Start Orbi service if not already running, then open the\r\n"
            "REM dashboard in app-mode (no URL bar, no tabs — feels native).\r\n"
            "sc start Orbi >nul 2>&1\r\n"
            "REM Give the service a few seconds to come up on a cold start.\r\n"
            "ping -n 4 127.0.0.1 >nul\r\n"
            "\r\n"
            "REM Try Chrome installed via the launcher (PATH) first\r\n"
            "where chrome.exe >nul 2>&1\r\n"
            f"if %ERRORLEVEL% == 0 ( start \"\" chrome.exe --app=\"{url}\" & exit /b )\r\n"
            "\r\n"
            "REM Standard Chrome install locations\r\n"
            "if exist \"%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe\" ( "
            f"start \"\" \"%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe\" --app=\"{url}\" & exit /b )\r\n"
            "if exist \"%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe\" ( "
            f"start \"\" \"%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe\" --app=\"{url}\" & exit /b )\r\n"
            "if exist \"%LocalAppData%\\Google\\Chrome\\Application\\chrome.exe\" ( "
            f"start \"\" \"%LocalAppData%\\Google\\Chrome\\Application\\chrome.exe\" --app=\"{url}\" & exit /b )\r\n"
            "\r\n"
            "REM Try Edge (ships with Windows 10/11 by default)\r\n"
            "if exist \"%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe\" ( "
            f"start \"\" \"%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe\" --app=\"{url}\" & exit /b )\r\n"
            "if exist \"%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe\" ( "
            f"start \"\" \"%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe\" --app=\"{url}\" & exit /b )\r\n"
            "\r\n"
            "REM Last resort: open in whatever the user's default browser is.\r\n"
            f"start \"\" \"{url}\"\r\n"
        )
        try:
            launcher.write_text(launcher_body, encoding="ascii", newline="")
        except OSError as e:
            log.warning("could not write launcher.cmd: %s", e)
            return

        # 2) Make the .lnk via PowerShell COM
        shortcut_path = desktop / "Orbi.lnk"
        # Embed paths via PS variable assignments to avoid quoting issues.
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$sc = $ws.CreateShortcut('{shortcut_path}'); "
            f"$sc.TargetPath = '{launcher}'; "
            f"$sc.WorkingDirectory = '{install_dir}'; "
            "$sc.Description = 'Open your Orbi dashboard'; "
            "$sc.Save()"
        )
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-Command", ps],
                check=False, timeout=15,
            )
            log.info("desktop shortcut created at %s", shortcut_path)
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("could not create Windows desktop shortcut: %s", e)
        return

    if _SYS == "linux":
        # 1) Write the launcher.sh — tries Chromium-family browsers with
        #    --app mode so the window has no URL bar (looks like a native
        #    app), falls back to xdg-open if none are installed.
        launcher = install_dir / "launcher.sh"
        url = f"http://localhost:{HEALTH_PORT}/owner/login"
        launcher_body = (
            "#!/bin/bash\n"
            f"URL=\"{url}\"\n"
            "\n"
            "# Idempotent service start (user systemd; fails silently if\n"
            "# the service is already running or systemd isn't available).\n"
            "systemctl --user start orbi 2>/dev/null\n"
            "\n"
            "# Wait briefly for the dashboard to come up.\n"
            "for _i in 1 2 3 4 5; do\n"
            "  if curl -s -o /dev/null --max-time 1 \"$URL\" 2>/dev/null; then break; fi\n"
            "  sleep 1\n"
            "done\n"
            "\n"
            "# Try Chrome/Chromium/Edge in --app mode for a native-app feel.\n"
            "for browser in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge brave-browser; do\n"
            "  if command -v \"$browser\" >/dev/null 2>&1; then\n"
            "    exec \"$browser\" --app=\"$URL\"\n"
            "  fi\n"
            "done\n"
            "\n"
            "# Fallback: default browser (URL bar visible, but at least it opens).\n"
            "exec xdg-open \"$URL\"\n"
        )
        try:
            launcher.write_text(launcher_body, encoding="utf-8")
            os.chmod(launcher, 0o755)
        except OSError as e:
            log.warning("could not write launcher.sh: %s", e)
            return

        # 2) Write the .desktop entry pointing at launcher.sh.
        shortcut_path = desktop / "Orbi.desktop"
        body = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Orbi\n"
            "Comment=Open your Orbi assistant\n"
            f"Exec={launcher}\n"
            "Icon=applications-internet\n"
            "Terminal=false\n"
            "Categories=Network;Utility;\n"
        )
        try:
            shortcut_path.write_text(body, encoding="utf-8")
            # GNOME / KDE want the .desktop file marked executable + "trusted"
            os.chmod(shortcut_path, 0o755)
            log.info("desktop shortcut created at %s", shortcut_path)
        except OSError as e:
            log.warning("could not create Linux desktop shortcut: %s", e)
        return

    # Mac falls through — skipped for now.
    log.info("desktop shortcut not implemented for %s — skipping", _SYS)


# ---------------------------------------------------------------------------
# 7. launch_setup_wizard
# ---------------------------------------------------------------------------

def _wait_for_health(port: int = HEALTH_PORT,
                     timeout: float = HEALTH_TIMEOUT_SECONDS) -> bool:
    """Poll http://localhost:<port>/health until 200 or timeout."""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass
        time.sleep(1.5)
    return False


def launch_setup_wizard(install_dir: Path, temp_password: str) -> None:
    """Wait for the service to be healthy, then open the owner login page
    in the customer's default browser with the temp password pre-filled
    as a query param. The owner page can read ?bootstrap=<pw> on first
    visit and pre-fill the login form."""
    install_dir = Path(install_dir)
    log.info("waiting for orbi service to come up …")
    healthy = _wait_for_health()
    if not healthy:
        log.warning("orbi service didn't respond on /health in time — "
                    "browser will still open but the page may need a refresh")

    # Read the username we just bootstrapped
    cfg = install_dir / "config.json"
    username = ""
    try:
        c = json.loads(cfg.read_text(encoding="utf-8"))
        username = c.get("owner", {}).get("username", "")
    except (json.JSONDecodeError, OSError):
        pass

    from urllib.parse import urlencode
    qs = urlencode({"u": username, "bootstrap": temp_password})
    url = f"http://localhost:{HEALTH_PORT}/owner/login?{qs}"
    try:
        webbrowser.open(url, new=2)
        log.info("opened browser at %s", url)
    except webbrowser.Error as e:
        log.warning("could not open browser automatically: %s", e)
        print(f"\nOpen this URL manually:\n  {url}\n")


# ---------------------------------------------------------------------------
# Orchestration — main installer flow
# ---------------------------------------------------------------------------

_BANNER = r"""
   ____       _     _   ___           _        _ _
  / __ \ ___ | |__ (_) |_ _|_ __  ___| |_ __ _| | | ___ _ __
 | |  | |/ _ \| '_ \| |  | || '_ \/ __| __/ _` | | |/ _ \ '__|
 | |__| |  __/| |_) | |  | || | | \__ \ || (_| | | |  __/ |
  \____/ \___||_.__/|_| |___|_| |_|___/\__\__,_|_|_|\___|_|
"""


def _print(msg: str = "") -> None:
    """Print without buffering. Avoids stdlib `print` quirks in PyInstaller."""
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _read_clipboard() -> str:
    """Best-effort clipboard read using stdlib tkinter. Works on Windows,
    Mac, and Linux (with a display server). Silent fallback to empty
    string if anything goes wrong — installer falls back to prompting."""
    # Try platform-native tools first (Mac pbpaste / Linux xclip) since
    # they don't require display server setup. Fall back to tkinter.
    import subprocess as _sp
    plat = platform.system().lower()
    try:
        if plat == "darwin":
            r = _sp.run(["pbpaste"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout:
                return r.stdout.strip()
        elif plat == "linux":
            for cmd in (["xclip", "-selection", "clipboard", "-o"],
                        ["wl-paste"],
                        ["xsel", "--clipboard", "--output"]):
                try:
                    r = _sp.run(cmd, capture_output=True, text=True, timeout=3)
                    if r.returncode == 0 and r.stdout:
                        return r.stdout.strip()
                except (FileNotFoundError, _sp.TimeoutExpired):
                    continue
        elif plat == "windows":
            # Windows: use PowerShell's Get-Clipboard (built-in, no install)
            r = _sp.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                        capture_output=True, text=True, timeout=4)
            if r.returncode == 0 and r.stdout:
                return r.stdout.strip()
    except Exception:
        pass
    # Final fallback: tkinter (works everywhere with a display)
    try:
        import tkinter as _tk
        root = _tk.Tk()
        root.withdraw()
        try:
            value = root.clipboard_get()
        finally:
            root.destroy()
        return (value or "").strip()
    except Exception:
        return ""


def run_interactive(install_dir: Path | None = None,
                    argv: list[str] | None = None) -> int:
    """Top-level installer flow. Returns the process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    install_dir = install_dir or default_install_dir()

    _print(_BANNER)
    _print("Welcome to the Orbi installer.")
    _print("")

    # ── Auto-detect token from clipboard or command-line arg ──
    # The install page at twickell.com/install?t=<token> auto-copies the
    # token to the clipboard on click. We try that first so the customer
    # doesn't have to paste anything.
    token = ""
    # 1. Command-line arg wins (--token=inst_...)
    for arg in (argv or sys.argv[1:]):
        if arg.startswith("--token="):
            token = arg.split("=", 1)[1].strip()
            break
        if arg.startswith("--t="):
            token = arg.split("=", 1)[1].strip()
            break
    # 2. Clipboard (tkinter is in stdlib + works cross-platform without
    #    extra packages — important for our PyInstaller bundle)
    if not token:
        clip = _read_clipboard()
        if clip:
            cand = _normalize_install_token(clip)
            if cand:
                token = cand
                _print(f"Found your install token in the clipboard: {token[:14]}…")
    # 3. Manual prompt as fallback — be forgiving about format. Customers
    # paste with leading/trailing whitespace, quote marks, or sometimes
    # the wrong half (the URL surrounding the token). Normalize them all.
    if not token:
        _print("Paste the install token you received in your email.")
        _print("(Tip: visit the link in your email and the token is "
               "copied automatically — you can also paste it here.)")
        _print("It looks like:  inst_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")
        _print("")
        while True:
            raw = input("Install token: ")
            token = _normalize_install_token(raw)
            if token:
                break
            _print("That doesn't look like an install token. It should "
                   "start with 'inst_' and be at least 24 characters. "
                   "Check your email and try again.")

    biz_name = ""
    if not (argv and any(a == "--silent" or a == "--quiet" for a in argv)):
        biz_name = input("Business name (optional, press Enter to skip): ").strip()

    _print("")
    _print("Verifying token with billing service …")
    billing = verify_token(token)
    if not billing:
        _print("ERROR: token could not be verified.")
        _print("       Check your internet connection and try again.")
        _print("       If it still fails, email orbiaisolutions@gmail.com.")
        return 2
    _print(f"  OK — tier: {billing['tier']}, owner: {billing['owner_email']}")

    _print("")
    _print(f"Creating install at {install_dir} …")
    try:
        setup_directories(install_dir)
        _print("Extracting Orbi app files …")
        extract_app_source(install_dir)
        _print("Installing Python dependencies (this takes a minute) …")
        install_python_deps(install_dir)
        write_config(install_dir, billing, biz_name)
        temp_pw = bootstrap_owner(install_dir, billing["owner_email"])
    except Exception as e:
        _print(f"ERROR: install failed during setup: {e}")
        log.exception("setup failed")
        return 3

    _print("")
    _print("Installing system service …")
    try:
        install_service(install_dir)
    except Exception as e:
        _print(f"WARNING: service install failed: {e}")
        _print("         You can run orbi.py manually for now.")
        log.exception("service install failed")

    _print("Creating desktop shortcut …")
    try:
        create_desktop_shortcut(install_dir)
    except Exception as e:
        # Cosmetic — never block the install over a shortcut failure.
        _print(f"NOTE: desktop shortcut could not be created: {e}")
        log.exception("desktop shortcut creation failed")

    _print("")
    if not verify_install(install_dir):
        _print("WARNING: install verification failed. See log for details.")

    _print("")
    _print("=" * 60)
    _print(" Setup wizard will start now.")
    _print(" Your TEMPORARY owner password is:")
    _print("")
    _print(f"     {temp_pw}")
    _print("")
    _print(" Write it down. It will be shown only once.")
    _print(" You'll change it on first login.")
    _print("=" * 60)
    _print("")

    launch_setup_wizard(install_dir, temp_pw)
    _print("Done. The Orbi service is running in the background.")
    return 0


# ---------------------------------------------------------------------------
# Smoke test (only when invoked with --smoke-test)
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    """End-to-end smoke test of the install flow that DOESN'T need root.
    Designed to run in CI so any regression that would ship a broken
    installer to a customer is caught at build time, not after the
    customer types their install token.

    Covers:
      1. setup_directories (file system layout)
      2. extract_app_source (the app actually unpacks from the bundle)
      3. install_python_deps (pip in venv works, requirements resolve)
      4. write_config (config.json written from billing payload)
      5. bootstrap_owner (owner user + password created)
      6. orbi.py imports cleanly under the freshly-created venv python
         (proves both the source + the deps actually work together)

    SKIPS (need root + a running systemd/launchd/SCM):
      - install_service
      - verify_install (HTTP probe)
      - launch_setup_wizard (opens a browser)
    """
    logging.basicConfig(level=logging.INFO)
    tmp = Path(tempfile.mkdtemp(prefix="orbi-installer-smoke-"))
    try:
        print("[1/6] setup_directories ...", flush=True)
        setup_directories(tmp)

        print("[2/6] extract_app_source ...", flush=True)
        n = extract_app_source(tmp)
        for required in ("orbi.py", "requirements.txt",
                          "modules/internal_messages.py",
                          "owner_dashboard/dashboard.html",
                          "owner_dashboard/dashboard.js",
                          "pwa/manifest.json"):
            if not (tmp / required).is_file():
                print(f"FAIL: required app file missing after extract: {required}")
                return 1
        print(f"  extracted {n} files")

        print("[3/6] install_python_deps ...", flush=True)
        install_python_deps(tmp)
        if _SYS.startswith("win"):
            venv_py = tmp / ".venv" / "Scripts" / "python.exe"
        else:
            venv_py = tmp / ".venv" / "bin" / "python"
        if not venv_py.exists():
            print(f"FAIL: venv python not created at {venv_py}")
            return 1

        print("[4/6] write_config ...", flush=True)
        write_config(tmp, {
            "api_key": "smoke_test_api_key_" + secrets.token_urlsafe(16),
            "owner_email": "smoke.test@example.com",
            "tier": "starter",
            "customer_id": "cust_smoketest",
        }, "Smoke Test Business")
        if not (tmp / "config.json").is_file():
            print("FAIL: config.json not written")
            return 1

        print("[5/6] bootstrap_owner ...", flush=True)
        pw = bootstrap_owner(tmp, "smoke.test@example.com")
        users_json = tmp / "data" / "users.json"
        if not users_json.exists():
            print("FAIL: users.json not written")
            return 1
        registry = json.loads(users_json.read_text(encoding="utf-8"))
        owners = [u for u, r in registry.items() if r.get("role") == "owner"]
        if not owners:
            print("FAIL: no owner user found")
            return 1
        if len(pw) != 12:
            print(f"FAIL: password is {len(pw)} chars, expected 12")
            return 1

        print("[6/6] import orbi under venv python ...", flush=True)
        env = os.environ.copy()
        env["ORBI_DIR"] = str(tmp)
        rc = subprocess.run(
            [str(venv_py), "-c",
             f"import sys; sys.path.insert(0, {str(tmp)!r}); "
             f"import orbi; "
             f"assert len(orbi.app.url_map._rules) > 50, 'too few routes'; "
             f"print('orbi imported,', len(orbi.app.url_map._rules), 'routes')"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        if rc.returncode != 0:
            print("FAIL: orbi.py would not import in venv")
            print("  stdout:", rc.stdout.strip())
            print("  stderr (last 2000):", rc.stderr.strip()[-2000:])
            return 1
        print(" ", rc.stdout.strip())

        print(f"PASS — owner={owners[0]} password={pw}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _setup_user_facing_log() -> Path | None:
    """Add a FileHandler that writes to the user's Desktop, so even if
    the cmd window auto-closes we still have a record of what happened.
    Returns the log path so the main wrapper can mention it in error msgs."""
    try:
        home = Path.home()
        # Try Desktop first; fall back to home if Desktop doesn't exist.
        candidates = [home / "Desktop", home / "OneDrive" / "Desktop", home]
        log_dir = next((c for c in candidates if c.exists()), home)
        log_path = log_dir / "orbi_install.log"
        fh = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        logging.getLogger().addHandler(fh)
        logging.getLogger().setLevel(logging.DEBUG)
        return log_path
    except Exception:
        return None


def _hold_window_open(message: str) -> None:
    """On Windows, the cmd window auto-closes when the script exits — which
    eats the error message. Pause for input so the user can read it."""
    if not _SYS.startswith("win"):
        return
    try:
        _print("")
        _print(message)
        _print("")
        input("Press Enter to close this window...")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        sys.exit(_smoke_test())

    # Wrap the whole install in try/except + file log so the user can
    # see the failure even after the cmd window closes.
    _log_path = _setup_user_facing_log()
    try:
        rc = run_interactive()
        if rc != 0:
            _hold_window_open(
                f"Install ended with exit code {rc}. Log saved to: "
                f"{_log_path}" if _log_path else
                f"Install ended with exit code {rc}."
            )
        sys.exit(rc)
    except KeyboardInterrupt:
        _print("Install cancelled.")
        sys.exit(130)
    except Exception as e:
        log.exception("Install failed with uncaught exception")
        _print("")
        _print("=" * 60)
        _print(" INSTALL FAILED")
        _print(f" Error: {e!r}")
        if _log_path:
            _print(f" Full log: {_log_path}")
        _print("=" * 60)
        _hold_window_open(
            "The install hit an error. Send the log file above to "
            "support so we can fix it."
        )
        sys.exit(1)
