#!/usr/bin/env python3
"""
Orby — customer-side service.

Runs on the customer's box. Exposes:

  Public (no auth):
    GET  /                      → PWA chat shell
    POST /chat                  → public chatbot
    GET  /pwa/manifest.json     → PWA manifest
    GET  /pwa/service-worker.js → PWA service worker
    GET  /health                → for the watchdog

  Owner (cookie-authed):
    GET  /owner                 → dashboard HTML
    GET  /owner/login           → login page
    POST /api/owner/login
    POST /api/owner/logout
    GET  /api/owner/status
    GET  /api/owner/messages
    POST /api/owner/messages/<id>/read
    DELETE /api/owner/messages/<id>
    GET  /api/owner/business_info
    PUT  /api/owner/business_info
    GET  /api/owner/settings
    PUT  /api/owner/settings
    POST /api/owner/change_password
    POST /api/owner/chat         → owner-mode chat (full access)

  Internal:
    POST /api/internal/notify   → from watchdog (no public exposure)

Lean by design. Three modules only. Three LLM tiers. No bloat.
"""

from __future__ import annotations

import json
import logging
import os
import re as _re
import threading
import time
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, make_response, request,
                   send_from_directory)

import audit
import auth
import auto_categorize
import backup
import briefing
import connectors
from connectors import base as connector_base
import contextual_reminders
import cross_search as xs
import customer_thread
import doc_convert
import file_fetch
import follow_up
import gcal
import image_gen
import ocr as ocr_mod
import review_responder
import safe_send
import scheduler as meeting_scheduler
import birthdays
import booking
import chart_gen
import email_inbox
import mail_merge
import onboarding
import pptx_gen
import style_learner
import translation
import universal_search
import voice_notes
import llm_client
import notifications as notify
import pre_execute as pre_exec
import prompts
import rate_limit
import users as users_mod
import voice
import wellbeing
from modules import business_info as mod_business
from modules import calendar as mod_calendar
from modules import catalog as mod_catalog
from modules import contacts as mod_contacts
from modules import learning_loop as mod_learning
from modules import memory as mod_memory
from modules import messages as mod_messages
from modules import notes as mod_notes
from modules import quick_capture as mod_qc
from modules import reminders as mod_reminders
from modules import tasks as mod_tasks
from modules import workspace as mod_workspace
from tools import url_fetch as tool_url_fetch
from tools import web_search as tool_web_search

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

def _default_orbi_dir() -> Path:
    """Platform-appropriate default if ORBI_DIR isn't set."""
    import platform as _p
    if _p.system() == "Windows":
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Orby"
    if _p.system() == "Darwin":
        return Path.home() / ".orbi"
    return Path("/opt/orbi")

ORBI_DIR     = Path(os.environ.get("ORBI_DIR", str(_default_orbi_dir())))
DATA_DIR     = ORBI_DIR / "data"
CONFIG_FILE  = ORBI_DIR / "config.json"
PWA_DIR      = ORBI_DIR / "pwa"
STATIC_DIR   = ORBI_DIR / "static"
DASHBOARD_DIR = ORBI_DIR / "owner_dashboard"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("orbi")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error(f"No config at {CONFIG_FILE} — run onboarding wizard first.")
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Config load failed: {e}")
        return {}

def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)

CONFIG: dict = load_config()

# Import all connector modules so each one's @register decorator fires.
# Done after CONFIG is loaded so connector imports can read config if they need to.
try:
    connectors.base.import_all()
except Exception as _e:
    # A single bad connector shouldn't take down the whole app
    logging.getLogger("orbi").warning(f"some connectors failed to import: {_e}")

# ---------------------------------------------------------------------------
# Billing check (background thread, polls Frank's central billing service)
# ---------------------------------------------------------------------------

BILLING_STATUS = {"active": True, "warning": None, "tier": "standard", "last_check": 0}

def billing_loop():
    while True:
        try:
            check_billing()
        except Exception as e:
            log.warning(f"billing check failed: {e}")
        interval = (CONFIG.get("billing") or {}).get("check_interval_seconds", 3600)
        time.sleep(interval)

def check_billing() -> None:
    import urllib.request
    cfg = CONFIG.get("billing", {})
    api_key = (CONFIG.get("brain", {}) or {}).get("api_key")
    base_url = cfg.get("check_url")
    if not api_key or not base_url:
        return
    url = base_url.rstrip("/") + "/" + api_key
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        BILLING_STATUS.update({
            "active":  bool(data.get("active")),
            "warning": data.get("warning"),
            "tier":    data.get("tier"),
            "last_check": int(time.time()),
        })
    except Exception as e:
        log.warning(f"billing endpoint unreachable: {e}")

