"""
cloudflare_setup — programmatic Cloudflare Tunnel + DNS setup for the
Orbi onboarding wizard.

This module is the GUI-driven counterpart to tunnel/setup_cloudflared.sh.
The shell script is still the manual fallback (an owner can SSH in and
run it). This module does the same job, but from the owner's browser:

    1. Owner pastes a Cloudflare API token (scope: Tunnel:Edit + DNS:Edit
       on their zone) and the account_id + zone_id from the Cloudflare
       dashboard.
    2. We POST /accounts/{account_id}/cfd_tunnel to create a named
       tunnel and receive back the tunnel_id + tunnel_token (an opaque
       blob that uniquely identifies + authenticates this tunnel).
    3. We POST /zones/{zone_id}/dns_records to add a proxied CNAME
       pointing <subdomain>.<owner-domain> → <tunnel_id>.cfargotunnel.com.
    4. We install_cloudflared_service(): write ~/.cloudflared/config.yml
       and a systemd unit (Linux) or launchd plist (Mac) that runs
       `cloudflared tunnel run --token <tunnel_token>` on boot.
    5. We verify_tunnel_health() by polling https://<hostname>/health.

All HTTP uses stdlib urllib (no extra deps).

SECURITY NOTE on tunnel-token storage
-------------------------------------
The Cloudflare tunnel_token grants the holder the ability to BE the
tunnel — i.e. terminate traffic for that hostname. If leaked it's
roughly equivalent to leaking a VPN key for that single subdomain.
Mitigations baked into this module:

  * The token is written to ~/.cloudflared/config.yml with mode 0o600
    (owner-readable only) on POSIX.
  * The systemd unit / launchd plist reference the token by FILE PATH
    in the service definition, not as plaintext args, so it doesn't
    show up in `ps aux`.
  * Logs redact the token via redact_token().
  * The token is NEVER bounced back to the dashboard once installed
    — current_tunnel_config() returns a redacted version only.
  * If the token leaks, the owner can rotate it in the Cloudflare
    dashboard ("Refresh token") and re-run install_service.

Risks we do NOT yet mitigate:
  * The customer's machine compromise = token compromise. We trust the
    OS file-permission boundary. Adding OS keyring storage is a future
    upgrade.
  * Backups of the home dir (e.g. Time Machine / Windows File History)
    will contain the token. We rely on the owner's general computer
    hygiene there — same threat surface as their browser password DB.

Routes the orchestrator (orbi.py) should wire — do NOT add them here:
    POST /api/owner/cloudflare/create_tunnel
        body: {api_token, account_id, zone_id, tunnel_name, hostname}
        → create_tunnel(...) + add_dns_route(...)
    POST /api/owner/cloudflare/install_service
        body: {tunnel_token, hostname}
        → install_cloudflared_service(...)
    GET  /api/owner/cloudflare/health?hostname=...
        → verify_tunnel_health(...)
    GET  /api/owner/cloudflare/status
        → current_tunnel_config(CONFIG)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("orbi.cloudflare_setup")

CF_API_BASE = "https://api.cloudflare.com/client/v4"

_STATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def redact_token(value: str | None) -> str:
    """Trim a Cloudflare API token / tunnel token to first-4 + last-3."""
    if not value:
        return ""
    s = str(value)
    if len(s) < 8:
        return "[redacted]"
    return f"{s[:4]}…[redacted]…{s[-3:]}"


def _err(msg: str, exc: Exception | None = None) -> dict:
    if exc is not None:
        log.exception("cloudflare_setup: %s", msg)
    else:
        log.error("cloudflare_setup: %s", msg)
    return {"ok": False, "error": msg}


def _atomic_write(path: Path, data: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)
    if mode is not None and os.name != "nt":
        try:
            os.chmod(path, mode)
        except OSError as exc:
            log.warning("cloudflare_setup: chmod %o on %s failed: %s",
                        mode, path, exc)


def _cf_request(method: str,
                path: str,
                api_token: str,
                body: dict | None = None,
                timeout: int = 20) -> dict:
    """Wrapper around urllib that handles JSON in/out and bearer auth.

    Returns the parsed `result` field on success. Raises RuntimeError
    with a friendly message on failure."""
    url = CF_API_BASE.rstrip("/") + "/" + path.lstrip("/")
    payload = None
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
        "User-Agent": "orbi-installer/1.0",
    }
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=payload, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        msg = _extract_cf_error(body_text) or exc.reason or "HTTP error"
        raise RuntimeError(
            f"Cloudflare API {method} {path} failed ({exc.code}): {msg}"
        )
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach Cloudflare API at {url} — check your "
            f"internet connection. ({exc.reason})"
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Cloudflare API returned non-JSON: {raw[:200]}")
    if not data.get("success", False):
        msg = _extract_cf_error(raw) or "Cloudflare API rejected the request."
        raise RuntimeError(msg)
    return data.get("result") or {}


def _extract_cf_error(body_text: str) -> str:
    """Pluck a human-readable error out of Cloudflare's error envelope."""
    try:
        d = json.loads(body_text)
        errs = d.get("errors") or []
        if errs and isinstance(errs, list):
            parts = []
            for e in errs:
                code = e.get("code", "")
                m = e.get("message", "")
                parts.append(f"{m} (code {code})" if code else m)
            return "; ".join(p for p in parts if p)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Public API — Cloudflare API
