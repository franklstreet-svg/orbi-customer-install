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
   b) Calls brain.twickell.com/api/verify/<token> over HTTPS
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

# Bundled runtime — Windows-only. We ship an embedded Python distribution
# inside the installer .exe so customers DON'T have to install Python
# themselves. install_runtime.py extracts these into the customer's install
# dir at install time and uses that Python for the venv.
_BUNDLED_RUNTIME_DIR = HERE / "_bundled_runtime"
_EMBED_PYTHON_ZIP = _BUNDLED_RUNTIME_DIR / "python-3.13.1-embed-amd64.zip"
_GET_PIP = _BUNDLED_RUNTIME_DIR / "get-pip.py"
_EMBED_PYTHON_URL = (
    "https://www.python.org/ftp/python/3.13.1/python-3.13.1-embed-amd64.zip"
)
_GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def fetch_windows_embed_python() -> list:
    """Ensure the Windows embeddable Python distribution + get-pip.py are
    cached locally for the next build. Returns the list of paths to
    include in the PyInstaller bundle (empty list if download fails so
    a non-Windows-targeted build doesn't break)."""
    _BUNDLED_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    import urllib.request
    for url, dest in [(_EMBED_PYTHON_URL, _EMBED_PYTHON_ZIP),
                       (_GET_PIP_URL, _GET_PIP)]:
        if dest.exists() and dest.stat().st_size > 1024:
            continue
        log.info("downloading %s → %s", url, dest)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "orbi-installer-build/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest.write_bytes(resp.read())
            log.info("  → %.1f MB", dest.stat().st_size / 1e6)
        except Exception as e:
            log.warning("could not fetch %s: %s", url, e)
            return []
    return [_EMBED_PYTHON_ZIP, _GET_PIP]

# Where the app-source tarball gets written before PyInstaller embeds it.
# install_runtime.py extracts this tarball into the customer's install dir
# at install time — that's how orbi.py, modules/, owner_dashboard/, etc.
# actually land on the customer's machine.
APP_TARBALL = BUILD_DIR / "orbi_app.tar.gz"

# Exclusions when building the app tarball. Everything else in
# customer_install/ ships to the customer.
_APP_TARBALL_EXCLUDES = {
    "data",            # per-install customer data, never ship
    "backups",         # local backups, never ship
    "snapshots",       # watchdog snapshots, never ship
    "_archive",        # deactivated-user archives, never ship
    "__pycache__",     # bytecode noise
    "installer",       # this very directory — recursive nope
    "_bin_cache",      # build cache
    "build",           # build cache
    "dist",            # build output
    "tests",           # test fixtures we don't need to ship
    ".git",            # never
    ".pytest_cache",
    ".mypy_cache",
}
_APP_TARBALL_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp"}


