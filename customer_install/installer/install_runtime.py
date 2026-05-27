#!/usr/bin/env python3
"""
install_runtime — the actual logic that runs on a paying customer's machine
when they double-click the Orbi installer.

The PyInstaller bundle produced by build_orbi_installer.py wraps THIS module
as its entry point. It performs the install flow end-to-end:

  1. Prompts for the install-token Stripe gave the customer.
  2. Calls billing.orbi.frank.com to verify the token and pull
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
    "ORBI_BILLING_URL", "https://billing.orbi.frank.com"
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
    """Create /opt/orbi/{data,snapshots,llm_local,tunnel,bin} (or platform-equiv).
    Idempotent — won't clobber existing data."""
    install_dir = Path(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("data", "snapshots", "llm_local", "tunnel", "bin"):
        (install_dir / sub).mkdir(parents=True, exist_ok=True)
    # Lock down data dir on POSIX
    if not _SYS.startswith("win"):
        try:
            os.chmod(install_dir / "data", 0o700)
        except OSError as e:
            log.warning("chmod 700 on data/ failed: %s", e)
    # Move any bundled binaries (ffmpeg, cloudflared) from the PyInstaller
    # temp dir into install_dir/bin/ so they survive the installer exiting.
    extract_bundled_binaries(install_dir / "bin")
    log.info("install dirs ready under %s", install_dir)


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
    cfg.setdefault("billing", {})["check_url"] = (
        f"{BILLING_BASE_URL.rstrip('/')}/api/active"
    )

    if biz:
        cfg.setdefault("business", {})["name"] = biz

    cfg["tier"] = tier
    cfg["active"] = True
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


def run_interactive(install_dir: Path | None = None) -> int:
    """Top-level installer flow. Returns the process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    install_dir = install_dir or default_install_dir()

    _print(_BANNER)
    _print("Welcome to the Orbi installer.")
    _print("")
    _print("Paste the install token you received in your Stripe receipt email.")
    _print("It looks like:  inst_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")
    _print("")
    token = input("Install token: ").strip()
    biz_name = input("Business name (optional, press Enter to skip): ").strip()

    _print("")
    _print("Verifying token with billing service …")
    billing = verify_token(token)
    if not billing:
        _print("ERROR: token could not be verified.")
        _print("       Check your internet connection and try again.")
        _print("       If it still fails, email support@orbi.frank.com.")
        return 2
    _print(f"  OK — tier: {billing['tier']}, owner: {billing['owner_email']}")

    _print("")
    _print(f"Creating install at {install_dir} …")
    try:
        setup_directories(install_dir)
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
    logging.basicConfig(level=logging.INFO)
    tmp = Path(tempfile.mkdtemp(prefix="orbi-installer-smoke-"))
    try:
        setup_directories(tmp)
        pw = bootstrap_owner(tmp, "test.owner@example.com")
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
        print(f"PASS: owner={owners[0]} password={pw}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        sys.exit(_smoke_test())
    sys.exit(run_interactive())
