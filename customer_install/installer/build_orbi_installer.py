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


def _pyinstaller_cmd(out_subdir: Path, name: str = "orbi-installer",
                     bundled_bins: list[Path] | None = None,
                     voice_models: list[Path] | None = None) -> list:
    """Common PyInstaller args used for all three targets.
    `bundled_bins` = ffmpeg / cloudflared / piper binaries → install_dir/bin/
    `voice_models` = Piper .onnx / .onnx.json files → install_dir/tts_models/
    """
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
    # Ship the helper binaries (ffmpeg + cloudflared + piper) so the
    # customer gets a self-contained install with no internet needed at
    # install time.
    for binpath in bundled_bins or []:
        if binpath and binpath.exists():
            cmd += ["--add-binary", f"{binpath}{sep}bin"]
    # Ship the Piper voice model files into a separate tts_models/ dir
    # so install_runtime can extract them to install_dir/tts_models/.
    for model in voice_models or []:
        if model and model.exists():
            cmd += ["--add-data", f"{model}{sep}tts_models"]
    cmd.append(str(RUNTIME_ENTRY))
    return cmd


# ---------------------------------------------------------------------------
# Bundle helper binaries — ffmpeg + cloudflared, per platform
# ---------------------------------------------------------------------------

BIN_CACHE = HERE / "_bin_cache"

# Static binary sources we trust:
#   ffmpeg     — johnvansickle (Linux), evermeet.cx (Mac), gyan.dev (Windows)
#   cloudflared — Cloudflare's own GitHub releases (every platform)
_BINARY_SOURCES = {
    "linux": {
        "ffmpeg":      "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
        "cloudflared": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
        "piper":       "https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz",
    },
    "darwin": {
        "ffmpeg":      "https://evermeet.cx/ffmpeg/getrelease/zip",
        "cloudflared": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz",
        "piper":       "https://github.com/rhasspy/piper/releases/latest/download/piper_macos_x64.tar.gz",
    },
    "windows": {
        "ffmpeg":      "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "cloudflared": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
        "piper":       "https://github.com/rhasspy/piper/releases/latest/download/piper_windows_amd64.zip",
    },
}

# Piper voice models — small dictionary of ONNX + JSON files to bundle.
# Adding more voices = bigger installer; default is one good US-English
# voice that sounds close to Twilio's Polly.Joanna (the phone receptionist
# voice) so phone + dashboard feel consistent. Customer can drop more
# .onnx files into install_dir/tts_models/ to add voices later.
PIPER_VOICE_MODELS = {
    "en_US-amy-medium.onnx":
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx",
    "en_US-amy-medium.onnx.json":
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json",
}


def _download(url: str, dest: Path) -> Path:
    """Download a URL to dest with a friendly progress log. Idempotent —
    if dest already exists, no re-download."""
    if dest.exists() and dest.stat().st_size > 1024:
        log.info("cached: %s (%.1f MB)", dest.name, dest.stat().st_size / 1_000_000)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s → %s", url, dest)
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Orbi-Build/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as fp:
        shutil.copyfileobj(resp, fp)
    log.info("got %s (%.1f MB)", dest.name, dest.stat().st_size / 1_000_000)
    return dest


