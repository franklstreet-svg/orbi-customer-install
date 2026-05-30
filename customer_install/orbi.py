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
import ad_gen
import updater
import friend_checkin
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

    # Capture lead / order / callback if visitor info is present.
    # If the chat widget hasn't collected name/phone in its form, try to
    # pull them out of the message body itself (e.g. "call me back at
    # 555-1234, this is Bob"). If we still can't find a phone OR email,
    # the LLM reply will ask for it on the next turn — we override the
    # reply below in that case so the visitor isn't left hanging while
    # their lead silently vaporizes.
    capture_kind = _detect_capture(user_msg, scope)
    if capture_kind:
        extracted_name  = visitor.get("name")
        extracted_phone = visitor.get("phone")
        extracted_email = visitor.get("email")
        # Always run extraction so widget data + body-extracted data merge.
        body_text = _conversation_text(history, user_msg)
        if not extracted_phone:
            extracted_phone = _extract_phone(body_text)
        if not extracted_email:
            extracted_email = _extract_email(body_text)
        if not extracted_name:
            extracted_name = _extract_name(body_text)
        has_contact = bool(extracted_phone or extracted_email)
        if has_contact:
            try:
                captured = mod_messages.capture(
                    DATA_DIR,
                    msg_type=capture_kind,
                    from_name=extracted_name,
                    from_phone=extracted_phone,
                    from_email=extracted_email,
                    body=user_msg,
                    source="chat",
                )
                event_map = {"order": "new_order", "lead": "new_lead",
                             "callback": "new_lead", "voicemail": "new_voicemail"}
                from_label = extracted_name or extracted_phone or extracted_email or 'Unknown'
                notify.send(
                    CONFIG, DATA_DIR,
                    event=event_map.get(capture_kind, "new_message"),
                    title=f"New {capture_kind} from {from_label}",
                    body=user_msg[:200],
                    url="/owner",
                )
            except Exception as e:
                log.warning(f"capture failed: {e}")
        else:
            # No contact info found — override the LLM reply with a direct
            # ask so the visitor knows we need their info, AND so the next
            # message they send (with their phone) actually completes the
            # capture instead of disappearing into the LLM's wording.
            log.info("capture intent (%s) but no contact info — asking for it",
                      capture_kind)
            ask = ("Of course — what's the best name and phone number "
                   "for the owner to reach you at?")
            return jsonify({
                "reply": ask, "tier": "local", "latency_ms": 0,
                "source": "capture_needs_contact",
                "capture_pending": capture_kind,
            })

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
        kw in msg for kw in ("call me back", "callback", "call back",
                              "someone to call", "have someone call")
    ):
        return "callback"
    if scope.get("public_can_request_quotes") and any(
        kw in msg for kw in ("quote", "estimate", "how much would", "price for")
    ):
        return "lead"
    return None


# ── Contact-info extractors for public chat ──────────────────────────────
# Used when the chat widget doesn't pre-collect name/phone in a form. We
# pull contact details out of whatever the visitor typed in the message
# (or recent history) so leads aren't lost when someone says "call me
# back at 555-1234, name's Bob" without filling out a form.

_PHONE_RE = _re.compile(
    r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)
_EMAIL_RE = _re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)
_NAME_INTRO_RE = _re.compile(
    # The intro phrase is case-insensitive ("My name is" / "my name is")
    # but the captured name must START WITH AN UPPERCASE LETTER so we
    # don't slurp trailing lowercase words like "reaching" in "this is
    # John Smith reaching out".
    r"(?i:(?:my\s+name\s+is|i\s+am|i'?m|this\s+is|it'?s|it\s+is|name'?s))\s+"
    r"([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){0,2})\b",
)


def _conversation_text(history: list, current_msg: str) -> str:
    """Concatenate visitor-side messages (history + current) into one
    string for regex scanning. Server-side responses aren't included —
    we only mine what the visitor said."""
    parts = []
    for h in history or []:
        if (h or {}).get("role") == "user":
            parts.append(str(h.get("content") or ""))
    parts.append(current_msg or "")
    return " ".join(parts)


def _extract_phone(text: str) -> str | None:
    m = _PHONE_RE.search(text or "")
    if not m:
        return None
    # Normalize to digits-only with a leading + if E.164-like, otherwise
    # keep formatting reasonable. Strip non-digit chars except leading +.
    raw = m.group(0)
    digits = _re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return raw  # leave as-is for weird formats


def _extract_email(text: str) -> str | None:
    m = _EMAIL_RE.search(text or "")
    return m.group(0).lower() if m else None


def _extract_name(text: str) -> str | None:
    """Find a likely visitor name from intro phrases ('my name is X',
    'I'm X', 'this is X'). Capitalized 1-3 word run after the intro."""
    m = _NAME_INTRO_RE.search(text or "")
    if not m:
        return None
    candidate = m.group(1).strip()
    # Reject pronouns / very short / numeric-looking
    if not candidate or len(candidate) < 2:
        return None
    if candidate.lower() in {"good", "fine", "ok", "okay", "calling",
                              "interested", "looking", "wondering", "trying"}:
        return None
    return candidate

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
        "tone": personality.get("tone", "friend"),
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

# ── Password reset (email-based) ────────────────────────────────────────
# Standard SaaS flow:
#   1. User clicks "Forgot password" on /owner/login
#   2. POSTs email to /api/owner/forgot_password
#   3. We generate a one-time token (24-hour expiry), store it, email the
#      reset link to the user's address via their own connected email
#      account (or, for owner, the configured owner email)
#   4. User clicks link → /owner/reset_password?token=X → enters new pw
#   5. /api/owner/reset_password verifies token, sets new password
#
# Tokens stored in data/password_reset_tokens.json. Single-use, expire
# after 24 hours. Cryptographically random (secrets.token_urlsafe).

_RESET_TOKEN_FILE = "password_reset_tokens.json"
_RESET_TOKEN_TTL = 24 * 3600


def _reset_tokens_path():
    return DATA_DIR / _RESET_TOKEN_FILE


def _load_reset_tokens() -> dict:
    p = _reset_tokens_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_reset_tokens(tokens: dict) -> None:
    p = _reset_tokens_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    tmp.replace(p)


def _create_reset_token(target_user: str, email: str) -> str:
    """Generate a one-time reset token for a username. Returns the token."""
    import secrets
    tokens = _load_reset_tokens()
    # Cleanup expired
    now = int(time.time())
    tokens = {k: v for k, v in tokens.items()
              if int(v.get("expires_at", 0)) > now}
    token = secrets.token_urlsafe(32)
    tokens[token] = {
        "username":   target_user,
        "email":      email,
        "created_at": now,
        "expires_at": now + _RESET_TOKEN_TTL,
    }
    _save_reset_tokens(tokens)
    return token


def _consume_reset_token(token: str) -> dict | None:
    """Validate a token and remove it from the store. Returns the token
    record (with username + email) or None if invalid/expired."""
    tokens = _load_reset_tokens()
    rec = tokens.get(token)
    if not rec:
        return None
    if int(rec.get("expires_at", 0)) < int(time.time()):
        del tokens[token]
        _save_reset_tokens(tokens)
        return None
    del tokens[token]
    _save_reset_tokens(tokens)
    return rec


def _send_reset_email(to_addr: str, reset_url: str, name: str = "") -> dict:
    """Send a password reset email. Tries owner's first connected email
    account; falls back to SMTP env vars (ORBI_SMTP_HOST etc.) if any."""
    # Try owner's configured email accounts (Gmail/Outlook/generic SMTP)
    try:
        # Owner's mailbox = whichever user folder belongs to the owner.
        # For the owner herself, that's the owner user. For staff resets,
        # the owner still sends FROM their own account.
        owner_user = (CONFIG.get("owner") or {}).get("name", "owner")
        from pathlib import Path as _P
        owner_dir = users_mod.get_user_dir(DATA_DIR, owner_user)
        accounts = imap_smtp.list_accounts(owner_dir)
        for a in accounts:
            if a.get("smtp_host"):
                subj = "Reset your Orby password"
                body = (
                    f"{('Hi ' + name + ',') if name else 'Hi,'}\n\n"
                    "You (or someone) requested a password reset for your "
                    "Orby login. Click the link below to set a new password. "
                    "The link expires in 24 hours.\n\n"
                    f"{reset_url}\n\n"
                    "If you didn't request this, you can ignore this email — "
                    "your current password still works.\n\n"
                    "— Orby"
                )
                result = imap_smtp.send_email(
                    owner_dir, a["id"], to=to_addr,
                    subject=subj, body=body)
                if result.get("ok"):
                    return {"ok": True, "via": "owner_account"}
    except Exception as e:
        log.warning(f"reset email via owner account failed: {e}")

    # Fallback: SMTP env vars (rare, but available)
    smtp_host = os.environ.get("ORBI_SMTP_HOST")
    if smtp_host:
        try:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText(
                f"Reset link (24h): {reset_url}\n\n"
                "If you didn't request this, ignore — your password still works.")
            msg["Subject"] = "Reset your Orby password"
            msg["From"] = os.environ.get("ORBI_SMTP_FROM", smtp_host)
            msg["To"] = to_addr
            port = int(os.environ.get("ORBI_SMTP_PORT", "587"))
            with smtplib.SMTP(smtp_host, port, timeout=20) as s:
                if os.environ.get("ORBI_SMTP_STARTTLS", "1") == "1":
                    s.ehlo(); s.starttls(); s.ehlo()
                user = os.environ.get("ORBI_SMTP_USER")
                if user:
                    s.login(user, os.environ.get("ORBI_SMTP_PASS", ""))
                s.send_message(msg)
            return {"ok": True, "via": "smtp_env"}
        except Exception as e:
            log.warning(f"reset email via SMTP env failed: {e}")

    return {"ok": False, "error": "no_email_configured"}


@app.route("/api/owner/forgot_password", methods=["POST"])
def owner_forgot_password():
    """Public — no auth. Triggers a reset email if the address matches
    a known user. Always returns 'ok' to avoid leaking which addresses
    have accounts."""
    data = request.get_json(silent=True) or {}
    email_addr = (data.get("email") or "").strip().lower()
    if not email_addr or "@" not in email_addr:
        return jsonify({"error": "valid email required"}), 400

    # Find the user by email — check owner config first, then user registry
    target_user = None
    owner = CONFIG.get("owner", {})
    if (owner.get("email") or "").strip().lower() == email_addr:
        target_user = owner.get("name", "owner")
    else:
        for u in users_mod.list_users(DATA_DIR):
            if (u.get("email") or "").strip().lower() == email_addr:
                target_user = u["username"]
                break

    if not target_user:
        # Don't leak which emails are registered. Return ok anyway.
        log.info(f"forgot_password: no user for {email_addr!r}")
        return jsonify({"status": "ok",
                        "message": "If that email is on file, a reset link has been sent."})

    token = _create_reset_token(target_user, email_addr)
    base, _ = _resolve_install_base()
    reset_url = f"{base}/owner/reset_password?token={token}"
    send_result = _send_reset_email(email_addr, reset_url, name=target_user)
    audit.log_event(DATA_DIR, actor=email_addr,
                    action="owner.forgot_password.sent",
                    meta={"username": target_user,
                          "sent": send_result.get("ok", False)})
    return jsonify({"status": "ok",
                    "message": "If that email is on file, a reset link has been sent.",
                    "email_sent": send_result.get("ok", False)})