threading.Thread(target=billing_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Archive sweeper — runs once on boot, then daily
# Purges _archived/<username>/ folders past their 90-day purge_after date
# (skipping any with hold=True). Same daemon-thread pattern as billing.
# ---------------------------------------------------------------------------

def archive_sweep_loop():
    # Brief delay so initial startup logs aren't interleaved with our output
    time.sleep(60)
    while True:
        try:
            purged = users_mod.purge_expired_archives(DATA_DIR)
            if purged:
                log.info(f"archive sweep: purged {len(purged)} user(s): {purged}")
        except Exception as e:
            log.warning(f"archive sweep failed: {e}")
        time.sleep(60 * 60 * 24)  # once per day

threading.Thread(target=archive_sweep_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Reminder firing worker — checks each user's pending reminders every minute
# and fires due ones through the notifications module.
# ---------------------------------------------------------------------------

def reminder_fire_loop():
    time.sleep(30)
    while True:
        try:
            _check_all_users_reminders()
        except Exception as e:
            log.warning(f"reminder fire loop error: {e}")
        time.sleep(60)

def _check_all_users_reminders():
    for u in users_mod.list_users(DATA_DIR):
        user_dir = users_mod.get_user_dir(DATA_DIR, u["username"])
        if not user_dir.exists():
            continue
        for r in mod_reminders.due_now(user_dir):
            try:
                notify.send(
                    CONFIG, DATA_DIR,
                    event="reminder_due",
                    title=f"Reminder for {u['username']}",
                    body=r.get("text", ""),
                    url="/owner",
                )
                mod_reminders.mark_fired(user_dir, r["id"])
                log.info(f"fired reminder for {u['username']}: {r.get('text','')[:40]}")
            except Exception as e:
                log.warning(f"could not fire reminder {r.get('id')}: {e}")

threading.Thread(target=reminder_fire_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Fleet heartbeat — phone home to Frank's central brain server every ~5 min
# so the mother ship knows this install is alive. Layered on top of the
# local watchdog (which restarts THIS Orby if it crashes); fleet heartbeat
# is the only way the central server can know that a customer's machine
# is OFF, their internet is DOWN, or their watchdog itself is dead.
# ---------------------------------------------------------------------------

HEARTBEAT_INTERVAL_SEC = int(os.environ.get("ORBI_HEARTBEAT_SEC", "300"))  # 5 min


def fleet_heartbeat_loop():
    """POST /api/heartbeat/<api_key> to the brain server every 5 min.
    Silent on failure — the brain server's dark-detector will notice the
    gap and alert Frank. We don't need to scream from this side."""
    # Initial delay so we don't race the rest of Orby coming up
    time.sleep(45)
    while True:
        try:
            _send_fleet_heartbeat()
        except Exception as e:
            log.debug(f"heartbeat send failed (will retry): {e}")
        time.sleep(HEARTBEAT_INTERVAL_SEC)


def _send_fleet_heartbeat() -> None:
    brain_cfg = CONFIG.get("brain") or {}
    brain_url = (brain_cfg.get("url") or "").rstrip("/")
    api_key   = brain_cfg.get("api_key") or ""
    if not brain_url or not api_key or "placeholder" in api_key.lower():
        return  # not configured yet — silently skip
    import platform as _platform
    import urllib.request, urllib.error
    # Customer's current public URL — needed so the brain can forward
    # Twilio voice webhooks + visitor-chat traffic to this machine.
    # Reads from the live cloudflared tunnel runner; falls back to
    # config-pinned URLs for dev / advanced setups.
    public_url = (current_tunnel_url() or
                  CONFIG.get("tunnel_url") or
                  (CONFIG.get("brain") or {}).get("local_public_url") or "")
    payload = {
        "uptime_sec":   int(time.time() - START_TIME),
        "version":      CONFIG.get("version", "0.1.0"),
        "platform":     _platform.system().lower(),
        "platform_rel": _platform.release(),
        "now_iso":      _dt_now_iso(),
        "billing_active": bool(BILLING_STATUS.get("active", True)),
        "public_url":   public_url,
    }
    req = urllib.request.Request(
        f"{brain_url}/api/heartbeat/{api_key}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent":   "Orby-Heartbeat/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            # Honor any commands the server sent back (future: remote
            # restart, force update, etc.). For now just log them.
            cmds = body.get("commands") or []
            if cmds:
                log.info(f"heartbeat: server sent {len(cmds)} commands: {cmds}")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        log.debug(f"heartbeat unreachable: {e}")


def _dt_now_iso() -> str:
    from datetime import datetime as _dt, timezone as _tz
    return _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# START_TIME is set near the bottom of this file but we reference it at
# import time — bind a placeholder that gets corrected once the module
# finishes loading.
if "START_TIME" not in dir():
    START_TIME = time.time()

threading.Thread(target=fleet_heartbeat_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Cloudflare tunnel — give the customer a stable-ish public URL so Twilio
# voice webhooks (from Frank's brain server) and visitor-chat traffic can
# reach this machine. Uses cloudflared's quick mode (trycloudflare.com)
# which is FREE and requires zero account setup — perfect for the
# zero-tech-customer install. URL is ephemeral (regenerated on each
# cloudflared restart) but the heartbeat reports the current URL so the
# brain always knows where to forward.
# ---------------------------------------------------------------------------

TUNNEL_URL_FILE = DATA_DIR / "tunnel.url"
_CURRENT_TUNNEL_URL = [""]  # mutable so loop can update + heartbeat can read


def _find_cloudflared() -> str | None:
    """Look for the cloudflared binary the installer bundled, or on PATH."""
    bin_dir = ORBI_DIR / "bin"
    for name in ("cloudflared", "cloudflared.exe"):
        candidate = bin_dir / name
        if candidate.exists():
            return str(candidate)
    import shutil as _shutil
    found = _shutil.which("cloudflared")
    return found


def tunnel_runner_loop():
    """Spawn cloudflared in quick mode, capture the assigned URL, restart
    on failure. Silent if cloudflared isn't installed (Twilio just won't
    work — every other Orby feature is unaffected)."""
    import subprocess
    import re as _re
    import shutil

    binpath = _find_cloudflared()
    if not binpath:
        log.info("cloudflared not found — tunnel disabled (no public URL)")
        return

    # Restore last-known URL while we wait for cloudflared to come up,
    # so the first heartbeat after restart isn't completely empty.
    if TUNNEL_URL_FILE.exists():
        try:
            _CURRENT_TUNNEL_URL[0] = TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    port = int(CONFIG.get("port", 5050))
    target = f"http://localhost:{port}"
    backoff = 5
    url_pattern = _re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

    while True:
        try:
            log.info(f"cloudflared starting tunnel → {target}")
            proc = subprocess.Popen(
                [binpath, "tunnel", "--no-autoupdate", "--url", target],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            # Parse stdout for the assigned URL
            for line in proc.stdout:
                m = url_pattern.search(line)
                if m and m.group(0) != _CURRENT_TUNNEL_URL[0]:
                    new_url = m.group(0)
                    _CURRENT_TUNNEL_URL[0] = new_url
                    try:
                        TUNNEL_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
                        TUNNEL_URL_FILE.write_text(new_url, encoding="utf-8")
                    except OSError as e:
                        log.warning(f"could not write tunnel.url: {e}")
                    log.info(f"cloudflared assigned URL: {new_url}")
                    backoff = 5  # reset backoff on successful URL
            # Pipe closed = process exited
            rc = proc.wait()
            log.warning(f"cloudflared exited rc={rc} — restarting in {backoff}s")
        except Exception as e:
            log.warning(f"tunnel_runner_loop error: {e}")
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)  # cap at 5 min


def current_tunnel_url() -> str:
    """Return the current public URL of this Orby (for heartbeat etc.)."""
    if _CURRENT_TUNNEL_URL[0]:
        return _CURRENT_TUNNEL_URL[0]
    if TUNNEL_URL_FILE.exists():
        try:
            return TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


# Only start the tunnel if config.json opts in (default ON for installed
# customers; lets Frank disable on his dev machine via config flag).
if (CONFIG.get("tunnel") or {}).get("enabled", True):
    threading.Thread(target=tunnel_runner_loop, daemon=True).start()
else:
    log.info("tunnel disabled in config — no public URL")


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # In Phase 1 we serve a single chat shell. Phase 2 adds branding per customer.
    chat_shell = STATIC_DIR / "chat.html"
    if chat_shell.exists():
        return send_from_directory(STATIC_DIR, "chat.html")
    return (f"Orby is running but no chat shell is installed. "
            f"Place chat.html in {STATIC_DIR}.", 503)

@app.route("/pwa/<path:filename>")
def pwa_files(filename):
    return send_from_directory(PWA_DIR, filename)

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(PWA_DIR / "icons", "icon-favicon.png",
                               mimetype="image/png")

# ---------------------------------------------------------------------------
# Text-to-speech (Edge TTS — Microsoft Azure neural voices, free)
# ---------------------------------------------------------------------------

_DEFAULT_VOICE = (CONFIG.get("voice", {}) or {}).get("name", "en-US-AvaNeural")

def _tts_via_piper(text: str, voice: str, rate: str, tts_cfg: dict):
    """Synthesize via the bundled Piper binary. Returns a Flask Response
    on success, None if Piper is not available (caller falls back to
    edge_tts). Output is WAV from Piper, transcoded to MP3 via the
    bundled ffmpeg so the browser can stream it like edge_tts output."""
    bin_dir = ORBI_DIR / "bin"
    piper_bin = bin_dir / ("piper.exe" if os.name == "nt" else "piper")
    if not piper_bin.exists():
        import shutil as _shutil
        found = _shutil.which("piper")
        if not found:
            return None
        piper_bin = Path(found)

    # Voice model selection — defaults to one that sounds close to
    # Polly.Joanna (the phone receptionist voice) for cross-product
    # consistency. Customer / Frank can override via config.tts.voice_model.
    model_dir = ORBI_DIR / "tts_models"
    model_name = (tts_cfg.get("voice_model") or
                  "en_US-amy-medium")
    model_path = model_dir / f"{model_name}.onnx"
    if not model_path.exists():
        log.warning(f"piper voice model {model_path} not found — skipping")
        return None

    ffmpeg_bin = bin_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not ffmpeg_bin.exists():
        import shutil as _shutil
        found = _shutil.which("ffmpeg")
        if not found:
            log.warning("ffmpeg not found — piper output stays as WAV "
                        "(browser will still play it)")
            ffmpeg_bin = None
        else:
            ffmpeg_bin = Path(found)

    import subprocess
    from flask import Response

    def stream_chunks():
        try:
            # Piper reads text on stdin, writes WAV on stdout
            piper = subprocess.Popen(
                [str(piper_bin), "--model", str(model_path), "--output_raw"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            piper.stdin.write(text.encode("utf-8"))
            piper.stdin.close()

            if ffmpeg_bin:
                # WAV → MP3
                ff = subprocess.Popen(
                    [str(ffmpeg_bin), "-loglevel", "quiet",
                     "-f", "s16le", "-ar", "22050", "-ac", "1", "-i", "-",
                     "-f", "mp3", "-b:a", "64k", "-"],
                    stdin=piper.stdout, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                while True:
                    chunk = ff.stdout.read(4096)
                    if not chunk:
                        break
                    yield chunk
                ff.wait()
            else:
                # Stream raw WAV (browser still plays it; bigger payload)
                while True:
                    chunk = piper.stdout.read(4096)
                    if not chunk:
                        break
                    yield chunk
            piper.wait()
        except Exception as e:
            log.warning(f"piper tts stream failed: {e}")

    return Response(
        stream_chunks(),
        mimetype="audio/mpeg" if ffmpeg_bin else "audio/wav",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/tts", methods=["GET", "POST"])
def tts():
    """Generate MP3 audio for text. Two engines:
      - 'piper'  → self-hosted, MIT-licensed, commercially safe (preferred)
      - 'edge'   → Edge TTS reverse-engineered (free but legally gray;
                   acceptable for personal use + first few customers)
    Pick via config.tts.engine. Defaults to 'edge' for backward compat;
    customer installer sets 'piper' as the default once the binary is
    bundled."""
    if request.method == "GET":
        text  = (request.args.get("text") or "").strip()
        voice = request.args.get("voice") or _DEFAULT_VOICE
        rate  = request.args.get("rate", "+0%")
    else:
        data  = request.get_json(silent=True) or {}
        text  = (data.get("text") or "").strip()
        voice = data.get("voice") or _DEFAULT_VOICE
        rate  = data.get("rate", "+0%")
    if not text:
        return jsonify({"error": "empty_text"}), 400
    # Cap on text length sent to TTS. Was 1500 chars (~2 min speech) which
    # cut off any long response — letters, marketing campaigns, multi-part
    # answers all got truncated mid-sentence. 12000 covers ~15 min of
    # continuous speech which is plenty; longer than that and the user
    # would lose interest before the TTS finished anyway.
    if len(text) > 12000:
        text = text[:12000]

    tts_cfg = CONFIG.get("tts") or {}
    engine = tts_cfg.get("engine", "edge").lower()

    # Try Piper first if configured. Falls back to edge on failure so a
    # missing binary doesn't break TTS entirely.
    if engine == "piper":
        piper_response = _tts_via_piper(text, voice, rate, tts_cfg)
        if piper_response is not None:
            return piper_response
        # Fall through to edge if Piper not available
        log.warning("piper TTS unavailable — falling back to edge_tts")

    try:
        import edge_tts
    except ImportError:
        return jsonify({"error": "edge_tts_not_installed"}), 503

    import asyncio
    from flask import Response

    # Bridge async edge_tts.stream() to a sync generator Flask can yield.
    def stream_chunks():
        loop = asyncio.new_event_loop()
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            it = communicate.stream().__aiter__()
            while True:
                try:
                    chunk = loop.run_until_complete(it.__anext__())
                except StopAsyncIteration:
                    break
                if chunk.get("type") == "audio":
                    data = chunk.get("data")
                    if data:
                        yield data
        except Exception as e:
            log.warning(f"tts stream failed: {e}")
        finally:
            try: loop.close()
            except Exception: pass

    return Response(
        stream_chunks(),
        mimetype="audio/mpeg",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )

@app.route("/api/voices", methods=["GET"])
def list_voices():
    """A curated list of nice-sounding English voices."""
    return jsonify({
        "default": _DEFAULT_VOICE,
        "voices": [
            {"id": "en-US-AvaNeural",    "label": "Ava (female, warm, conversational)"},
            {"id": "en-US-AriaNeural",   "label": "Aria (female, professional)"},
            {"id": "en-US-JennyNeural",  "label": "Jenny (female, friendly)"},
            {"id": "en-US-EmmaNeural",   "label": "Emma (female, cheerful)"},
            {"id": "en-US-AndrewNeural", "label": "Andrew (male, warm)"},
            {"id": "en-US-GuyNeural",    "label": "Guy (male, professional)"},
            {"id": "en-US-BrianNeural",  "label": "Brian (male, casual)"},
        ],
    })

@app.route("/health")
def health():
    business = mod_business.load(DATA_DIR)
    return jsonify({
        "status": "ok",
        "version": CONFIG.get("version", "0.1.0"),
        "uptime": int(time.time() - START_TIME),
        "billing": BILLING_STATUS.get("active", True),
        "business_name": business.get("name") or CONFIG.get("business", {}).get("name", ""),
    })


@app.route("/api/owner/notifications/inbox")
def owner_notifications_inbox():
    """Return in-app notifications for the current user. Used by the
    dashboard's polling loop to show toasts for fired reminders + leads
    when no other channel (push/email/sms) is configured."""
    auth.require_user(ORBI_DIR, DATA_DIR)
    unseen_only = request.args.get("unseen", "").lower() in ("1", "true", "yes")
    return jsonify({"items": notify.list_inbox(DATA_DIR, unseen_only=unseen_only)})


@app.route("/api/owner/notifications/<nid>/seen", methods=["POST"])
def owner_notifications_mark_seen(nid: str):
    auth.require_user(ORBI_DIR, DATA_DIR)
    ok = notify.mark_inbox_seen(DATA_DIR, nid)
    return jsonify({"ok": ok})


@app.route("/api/owner/notifications/all_seen", methods=["POST"])
def owner_notifications_mark_all_seen():
    auth.require_user(ORBI_DIR, DATA_DIR)
    n = notify.mark_inbox_all_seen(DATA_DIR)
    return jsonify({"ok": True, "marked": n})


@app.route("/api/owner/notifications/<nid>/ack", methods=["POST"])
def owner_notifications_ack(nid: str):
    """Mark a reminder acknowledged — stops the re-fire / re-speak loop."""
    auth.require_user(ORBI_DIR, DATA_DIR)
    ok = notify.mark_inbox_acknowledged(DATA_DIR, nid)
    return jsonify({"ok": ok})


@app.route("/api/help/capabilities")
def help_capabilities():
    """Serve the Orby capabilities markdown. The dashboard renders it
    client-side; the chat teach-intent reads it too so a single source
    feeds both surfaces.

    Search order:
      1. ORBI_DIR/orbi_capabilities.md  (customer override, if they edited it)
      2. The shipped copy that sits next to this module
    """
    candidates = [
        ORBI_DIR / "orbi_capabilities.md",
        Path(__file__).parent / "orbi_capabilities.md",
    ]
    for p in candidates:
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            return Response(text, mimetype="text/markdown; charset=utf-8")
    return jsonify({"error": "capabilities_doc_missing"}), 404

@app.route("/api/catalog/search")
def catalog_search():
    """Public catalog search — no auth required. Returns matching items
    from the owner's dropped catalog file. Used by tests + as a fallback
    discovery endpoint. Limit and query come from query string."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": [], "query": "", "count": 0})
    try:
        limit = max(1, min(50, int(request.args.get("limit", "10"))))
    except ValueError:
        limit = 10
    results = mod_catalog.search(DATA_DIR, q, limit=limit)
    return jsonify({"results": results, "query": q, "count": len(results)})


@app.route("/api/owner/catalog/status")
def owner_catalog_status():
    """Owner-only — how many items are indexed, when was the last import,
    which columns did Orby map. Powers the 'Catalog' widget in the
    owner dashboard."""
    auth.require_owner(ORBI_DIR)
    return jsonify(mod_catalog.status(DATA_DIR))


@app.route("/api/owner/learning/pending")
def owner_learning_pending():
    """Owner-only — the list of unanswered questions visitors have asked.
    Each entry has: token, question, asker (name/phone/email/preferred_channel),
    asked_at, asked_count. Owner answers via /api/owner/learning/answer."""
    auth.require_owner(ORBI_DIR)
    return jsonify({"pending": mod_learning.list_pending(DATA_DIR)})


@app.route("/api/owner/learning/learned")
def owner_learning_learned():
    """Owner-only — full history of answered questions. Most recent first.
    Limit defaults to 50."""
    auth.require_owner(ORBI_DIR)
    try:
        limit = max(1, min(500, int(request.args.get("limit", "50"))))
    except ValueError:
        limit = 50
    return jsonify({"learned": mod_learning.list_learned(DATA_DIR, limit=limit)})


@app.route("/api/internal/owner_reply", methods=["POST"])
def internal_owner_reply():
    """Webhook for owner-reply-by-text / owner-reply-by-email.

    When the owner replies to the notification email or SMS, the email
    server / Twilio webhook calls this endpoint with the reply body.
    We parse a question token from the body (it's prefixed in the
    notification: 'Q_xxx: question text') and save the reply as the
    answer.

    Accepts JSON:
      { "from": "owner-phone-or-email", "body": "Q_h29o...: yes we do" }
    OR Twilio form-encoded SMS:
      Body=...  From=...

    Owner authentication happens via a shared secret in the URL or
    by matching the "from" field against the owner's configured
    phone/email in CONFIG.owner. Safe-fail: if we can't link the
    reply to a pending question, log + ignore."""
    body = ""
    from_addr = ""
    if request.content_type and "application/json" in request.content_type:
        data = request.get_json(silent=True) or {}
        body = (data.get("body") or "").strip()
        from_addr = (data.get("from") or "").strip()
    else:
        body = (request.form.get("Body") or "").strip()
        from_addr = (request.form.get("From") or "").strip()
    if not body:
        return jsonify({"error": "empty body"}), 400

    # Verify the reply came from the owner's known phone / email
    owner_cfg = CONFIG.get("owner") or {}
    expected_phone = (owner_cfg.get("phone") or "").strip()
    expected_email = (owner_cfg.get("email") or "").strip()
    from_norm = from_addr.strip().lower()
    if from_norm not in (expected_phone.lower(), expected_email.lower()):
        log.warning("owner_reply rejected: from=%r is not the configured owner", from_addr)
        return jsonify({"error": "unrecognized sender"}), 403

    # Extract the Q_xxx token from the start of the body
    token_match = re.match(r"\s*(Q_[A-Za-z0-9]{6,16})\s*[:\-,]?\s*(.*)$",
                          body, re.DOTALL)
    if not token_match:
        return jsonify({"error": "no question token found in reply"}), 400
    token = token_match.group(1)
    answer = token_match.group(2).strip()
    if not answer:
        return jsonify({"error": "empty answer"}), 400

    learned = mod_learning.answer_pending(
        DATA_DIR, token=token, answer=answer,
        answered_by=expected_email or expected_phone or "owner",
    )
    if not learned:
        return jsonify({"error": "pending question not found"}), 404
    audit.log_event(DATA_DIR, actor=from_addr,
                    action="learning.answered_via_reply",
                    resource=f"question/{token}",
                    after={"question": learned["question"],
                           "answer": answer[:120]})
    return jsonify({"status": "ok", "learned": learned})


@app.route("/api/owner/learning/answer", methods=["POST"])
def owner_learning_answer():
    """Owner submits an answer to a pending question. Body:
       { token: "Q_...", answer: "..." }
    Moves the Q+A from pending → learned (verified=True). Future
    visitors asking the same question get the verified answer instantly.
    The customer-callback dispatcher (separate process) will then notify
    the original asker via their preferred channel."""
    owner_session = auth.require_owner(ORBI_DIR)
    payload = request.get_json(silent=True) or {}
    token = (payload.get("token") or "").strip()
    answer = (payload.get("answer") or "").strip()
    if not token or not answer:
        return jsonify({"error": "token and answer are required"}), 400
    pending = mod_learning.find_pending(DATA_DIR, token)
    if not pending:
        return jsonify({"error": "pending question not found"}), 404
    learned = mod_learning.answer_pending(
        DATA_DIR, token=token, answer=answer,
        answered_by=owner_session.get("email", "owner"),
    )
    if not learned:
        return jsonify({"error": "could not save answer"}), 500
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="learning.answered",
                    resource=f"question/{token}",
                    after={"question": learned["question"],
                           "answer": answer[:120]})
    return jsonify({"status": "ok", "learned": learned})


@app.route("/api/owner/catalog/reindex", methods=["POST"])
def owner_catalog_reindex():
    """Owner-only — force a re-index of the newest catalog file. Useful
    when the watcher hasn't picked up a change yet (or the owner wants
    immediate confirmation that their fresh export worked)."""
    owner_session = auth.require_owner(ORBI_DIR)
    result = mod_catalog.reindex(DATA_DIR)
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="catalog.reindex",
                    after={"item_count": result.get("item_count"),
                           "source_file": result.get("source_file")})
    return jsonify(result)


@app.route("/api/public/business_summary")
def public_business_summary():
    """Lightweight, no-auth snapshot for the visitor-facing chat shell.
    Returns ONLY what's safe for any visitor to see."""
    business = mod_business.load(DATA_DIR)
    # Default quick actions based on what scope is enabled
    scope = CONFIG.get("scope", {})
    actions = ["Are you open right now?", "Where are you located?"]
    if business.get("services") or business.get("menu"):
        actions.append("What do you offer?")
    if scope.get("public_can_take_orders"):
        actions.append("I'd like to place an order")
    if scope.get("public_can_book_appointments"):
        actions.append("Can I book an appointment?")
    if scope.get("public_can_request_quotes"):
        actions.append("I'd like a quote")
    return jsonify({
        "name": business.get("name") or CONFIG.get("business", {}).get("name", ""),
        "tagline": business.get("tagline", ""),
        "welcome": True,
        "quick_actions": actions[:4],
    })

# ---------------------------------------------------------------------------
# Public chat
# ---------------------------------------------------------------------------

@app.route("/chat", methods=["POST"])
def public_chat():
    if not BILLING_STATUS.get("active", True):
        return jsonify({
            "reply": (
                f"This {CONFIG.get('business', {}).get('name', 'business')} "
                "assistant is temporarily paused. Please reach out by phone or email."
            ),
            "tier": "none",
            "billing_inactive": True,
        }), 200

    data = request.get_json(silent=True) or {}
    user_msg = (data.get("message") or "").strip()
    history  = data.get("history") or []  # list of {role, content}
    visitor  = data.get("visitor") or {}  # optional {name, phone, email}

    if not user_msg:
        return jsonify({"error": "empty message"}), 400

    business = mod_business.load(DATA_DIR)
    scope    = (CONFIG.get("scope") or {})
    system   = prompts.build_public_prompt(business, scope)

    # PRE-EXECUTE — fast local answer for common questions (greetings,
    # time, date, hours, address, phone, catalog count). When this fires
    # with "direct", we skip the LLM entirely — instant answer, $0 cost,
    # zero hallucination risk. When it returns "data:..." we still call
    # the LLM but with authoritative data injected, so the LLM only
    # composes wording around real facts (109 tokens instead of 6,302
    # per the my_orby measurement).
    # MUST run AFTER the wellbeing check below so crisis messages don't
    # get reduced to a "Hi! What can I help with?" greeting reply.
    pre_resp, pre_kind = pre_exec.pre_execute(user_msg, DATA_DIR, business)
    # We defer the direct-return until after wellbeing has had its say.

    # WELLBEING — scan EVERY public-chat message for crisis / distress signals
    # BEFORE we route to learned answers, catalog, or the LLM. If a signal
    # fires, log it for the owner's dashboard AND inject the appropriate
    # system-prompt context so Orby handles the moment with care instead
    # of barreling into the buy flow. (Ported from orby_5050/engine/wellbeing.py)
    _wb_level = "ok"
    try:
        _wb = wellbeing.check_message(user_msg)
        _wb_level = _wb["level"]
        if _wb_level != "ok":
            wellbeing.log_flag(DATA_DIR, _wb_level, _wb["signal"],
                                user_msg, source="chat")
            log.warning("public chat wellbeing flag fired: level=%s signal=%r",
                        _wb_level, _wb["signal"])
            if _wb_level == "crisis":
                system += "\n\n" + wellbeing.get_crisis_context()
            else:
                system += "\n\n" + wellbeing.get_distress_context()
    except Exception as e:
        log.warning(f"wellbeing check failed: {e}")

    # PRIORITY 0a — pre_execute DIRECT answers (only if wellbeing didn't
    # flag the message; we want crisis messages to get the careful LLM
    # path, not a chipper "Hi! What can I help with?" greeting reply).
    if pre_kind == "direct" and _wb_level == "ok":
        log.info("pre_execute direct hit: %r", pre_resp[:60])
        return jsonify({"reply": pre_resp, "tier": "local"}), 200

    # PRIORITY 0b — LEARNED ANSWERS (never-guess pattern). If the OWNER has
    # already answered this exact question for a previous visitor, return
    # that answer instantly without calling the LLM. This is what makes
    # Orby's knowledge compound over time and never invent facts about
    # the business.
    learned = mod_learning.find_learned(DATA_DIR, user_msg)
    if learned:
        log.info("learned-answer hit (asked %sx)", learned.get("asked_count", 1))
        return jsonify({
            "reply": learned["answer"],
            "tier": "learned",
            "verified": True,
        }), 200

    # PRIORITY 0c — pre_execute DATA hits (e.g. catalog count, services).
    # The LLM still composes the reply but with authoritative business
    # data injected so it can't invent.
    if pre_kind and pre_kind.startswith("data:"):
        extras_pre = ("LOCAL BUSINESS DATA (AUTHORITATIVE — quote exactly, "
                      "don't paraphrase, don't invent):\n" + pre_resp)
        system += "\n\n" + extras_pre
        log.info("pre_execute data hit: kind=%s len=%d", pre_kind, len(pre_resp))

    # Public chat gets workspace context first (so she can answer about
    # promotions, menus, FAQs the owner dropped into ~/Orby/) — these are
    # AUTHORITATIVE. Web search only fires if workspace had no strong match
    # AND the query smells like it needs current info.
    extras = []

    # PRIORITY 1 — PRODUCT CATALOG (highest authority).
    # If the owner has dropped a CSV / Excel into Orby/Catalog/, surface
    # matching items BEFORE workspace and web. Catalog data is the most
    # specific real-world info Orby can use (real SKUs, real prices, real
    # stock counts) — never let the LLM invent product details when the
    # catalog has the answer.
    try:
        cat_matches = mod_catalog.search(DATA_DIR, user_msg, limit=5)
        # Threshold: score >= 10 means at least one name-token matched OR
        # an exact SKU hit (which scores 1000). Below 10 is fuzzy noise.
        strong_cat = [m for m in cat_matches if m.get("score", 0) >= 10]
        if strong_cat:
            lines = ["PRODUCT CATALOG MATCHES (AUTHORITATIVE — these are real "
                     "items the owner sells. Quote name, SKU, price, and stock "
                     "EXACTLY from this list. Do NOT invent products or similar "
                     "items not shown here. If the visitor asks about a part NOT "
                     "in this list, say honestly 'I don't see that in our "
                     "current inventory — let me check with the owner and get "
                     "back to you. Can I get your name and number?'):"]
            for m in strong_cat:
                bits = [m.get("name", "")]
                if m.get("sku"):     bits.append(f"part #{m['sku']}")
                if m.get("price") is not None: bits.append(f"${m['price']:.2f}")
                if m.get("stock") is not None: bits.append(f"{m['stock']} in stock")
                if m.get("brand"):   bits.append(f"({m['brand']})")
                desc = (m.get("description") or "").strip()
                line = "  - " + " · ".join(b for b in bits if b)
                if desc:
                    line += f"\n      {desc[:160]}"
                lines.append(line)
            extras.append("\n".join(lines))
            log.info("public catalog hits: %d strong matches for %r", len(strong_cat), user_msg[:60])
    except Exception as e:
        log.warning(f"public catalog lookup failed: {e}")
    # Priority decision — three-way:
    #   STRONG workspace match (score >= 3) → workspace wins, even for fresh queries.
    #     "What's special right now?" → workspace has a high-scoring "promotion" file → use it.
    #   FRESH query (weather/news/today/etc) + no strong workspace → web search wins.
    #     "Weather in Reno right now" → workspace only weakly matches "Reno" → ignore, hit web.
    #   WEAK workspace match (score 1-2) and not a fresh query → workspace wins.
    #     "What do you do?" → no fresh keyword, workspace has some hits → use workspace.
    msg_lower = user_msg.lower()
    is_fresh_query = any(k in msg_lower for k in tool_web_search._FRESH_KEYWORDS)
    workspace_hit = False
    top_score = 0
    try:
        ws_matches = mod_workspace.search(CONFIG, DATA_DIR, user_msg, limit=3)
        if ws_matches:
            top_score = ws_matches[0].get("score", 0)
            # Workspace wins if it's a strong match, OR if it's a non-fresh query
            if top_score >= 3 or (top_score >= 1 and not is_fresh_query):
                workspace_hit = True
                ws_ctx = mod_workspace.context_block(CONFIG, DATA_DIR, user_msg)
                if ws_ctx:
                    extras.append(
                        "OWNER'S WORKSPACE FILES (AUTHORITATIVE — quote exactly, "
                        "don't paraphrase, don't invent alternatives):\n" + ws_ctx
                    )
                    log.info(f"public workspace hit: top score {top_score}, fresh={is_fresh_query}")
    except Exception as e:
        log.warning(f"public workspace context failed: {e}")
    if not workspace_hit and tool_web_search.needs_web_search(user_msg):
        try:
            web_ctx = tool_web_search.context_block(user_msg)
            if web_ctx:
                extras.append(
                    "WEB SEARCH RESULTS (current real-time info). Quote facts "
                    "directly from these results. If the specific answer the "
                    "user asked about is NOT in the results below, say so "
                    "honestly — do NOT fall back to your training data or "
                    "invent facts.\n" + web_ctx
                )
                log.info(f"public web search: {user_msg[:60]!r}")
        except Exception as e:
            log.warning(f"public web search failed: {e}")

    # URL FETCH — if the visitor pasted a specific URL, go fetch the page
    try:
        urls_in_msg = tool_url_fetch.extract_urls(user_msg)[:3]
        if urls_in_msg:
            fetched = [tool_url_fetch.fetch_or_search(u) for u in urls_in_msg]
            url_ctx = tool_url_fetch.context_block(fetched)
            if url_ctx:
                extras.append(url_ctx)
                log.info(f"public url_fetch: {len(urls_in_msg)} url(s)")
    except Exception as e:
        log.warning(f"public url_fetch failed: {e}")
    if extras:
        system += "\n\n" + "\n\n".join(extras)

    messages = [m for m in history if m.get("role") in ("user", "assistant")][-10:]
    messages.append({"role": "user", "content": user_msg})

    # Visitor rate limit — protect the LLM budget from runaway loops or
    # malicious traffic. Identity = client IP for unauthenticated chat.
    _visitor_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "anon")
    _rl_ok, _rl_used, _rl_cap = rate_limit.check_and_increment(_visitor_ip, role="visitor")
    if not _rl_ok:
        log.warning(f"public chat rate-limited: {_visitor_ip} {_rl_used}/{_rl_cap}")
        return jsonify({
            "reply": ("I've been quite busy today. Please try again in a little while, "
                      "or leave your name and number and someone will reach out."),
            "tier": "rate_limited", "latency_ms": 0,
        })

    resp = llm_client.generate(CONFIG, system, messages)

    # LEARNING-LOOP TRIGGER — if Orby's reply reads like "I don't know"
    # AND the visitor was actually asking a question, kick off the
    # learning loop: capture the question for the owner to answer, ask
    # the visitor for their contact info, and override Orby's bluff
    # with a clear "I'll find out and get back to you" reply.
    pending_record = None
    if (mod_learning.reply_indicates_unknown(resp.text or "")
        and mod_learning.is_question_form(user_msg)):
        try:
            asker = {
                "name":  (visitor.get("name") or "").strip(),
                "phone": (visitor.get("phone") or "").strip(),
                "email": (visitor.get("email") or "").strip(),
                "preferred_channel": (visitor.get("preferred_channel") or "").strip(),
            }
            session_id = (visitor.get("session_id") or "").strip()
            pending_record = mod_learning.capture_pending(
                DATA_DIR, question=user_msg, asker=asker, session_id=session_id,
            )
            log.info("learning loop captured: token=%s question=%r",
                     pending_record["token"], user_msg[:60])
            # Notify the owner. The body includes the question token
            # ("Q_xxx: ...") so when the owner replies by text/email,
            # the /api/internal/owner_reply webhook can parse the
            # reply and link the answer back to this pending question.
            try:
                asker_label = (asker.get("name") or asker.get("phone")
                               or asker.get("email") or "a visitor")
                token = pending_record["token"]
                notify.send(
                    CONFIG, DATA_DIR,
                    event="new_question",
                    title=f"Question from {asker_label}",
                    body=(
                        f"{token}: {user_msg[:200]}\n\n"
                        f"Reply to this with your answer — text/email reply "
                        f"will save it and deliver to {asker_label}."
                    ),
                    url="/owner#learning",
                )
                mod_learning.mark_owner_notified(
                    DATA_DIR, token, channel="auto",
                )
            except Exception as e:
                log.warning(f"owner notify failed: {e}")
            # Override Orby's reply with the standard "I'll find out" ask.
            # If the visitor already gave us contact info, thank them; if
            # not, ask for it so we can deliver the owner's answer back.
            have_contact = bool(asker.get("phone") or asker.get("email"))
            if have_contact:
                resp_text_override = (
                    f"That's a great question — I'm not sure about that one, "
                    f"so I'm going to make sure the owner gets you the right "
                    f"answer. I've got your contact info, and I'll reach out "
                    f"to you the moment I hear back. Anything else?"
                )
            else:
                resp_text_override = (
                    "That's a great question — I'm not sure about that one, "
                    "so I'm going to make sure the owner gets you the right "
                    "answer. Can I get your name and the best way to reach "
                    "you — text, call, or email?"
                )
            resp = type(resp)(text=resp_text_override, **{
                k: getattr(resp, k, None)
                for k in ("tier", "model", "latency_ms", "error")
                if hasattr(resp, k)
            }) if hasattr(resp, "_replace") is False else resp
            # If resp is a namedtuple or dataclass, can't easily replace —
            # just patch the text field if writable, else build a stub.
            try:
                resp.text = resp_text_override
            except (AttributeError, TypeError):
                class _R:
                    def __init__(self, text):
                        self.text = text
                        self.tier = getattr(resp, "tier", "learning_loop")
                        self.error = None
                resp = _R(resp_text_override)
        except Exception as e:
            log.warning(f"learning-loop capture failed: {e}")

    # Capture lead / order / callback if visitor info is present
    capture_kind = _detect_capture(user_msg, scope)
    if capture_kind and (visitor.get("name") or visitor.get("phone") or visitor.get("email")):
        try:
            captured = mod_messages.capture(
                DATA_DIR,
                msg_type=capture_kind,
                from_name=visitor.get("name"),
                from_phone=visitor.get("phone"),
                from_email=visitor.get("email"),
                body=user_msg,
                source="chat",
            )
            event_map = {"order": "new_order", "lead": "new_lead",
                         "callback": "new_lead", "voicemail": "new_voicemail"}
            from_name = visitor.get('name') or visitor.get('phone') or 'Unknown'
            notify.send(
                CONFIG, DATA_DIR,
                event=event_map.get(capture_kind, "new_message"),
                title=f"New {capture_kind} from {from_name}",
                body=user_msg[:200],
                url="/owner",
            )
        except Exception as e:
            log.warning(f"capture failed: {e}")

    return jsonify({
        "reply": resp.text or (
            "I'm having trouble reaching my AI right now. "
            "Please call us or try again in a moment."
        ),
        "tier": resp.tier,
        "latency_ms": resp.latency_ms,
        "billing_warning": BILLING_STATUS.get("warning"),
    })

def _detect_capture(user_msg: str, scope: dict) -> str | None:
    msg = user_msg.lower()
    if scope.get("public_can_take_orders") and any(
        kw in msg for kw in ("order", "i'll have", "i want to buy", "can i get")
    ):
        return "order"
    if scope.get("public_can_book_appointments") and any(
        kw in msg for kw in ("appointment", "book", "schedule")
    ):
        return "lead"
    if scope.get("public_can_request_callbacks") and any(
        kw in msg for kw in ("call me back", "callback", "call back")
    ):
        return "callback"
    if scope.get("public_can_request_quotes") and any(
        kw in msg for kw in ("quote", "estimate", "how much would", "price for")
    ):
        return "lead"
    return None

# ---------------------------------------------------------------------------
# Owner UI
# ---------------------------------------------------------------------------

@app.route("/owner/login")
def owner_login_page():
    return send_from_directory(DASHBOARD_DIR, "login.html")

@app.route("/owner")
def owner_dashboard_page():
    if not auth.current_owner(ORBI_DIR):
        from flask import redirect
        return redirect("/owner/login")
    return send_from_directory(DASHBOARD_DIR, "dashboard.html")

@app.route("/api/owner/login", methods=["POST"])
def owner_login():
    """Login accepts either {username, password} (multi-user) or legacy
    {email, password} (single-owner installs pre-multi-user).
    On first multi-user login, bootstrap the legacy CONFIG.owner into
    users.json so subsequent logins go through the proper registry."""
    data = request.get_json(silent=True) or {}
    raw_id = (data.get("username") or data.get("email") or "").strip()
    password = data.get("password") or ""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    if not raw_id or not password:
        return jsonify({"error": "Username and password required"}), 400

    # First-time multi-user bootstrap: legacy single-owner install with
    # an entry in CONFIG.owner but no users.json yet.
    legacy_owner = CONFIG.get("owner", {})
    existing_users = users_mod.load_users(DATA_DIR)
    if not existing_users and legacy_owner.get("email"):
        legacy_email = legacy_owner["email"].lower()
        if raw_id.lower() in (legacy_email, legacy_email.split("@")[0]) \
           and auth.verify_password(password, legacy_owner.get("_password_hash", "")):
            bootstrap_username = legacy_email.split("@")[0] or "owner"
            try:
                users_mod.add_user(DATA_DIR, bootstrap_username, password,
                                   role="owner", display_name=legacy_owner.get("name", bootstrap_username))
                log.info(f"bootstrapped owner from legacy CONFIG: {bootstrap_username}")
                audit.log_event(DATA_DIR, actor=bootstrap_username,
                                action="owner.bootstrap_from_config")
            except ValueError as e:
                log.warning(f"bootstrap failed: {e}")

    # Standard multi-user verify path
    username = raw_id.split("@")[0].lower() if "@" in raw_id else raw_id.lower()
    user_rec = users_mod.verify_user(DATA_DIR, username, password)
    if not user_rec:
        audit.log_event(DATA_DIR, actor=username, action="owner.login.failed",
                        ip=ip, meta={"reason": "bad_credentials"})
        return jsonify({"error": "Invalid username or password"}), 401

    token = auth.issue_session(ORBI_DIR, username=user_rec["username"],
                               role=user_rec.get("role", "staff"))
    resp = make_response(jsonify({
        "status": "ok",
        "username": user_rec["username"],
        "role": user_rec["role"],
        "display_name": user_rec.get("display_name"),
    }))
    auth.set_session_cookie(resp, token)
    audit.log_event(DATA_DIR, actor=username, action="owner.login.success", ip=ip)
    return resp

@app.route("/api/owner/logout", methods=["POST"])
def owner_logout():
    owner_session = auth.current_owner(ORBI_DIR)
    if owner_session:
        audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                        action="owner.logout")
    resp = make_response(jsonify({"status": "ok"}))
    auth.clear_session_cookie(resp)
    return resp

@app.route("/api/owner/status")
def owner_status():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    business = mod_business.load(DATA_DIR)
    return jsonify({
        "username":      user["username"],
        "role":          user.get("role", "staff"),
        "display_name":  user.get("display_name"),
        "business_name": business.get("name") or CONFIG.get("business", {}).get("name", ""),
        "tier":          BILLING_STATUS.get("tier"),
        "active":        BILLING_STATUS.get("active"),
        "warning":       BILLING_STATUS.get("warning"),
        "period_end":    None,
        "connection":    llm_client.current_connection_state(CONFIG),
    })

@app.route("/api/owner/messages")
def owner_messages():
    auth.require_owner(ORBI_DIR)
    return jsonify({"messages": mod_messages.list_all(DATA_DIR)})

@app.route("/api/owner/messages/<msg_id>/read", methods=["POST"])
def owner_mark_read(msg_id):
    auth.require_owner(ORBI_DIR)
    ok = mod_messages.mark_read(DATA_DIR, msg_id)
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404

@app.route("/api/owner/messages/<msg_id>", methods=["DELETE"])
def owner_delete_message(msg_id):
    auth.require_owner(ORBI_DIR)
    ok = mod_messages.delete(DATA_DIR, msg_id)
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404

@app.route("/api/owner/business_info", methods=["GET"])
def owner_get_business():
    auth.require_owner(ORBI_DIR)
    return jsonify(mod_business.load(DATA_DIR))

@app.route("/api/owner/business_info", methods=["PUT"])
def owner_put_business():
    owner_session = auth.require_owner(ORBI_DIR)
    payload = request.get_json(silent=True) or {}
    existing = mod_business.load(DATA_DIR)
    before = dict(existing)
    existing.update(payload)
    mod_business.save(DATA_DIR, existing)
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="business_info.updated", resource="business_info",
                    before=before, after=existing)
    return jsonify({"status": "ok"})

@app.route("/api/owner/settings", methods=["GET"])
def owner_get_settings():
    auth.require_owner(ORBI_DIR)
    scope = CONFIG.get("scope", {}) or {}
    notify = CONFIG.get("notifications", {}) or {}
    business = mod_business.load(DATA_DIR)
    personality = business.get("personality", {}) or {}
    return jsonify({
        "tone": personality.get("tone", "friendly_professional"),
        "topics_to_avoid": scope.get("topics_to_avoid", []),
        **{k: scope.get(k, False) for k in (
            "public_can_take_orders", "public_can_book_appointments",
            "public_can_request_quotes", "public_can_request_callbacks",
        )},
        **{k: notify.get(k, False) for k in (
            "owner_pwa_push", "owner_email", "owner_sms",
            "notify_on_new_lead", "notify_on_new_message", "notify_on_failed_billing",
        )},
    })

@app.route("/api/owner/settings", methods=["PUT"])
def owner_put_settings():
    auth.require_owner(ORBI_DIR)
    payload = request.get_json(silent=True) or {}
    scope = CONFIG.setdefault("scope", {})
    notify = CONFIG.setdefault("notifications", {})
    scope["topics_to_avoid"] = payload.get("topics_to_avoid") or []
    for k in ("public_can_take_orders", "public_can_book_appointments",
              "public_can_request_quotes", "public_can_request_callbacks"):
        scope[k] = bool(payload.get(k))
    for k in ("owner_pwa_push", "owner_email", "owner_sms",
              "notify_on_new_lead", "notify_on_new_message", "notify_on_failed_billing"):
        notify[k] = bool(payload.get(k))
    # Personality tone is stored in business_info, not config
    if payload.get("tone"):
        biz = mod_business.load(DATA_DIR)
        biz.setdefault("personality", {})["tone"] = payload["tone"]
        mod_business.save(DATA_DIR, biz)
    save_config(CONFIG)
    return jsonify({"status": "ok"})

@app.route("/api/owner/workspace", methods=["GET"])
def owner_workspace_list():
    auth.require_owner(ORBI_DIR)
    return jsonify({
        "path": str(mod_workspace.workspace_path(CONFIG)),
        "files": mod_workspace.list_files(CONFIG, DATA_DIR),
    })

@app.route("/api/owner/workspace/scan", methods=["POST"])
def owner_workspace_scan():
    auth.require_owner(ORBI_DIR)
    return jsonify(mod_workspace.scan(CONFIG, DATA_DIR))


_ALLOWED_UPLOAD_EXTS = {
    ".txt", ".md", ".csv", ".log", ".json", ".html", ".htm",
    ".pdf", ".docx", ".xlsx",
    ".png", ".jpg", ".jpeg", ".gif",
}
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB per file


@app.route("/api/owner/workspace/upload", methods=["POST"])
def owner_workspace_upload():
    """Save uploaded files into the workspace folder and re-index. The owner's
    files NEVER leave the local computer — they sit in workspace_path() on disk
    and only get summarized into the index. Filenames are sanitized so a
    malicious upload can't write outside the workspace folder."""
    owner_session = auth.require_owner(ORBI_DIR)
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "no_files"}), 400

    import re as _re
    ws = mod_workspace.workspace_path(CONFIG)
    ws.mkdir(parents=True, exist_ok=True)

    saved = []
    rejected = []
    for f in files:
        original = (f.filename or "").strip()
        if not original:
            continue
        # Sanitize: keep basename only, strip any path components
        base = os.path.basename(original.replace("\\", "/"))
        # Strip leading dots and weird chars
        safe = _re.sub(r"[^\w\s.\-()]+", "", base).strip(" .")
        if not safe or safe.startswith("."):
            rejected.append({"name": original, "reason": "invalid_filename"})
            continue
        suffix = Path(safe).suffix.lower()
        if suffix not in _ALLOWED_UPLOAD_EXTS:
            rejected.append({"name": original, "reason": f"unsupported_type ({suffix or 'no extension'})"})
            continue
        dest = ws / safe
        # If a file with this name exists, append timestamp to avoid clobbering
        if dest.exists():
            stem, ext = dest.stem, dest.suffix
            dest = ws / f"{stem}__{int(time.time())}{ext}"
        try:
            f.save(str(dest))
            size = dest.stat().st_size
            if size > _MAX_UPLOAD_BYTES:
                dest.unlink(missing_ok=True)
                rejected.append({"name": original, "reason": f"too_large ({size} bytes, max {_MAX_UPLOAD_BYTES})"})
                continue
            saved.append({"name": dest.name, "size": size})
            log.info(f"workspace upload by {owner_session.get('username','?')}: "
                     f"{dest.name} ({size} bytes)")
        except Exception as e:
            rejected.append({"name": original, "reason": f"save_failed: {e}"})

    scan_result = mod_workspace.scan(CONFIG, DATA_DIR) if saved else {}
    audit.log_event(DATA_DIR,
                    actor=owner_session.get("username", "?"),
                    action="workspace.upload",
                    meta={"saved": [s["name"] for s in saved],
                          "rejected": [r["name"] for r in rejected]})
    return jsonify({
        "status": "ok",
        "saved": saved,
        "rejected": rejected,
        "indexed": scan_result.get("added", 0) + scan_result.get("updated", 0),
    })


@app.route("/api/owner/workspace/<path:filename>/convert", methods=["POST"])
def owner_workspace_convert(filename):
    """Clean a file with the LLM and write it back in a different format.
    Saves the result alongside the source in the workspace folder so it shows
    up in the Files tab and can be downloaded via /api/owner/files/request."""
    owner_session = auth.require_owner(ORBI_DIR)
    import re as _re
    safe = os.path.basename(filename.replace("\\", "/"))
    safe = _re.sub(r"[^\w\s.\-()]+", "", safe).strip(" .")
    if not safe:
        return jsonify({"error": "invalid_name"}), 400

    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").lower().strip()
    if target not in doc_convert.SUPPORTED_TARGETS:
        return jsonify({"error": "invalid_target",
                        "valid": list(doc_convert.SUPPORTED_TARGETS)}), 400
    hint = (data.get("hint") or "").strip()
    clean = bool(data.get("clean", True))

    ws = mod_workspace.workspace_path(CONFIG)
    src = ws / safe
    if not src.exists():
        return jsonify({"error": "source_not_found"}), 404

    try:
        result = doc_convert.convert(CONFIG, src, target, out_dir=ws,
                                     hint=hint, clean=clean)
    except (ValueError, FileNotFoundError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.warning(f"convert failed: {e}")
        return jsonify({"error": f"convert_failed: {e}"}), 500

    # Re-index workspace so the new file shows up in the Files tab
    try:
        mod_workspace.scan(CONFIG, DATA_DIR)
    except Exception as e:
        log.warning(f"post-convert scan failed: {e}")

    # Mint a one-time download token. The workspace folder is implicitly safe
    # for these tokens since Orby just wrote the file there itself — we pass
    # it as an extra allowed root so the owner doesn't have to widen their
    # global file-fetch scope just to download their own cleaned-up doc.
    try:
        token = file_fetch.mint_download_token(
            DATA_DIR, result["output_path"], ttl_minutes=30,
            extra_allowed_roots=[ws],
        )
        result["download_url"] = f"/download/{token}"
    except Exception as e:
        log.warning(f"could not mint download token: {e}")
        result["download_url"] = None

    audit.log_event(DATA_DIR, actor=owner_session.get("username", "?"),
                    action="workspace.convert",
                    resource=safe,
                    meta={"target": target, "output": result["output_name"],
                          "clean": clean})
    return jsonify({"status": "ok", **result})


@app.route("/api/owner/workspace/<path:filename>", methods=["DELETE"])
def owner_workspace_delete(filename):
    owner_session = auth.require_owner(ORBI_DIR)
    import re as _re
    # Sanitize same way as upload — no path traversal
    safe = os.path.basename(filename.replace("\\", "/"))
    safe = _re.sub(r"[^\w\s.\-()]+", "", safe).strip(" .")
    if not safe:
        return jsonify({"error": "invalid_name"}), 400
    target = mod_workspace.workspace_path(CONFIG) / safe
    if not target.exists():
        return jsonify({"error": "not_found"}), 404
    try:
        target.unlink()
        mod_workspace.scan(CONFIG, DATA_DIR)
        audit.log_event(DATA_DIR, actor=owner_session.get("username","?"),
                        action="workspace.delete", resource=safe)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/owner/change_password", methods=["POST"])
def owner_change_password():
    owner_session = auth.require_owner(ORBI_DIR)
    data = request.get_json(silent=True) or {}
    current = data.get("current", "")
    next_pw = data.get("next", "")
    owner = CONFIG.get("owner", {})
    if not auth.verify_password(current, owner.get("_password_hash", "")):
        audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                        action="owner.password_change.failed")
        return jsonify({"error": "Current password is incorrect"}), 401
    if len(next_pw) < 8:
        return jsonify({"error": "New password must be at least 8 characters"}), 400
    owner["_password_hash"] = auth.hash_password(next_pw)
    CONFIG["owner"] = owner
    save_config(CONFIG)
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="owner.password_changed")
    return jsonify({"status": "ok"})

@app.route("/api/owner/backup/set_passphrase", methods=["POST"])
def owner_backup_set_pw():
    owner_session = auth.require_owner(ORBI_DIR)
    data = request.get_json(silent=True) or {}
    passphrase = data.get("passphrase", "")
    try:
        backup.set_passphrase(DATA_DIR, passphrase)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="backup.passphrase_set")
    return jsonify({"status": "ok"})

@app.route("/api/owner/backup/run", methods=["POST"])
def owner_backup_run():
    owner_session = auth.require_owner(ORBI_DIR)
    data = request.get_json(silent=True) or {}
    passphrase = data.get("passphrase", "")
    key = backup.verify_passphrase(DATA_DIR, passphrase)
    if not key:
        return jsonify({"error": "wrong_passphrase"}), 401
    import base64
    bk_cfg = CONFIG.setdefault("backup", {})
    bk_cfg["_runtime_key_b64"] = base64.b64encode(key).decode("ascii")
    result = backup.run_backup(CONFIG, DATA_DIR)
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="backup.run", meta=result)
    return jsonify(result)