def _extract_binary(archive: Path, target_name: str, out_dir: Path) -> Path | None:
    """Pull a single named executable out of a downloaded archive
    (tar.xz / zip / tgz). Returns the extracted path or None."""
    out_dir.mkdir(parents=True, exist_ok=True)
    name_lower = target_name.lower()
    extracted: Path | None = None
    suffix = archive.suffix.lower()
    if suffix in (".xz", ".tar", ".gz", ".tgz"):
        import tarfile
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                base = Path(member.name).name.lower()
                if base == name_lower or base == name_lower + ".exe":
                    tar.extract(member, out_dir)
                    extracted = out_dir / member.name
                    break
    elif suffix == ".zip":
        import zipfile
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                base = Path(member).name.lower()
                if base == name_lower + ".exe" or base == name_lower:
                    z.extract(member, out_dir)
                    extracted = out_dir / member
                    break
    elif archive.is_file():
        # Already a bare executable — copy + rename so the bundled file
        # has the canonical name.
        out_name = target_name + (".exe" if archive.name.endswith(".exe") else "")
        dst = out_dir / out_name
        shutil.copy2(archive, dst)
        extracted = dst
    if extracted and extracted.exists():
        try:
            extracted.chmod(0o755)
        except OSError:
            pass
        # Normalize the filename to plain "ffmpeg" / "cloudflared" (+ .exe on Windows)
        norm = target_name + (".exe" if extracted.suffix.lower() == ".exe" else "")
        normalized = out_dir / norm
        if extracted != normalized:
            shutil.move(str(extracted), str(normalized))
            extracted = normalized
    return extracted


def fetch_platform_binaries(target: str) -> list[Path]:
    """Download ffmpeg + cloudflared + piper for `target` (linux/darwin/
    windows) and return the list of paths PyInstaller should --add-binary
    in."""
    if target not in _BINARY_SOURCES:
        log.warning("no binary sources defined for %s — skipping bundling", target)
        return []
    target_cache = BIN_CACHE / target
    target_cache.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for tool, url in _BINARY_SOURCES[target].items():
        try:
            # Download to a deterministic filename based on the URL extension
            from urllib.parse import urlparse
            url_path = urlparse(url).path
            ext = Path(url_path).suffix or ".bin"
            archive = target_cache / f"_dl_{tool}{ext}"
            _download(url, archive)
            binpath = _extract_binary(archive, tool, target_cache)
            if binpath:
                out.append(binpath)
                log.info("✓ bundling %s for %s: %s", tool, target, binpath.name)
            else:
                log.warning("could not extract %s from %s", tool, archive)
        except Exception as e:
            log.warning("failed to fetch %s for %s: %s — installer will work, "
                        "but the customer's machine needs %s installed manually",
                        tool, target, e, tool)
    return out


def fetch_piper_voice_models() -> list[Path]:
    """Download Piper voice model files (ONNX + matching JSON metadata).
    Same files for every platform — Piper is portable. Returns a list of
    paths PyInstaller should --add-data in (target dir = tts_models/)."""
    out_dir = BIN_CACHE / "voice_models"
    out_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for fname, url in PIPER_VOICE_MODELS.items():
        try:
            dst = out_dir / fname
            _download(url, dst)
            if dst.exists() and dst.stat().st_size > 1024:
                out.append(dst)
                log.info("✓ bundling voice model: %s (%.1f MB)",
                         fname, dst.stat().st_size / 1e6)
        except Exception as e:
            log.warning("failed to fetch voice model %s: %s — TTS will fall "
                        "back to edge_tts (still works, less commercially clean)",
                        fname, e)
    return out


# ---------------------------------------------------------------------------
# Per-platform build steps
# ---------------------------------------------------------------------------

def build_windows() -> Path:
    """Produces dist/windows/orbi-installer.exe. Must run on Windows."""
    out = DIST_DIR / "windows"
    out.mkdir(parents=True, exist_ok=True)
    _ensure_pyinstaller()
    bins = fetch_platform_binaries("windows")
    models = fetch_piper_voice_models()
    cmd = _pyinstaller_cmd(out, name="orbi-installer", bundled_bins=bins,
                           voice_models=models)
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
    bins = fetch_platform_binaries("darwin")
    models = fetch_piper_voice_models()
    _run(_pyinstaller_cmd(out, name="orbi-installer", bundled_bins=bins,
                          voice_models=models))
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
    bins = fetch_platform_binaries("linux")
    models = fetch_piper_voice_models()
    _run(_pyinstaller_cmd(out, name="orbi-installer", bundled_bins=bins,
                          voice_models=models))
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