@app.route("/owner/reset_password", methods=["GET"])
def owner_reset_password_page():
    """Public reset form. Token in query string. Renders inline HTML."""
    token = request.args.get("token", "")
    # We don't validate here (consume on submit) — just render the form
    safe_token = (token or "")[:200]   # cap size for the hidden field
    return Response(f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reset Orby password</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #0b0f1a;
          color: #eaf0ff; min-height: 100vh; display: flex; align-items: center;
          justify-content: center; padding: 24px; margin: 0; }}
  .card {{ background: #131a2e; padding: 28px 24px; border-radius: 14px;
           max-width: 380px; width: 100%; }}
  h1 {{ margin: 0 0 8px; font-size: 22px; }}
  p {{ color: #9aa4c0; font-size: 13px; margin: 0 0 16px; }}
  input {{ width: 100%; padding: 10px; background: #1a2240; color: #eaf0ff;
           border: 1px solid #2c3756; border-radius: 6px; font-size: 14px;
           box-sizing: border-box; }}
  label {{ display: block; font-size: 12px; color: #9aa4c0; margin: 12px 0 4px; }}
  button {{ width: 100%; padding: 12px; background: #8b5cf6; color: white;
            border: none; border-radius: 6px; font-size: 14px; font-weight: 600;
            margin-top: 16px; cursor: pointer; }}
  button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  #msg {{ margin-top: 12px; font-size: 13px; }}
  .ok {{ color: #4ade80; }}
  .err {{ color: #ff7a7a; }}
</style></head><body>
<div class="card">
  <h1>Reset your password</h1>
  <p>Enter a new password below. Minimum 8 characters.</p>
  <form id="resetForm">
    <input type="hidden" name="token" value="{safe_token}">
    <label for="pw">New password</label>
    <input type="password" id="pw" name="password" required minlength="8" autofocus>
    <label for="pw2">Confirm new password</label>
    <input type="password" id="pw2" name="confirm" required minlength="8">
    <button type="submit">Set new password</button>
    <div id="msg"></div>
  </form>
</div>
<script>
  document.getElementById("resetForm").addEventListener("submit", async (e) => {{
    e.preventDefault();
    const pw = document.getElementById("pw").value;
    const pw2 = document.getElementById("pw2").value;
    const msg = document.getElementById("msg");
    if (pw !== pw2) {{ msg.className = "err"; msg.textContent = "Passwords don't match."; return; }}
    const tok = e.target.elements.token.value;
    const r = await fetch("/api/owner/reset_password", {{
      method: "POST", headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{token: tok, password: pw}})
    }});
    const data = await r.json();
    if (r.ok) {{
      msg.className = "ok"; msg.textContent = "Password set. Redirecting to login…";
      setTimeout(() => window.location.href = "/owner/login", 1500);
    }} else {{
      msg.className = "err"; msg.textContent = data.error || "Reset failed.";
    }}
  }});
</script>
</body></html>""", mimetype="text/html")


@app.route("/api/owner/reset_password", methods=["POST"])
def owner_reset_password():
    """Verify token + set new password. Public — no auth (the token IS
    the auth)."""
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    new_pw = data.get("password", "")
    if not token:
        return jsonify({"error": "token required"}), 400
    if len(new_pw) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    rec = _consume_reset_token(token)
    if not rec:
        return jsonify({"error": "Reset link expired or already used"}), 401

    target_user = rec.get("username")
    # Owner reset (target_user matches owner.name) vs staff reset
    owner = CONFIG.get("owner", {})
    if target_user == owner.get("name", "owner"):
        owner["_password_hash"] = auth.hash_password(new_pw)
        CONFIG["owner"] = owner
        save_config(CONFIG)
        audit.log_event(DATA_DIR, actor=rec.get("email"),
                        action="owner.password_reset_via_email")
    else:
        ok = users_mod.change_password(DATA_DIR, target_user, new_pw)
        if not ok:
            return jsonify({"error": "User no longer exists"}), 404
        audit.log_event(DATA_DIR, actor=rec.get("email"),
                        action="staff.password_reset_via_email",
                        meta={"username": target_user})
    return jsonify({"status": "ok"})


# ── Staff CRUD ──────────────────────────────────────────────────────────
# Owner-only. List / add / deactivate / generate-reset-link for staff users.
# Backed by users.py which already has:
#   add_user, deactivate_user (archive-not-delete with 90-day purge),
#   change_password, list_users (active), list_archived,
#   set_purge_hold (owner can prevent purge), transfer_items

# ── Internal messaging (staff-to-staff within one install) ────────────────

from modules import internal_messages as mod_imsg


@app.route("/api/owner/whoami", methods=["GET"])
def owner_whoami():
    """Returns the logged-in user's identity. Used by the dashboard JS
    to know who 'me' is for filtering inbound vs outbound messages."""
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    return jsonify({
        "username":     user.get("username"),
        "display_name": user.get("display_name"),
        "role":         user.get("role"),
        "email":        user.get("email"),
    })


@app.route("/api/owner/internal_messages", methods=["GET"])
def internal_messages_list():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    only_unread = request.args.get("unread") == "1"
    limit = int(request.args.get("limit", "200"))
    active = [u.get("username") for u in users_mod.list_users(DATA_DIR)]
    return jsonify({
        "messages": mod_imsg.list_for_user(DATA_DIR, user["username"],
                                            limit=limit, only_unread=only_unread,
                                            active_usernames=active),
        "unread_count": mod_imsg.unread_count(DATA_DIR, user["username"],
                                                active_usernames=active),
    })


@app.route("/api/owner/internal_messages/thread/<other>", methods=["GET"])
def internal_messages_thread(other):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    return jsonify({
        "thread": mod_imsg.thread_with(DATA_DIR, user["username"], other,
                                        limit=200),
    })


@app.route("/api/owner/internal_messages", methods=["POST"])
def internal_messages_send():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    to_user = (data.get("to") or "").strip().lower()
    body = data.get("body") or ""
    if not to_user or not body:
        return jsonify({"error": "to + body required"}), 400
    # Verify recipient exists + is active
    recipient = users_mod.get_user(DATA_DIR, to_user)
    if not recipient or recipient.get("status") != "active":
        return jsonify({"error": "recipient not found or inactive"}), 404
    try:
        entry = mod_imsg.send(
            DATA_DIR,
            from_user=user["username"],
            from_name=user.get("display_name", user["username"]),
            to_user=to_user,
            to_name=recipient.get("display_name", to_user),
            body=body, via="manual")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # Notify recipient via push
    try:
        notify.send(CONFIG, DATA_DIR, event="new_message",
                     title=f"Message from {entry['from_name']}",
                     body=body[:140], url="/owner#messages")
    except Exception:
        log.exception("internal msg notify failed")
    audit.log_event(DATA_DIR, actor=user["username"],
                    action="internal_msg.sent",
                    meta={"to": to_user, "via": "manual"})
    return jsonify({"status": "ok", "message": entry})


@app.route("/api/owner/internal_messages/<msg_id>/read", methods=["POST"])
def internal_messages_mark_read(msg_id):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    ok = mod_imsg.mark_read(DATA_DIR, msg_id, by_user=user["username"])
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404


@app.route("/api/owner/internal_messages/mark_all_read", methods=["POST"])
def internal_messages_mark_all_read():
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    active = [u.get("username") for u in users_mod.list_users(DATA_DIR)]
    n = mod_imsg.mark_all_read(DATA_DIR, user["username"],
                                active_usernames=active)
    return jsonify({"status": "ok", "marked": n})


# ── Internal Groups (multi-recipient threads) ──────────────────────────────

def _group_members_or_400(data, key="members"):
    raw = data.get(key) or []
    if not isinstance(raw, list):
        return None, ("members must be a list", 400)
    members = [(m or "").strip().lower() for m in raw if (m or "").strip()]
    return members, None


@app.route("/api/owner/groups", methods=["GET"])
def groups_list():
    """List groups the current user can see. Owner sees all stored groups
    plus the virtual Whole Team. Staff see only groups they belong to plus
    Whole Team."""
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    is_owner = user.get("role") == "owner"
    groups = mod_imsg.list_groups(DATA_DIR,
                                    for_user=None if is_owner else user["username"])
    active = users_mod.list_users(DATA_DIR)
    # Enrich members with display names for the UI
    by_uname = {u["username"]: u.get("display_name", u["username"])
                 for u in active}
    for g in groups:
        g["member_names"] = [by_uname.get(m, m) for m in g.get("members", [])]
    # Virtual "Whole Team" — always available, members resolved live
    all_members = [u["username"] for u in active]
    all_member_names = [u.get("display_name", u["username"]) for u in active]
    virtual_all = {
        "id":           mod_imsg.ALL_GROUP_ID,
        "name":         mod_imsg.ALL_GROUP_NAME,
        "members":      all_members,
        "member_names": all_member_names,
        "virtual":      True,
    }
    return jsonify({"groups": [virtual_all] + groups})


@app.route("/api/owner/groups", methods=["POST"])
def groups_create():
    """Create a new group. Owner only — they curate groups."""
    owner_session = auth.require_owner(ORBI_DIR)
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    members, err = _group_members_or_400(data)
    if err:
        return jsonify({"error": err[0]}), err[1]
    if not name:
        return jsonify({"error": "name required"}), 400
    # Ensure all members exist and are active
    active = {u["username"] for u in users_mod.list_users(DATA_DIR)}
    bad = [m for m in members if m not in active]
    if bad:
        return jsonify({"error": f"unknown or inactive users: {', '.join(bad)}"}), 400
    try:
        entry = mod_imsg.create_group(DATA_DIR, name=name, members=members,
                                       created_by=user["username"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    audit.log_event(DATA_DIR, actor=user["username"],
                    action="group.created",
                    meta={"group_id": entry["id"], "name": name,
                          "members": members})
    return jsonify({"status": "ok", "group": entry})


@app.route("/api/owner/groups/<group_id>", methods=["PATCH"])
def groups_update(group_id: str):
    """Rename and/or update members. Owner only."""
    if group_id == mod_imsg.ALL_GROUP_ID:
        return jsonify({"error": "Whole Team is built-in and can't be edited"}), 400
    auth.require_owner(ORBI_DIR)
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    kw = {}
    if "name" in data:
        kw["name"] = data["name"]
    if "members" in data:
        members, err = _group_members_or_400(data)
        if err:
            return jsonify({"error": err[0]}), err[1]
        kw["members"] = members
    g = mod_imsg.update_group(DATA_DIR, group_id, **kw)
    if not g:
        return jsonify({"error": "group not found"}), 404
    audit.log_event(DATA_DIR, actor=user["username"],
                    action="group.updated",
                    meta={"group_id": group_id, **{k: kw[k] for k in kw}})
    return jsonify({"status": "ok", "group": g})


@app.route("/api/owner/groups/<group_id>", methods=["DELETE"])
def groups_delete(group_id: str):
    if group_id == mod_imsg.ALL_GROUP_ID:
        return jsonify({"error": "Whole Team is built-in and can't be deleted"}), 400
    auth.require_owner(ORBI_DIR)
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    ok = mod_imsg.delete_group(DATA_DIR, group_id)
    if ok:
        audit.log_event(DATA_DIR, actor=user["username"],
                        action="group.deleted",
                        meta={"group_id": group_id})
    return jsonify({"status": "ok" if ok else "not_found"}), 200 if ok else 404


@app.route("/api/owner/internal_messages/group_thread/<group_id>",
            methods=["GET"])
def internal_messages_group_thread(group_id: str):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    # Access check
    if group_id == mod_imsg.ALL_GROUP_ID:
        active = {u["username"] for u in users_mod.list_users(DATA_DIR)}
        if user["username"] not in active:
            return jsonify({"error": "not in group"}), 403
    else:
        g = mod_imsg.get_group(DATA_DIR, group_id)
        if not g:
            return jsonify({"error": "group not found"}), 404
        if user["username"] not in set(g.get("members", [])) \
                and user.get("role") != "owner":
            return jsonify({"error": "not in group"}), 403
    return jsonify({
        "thread": mod_imsg.group_thread(DATA_DIR, group_id, limit=200),
    })


@app.route("/api/owner/internal_messages/group/<group_id>", methods=["POST"])
def internal_messages_group_send(group_id: str):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    body = data.get("body") or ""
    if not body.strip():
        return jsonify({"error": "body required"}), 400
    # Resolve group + permission + member list
    if group_id == mod_imsg.ALL_GROUP_ID:
        active = users_mod.list_users(DATA_DIR)
        if user["username"] not in {u["username"] for u in active}:
            return jsonify({"error": "not in group"}), 403
        members = [u["username"] for u in active]
        group_name = mod_imsg.ALL_GROUP_NAME
    else:
        g = mod_imsg.get_group(DATA_DIR, group_id)
        if not g:
            return jsonify({"error": "group not found"}), 404
        members = list(g.get("members", []))
        if user["username"] not in members and user.get("role") != "owner":
            return jsonify({"error": "not in group"}), 403
        # If owner is sending and not a member, still let it through; they
        # see all groups anyway.
        if user["username"] not in members:
            members = members + [user["username"]]
        group_name = g.get("name") or "Group"
    try:
        entry = mod_imsg.send_to_group(
            DATA_DIR,
            group_id=group_id, group_name=group_name,
            member_usernames=members,
            from_user=user["username"],
            from_name=user.get("display_name", user["username"]),
            body=body, via="manual")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # Push-notify each member except sender
    for m in members:
        if m == user["username"]:
            continue
        try:
            notify.send(CONFIG, DATA_DIR, event="new_message",
                         title=f"{group_name}: {entry['from_name']}",
                         body=body[:140], url="/owner#messages")
        except Exception:
            pass
    audit.log_event(DATA_DIR, actor=user["username"],
                    action="group_msg.sent",
                    meta={"group_id": group_id, "name": group_name,
                          "members": len(members)})
    return jsonify({"status": "ok", "message": entry})


@app.route("/api/owner/internal_messages/group_thread/<group_id>/mark_read",
            methods=["POST"])
def internal_messages_group_mark_read(group_id: str):
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    n = mod_imsg.mark_group_read(DATA_DIR, group_id, by_user=user["username"])
    return jsonify({"status": "ok", "marked": n})


@app.route("/api/owner/staff", methods=["GET"])
def owner_staff_list():
    auth.require_owner(ORBI_DIR)
    return jsonify({
        "active":   users_mod.list_users(DATA_DIR, include_archived=False),
        "archived": users_mod.list_archived(DATA_DIR),
    })


@app.route("/api/owner/staff", methods=["POST"])
def owner_staff_add():
    owner_session = auth.require_owner(ORBI_DIR)
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    email_addr = (data.get("email") or "").strip().lower()
    role = (data.get("role") or "staff").strip()
    if not username or len(username) < 2:
        return jsonify({"error": "username required (2+ chars)"}), 400
    if len(password) < 8:
        return jsonify({"error": "password must be 8+ characters"}), 400
    try:
        user = users_mod.add_user(DATA_DIR, username=username,
                                   password=password, email=email_addr,
                                   role=role)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="staff.added",
                    meta={"username": username, "role": role})
    return jsonify({"status": "ok", "user": user})


@app.route("/api/owner/staff/<username>/deactivate", methods=["POST"])
def owner_staff_deactivate(username: str):
    """Archive a staff user. Data moved to data/_archive/<username>/ and
    auto-purged after 90 days unless owner sets a hold."""
    owner_session = auth.require_owner(ORBI_DIR)
    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "")
    try:
        result = users_mod.deactivate_user(DATA_DIR, username,
                                            reason=reason)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="staff.deactivated",
                    meta={"username": username, "reason": reason[:120]})
    return jsonify({"status": "ok", "archive": result})


@app.route("/api/owner/staff/<username>/reactivate", methods=["POST"])
def owner_staff_reactivate(username: str):
    """Restore an archived user. Moves their _archived folder back to
    active. Fails if their old user folder already exists (someone
    re-added that username after archiving — owner needs to resolve
    the collision manually)."""
    owner_session = auth.require_owner(ORBI_DIR)
    try:
        rec = users_mod.reactivate_user(DATA_DIR, username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="staff.reactivated",
                    meta={"username": username})
    return jsonify({"status": "ok", "user": rec})


@app.route("/api/owner/staff/<username>/reset_link", methods=["POST"])
def owner_staff_reset_link(username: str):
    """Owner generates a one-time reset link for a staff member. Owner
    shares the link (text/email/in-person). Staff opens it, sets a new
    password. The link expires in 24h."""
    owner_session = auth.require_owner(ORBI_DIR)
    user = users_mod.get_user(DATA_DIR, username)
    if not user:
        return jsonify({"error": "user not found"}), 404
    token = _create_reset_token(username, user.get("email", ""))
    base, _ = _resolve_install_base()
    reset_url = f"{base}/owner/reset_password?token={token}"
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="staff.reset_link_created",
                    meta={"username": username})
    return jsonify({"status": "ok", "reset_url": reset_url,
                    "expires_in_hours": 24})


@app.route("/api/owner/staff/<username>/purge_hold", methods=["POST"])
def owner_staff_purge_hold(username: str):
    """Toggle the purge-hold flag on an archived user. When held=true,
    their data won't be auto-purged at 90 days."""
    owner_session = auth.require_owner(ORBI_DIR)
    data = request.get_json(silent=True) or {}
    hold = bool(data.get("hold"))
    ok = users_mod.set_purge_hold(DATA_DIR, username, hold)
    if not ok:
        return jsonify({"error": "user not found in archive"}), 404
    audit.log_event(DATA_DIR, actor=owner_session.get("email", "?"),
                    action="staff.purge_hold_set",
                    meta={"username": username, "hold": hold})
    return jsonify({"status": "ok", "hold": hold})


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

# ── Mobile install QR ────────────────────────────────────────────────────
# Owner / staff opens Settings → "Install on phone", scans the QR with
# their phone camera, lands on /owner/login on their phone, signs in with
# their username/password, taps "Add to Home Screen". Done.
#
# The QR encodes the publicly-reachable URL — preferably the cloudflared
# tunnel (config.server.tunnel_url) OR config.server.public_url if the
# owner manually set one. Falls back to the request's Host header (works
# for local LAN testing).

def _resolve_install_base() -> tuple[str, str]:
    """Pick the best URL to advertise for mobile install.
    Returns (base_url, source_tag)."""
    server = CONFIG.get("server") or {}
    public_url = (server.get("public_url") or "").rstrip("/")
    config_tunnel = (server.get("tunnel_url") or "").rstrip("/")
    live_tunnel = (_CURRENT_TUNNEL_URL[0] or "").rstrip("/") if _CURRENT_TUNNEL_URL else ""
    if public_url:
        return public_url, "configured"
    if live_tunnel:
        return live_tunnel, "tunnel"
    if config_tunnel:
        return config_tunnel, "tunnel"
    return request.host_url.rstrip("/"), "host_header"


@app.route("/api/owner/install_url", methods=["GET"])
def owner_install_url():
    """Returns the URL to put in the QR code + a human-readable label
    indicating how stable it is (named tunnel / random tunnel / local IP)."""
    auth.require_owner(ORBI_DIR)
    chosen, source = _resolve_install_base()

    full = f"{chosen}/owner/login"
    is_stable = source == "configured"
    return jsonify({
        "url":       full,
        "base":      chosen,
        "source":    source,
        "stable":    is_stable,
        "hint":      ("Stable URL — safe to print + share." if is_stable
                       else "TEMPORARY URL — changes when you restart Orby. "
                            "For a permanent URL, set up a named cloudflared "
                            "tunnel on your domain."),
    })


@app.route("/api/owner/install_qr.png", methods=["GET"])
def owner_install_qr():
    """Returns a QR code PNG that encodes the install URL. Scan with
    phone camera → opens /owner/login on the phone → log in → Add to
    Home Screen → done."""
    auth.require_owner(ORBI_DIR)
    try:
        import qrcode
        import qrcode.image.pil   # ensure PIL factory is registered
    except ImportError:
        return jsonify({"error": "qrcode_lib_missing",
                        "message": "Install python-qrcode + Pillow."}), 503

    base, _ = _resolve_install_base()
    url = f"{base}/owner/login"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=12,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#8b5cf6", back_color="white")
    import io as _io
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png",
                     headers={"Cache-Control": "no-store, max-age=0"})


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

    # Mark owner as active so the friend-checkin scheduler doesn't ping them
    # mid-conversation.
    try:
        friend_checkin.mark_owner_active(DATA_DIR)
    except Exception:
        log.exception("friend_checkin.mark_owner_active failed")

    # ── Fast-path personal-assistant intents (no LLM needed) ──────────────
    # Recovery commands — owner-only self-service for drift / data
    # corruption. Two-step confirmation: first call returns a confirm
    # token, second call (within 60s) executes.
    recovery = _try_recovery_command(user_msg, username, user_dir)
    if recovery is not None:
        return jsonify(recovery)

    # Gift logging — when owner mentions a gift they gave OR how a
    # previous gift was received, silently store it in their gifts
    # history. Doesn't intercept the chat — just enriches the memory
    # so future suggest_gift calls get smarter over time.
    try:
        _capture_gift_mention(user_msg, user_dir)
    except Exception:
        log.exception("gift mention capture failed")

    # Win logging — when the owner shares a win in chat ("closed the
    # Maxwell deal", "best month ever"), silently store it. Later when
    # they sound stressed, Orby pulls these back to remind them they've
    # been here before and came out the other side.
    try:
        from modules import wins as mod_wins
        win_text = mod_wins.detect_win_mention(user_msg)
        if win_text:
            mod_wins.record_win(user_dir, win_text, source="auto")
            log.info(f"win auto-logged: {win_text[:60]}")
    except Exception:
        log.exception("win mention capture failed")

    # Client personal-detail capture — when owner mentions a fact ABOUT a
    # contact ("Maxwell's daughter graduated", "Sarah loves jazz"), silently
    # append it to that contact's personal_notes. Months of these = the
    # owner remembers their clients' lives, which is irreplaceable.
    try:
        _capture_contact_facts(user_msg, user_dir)
    except Exception:
        log.exception("contact fact capture failed")

    # Update commands — "is there an update?" / "install update"
    upd = _try_update_command(user_msg, username)
    if upd is not None:
        return jsonify(upd)

    # "Gift for X" / "What should I get Y for her birthday" — uses the
    # owner's taste profile + past gifts to that person + contact notes.
    gift_resp = _try_gift_suggestion(user_msg, user_dir, user_rec)
    if gift_resp is not None:
        return jsonify(gift_resp)

    # "Message the sales team X" / "Tell everyone Y" — GROUP send. Must
    # run BEFORE the individual-recipient handler because that one would
    # parse "tell the sales team..." as recipient="the".
    grp = _try_send_group_message(user_msg, user_rec)
    if grp is not None:
        return jsonify(grp)

    # "Tell Cathi X" / "Send Joe a message: Y" — internal staff messaging
    im = _try_send_internal_message(user_msg, user_rec)
    if im is not None:
        return jsonify(im)

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
    tone = ((business.get("personality") or {}).get("tone") or "friend").lower()

    # Owner mode gets memory + notes + workspace + per-user PA as extra context
    extras = []

    # ── FRIEND-MODE personal context block (top priority) ──────────────
    # When tone == "friend", combine notes + long-term memory + owner
    # business info into ONE high-prominence block labeled as "what you
    # know about this person." Pushes the friend prompt's "weave naturally"
    # instruction onto real personal data instead of generic memory pulls.
    if tone == "friend":
        personal = _friend_personal_context(business, user_dir)
        if personal:
            extras.append(personal)
        # Past-wins recall — if the current message has stress cues, pull
        # a relevant past win for the LLM to weave in naturally. Real
        # friend behavior: remind you that you've been here before and
        # came out the other side.
        try:
            from modules import wins as mod_wins
            wins_block = mod_wins.context_block(user_dir, user_msg)
            if wins_block:
                extras.append(wins_block)
        except Exception:
            log.exception("wins context_block failed")

        # Client-context surfacing — if a known contact's name appears in
        # the current message, surface what Orby knows about them. The
        # owner mentioning "Maxwell" should remind Orby that Maxwell's
        # daughter graduated last month, etc.
        try:
            cc = _contact_context_for_message(user_msg, user_dir)
            if cc:
                extras.append(cc)
        except Exception:
            log.exception("contact context lookup failed")

    # "What can you do?" / "Walk me through X" — pull from the shipped
    # capabilities doc so the answer is grounded in real features.
    cap_ctx = _capabilities_context_block(user_msg)
    if cap_ctx:
        extras.append(cap_ctx)
    # Skip the generic notes/memory blocks when friend-mode already pulled them
    if tone != "friend":
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
# Must be the subject of a request — "list your capabilities", "what are
# your capabilities", "show me everything you can do". The bare word
# "capabilities" anywhere in a message used to fire — that was wrong
# (pasted text saying "advanced AI capabilities" shouldn't trigger an
# overview of Orby's own features).
_CAPABILITIES_RE = _re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:please\s+)?"
    r"(?:list|show\s+me|tell\s+me|give\s+me|share|run\s+through|walk\s+me\s+through|"
    r"what\s+(?:are|is)|what'?s)\s+"
    r"(?:me\s+|us\s+)?(?:all\s+(?:of\s+)?)?"
    r"(?:your|the|orbi'?s|orby'?s)\s+"
    r"(?:full\s+|complete\s+|entire\s+)?(?:list\s+of\s+)?"
    r"(?:capabilit(?:y|ies)|abilit(?:y|ies)|features)\b",
    _re.IGNORECASE,
)
_WHAT_CAN_YOU_DO_RE = _re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:please\s+)?"
    r"(?:tell\s+me\s+|show\s+me\s+)?"
    r"what\s+(?:(?:can|do)\s+you\s+do|you\s+(?:can|could|do)\s+do)\b",
    _re.IGNORECASE,
)


# ── Recovery commands ──────────────────────────────────────────────────────
# Two-step pattern. First call: "factory reset" / "rollback yesterday" /
# "restore from backup" → returns a confirmation token + warning. Second
# call: "confirm reset TOKEN" within 60s → actually executes. Prevents
# accidental wipes while still being usable from chat.

_RECOVERY_RE = _re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:please\s+)?"
    r"(?:"
    # Factory reset variants
    r"(?P<factory>(?:do\s+a\s+)?factory\s+reset|wipe\s+(?:my\s+)?(?:data|orbi|notes|memory)"
    r"|reset\s+(?:everything|my\s+notes|my\s+memory|to\s+factory)"
    r"|start\s+(?:over\s+)?from\s+scratch|nuke\s+(?:my\s+)?data)"
    # Rollback variants
    r"|(?P<rollback>rollback\s+(?:my\s+|the\s+)?(?:yesterday|today|last\s+\d+\s*(?:hour|day)s?|"
    r"changes|notes|memory)|undo\s+(?:yesterday|today|the\s+last\s+\d+\s*hours?))"
    # Restore from backup
    r"|(?P<restore>restore\s+(?:from\s+)?(?:my\s+)?backup|"
    r"restore\s+(?:from\s+)?(?:yesterday|last\s+night))"
    r")\b",
    _re.IGNORECASE,
)