@app.route("/api/owner/backup/status", methods=["GET"])
def owner_backup_status():
    auth.require_owner(ORBI_DIR)
    state = backup._load_state(DATA_DIR)
    return jsonify({
        "passphrase_set": bool(state.get("salt")),
        "last_backup_ts": state.get("last_backup_ts"),
        "last_backup_name": state.get("last_backup_name"),
        "last_backup_bytes": state.get("last_backup_bytes"),
        "enabled": bool((CONFIG.get("backup") or {}).get("enabled")),
    })

@app.route("/api/owner/audit", methods=["GET"])
def owner_audit_view():
    owner_session = auth.require_owner(ORBI_DIR)
    limit = int(request.args.get("limit", "200"))
    return jsonify({
        "entries": audit.tail(DATA_DIR, limit=limit),
        "integrity": audit.verify_integrity(DATA_DIR),
    })

@app.route("/api/owner/audit.csv", methods=["GET"])
def owner_audit_csv():
    auth.require_owner(ORBI_DIR)
    csv_text = audit.export_csv(DATA_DIR)
    from flask import Response
    return Response(csv_text, mimetype="text/csv",
                    headers={"Content-Disposition":
                             'attachment; filename="orbi-audit.csv"'})

@app.route("/api/owner/chat", methods=["POST"])
def owner_chat():
    """User-scoped chat. Owner mode gets full personal-assistant access;
    staff get the same access scoped to their own per-user folder."""
    user_rec = auth.require_user(ORBI_DIR, DATA_DIR)
    username = user_rec["username"]
    user_dir = users_mod.get_user_dir(DATA_DIR, username)
    user_dir.mkdir(parents=True, exist_ok=True)

    data = request.get_json(silent=True) or {}
    user_msg = (data.get("message") or "").strip()
    history  = data.get("history") or []
    if not user_msg:
        return jsonify({"error": "empty message"}), 400

    # ── Fast-path personal-assistant intents (no LLM needed) ──────────────
    # Capability overview — answers "give me a full list of your capabilities"
    # without touching the LLM, so it works in offline mode.
    cap_overview = _try_capabilities_overview(user_msg)
    if cap_overview is not None:
        return jsonify({"reply": cap_overview, "tier": "local", "latency_ms": 0,
                        "source": "capabilities_overview"})

    # READ patterns: answer directly from the user's per-user data.
    pa_direct = _try_personal_assistant_read(user_msg, user_dir)
    if pa_direct is not None:
        return jsonify({"reply": pa_direct, "tier": "local", "latency_ms": 0,
                        "source": "personal_assistant"})

    # World time — 'what time is it in Tokyo'. Accurate to the second,
    # no LLM call. Falls through if the place isn't recognized.
    world_time = _try_world_time(user_msg)
    if world_time is not None:
        return jsonify({"reply": world_time, "tier": "local", "latency_ms": 0,
                        "source": "world_time"})

    # 'Why didn't I get the reminder' — explain her actual reminder
    # system honestly so she never hallucinates 'I can't set reminders'.
    rdiag = _try_reminder_diagnostic(user_msg, user_dir)
    if rdiag is not None:
        return jsonify({"reply": rdiag, "tier": "local", "latency_ms": 0,
                        "source": "reminder_diagnostic"})

    # List IMAP folders — diagnostic so the owner can see exactly which
    # folders Orby has access to and which folder each email lives in.
    folders_reply = _try_list_folders(user_msg, user_dir)
    if folders_reply is not None:
        return jsonify({"reply": folders_reply, "tier": "local", "latency_ms": 0,
                        "source": "list_folders"})

    # INBOX check — fast-path so "check my email" doesn't bounce to the LLM
    # which has no awareness of the connected IMAP/Gmail/Outlook accounts.
    inbox_reply = _try_inbox_check(user_msg, user_dir)
    if inbox_reply is not None:
        return jsonify({"reply": inbox_reply, "tier": "local", "latency_ms": 0,
                        "source": "inbox_check"})

    # FILE FETCH: "send me the Maxwell estimate from my computer"
    ff = _try_file_fetch(user_msg, username)
    if ff is not None:
        return jsonify(ff)

    # OFFICE GENERATION — chart / deck / image fast-paths
    office_result = _try_office_gen(user_msg, username)
    if office_result is not None:
        return jsonify(office_result)

    # CREATE patterns: route through quick_capture which classifies and files.
    qc_result = _try_quick_capture(user_msg, user_dir)
    if qc_result is not None:
        audit.log_event(DATA_DIR, actor=username, action=f"pa.capture.{qc_result['kind']}",
                        meta={"summary": qc_result.get("summary", "")})
        return jsonify({"reply": qc_result["summary"], "tier": "local", "latency_ms": 0,
                        "source": "quick_capture", "captured_as": qc_result["kind"]})

    business = mod_business.load(DATA_DIR)
    system = prompts.build_owner_prompt(business)

    # Owner mode gets memory + notes + workspace + per-user PA as extra context
    extras = []
    # "What can you do?" / "Walk me through X" — pull from the shipped
    # capabilities doc so the answer is grounded in real features.
    cap_ctx = _capabilities_context_block(user_msg)
    if cap_ctx:
        extras.append(cap_ctx)
    notes_ctx = mod_notes.context_block(DATA_DIR)
    if notes_ctx:
        extras.append(notes_ctx)
    memory_ctx = mod_memory.context_block(DATA_DIR)
    if memory_ctx:
        extras.append(memory_ctx)
    # Per-user personal-assistant context blocks
    for module_ctx in (mod_calendar.context_block(user_dir),
                       mod_reminders.context_block(user_dir),
                       mod_tasks.context_block(user_dir),
                       mod_contacts.context_block(user_dir)):
        if module_ctx:
            extras.append(module_ctx)
    # Same three-way priority as public chat
    msg_lower = user_msg.lower()
    is_fresh_query = any(k in msg_lower for k in tool_web_search._FRESH_KEYWORDS)
    workspace_hit = False
    try:
        ws_matches = mod_workspace.search(CONFIG, DATA_DIR, user_msg, limit=3)
        if ws_matches:
            top_score = ws_matches[0].get("score", 0)
            if top_score >= 3 or (top_score >= 1 and not is_fresh_query):
                workspace_hit = True
                ws_ctx = mod_workspace.context_block(CONFIG, DATA_DIR, user_msg)
                if ws_ctx:
                    extras.append(
                        "WORKSPACE FILES (authoritative — quote exactly):\n" + ws_ctx
                    )
    except Exception as e:
        log.warning(f"workspace context failed: {e}")
    if not workspace_hit and tool_web_search.needs_web_search(user_msg):
        try:
            web_ctx = tool_web_search.context_block(user_msg)
            if web_ctx:
                extras.append(
                    "WEB SEARCH RESULTS (current). Quote facts directly. "
                    "If the answer is NOT in these results, say so honestly.\n"
                    + web_ctx
                )
                log.info(f"web search invoked for: {user_msg[:60]!r}")
        except Exception as e:
            log.warning(f"web search failed: {e}")

    # URL FETCH — owner pasted a specific URL? Fetch it and quote from it.
    try:
        urls_in_msg = tool_url_fetch.extract_urls(user_msg)[:3]
        if urls_in_msg:
            fetched = [tool_url_fetch.fetch_or_search(u) for u in urls_in_msg]
            url_ctx = tool_url_fetch.context_block(fetched)
            if url_ctx:
                extras.append(url_ctx)
                log.info(f"owner url_fetch: {len(urls_in_msg)} url(s)")
    except Exception as e:
        log.warning(f"owner url_fetch failed: {e}")

    if extras:
        system += "\n\n" + "\n\n".join(extras)

    messages = [m for m in history if m.get("role") in ("user", "assistant")][-20:]
    messages.append({"role": "user", "content": user_msg})

    # Owner/staff rate limit — per-user daily cap protects the HF budget.
    _rl_ok, _rl_used, _rl_cap = rate_limit.check_and_increment(
        username, role=user_rec.get("role", "staff"))
    if not _rl_ok:
        log.warning(f"owner chat rate-limited: {username} {_rl_used}/{_rl_cap}")
        return jsonify({
            "reply": (f"Daily AI-chat limit reached ({_rl_used}/{_rl_cap}). "
                      f"This protects your LLM budget from accidental loops. "
                      f"Resets at midnight UTC. Fast-path commands "
                      f"(tasks, calendar, contacts) still work."),
            "tier": "rate_limited", "latency_ms": 0,
        })

    resp = llm_client.generate(CONFIG, system, messages)

    # Auto-remember key facts the owner mentions (very simple heuristic for v1)
    if any(p in user_msg.lower() for p in ("remember that", "make a note", "don't forget")):
        try:
            mod_notes.add(DATA_DIR, user_msg, tags=["auto"])
        except Exception as e:
            log.warning(f"auto-note add failed: {e}")

    return jsonify({
        "reply": resp.text or "I couldn't reach any AI tier just now. Please try again.",
        "tier": resp.tier,
        "latency_ms": resp.latency_ms,
    })