# ---------------------------------------------------------------------------

def create_tunnel(api_token: str,
                  account_id: str,
                  tunnel_name: str) -> dict:
    """Create a new Cloudflare Tunnel (formerly Argo Tunnel).

    POST https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel
    body: {name: "<name>", config_src: "cloudflare"}

    Returns: {ok, tunnel_id, tunnel_token, name}
    """
    if not api_token:
        return _err("Cloudflare API token is required.")
    if not account_id:
        return _err("Cloudflare account_id is required (find it on the "
                    "right sidebar of any Cloudflare dashboard page).")
    if not tunnel_name:
        return _err("tunnel_name is required.")

    log.info("cloudflare_setup: creating tunnel %r (account=%s, token=%s)",
             tunnel_name, account_id, redact_token(api_token))
    try:
        result = _cf_request(
            "POST",
            f"accounts/{account_id}/cfd_tunnel",
            api_token,
            body={"name": tunnel_name, "config_src": "cloudflare"},
        )
    except RuntimeError as exc:
        return _err(str(exc), exc)

    tunnel_id    = result.get("id") or ""
    tunnel_token = result.get("token") or ""
    if not tunnel_id or not tunnel_token:
        return _err("Cloudflare returned an unexpected response (missing "
                    "tunnel id or token).")
    log.info("cloudflare_setup: tunnel created id=%s token=%s",
             tunnel_id, redact_token(tunnel_token))
    return {
        "ok":            True,
        "tunnel_id":     tunnel_id,
        "tunnel_token":  tunnel_token,
        "name":          result.get("name") or tunnel_name,
    }