_CONFIRM_RECOVERY_RE = _re.compile(
    r"^\s*(?:confirm|yes\s+confirm|do\s+it)\s+(?P<action>reset|rollback|restore)\s+"
    r"(?P<token>[A-Za-z0-9_-]{8,})\b",
    _re.IGNORECASE,
)

_PENDING_RECOVERY: dict[str, tuple[str, str, float]] = {}
_RECOVERY_TTL = 60


def _try_recovery_command(message: str, username: str, user_dir: Path) -> dict | None:
    """Detect and handle factory reset / rollback / restore commands.
    Two-step confirmation for safety. Returns chat reply dict or None."""
    import uuid as _uuid
    msg = (message or "").strip()
    if not msg:
        return None

    # Step 2: user confirming a pending recovery
    cm = _CONFIRM_RECOVERY_RE.match(msg)
    if cm:
        action_typed = cm.group("action").lower()
        token = cm.group("token")
        pending = _PENDING_RECOVERY.get(username)
        if not pending or (time.time() - pending[2]) > _RECOVERY_TTL:
            return {"reply": "No pending recovery to confirm (or it expired). "
                              "Start over by saying 'factory reset' or 'rollback'.",
                    "tier": "local", "latency_ms": 0,
                    "source": "recovery_expired"}
        pending_action, pending_token, _ = pending
        if pending_token != token or pending_action != action_typed:
            return {"reply": "Token or action doesn't match the pending recovery. "
                              f"Pending: {pending_action} {pending_token}. Try again.",
                    "tier": "local", "latency_ms": 0,
                    "source": "recovery_mismatch"}
        # Execute
        del _PENDING_RECOVERY[username]
        try:
            if pending_action == "reset":
                # Wipe per-user data dir (keep .session_secret + .audit_secret)
                preserve = {".session_secret", ".audit_secret",
                            ".backup_local_key", ".backup_state.json",
                            ".vapid_keys.json"}
                wiped = []
                for p in list(user_dir.iterdir()):
                    if p.name in preserve:
                        continue
                    if p.is_file():
                        p.unlink()
                        wiped.append(p.name)
                    elif p.is_dir():
                        import shutil as _shutil
                        _shutil.rmtree(p, ignore_errors=True)
                        wiped.append(p.name + "/")
                audit.log_event(DATA_DIR, actor=username,
                                action="recovery.factory_reset",
                                meta={"wiped_count": len(wiped)})
                return {"reply": f"Factory reset complete. Wiped {len(wiped)} "
                                  "items from your data folder. Your secrets "
                                  "(audit, session, backup key) were preserved. "
                                  "Orbi is fresh — re-onboard via the setup wizard.",
                        "tier": "local", "latency_ms": 0,
                        "source": "recovery_done"}
            elif pending_action == "restore":
                # Find the most recent local backup and restore it
                bk_dir = backup._local_backup_dir(CONFIG, DATA_DIR)
                if not bk_dir.exists():
                    return {"reply": "No backup directory found. Nothing to restore from.",
                            "tier": "local", "latency_ms": 0,
                            "source": "recovery_no_backup"}
                snapshots = sorted(
                    [p for p in bk_dir.iterdir() if p.suffix == ".enc"],
                    key=lambda p: p.stat().st_mtime, reverse=True)
                if not snapshots:
                    return {"reply": "No backup snapshots in the backup directory.",
                            "tier": "local", "latency_ms": 0,
                            "source": "recovery_no_backup"}
                latest = snapshots[0]
                key = backup._get_or_create_local_key(DATA_DIR)
                blob = latest.read_bytes()
                try:
                    raw = backup.decrypt(blob, key)
                except Exception as e:
                    return {"reply": f"Backup decrypt failed: {e}. "
                                      "The local backup key may have changed.",
                            "tier": "local", "latency_ms": 0,
                            "source": "recovery_decrypt_failed"}
                # Extract tar to user_dir (overwrite)
                import io as _io, tarfile as _tarfile
                with _tarfile.open(fileobj=_io.BytesIO(raw), mode="r:gz") as tar:
                    tar.extractall(path=DATA_DIR)
                audit.log_event(DATA_DIR, actor=username,
                                action="recovery.restore",
                                meta={"snapshot": latest.name})
                return {"reply": f"Restored from {latest.name}. "
                                  "Restart Orbi for all modules to re-read state.",
                        "tier": "local", "latency_ms": 0,
                        "source": "recovery_done"}
            elif pending_action == "rollback":
                # Restore is the same mechanism but framed differently for the user.
                # Recurse — but reuse the restore branch by re-triggering pending.
                # Simpler: tell the user to confirm 'restore' instead.
                return {"reply": "Rollback uses the same mechanism as restore. "
                                  "Say 'restore from backup' to restore the most "
                                  "recent snapshot.",
                        "tier": "local", "latency_ms": 0,
                        "source": "recovery_rollback_redirect"}
        except Exception as e:
            log.exception("recovery execution failed")
            return {"reply": f"Recovery failed: {e}",
                    "tier": "local", "latency_ms": 0,
                    "source": "recovery_error"}

    # Step 1: detect a recovery request, generate a confirm token
    m = _RECOVERY_RE.match(msg)
    if not m:
        return None
    action = ("reset" if m.group("factory")
              else "rollback" if m.group("rollback")
              else "restore" if m.group("restore")
              else None)
    if not action:
        return None
    token = _uuid.uuid4().hex[:10]
    _PENDING_RECOVERY[username] = (action, token, time.time())
    description = {
        "reset": ("Factory reset will WIPE all your notes, memory, contacts, "
                   "calendar cache, messages, learned answers, and workspace "
                   "index. Your secrets (audit log, session, backup key) stay. "
                   "Business profile (business_info.json) stays. This is "
                   "near-irreversible without a backup."),
        "rollback": ("Rollback restores from your most recent backup snapshot, "
                      "overwriting current data. Anything you added today will "
                      "be lost."),
        "restore": ("Restore from backup will overwrite current data with the "
                     "most recent encrypted snapshot in your backup folder."),
    }[action]
    return {"reply": (f"{description}\n\n"
                      f"To confirm, reply with:\n"
                      f"  confirm {action} {token}\n\n"
                      f"(60-second window. If you don't confirm, nothing happens.)"),
            "tier": "local", "latency_ms": 0,
            "source": "recovery_pending"}