# ---------------------------------------------------------------------------
# Personal-assistant fast-path helpers (no LLM needed for these intents)
# ---------------------------------------------------------------------------

_PA_TODAY_RE = _re.compile(
    r"\bwhat(?:'s|s| is)?\s+(?:on\s+)?(?:my\s+)?(?:calendar|schedule|agenda)?\s*(?:for\s+)?today\b|"
    r"\b(?:what(?:'s|s| is) on|what do i have)\s+(?:going on\s+)?today\b|"
    r"\bany(?:thing|meetings)\s+today\b",
    _re.IGNORECASE,
)
_PA_WEEK_RE = _re.compile(
    r"\bwhat(?:'s|s| is)?\s+(?:on\s+)?(?:my\s+)?(?:calendar|schedule|agenda)?\s*(?:for\s+)?(?:this\s+)?(?:week|next\s+week|upcoming)\b|"
    r"\bupcoming\s+(?:events|meetings|appointments)\b",
    _re.IGNORECASE,
)
_PA_TASKS_RE = _re.compile(
    r"\b(?:show|list|what(?:'s|s| are))\s+(?:me\s+)?(?:my\s+)?(?:open\s+)?"
    r"(?:todo|to[\s-]?do|tasks?)(?:\s+list)?\b",
    _re.IGNORECASE,
)
_PA_REMINDERS_RE = _re.compile(
    r"\b(?:show|list|what(?:'s|s| are))\s+(?:me\s+)?(?:my\s+)?(?:pending\s+)?"
    r"reminders?\b"
    r"|\bwhat\s+do\s+i\s+need\s+to\s+be\s+reminded\s+(?:about|of)\b",
    _re.IGNORECASE,
)
_PA_WHO_IS_RE = _re.compile(
    r"\bwho\s+is\s+(?P<name>[A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+){0,2})\b",
)
_PA_STAFF_RE = _re.compile(
    # "who's on/in my staff", "show me my staff", "list my staff",
    # "who works for me", "who are my employees", etc.
    r"\b(?:who(?:'s|s| is| are)\s+(?:(?:on|in)\s+)?(?:my\s+)?(?:staff|employees|team)\b"
    r"|(?:show|list|tell\s+me\s+about)\s+(?:me\s+)?(?:my\s+)?(?:staff|employees|team)\b"
    r"|who\s+works\s+(?:for\s+me|here)\b)",
    _re.IGNORECASE,
)
_PA_PHONE_OF_RE = _re.compile(
    r"\b(?:what(?:'s|s| is)?|find|get|look\s*up)\s+"
    r"(?P<name>[A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+){0,2})"
    r"(?:'s|s')?\s+"
    r"(?:phone|number|email|contact)\b",
)


# Phrases that mean "teach me what Orby can do" or "walk me through how".
# Conservative on purpose — a vague "help" alone shouldn't trigger.
_TEACH_INTENT_PATTERNS = [
    _re.compile(r"\bwhat\s+(can|do)\s+you\s+do\b", _re.I),
    _re.compile(r"\bwhat\s+(are\s+your\s+)?capabilities\b", _re.I),
    _re.compile(r"\bshow\s+me\s+how\s+(to|do)\b", _re.I),
    _re.compile(r"\bwalk\s+me\s+through\b", _re.I),
    _re.compile(r"\bteach\s+me\s+(how|about)\b", _re.I),
    _re.compile(r"\bhow\s+do\s+i\s+(?!feel|look)\b", _re.I),
    _re.compile(r"\bquick\s+tour\b", _re.I),
]


def _is_teach_intent(message: str) -> bool:
    return any(p.search(message or "") for p in _TEACH_INTENT_PATTERNS)


# Cache the markdown content so we don't re-read on every chat turn.
_CAPABILITIES_CACHE: dict = {"text": None, "mtime": 0.0}


def _load_capabilities_doc() -> str:
    """Return the orbi_capabilities.md text. Re-reads on file mtime change so
    edits to the doc (or a customer override at ORBI_DIR/orbi_capabilities.md)
    take effect without restarting Orby."""
    candidates = [
        ORBI_DIR / "orbi_capabilities.md",
        Path(__file__).parent / "orbi_capabilities.md",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            mtime = p.stat().st_mtime
            if _CAPABILITIES_CACHE["text"] and mtime == _CAPABILITIES_CACHE["mtime"]:
                return _CAPABILITIES_CACHE["text"]
            text = p.read_text(encoding="utf-8")
            _CAPABILITIES_CACHE["text"]  = text
            _CAPABILITIES_CACHE["mtime"] = mtime
            return text
        except OSError:
            continue
    return ""


def _capabilities_context_block(message: str) -> str | None:
    """When the owner is asking what Orby can do or how to do something,
    inject the capabilities doc into the prompt so the answer is grounded
    in the documented features (not invented). Returns None when the user
    isn't asking a teach-style question."""
    if not _is_teach_intent(message):
        return None
    doc = _load_capabilities_doc()
    if not doc:
        return None
    return (
        "ORBI CAPABILITIES (authoritative — answer from this doc, do not invent "
        "features). When walking the user through something, give 3-6 concrete "
        "steps with the EXACT example phrases from the doc. If they asked a "
        "broad question like 'what can you do', give a short overview (5-7 "
        "headline capabilities) then ask which area they'd like to dive into.\n\n"
        + doc
    )


# Voice-to-text spells Orby as "Orbeez" / "orby" / "orbie" sometimes —
# normalize so capability queries still match.
_ORBI_PHONETIC_RE = _re.compile(r"\borb(?:eez|y|ie|i)\b", _re.IGNORECASE)
_CAPABILITIES_RE = _re.compile(r"\bcapabilit(?:y|ies)\b", _re.IGNORECASE)
_WHAT_CAN_YOU_DO_RE = _re.compile(r"\bwhat\s+(?:can|do)\s+you\s+do\b", _re.IGNORECASE)


def _try_capabilities_overview(message: str) -> str | None:
    """Fast-path for 'list your capabilities' / 'what can you do' that
    works even when the LLM is offline. Returns a curated overview text
    directly from the shipped doc — no LLM call needed."""
    if not message:
        return None
    # Run polite-prefix strip + Orbeez→Orby normalization first, then test.
    cleaned = _strip_polite_prefix(message)
    cleaned = _ORBI_PHONETIC_RE.sub("orbi", cleaned)
    if not (_CAPABILITIES_RE.search(cleaned) or _WHAT_CAN_YOU_DO_RE.search(cleaned)):
        return None
    doc = _load_capabilities_doc()
    if not doc:
        return None
    # Pull H1, H2 and H3 headers — but skip the document title (the very
    # first H1) and admin-y sections users don't care about as "things
    # she can do".
    lines = doc.split("\n")
    sections = []
    saw_first_h1 = False
    for ln in lines:
        if ln.startswith("# ") and not ln.startswith("## "):
            if not saw_first_h1:
                saw_first_h1 = True
                continue
            sections.append(ln.lstrip("# ").strip())
        elif ln.startswith("## ") or ln.startswith("### "):
            sections.append(ln.lstrip("# ").strip())
    if not sections:
        return None
    skip = {
        "quick start — your first 10 minutes",
        "when something's wrong",
        "what orby won't do",
    }
    bullets = "\n".join(f"  • {s}" for s in sections
                        if s and s.lower() not in skip)
    return ("Here's everything I can do — pick any one and I'll walk you "
            "through it (or open the **Help** tab for the full guide with "
            "example phrases):\n\n" + bullets)


_PA_INBOX_RE = _re.compile(
    # Covers: 'check my email', 'check that email', 'show me my email',
    # 'read me my email', 'any new emails?', 'what's in my inbox',
    # 'do I have any new mail', 'what's important in my inbox'.
    # "(?:e[\s-]?mails?|inbox|mails?|messages?)" tolerates plurals.
    r"\b(?:check|read|show|list|see|fetch|get|pull|look\s+at|tell\s+me\s+about)"
    r"\s+(?:me\s+)?(?:my\s+|the\s+|that\s+|some\s+)?(?:e[\s-]?mails?|inbox|mails?|messages?)\b"
    r"|\b(?:any|got|have|do\s+i\s+have)\s+(?:any\s+)?(?:new\s+)?(?:e[\s-]?mails?|mails?|messages?)\b"
    r"|\bwhat(?:'s|s| is)?\s+(?:important\s+)?(?:in|on)\s+(?:my\s+)?(?:e[\s-]?mails?|inbox|mails?)\b"
    r"|\bwhat(?:'s|s| is)\s+new\s+in\s+(?:my\s+)?(?:e[\s-]?mails?|inbox|mails?)\b",
    _re.IGNORECASE,
)

# Capture the explicit count when the user asks for a specific number of
# emails: 'show me my last 25 emails', 'give me 10 messages', 'last 50 emails'.
_PA_INBOX_COUNT_RE = _re.compile(
    r"\b(?:show|list|read|give|fetch|pull|see|get|tell)\s+(?:me\s+)?"
    r"(?:my\s+)?(?:last\s+|recent\s+|latest\s+|top\s+)?"
    r"(?P<n>\d{1,3})\s+"
    r"(?:e[\s-]?mails?|messages?|mails?)\b",
    _re.IGNORECASE,
)


# Sender local-parts that almost never represent a real person.
# We hide emails from these by default. The user can override with
# "show all my email" / "include newsletters" / etc.
_NOISE_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "do_not_reply",
    "marketing", "newsletter", "newsletters", "news", "updates",
    "notifications", "notification", "alerts", "alert", "deals", "promo",
    "promos", "promotions", "offers", "ads", "sales", "info",
    "auto", "automated", "auto-reply", "system",
}
_NOISE_SUBJECT_RE = _re.compile(
    r"\b(?:\d{1,3}\s*%\s*off|off!|on\s+sale|limited\s+time|free\s+shipping|"
    r"exclusive\s+(?:deal|offer)|memorial\s+day\s+sale|labor\s+day\s+sale|"
    r"unsubscribe|cyber\s+monday|black\s+friday|flash\s+sale|"
    r"don'?t\s+miss|act\s+now|today\s+only|hurry|"
    r"open\s+rate|click\s+here|claim\s+now|invitation\s+to\s+earn|"
    r"survey|webinar|"
    # extra patterns caught in real-world testing:
    r"\$\d+\s+down|no\s+perfect\s+credit|apply\s+for\s+the|"
    r"qualified\s+for|qualify\s+for|up\s+to\s+\d+%|save\s+(?:up\s+to|\d+)|"
    r"discount|coupon|special\s+offer|grand\s+opening|new\s+arrival|"
    r"summer.?ready|new\s+listings|cover\s+your\s+vehicle|"
    r"betting\s+outlook|mlb\s+plays|free\s+trial|risk[- ]?free|"
    r"\bad\s+credit\b|just\s+for\s+you|today's\s+outlook|"
    r"membership\s+deal|metal\s+roof|insurance\s+options|"
    r"social\s+security\s+update|benefits\s+now\s+available|"
    r"flexible\s+pricing|cost-effective|"
    r"final\s+(?:offer|opportunity|hours)|smb\s+month|"
    r"phone\s+making|making\s+more\s+money|"
    r"steady\s+energy|gut\s+doctor|"
    r"perfect\s+credit\s*needed|reliable\s+and\s+cost)\b",
    _re.IGNORECASE,
)
# Sender display-name patterns that scream "marketing list"
_NOISE_DISPLAY_NAME_RE = _re.compile(
    r"\b(?:save\s+big|cheap|discount|insure|insurance|vehicle\s+protection|"
    r"protection\s+usa|roofing|mastercard|credit\s+card|cash\s+for\s+you|"
    r"endurance\s+auto|seniors?\s+(?:save|discount)|deals?|"
    r"quote\s*reduction|loans?\s+(?:for|to)|sweepstake|"
    r"ballot\s+news|betting\s+daily|inside.?vegas|"
    r"\bads?\b|advertising|advertiser|coach\s+\(re\)?loved|"
    r"morning\s+health\s+fix|ancient\s+remedies|"
    r"casino|gambling|sports?\s+book|free\s+spins?)\b",
    _re.IGNORECASE,
)
# Subjects with these stay — even if other heuristics would hide them.
_KEEP_SUBJECT_RE = _re.compile(
    r"\b(?:invoice|receipt|order|payment|refund|appointment|meeting|"
    r"booking|reservation|delivery|shipped|tracking|security\s+alert|"
    r"sign[- ]?in|verify|verification|password\s+(?:was|changed|reset)|"
    r"two[- ]factor|2fa|action\s+required|account|deposit|paid|"
    r"thank\s+you|quote|estimate|lead|inquiry|contract|signed|"
    r"complaint|cancellation)\b",
    _re.IGNORECASE,
)


def _is_promotional(msg: dict) -> bool:
    """Quick heuristic to decide whether an email is noisy newsletter/promo
    junk vs something a human owner cares about. Conservative: when in
    doubt, treat as personal (false negatives are MUCH better than hiding
    a real lead)."""
    subj = (msg.get("subject") or "").strip()
    sender = (msg.get("from") or "").lower()

    # KEEP overrides — receipts, security, real-business signals
    if _KEEP_SUBJECT_RE.search(subj):
        return False

    # Extract local-part from From header
    local = ""
    if "<" in sender and ">" in sender:
        addr = sender.split("<", 1)[1].split(">", 1)[0]
    else:
        addr = sender
    if "@" in addr:
        local = addr.split("@", 1)[0]

    if local in _NOISE_LOCAL_PARTS:
        return True
    # Local-parts that LOOK auto-generated: long random strings, lots of
    # digits, hyphenated marketing-y compounds
    if local and len(local) > 18 and any(c.isdigit() for c in local):
        return True
    # Subject contains classic promo language
    if _NOISE_SUBJECT_RE.search(subj):
        return True
    # Display name contains marketing-list signals
    display_name = sender.split("<", 1)[0].strip(' "')
    if display_name and _NOISE_DISPLAY_NAME_RE.search(display_name):
        return True
    # Subject starts with "Frank, $X" / "Frank, $300" — classic personalized
    # marketing pattern
    if _re.match(r"^frank,?\s*\$\d", subj.strip(), _re.IGNORECASE):
        return True
    return False


_PA_REMINDER_DIAGNOSTIC_RE = _re.compile(
    r"\bwhy\s+(?:didn'?t|did\s+not)\s+i\s+(?:get|see|hear)\s+(?:the|a|my)?\s*reminder\b"
    r"|\bdid\s+(?:you|orby)\s+remind\s+me\b"
    r"|\bwhere'?s\s+my\s+reminder\b"
    r"|\bwhat\s+happened\s+to\s+(?:the|my)\s+reminder\b"
    r"|\bdid\s+my\s+reminder\s+(?:fire|go\s+off|come)\b",
    _re.IGNORECASE,
)


def _try_reminder_diagnostic(message: str, user_dir: Path) -> str | None:
    """Honest answer for 'why didn't I get the reminder I just set'.
    Without this, the LLM hallucinates 'I don't have the capability to
    set reminders that trigger outside of our conversation' — total
    fabrication because she literally has a firing worker and a toast/
    voice/chat-bubble pipeline."""
    if not message or not _PA_REMINDER_DIAGNOSTIC_RE.search(message):
        return None
    try:
        items = mod_reminders.list_all(user_dir)
    except Exception as e:
        return f"I couldn't read your reminders to check: {e}"
    pending = [r for r in items if r.get("status") == "pending"]
    fired = [r for r in items if r.get("status") == "fired"]
    notify_cfg = (CONFIG.get("notifications") or {})
    channels = []
    if notify_cfg.get("owner_pwa_push", True):
        channels.append("web push (if you installed the PWA + allowed notifications)")
    if notify_cfg.get("owner_email") and (CONFIG.get("owner") or {}).get("email"):
        channels.append("email")
    if notify_cfg.get("owner_sms") and (CONFIG.get("owner") or {}).get("phone"):
        channels.append("SMS")
    lines = [
        "My reminder system IS working — here's what's actually happening:",
        "",
        f"  • Pending reminders: {len(pending)}",
        f"  • Fired (already triggered): {len(fired)}",
        "",
        "When a reminder fires, I do ALL of these:",
        "  1. Play a chime",
        "  2. Speak it out loud (TTS): 'Hey Frank, this is your reminder. <body>'",
        "  3. Show a big yellow pulsing banner with Got It + Snooze buttons",
        "  4. Drop a ⏰ message into the Ask Orby chat history",
        "  5. Repeat the chime + voice every 3 min (up to 3x) until you click Got It",
    ]
    if fired:
        lines.append("")
        lines.append("Most recent fires:")
        for r in fired[-3:]:
            fired_at = _fmt_email_date(r.get("fired_at", ""))
            lines.append(f"  • {fired_at} — \"{r.get('text','')}\"")
    lines.append("")
    if channels:
        lines.append("External channels configured: " + ", ".join(channels))
    else:
        lines.append("⚠ No external channels (push / email / SMS) are configured "
                     "in your notification settings — that means the in-app toast "
                     "+ chime + voice are your ONLY signals. If the dashboard tab "
                     "is closed or the chime is muted, you'd miss it.")
    lines.append("")
    lines.append("If you DIDN'T see/hear the in-app signals: refresh the dashboard "
                 "and try a 1-min test reminder. If you still don't, that's a real "
                 "bug — tell me and I'll dig in.")
    return "\n".join(lines)


