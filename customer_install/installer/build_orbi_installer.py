#!/usr/bin/env python3
"""
build_orbi_installer — builds the customer-side Orbi installer artifacts
for Windows, macOS, and Linux.

What this script produces:

    dist/windows/orbi-installer.exe   (PyInstaller --onefile, built on a
                                       Windows runner — GitHub Actions
                                       is the easy path)
    dist/mac/orbi-installer.pkg       (pkgbuild wrapping the PyInstaller
                                       bundle + the LaunchDaemon plist)
    dist/linux/orbi-installer.sh      (self-extracting shar archive that
                                       drops the bundle in /tmp and runs
                                       it with sudo)

Each artifact, when a customer runs it:
   a) Asks for the install token (the one Stripe gave them in email)
   b) Calls billing.orbi.frank.com/api/verify/<token> over HTTPS
   c) On success, creates /opt/orbi (or C:\\Program Files\\Orbi)
   d) Writes config.json from the template, pre-populated with api_key
      and owner_email from the billing response
   e) Calls users.add_user(...) to create the bootstrap owner user with
      a random 12-char temporary password (displayed once on screen)
   f) Registers the OS service (systemd / launchd / Windows service)
   g) Opens the customer's browser at http://localhost:5050/owner/login
      with the temp password pre-filled
   h) Exits.

This script does NOT cross-compile — each platform needs its own runner.
The intent is that CI matrix-builds all three. Locally you can build the
one for the host platform.

Usage:
    python build_orbi_installer.py [--target windows|mac|linux|all]
                                   [--clean] [--no-strip]
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("orbi.installer.build")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
CUSTOMER_INSTALL = HERE.parent          # /home/frank/orbi_web/customer_install
PROJECT_ROOT = CUSTOMER_INSTALL.parent  # /home/frank/orbi_web
BUILD_DIR = HERE / "build"
DIST_DIR = HERE / "dist"
RUNTIME_ENTRY = HERE / "install_runtime.py"

# Files that must be bundled with the installer so install_runtime can
# read them at customer-install-time
DATA_FILES = [
    CUSTOMER_INSTALL / "config.json.template",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> int:
    """Run a subprocess; bubble up its stdout/stderr."""
    log.info("$ %s", " ".join(str(c) for c in cmd))
    rc = subprocess.run(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
    ).returncode
    if check and rc != 0:
        raise RuntimeError(f"command failed (rc={rc}): {cmd[0]}")
    return rc


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        pass
    log.info("PyInstaller not found — installing into current interpreter")
    _run([sys.executable, "-m", "pip", "install", "--quiet", "pyinstaller"])


def _clean() -> None:
    for d in (BUILD_DIR, DIST_DIR):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            log.info("removed %s", d)


def _pyinstaller_cmd(out_subdir: Path, name: str = "orbi-installer") -> list:
    """Common PyInstaller args used for all three targets."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", name,
        "--distpath", str(out_subdir),
        "--workpath", str(BUILD_DIR / name),
        "--specpath", str(BUILD_DIR),
        "--clean",
        "--noconfirm",
    ]
    # Bundle the config template + the users.py module so the runtime can
    # use them on the customer machine without any source-tree dependency.
    sep = ";" if platform.system().lower() == "windows" else ":"
    for f in DATA_FILES:
        cmd += ["--add-data", f"{f}{sep}."]
    # Also include users.py + auth.py from the customer_install dir as
    # hidden modules so install_runtime.bootstrap_owner can import them
    cmd += ["--add-data",
            f"{CUSTOMER_INSTALL / 'users.py'}{sep}."]
    cmd += ["--add-data",
            f"{CUSTOMER_INSTALL / 'auth.py'}{sep}."]
    cmd.append(str(RUNTIME_ENTRY))
    return cmd


# ---------------------------------------------------------------------------
# Per-platform build steps
# ---------------------------------------------------------------------------

def build_windows() -> Path:
    """Produces dist/windows/orbi-installer.exe. Must run on Windows."""
    out = DIST_DIR / "windows"
    out.mkdir(parents=True, exist_ok=True)
    _ensure_pyinstaller()
    cmd = _pyinstaller_cmd(out, name="orbi-installer")
    _run(cmd)
    exe = out / "orbi-installer.exe"
    if not exe.exists():
        # PyInstaller may have nested it
        candidates = list(out.rglob("orbi-installer.exe"))
        if candidates:
            shutil.move(str(candidates[0]), str(exe))
    if not exe.exists():
        raise RuntimeError("orbi-installer.exe not produced")
    log.info("built %s (%.1f MB)", exe, exe.stat().st_size / 1e6)
    return exe