# ── Update commands ────────────────────────────────────────────────────────
# "is there an update?" / "check for updates" → quick answer from cached state
# (or live check if last_check > 24h ago).
# "install update" / "apply update" / "update orbi" → download to staging,
# tell owner where to run the install (root permissions needed for binary swap).

_UPDATE_CHECK_RE = _re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:is\s+there\s+(?:an?\s+)?update|check\s+for\s+(?:an?\s+)?updates?|"
    r"any\s+updates?\s+available|are\s+you\s+up\s+to\s+date|"
    r"what\s+version\s+(?:are\s+you|is\s+this))",
    _re.IGNORECASE,
)
_UPDATE_INSTALL_RE = _re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:install\s+(?:the\s+)?update|apply\s+(?:the\s+)?update|"
    r"update\s+(?:orbi|yourself|now)|do\s+the\s+update|"
    r"upgrade\s+(?:orbi|yourself|now))",
    _re.IGNORECASE,
)


# Match a capitalized 1-3 word name. Case-sensitive so we don't slurp
# lowercase filler words. "my daughter Tamra" → Tamra, not "my".
_NAME_AFTER_FILLER = (
    # Optional "my/his/her/the [relation]" filler before the name:
    r"(?:(?:my|his|her|the)\s+(?:daughter|son|wife|husband|partner|"
    r"spouse|girlfriend|boyfriend|fiance|fiancee|mom|mother|dad|father|"
    r"sister|brother|aunt|uncle|cousin|niece|nephew|grandma|grandpa|"
    r"grandmother|grandfather|friend|coworker|boss|employee|client)\s+)?"
    # Then the actual name — capitalized first letter, 1-3 word run.
    # Case-sensitive (not part of IGNORECASE) via inline (?-i).
    r"(?-i:(?P<%s>[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}))"
)
_GIFT_SUGGEST_RE = _re.compile(
    # "gift for X" / "gift idea(s) for X" / "what should I get X"
    # / "what's a good gift for X" / "any ideas for X's birthday"
    r"(?:"
    r"(?:gift|present)\s+(?:idea(?:s)?\s+)?for\s+" + (_NAME_AFTER_FILLER % "name1") +
    r"|what(?:'s| is| should|'ll)?\s+(?:a\s+|some\s+)?"
    r"(?:good\s+|nice\s+|thoughtful\s+|cool\s+|great\s+)?(?:gift|present)\s+"
    r"(?:idea\s+)?for\s+" + (_NAME_AFTER_FILLER % "name2") +
    r"|(?:tell\s+me\s+)?what\s+(?:should\s+i\s+|to\s+)(?:get|buy|give)\s+"
        + (_NAME_AFTER_FILLER % "name3") +
    r"|tell\s+me\s+what\s+to\s+(?:get|buy|give)\s+"
        + (_NAME_AFTER_FILLER % "name3b") +
    r"|(?:what\s+to|tell\s+me\s+what\s+to)\s+(?:get|buy|give)\s+for\s+"
        + (_NAME_AFTER_FILLER % "name3c") +
    r"|(?:any\s+)?(?:gift\s+)?ideas\s+for\s+" + (_NAME_AFTER_FILLER % "name4") +
    r")"
    r"(?P<rest>.*)$",
    _re.IGNORECASE,
)
_GIFT_BUDGET_RE = _re.compile(
    r"\$?\s*(\d{2,4})\s*(?:-\s*\$?\s*(\d{2,4})\s*)?"
    r"|\baround\s+\$?\s*(\d{2,4})"
    r"|\bunder\s+\$?\s*(\d{2,4})"
    r"|\bup\s+to\s+\$?\s*(\d{2,4})"
    r"|\b(tight|small|big|large|no\s+limit)\s+budget",
    _re.IGNORECASE,
)
_OCCASION_WORDS = ("birthday", "anniversary", "graduation", "wedding",
                    "christmas", "hanukkah", "promotion", "retirement",
                    "housewarming", "baby\\s+shower", "engagement")
_GIFT_OCCASION_RE = _re.compile(
    r"\b(" + "|".join(_OCCASION_WORDS) + r")\b", _re.IGNORECASE)


def _try_gift_suggestion(message: str, user_dir, user_rec: dict) -> dict | None:
    """Detect 'gift for X' / 'what should I get X' / 'ideas for X's
    birthday' and return real suggestions using birthdays.suggest_gift
    (which pulls the owner's taste profile + past gifts to that person
    + the contact's notes). Falls through to None if no match — the LLM
    handles general gift-chat that isn't asking for a specific person.

    Fixes test #19 from the sweep: chat asked "gift for Tamra?" and
    got generic "if she's into tech... if she's into art" reply
    instead of using the gifts.py module that's already built.
    """
    msg = (message or "").strip()
    if not msg or len(msg) > 500:
        return None
    m = _GIFT_SUGGEST_RE.search(msg)
    if not m:
        return None
    name_raw = (m.group("name1") or m.group("name2") or m.group("name3")
                or m.group("name3b") or m.group("name3c")
                or m.group("name4") or "").strip()
    # Strip trailing words that aren't part of the name (occasion words,
    # for-clauses we already captured, "this year", etc.)
    name = _re.split(
        r"\b(?:for\s+(?:his|her|their)|this\s+|next\s+|in\s+|under\s+|"
        r"birthday|anniversary|graduation|wedding|christmas|"
        r"hanukkah|promotion|retirement|housewarming|engagement)\b",
        name_raw, maxsplit=1, flags=_re.IGNORECASE)[0].strip().rstrip("'s").strip()
    if not name or len(name) < 2:
        return None
    # Look up the contact (case-insensitive first-name match is enough)
    try:
        from modules import contacts as mod_contacts
        contact = mod_contacts.find_by_name(user_dir, name)
    except Exception as e:
        log.warning(f"contact lookup for gift failed: {e}")
        contact = None
    if not contact:
        # No contact record. Synthesize a minimal one so suggest_gift can
        # still try — at least it gets the name. Note this in the reply
        # so the owner knows it's a generic suggestion.
        contact = {"name": name, "notes": "", "relationship": "",
                    "tags": []}
        unknown_contact = True
    else:
        unknown_contact = False
    # Parse occasion + budget from the rest of the message
    rest = (m.group("rest") or "") + " " + msg  # search whole msg for occ/budget
    occ_m = _GIFT_OCCASION_RE.search(rest)
    occasion = occ_m.group(1).lower() if occ_m else "birthday"
    budget_hint = None
    bm = _GIFT_BUDGET_RE.search(rest)
    if bm:
        # Take the first non-None numeric or qualitative group
        budget_hint = next((g for g in bm.groups() if g), None)
        if budget_hint and budget_hint.isdigit():
            budget_hint = f"around ${budget_hint}"
    try:
        result = birthdays.suggest_gift(
            CONFIG, contact, kind=occasion,
            budget_hint=budget_hint, occasion=occasion,
            user_dir=user_dir)
    except Exception as e:
        log.exception("suggest_gift failed")
        return {"reply": f"Couldn't generate gift ideas right now: {e}",
                "tier": "local", "latency_ms": 0,
                "source": "gift_suggest_error"}
    suggestions = (result or {}).get("suggestions") or []
    if not suggestions:
        return None  # Let the LLM take it
    # Format the reply
    lines = []
    if unknown_contact:
        lines.append(f"I don't have a contact record for {name} — these are "
                      f"more generic. Add them in People for taste-aware suggestions next time.")
    lines.append(f"Gift ideas for {name}'s {occasion}:")
    lines.append("")
    for i, s in enumerate(suggestions[:3], 1):
        idea = s.get("idea", "").strip()
        cost = s.get("rough_cost", "").strip()
        why  = s.get("why", "").strip()
        cost_bit = f" — *{cost}*" if cost else ""
        why_bit = f"\n   _{why}_" if why else ""
        lines.append(f"{i}. **{idea}**{cost_bit}{why_bit}")
    if not budget_hint:
        lines.append("")
        lines.append("(Tell me a budget and I'll narrow it down.)")
    return {"reply": "\n".join(lines), "tier": "local", "latency_ms": 0,
            "source": "gift_suggest"}


def _try_send_group_message(message: str, user_rec: dict) -> dict | None:
    """Detect group-send phrasings ('message the sales team', 'tell
    everyone', 'announce to the kitchen staff') and route to the
    multi-recipient group thread. Returns chat reply dict or None.

    Group resolution:
      - "everyone" / "whole team" / "all staff" → __all__ (Whole Team)
      - "the X team/staff/group/crew" → fuzzy-match against stored groups
      - No match → falls through to individual handler (which will say
        "I don't see a staff member named X" with the active list)
    """
    intent = mod_imsg.detect_group_send_intent(message)
    if not intent:
        return None
    sender_username = user_rec["username"]
    group_query = intent["group_query"]
    body = intent["body"]
    group = mod_imsg.resolve_group(DATA_DIR, group_query)
    if not group:
        # No saved group with that name. Tell Frank and list what's available.
        existing = [g.get("name") for g in mod_imsg.list_groups(DATA_DIR)]
        existing_str = ", ".join(existing) if existing else "(no saved groups yet)"
        return {"reply": (f"I don't see a group called \"{group_query}\". "
                          f"Your saved groups: {existing_str}. "
                          "Plus the built-in Whole Team. Create a new group in "
                          "Team Chat → + New, then try again."),
                "tier": "local", "latency_ms": 0,
                "source": "internal_group_no_match"}
    # Resolve members. For Whole Team it's all active staff; for stored
    # groups it's the group's member list.
    if group["id"] == mod_imsg.ALL_GROUP_ID:
        active = users_mod.list_users(DATA_DIR)
        members = [u["username"] for u in active]
        group_name = mod_imsg.ALL_GROUP_NAME
    else:
        members = list(group.get("members", []))
        group_name = group.get("name") or "Group"
    # Owner sending into a group they're not in? Add them so they appear
    # in the snapshot (mirrors the REST endpoint behavior).
    if sender_username not in members:
        members = members + [sender_username]
    try:
        entry = mod_imsg.send_to_group(
            DATA_DIR,
            group_id=group["id"], group_name=group_name,
            member_usernames=members,
            from_user=sender_username,
            from_name=user_rec.get("display_name") or sender_username,
            body=body, via="orby")
    except ValueError as e:
        return {"reply": f"Couldn't send: {e}",
                "tier": "local", "latency_ms": 0,
                "source": "internal_group_error"}
    # Notify each recipient except sender
    for m in members:
        if m == sender_username:
            continue
        try:
            notify.send(CONFIG, DATA_DIR, event="new_message",
                         title=f"{group_name}: {entry['from_name']}",
                         body=body[:140], url="/owner#messages")
        except Exception:
            pass
    audit.log_event(DATA_DIR, actor=sender_username,
                    action="group_msg.sent",
                    meta={"group_id": group["id"], "name": group_name,
                          "members": len(members), "via": "orby"})
    n_others = len([m for m in members if m != sender_username])
    return {"reply": (f"Sent — \"{body[:80]}{'...' if len(body) > 80 else ''}\" "
                      f"to {group_name} ({n_others} {'person' if n_others == 1 else 'people'})."),
            "tier": "local", "latency_ms": 0,
            "source": "internal_group_sent"}


def _try_send_internal_message(message: str, user_rec: dict) -> dict | None:
    """Detect 'tell X', 'send X a message', 'let X know', 'message X about Y'
    and route to internal staff messaging. Returns chat reply dict or None
    to fall through.

    Recipient resolution: tries exact username, then display_name
    case-insensitive, then unambiguous first-name match. If multiple
    matches OR no matches, asks the owner to specify.
    """
    intent = mod_imsg.detect_send_intent(message)
    if not intent:
        return None
    recipient_name = intent["recipient_name"]
    body = intent["body"]
    sender_username = user_rec["username"]

    # Resolve recipient to a known active user
    all_users = users_mod.list_users(DATA_DIR, include_archived=False)
    name_lower = recipient_name.lower()
    candidates = []
    for u in all_users:
        uname = (u.get("username") or "").lower()
        dname = (u.get("display_name") or "").lower()
        if uname == name_lower:
            candidates = [u]
            break
        if dname == name_lower or dname.split()[0:1] == [name_lower]:
            candidates.append(u)
        elif uname.startswith(name_lower) and len(name_lower) >= 3:
            candidates.append(u)
    candidates = [c for c in candidates
                  if (c.get("username") or "").lower() != sender_username.lower()]

    if not candidates:
        active_names = [u.get("display_name") or u.get("username")
                        for u in all_users
                        if (u.get("username") or "").lower() != sender_username.lower()]
        return {"reply": (f"I don't see a staff member named \"{recipient_name}\". "
                          f"Active staff: {', '.join(active_names) or '(none)'}. "
                          "Add them in Settings → Staff first, then try again."),
                "tier": "local", "latency_ms": 0,
                "source": "internal_msg_no_recipient"}

    if len(candidates) > 1:
        names = ", ".join(c.get("display_name") or c.get("username") for c in candidates)
        return {"reply": (f"More than one match for \"{recipient_name}\": {names}. "
                          "Try the full name or username."),
                "tier": "local", "latency_ms": 0,
                "source": "internal_msg_ambiguous"}

    recipient = candidates[0]
    to_user = recipient["username"]
    try:
        entry = mod_imsg.send(
            DATA_DIR,
            from_user=sender_username,
            from_name=user_rec.get("display_name") or sender_username,
            to_user=to_user,
            to_name=recipient.get("display_name") or to_user,
            body=body,
            via="orby")
    except ValueError as e:
        return {"reply": f"Couldn't send: {e}",
                "tier": "local", "latency_ms": 0,
                "source": "internal_msg_error"}
    try:
        notify.send(CONFIG, DATA_DIR, event="new_message",
                     title=f"Message from {entry['from_name']}",
                     body=body[:140], url="/owner#messages")
    except Exception:
        log.exception("internal msg notify failed")
    audit.log_event(DATA_DIR, actor=sender_username,
                    action="internal_msg.sent",
                    meta={"to": to_user, "via": "orby"})
    return {"reply": (f"Sent — \"{body[:80]}{'...' if len(body) > 80 else ''}\" "
                      f"to {recipient.get('display_name') or to_user}."),
            "tier": "local", "latency_ms": 0,
            "source": "internal_msg_sent"}