# Capture 'emails from X' / 'any new emails from Bill'
_PA_EMAIL_FROM_RE = _re.compile(
    r"\b(?:any\s+(?:new\s+)?|got\s+|do\s+i\s+have\s+(?:any\s+)?)?"
    r"(?:e[\s-]?mails?|messages?|mails?)\s+from\s+"
    r"(?P<sender>[A-Za-z][A-Za-z0-9\s.@'-]+?)\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


_PA_INBOX_INCLUDE_ALL_RE = _re.compile(
    r"\b(?:include|with|show)\s+(?:newsletters?|promos?|promotions?|all|everything)\b"
    r"|\b(?:show|see)\s+(?:me\s+)?(?:all|everything)\b"
    r"|\bincluding\s+(?:newsletters?|promos?)\b",
    _re.IGNORECASE,
)


def _fmt_email_date(iso: str) -> str:
    """Turn the email's Date header into a friendly local-time string the
    owner can compare against what they see in their inbox UI."""
    if not iso:
        return ""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    try:
        # Python 3.10's fromisoformat doesn't accept trailing 'Z' (it
        # expects '+00:00'); strip it so reminder UTC timestamps like
        # '2026-05-29T00:00:00Z' parse instead of falling to the
        # raw-UTC fallback.
        s = iso.rstrip().replace("Z", "+00:00")
        dt = _dt.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        local = dt.astimezone()
        now_local = _dt.now().astimezone()
        same_day = local.date() == now_local.date()
        yesterday = local.date() == (now_local - _td(days=1)).date()
        time_str = local.strftime("%I:%M %p").lstrip("0").lower()
        if same_day:
            return time_str
        if yesterday:
            return "yesterday " + time_str
        tomorrow = local.date() == (now_local + _td(days=1)).date()
        if tomorrow:
            return "tomorrow " + time_str
        if local.year == now_local.year:
            return local.strftime("%a %b %d ").lower() + time_str
        return local.strftime("%Y-%m-%d ").lower() + time_str
    except (ValueError, TypeError):
        return iso[:16].replace("T", " ")


def _try_inbox_check(message: str, user_dir: Path) -> str | None:
    """Detect 'check my email / what's in my inbox' and pull a live summary
    directly from email_inbox.fetch_inbox. Without this fast-path the
    request bounces to the LLM, which has no idea the IMAP/Gmail/Outlook
    accounts are connected and tells the user 'I don't have access'."""
    # Match either the broad inbox-check pattern, the explicit-count one,
    # or the "emails from X" sender filter.
    count_match  = _PA_INBOX_COUNT_RE.search(message or "")
    sender_match = _PA_EMAIL_FROM_RE.search(message or "")
    if not message or (not _PA_INBOX_RE.search(message)
                       and not count_match and not sender_match):
        return None
    sender_filter = None
    if sender_match:
        sender_filter = sender_match.group("sender").strip().rstrip("?.!").strip()

    # If the user said "show me 25 emails", honor that count exactly. Cap at
    # 200 so we don't accidentally pull the entire inbox.
    requested_n = None
    if count_match:
        try:
            requested_n = max(1, min(200, int(count_match.group("n"))))
        except ValueError:
            requested_n = None
    fetch_limit = max(100, (requested_n or 0) * 2)  # pull extra so the filter
                                                    # can hide promos and still
                                                    # leave the requested count

    try:
        result = email_inbox.fetch_inbox(CONFIG, user_dir, source="all",
                                         limit=fetch_limit, force_refresh=True)
    except Exception as e:
        log.warning(f"inbox fetch_inbox failed: {e}")
        return f"I tried to check your inbox but hit an error: {e}"

    messages = result.get("messages") or []
    errors   = result.get("errors") or {}
    # Sender filter — narrow to just the sender the user asked about
    if sender_filter:
        sf = sender_filter.lower()
        messages = [m for m in messages
                    if sf in (m.get("from") or "").lower()]
        if not messages:
            return (f"I don't see any recent emails from \"{sender_filter}\" "
                    f"in your inbox. (Check the spelling, or try a partial "
                    f"match like just the first name.)")
    if not messages:
        msg = "Your inbox is empty (or I can't see anything new)."
        if errors:
            details = "; ".join(f"{k}: {v[:80]}" for k, v in errors.items())
            msg += f" Errors: {details}"
        return msg

    # When the user asks for a specific N emails OR for emails from a
    # specific sender, treat as 'include all' — don't pre-filter promos
    # or the count won't match / the sender filter will look broken.
    if requested_n is not None or sender_filter is not None:
        show_all = True
    else:
        show_all = bool(_PA_INBOX_INCLUDE_ALL_RE.search(message))
    if show_all:
        kept = messages
        hidden_promo = []
    else:
        kept = []
        hidden_promo = []
        for m in messages:
            (hidden_promo if _is_promotional(m) else kept).append(m)

    by_provider = result.get("by_provider") or {}
    unread = sum(1 for m in kept if m.get("unread"))
    important = [m for m in kept if m.get("flagged") or
                 any(t in ("lead", "urgent", "complaint") for t in (m.get("tags") or []))]

    if show_all:
        lines = [f"You have {len(messages)} recent messages ({unread} unread)."]
    else:
        lines = [f"You have {len(kept)} important / personal messages "
                 f"({unread} unread). I filtered out {len(hidden_promo)} "
                 f"newsletters & promos — say 'show me everything' to see them."]
    if by_provider:
        lines.append("Sources: " + ", ".join(f"{k} ({v})" for k, v in by_provider.items()) + ".")

    if important:
        lines.append("")
        lines.append(f"⚡ {len(important)} flagged as important:")
        for m in important[:5]:
            date_str = _fmt_email_date(m.get("date", ""))
            lines.append(f"  - [{date_str}] {m.get('from','?')[:35]}: "
                         f"{m.get('subject','(no subject)')[:55]}")

    lines.append("")
    if requested_n is not None:
        show_n = min(len(kept), requested_n)
        lines.append(f"Your last {show_n} emails:")
    else:
        show_n = min(len(kept), 40)
        label = "everything" if show_all else "newest (filtered)"
        lines.append(f"Top {show_n} {label}:")
    for m in kept[:show_n]:
        unread_mark = "● " if m.get("unread") else "  "
        date_str = _fmt_email_date(m.get("date", ""))
        sender  = (m.get("from") or "?")[:26]
        subject = (m.get("subject") or "(no subject)")[:46]
        folder  = m.get("folder") or m.get("provider") or "?"
        lines.append(f"  {unread_mark}[{folder:<6} {date_str:>13}]  {sender:<26} — {subject}")

    if not show_all and hidden_promo:
        lines.append("")
        lines.append(f"📭 Hidden ({len(hidden_promo)} promos / newsletters):")
        # Just list the senders so Frank knows who's been emailing him
        senders = {}
        for m in hidden_promo:
            s = (m.get("from") or "?").split("<", 1)[0].strip(' "')
            senders[s] = senders.get(s, 0) + 1
        sender_list = sorted(senders.items(), key=lambda x: -x[1])
        for s, n in sender_list[:8]:
            lines.append(f"  - {s[:50]}" + (f" ({n})" if n > 1 else ""))
        if len(sender_list) > 8:
            lines.append(f"  ...and {len(sender_list) - 8} more")

    if errors:
        lines.append("")
        lines.append("(Some sources errored: " +
                     ", ".join(f"{k}={v[:50]}" for k, v in errors.items()) + ")")

    return "\n".join(lines)


# ─── World-time fast-path ──────────────────────────────────────────────
# Orby should know the local time anywhere in the world. We map city /
# country names to IANA timezones and look up the live time with
# zoneinfo. No LLM call — accurate to the second.
_WORLD_TIMEZONES = {
    # United States (cities + states)
    "new york": "America/New_York", "nyc": "America/New_York",
    "boston": "America/New_York", "philadelphia": "America/New_York",
    "miami": "America/New_York", "atlanta": "America/New_York",
    "washington": "America/New_York", "washington dc": "America/New_York",
    "florida": "America/New_York", "georgia": "America/New_York",
    "new york city": "America/New_York",
    "chicago": "America/Chicago", "dallas": "America/Chicago",
    "houston": "America/Chicago", "austin": "America/Chicago",
    "new orleans": "America/Chicago", "texas": "America/Chicago",
    "minneapolis": "America/Chicago",
    "denver": "America/Denver", "salt lake city": "America/Denver",
    "phoenix": "America/Phoenix", "arizona": "America/Phoenix",
    "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "sf": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "portland": "America/Los_Angeles",
    "reno": "America/Los_Angeles", "las vegas": "America/Los_Angeles",
    "vegas": "America/Los_Angeles", "san diego": "America/Los_Angeles",
    "california": "America/Los_Angeles", "nevada": "America/Los_Angeles",
    "oregon": "America/Los_Angeles", "washington state": "America/Los_Angeles",
    "anchorage": "America/Anchorage", "alaska": "America/Anchorage",
    "honolulu": "Pacific/Honolulu", "hawaii": "Pacific/Honolulu",
    # Canada
    "toronto": "America/Toronto", "ottawa": "America/Toronto",
    "montreal": "America/Montreal", "vancouver": "America/Vancouver",
    "calgary": "America/Edmonton",
    # Mexico
    "mexico city": "America/Mexico_City", "mexico": "America/Mexico_City",
    # UK + Europe
    "london": "Europe/London", "manchester": "Europe/London",
    "edinburgh": "Europe/London", "dublin": "Europe/Dublin",
    "uk": "Europe/London", "england": "Europe/London", "ireland": "Europe/Dublin",
    "paris": "Europe/Paris", "france": "Europe/Paris",
    "berlin": "Europe/Berlin", "germany": "Europe/Berlin",
    "madrid": "Europe/Madrid", "spain": "Europe/Madrid",
    "barcelona": "Europe/Madrid",
    "rome": "Europe/Rome", "italy": "Europe/Rome", "milan": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam", "netherlands": "Europe/Amsterdam",
    "brussels": "Europe/Brussels", "belgium": "Europe/Brussels",
    "athens": "Europe/Athens", "greece": "Europe/Athens",
    "moscow": "Europe/Moscow", "russia": "Europe/Moscow",
    "stockholm": "Europe/Stockholm", "sweden": "Europe/Stockholm",
    "oslo": "Europe/Oslo", "norway": "Europe/Oslo",
    "copenhagen": "Europe/Copenhagen", "denmark": "Europe/Copenhagen",
    "warsaw": "Europe/Warsaw", "poland": "Europe/Warsaw",
    "lisbon": "Europe/Lisbon", "portugal": "Europe/Lisbon",
    "vienna": "Europe/Vienna", "austria": "Europe/Vienna",
    "zurich": "Europe/Zurich", "switzerland": "Europe/Zurich",
    "istanbul": "Europe/Istanbul", "turkey": "Europe/Istanbul",
    "kyiv": "Europe/Kyiv", "kiev": "Europe/Kyiv", "ukraine": "Europe/Kyiv",
    # Middle East
    "dubai": "Asia/Dubai", "uae": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai", "united arab emirates": "Asia/Dubai",
    "riyadh": "Asia/Riyadh", "saudi arabia": "Asia/Riyadh",
    "tel aviv": "Asia/Jerusalem", "jerusalem": "Asia/Jerusalem",
    "israel": "Asia/Jerusalem",
    "tehran": "Asia/Tehran", "iran": "Asia/Tehran",
    # Asia
    "tokyo": "Asia/Tokyo", "osaka": "Asia/Tokyo", "japan": "Asia/Tokyo",
    "seoul": "Asia/Seoul", "korea": "Asia/Seoul", "south korea": "Asia/Seoul",
    "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai",
    "china": "Asia/Shanghai", "hong kong": "Asia/Hong_Kong",
    "taipei": "Asia/Taipei", "taiwan": "Asia/Taipei",
    "singapore": "Asia/Singapore",
    "bangkok": "Asia/Bangkok", "thailand": "Asia/Bangkok",
    "jakarta": "Asia/Jakarta", "indonesia": "Asia/Jakarta",
    "manila": "Asia/Manila", "philippines": "Asia/Manila",
    "kuala lumpur": "Asia/Kuala_Lumpur", "malaysia": "Asia/Kuala_Lumpur",
    "ho chi minh city": "Asia/Ho_Chi_Minh", "saigon": "Asia/Ho_Chi_Minh",
    "hanoi": "Asia/Ho_Chi_Minh", "vietnam": "Asia/Ho_Chi_Minh",
    "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata", "kolkata": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata", "bengaluru": "Asia/Kolkata",
    "chennai": "Asia/Kolkata", "india": "Asia/Kolkata",
    "karachi": "Asia/Karachi", "lahore": "Asia/Karachi",
    "islamabad": "Asia/Karachi", "pakistan": "Asia/Karachi",
    "dhaka": "Asia/Dhaka", "bangladesh": "Asia/Dhaka",
    # Australia + Pacific
    "sydney": "Australia/Sydney", "canberra": "Australia/Sydney",
    "melbourne": "Australia/Melbourne", "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth", "australia": "Australia/Sydney",
    "auckland": "Pacific/Auckland", "wellington": "Pacific/Auckland",
    "new zealand": "Pacific/Auckland",
    # South America
    "sao paulo": "America/Sao_Paulo", "rio": "America/Sao_Paulo",
    "rio de janeiro": "America/Sao_Paulo", "brazil": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires", "argentina": "America/Argentina/Buenos_Aires",
    "santiago": "America/Santiago", "chile": "America/Santiago",
    "lima": "America/Lima", "peru": "America/Lima",
    "bogota": "America/Bogota", "colombia": "America/Bogota",
    "caracas": "America/Caracas", "venezuela": "America/Caracas",
    # Africa
    "lagos": "Africa/Lagos", "nigeria": "Africa/Lagos",
    "johannesburg": "Africa/Johannesburg", "south africa": "Africa/Johannesburg",
    "cape town": "Africa/Johannesburg",
    "cairo": "Africa/Cairo", "egypt": "Africa/Cairo",
    "nairobi": "Africa/Nairobi", "kenya": "Africa/Nairobi",
    "addis ababa": "Africa/Addis_Ababa", "ethiopia": "Africa/Addis_Ababa",
    "casablanca": "Africa/Casablanca", "morocco": "Africa/Casablanca",
    # Common abbreviations
    "est": "America/New_York", "edt": "America/New_York",
    "cst": "America/Chicago", "cdt": "America/Chicago",
    "mst": "America/Denver", "mdt": "America/Denver",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "gmt": "Etc/GMT", "utc": "Etc/UTC",
}

_TIME_IN_PLACE_RE = _re.compile(
    r"\b(?:what(?:'s|s| is)\s+|current\s+|the\s+)?"
    r"time\s+(?:is\s+it\s+|right\s+now\s+)?(?:in|at)\s+"
    r"(?P<place>[A-Za-z][A-Za-z\s\-/_.]+?)\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _try_world_time(message: str) -> str | None:
    """Answer 'what time is it in X' from zoneinfo, no LLM call needed.
    Falls through to None if the place isn't recognized so the LLM gets
    a chance (and an honest 'I don't have that timezone' response is the
    expected behavior for obscure places)."""
    if not message:
        return None
    m = _TIME_IN_PLACE_RE.search(message)
    if not m:
        return None
    place = m.group("place").strip().rstrip("?.!").strip().lower()
    # Direct IANA name (e.g. 'America/Los_Angeles')
    tz_name = None
    try:
        from zoneinfo import ZoneInfo, available_timezones
        if "/" in place and place in {z.lower() for z in available_timezones()}:
            for z in available_timezones():
                if z.lower() == place:
                    tz_name = z
                    break
        if not tz_name:
            tz_name = _WORLD_TIMEZONES.get(place)
        if not tz_name:
            # Try removing trailing words: 'time in tokyo japan' →
            # try 'tokyo japan' then 'tokyo'
            words = place.split()
            for i in range(len(words), 0, -1):
                candidate = " ".join(words[:i])
                if candidate in _WORLD_TIMEZONES:
                    tz_name = _WORLD_TIMEZONES[candidate]
                    place = candidate
                    break
        if not tz_name:
            return (f"I don't have the timezone for \"{place}\" yet — "
                    f"if you tell me the country it's in (e.g. 'time in "
                    f"Reykjavik Iceland') I can usually find it.")
        from datetime import datetime as _dt
        now_there = _dt.now(ZoneInfo(tz_name))
        time_str = now_there.strftime("%I:%M %p").lstrip("0")
        day_str  = now_there.strftime("%A, %B %-d") \
                   if hasattr(now_there, "strftime") else ""
        try:
            day_str = now_there.strftime("%A, %B %-d")
        except (ValueError, OSError):
            day_str = now_there.strftime("%A, %B %d").replace(" 0", " ")
        # Diff from owner's local
        my_now = _dt.now().astimezone()
        diff_hours = (now_there.utcoffset().total_seconds() - my_now.utcoffset().total_seconds()) / 3600
        if abs(diff_hours) < 0.01:
            diff_str = "(same as you)"
        else:
            sign = "+" if diff_hours > 0 else ""
            diff_str = f"({sign}{diff_hours:g} hr from your time)"
        return (f"It's **{time_str}** in {place.title()} — {day_str} {diff_str}. "
                f"(Timezone: {tz_name})")
    except Exception as e:
        return f"Couldn't resolve the time in {place}: {e}"


_PA_FOLDERS_RE = _re.compile(
    r"\b(?:list|show|what\s+are)\s+(?:me\s+)?(?:my\s+|the\s+|all\s+)?"
    r"(?:e[\s-]?mail\s+)?(?:imap\s+)?folders?\b",
    _re.IGNORECASE,
)


def _try_list_folders(message: str, user_dir: Path) -> str | None:
    if not message or not _PA_FOLDERS_RE.search(message):
        return None
    try:
        import imap_smtp
        accounts = imap_smtp.list_folders(user_dir)
    except Exception as e:
        return f"I couldn't list folders: {e}"
    if not accounts:
        return "You don't have any IMAP accounts connected. Add one in Settings → Integrations → + Add email account."
    lines = []
    for acct in accounts:
        lines.append(f"📁 {acct['account_email']} — {len(acct['folders'])} folders:")
        for name in acct["folders"]:
            lines.append(f"    • {name}")
        lines.append("")
    lines.append("Note: 'check my email' only pulls from INBOX. Tell me 'check my Bulk Mail' or 'check my Sent' to look at others.")
    return "\n".join(lines)


def _try_personal_assistant_read(message: str, user_dir: Path) -> str | None:
    """Detect 'what's on today / show my tasks / who is X' READ patterns
    and answer from the user's per-user modules. Returns the answer text
    or None to fall through."""
    if not message:
        return None

    if _PA_TODAY_RE.search(message):
        events = mod_calendar.today(user_dir)
        if not events:
            return "Nothing on your calendar for today."
        return "Today's calendar:\n" + "\n".join(
            f"  - {_fmt_email_date(e.get('start',''))}  {e.get('title','')}"
            for e in events
        )

    if _PA_WEEK_RE.search(message):
        events = mod_calendar.upcoming(user_dir, days=7)
        if not events:
            return "Nothing on your calendar this week."
        return "Upcoming this week:\n" + "\n".join(
            f"  - {_fmt_email_date(e.get('start',''))}  {e.get('title','')}"
            for e in events
        )

    if _PA_TASKS_RE.search(message):
        items = mod_tasks.list_all(user_dir)
        if not items:
            return "Your task list is empty."
        return "Open tasks:\n" + "\n".join(f"  - {t.get('text','')}" for t in items)

    if _PA_REMINDERS_RE.search(message):
        items = mod_reminders.list_all(user_dir)
        if not items:
            return "No pending reminders."
        return "Pending reminders:\n" + "\n".join(
            f"  - {_fmt_email_date(r.get('due',''))}  {r.get('text','')}"
            for r in items
        )

    if _PA_STAFF_RE.search(message):
        active = users_mod.list_users(DATA_DIR, include_archived=False)
        archived = users_mod.list_archived(DATA_DIR)
        if not active:
            base = "You have no active staff right now — just you."
            if archived:
                names = ", ".join(u.get("display_name") or u.get("username", "")
                                  for u in archived)
                base += f" Archived: {names}."
            return base
        lines = []
        for u in active:
            name = u.get("display_name") or u.get("username", "")
            role = u.get("role", "staff")
            lines.append(f"  - {name} ({role})")
        return f"Your active staff ({len(active)}):\n" + "\n".join(lines)

    m = _PA_PHONE_OF_RE.search(message)
    if m:
        name = m.group("name").strip()
        hits = mod_contacts.search(user_dir, name)
        if not hits:
            return f"No contact found matching \"{name}\"."
        c = hits[0]
        bits = [f"{c.get('name','')}"]
        if c.get("phone"): bits.append(f"phone {c['phone']}")
        if c.get("email"): bits.append(f"email {c['email']}")
        if c.get("company"): bits.append(f"({c['company']})")
        return ": ".join([bits[0], ", ".join(bits[1:])]) if len(bits) > 1 else bits[0]

    m = _PA_WHO_IS_RE.search(message)
    if m:
        name = m.group("name").strip()
        hits = mod_contacts.search(user_dir, name)
        if not hits:
            return None  # let LLM handle "who is" for general-knowledge people
        c = hits[0]
        bits = [c.get("name", "")]
        if c.get("company"): bits.append(f"at {c['company']}")
        if c.get("phone"): bits.append(f"phone {c['phone']}")
        if c.get("notes"): bits.append(f"notes: {c['notes']}")
        return " — ".join(bits)

    return None


# Quick-capture trigger words — only fire on these explicit phrasings, so
# normal questions don't accidentally get filed as notes.
_QC_TRIGGER_RE = _re.compile(
    r"^(?:remind\s+me|nudge\s+me|add\s+(?:to\s+)?(?:my\s+)?(?:todo|task|contact|person)|"
    r"appointment|meeting|book|schedule\s+(?:a|me)|save\s+contact|todo:|task:)",
    _re.IGNORECASE,
)


# Two flavors of conversational lead-in:
#   POLITE  — "can you", "could you", "please", "let's" — the verb is REQUIRED
#             after this. ("can you" → "can you draw a picture")
#   ACTION  — "i want", "give me", "show me", "i'd like" — the verb is IMPLIED,
#             so the noun can follow directly. ("show me a chart" needs no verb)
# Real users mix both. Without this split, "show me a chart of revenue" falls
# through to the LLM because there's no draw/make/build verb in it.
_POLITE_PREFIX = (
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:try\s+(?:to|and)\s+)?"
    r"|please\s+(?:can\s+you\s+)?"
    r"|let'?s\s+"
    r"|how\s+about\s+(?:you\s+)?"
    r")?"
)
_ACTION_PREFIX = (
    r"(?:i(?:'m| am)?\s+(?:want|need)(?:ing)?\s+(?:you\s+)?(?:to\s+(?:see|have|get)\s+)?"
    r"|i(?:'d| would)\s+like(?:\s+(?:you\s+)?to\s+(?:see|have|get)?)?\s*"
    r"|give\s+me\s+"
    r"|show\s+me\s+"
    r"|gimme\s+"
    r"|may\s+i\s+have\s+"
    r")"
)
_QUANT = r"(?:me\s+|us\s+)?(?:a\s+|an\s+|the\s+|some\s+)?"

_CHART_VERBS = r"(?:make|build|create|generate|draw|render|plot|chart|graph)"
_CHART_NOUNS = r"(?:bar|line|pie|scatter)?\s*(?:chart|graph|plot|visualization|visualisation)"

_DECK_VERBS = r"(?:make|build|create|generate|put\s+together|throw\s+together|whip\s+up)"
_DECK_NOUNS = r"(?:\d+[-\s]?slide\s+)?(?:pitch\s+)?(?:slide\s+)?(?:deck|presentation|powerpoint|pptx|slideshow)"

# Image verbs cover everything from "draw" to "whip up" to "illustrate".
_IMG_VERBS = (
    r"(?:make|build|create|generate|design|draw|paint|render|sketch|"
    r"mock\s*up|come\s+up\s+with|whip\s+up|illustrate|visualize|visualise)"
)
# Image nouns — anything someone might call a visual artifact.
_IMG_NOUNS = (
    r"(?:social\s+post|facebook\s+post|instagram\s+post|tiktok\s+post|"
    r"flyer|banner|poster|image|graphic|picture|pic|post|"
    r"logo|illustration|icon|headshot|thumbnail|infographic|"
    r"diagram|sketch|drawing|mockup|mock\s?up|ad|advert|photo|"
    r"meme|avatar|profile|cover|hero|wallpaper|art|artwork|visual)"
)

_CHART_TRIGGER_RE = _re.compile(
    r"^\s*(?:"
    + _ACTION_PREFIX + _QUANT + _CHART_NOUNS
    + r"|" + _POLITE_PREFIX + _CHART_VERBS + r"\s+" + _QUANT + _CHART_NOUNS
    + r")\b",
    _re.IGNORECASE,
)
_DECK_TRIGGER_RE = _re.compile(
    r"^\s*(?:"
    + _ACTION_PREFIX + _QUANT + _DECK_NOUNS
    + r"|" + _POLITE_PREFIX + _DECK_VERBS + r"\s+" + _QUANT + _DECK_NOUNS
    + r")\b",
    _re.IGNORECASE,
)
_IMAGE_TRIGGER_RE = _re.compile(
    r"^\s*(?:"
    + _ACTION_PREFIX + _QUANT + _IMG_NOUNS
    + r"|" + _POLITE_PREFIX + _IMG_VERBS + r"\s+" + _QUANT + _IMG_NOUNS
    + r")\b",
    _re.IGNORECASE,
)
# Loose catch-all: a bare drawing verb without an explicit noun.
# "can you draw what an Orby user would look like" doesn't say "picture" —
# still fire image gen because the verb is clearly visual.
_IMAGE_LOOSE_RE = _re.compile(
    r"^\s*" + _POLITE_PREFIX +
    r"(?:draw|paint|sketch|illustrate|visualize|visualise)\s+",
    _re.IGNORECASE,
)
# Self-portrait detector: the user wants Orby to draw HERSELF, not a
# generic prompt. "what you look like / what you imagine / draw yourself /
# your own appearance / what orbi looks like". When this matches we
# substitute a brand-aligned orb portrait prompt so FLUX doesn't default
# to a stock photo of a person.
_IMAGE_SELF_RE = _re.compile(
    r"\b(?:"
    r"(?:what|how)\s+(?:does?\s+)?(?:you|orbi|orby)\s+(?:would\s+|might\s+|could\s+)?"
    r"(?:imagine|look|appear)(?:s|ed|ing)?(?:\s+like)?"
    r"|you(?:r|rself)?\s+(?:would\s+)?(?:imagine|look|appear)(?:\s+like)?"
    r"|(?:draw|paint|sketch|render)\s+yourself"
    r"|yourself|your\s+(?:own\s+)?(?:appearance|self\s?-?portrait|avatar|image|face|likeness|look)"
    r"|self[-\s]?portrait\s+of\s+(?:you|orbi|orby)"
    r")\b",
    _re.IGNORECASE,
)


def _try_office_gen(message: str, username: str) -> dict | None:
    """Detect chart / deck / image generation intents and fire the
    corresponding generator. Returns a chat-shaped reply or None to
    fall through to the LLM."""
    msg = (message or "").strip()
    if not msg:
        return None

    try:
        if _CHART_TRIGGER_RE.match(msg):
            parsed = chart_gen.parse_chart_request(CONFIG, msg)
            png = chart_gen.generate_chart(
                CONFIG,
                title=parsed.get("title", "Chart"),
                kind=parsed.get("kind", "bar"),
                data=parsed.get("data") or {},
            )
            fname = f"chart_{int(time.time())}.png"
            saved = _save_and_token(png, fname)
            audit.log_event(DATA_DIR, actor=username, action="chart.via_chat",
                            meta={"req": msg[:120]})
            return {"reply": (f"Here's the {parsed.get('kind','bar')} chart titled "
                              f"\"{parsed.get('title','Chart')}\" — also saved to your Files tab."),
                    "tier": "local", "latency_ms": 0,
                    "source": "chart_gen", "download_url": saved.get("download_url")}

        if _DECK_TRIGGER_RE.match(msg):
            # Extract topic — everything after "deck about/on/for" or after the word "deck"
            topic_match = _re.search(
                r"(?:about|on|for|titled)\s+(.+)$", msg, _re.IGNORECASE)
            topic = (topic_match.group(1).strip() if topic_match
                     else _re.sub(r"^.{0,80}?(deck|presentation|powerpoint|pptx)\b\s*", "",
                                  msg, flags=_re.IGNORECASE).strip())
            if not topic:
                topic = "my business"
            slide_count = 7
            sc_m = _re.search(r"(\d+)[\s-]?slide", msg, _re.IGNORECASE)
            if sc_m:
                try: slide_count = max(3, min(20, int(sc_m.group(1))))
                except ValueError: pass
            biz = mod_business.load(DATA_DIR)
            result = pptx_gen.build_deck(CONFIG, topic=topic,
                                         target_slide_count=slide_count,
                                         theme="modern", business_info=biz)
            slug = _re.sub(r"\W+", "_", topic[:40]).strip("_") or "deck"
            fname = f"deck_{slug}_{int(time.time())}.pptx"
            saved = _save_and_token(result["pptx_bytes"], fname)
            audit.log_event(DATA_DIR, actor=username, action="pptx.via_chat",
                            meta={"topic": topic})
            return {"reply": (f"Built a {result.get('slide_count', slide_count)}-slide "
                              f"deck on \"{topic}\". "
                              f"[Download .pptx]({saved['download_url']}) — "
                              f"also in your Files tab."),
                    "tier": "local", "latency_ms": 0,
                    "source": "pptx_gen", "download_url": saved.get("download_url")}

        image_match = _IMAGE_TRIGGER_RE.match(msg) or _IMAGE_LOOSE_RE.match(msg)
        if image_match:
            # ── Self-portrait shortcut ──────────────────────────────────────
            # "draw yourself" / "what you look like" / "what you imagine you
            # would look like" — the user wants Orby's self-image, not a
            # generic prompt. Use a brand-aligned orb portrait so FLUX
            # doesn't default to a stock photo of a person.
            if _IMAGE_SELF_RE.search(msg):
                prompt = ("a friendly glowing translucent purple orb of light, "
                          "floating in a soft dark cosmic background with gentle "
                          "blue and violet wisps of aurora, smooth volumetric "
                          "lighting, ethereal, abstract, no human figures, no faces, "
                          "no text, modern minimalist digital art, square composition, "
                          "high detail, cinematic")
            else:
                # Strip conversational prefix + verb + filler to get the
                # visual prompt. "can you draw me a picture of a robot"
                # → "of a robot" → "a robot"
                prompt = msg
                for pat in (
                    # 1. conversational prefix (can you, please, i want, etc.)
                    r"^\s*(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:try\s+(?:to|and)\s+)?"
                    r"|please\s+(?:can\s+you\s+)?"
                    r"|i(?:'m| am)?\s+(?:want|need)(?:ing)?\s+(?:you\s+)?(?:to\s+)?"
                    r"|i(?:'d| would)\s+like(?:\s+(?:you\s+)?to)?\s+"
                    r"|let'?s\s+|how\s+about\s+(?:you\s+)?"
                    r"|may\s+i\s+have\s+|give\s+me\s+|show\s+me\s+|gimme\s+)",
                    # 2. drawing verb + me/us + a/an/the/some
                    r"^\s*(?:make|build|create|generate|design|draw|paint|render|sketch|"
                    r"mock\s*up|come\s+up\s+with|whip\s+up|illustrate|visualize|visualise)\s+"
                    r"(?:me\s+|us\s+)?(?:a\s+|an\s+|the\s+|some\s+)?",
                    # 3. leading noun-of: "picture of a robot" → "a robot"
                    r"^(?:social\s+post|facebook\s+post|instagram\s+post|tiktok\s+post|"
                    r"flyer|banner|poster|image|graphic|picture|pic|post|"
                    r"logo|illustration|icon|headshot|thumbnail|infographic|"
                    r"diagram|sketch|drawing|mockup|mock\s?up|ad|advert|photo|"
                    r"meme|avatar|profile|cover|hero|wallpaper|art|artwork|visual)\s+"
                    r"(?:of\s+|showing\s+|for\s+|that\s+(?:shows?|depicts?|has)\s+|"
                    r"depicting\s+|with\s+|about\s+)?",
                ):
                    prompt = _re.sub(pat, "", prompt, flags=_re.IGNORECASE).strip()
                # Fall back to original if stripping nuked everything
                if not prompt:
                    prompt = msg
            png = image_gen.generate(CONFIG, prompt, kind="social_post")
            ws = mod_workspace.workspace_path(CONFIG)
            saved_path = image_gen.save_to_workspace(png, prompt, ws)
            try:
                token = file_fetch.mint_download_token(
                    DATA_DIR, str(saved_path), ttl_minutes=30,
                    extra_allowed_roots=[ws])
                url = f"/download/{token}"
            except Exception:
                url = None
            audit.log_event(DATA_DIR, actor=username, action="image.via_chat",
                            meta={"prompt": prompt[:120]})
            # Short caption only — the inline <img> in the bubble IS the
            # preview, so a separate "[Download it](...)" markdown link
            # would render as ugly raw text. Click the image for full size.
            short_prompt = prompt if len(prompt) <= 80 else prompt[:77] + "..."
            return {"reply": f"Here's what I drew for \"{short_prompt}\" — also saved to your Files tab.",
                    "tier": "local", "latency_ms": 0,
                    "source": "image_gen", "download_url": url}
    except Exception as e:
        log.warning(f"office_gen fast-path failed: {e}")
        return None

    return None


_POLITE_PREFIX_RE = _re.compile(
    r"^\s*(?:hey\s+orbi[,\s]+|orbi[,\s]+|"
    r"(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
    r"please\s+|"
    r"i\s+(?:want|need)\s+(?:you\s+)?to\s+|"
    r"i'?d\s+like\s+(?:you\s+)?to\s+|"
    r"no[,\s]+(?:i\s+(?:want|need|meant)\s+(?:you\s+)?to\s+|actually\s+)?|"
    r"actually[,\s]+|wait[,\s]+)+",
    _re.IGNORECASE,
)


def _strip_polite_prefix(message: str) -> str:
    """Remove leading conversational openers so 'can you remind me to X'
    matches the same fast-path as 'remind me to X'. Critical: without this
    the request falls through to the LLM which then hallucinates that it
    saved the reminder when it didn't."""
    return _POLITE_PREFIX_RE.sub("", message or "", count=1).strip()


def _try_quick_capture(message: str, user_dir: Path) -> dict | None:
    """If the message starts with a quick-capture trigger word, run it
    through quick_capture.capture() and return the result dict. Otherwise None."""
    if not message:
        return None
    stripped = _strip_polite_prefix(message)
    if not _QC_TRIGGER_RE.match(stripped):
        return None
    try:
        return mod_qc.capture(user_dir, stripped)
    except Exception as e:
        log.warning(f"quick_capture failed: {e}")
        return None


def _try_file_fetch(message: str, username: str) -> dict | None:
    """If the message looks like 'send me the X file from my computer',
    search the allowed scope and either return a download link (single
    strong match), an ambiguous-result picker, or an honest 'not found'.
    Returns None if the intent doesn't match (fall through to LLM)."""
    intent = file_fetch.extract_file_request(message)
    if not intent:
        return None
    query = intent.get("query", "").strip()
    kind = intent.get("kind", "file")
    if not query:
        return None
    try:
        matches = file_fetch.search(DATA_DIR, query, limit=6)
    except Exception as e:
        log.warning(f"file_fetch search failed: {e}")
        return None

    audit.log_event(DATA_DIR, actor=username, action="file_fetch.query",
                    meta={"query": query, "kind": kind, "hits": len(matches)})

    if not matches:
        scope = file_fetch.load_scope(DATA_DIR)
        folders = ", ".join(scope.get("allowed_paths", []) or ["the allowed folders"])
        return {
            "reply": (f"I searched {folders} for \"{query}\" but didn't find anything. "
                      f"Want me to widen the search, or tell me a more specific filename?"),
            "tier": "local", "latency_ms": 0, "source": "file_fetch_miss",
        }

    if len(matches) == 1 or (matches[0].get("score", 0) > matches[1].get("score", 0) * 2 if len(matches) > 1 else True):
        # Single strong match — mint and return link
        m = matches[0]
        path = m["path"]
        try:
            if kind == "folder" or m.get("is_dir"):
                zip_path = file_fetch.prepare_folder_download(DATA_DIR, path)
                token = file_fetch.mint_download_token(DATA_DIR, zip_path, ttl_minutes=10)
                label = f"{m['name']} (zipped)"
            else:
                token = file_fetch.mint_download_token(DATA_DIR, path, ttl_minutes=10)
                size_mb = (m.get("size_bytes", 0) / 1_000_000)
                label = f"{m['name']} ({size_mb:.1f} MB)" if size_mb >= 0.05 else m["name"]
            audit.log_event(DATA_DIR, actor=username, action="file_fetch.token_minted",
                            resource=path, meta={"via": "chat", "kind": kind})
            return {
                "reply": f"Found it — [download {label}](/download/{token}) (link expires in 10 minutes, single-use).",
                "tier": "local", "latency_ms": 0, "source": "file_fetch_hit",
                "download_url": f"/download/{token}",
            }
        except (PermissionError, ValueError) as e:
            return {"reply": f"I found that but can't share it: {e}",
                    "tier": "local", "latency_ms": 0, "source": "file_fetch_blocked"}

    # Multiple matches — ask which one
    options = "\n".join(
        f"  {i+1}. {m['name']} — in {m.get('parent_folder','?')}"
        for i, m in enumerate(matches[:5])
    )
    return {
        "reply": (f"I found {len(matches)} possible matches for \"{query}\":\n{options}\n\n"
                  f"Tell me the number or be more specific."),
        "tier": "local", "latency_ms": 0, "source": "file_fetch_ambiguous",
        "candidates": matches[:5],
    }


# ---------------------------------------------------------------------------
# Personal-assistant CRUD routes (per-user, dashboard reads/writes via these)
# ---------------------------------------------------------------------------


def _current_user_dir() -> Path:
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    d = users_mod.get_user_dir(DATA_DIR, user["username"])
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.route("/api/owner/pa/calendar", methods=["GET", "POST"])
def pa_calendar():
    ud = _current_user_dir()
    if request.method == "GET":
        return jsonify({"events": mod_calendar.list_all(ud)})
    data = request.get_json(silent=True) or {}
    try:
        event = mod_calendar.add(
            ud,
            title=data.get("title", ""),
            start=data.get("start", ""),
            end=data.get("end"),
            all_day=bool(data.get("all_day")),
            notes=data.get("notes", ""),
            with_=data.get("with") or [],
            location=data.get("location", ""),
        )
        return jsonify({"status": "ok", "event": event})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/owner/pa/calendar/<event_id>", methods=["DELETE", "PATCH"])
def pa_calendar_one(event_id):
    ud = _current_user_dir()
    if request.method == "DELETE":
        ok = mod_calendar.remove(ud, event_id)
        return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404
    data = request.get_json(silent=True) or {}
    updated = mod_calendar.update(ud, event_id, **data)
    if not updated:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"status": "ok", "event": updated})