def add_dns_route(api_token: str,
                  zone_id: str,
                  tunnel_id: str,
                  hostname: str) -> dict:
    """Add a proxied CNAME pointing hostname → <tunnel_id>.cfargotunnel.com.

    POST /zones/{zone_id}/dns_records
    """
    if not zone_id:
        return _err("Cloudflare zone_id is required (open the domain in "
                    "the Cloudflare dashboard — it's on the Overview tab "
                    "right sidebar).")
    if not tunnel_id:
        return _err("tunnel_id is required.")
    if not hostname:
        return _err("hostname is required (e.g. orbi.yourdomain.com).")

    target = f"{tunnel_id}.cfargotunnel.com"
    log.info("cloudflare_setup: adding DNS route %s → %s", hostname, target)
    try:
        result = _cf_request(
            "POST",
            f"zones/{zone_id}/dns_records",
            api_token,
            body={
                "type":    "CNAME",
                "name":    hostname,
                "content": target,
                "proxied": True,
                "ttl":     1,   # 1 = "automatic"
                "comment": "Orbi tunnel — managed by orbi_web/customer_install/cloudflare_setup.py",
            },
        )
    except RuntimeError as exc:
        # The most common cause: record already exists.
        msg = str(exc)
        if "already exists" in msg.lower() or "81053" in msg or "81057" in msg:
            return {
                "ok":       True,
                "hostname": hostname,
                "target":   target,
                "note":     "DNS record already existed — left alone.",
                "reused":   True,
            }
        return _err(msg, exc)

    return {
        "ok":       True,
        "hostname": hostname,
        "target":   target,
        "record_id": result.get("id") or "",
        "proxied":  bool(result.get("proxied", True)),
        "reused":   False,
    }


# ---------------------------------------------------------------------------
# Public API — local service install
# ---------------------------------------------------------------------------

_SYSTEMD_UNIT = """\
[Unit]
Description=Orbi Cloudflare Tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/env cloudflared --config {config_path} tunnel run
Restart=on-failure
RestartSec=5
User={user}
# The token is in {config_path} (mode 0600) — not exposed via /proc/.../cmdline
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
"""

_LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.orbi.cloudflared</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/cloudflared</string>
    <string>--config</string>
    <string>{config_path}</string>
    <string>tunnel</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_path}</string>
  <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