def _try_update_command(message: str, username: str) -> dict | None:
    msg = (message or "").strip()
    if not msg:
        return None

    # "Is there an update?" — check + report
    if _UPDATE_CHECK_RE.match(msg):
        current = (CONFIG or {}).get("version", "0.0.0")
        # Use cached state if recent (last check < 24h), else live check
        state = updater._load_state(DATA_DIR)
        last_check = state.get("last_check", 0)
        info = state.get("available")
        if time.time() - last_check > 3600 or not last_check:
            info = updater.check_for_update(CONFIG, DATA_DIR) or info
        if not info:
            return {"reply": (f"You're on version {current} and that's the "
                              "latest. Last checked just now."),
                    "tier": "local", "latency_ms": 0,
                    "source": "update_current"}
        size_mb = round((info.get("asset_size", 0) or 0) / 1e6, 1)
        notes = (info.get("body") or "")[:300].strip()
        notes_line = f"\n\nRelease notes:\n{notes}" if notes else ""
        return {"reply": (f"Update available: {current} → {info['tag']} "
                          f"({size_mb} MB). Say \"install update\" to "
                          f"download + apply it.{notes_line}"),
                "tier": "local", "latency_ms": 0,
                "source": "update_available"}

    # "Install update"
    if _UPDATE_INSTALL_RE.match(msg):
        info = updater.get_pending_update(DATA_DIR)
        if not info:
            return {"reply": ("No update is pending. Say \"check for updates\" "
                              "first to see if a new version exists."),
                    "tier": "local", "latency_ms": 0,
                    "source": "update_none_pending"}

        install_root = Path(__file__).resolve().parent
        if updater.is_git_checkout(install_root):
            result = updater.git_pull_update(install_root)
            audit.log_event(DATA_DIR, actor=username, action="update.git_pull",
                            meta={"tag": info.get("tag"), "ok": result.get("ok")})
            if result.get("ok"):
                return {"reply": (f"Pulled latest code (git). Restart Orbi to "
                                   f"activate the changes.\n\n{result.get('stdout','')}"),
                        "tier": "local", "latency_ms": 0,
                        "source": "update_git_done"}
            return {"reply": (f"git pull failed: {result.get('stderr') or result.get('error')}"),
                    "tier": "local", "latency_ms": 0,
                    "source": "update_git_failed"}

        # Binary install — download to staging
        staging = DATA_DIR / "_updates" / info["tag"]
        dest = updater.download_update(info, staging)
        if not dest:
            return {"reply": ("Couldn't download the update. Check internet + "
                              "try again later."),
                    "tier": "local", "latency_ms": 0,
                    "source": "update_download_failed"}
        audit.log_event(DATA_DIR, actor=username, action="update.downloaded",
                        meta={"tag": info.get("tag"), "path": str(dest)})
        return {"reply": (f"Downloaded {info['tag']} to {dest}. "
                          "To install it, run (as administrator/root):\n\n"
                          f"  {dest}\n\n"
                          "Orbi will restart automatically after the swap. "
                          "Your data is safe — only the program files change."),
                "tier": "local", "latency_ms": 0,
                "source": "update_downloaded"}

    return None


def _contact_context_for_message(message: str, user_dir: Path) -> str:
    """If a known contact's name appears in the message, return a context
    block with what we know about them. Multiple contacts → multiple
    sub-blocks. Empty string when no match."""
    from modules import contacts as mod_contacts

    msg = (message or "").strip()
    if not msg:
        return ""
    contacts = mod_contacts.list_all(user_dir)
    if not contacts:
        return ""

    # Find contacts whose name appears in the message (full name or
    # unambiguous first name)
    msg_lower = msg.lower()
    first_count: dict[str, list[str]] = {}
    for c in contacts:
        first = (c.get("name") or "").split()[:1]
        if first:
            first_count.setdefault(first[0].lower(), []).append(c["id"])

    mentioned: list[dict] = []
    seen_ids = set()
    for c in contacts:
        nm = (c.get("name") or "").strip()
        if not nm:
            continue
        nm_lower = nm.lower()
        first_lower = nm_lower.split()[0]
        # Full name match always counts
        if nm_lower in msg_lower:
            if c["id"] not in seen_ids:
                mentioned.append(c)
                seen_ids.add(c["id"])
            continue
        # First name match only when unambiguous AND surrounded by word
        # boundaries (don't match "Joe" inside "Joey")
        if len(first_count.get(first_lower, [])) == 1:
            if _re.search(r"\b" + _re.escape(first_lower) + r"\b",
                          msg_lower):
                if c["id"] not in seen_ids:
                    mentioned.append(c)
                    seen_ids.add(c["id"])

    if not mentioned:
        return ""

    lines = ["WHAT YOU KNOW ABOUT THE PEOPLE THEY MENTIONED — weave in "
             "naturally, especially anything personal:"]
    for c in mentioned[:3]:
        nm = c.get("name", "")
        company = c.get("company", "")
        phone = c.get("phone", "")
        email = c.get("email", "")
        last = c.get("last_contact", "")
        lines.append(f"\n- {nm}" + (f" ({company})" if company else ""))
        bits = []
        if phone:
            bits.append(f"phone {phone}")
        if email:
            bits.append(f"email {email}")
        if last:
            bits.append(f"last contact {last[:10]}")
        if bits:
            lines.append(f"  · {' · '.join(bits)}")
        general_notes = (c.get("notes") or "").strip()
        if general_notes:
            lines.append(f"  · {general_notes[:200]}")
        # Personal_notes — these are the gold (auto-captured personal facts)
        personal = c.get("personal_notes") or []
        if personal:
            lines.append("  · Personal facts you've picked up:")
            for n in personal[:6]:
                txt = (n.get("note") or "").strip()
                if txt:
                    lines.append(f"      · {txt}")
    return "\n".join(lines)


def _capture_contact_facts(message: str, user_dir: Path) -> None:
    """Scan the message for facts about known contacts. When a contact's
    name appears with a personal-sounding clause, capture it as a
    personal_note on that contact.

    Patterns it catches:
      "Maxwell's daughter Maria just graduated"
      "Joe's wife is sick"
      "Sarah loves jazz, especially Miles Davis"
      "Tom's anniversary is May 15"
      "Mike's son broke his arm last week"

    Skipped — these are NOT personal facts:
      "Send Joe the invoice"          (instruction)
      "Did Sarah call back yet"       (question)
      "Tom's the lead on Maxwell"     (work-only descriptor)
    """
    from modules import contacts as mod_contacts

    msg = (message or "").strip()
    if not msg or len(msg) > 600:
        return
    # Don't capture from imperatives / questions to Orby herself
    if msg.endswith("?") or _re.match(
            r"^\s*(?:send|email|call|text|remind|tell|ask|check|find|search|"
            r"book|schedule|cancel|delete|update|set|add|remove)\b",
            msg, _re.IGNORECASE):
        return

    contacts = mod_contacts.list_all(user_dir)
    if not contacts:
        return

    # Build a lookup of names → contact id. Include first names only when
    # unambiguous (so "Joe" matches Joe Smith if there's no other Joe).
    by_full = {}
    by_first = {}
    first_count: dict[str, int] = {}
    for c in contacts:
        nm = (c.get("name") or "").strip()
        if not nm:
            continue
        by_full[nm.lower()] = c["id"]
        first = nm.split()[0].lower()
        first_count[first] = first_count.get(first, 0) + 1
        if first not in by_first:
            by_first[first] = c["id"]
    by_first = {k: v for k, v in by_first.items() if first_count[k] == 1}

    # Personal-fact pattern: [Name][optional 's] + [verb/copula/possessive] +
    # [personal noun or sentence]
    PERSONAL_VERBS = (
        r"is|was|are|were|has|have|had|loves|loved|hates|hated|prefers|"
        r"got\s+(?:married|engaged|divorced)|graduated|started|stopped|quit|"
        r"moved|bought|sold|inherited|adopted|broke|injured|sick|recovering|"
        r"birthday|anniversary|wedding|baby|pregnant|"
        r"likes|dislikes|favorite|hobby|collects|plays|watches|reads"
    )
    POSS_NOUNS = (
        r"daughter|son|kid|child|wife|husband|spouse|partner|mom|dad|"
        r"mother|father|brother|sister|sibling|family|parent|in[- ]?law|"
        r"grandkid|grandchild|grandson|granddaughter|"
        r"birthday|anniversary|hobby|favorite|pet|dog|cat|car|house|home|"
        r"surgery|illness|business|company|job|new\s+(?:job|role|gig)"
    )

    pat_possessive = _re.compile(
        r"\b(?P<name>[A-Z][a-zA-Z]{1,30})'s\s+"
        r"(?P<rest>(?:" + POSS_NOUNS + r")\b[^.!?\n]{0,140})",
    )
    pat_verb = _re.compile(
        r"\b(?P<name>[A-Z][a-zA-Z]{1,30})\s+"
        r"(?P<rest>(?:" + PERSONAL_VERBS + r")\b[^.!?\n]{0,140})",
    )

    captured = 0
    for pat in (pat_possessive, pat_verb):
        for m in pat.finditer(msg):
            name = m.group("name")
            rest = m.group("rest").strip().rstrip(".,;:")
            # Resolve name → contact id
            cid = (by_full.get(name.lower())
                    or by_first.get(name.lower()))
            if not cid:
                continue
            # Skip pure work shorthand ("Joe's the lead", "Joe's behind")
            if rest.lower().startswith(("the ", "behind", "ahead", "on it",
                                          "doing", "working", "handling")):
                continue
            note = f"{name}'s {rest}" if pat is pat_possessive else f"{name} {rest}"
            try:
                result = mod_contacts.append_personal_note(
                    user_dir, cid, note, source="chat")
                if result:
                    captured += 1
                    log.info(f"contact fact auto-captured for {name}: {note[:80]}")
            except Exception:
                log.exception("append_personal_note failed")
            if captured >= 3:    # cap per message
                return


def _capture_gift_mention(message: str, user_dir: Path) -> None:
    """Background gift-history enrichment. When the owner says something
    like 'I got Kathy a necklace for her birthday', silently log it so
    suggest_gift learns the owner's taste over time. Same for outcome
    mentions ('Kathy loved the necklace')."""
    from modules import gifts as mod_gifts

    # Detect a NEW gift mention
    g = mod_gifts.detect_logged_gift(message)
    if g:
        try:
            mod_gifts.record_gift(
                user_dir,
                recipient=g["recipient"],
                occasion=g["occasion"],
                item=g["item"],
                rough_cost=g.get("rough_cost", ""),
            )
            log.info(f"gift auto-logged: {g['item']} → {g['recipient']} "
                     f"({g['occasion']})")
        except Exception:
            log.exception("record_gift failed")
        return

    # Detect an OUTCOME mention — match to most recent gift to that person
    out = mod_gifts.detect_outcome_mention(message)
    if out:
        try:
            past = mod_gifts.list_for_recipient(user_dir, out["recipient"], limit=1)
            if past:
                # Update the most recent gift to this recipient with the outcome
                mod_gifts.record_outcome(user_dir, past[0]["id"],
                                          out["outcome"], note=message[:120])
                log.info(f"gift outcome auto-logged: {out['recipient']} "
                         f"{out['outcome']} → gift {past[0]['id']}")
        except Exception:
            log.exception("record_outcome failed")


def _friend_personal_context(business: dict, user_dir: Path) -> str:
    """Single combined 'what you know about this person' block for friend
    mode. Pulls from business profile (name, role), long-term memory
    (lasting facts), notes (owner-authored), and any 'personal' field
    in the business profile. Framed so the LLM uses it conversationally
    instead of reciting it like a database lookup."""
    personality = business.get("personality") or {}
    owner_full = personality.get("owner_name") or business.get("owner_name") or ""
    owner_first = owner_full.split()[0] if owner_full else "the owner"
    owner_role = personality.get("owner_role") or "owner"
    biz_name = business.get("name") or ""

    lines = [f"WHO YOU'RE TALKING TO — use this naturally, don't recite "
             "it like trivia or a database lookup:"]
    if owner_full:
        if biz_name:
            lines.append(f"- Name: {owner_full} ({owner_role} of {biz_name})")
        else:
            lines.append(f"- Name: {owner_full} ({owner_role})")
    # Owner's freeform personal context (a dedicated field they can fill
    # in via Settings or by saying "remember that I [...]")
    personal_text = (business.get("owner_personal")
                      or personality.get("personal_context") or "").strip()
    if personal_text:
        lines.append(f"- Personal context: {personal_text}")

    # Long-term memory entries (lasting facts about the owner)
    try:
        mem_data = mod_memory._load_raw(DATA_DIR)
        lt = [e.get("content", "") for e in (mem_data.get("long_term") or [])][-12:]
        if lt:
            lines.append("- Things you remember about them (long-term):")
            for item in lt:
                if item:
                    lines.append(f"  · {item}")
    except Exception:
        pass

    # Recent notes (owner-authored, freeform)
    try:
        notes = mod_notes.list_all(DATA_DIR)
        notes = sorted(notes, key=lambda n: n.get("ts", 0), reverse=True)[:10]
        if notes:
            lines.append("- Notes they've added for you to keep in mind:")
            for n in notes:
                c = (n.get("content") or "").strip()
                if c:
                    lines.append(f"  · {c}")
    except Exception:
        pass

    # If we only got the header line, return empty (nothing useful to share)
    if len(lines) <= 1:
        return ""
    lines.append("\nWeave this in naturally when context invites it — "
                 "asking about a partner by name, referencing a struggle "
                 "they mentioned last week, celebrating a win you remember. "
                 "Don't dump it back at them.")
    return "\n".join(lines)


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
    # "I need you to X" / "I want you to X" / "I'd like you to X" —
    # treats these the same as polite-prefix + verb. Required so
    # "I need you to create me a Facebook ad" actually fires the ad
    # trigger (was leaking to the LLM and producing 'free trial' text).
    r"|i\s+(?:need|want)\s+you\s+to\s+"
    r"|i(?:'d| would)\s+like\s+you\s+to\s+"
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