@app.route("/api/owner/pa/reminders", methods=["GET", "POST"])
def pa_reminders():
    ud = _current_user_dir()
    if request.method == "GET":
        include_done = request.args.get("include_done") == "1"
        return jsonify({"reminders": mod_reminders.list_all(ud, include_done=include_done)})
    data = request.get_json(silent=True) or {}
    r = mod_reminders.add(ud, text=data.get("text", ""),
                          due=data.get("due", ""),
                          channel=data.get("channel", "in_app"))
    return jsonify({"status": "ok", "reminder": r})


@app.route("/api/owner/pa/reminders/<reminder_id>/done", methods=["POST"])
def pa_reminder_done(reminder_id):
    ud = _current_user_dir()
    ok = mod_reminders.mark_done(ud, reminder_id)
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404


@app.route("/api/owner/pa/reminders/<reminder_id>", methods=["DELETE"])
def pa_reminder_delete(reminder_id):
    ud = _current_user_dir()
    # No remove in module; mark done = soft delete from active view
    ok = mod_reminders.mark_done(ud, reminder_id)
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404


@app.route("/api/owner/pa/contacts", methods=["GET", "POST"])
def pa_contacts():
    ud = _current_user_dir()
    if request.method == "GET":
        q = request.args.get("q", "")
        return jsonify({"contacts": mod_contacts.search(ud, q) if q else mod_contacts.list_all(ud)})
    data = request.get_json(silent=True) or {}
    c = mod_contacts.add(ud,
                         name=data.get("name", ""),
                         phone=data.get("phone", ""),
                         email=data.get("email", ""),
                         notes=data.get("notes", ""),
                         tags=data.get("tags") or [],
                         source=data.get("source", "manual"),
                         company=data.get("company", ""))
    return jsonify({"status": "ok", "contact": c})


@app.route("/api/owner/pa/contacts/<contact_id>", methods=["DELETE", "PATCH"])
def pa_contact_one(contact_id):
    ud = _current_user_dir()
    if request.method == "DELETE":
        ok = mod_contacts.remove(ud, contact_id)
        return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404
    data = request.get_json(silent=True) or {}
    updated = mod_contacts.update(ud, contact_id, **data)
    if not updated:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"status": "ok", "contact": updated})


@app.route("/api/owner/pa/tasks", methods=["GET", "POST"])
def pa_tasks():
    ud = _current_user_dir()
    if request.method == "GET":
        include_done = request.args.get("include_done") == "1"
        return jsonify({"tasks": mod_tasks.list_all(ud, include_done=include_done)})
    data = request.get_json(silent=True) or {}
    t = mod_tasks.add(ud, text=data.get("text", ""), tags=data.get("tags") or [])
    return jsonify({"status": "ok", "task": t})


@app.route("/api/owner/pa/tasks/<task_id>/done", methods=["POST"])
def pa_task_done(task_id):
    ud = _current_user_dir()
    ok = mod_tasks.mark_done(ud, task_id)
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404


@app.route("/api/owner/pa/tasks/<task_id>", methods=["DELETE"])
def pa_task_delete(task_id):
    ud = _current_user_dir()
    ok = mod_tasks.remove(ud, task_id)
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404


@app.route("/api/owner/pa/quick_capture", methods=["POST"])
def pa_quick_capture():
    """Single endpoint where the dashboard can stream "remember this" snippets
    and let the classifier file them automatically."""
    ud = _current_user_dir()
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty"}), 400
    return jsonify(mod_qc.capture(ud, text))


# ---------------------------------------------------------------------------
# Multi-user management (owner-only)
# ---------------------------------------------------------------------------


@app.route("/api/owner/users", methods=["GET", "POST"])
def users_route():
    owner = auth.require_role(ORBI_DIR, DATA_DIR, "owner")
    if request.method == "GET":
        return jsonify({
            "users": users_mod.list_users(DATA_DIR, include_archived=False),
            "archived": users_mod.list_archived(DATA_DIR),
        })
    data = request.get_json(silent=True) or {}
    try:
        rec = users_mod.add_user(DATA_DIR,
                                 username=data.get("username", ""),
                                 password=data.get("password", ""),
                                 role=data.get("role", "staff"),
                                 display_name=data.get("display_name"))
        audit.log_event(DATA_DIR, actor=owner["username"],
                        action="users.add", resource=rec["username"])
        return jsonify({"status": "ok", "user": rec})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/owner/users/<username>/deactivate", methods=["POST"])
def user_deactivate(username):
    owner = auth.require_role(ORBI_DIR, DATA_DIR, "owner")
    try:
        meta = users_mod.deactivate_user(DATA_DIR, username)
        audit.log_event(DATA_DIR, actor=owner["username"],
                        action="users.deactivate", resource=username, meta=meta)
        return jsonify({"status": "ok", "archive": meta})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/owner/users/<username>/hold", methods=["POST"])
def user_hold(username):
    owner = auth.require_role(ORBI_DIR, DATA_DIR, "owner")
    data = request.get_json(silent=True) or {}
    ok = users_mod.set_purge_hold(DATA_DIR, username, bool(data.get("hold")))
    audit.log_event(DATA_DIR, actor=owner["username"],
                    action="users.set_hold", resource=username,
                    meta={"hold": bool(data.get("hold"))})
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404


@app.route("/api/owner/users/<from_user>/transfer/<to_user>", methods=["POST"])
def user_transfer(from_user, to_user):
    owner = auth.require_role(ORBI_DIR, DATA_DIR, "owner")
    data = request.get_json(silent=True) or {}
    source = data.get("source", "")
    ids = data.get("ids") or []
    if not source or not ids:
        return jsonify({"error": "source and ids required"}), 400
    moved = users_mod.transfer_items(DATA_DIR, from_user, to_user, source, ids)
    audit.log_event(DATA_DIR, actor=owner["username"], action="users.transfer",
                    resource=f"{from_user}->{to_user}",
                    meta={"source": source, "moved": moved, "ids": ids})
    return jsonify({"status": "ok", "moved": moved})


@app.route("/api/owner/users/purge_now", methods=["POST"])
def users_purge_now():
    owner = auth.require_role(ORBI_DIR, DATA_DIR, "owner")
    purged = users_mod.purge_expired_archives(DATA_DIR)
    audit.log_event(DATA_DIR, actor=owner["username"],
                    action="users.purge_expired", meta={"purged": purged})
    return jsonify({"status": "ok", "purged": purged})


# ---------------------------------------------------------------------------
# Google Calendar two-way sync (per-user)
# ---------------------------------------------------------------------------

def _gcal_oauth_creds() -> tuple[str, str]:
    """Read OAuth client credentials from CONFIG. These are baked in by the
    installer (one Cloud project per Orby deployment) or set via dashboard."""
    g = CONFIG.get("gcal_oauth") or {}
    return g.get("client_id", ""), g.get("client_secret", "")


def _gcal_redirect_uri() -> str:
    """Loopback redirect for Desktop-app OAuth client type. Customer connects
    Google while on the same machine as their Orby install (first-time setup)."""
    port = (CONFIG.get("server") or {}).get("port") or 5050
    return f"http://localhost:{port}/api/owner/gcal/callback"


@app.route("/api/owner/gcal/connect", methods=["POST"])
def gcal_connect():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    client_id, client_secret = _gcal_oauth_creds()
    if not client_id or not client_secret:
        return jsonify({"error": "gcal_oauth_not_configured",
                        "hint": "Owner must paste Google Cloud Client ID + Secret in Settings"}), 400
    try:
        auth_url = gcal.start_auth_flow(client_id, client_secret, _gcal_redirect_uri())
        return jsonify({"auth_url": auth_url})
    except Exception as e:
        log.warning(f"gcal connect failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/owner/gcal/callback", methods=["GET"])