"""


def install_cloudflared_service(install_dir: Path,
                                tunnel_token: str) -> dict:
    """Write ~/.cloudflared/config.yml and a boot-time service.

    Layout written:
        <install_dir>/config.yml        (chmod 0600 — contains the token)
        <install_dir>/orbi-tunnel.service     (systemd unit file)
                  -- or --
        <install_dir>/com.orbi.cloudflared.plist  (launchd, on Mac)

    Returns: {ok, config_path, service_path, install_command}
    """
    if not tunnel_token:
        return _err("tunnel_token is required (it came from create_tunnel).")

    install_dir = Path(install_dir).expanduser().resolve()
    log.info("cloudflared service install: dir=%s token=%s",
             install_dir, redact_token(tunnel_token))

    config_path  = install_dir / "config.yml"
    service_path = install_dir / "orbi-tunnel.service"
    plist_path   = install_dir / "com.orbi.cloudflared.plist"
    log_path     = install_dir / "cloudflared.log"

    # config.yml — references the token by value, mode 0600.
    # cloudflared also accepts --token on the CLI; we use the config-file
    # form so the token doesn't appear in `ps aux`.
    config_yaml = (
        "# Generated by orbi_web/customer_install/cloudflare_setup.py\n"
        "# Mode 0600 — owner-readable only. Contains the tunnel token.\n"
        f"tunnel-token: {tunnel_token}\n"
        "no-autoupdate: false\n"
    )
    with _STATE_LOCK:
        try:
            _atomic_write(config_path, config_yaml, mode=0o600)
        except OSError as exc:
            return _err(
                f"Could not write {config_path}. Check the install "
                "directory permissions.",
                exc,
            )

    system = platform.system()
    service_written: Path | None = None
    install_command = ""

    try:
        if system == "Darwin":
            plist = _LAUNCHD_PLIST.format(
                config_path=str(config_path),
                log_path=str(log_path),
            )
            _atomic_write(plist_path, plist, mode=0o644)
            service_written = plist_path
            install_command = (
                f"cp {plist_path} ~/Library/LaunchAgents/ && "
                f"launchctl load ~/Library/LaunchAgents/{plist_path.name}"
            )
        else:
            # Linux / WSL — write a systemd unit. The owner runs the
            # printed `install_command` (with sudo) to activate it.
            user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
            unit = _SYSTEMD_UNIT.format(
                config_path=str(config_path),
                user=user,
            )
            _atomic_write(service_path, unit, mode=0o644)
            service_written = service_path
            install_command = (
                f"sudo cp {service_path} /etc/systemd/system/ && "
                "sudo systemctl daemon-reload && "
                "sudo systemctl enable --now orbi-tunnel.service"
            )
    except OSError as exc:
        return _err(
            f"Could not write the service file at {service_written}. "
            "Check directory permissions.",
            exc,
        )

    if not shutil.which("cloudflared"):
        log.warning("cloudflared binary not found on PATH — owner still "
                    "needs to install it before starting the service.")

    return {
        "ok":               True,
        "config_path":      str(config_path),
        "service_path":     str(service_written) if service_written else "",
        "platform":         system,
        "install_command":  install_command,
        "binary_present":   bool(shutil.which("cloudflared")),
    }


# ---------------------------------------------------------------------------
# Public API — health probe
# ---------------------------------------------------------------------------

def verify_tunnel_health(hostname: str, timeout: int = 15) -> dict:
    """Hit https://<hostname>/health once per second until success or
    timeout. Returns {ok: True, latency_ms} on a 200, or
    {ok: False, error: ...} on timeout / non-2xx."""
    if not hostname:
        return _err("hostname is required.")
    url = f"https://{hostname}/health"
    deadline = time.time() + max(1, int(timeout))
    last_error = "no response yet"
    attempts = 0

    ctx = ssl.create_default_context()
    while time.time() < deadline:
        attempts += 1
        start = time.time()
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "orbi-tunnel-health/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                code = resp.status
                if 200 <= code < 300:
                    latency_ms = int((time.time() - start) * 1000)
                    log.info("cloudflare_setup: tunnel healthy "
                             "(%s, %d ms, %d attempts)",
                             hostname, latency_ms, attempts)
                    return {
                        "ok":         True,
                        "latency_ms": latency_ms,
                        "attempts":   attempts,
                        "url":        url,
                    }
                last_error = f"HTTP {code}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            last_error = str(getattr(exc, "reason", exc) or exc)
        time.sleep(1)

    log.warning("cloudflare_setup: tunnel health check timed out for %s (%s)",
                hostname, last_error)
    return {
        "ok":       False,
        "error":    f"Tunnel did not respond at {url} within {timeout}s "
                    f"({last_error}). Make sure cloudflared is running "
                    "and Orbi is up on http://localhost:5050.",
        "attempts": attempts,
        "url":      url,
    }


# ---------------------------------------------------------------------------
# Public API — helpers + current state
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def pick_subdomain(business_name: str) -> str:
    """Slugify "Joe's Deli & Market" → "joes-deli-market".
    Used to suggest a default subdomain on the base orbi domain.
    """
    if not business_name:
        return "orbi"
    s = business_name.lower().strip()
    # Collapse common possessives / punctuation before the regex pass.
    s = s.replace("'", "").replace("&", "and")
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    # Length guard — Cloudflare hostnames must be ≤ 63 chars per label.
    if len(s) > 40:
        s = s[:40].rstrip("-")
    return s or "orbi"


def current_tunnel_config(config: dict) -> dict:
    """Return the current tunnel state for the dashboard.
    Tokens are REDACTED. The dashboard never sees the raw secret."""
    tun = (config or {}).get("tunnel") or {}
    server = (config or {}).get("server") or {}
    return {
        "tunnel_id":   tun.get("tunnel_id", "") or "",
        "tunnel_name": tun.get("tunnel_name", "") or "",
        "hostname":    tun.get("hostname", "") or "",
        "tunnel_token": redact_token(tun.get("tunnel_token", "")),
        "service_installed": bool(tun.get("service_installed")),
        "installed_at": tun.get("installed_at", "") or "",
        "tunnel_url":  server.get("tunnel_url", "") or "",
        "configured":  bool(tun.get("tunnel_id") and tun.get("hostname")),
    }