# Verbs tolerate the natural conjugations users actually type: "makes",
# "drew", "drawing", "creating" — without this, "makes a picture" silently
# fails the trigger and the LLM has to fall back to hallucinating.
_CHART_VERBS = r"(?:make(?:s|d|ing)?|build(?:s|ing)?|create(?:s|d|ing)?|generate(?:s|d|ing)?|draw(?:s|ing|n)?|drew|render(?:s|ed|ing)?|plot(?:s|ted|ting)?|chart(?:s|ed|ing)?|graph(?:s|ed|ing)?)"
_CHART_NOUNS = r"(?:bar|line|pie|scatter)?\s*(?:chart|graph|plot|visualization|visualisation)"

_DECK_VERBS = r"(?:make(?:s|d|ing)?|build(?:s|ing)?|create(?:s|d|ing)?|generate(?:s|d|ing)?|put(?:s|ting)?\s+together|throw(?:s|n|ing)?\s+together|whip(?:s|ping|ped)?\s+up)"
_DECK_NOUNS = r"(?:\d+[-\s]?slide\s+)?(?:pitch\s+)?(?:slide\s+)?(?:deck|presentation|powerpoint|pptx|slideshow)"

# Image verbs cover everything from "draw" to "whip up" to "illustrate",
# plus their natural conjugations.
_IMG_VERBS = (
    r"(?:make(?:s|d|ing)?|build(?:s|ing)?|create(?:s|d|ing)?|generate(?:s|d|ing)?|"
    r"design(?:s|ed|ing)?|draw(?:s|ing|n)?|drew|paint(?:s|ed|ing)?|"
    r"render(?:s|ed|ing)?|sketch(?:es|ed|ing)?|"
    r"mock(?:s|ed|ing)?\s*up|come(?:s)?\s+up\s+with|came\s+up\s+with|"
    r"whip(?:s|ping|ped)?\s+up|illustrate(?:s|d|ing)?|"
    r"visualize(?:s|d|ing)?|visualise(?:s|d|ing)?)"
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
# "save this ad as a style example" / "learn from this ad" / "remember this
# ad style" / "I like this ad" + a paste of the actual ad text. Stores in
# ad_gen's exemplar corpus so future build_ad calls use it as a few-shot.
_AD_LEARN_TRIGGER_RE = _re.compile(
    r"^\s*(?:" + _POLITE_PREFIX +
    r"(?:save|remember|learn\s+from|use|study|file\s+away|keep)"
    r"\s+(?:this|the\s+following|the\s+next)?\s*"
    r"(?:ad|advert|advertisement|copy|example|style|sample|one)"
    r"|" +
    r"i\s+(?:like|love|want\s+more\s+like)\s+(?:this|the\s+following)\s+"
    r"(?:ad|advert|advertisement|style|copy|sample|one)"
    r"|" +
    r"(?:here'?s|this\s+is)\s+(?:a\s+|an\s+|the\s+)?"
    r"(?:ad|advert|advertisement|example|style|sample)\s+"
    r"(?:i\s+(?:like|want\s+more\s+like)|to\s+(?:save|remember|learn\s+from))"
    r")",
    _re.IGNORECASE,
)

# Detect "create/build/design me an ad" — the user wants a FINISHED
# composited ad (background image + headline + body + CTA button) not
# just a picture or just copy. This routes to ad_gen.build_ad which
# orchestrates the LLM (designs the ad) + image_gen (background) + PIL
# (composite).
_AD_TRIGGER_RE = _re.compile(
    r"^\s*(?:" + _POLITE_PREFIX +
    r"(?:make|build|create|generate|design|put\s+together|whip\s+up|do)"
    r"\s+(?:me\s+|us\s+)?(?:a\s+|an\s+|the\s+)?"
    r"(?:complete\s+|full\s+|finished\s+|actual\s+|real\s+|whole\s+)?"
    r"(?:facebook|fb|instagram|ig|twitter|x|linkedin|tiktok|youtube|yt|"
    r"pinterest|social\s+media)?\s*"
    r"(?:story|stories|post|feed|cover|reels?|carousel|video|photo)?\s*"
    r"(?:ad|advert|advertisement|ad\s+creative|finished\s+ad|"
    r"complete\s+ad|whole\s+ad|full\s+ad|actual\s+ad)\b"
    r"|" + _ACTION_PREFIX +
    r"(?:a\s+|an\s+|the\s+)?(?:complete\s+|full\s+|finished\s+|actual\s+|real\s+)?"
    r"(?:facebook|fb|instagram|ig|twitter|x|linkedin|tiktok|youtube|yt|"
    r"pinterest|social\s+media)?\s*"
    r"(?:story|stories|post|feed|cover|reels?|carousel|video|photo)?\s*"
    r"(?:ad|advert|advertisement|ad\s+creative|finished\s+ad|"
    r"complete\s+ad|whole\s+ad|full\s+ad|actual\s+ad)\b"
    r")",
    _re.IGNORECASE,
)

# "the images you mentioned / those pictures / the ones you described" —
# referring back to images named in a prior LLM response (e.g. inside a
# marketing campaign brief). The fast-path can't draw a meaningful image
# because the subject is in the LLM's prior turn, not in the user's
# message. We catch this and prompt the user to pick one.
_IMAGE_REFERENCED_RE = _re.compile(
    r"\b(?:"
    # PATTERN A: "those/the/these/that [image words] you mentioned/described"
    r"(?:those|these|the|that)\s+"
    r"(?:image|images|picture|pictures|pic|pics|graphic|graphics|"
    r"photo|photos|drawing|drawings|visual|visuals|one|ones)\s+"
    r"(?:that\s+)?"
    r"(?:you(?:'re|\s+are|\s+were)?|i)\s+"
    r"(?:just\s+)?"
    r"(?:talking\s+about|talked\s+about|mentioned|described|"
    r"named|listed|said|referred\s+to|brought\s+up|came\s+up\s+with)"
    # PATTERN B: "all (of) the (campaign/marketing/ad/social) images" — refers
    # to a set Orby described in a prior turn, usually inside a marketing
    # campaign brief. "campaign images" sent to FLUX triggers the military
    # association → produced a battle scene of 8 soldiers. Never do that.
    r"|all\s+(?:of\s+)?(?:the\s+|those\s+|these\s+|that\s+)?"
    r"(?:campaign\s+|marketing\s+|ad\s+|ads\s+|post\s+|posts\s+|"
    r"social\s+(?:media\s+)?|facebook\s+|instagram\s+|tiktok\s+|"
    r"linkedin\s+|brief\s+|briefs\s+|suggested\s+|proposed\s+|"
    r"recommended\s+)*"
    r"(?:image|images|picture|pictures|pic|pics|graphic|graphics|"
    r"photo|photos|visual|visuals|post|posts|ad|ads|creative|creatives)"
    # PATTERN C: "the (campaign/marketing) [image-words]" without "all"
    r"|(?:the|these|those|that)\s+"
    r"(?:campaign|marketing|ad|ads|social\s+(?:media)?|brief|briefs|"
    r"suggested|proposed|recommended)\s+"
    r"(?:image|images|picture|pictures|graphic|graphics|"
    r"photo|photos|visual|visuals|post|posts|creative|creatives)"
    r")\b",
    _re.IGNORECASE,
)
# Loose catch-all: a bare drawing verb without an explicit noun.
# "can you draw what an Orby user would look like" doesn't say "picture" —
# still fire image gen because the verb is clearly visual.
_IMAGE_LOOSE_RE = _re.compile(
    r"^\s*" + _POLITE_PREFIX +
    r"(?:draw(?:s|ing|n)?|drew|paint(?:s|ed|ing)?|sketch(?:es|ed|ing)?|"
    r"illustrate(?:s|d|ing)?|visualize(?:s|d|ing)?|visualise(?:s|d|ing)?)\s+",
    _re.IGNORECASE,
)
# Subject-led catch-all: "show me a robot", "i want a sunset", "give me a
# logo for a deli". The user named a subject and used an action prefix —
# in chat context that almost always means "draw it". The LLM has no
# tool-calling so falling through there just produces a text description.
# We restrict this to messages that LOOK visual: must follow an action
# prefix AND name "a/an/the/some [subject]", AND the message is short
# enough that it's clearly a request, not a paragraph of context.
_IMAGE_SUBJECT_LED_RE = _re.compile(
    r"^\s*" + _ACTION_PREFIX +
    r"(?:a\s+|an\s+|the\s+|some\s+)"
    r"[a-z][a-z\-' ]{1,60}$",   # any concrete-looking subject, short request
    _re.IGNORECASE,
)
# Self-portrait detector: the user wants Orby to draw HERSELF, not a
# generic prompt. "what you look like / what you imagine / draw yourself /
# your own appearance / what orbi looks like". When this matches we
# substitute a brand-aligned orb portrait prompt so FLUX doesn't default
# to a stock photo of a person.
_IMAGE_SELF_RE = _re.compile(
    r"\b(?:"
    # "what/how (does) you/orbi (think you should | imagine you might | etc.)
    #  look/appear/imagine like" — flexible clause between subject and verb
    r"(?:what|how)\s+(?:does?\s+|do\s+)?(?:you|orbi|orby)"
    r"(?:\s+(?:think|believe|feel|imagine|reckon|figure|should|would|could|"
    r"might|ought\s+to|hope\s+to|want\s+to|like\s+to)){0,3}"
    r"(?:\s+(?:you|orbi|orby|i|it|she|he|they))?\s+"
    r"(?:would\s+|might\s+|could\s+|should\s+|ought\s+to\s+|hope\s+to\s+|"
    r"want\s+to\s+|like\s+to\s+|be\s+)*"
    r"(?:imagine|look|appear|be)(?:s|ed|ing)?(?:\s+like)?"
    # OR  "yourself" with a drawing verb
    r"|(?:draw|paint|sketch|render|design|generate|make)\s+yourself"
    # OR plain "yourself" anywhere
    r"|yourself"
    # OR "your (own) appearance/avatar/face/etc"
    r"|your\s+(?:own\s+)?(?:appearance|self\s?-?portrait|avatar|image|face|"
    r"likeness|look|body|form)"
    # OR "self-portrait of you/orbi"
    r"|self[-\s]?portrait\s+of\s+(?:you|orbi|orby)"
    r")\b",
    _re.IGNORECASE,
)

# Refinement detector: the user is following up on a JUST-GENERATED image.
# "more humanoid" / "make it bigger" / "different style" / "with blue eyes" /
# "I'd like to see X" / "try again with Y" / "less Z". When this matches AND
# we have a recent prior image for this user, we re-fire image_gen with a
# rebuilt prompt instead of falling through to the LLM (which would just
# describe what it would draw, not draw it).
_IMAGE_REFINE_RE = _re.compile(
    r"\b(?:"
    r"more|less|bigger|smaller|brighter|darker|softer|sharper|"
    r"with(?:out)?|add|remove|drop|change(?:\s+(?:it|to))?|make\s+it|"
    r"i(?:'d| would)\s+like\s+(?:to\s+see\s+)?|i\s+(?:want|prefer)\s+|"
    r"how\s+about|what\s+about|"
    r"try\s+(?:again|once\s+more|something|with|it)|"
    r"different|another|instead|but|except|"
    r"redo|retry|do\s+(?:it|that)\s+again"
    r")\b",
    _re.IGNORECASE,
)

# In-memory cache of the last image prompt per username. 10-minute TTL.
# Tuple shape: (base_prompt: str, generated_at: float, mode: "self"|"free")
# Cleared on process restart — that's fine, refinement is short-window UX.
_LAST_IMAGE_PROMPT: dict[str, tuple[str, float, str]] = {}
_IMAGE_REFINE_TTL_SECONDS = 600  # 10 minutes

# Per-user lock so a second image request while one is in flight doesn't
# blow the Pollinations 1-concurrent-request-per-IP limit. The second
# request waits behind the first instead of racing it (and getting an
# HTTP 402 "queue full" rejection).
import threading as _threading
_IMAGE_USER_LOCKS: dict[str, "_threading.Lock"] = {}
_IMAGE_LOCKS_GUARD = _threading.Lock()

# Pending ad brief — set when we ask the user clarifying questions,
# consumed on the next user message which we treat as their answers.
# Tuple: (original_brief, platform, set_at_ts). 5 minute TTL.
_PENDING_AD_BRIEF: dict[str, tuple[str, str, float]] = {}
_PENDING_AD_TTL_SECONDS = 300

def _user_image_lock(username: str) -> "_threading.Lock":
    with _IMAGE_LOCKS_GUARD:
        lk = _IMAGE_USER_LOCKS.get(username)
        if lk is None:
            lk = _threading.Lock()
            _IMAGE_USER_LOCKS[username] = lk
        return lk

# Platform/format detection — pick the right canvas size from natural
# language. Order matters: more specific patterns first (instagram_story
# before instagram_square, facebook_cover before facebook_post).
_IMAGE_KIND_RULES = [
    # (regex, kind)
    (r"\binstagram\s+stor(?:y|ies)\b|\big\s+stor(?:y|ies)\b",          "instagram_story"),
    (r"\binstagram\s+portrait\b|\bportrait\s+(?:for\s+)?(?:ig|insta(?:gram)?)\b", "instagram_portrait"),
    (r"\binstagram\s+(?:post|square)\b|\big\s+post\b|\binsta(?:gram)?\b", "instagram_square"),
    (r"\bfacebook\s+cover\b|\bfb\s+cover\b",                            "facebook_cover"),
    (r"\bfacebook\s+post\b|\bfb\s+post\b|\bfacebook\b",                 "facebook_post"),
    (r"\btwitter\s+post\b|\bx\s+post\b|\btweet\b|\btwitter\b|\bx\.com\b","twitter_post"),
    (r"\blinkedin\s+(?:post)?\b",                                        "linkedin_post"),
    (r"\btiktok\b|\btik[-\s]?tok\b",                                     "tiktok_post"),
    (r"\byoutube\s+thumbnail\b|\byt\s+thumb(?:nail)?\b|\byoutube\b",     "youtube_thumbnail"),
    (r"\bpinterest\s+pin\b|\bpin(?:terest)?\b",                          "pinterest_pin"),
    (r"\bbusiness\s+card\b",                                             "business_card"),
    (r"\bflyer\b|\bhandout\b",                                           "flyer_portrait"),
    (r"\bposter\b",                                                      "poster_portrait"),
    (r"\bstory\b",                                                       "instagram_story"),
    (r"\b(?:wide|landscape|horizontal|widescreen|16[:x]9)\b",            "wide"),
    (r"\b(?:tall|portrait|vertical|9[:x]16)\b",                          "tall"),
    (r"\b(?:square|1[:x]1)\b",                                           "square"),
    (r"\bbanner\b",                                                      "banner"),
]
_IMAGE_KIND_RES = [(_re.compile(p, _re.IGNORECASE), k) for p, k in _IMAGE_KIND_RULES]

def _detect_image_kind(msg: str) -> str:
    for rx, kind in _IMAGE_KIND_RES:
        if rx.search(msg):
            return kind
    return "instagram_square"  # default — most-used marketing format

# Caption extraction — "with the text 'X'", "with caption 'X'", "saying 'X'",
# "that says 'X'". Extracts X (inside quotes if present, else up to end).
_CAPTION_RES = [
    _re.compile(r"""(?:with\s+(?:the\s+)?(?:text|caption|words|title|headline)|saying|that\s+says|reads?)\s*[:]?\s*['"“‘]([^'"”’]+)['"”’]""", _re.IGNORECASE),
    _re.compile(r"""(?:with\s+(?:the\s+)?(?:text|caption|words|title|headline)|saying|that\s+says|reads?)\s*[:]?\s+(.{2,80}?)(?:\s*$|[\.!?])""", _re.IGNORECASE),
]
def _extract_caption(msg: str) -> str:
    for rx in _CAPTION_RES:
        m = rx.search(msg)
        if m:
            cap = m.group(1).strip().strip('"“”‘’')
            # Don't accept obvious garbage like a single word "X" or 1 char
            if len(cap) >= 2 and len(cap) <= 120:
                return cap
    return ""

# Anchor used when the prior image was a self-portrait, so refinements like
# "more humanoid" keep brand identity (purple, glowing, friendly) instead
# of drifting into generic stock imagery.
_ORBY_SELF_BASE = (
    "Orbi the friendly AI assistant, translucent glowing purple aura, "
    "soft blue and violet accent lighting, modern minimalist digital art, "
    "cosmic background with gentle aurora wisps, square composition, "
    "high detail, cinematic, no text, no watermark"
)
_ORBY_SELF_ORB = (
    "a friendly glowing translucent purple orb of light, "
    "floating in a soft dark cosmic background with gentle "
    "blue and violet wisps of aurora, smooth volumetric "
    "lighting, ethereal, abstract, no human figures, no faces, "
    "no text, modern minimalist digital art, square composition, "
    "high detail, cinematic"
)


def _try_office_gen(message: str, username: str) -> dict | None:
    """Detect chart / deck / image generation intents and fire the
    corresponding generator. Returns a chat-shaped reply or None to
    fall through to the LLM."""
    msg = (message or "").strip()
    if not msg:
        return None
    # Strip leading wrapper punctuation that users often type when pasting
    # back a quoted example or example list (— • " ' " " etc.). Without
    # this, "\"create me a complete facebook ad...\"" silently fails every
    # trigger regex because '^\s*' doesn't span past the leading quote.
    msg = msg.lstrip("\"'`“”‘’*-•—> \t")
    # Multi-line pastes (bullet lists, explanations) — match only the first
    # line for the trigger. Otherwise "create me a facebook ad\n- another\n
    # - third" tries to use the whole paste as the brief.
    first_line = msg.split("\n", 1)[0].strip()
    # Visibility for diagnosing "wrong thing got drawn" / "she didn't draw" reports.
    log.info("office_gen entry msg=%r first_line=%r",
             msg[:120], first_line[:80])

    # ── Pending-ad-clarification consumer ───────────────────────────────────
    # If we asked the user 2-3 questions about their ad on the previous turn,
    # this turn's message is the answers. Combine with the original partial
    # brief and route straight to ad_gen.build_ad (skip the clarification
    # check that triggered the questions in the first place).
    #
    # BAIL-OUT: if the new message clearly isn't an answer (it's a fresh
    # command/question/composition request), CLEAR the pending state and
    # let the message route normally. Otherwise we'd hijack unrelated
    # follow-ups as ad answers.
    pending = _PENDING_AD_BRIEF.get(username)
    looks_like_fresh_intent = bool(_re.match(
        r"^\s*(?:(?:can|could|would|will)\s+you\s+|please\s+)?"
        r"(?:create|build|make|design|draft|write|compose|put\s+together|"
        r"show|find|search|send|email|text|call|book|schedule|cancel|"
        r"add|remove|delete|update|remember|note|save|"
        r"what|how|when|where|why|who|which|"
        r"factory\s+reset|rollback|restore|"
        r"is\s+there|check\s+for|tell\s+me|list)\b",
        first_line, _re.IGNORECASE))
    if pending and (time.time() - pending[2]) < _PENDING_AD_TTL_SECONDS \
            and not looks_like_fresh_intent:
        original_brief, platform, _ = pending
        del _PENDING_AD_BRIEF[username]
        combined_brief = (f"{original_brief}. {msg}" if original_brief else msg).strip()
        log.info("office_gen ad clarification answered: combined_brief=%r",
                 combined_brief[:120])
        biz = mod_business.load(DATA_DIR)
        try:
            with _user_image_lock(username):
                png, components = ad_gen.build_ad(
                    CONFIG, combined_brief, business=biz, platform=platform,
                    data_dir=DATA_DIR)
        except RuntimeError as e:
            if "image_service_unavailable" in str(e):
                return {"reply": ("Image service is busy — try saying \"redo\" "
                                   "in a few seconds."),
                        "tier": "local", "latency_ms": 0,
                        "source": "image_gen_busy"}
            return {"reply": f"Ad build failed: {e}",
                    "tier": "local", "latency_ms": 0,
                    "source": "ad_gen_error"}
        ws = mod_workspace.workspace_path(CONFIG)
        saved_path = ad_gen.save_ad_to_workspace(png, combined_brief, ws)
        try:
            token = file_fetch.mint_download_token(
                DATA_DIR, str(saved_path), ttl_minutes=30,
                extra_allowed_roots=[ws])
            url = f"/download/{token}"
        except Exception:
            url = None
        audit.log_event(DATA_DIR, actor=username, action="ad.via_chat",
                        meta={"brief": combined_brief[:120],
                              "headline": components["headline"][:80]})
        _LAST_IMAGE_PROMPT[username] = (
            components["image_brief"], time.time(), "free")
        alts = components.get("headline_alts") or []
        alts_line = (" Alt headlines: "
                     + " · ".join(f'"{a}"' for a in alts[:2])) if alts else ""
        reply = (f"Built your {platform.replace('_', ' ')} ad — "
                 f"headline: \"{components['headline']}\" · "
                 f"CTA: \"{components['cta']}\".{alts_line} "
                 "Composited image + copy + button, saved to your Files tab.")
        return {"reply": reply,
                "tier": "local", "latency_ms": 0,
                "source": "ad_gen", "download_url": url}

    try:
        if _CHART_TRIGGER_RE.match(first_line):
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

        if _DECK_TRIGGER_RE.match(first_line):
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

        # ── Refinement-after-image shortcut ─────────────────────────────────
        # "more humanoid" / "make it bigger" / "different style" / "(for
        # instagram)" / "draw it again but full body" — these are all
        # follow-ups on the last image. Cached prompt + refinement text
        # → re-fire image_gen.
        refine_now = time.time()
        cached = _LAST_IMAGE_PROMPT.get(username)
        msg_for_check = msg.strip("() ")
        # GUARD: detect when the user pasted an LLM response back in (often
        # by accident or to show it to me). These should NEVER trigger
        # refinement — they're not user instructions, they're the previous
        # assistant turn. Signals: starts with a typical assistant opener,
        # contains "I drew" / "I'll try" / etc., or has quoted phrases.
        looks_like_llm_paste = bool(_re.match(
            r"^\s*(?:here'?s\s+(?:what|the|a)|sure[,!]|i'?ll\s+try|"
            r"i'd\s+be\s+happy|got\s+it[,!]|i\s+(?:drew|generated|created|"
            r"made|designed|rendered)|i'?m\s+(?:going\s+to|gonna)|"
            r"let\s+me|absolutely[,!]|of\s+course[,!])",
            msg, _re.IGNORECASE
        ))
        looks_like_platform_only = (
            len(msg_for_check) < 60
            and bool(_re.match(r"^\s*(?:for\s+)?", msg_for_check, _re.IGNORECASE))
            and any(rx.search(msg_for_check) for rx, _ in _IMAGE_KIND_RES)
        )
        # Explicit "do this again" language. STRONGER signal than the
        # generic drawing trigger — "draw it again" / "redo" / "one more"
        # always means "refine the previous", never "fresh draw of new
        # subject", even though the message contains a drawing verb.
        is_explicit_refinement = bool(_re.search(
            r"\b(?:again|one\s+more(?:\s+time)?|once\s+(?:more|again)|"
            r"another\s+(?:version|one|try|round|time)|"
            r"redo|retry|do\s+(?:it|that)\s+(?:over|again)|"
            r"same\s+(?:thing|image|one)\s+but)\b",
            msg, _re.IGNORECASE))
        # _IMAGE_REFINE_RE has loose words like 'with' / 'more' / 'less' that
        # appear in plenty of NON-refinement messages ("remember I run Orbi
        # with no developer background"). Require the refinement word to
        # appear near the START of the message OR for the whole message to
        # be very short — otherwise it's not really a refinement.
        refine_kw_match = _IMAGE_REFINE_RE.search(msg)
        refine_at_start = bool(refine_kw_match) and (
            refine_kw_match.start() < 12 or len(msg) < 40)
        is_refinement = (
            cached
            and (refine_now - cached[1]) < _IMAGE_REFINE_TTL_SECONDS
            and len(msg) < 200
            and not looks_like_llm_paste   # never refine on a pasted assistant reply
            and (
                is_explicit_refinement
                or refine_at_start
                or looks_like_platform_only
            )
            and (
                # Explicit refinement language overrides the trigger; otherwise
                # require trigger absence so genuine fresh draws don't get
                # mis-routed.
                is_explicit_refinement
                or (not _IMAGE_TRIGGER_RE.match(msg)
                    and not _IMAGE_LOOSE_RE.match(msg)
                    and not _AD_TRIGGER_RE.match(first_line)
                    and not _AD_LEARN_TRIGGER_RE.match(first_line)
                    and not _re.match(
                        r"^\s*(?:remember|note|save|long[- ]?term\s+memory)",
                        msg, _re.IGNORECASE))
            )
        )
        if cached and not is_refinement:
            log.info("office_gen refinement skipped: msg=%r cache_age=%.0fs "
                     "refine_kw=%s platform_only=%s explicit=%s",
                     msg[:80], refine_now - cached[1],
                     bool(_IMAGE_REFINE_RE.search(msg)),
                     looks_like_platform_only,
                     is_explicit_refinement)
        elif is_refinement:
            log.info("office_gen refinement HIT: msg=%r cache_age=%.0fs mode=%s "
                     "explicit=%s",
                     msg[:80], refine_now - cached[1], cached[2],
                     is_explicit_refinement)
        if is_refinement:
            base_prompt, _, mode = cached
            refinement = msg.strip().rstrip(".?!")
            # Strip away "draw it again" / "redo" / "another version" filler
            # from the refinement text so we don't pass FLUX literal phrases
            # like "draw it again but X" — we just want the "X" part.
            refinement = _re.sub(
                r"^\s*(?:(?:can|could|would|will)\s+you\s+)?"
                r"(?:please\s+)?"
                r"(?:draw|paint|sketch|render|make|create|generate|do|try)\s+"
                r"(?:it|that|this|one|another\s+(?:one|version))?\s*"
                r"(?:again|one\s+more\s+time|once\s+more)?\s*"
                r"(?:but|except|with|in|as)?\s*",
                "", refinement, flags=_re.IGNORECASE).strip()
            # If stripping nuked everything, fall back to original
            if not refinement:
                refinement = msg.strip().rstrip(".?!")
            # For SELF-portrait refinements, keep the cached prompt as-is
            # (it already includes prior descriptors like "humanoid robot")
            # UNLESS the cached prompt is the bare orb template (which has
            # "no human figures" negatives that conflict with most
            # refinements). In that case swap to the humanoid base.
            if mode == "self":
                if ("no human figures" in base_prompt
                    or "no faces" in base_prompt):
                    base = _ORBY_SELF_BASE
                else:
                    base = base_prompt
            else:
                base = base_prompt
            prompt = f"{base}, {refinement}"
            kind = _detect_image_kind(msg) if _detect_image_kind(msg) != "instagram_square" else _detect_image_kind(base_prompt)
            try:
                with _user_image_lock(username):
                    png = image_gen.generate(CONFIG, prompt, kind=kind)
            except RuntimeError as e:
                if "image_service_unavailable" in str(e):
                    return {"reply": ("The image service is busy — your refinement "
                                       "didn't go through. Try again in a few seconds."),
                            "tier": "local", "latency_ms": 0,
                            "source": "image_gen_busy"}
                raise
            caption = _extract_caption(msg)
            if caption:
                png = image_gen.overlay_caption(png, caption)
            ws = mod_workspace.workspace_path(CONFIG)
            saved_path = image_gen.save_to_workspace(png, prompt, ws)
            try:
                token = file_fetch.mint_download_token(
                    DATA_DIR, str(saved_path), ttl_minutes=30,
                    extra_allowed_roots=[ws])
                url = f"/download/{token}"
            except Exception:
                url = None
            _LAST_IMAGE_PROMPT[username] = (prompt, refine_now, mode)
            audit.log_event(DATA_DIR, actor=username, action="image.refine",
                            meta={"refinement": refinement[:120]})
            short = refinement if len(refinement) <= 80 else refinement[:77] + "..."
            return {"reply": f"Here's a new version — {short}. Also saved to your Files tab.",
                    "tier": "local", "latency_ms": 0,
                    "source": "image_gen", "download_url": url}

        # ── SAVE-AD-EXEMPLAR ("learn from this ad: [paste]") ──────────────
        # Match against first_line so we can store the rest of the multi-line
        # paste as the exemplar. "save this ad: <newline> [pasted ad text]"
        if _AD_LEARN_TRIGGER_RE.match(first_line):
            log.info("office_gen ad-learn branch fired first_line=%r",
                     first_line[:120])
            # Exemplar text = everything AFTER the trigger phrase. If multi-
            # line, lines 2+ are almost always the pasted ad. If single-line,
            # take the part after a ":" if there is one.
            after_trigger = _re.sub(
                _AD_LEARN_TRIGGER_RE.pattern,
                "",
                first_line,
                count=1,
                flags=_re.IGNORECASE,
            ).strip(" :,.-—")
            rest_of_msg = (msg.split("\n", 1)[1].strip()
                            if "\n" in msg else "")
            exemplar_text = (rest_of_msg or after_trigger).strip()
            if not exemplar_text or len(exemplar_text) < 20:
                return {
                    "reply": ("Paste the ad text on the next line and try "
                              "again — I need at least a headline and body "
                              "to store as a style example. Like:\n\n"
                              "  save this ad:\n"
                              "  Tired of missed calls?\n"
                              "  Our AI receptionist answers 24/7, books "
                              "appointments, and follows up — so you can "
                              "focus on the work.\n"
                              "  Try it free for 14 days."),
                    "tier": "local", "latency_ms": 0,
                    "source": "ad_learn_empty",
                }
            try:
                entry = ad_gen.save_exemplar(
                    DATA_DIR, exemplar_text, source="owner_paste")
            except Exception as e:
                log.exception("save_exemplar failed")
                return {"reply": f"Couldn't save the exemplar: {e}",
                        "tier": "local", "latency_ms": 0,
                        "source": "ad_learn_error"}
            audit.log_event(DATA_DIR, actor=username,
                             action="ad.exemplar_saved",
                             meta={"chars": len(exemplar_text),
                                   "id": entry["id"]})
            count = len(ad_gen.load_exemplars(DATA_DIR, limit=999))
            return {
                "reply": (f"Saved that as a style example. I'll match its "
                          f"tone and structure on future ads. ({count} "
                          f"example{'s' if count != 1 else ''} on file now.) "
                          "Build an ad with \"create me a facebook ad for "
                          "...\" and you'll see the style come through."),
                "tier": "local", "latency_ms": 0,
                "source": "ad_learn_saved",
            }

        # ── FINISHED AD (image + headline + body + CTA composite) ──────────
        # "create me a complete facebook ad for our weekend brunch" →
        # ad_gen designs the ad (LLM), generates the background image,
        # composites text + CTA button → one finished PNG ready to upload.
        # Match against first_line so multi-line / quoted pastes still fire.
        if _AD_TRIGGER_RE.match(first_line):
            log.info("office_gen ad branch fired first_line=%r", first_line[:120])
            # Extract platform from the message — same kind-detector as images
            platform = _detect_image_kind(first_line)
            # Strip the trigger phrase so the brief sent to ad_gen is just
            # the user's intent (e.g. "for our weekend brunch")
            brief = _re.sub(
                _AD_TRIGGER_RE.pattern,
                "",
                first_line,
                count=1,
                flags=_re.IGNORECASE,
            ).strip(" ,.:;-—\"'")
            # Remove leading "for" / "about" so brief reads naturally
            brief = _re.sub(r"^(?:for|about|on|to\s+promote)\s+",
                            "", brief, flags=_re.IGNORECASE).strip()
            if not brief:
                brief = ""
            biz = mod_business.load(DATA_DIR)
            # ChatGPT-style: if the brief is thin, ASK 2-3 clarifying
            # questions before burning a Pollinations call on a vague ad.
            clarifications = ad_gen.brief_needs_clarification(brief)
            if clarifications:
                q_lines = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(clarifications))
                reply = ("Before I build the ad, can you fill in a couple of "
                          "details so it actually performs?\n\n"
                          f"{q_lines}\n\n"
                          "Reply with the answers (one line is fine) and I'll "
                          "design the headline, body, CTA, and the image — all "
                          "composited into one PNG ready to upload.")
                # Cache the partial brief so the next answer message rebuilds
                # the request with the original "make me an ad" intent.
                _PENDING_AD_BRIEF[username] = (brief, platform, time.time())
                return {"reply": reply, "tier": "local", "latency_ms": 0,
                        "source": "ad_clarify"}
            try:
                with _user_image_lock(username):
                    png, components = ad_gen.build_ad(
                        CONFIG, brief, business=biz, platform=platform,
                        data_dir=DATA_DIR)
            except RuntimeError as e:
                if "image_service_unavailable" in str(e):
                    return {"reply": ("The image service is busy — couldn't "
                                       "render the ad background. Try again in "
                                       "a few seconds."),
                            "tier": "local", "latency_ms": 0,
                            "source": "image_gen_busy"}
                log.exception("ad_gen failed")
                return {"reply": (f"I couldn't build the ad: {e}. "
                                   "Try simplifying the brief or asking again."),
                        "tier": "local", "latency_ms": 0,
                        "source": "ad_gen_error"}
            ws = mod_workspace.workspace_path(CONFIG)
            saved_path = ad_gen.save_ad_to_workspace(png, brief, ws)
            try:
                token = file_fetch.mint_download_token(
                    DATA_DIR, str(saved_path), ttl_minutes=30,
                    extra_allowed_roots=[ws])
                url = f"/download/{token}"
            except Exception:
                url = None
            audit.log_event(DATA_DIR, actor=username, action="ad.via_chat",
                            meta={"brief": brief[:120],
                                  "headline": components["headline"][:80]})
            # Cache the image_brief so refinements ("make it brighter") still work
            _LAST_IMAGE_PROMPT[username] = (
                components["image_brief"], time.time(), "free")
            alts = components.get("headline_alts") or []
            alts_line = (" Alt headlines: "
                         + " · ".join(f'"{a}"' for a in alts[:2])) if alts else ""
            reply = (f"Built a {platform.replace('_', ' ')} ad — "
                     f"headline: \"{components['headline']}\" · "
                     f"CTA: \"{components['cta']}\".{alts_line} "
                     "Composited image + copy + button, saved to your Files tab.")
            return {"reply": reply,
                    "tier": "local", "latency_ms": 0,
                    "source": "ad_gen", "download_url": url}

        # ── Referenced-image disambiguation ─────────────────────────────────
        # "show me the images you were talking about" / "draw those pictures
        # you mentioned" — the user is referring to image briefs the LLM
        # described in a prior turn (often inside a marketing campaign).
        # We have NO way to know which one without context, and sending the
        # literal phrase to FLUX produces a stock-photo woman.  Respond
        # with a coaching prompt instead.
        if _IMAGE_REFERENCED_RE.search(first_line):
            log.info("office_gen referenced-image disambiguation: msg=%r", first_line[:80])
            is_all = bool(_re.search(r"\ball\b", first_line, _re.IGNORECASE))
            if is_all:
                reply = ("I have to draw the campaign images one at a time — "
                         "if I send a single batch prompt like \"all the campaign "
                         "images\" the model tries to fit everything into ONE "
                         "image and you get a mess (the word \"campaign\" alone "
                         "also makes it produce military scenes). "
                         "Tell me which one to draw first, in your own words. "
                         "For example: \"draw the futuristic Orbi interface\" "
                         "or \"draw the busy business storefront\". "
                         "After that one's done, just say \"next one\" and "
                         "we'll keep going.")
            else:
                reply = ("I can draw each of those for you, one at a time — "
                         "just tell me which one you want and I'll generate it. "
                         "For example: \"draw the futuristic Orbi interface image\" "
                         "or \"draw the busy business storefront image\".")
            return {
                "reply": reply,
                "tier": "local", "latency_ms": 0,
                "source": "image_disambiguation",
            }

        image_match = (_IMAGE_TRIGGER_RE.match(first_line)
                       or _IMAGE_LOOSE_RE.match(first_line)
                       or _IMAGE_SUBJECT_LED_RE.match(first_line))
        if image_match:
            # ── Self-portrait shortcut ──────────────────────────────────────
            # "draw yourself" / "what you look like" / "what you imagine you
            # would look like" — the user wants Orby's self-image, not a
            # generic prompt. Use a brand-aligned orb portrait so FLUX
            # doesn't default to a stock photo of a person.
            mode = "free"
            if _IMAGE_SELF_RE.search(msg):
                mode = "self"
                # Hybrid request? "draw yourself as a robot", "draw you in a
                # forest", "draw your face on a t-shirt". Pull the descriptor
                # so we don't drop the user's actual subject hint.
                as_match = _re.search(
                    r"\bas\s+(?:an?\s+)?(.{2,120}?)(?:[\.!?]|$)",
                    msg, _re.IGNORECASE)
                descriptor = as_match.group(1).strip().rstrip(",") if as_match else ""
                # Form keywords that imply the user wants a body, not an orb
                form_words = _re.search(
                    r"\b(?:robot|humanoid|character|figure|person|human|woman|"
                    r"man|girl|guy|cyborg|android|hero|mascot|cartoon|anime)\b",
                    msg, _re.IGNORECASE)
                if descriptor or form_words:
                    # Hybrid — use the humanoid Orby base + descriptor so
                    # "draw yourself as a robot" actually produces a robot
                    # in Orby colors instead of a pure orb.
                    prompt = _ORBY_SELF_BASE
                    if descriptor:
                        prompt = f"{prompt}, depicted as {descriptor}"
                    elif form_words:
                        prompt = f"{prompt}, {form_words.group(0)} form"
                else:
                    prompt = _ORBY_SELF_ORB
                log.info("image_gen: self-portrait branch fired msg=%r descriptor=%r form=%r",
                         msg[:120], descriptor, bool(form_words))
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
                    # 2. drawing verb (+ conjugation) + me/us + a/an/the/some
                    r"^\s*(?:make(?:s|d|ing)?|build(?:s|ing)?|create(?:s|d|ing)?|"
                    r"generate(?:s|d|ing)?|design(?:s|ed|ing)?|draw(?:s|ing|n)?|drew|"
                    r"paint(?:s|ed|ing)?|render(?:s|ed|ing)?|sketch(?:es|ed|ing)?|"
                    r"mock\s*up|come\s+up\s+with|whip\s+up|illustrate(?:s|d|ing)?|"
                    r"visualize(?:s|d|ing)?|visualise(?:s|d|ing)?)\s+"
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
            # Detect platform/format ("for instagram story", "twitter post",
            # "wide", "tall", "flyer", "youtube thumbnail") and pick the
            # right canvas size. Falls back to instagram_square if no hint.
            kind = _detect_image_kind(msg)
            try:
                with _user_image_lock(username):
                    png = image_gen.generate(CONFIG, prompt, kind=kind)
            except RuntimeError as e:
                if "image_service_unavailable" in str(e):
                    return {"reply": ("The image service is busy right now — "
                                       "Pollinations queues up under load. "
                                       "Try the same prompt again in a few seconds."),
                            "tier": "local", "latency_ms": 0,
                            "source": "image_gen_busy"}
                raise
            # If the owner asked for caption text ("with text X" / "saying X"),
            # PIL-overlay it on top — FLUX can't render readable text itself.
            caption = _extract_caption(msg)
            if caption:
                png = image_gen.overlay_caption(png, caption)
            ws = mod_workspace.workspace_path(CONFIG)
            saved_path = image_gen.save_to_workspace(png, prompt, ws)
            try:
                token = file_fetch.mint_download_token(
                    DATA_DIR, str(saved_path), ttl_minutes=30,
                    extra_allowed_roots=[ws])
                url = f"/download/{token}"
            except Exception:
                url = None
            # Cache for refinement follow-ups ("more humanoid", "make it bigger")
            _LAST_IMAGE_PROMPT[username] = (prompt, time.time(), mode)
            audit.log_event(DATA_DIR, actor=username, action="image.via_chat",
                            meta={"prompt": prompt[:120]})
            # Short caption only — the inline <img> in the bubble IS the
            # preview, so a separate "[Download it](...)" markdown link
            # would render as ugly raw text. Click the image for full size.
            # For self-portraits the prompt is a long brand template; show
            # the original user message instead so the caption is readable.
            caption_src = msg if mode == "self" else prompt
            short_prompt = caption_src if len(caption_src) <= 80 else caption_src[:77] + "..."
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
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    try:
        meta = users_mod.deactivate_user(DATA_DIR, username)
        # Capture the reason in audit so owner can see WHY later (leave of
        # absence vs departed vs other)
        log_meta = dict(meta)
        if reason:
            log_meta["reason"] = reason[:200]
        audit.log_event(DATA_DIR, actor=owner["username"],
                        action="users.deactivate", resource=username, meta=log_meta)
        return jsonify({"status": "ok", "archive": meta, "reason": reason})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/owner/users/<username>/reactivate", methods=["POST"])