def gcal_callback():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    code = request.args.get("code", "")
    if not code:
        return "Missing authorization code", 400
    client_id, client_secret = _gcal_oauth_creds()
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    user_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = gcal.complete_auth_flow(client_id, client_secret,
                                         _gcal_redirect_uri(), code, user_dir)
        audit.log_event(DATA_DIR, actor=user["username"],
                        action="gcal.connected", meta={"email": result.get("email")})
        from flask import redirect
        return redirect("/owner#gcal")
    except Exception as e:
        log.warning(f"gcal callback failed: {e}")
        return f"Connect failed: {e}", 500


@app.route("/api/owner/gcal/disconnect", methods=["POST"])
def gcal_disconnect():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    gcal.disconnect(user_dir)
    audit.log_event(DATA_DIR, actor=user["username"], action="gcal.disconnected")
    return jsonify({"ok": True})


@app.route("/api/owner/gcal/sync_now", methods=["POST"])
def gcal_sync_now():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    try:
        result = gcal.sync_all(user_dir)
        return jsonify(result)
    except Exception as e:
        log.warning(f"gcal sync failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/owner/gcal/status", methods=["GET"])
def gcal_status():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    return jsonify(gcal.get_status(user_dir))


# ---------------------------------------------------------------------------
# Generic connector dispatch — every registered Connector subclass plugs in
# automatically. Lets us add a new integration just by dropping a file in
# connectors/ — no orbi.py change needed for the common ops.
# ---------------------------------------------------------------------------


def _connector_instance(connector_id: str):
    """Resolve connector_id to a live instance scoped to the logged-in user."""
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    user_dir.mkdir(parents=True, exist_ok=True)
    inst = connector_base.get_instance(connector_id, CONFIG, user_dir)
    if inst is None:
        abort(404, description=f"connector {connector_id!r} not found")
    return user, inst


def _connector_redirect_uri(connector_id: str) -> str:
    port = (CONFIG.get("server") or {}).get("port") or 5050
    return f"http://localhost:{port}/api/owner/connectors/{connector_id}/callback"


@app.route("/api/owner/connectors", methods=["GET"])
def connectors_list():
    """Return all registered connectors + the logged-in user's status on each."""
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    out = []
    for cls in connector_base.list_connectors():
        try:
            inst = cls(CONFIG, user_dir)
            out.append(inst.status())
        except Exception as e:
            log.warning(f"connector {cls.id} status failed: {e}")
            out.append({"id": cls.id, "label": cls.label, "connected": False,
                        "error": str(e)})
    return jsonify({"connectors": out})


@app.route("/api/owner/connectors/<connector_id>/connect", methods=["POST"])
def connector_connect(connector_id):
    user, inst = _connector_instance(connector_id)
    if inst.auth_kind != "oauth":
        return jsonify({"error": "not_oauth", "auth_kind": inst.auth_kind}), 400
    try:
        url = inst.start_oauth(_connector_redirect_uri(connector_id))
        return jsonify({"auth_url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/owner/connectors/<connector_id>/callback", methods=["GET"])
def connector_callback(connector_id):
    user, inst = _connector_instance(connector_id)
    code = request.args.get("code", "")
    if not code:
        return "Missing authorization code", 400
    try:
        result = inst.complete_oauth(code, _connector_redirect_uri(connector_id))
        audit.log_event(DATA_DIR, actor=user["username"],
                        action=f"connector.connected.{connector_id}",
                        meta={k: v for k, v in result.items() if "token" not in k})
        from flask import redirect
        return redirect("/owner#integrations")
    except Exception as e:
        log.warning(f"connector {connector_id} callback failed: {e}")
        return f"Connect failed: {e}", 500


@app.route("/api/owner/connectors/<connector_id>/disconnect", methods=["POST"])
def connector_disconnect(connector_id):
    user, inst = _connector_instance(connector_id)
    inst.disconnect()
    audit.log_event(DATA_DIR, actor=user["username"],
                    action=f"connector.disconnected.{connector_id}")
    return jsonify({"ok": True})


@app.route("/api/owner/connectors/<connector_id>/status", methods=["GET"])
def connector_status(connector_id):
    user, inst = _connector_instance(connector_id)
    return jsonify(inst.status())


@app.route("/api/owner/connectors/<connector_id>/save_key", methods=["POST"])
def connector_save_key(connector_id):
    """For API-key connectors (Stripe, Yelp): owner pastes their key here."""
    user, inst = _connector_instance(connector_id)
    if inst.auth_kind != "api_key":
        return jsonify({"error": "not_api_key", "auth_kind": inst.auth_kind}), 400
    data = request.get_json(silent=True) or {}
    key = data.get("key", "")
    meta = {k: v for k, v in data.items() if k != "key"}
    try:
        result = inst.save_api_key(key, meta=meta)
        audit.log_event(DATA_DIR, actor=user["username"],
                        action=f"connector.key_saved.{connector_id}",
                        meta={k: v for k, v in result.items() if "key" not in k})
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/owner/connectors/<connector_id>/<action>", methods=["GET", "POST"])
def connector_action(connector_id, action):
    """Generic action dispatch — any method on the Connector class that doesn't
    start with _ can be called via this route. Method args come from the JSON
    body (POST) or query string (GET). For example:

      GET /api/owner/connectors/gmail/list_recent?limit=20
        → calls inst.list_recent(limit=20)
      POST /api/owner/connectors/gmail/draft_reply with {message_id, reply_text}
        → calls inst.draft_reply(message_id="...", reply_text="...")
    """
    # Block the routes we already have explicit handlers for
    if action in ("connect", "callback", "disconnect", "status", "save_key"):
        abort(404)
    user, inst = _connector_instance(connector_id)
    method = getattr(inst, action, None)
    if method is None or not callable(method) or action.startswith("_"):
        return jsonify({"error": "unknown_action", "action": action}), 404
    if request.method == "GET":
        kwargs = {k: (int(v) if v.isdigit() else v)
                  for k, v in request.args.items()}
    else:
        kwargs = request.get_json(silent=True) or {}
    try:
        result = method(**kwargs)
        return jsonify({"result": result})
    except TypeError as e:
        return jsonify({"error": f"bad_args: {e}"}), 400
    except Exception as e:
        log.warning(f"connector {connector_id}.{action} failed: {e}")
        inst.update_status(last_error=str(e)[:200])
        return jsonify({"error": str(e)}), 500


# Background gcal sync — runs every 5 min per active user
def gcal_sync_loop():
    time.sleep(90)
    while True:
        try:
            for u in users_mod.list_users(DATA_DIR):
                user_dir = users_mod.get_user_dir(DATA_DIR, u["username"])
                if not user_dir.exists() or not gcal.is_connected(user_dir):
                    continue
                try:
                    result = gcal.sync_all(user_dir)
                    if result.get("pulled") or result.get("pushed"):
                        log.info(f"gcal background sync {u['username']}: "
                                 f"pulled={result.get('pulled',0)} pushed={result.get('pushed',0)}")
                except Exception as e:
                    log.warning(f"gcal sync error for {u['username']}: {e}")
        except Exception as e:
            log.warning(f"gcal loop error: {e}")
        time.sleep(60 * 5)

threading.Thread(target=gcal_sync_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Remote file fetch — "send me the Maxwell file from my computer"
# Phone PWA → cloudflared tunnel → home Orby → scoped file resolution
# → one-time download token → file streams back.
# Files NEVER leave the owner's computer except through the token-gated
# /download/<token> route. Tunnel is transport-only.
# ---------------------------------------------------------------------------


@app.route("/api/owner/files/scope", methods=["GET", "PUT"])
def file_scope_route():
    auth.require_role(ORBI_DIR, DATA_DIR, "owner")
    if request.method == "GET":
        return jsonify(file_fetch.load_scope(DATA_DIR))
    data = request.get_json(silent=True) or {}
    file_fetch.save_scope(DATA_DIR, data)
    return jsonify({"status": "ok", "scope": file_fetch.load_scope(DATA_DIR)})


@app.route("/api/owner/files/search", methods=["GET"])
def file_search_route():
    auth.require_user(ORBI_DIR, DATA_DIR)
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"matches": []})
    limit = int(request.args.get("limit", "10"))
    return jsonify({"matches": file_fetch.search(DATA_DIR, q, limit=limit)})


@app.route("/api/owner/files/request", methods=["POST"])
def file_request_route():
    """Owner picks a search result and requests a downloadable link for it.
    Returns a public /download/<token> URL that's single-use and TTL-bound."""
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    kind = data.get("kind", "file")
    ttl = int(data.get("ttl_minutes", 10))
    if not path:
        return jsonify({"error": "path required"}), 400
    try:
        if kind == "folder":
            zip_path = file_fetch.prepare_folder_download(DATA_DIR, path)
            token = file_fetch.mint_download_token(DATA_DIR, zip_path, ttl_minutes=ttl)
        else:
            token = file_fetch.mint_download_token(DATA_DIR, path, ttl_minutes=ttl)
    except (PermissionError, ValueError) as e:
        return jsonify({"error": str(e)}), 403
    audit.log_event(DATA_DIR, actor=user["username"], action="file_fetch.token_minted",
                    resource=path, meta={"kind": kind, "ttl_minutes": ttl})
    return jsonify({"token": token, "url": f"/download/{token}", "ttl_minutes": ttl})


@app.route("/download/<token>", methods=["GET"])
def download_route(token):
    """PUBLIC — single-use token-gated download. No session check on purpose:
    the phone (away from the home computer) needs to fetch through the tunnel.
    Token entropy + single-use + short TTL is the entire auth boundary."""
    redeemed = file_fetch.redeem_token(DATA_DIR, token)
    if not redeemed:
        return ("Link expired or already used.", 410)
    from flask import send_file
    try:
        return send_file(
            redeemed["path"],
            mimetype=redeemed.get("mime", "application/octet-stream"),
            as_attachment=True,
            download_name=redeemed["filename"],
        )
    except FileNotFoundError:
        return ("File no longer exists.", 410)


# Background sweep for stale zip temp files (folder downloads)
def file_temp_sweep_loop():
    time.sleep(120)
    while True:
        try:
            purged = file_fetch.purge_old_temp_downloads(DATA_DIR, older_than_minutes=60)
            if purged:
                log.info(f"file_fetch temp sweep: purged {purged} stale zips")
        except Exception as e:
            log.warning(f"file_fetch temp sweep failed: {e}")
        time.sleep(60 * 30)

threading.Thread(target=file_temp_sweep_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Internal (watchdog → us)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Morning briefing — personalized digest delivered via push every morning
# ---------------------------------------------------------------------------


@app.route("/api/owner/briefing/now", methods=["GET"])
def briefing_now():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    try:
        return jsonify(briefing.build_briefing(CONFIG, DATA_DIR, user["username"]))
    except Exception as e:
        log.warning(f"briefing build failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/owner/briefing/send_now", methods=["POST"])
def briefing_send_now():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    result = briefing.send_morning_brief(CONFIG, DATA_DIR, user["username"])
    audit.log_event(DATA_DIR, actor=user["username"], action="briefing.send_now")
    return jsonify(result)


@app.route("/api/owner/briefing/preferences", methods=["GET", "PUT"])
def briefing_preferences():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    if request.method == "GET":
        return jsonify(briefing.get_preferences(user_dir))
    data = request.get_json(silent=True) or {}
    briefing.set_preferences(user_dir, data)
    return jsonify(briefing.get_preferences(user_dir))


def briefing_scheduler_loop():
    """Once per minute, check each active user's preferences. If their
    configured hour has passed today AND they haven't been brief'd today,
    send the briefing. Keeps the sweep cheap by short-circuiting fast."""
    time.sleep(45)
    while True:
        try:
            now_hour = datetime.now(timezone.utc).hour
            for u in users_mod.list_users(DATA_DIR):
                username = u["username"]
                user_dir = users_mod.get_user_dir(DATA_DIR, username)
                if not user_dir.exists():
                    continue
                try:
                    prefs = briefing.get_preferences(user_dir)
                    if not prefs.get("enabled", True):
                        continue
                    if now_hour < int(prefs.get("hour", 7)):
                        continue
                    if not briefing.should_send_today(user_dir):
                        continue
                    briefing.send_morning_brief(CONFIG, DATA_DIR, username)
                    log.info(f"morning brief sent: {username}")
                except Exception as e:
                    log.warning(f"briefing schedule error for {username}: {e}")
        except Exception as e:
            log.warning(f"briefing loop error: {e}")
        time.sleep(60)


# Lazy import for the datetime stuff this scheduler uses
from datetime import datetime, timezone  # noqa: E402  (already imported elsewhere but explicit here)
threading.Thread(target=briefing_scheduler_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Universal search — one query, every data source
# ---------------------------------------------------------------------------


@app.route("/api/owner/search", methods=["GET"])
def universal_search_route():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"query": "", "total_hits": 0, "by_source": {}})
    limit = int(request.args.get("limit", "5"))
    return jsonify(universal_search.search(CONFIG, DATA_DIR, user_dir, q,
                                            limit_per_source=limit))


# ---------------------------------------------------------------------------
# Follow-up tracker — surface unresponded items
# ---------------------------------------------------------------------------


@app.route("/api/owner/follow_up", methods=["GET"])
def follow_up_list():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    min_days = int(request.args.get("min_days", "2"))
    limit = int(request.args.get("limit", "20"))
    items = follow_up.find_stale_items(CONFIG, DATA_DIR, user_dir,
                                        min_days=min_days, limit=limit)
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/owner/follow_up/draft", methods=["POST"])
def follow_up_draft():
    auth.require_user(ORBI_DIR, DATA_DIR)
    item = request.get_json(silent=True) or {}
    return jsonify({"text": follow_up.draft_nudge(CONFIG, item)})


# ---------------------------------------------------------------------------
# OCR — receipts + business cards from photos
# ---------------------------------------------------------------------------


@app.route("/api/owner/ocr/process", methods=["POST"])
def ocr_process():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    if not filename:
        return jsonify({"error": "filename required"}), 400
    # Resolve the workspace path safely (filename must be inside the workspace)
    import re as _re
    safe = os.path.basename(filename.replace("\\", "/"))
    safe = _re.sub(r"[^\w\s.\-()]+", "", safe).strip(" .")
    if not safe:
        return jsonify({"error": "invalid filename"}), 400
    ws = mod_workspace.workspace_path(CONFIG)
    image_path = ws / safe
    if not image_path.exists():
        return jsonify({"error": "file_not_found"}), 404
    try:
        result = ocr_mod.process_image(CONFIG, image_path, user_dir)
        audit.log_event(DATA_DIR, actor=user["username"],
                        action=f"ocr.{result.get('kind','unknown')}",
                        resource=safe)
        return jsonify(result)
    except Exception as e:
        log.warning(f"OCR process failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/owner/receipts", methods=["GET"])
def receipts_list():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    return jsonify({"receipts": ocr_mod.list_receipts(user_dir, limit=200)})


@app.route("/api/owner/receipts/<receipt_id>", methods=["GET", "DELETE"])
def receipts_one(receipt_id):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    if request.method == "GET":
        rec = ocr_mod.get_receipt(user_dir, receipt_id)
        if not rec:
            return jsonify({"error": "not_found"}), 404
        return jsonify(rec)
    ok = ocr_mod.delete_receipt(user_dir, receipt_id)
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404


# ---------------------------------------------------------------------------
# Messages — bulk re-tagging and manual tag override
# ---------------------------------------------------------------------------


@app.route("/api/owner/messages/retag_all", methods=["POST"])
def messages_retag_all():
    user = auth.require_role(ORBI_DIR, DATA_DIR, "owner")
    n = mod_messages.retag_all(DATA_DIR, CONFIG)
    audit.log_event(DATA_DIR, actor=user["username"], action="messages.retag_all",
                    meta={"count": n})
    return jsonify({"retagged": n})


@app.route("/api/owner/messages/<msg_id>/tags", methods=["PUT"])
def messages_update_tags(msg_id):
    auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    tags = data.get("tags") or []
    ok = mod_messages.update_tags(DATA_DIR, msg_id, tags)
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404


# ---------------------------------------------------------------------------
# Onboarding wizard — discover business from website + ask gap questions
# ---------------------------------------------------------------------------


@app.route("/api/owner/onboarding/discover", methods=["POST"])
def onboarding_discover():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        draft = onboarding.discover_from_url(url)
        questions = onboarding.gap_questions(draft)
        audit.log_event(DATA_DIR, actor=user["username"],
                        action="onboarding.discover", meta={"url": url})
        return jsonify({"draft": draft, "gap_questions": questions})
    except Exception as e:
        log.warning(f"onboarding discover failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/owner/onboarding/answer", methods=["POST"])
def onboarding_answer():
    """Owner answered one of the gap questions. Convert the answer to
    the structured shape and return it (caller merges into the draft)."""
    auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    field = data.get("field", "")
    answer = data.get("answer", "")
    return jsonify({"patch": onboarding.parse_answer(field, answer)})


@app.route("/api/owner/onboarding/apply", methods=["POST"])
def onboarding_apply():
    """Owner clicked 'Save' on the wizard. Merge the draft into
    business_info.json — overwrite=True means owner answers win over
    any existing values."""
    user = auth.require_role(ORBI_DIR, DATA_DIR, "owner")
    data = request.get_json(silent=True) or {}
    draft = data.get("draft") or {}
    overwrite = bool(data.get("overwrite", True))
    saved = onboarding.apply_to_business(DATA_DIR, draft, overwrite=overwrite)
    audit.log_event(DATA_DIR, actor=user["username"], action="onboarding.apply",
                    meta={"name": saved.get("name", "")})
    return jsonify({"status": "ok", "business": saved})


@app.route("/api/owner/onboarding/explain", methods=["GET"])
def onboarding_explain():
    """Returns the canned 'here's how I learn about your business' text
    so chat can pull it verbatim or the dashboard can show it."""
    return jsonify({"text": onboarding.explain_flow()})


# ---------------------------------------------------------------------------
# Email inbox — unified Gmail + Outlook view with categories + flagging
# ---------------------------------------------------------------------------


@app.route("/api/owner/email/inbox", methods=["GET"])
def email_inbox_route():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    return jsonify(email_inbox.fetch_inbox(
        CONFIG, user_dir,
        source=request.args.get("source", "all"),
        limit=int(request.args.get("limit", "50")),
        query=request.args.get("q", ""),
        force_refresh=request.args.get("refresh") == "1",
    ))


@app.route("/api/owner/email/<message_id>", methods=["GET"])
def email_message_get(message_id):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    return jsonify(email_inbox.get_message(CONFIG, user_dir, message_id))


@app.route("/api/owner/email/<message_id>/reply", methods=["POST"])
def email_reply(message_id):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    text = data.get("reply_text", "")
    result = email_inbox.draft_reply(CONFIG, user_dir, message_id, text)
    audit.log_event(DATA_DIR, actor=user["username"], action="email.draft_reply",
                    resource=message_id, meta={"chars": len(text)})
    return jsonify(result)


@app.route("/api/owner/email/<message_id>/mark_read", methods=["POST"])
def email_mark_read(message_id):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    return jsonify(email_inbox.mark_read(CONFIG, user_dir, message_id))


@app.route("/api/owner/email/<message_id>/archive", methods=["POST"])
def email_archive(message_id):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    audit.log_event(DATA_DIR, actor=user["username"], action="email.archive",
                    resource=message_id)
    return jsonify(email_inbox.archive_message(CONFIG, user_dir, message_id))


@app.route("/api/owner/email/<message_id>/flag", methods=["POST"])
def email_flag(message_id):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    return jsonify(email_inbox.flag_message(
        CONFIG, user_dir, message_id, flagged=bool(data.get("flagged", True))))


@app.route("/api/owner/email/settings", methods=["GET", "PUT"])
def email_settings():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    if request.method == "GET":
        return jsonify(email_inbox.load_settings(user_dir))
    data = request.get_json(silent=True) or {}
    current = email_inbox.load_settings(user_dir)
    current.update(data)
    email_inbox.save_settings(user_dir, current)
    return jsonify(current)


# ---------------------------------------------------------------------------
# Generic IMAP/SMTP email accounts — Yahoo, iCloud, Fastmail, custom domains
# ---------------------------------------------------------------------------

@app.route("/api/owner/email/imap/providers", methods=["GET"])
def email_imap_providers():
    """Return the list of preset providers so the UI can show a dropdown."""
    auth.require_user(ORBI_DIR, DATA_DIR)
    import imap_smtp
    out = []
    for pid, p in imap_smtp.PROVIDER_PRESETS.items():
        out.append({
            "id":            pid,
            "label":         p["label"],
            "imap_host":     p["imap_host"],
            "imap_port":     p["imap_port"],
            "imap_ssl":      p["imap_ssl"],
            "smtp_host":     p["smtp_host"],
            "smtp_port":     p["smtp_port"],
            "smtp_starttls": p["smtp_starttls"],
            "help":          p.get("help", ""),
            "help_url":      p.get("help_url", ""),
        })
    return jsonify({"providers": out})


@app.route("/api/owner/email/imap/accounts", methods=["GET", "POST"])
def email_imap_accounts():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    import imap_smtp
    if request.method == "GET":
        return jsonify({"accounts": imap_smtp.list_accounts(user_dir)})
    data = request.get_json(silent=True) or {}
    result = imap_smtp.add_account(
        user_dir,
        email_addr=data.get("email", ""),
        password=data.get("password", ""),
        provider=data.get("provider", "custom"),
        imap_host=data.get("imap_host") or None,
        imap_port=data.get("imap_port") or None,
        imap_ssl=data.get("imap_ssl") if "imap_ssl" in data else None,
        smtp_host=data.get("smtp_host") or None,
        smtp_port=data.get("smtp_port") or None,
        smtp_starttls=data.get("smtp_starttls") if "smtp_starttls" in data else None,
        label=data.get("label") or None,
    )
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/api/owner/email/imap/accounts/<account_id>", methods=["DELETE"])
def email_imap_remove(account_id: str):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    import imap_smtp
    ok = imap_smtp.remove_account(user_dir, account_id)
    return jsonify({"ok": ok})