def build_app_tarball() -> Path:
    """Snapshot the customer_install/ tree (minus per-install state) into
    a .tar.gz that install_runtime extracts on the customer's machine.

    Also picks up extra source dirs that live at the project root but are
    used by the customer install — owner_dashboard/ is the main one. Its
    customer_install/ entry was historically a local symlink that's
    untracked in git, so on a CI fresh checkout it simply doesn't exist
    and the dashboard would be missing from the bundled installer.

    Idempotent: re-runs always regenerate the tarball so source edits show
    up in the next build.
    """
    import tarfile
    APP_TARBALL.parent.mkdir(parents=True, exist_ok=True)
    if APP_TARBALL.exists():
        APP_TARBALL.unlink()
    log.info("packing app source → %s", APP_TARBALL)
    total = 0
    # Extra source roots → arcname-prefix. These are project-root dirs that
    # the customer install needs but aren't located inside customer_install/
    # in git. Add them to the tarball as if they lived inside
    # customer_install/ so install_runtime extracts them to the same place.
    extra_roots: list[tuple[Path, str]] = []
    project_owner_dash = PROJECT_ROOT / "owner_dashboard"
    customer_owner_dash = CUSTOMER_INSTALL / "owner_dashboard"
    # Only add the project-root version if the customer_install copy isn't
    # a real directory with content (which it would be after `git
    # checkout` on CI, since the symlink isn't tracked).
    if project_owner_dash.is_dir() and not customer_owner_dash.is_dir():
        extra_roots.append((project_owner_dash, "owner_dashboard"))
    # If customer_install/owner_dashboard IS present (e.g. local dev where
    # the symlink resolves), os.walk(followlinks=True) below picks it up
    # natively — no extra_roots entry needed.
    def _resolve_stub(abs_path: Path) -> Path:
        """If `abs_path` is a 'symlink stub' — a small text file whose
        content is a relative path to another file (this is what GitHub's
        ZIP export does to symlinks) — resolve it and return the real
        file path. Otherwise return abs_path unchanged.

        The stub bug bit Kathy + Frank on 2026-06-07: static/dashboard.js
        was a symlink in the repo, but the GitHub ZIP download turned it
        into a 34-byte text file containing `../../owner_dashboard/...`.
        The build packed the stub. Browsers then tried to parse the path
        as JavaScript and barfed `Unexpected token '.'`.
        """
        try:
            size = abs_path.stat().st_size
        except OSError:
            return abs_path
        if size > 500:
            return abs_path  # Real file, not a stub
        try:
            body = abs_path.read_bytes()
        except OSError:
            return abs_path
        # Stubs are a SINGLE line of ASCII with no embedded NULs that names
        # a real file when resolved relative to the stub's own folder.
        if b"\0" in body or b"\n" in body[:-1]:
            return abs_path
        try:
            candidate_text = body.decode("ascii").strip()
        except UnicodeDecodeError:
            return abs_path
        if not candidate_text or not any(candidate_text.endswith(ext)
                for ext in (".js", ".css", ".html", ".png", ".svg", ".json")):
            return abs_path
        candidate = (abs_path.parent / candidate_text).resolve()
        return candidate if candidate.is_file() else abs_path

    def _pack_tree(tar, base: Path, arcname_prefix: str = "") -> int:
        """Walk `base`, packing files into `tar` rooted at arcname_prefix.
        Returns count of files packed. Symlinks are dereferenced so the
        archive only has regular file entries — required because
        tarfile.data_filter (default in Python 3.12+) rejects symlinks
        that point outside the extraction destination.

        Also handles 'symlink stubs' — text files that contain a relative
        path. GitHub's ZIP exports replace symlinks with these. Without
        this resolver, the build would pack a 34-byte stub for files like
        static/dashboard.js, browsers would fail to parse it, and the
        whole dashboard would silently break."""
        count = 0
        for root, dirs, files in os.walk(base, followlinks=True):
            dirs[:] = [d for d in dirs if d not in _APP_TARBALL_EXCLUDES
                        and not d.startswith(".")]
            for f in files:
                if any(f.endswith(s) for s in _APP_TARBALL_EXCLUDE_SUFFIXES):
                    continue
                if f.startswith(".") and f not in {".gitkeep"}:
                    continue
                abs_path = Path(root) / f
                rel_inside = abs_path.relative_to(base)
                arc = f"{arcname_prefix}/{rel_inside}" if arcname_prefix \
                    else str(rel_inside)
                real = _resolve_stub(abs_path.resolve())
                info = tar.gettarinfo(name=str(real), arcname=arc)
                if info is None:
                    continue
                with open(real, "rb") as fp:
                    tar.addfile(info, fp)
                count += 1
        return count

    with tarfile.open(APP_TARBALL, "w:gz", compresslevel=6) as tar:
        # 1) Pack customer_install/ as the main payload
        total += _pack_tree(tar, CUSTOMER_INSTALL, arcname_prefix="")
        # 2) Pack each extra source root under its configured arcname
        for src, arc in extra_roots:
            n = _pack_tree(tar, src, arcname_prefix=arc)
            log.info("packed extra root %s (%d files) → %s/", src, n, arc)
            total += n
    size_mb = APP_TARBALL.stat().st_size / 1_000_000
    log.info("packed %d files into %s (%.2f MB)", total, APP_TARBALL.name, size_mb)
    return APP_TARBALL


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
                     voice_models: list[Path] | None = None,
                     embed_python: list[Path] | None = None) -> list:
    """Common PyInstaller args used for all three targets.
    `bundled_bins` = ffmpeg / cloudflared / piper binaries → install_dir/bin/
    `voice_models` = Piper .onnx / .onnx.json files → install_dir/tts_models/
    `embed_python` = Windows embeddable Python zip + get-pip.py →
        bundle/python_embed/ (extracted at install time so the customer
        doesn't need to install Python themselves)
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
    # Include the app-source tarball so install_runtime.extract_app_source
    # can drop orbi.py + modules/ + dashboards into the customer's
    # install dir at install time. Without this the systemd service
    # would point at a non-existent /opt/orbi/orbi.py.
    if APP_TARBALL.exists():
        cmd += ["--add-data", f"{APP_TARBALL}{sep}."]
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
    # Ship the Windows embeddable Python distribution + get-pip.py into
    # python_embed/ so install_runtime can extract them on the customer's
    # machine. Customers no longer need to install Python first.
    for embed_file in embed_python or []:
        if embed_file and embed_file.exists():
            cmd += ["--add-data", f"{embed_file}{sep}python_embed"]
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
    build_app_tarball()
    bins = fetch_platform_binaries("windows")
    models = fetch_piper_voice_models()
    embed_python = fetch_windows_embed_python()
    cmd = _pyinstaller_cmd(out, name="orbi-installer", bundled_bins=bins,
                           voice_models=models, embed_python=embed_python)
    # Embed a Windows manifest that requests administrator elevation. The
    # installer writes to C:\Program Files\Orbi (UAC-protected), so the
    # customer must double-click and click "Yes" on the UAC prompt — no
    # need to remember "right-click → Run as administrator". install_runtime
    # also has a runtime self-elevate fallback for any path that misses this.
    cmd.insert(cmd.index("--onefile") + 1, "--uac-admin")
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
    build_app_tarball()
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
    build_app_tarball()
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
