"""
Cross-platform service manager for Orbi.

Same API, different backend per OS:
  - Linux  → systemd
  - Windows → Windows service (via pywin32 or NSSM)
  - macOS  → launchd

Used by install scripts to create + start + stop the Orbi background services
without each platform's install.sh needing to know the details.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"
IS_MAC     = platform.system() == "Darwin"
IS_LINUX   = platform.system() == "Linux"


class ServiceManager:
    """Abstract service manager. Subclasses implement per-OS."""

    def install(self, name: str, command: list[str], working_dir: Path,
                env: dict | None = None, description: str = "") -> bool:
        raise NotImplementedError

    def start(self, name: str) -> bool:
        raise NotImplementedError

    def stop(self, name: str) -> bool:
        raise NotImplementedError

    def restart(self, name: str) -> bool:
        raise NotImplementedError

    def status(self, name: str) -> str:
        """Returns 'running', 'stopped', or 'unknown'."""
        raise NotImplementedError

    def uninstall(self, name: str) -> bool:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Linux (systemd)
# ---------------------------------------------------------------------------

class SystemdServiceManager(ServiceManager):
    UNIT_DIR = Path("/etc/systemd/system")

    def install(self, name, command, working_dir, env=None, description=""):
        unit = self.UNIT_DIR / f"{name}.service"
        env_lines = "\n".join(f"Environment={k}={v}" for k, v in (env or {}).items())
        exec_start = " ".join(self._shquote(c) for c in command)
        unit_content = f"""[Unit]
Description={description or name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
{env_lines}

[Install]
WantedBy=multi-user.target
"""
        unit.write_text(unit_content)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", name], check=True)
        return True

    def start(self, name): return self._run(["systemctl", "start", name])
    def stop(self, name): return self._run(["systemctl", "stop", name])
    def restart(self, name): return self._run(["systemctl", "restart", name])
    def status(self, name):
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True)
        s = r.stdout.strip()
        if s == "active": return "running"
        if s == "inactive" or s == "failed": return "stopped"
        return "unknown"

    def uninstall(self, name):
        self.stop(name)
        subprocess.run(["systemctl", "disable", name], capture_output=True)
        unit = self.UNIT_DIR / f"{name}.service"
        if unit.exists():
            unit.unlink()
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        return True

    @staticmethod
    def _shquote(s):
        if " " in s or '"' in s:
            return '"' + s.replace('"', '\\"') + '"'
        return s

    @staticmethod
    def _run(cmd):
        return subprocess.run(cmd, capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# macOS (launchd)
# ---------------------------------------------------------------------------

class LaunchdServiceManager(ServiceManager):
    @staticmethod
    def _plist_path(name: str) -> Path:
        # Per-user launch agent — runs when user logs in, no admin needed
        return Path.home() / "Library" / "LaunchAgents" / f"com.orbi.{name}.plist"

    def install(self, name, command, working_dir, env=None, description=""):
        plist = self._plist_path(name)
        plist.parent.mkdir(parents=True, exist_ok=True)
        env_xml = ""
        if env:
            items = "".join(f"<key>{k}</key><string>{v}</string>" for k, v in env.items())
            env_xml = f"<key>EnvironmentVariables</key><dict>{items}</dict>"
        args_xml = "".join(f"<string>{c}</string>" for c in command)
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.orbi.{name}</string>
  <key>ProgramArguments</key><array>{args_xml}</array>
  <key>WorkingDirectory</key><string>{working_dir}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{working_dir}/{name}.log</string>
  <key>StandardErrorPath</key><string>{working_dir}/{name}.log</string>
  {env_xml}
</dict></plist>
"""
        plist.write_text(plist_content)
        # Load (this also starts it because RunAtLoad=true)
        subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True)
        return True

    def start(self, name):
        plist = self._plist_path(name)
        return subprocess.run(["launchctl", "load", "-w", str(plist)],
                              capture_output=True).returncode == 0

    def stop(self, name):
        plist = self._plist_path(name)
        return subprocess.run(["launchctl", "unload", str(plist)],
                              capture_output=True).returncode == 0

    def restart(self, name):
        self.stop(name)
        return self.start(name)

    def status(self, name):
        r = subprocess.run(["launchctl", "list", f"com.orbi.{name}"],
                           capture_output=True, text=True)
        if r.returncode == 0 and "PID" in r.stdout:
            return "running"
        return "stopped"

    def uninstall(self, name):
        self.stop(name)
        plist = self._plist_path(name)
        if plist.exists():
            plist.unlink()
        return True


# ---------------------------------------------------------------------------
# Windows (Windows service via NSSM — Non-Sucking Service Manager)
# ---------------------------------------------------------------------------
# NSSM is bundled with the installer at C:\Program Files\Orbi\nssm.exe
# It's a tiny (1MB) tool that wraps any executable as a real Windows service.

class WindowsServiceManager(ServiceManager):
    def __init__(self, nssm_path: str | None = None):
        self.nssm = nssm_path or self._find_nssm()

    @staticmethod
    def _find_nssm() -> str:
        # Check standard locations
        candidates = [
            r"C:\Program Files\Orbi\nssm.exe",
            r"C:\Program Files (x86)\Orbi\nssm.exe",
            os.path.join(os.environ.get("ORBI_HOME", ""), "nssm.exe"),
            "nssm.exe",  # on PATH
        ]
        for p in candidates:
            if p and Path(p).exists():
                return p
        return "nssm.exe"

    def install(self, name, command, working_dir, env=None, description=""):
        if not command:
            return False
        exe = command[0]
        args = " ".join(f'"{a}"' if " " in a else a for a in command[1:])
        # Install
        r = subprocess.run([self.nssm, "install", name, exe, args],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False
        # Configure
        subprocess.run([self.nssm, "set", name, "AppDirectory", str(working_dir)],
                       capture_output=True)
        subprocess.run([self.nssm, "set", name, "Description", description or name],
                       capture_output=True)
        subprocess.run([self.nssm, "set", name, "Start", "SERVICE_AUTO_START"],
                       capture_output=True)
        subprocess.run([self.nssm, "set", name, "AppStdout", str(working_dir / f"{name}.log")],
                       capture_output=True)
        subprocess.run([self.nssm, "set", name, "AppStderr", str(working_dir / f"{name}.log")],
                       capture_output=True)
        if env:
            env_str = "\n".join(f"{k}={v}" for k, v in env.items())
            subprocess.run([self.nssm, "set", name, "AppEnvironmentExtra", env_str],
                           capture_output=True)
        return True

    def start(self, name):
        return subprocess.run([self.nssm, "start", name],
                              capture_output=True).returncode == 0

    def stop(self, name):
        return subprocess.run([self.nssm, "stop", name],
                              capture_output=True).returncode == 0

    def restart(self, name):
        return subprocess.run([self.nssm, "restart", name],
                              capture_output=True).returncode == 0

    def status(self, name):
        r = subprocess.run([self.nssm, "status", name],
                           capture_output=True, text=True)
        s = r.stdout.strip().upper()
        if "SERVICE_RUNNING" in s: return "running"
        if "SERVICE_STOPPED" in s: return "stopped"
        return "unknown"

    def uninstall(self, name):
        self.stop(name)
        return subprocess.run([self.nssm, "remove", name, "confirm"],
                              capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_service_manager() -> ServiceManager:
    if IS_LINUX:   return SystemdServiceManager()
    if IS_MAC:     return LaunchdServiceManager()
    if IS_WINDOWS: return WindowsServiceManager()
    raise RuntimeError(f"Unsupported platform: {platform.system()}")