@app.route("/api/owner/email/imap/accounts/<account_id>/test", methods=["POST"])
def email_imap_test(account_id: str):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    import imap_smtp
    result = imap_smtp.test_account(user_dir, account_id)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Office gaps — charts/graphs, PowerPoint, mail merge
# ---------------------------------------------------------------------------


def _save_and_token(file_bytes: bytes, filename: str, mime_hint: str = "") -> dict:
    """Save bytes into the workspace, scan, mint a download token, return
    {filename, workspace_path, download_url}."""
    ws = mod_workspace.workspace_path(CONFIG)
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / filename
    target.write_bytes(file_bytes)
    try:
        mod_workspace.scan(CONFIG, DATA_DIR)
    except Exception:
        pass
    try:
        token = file_fetch.mint_download_token(
            DATA_DIR, str(target), ttl_minutes=30, extra_allowed_roots=[ws])
        url = f"/download/{token}"
    except Exception:
        url = None
    return {"filename": filename, "workspace_path": str(target), "download_url": url}


@app.route("/api/owner/chart/from_data", methods=["POST"])
def chart_from_data():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    try:
        png = chart_gen.generate_chart(
            CONFIG,
            title=data.get("title", "Chart"),
            kind=data.get("kind", "bar"),
            data=data.get("data") or {},
            style=data.get("style", "modern"),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    fname = f"chart_{int(time.time())}.png"
    saved = _save_and_token(png, fname)
    audit.log_event(DATA_DIR, actor=user["username"], action="chart.from_data",
                    meta={"kind": data.get("kind"), "title": data.get("title")})
    return jsonify(saved)


@app.route("/api/owner/chart/from_request", methods=["POST"])
def chart_from_request():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    req_text = data.get("request", "")
    parsed = chart_gen.parse_chart_request(CONFIG, req_text)
    try:
        png = chart_gen.generate_chart(
            CONFIG,
            title=parsed.get("title", "Chart"),
            kind=parsed.get("kind", "bar"),
            data=parsed.get("data") or {},
        )
    except Exception as e:
        return jsonify({"error": str(e), "parsed": parsed}), 400
    fname = f"chart_{int(time.time())}.png"
    saved = _save_and_token(png, fname)
    saved["parsed"] = parsed
    audit.log_event(DATA_DIR, actor=user["username"], action="chart.from_request",
                    meta={"request": req_text[:120]})
    return jsonify(saved)


@app.route("/api/owner/pptx/outline", methods=["POST"])
def pptx_outline_route():
    auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    return jsonify(pptx_gen.build_outline(
        CONFIG,
        topic=data.get("topic", ""),
        target_slide_count=int(data.get("slide_count", 7)),
    ))


@app.route("/api/owner/pptx/build", methods=["POST"])
def pptx_build_route():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    try:
        biz = mod_business.load(DATA_DIR)
        result = pptx_gen.build_deck(
            CONFIG,
            topic=data.get("topic", ""),
            target_slide_count=int(data.get("slide_count", 7)),
            theme=data.get("theme", "modern"),
            business_info=biz,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    slug = re.sub(r"\W+", "_", (data.get("topic") or "deck")[:40]).strip("_")
    fname = f"deck_{slug}_{int(time.time())}.pptx"
    saved = _save_and_token(result["pptx_bytes"], fname)
    saved["outline"] = result.get("outline")
    saved["slide_count"] = result.get("slide_count")
    audit.log_event(DATA_DIR, actor=user["username"], action="pptx.build",
                    meta={"topic": data.get("topic"), "slides": result.get("slide_count")})
    return jsonify(saved)


# Need re module accessible here
import re  # noqa: E402


@app.route("/api/owner/pptx/from_outline", methods=["POST"])
def pptx_from_outline_route():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    outline = data.get("outline") or {}
    try:
        biz = mod_business.load(DATA_DIR)
        pptx_bytes = pptx_gen.render_deck(outline, theme=data.get("theme", "modern"),
                                           business_info=biz)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    title = outline.get("title", "deck")
    slug = re.sub(r"\W+", "_", title[:40]).strip("_") or "deck"
    fname = f"deck_{slug}_{int(time.time())}.pptx"
    saved = _save_and_token(pptx_bytes, fname)
    audit.log_event(DATA_DIR, actor=user["username"], action="pptx.from_outline")
    return jsonify(saved)


@app.route("/api/owner/mail_merge/preview", methods=["POST"])
def mail_merge_preview():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    template = data.get("template", "")
    contact_id = data.get("contact_id", "")
    extras = data.get("extras") or {}
    contacts_list = mod_contacts.list_all(user_dir)
    contact = next((c for c in contacts_list if c.get("id") == contact_id), None)
    if not contact:
        return jsonify({"error": "contact_not_found"}), 404
    return jsonify({"rendered": mail_merge.merge_one(template, contact, extras=extras)})


@app.route("/api/owner/mail_merge/run", methods=["POST"])
def mail_merge_run():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    try:
        result = mail_merge.merge_all(
            CONFIG, user_dir,
            template_text=data.get("template", ""),
            contact_ids=data.get("contact_ids") or [],
            target_format=data.get("target_format", "pdf"),
            extras=data.get("extras") or {},
            llm_personalize=bool(data.get("llm_personalize")),
        )
        # Mint a download token for the zip if it exists
        if result.get("zip_path"):
            try:
                ws = mod_workspace.workspace_path(CONFIG)
                token = file_fetch.mint_download_token(
                    DATA_DIR, result["zip_path"], ttl_minutes=60,
                    extra_allowed_roots=[ws])
                result["download_url"] = f"/download/{token}"
            except Exception as e:
                log.warning(f"mail_merge token mint failed: {e}")
                result["download_url"] = None
        audit.log_event(DATA_DIR, actor=user["username"], action="mail_merge.run",
                        meta={"count": len(result.get("merged", [])),
                              "format": data.get("target_format", "pdf")})
        return jsonify(result)
    except Exception as e:
        log.warning(f"mail_merge run failed: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Tier 3 — public booking widget (visitor books a time on the owner's calendar)
# ---------------------------------------------------------------------------


@app.route("/book", methods=["GET"])
def booking_page():
    """Public booking page. URL: /book?u=<username>"""
    return send_from_directory(STATIC_DIR, "booking.html")


@app.route("/api/public/booking/slots", methods=["GET"])
def booking_public_slots():
    """No auth — visitor pulling open times for an owner."""
    username = (request.args.get("u") or "").strip().lower()
    if not username:
        abort(404)
    user_rec = users_mod.get_user(DATA_DIR, username)
    if not user_rec:
        abort(404)
    user_dir = users_mod.get_user_dir(DATA_DIR, username)
    cfg = booking.get_booking_config(CONFIG, user_dir)
    if not cfg.get("enabled"):
        abort(404)
    slots = booking.get_public_availability(
        CONFIG, DATA_DIR, user_dir,
        duration_minutes=int(request.args.get("duration",
                                              cfg.get("duration_minutes", 30))),
        days_ahead=int(cfg.get("days_ahead", 14)),
    )
    return jsonify({"slots": slots, "config": {
        k: v for k, v in cfg.items() if k != "owner_email"
    }})


@app.route("/api/public/booking/book", methods=["POST"])
def booking_public_book():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    if not username:
        return jsonify({"error": "username required"}), 400
    user_rec = users_mod.get_user(DATA_DIR, username)
    if not user_rec:
        return jsonify({"error": "not_found"}), 404
    user_dir = users_mod.get_user_dir(DATA_DIR, username)
    try:
        result = booking.book_public_slot(
            CONFIG, DATA_DIR, user_dir,
            visitor_name=data.get("visitor_name", ""),
            visitor_email=data.get("visitor_email", ""),
            visitor_phone=data.get("visitor_phone", ""),
            start_iso=data.get("start_iso", ""),
            end_iso=data.get("end_iso", ""),
            duration_minutes=int(data.get("duration_minutes", 30)),
            notes=data.get("notes", ""),
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/owner/booking/config", methods=["GET", "PUT"])
def booking_owner_config():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    if request.method == "GET":
        return jsonify(booking.get_booking_config(CONFIG, user_dir))
    booking.set_booking_config(user_dir, request.get_json(silent=True) or {})
    return jsonify(booking.get_booking_config(CONFIG, user_dir))


# ---------------------------------------------------------------------------
# Tier 3 — birthdays + anniversaries
# ---------------------------------------------------------------------------


@app.route("/api/owner/birthdays/upcoming", methods=["GET"])
def birthdays_upcoming():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    days = int(request.args.get("days_ahead", "14"))
    return jsonify({"upcoming": birthdays.find_upcoming_dates(user_dir, days_ahead=days)})


@app.route("/api/owner/birthdays/draft", methods=["POST"])
def birthdays_draft():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    contact_id = data.get("contact_id", "")
    kind = data.get("kind", "birthday")
    # Find the contact
    contacts_list = mod_contacts.list_all(user_dir)
    contact = next((c for c in contacts_list if c.get("id") == contact_id), None)
    if not contact:
        return jsonify({"error": "contact_not_found"}), 404
    return jsonify({"text": birthdays.draft_card_text(CONFIG, contact, kind)})


@app.route("/api/owner/birthdays/sweep_now", methods=["POST"])
def birthdays_sweep_now():
    auth.require_role(ORBI_DIR, DATA_DIR, "owner")
    created = birthdays.run_sweep(CONFIG, DATA_DIR)
    return jsonify({"reminders_created": created})


# ---------------------------------------------------------------------------
# Tier 3 — translation
# ---------------------------------------------------------------------------


@app.route("/api/owner/translate", methods=["POST"])
def translate_route():
    auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    out = translation.translate(
        CONFIG, text,
        target_lang=data.get("target_lang", "en"),
        source_lang=data.get("source_lang"),
    )
    return jsonify({"translated": out})


@app.route("/api/owner/detect_language", methods=["POST"])
def detect_language_route():
    auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    return jsonify({"lang": translation.detect_language(data.get("text", ""))})


# ---------------------------------------------------------------------------
# Tier 3 — voice notes (record → transcribe → quick_capture)
# ---------------------------------------------------------------------------


@app.route("/api/owner/voice_notes/process", methods=["POST"])
def voice_notes_process():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    if "audio" not in request.files:
        return jsonify({"error": "audio file required (multipart 'audio' field)"}), 400
    audio = request.files["audio"].read()
    hint = (request.form.get("hint") or "").strip()
    try:
        result = voice_notes.process(CONFIG, user_dir, audio, hint=hint)
        audit.log_event(DATA_DIR, actor=user["username"],
                        action=f"voice_note.{result.get('capture_kind','?')}",
                        meta={"chars": len(result.get("transcript", ""))})
        return jsonify(result)
    except Exception as e:
        log.warning(f"voice_note process failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/owner/voice_notes", methods=["GET"])
def voice_notes_list():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    try:
        return jsonify({"voice_notes": voice_notes.list_recordings(user_dir)})
    except AttributeError:
        # Helper not implemented yet — return empty
        return jsonify({"voice_notes": []})


# ---------------------------------------------------------------------------
# Tier 3 — style learner (draft in owner's voice)
# ---------------------------------------------------------------------------


@app.route("/api/owner/style/refresh", methods=["POST"])
def style_refresh():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    result = style_learner.index_owner_sent(
        CONFIG, user_dir, limit=int(data.get("limit", 200)))
    audit.log_event(DATA_DIR, actor=user["username"],
                    action="style.refresh", meta=result)
    return jsonify(result)


@app.route("/api/owner/style/status", methods=["GET"])
def style_status():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    return jsonify(style_learner.corpus_status(user_dir))


@app.route("/api/owner/style/draft", methods=["POST"])
def style_draft():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    text = style_learner.draft_in_owner_voice(
        CONFIG, user_dir,
        draft_context=data.get("draft_context", ""),
        what_to_say=data.get("what_to_say", ""),
    )
    return jsonify({"draft": text})


# ---------------------------------------------------------------------------
# Tier 2 — meeting scheduler
# ---------------------------------------------------------------------------


@app.route("/api/owner/scheduler/find_slots", methods=["POST"])
def scheduler_find_slots():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    return jsonify({"slots": meeting_scheduler.find_open_slots(
        user_dir,
        duration_minutes=int(data.get("duration_minutes", 30)),
        days_ahead=int(data.get("days_ahead", 7)),
    )})


@app.route("/api/owner/scheduler/propose", methods=["POST"])
def scheduler_propose():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    return jsonify(meeting_scheduler.propose_meeting(
        user_dir,
        attendee_name=data.get("attendee_name", ""),
        attendee_email=data.get("attendee_email", ""),
        duration_minutes=int(data.get("duration_minutes", 30)),
        days_ahead=int(data.get("days_ahead", 7)),
    ))


@app.route("/api/owner/scheduler/book", methods=["POST"])
def scheduler_book():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    result = meeting_scheduler.book_meeting(
        user_dir,
        attendee_name=data.get("attendee_name", ""),
        attendee_email=data.get("attendee_email", ""),
        start_iso=data.get("start_iso", ""),
        end_iso=data.get("end_iso", ""),
        title=data.get("title"),
        notes=data.get("notes", ""),
    )
    audit.log_event(DATA_DIR, actor=user["username"], action="scheduler.book",
                    meta={"attendee": data.get("attendee_email")})
    return jsonify(result)


@app.route("/api/owner/scheduler/parse_reply", methods=["POST"])
def scheduler_parse_reply():
    auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    return jsonify(meeting_scheduler.parse_reschedule_request(data.get("text", "")) or {})


# ---------------------------------------------------------------------------
# Tier 2 — safe-send email (gated by category whitelist)
# ---------------------------------------------------------------------------


@app.route("/api/owner/safe_send/preferences", methods=["GET", "PUT"])
def safe_send_preferences():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    if request.method == "GET":
        return jsonify(safe_send.get_preferences(user_dir))
    safe_send.set_preferences(user_dir, request.get_json(silent=True) or {})
    return jsonify(safe_send.get_preferences(user_dir))


@app.route("/api/owner/safe_send/send", methods=["POST"])
def safe_send_route():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    result = safe_send.send_email(
        CONFIG, user_dir,
        to_email=data.get("to_email", ""),
        subject=data.get("subject", ""),
        body=data.get("body", ""),
        category=data.get("category", ""),
    )
    audit.log_event(DATA_DIR, actor=user["username"],
                    action=f"safe_send.{result.get('action','unknown')}",
                    meta={"to": data.get("to_email"), "category": data.get("category")})
    return jsonify(result)


# ---------------------------------------------------------------------------
# Tier 2 — customer thread (unified timeline per contact)
# ---------------------------------------------------------------------------


@app.route("/api/owner/customer_thread/top", methods=["GET"])
def customer_thread_top():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    limit = int(request.args.get("limit", "10"))
    return jsonify({"contacts": customer_thread.list_top_contacts(
        CONFIG, DATA_DIR, user_dir, limit=limit)})


@app.route("/api/owner/customer_thread", methods=["GET"])
def customer_thread_by_query():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    q = request.args.get("email") or request.args.get("name") or request.args.get("phone") or request.args.get("q") or ""
    if not q:
        return jsonify({"error": "missing email/name/phone/q"}), 400
    return jsonify(customer_thread.build_thread(CONFIG, DATA_DIR, user_dir, q))


@app.route("/api/owner/customer_thread/<contact_ref>", methods=["GET"])
def customer_thread_by_id(contact_ref):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    return jsonify(customer_thread.build_thread(CONFIG, DATA_DIR, user_dir, contact_ref))


# ---------------------------------------------------------------------------
# Tier 2 — contextual reminders (extract promises from chat/email text)
# ---------------------------------------------------------------------------


@app.route("/api/owner/promises/extract", methods=["POST"])
def promises_extract():
    auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    return jsonify({"promises": contextual_reminders.extract_promises(data.get("text", ""))})


@app.route("/api/owner/promises/auto_create", methods=["POST"])
def promises_auto_create():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    data = request.get_json(silent=True) or {}
    created = contextual_reminders.auto_create_reminders(
        user_dir, data.get("text", ""), data.get("source_id", ""))
    return jsonify({"created": created, "count": len(created)})


# ---------------------------------------------------------------------------
# Tier 2 — review autoresponder
# ---------------------------------------------------------------------------


@app.route("/api/owner/reviews/new", methods=["GET"])
def reviews_new():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    return jsonify(review_responder.scan_recent_reviews(CONFIG, user_dir))


@app.route("/api/owner/reviews/<review_id>/mark_reviewed", methods=["POST"])
def reviews_mark(review_id):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    user_dir = users_mod.get_user_dir(DATA_DIR, user["username"])
    ok = review_responder.mark_reviewed(user_dir, review_id)
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404


# ---------------------------------------------------------------------------
# Tier 2 — image generation for social posts / flyers
# ---------------------------------------------------------------------------


@app.route("/api/owner/image_gen", methods=["POST"])
def image_gen_route():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    kind = (data.get("kind") or "social_post").strip()
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    try:
        png_bytes = image_gen.generate(CONFIG, prompt, kind=kind)
    except Exception as e:
        log.warning(f"image_gen failed: {e}")
        return jsonify({"error": str(e)}), 500
    ws = mod_workspace.workspace_path(CONFIG)
    saved = image_gen.save_to_workspace(png_bytes, prompt, ws)
    try:
        mod_workspace.scan(CONFIG, DATA_DIR)
    except Exception:
        pass
    try:
        token = file_fetch.mint_download_token(
            DATA_DIR, str(saved), ttl_minutes=30, extra_allowed_roots=[ws])
        download_url = f"/download/{token}"
    except Exception:
        download_url = None
    audit.log_event(DATA_DIR, actor=user["username"], action="image_gen",
                    meta={"kind": kind, "prompt": prompt[:80], "file": saved.name})
    return jsonify({
        "filename": saved.name,
        "workspace_path": str(saved),
        "download_url": download_url,
    })


@app.route("/api/internal/notify", methods=["POST"])
def internal_notify():
    if request.headers.get("X-Watchdog") != "1":
        abort(403)
    data = request.get_json(silent=True) or {}
    title = data.get("title") or "Orby alert"
    body  = data.get("body") or ""
    urgent = bool(data.get("urgent"))
    log.info(f"[watchdog notify] {title}: {body}")
    event = "watchdog_failed" if urgent else "watchdog_rollback"
    notify.send(CONFIG, DATA_DIR, event=event, title=title, body=body, url="/owner")
    return jsonify({"status": "queued"})

# ---------------------------------------------------------------------------
# Push subscription endpoints (PWA registers here)
# ---------------------------------------------------------------------------

@app.route("/api/push/vapid_public_key", methods=["GET"])
def push_vapid_pub():
    keys = notify.get_vapid_keys(DATA_DIR)
    if not keys:
        return jsonify({"error": "vapid_not_available",
                        "message": "Push not configured on server"}), 503
    return jsonify({"public_key": keys["public_key"]})

@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    auth.require_owner(ORBI_DIR)
    sub = request.get_json(silent=True) or {}
    if not sub.get("endpoint"):
        return jsonify({"error": "invalid_subscription"}), 400
    subs = notify.load_subscriptions(DATA_DIR)
    # de-dupe by endpoint
    subs = [s for s in subs if s.get("endpoint") != sub["endpoint"]]
    subs.append(sub)
    notify.save_subscriptions(DATA_DIR, subs)
    return jsonify({"status": "subscribed", "count": len(subs)})

@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    auth.require_owner(ORBI_DIR)
    sub = request.get_json(silent=True) or {}
    endpoint = sub.get("endpoint", "")
    subs = notify.load_subscriptions(DATA_DIR)
    subs = [s for s in subs if s.get("endpoint") != endpoint]
    notify.save_subscriptions(DATA_DIR, subs)
    return jsonify({"status": "unsubscribed", "count": len(subs)})

@app.route("/api/push/test", methods=["POST"])
def push_test():
    auth.require_owner(ORBI_DIR)
    notify.send(CONFIG, DATA_DIR,
                event="new_message",
                title="Orby test notification",
                body="If you're seeing this, push notifications are working.",
                url="/owner")
    return jsonify({"status": "test_sent"})

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    """Stray paths (typos, /:, /foo, /bar) → bounce to the chat shell.
    Keeps the experience friendly for visitors who fat-fingered a URL."""
    from flask import redirect
    return redirect("/", code=302)

START_TIME = time.time()

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    voice.register(app, CONFIG, DATA_DIR)
    try:
        mod_workspace.start_watcher(CONFIG, DATA_DIR)
    except Exception as e:
        log.warning(f"workspace watcher failed to start: {e}")
    try:
        mod_catalog.start_watcher(DATA_DIR)
    except Exception as e:
        log.warning(f"catalog watcher failed to start: {e}")
    try:
        mod_learning.start_delivery_dispatcher(CONFIG, DATA_DIR)
    except Exception as e:
        log.warning(f"learning-loop dispatcher failed to start: {e}")
    try:
        backup.start_daily_backup(CONFIG, DATA_DIR)
    except Exception as e:
        log.warning(f"backup scheduler failed to start: {e}")
    server = CONFIG.get("server", {})
    host = server.get("host", "127.0.0.1")
    port = int(server.get("port", 5050))
    log.info(f"Orby starting on {host}:{port}")
    log.info(f"  Brain:       {(CONFIG.get('brain') or {}).get('url', 'not configured')}")
    log.info(f"  HuggingFace: {'enabled' if (CONFIG.get('huggingface') or {}).get('enabled') else 'disabled'}")
    log.info(f"  Local LLM:   {'enabled' if (CONFIG.get('local_llm') or {}).get('enabled') else 'disabled'}")
    log.info(f"  Data dir:    {DATA_DIR}")
    app.run(host=host, port=port, threaded=True)

if __name__ == "__main__":
    main()