def user_reactivate(username):
    """People-tab consistent path for reactivation. Same function as
    /api/owner/staff/<username>/reactivate — kept for namespace symmetry
    with /deactivate, /hold, /transfer."""
    owner = auth.require_role(ORBI_DIR, DATA_DIR, "owner")
    try:
        rec = users_mod.reactivate_user(DATA_DIR, username)
        audit.log_event(DATA_DIR, actor=owner["username"],
                        action="users.reactivate", resource=username)
        return jsonify({"status": "ok", "user": rec})
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
                    if prefs.get("enabled", True):
                        if (now_hour >= int(prefs.get("hour", 7))
                                and briefing.should_send_today(user_dir)):
                            briefing.send_morning_brief(CONFIG, DATA_DIR, username)
                            log.info(f"morning brief sent: {username}")
                    # End-of-day summary — separate trigger, separate state
                    if prefs.get("eod_enabled", True):
                        if (now_hour >= int(prefs.get("eod_hour", 18))
                                and briefing.should_send_eod_today(user_dir)):
                            briefing.send_eod_summary(CONFIG, DATA_DIR, username)
                            log.info(f"eod summary sent: {username}")
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
    try:
        def _update_notify(info: dict) -> None:
            """When the updater finds a new release, push to the owner."""
            try:
                notify.send(CONFIG, DATA_DIR,
                             event="update_available",
                             title=f"Orbi {info.get('tag')} is available",
                             body=("Say \"install update\" in chat or open "
                                    "Settings → Updates to apply it."),
                             url="/owner")
            except Exception:    # noqa: BLE001
                log.exception("update notify push failed")
        updater.start_update_check_scheduler(CONFIG, DATA_DIR,
                                              notify_callback=_update_notify)
    except Exception as e:
        log.warning(f"updater scheduler failed to start: {e}")
    try:
        biz = mod_business.load(DATA_DIR)
        def _friend_checkin_notify(title: str, body: str) -> None:
            try:
                notify.send(CONFIG, DATA_DIR,
                             event="friend_checkin",
                             title=title, body=body, url="/owner")
            except Exception:
                log.exception("friend check-in push failed")
        friend_checkin.start_checkin_scheduler(
            CONFIG, DATA_DIR, biz, notify_callback=_friend_checkin_notify)
    except Exception as e:
        log.warning(f"friend check-in scheduler failed to start: {e}")

    # Birthdays + anniversaries — daily sweep that schedules reminders
    # ~3 days before each upcoming personal milestone. Idempotent per
    # contact per year. Was previously only triggerable manually via
    # /api/owner/birthdays/sweep_now.
    def _birthdays_sweep_loop():
        time.sleep(120)   # let app fully boot first
        while True:
            try:
                created = birthdays.run_sweep(CONFIG, DATA_DIR)
                if created:
                    log.info(f"birthdays daily sweep: {created} reminder(s) scheduled")
            except Exception:
                log.exception("birthdays sweep crashed")
            time.sleep(24 * 3600)
    threading.Thread(target=_birthdays_sweep_loop, daemon=True,
                      name="orbi-birthdays-sweep").start()
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