def build_mac() -> Path:
    """Produces dist/mac/orbi-installer.pkg.
    Step 1: PyInstaller --onefile → orbi-installer (no extension)
    Step 2: wrap with pkgbuild → orbi-installer.pkg
    """
    out = DIST_DIR / "mac"
    out.mkdir(parents=True, exist_ok=True)
    _ensure_pyinstaller()
    # Stage 1: PyInstaller binary
    _run(_pyinstaller_cmd(out, name="orbi-installer"))
    binary = out / "orbi-installer"
    if not binary.exists():
        raise RuntimeError("PyInstaller did not produce orbi-installer")

    # Stage 2: lay out a pkg payload root
    payload = BUILD_DIR / "mac-payload"
    if payload.exists():
        shutil.rmtree(payload)
    (payload / "usr" / "local" / "bin").mkdir(parents=True)
    shutil.copy2(binary, payload / "usr" / "local" / "bin" / "orbi-installer")
    os.chmod(payload / "usr" / "local" / "bin" / "orbi-installer", 0o755)

    pkg_path = out / "orbi-installer.pkg"
    pkgbuild = shutil.which("pkgbuild")
    if not pkgbuild:
        log.warning(
            "pkgbuild not on PATH — skipping .pkg wrap. The bare binary at "
            "%s is still usable.", binary
        )
        return binary

    # postinstall script: runs the installer GUI on first launch.
    scripts_dir = BUILD_DIR / "mac-scripts"
    if scripts_dir.exists():
        shutil.rmtree(scripts_dir)
    scripts_dir.mkdir(parents=True)
    postinstall = scripts_dir / "postinstall"
    postinstall.write_text(
        "#!/bin/bash\n"
        "# Run the Orbi installer right after the pkg drops it on disk.\n"
        "/usr/local/bin/orbi-installer\n"
        "exit 0\n"
    )
    os.chmod(postinstall, 0o755)

    _run([
        pkgbuild,
        "--identifier", "com.orbi.customer.installer",
        "--version", "0.1.0",
        "--root", str(payload),
        "--scripts", str(scripts_dir),
        "--install-location", "/",
        str(pkg_path),
    ])
    log.info("built %s (%.1f MB)", pkg_path, pkg_path.stat().st_size / 1e6)
    return pkg_path


def build_linux() -> Path:
    """Produces dist/linux/orbi-installer.sh — a self-extracting shar archive
    that contains the PyInstaller --onefile binary plus a small bootstrap."""
    out = DIST_DIR / "linux"
    out.mkdir(parents=True, exist_ok=True)
    _ensure_pyinstaller()
    _run(_pyinstaller_cmd(out, name="orbi-installer"))
    binary = out / "orbi-installer"
    if not binary.exists():
        raise RuntimeError("PyInstaller did not produce orbi-installer")
    os.chmod(binary, 0o755)

    sh_path = out / "orbi-installer.sh"
    import base64
    payload_b64 = base64.b64encode(binary.read_bytes()).decode("ascii")
    # Wrap into a small self-extracting shell script
    sh = f"""#!/usr/bin/env bash
# Orbi self-extracting installer (Linux x86_64).
# Decodes the embedded binary, drops it in /tmp, and runs it with sudo.
set -e
if [[ $EUID -ne 0 ]]; then
  echo "This installer must be run as root. Re-running with sudo …"
  exec sudo bash "$0" "$@"
fi
TMPDIR=$(mktemp -d /tmp/orbi-installer.XXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT
echo "Extracting installer …"
base64 -d > "$TMPDIR/orbi-installer" <<'__ORBI_BLOB__'
{payload_b64}
__ORBI_BLOB__
chmod +x "$TMPDIR/orbi-installer"
"$TMPDIR/orbi-installer" "$@"
"""
    sh_path.write_text(sh)
    os.chmod(sh_path, 0o755)
    log.info("built %s (%.1f MB)", sh_path, sh_path.stat().st_size / 1e6)
    return sh_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Build Orbi installer artifacts.")
    parser.add_argument(
        "--target", choices=["windows", "mac", "linux", "all"],
        default="host", help="which OS to build for (default: host)",
    )
    parser.add_argument("--clean", action="store_true",
                        help="wipe build/ and dist/ before building")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.clean:
        _clean()
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    host = platform.system().lower()
    targets: list[str]
    if args.target == "host":
        targets = ["windows" if host == "windows" else
                   "mac" if host == "darwin" else "linux"]
    elif args.target == "all":
        targets = ["windows", "mac", "linux"]
    else:
        targets = [args.target]

    built = []
    for t in targets:
        log.info("=" * 60)
        log.info("Building target: %s", t)
        log.info("=" * 60)
        if t == "windows":
            if host != "windows":
                log.warning(
                    "Skipping windows target — host is %s. "
                    "Run this script on a Windows machine or in a "
                    "Windows GitHub Actions runner.", host
                )
                continue
            built.append(build_windows())
        elif t == "mac":
            if host != "darwin":
                log.warning(
                    "Skipping mac target — host is %s. Run on macOS.", host
                )
                continue
            built.append(build_mac())
        elif t == "linux":
            if host != "linux":
                log.warning(
                    "Skipping linux target — host is %s. Run on Linux.", host
                )
                continue
            built.append(build_linux())

    log.info("")
    log.info("Done. Built artifacts:")
    for p in built:
        log.info("  %s", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
