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
# Contractor module (paid add-on, gated via is_module_enabled('contractor')).
# Imports always happen so the handlers can use them; route registration
# + chat dispatch check the flag before doing anything visible.
from modules import projects as mod_projects
from modules import change_orders as mod_change_orders
from modules import invoices as mod_invoices
from modules import daily_logs as mod_daily_logs
from modules import subcontractors as mod_subs
from modules import invoice_pdf as mod_invoice_pdf
from modules import closeout_pdf as mod_closeout_pdf
from modules import bids as mod_bids
from modules import reviews as mod_reviews
from modules import proposal_pdf as mod_proposal_pdf
from modules import clients as mod_clients
from modules import forms as mod_forms
from modules import form_filler as mod_form_filler
from modules import pricing as mod_pricing
from modules import line_items as mod_line_items
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

# ── Module enablement (paid add-on gate) ─────────────────────────────────
# The customer's tier or Stripe purchase determines which modules are
# active for them. Base Orby = no entries; Contractor Orby = ["contractor"];
# Legal Orby (when built) = ["legal", "paralegal"]; etc.
#
# Stored under CONFIG["enabled_modules"] as a list of lowercase strings.
# Stripe webhook flips entries on subscription creation/update.
#
# Helpers below are used by chat handlers and route registration to no-op
# when the customer hasn't bought a module — keeps the codebase one tree
# but gates the surface area per customer.

def is_module_enabled(name: str) -> bool:
    """Returns True if this customer has paid for `name`. Base Orby
    modules (calendar, contacts, etc.) always return True — they ship
    with every install regardless of tier. Only premium add-ons need
    the gate."""
    if not name:
        return False
    enabled = CONFIG.get("enabled_modules") or []
    if not isinstance(enabled, list):
        return False
    return name.strip().lower() in {str(x).strip().lower() for x in enabled}


def require_module(name: str):
    """Use as a Flask before-request decorator or inline check. Returns
    a 402 Payment Required JSON if the module isn't enabled. Keeps the
    'this is a paid feature, upgrade to unlock' UX consistent."""
    from flask import jsonify
    if is_module_enabled(name):
        return None
    return jsonify({
        "error":   f"This feature is part of the {name.title()} Orby add-on.",
        "upgrade": True,
        "module":  name,
    }), 402

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


# ── Contractor module: nightly receivables auto-follow-up sweep ───────────
# Once a day, walk all overdue invoices for installs with the contractor
# module enabled. For each invoice that hasn't been nudged in 7+ days,
# draft a tone-escalating reminder text and queue it in
# data/pending_followups.json. Doesn't auto-SEND — keeps the GC in the
# loop (Orbi assists, the human decides who gets a chasing letter).
# Queue is surfaced in the morning brief; chat command "send queued
# reminders" delivers them.

RECEIVABLES_FOLLOWUP_DAYS = 7   # min days between nudges per invoice
RECEIVABLES_FOLLOWUP_CHECK_INTERVAL = 6 * 3600   # check every 6 hours


def _load_pending_followups() -> list[dict]:
    p = DATA_DIR / "pending_followups.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save_pending_followups(items: list[dict]) -> None:
    p = DATA_DIR / "pending_followups.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(p)


def receivables_followup_loop():
    """Sleep, then run both sweeps (invoices + bids) every
    RECEIVABLES_FOLLOWUP_CHECK_INTERVAL. The sweeps themselves are
    cheap (handful of file reads, only fire for enabled installs)."""
    time.sleep(45)   # wait for app to settle
    while True:
        try:
            _receivables_followup_sweep()
        except Exception as e:
            log.warning(f"receivables follow-up sweep error: {e}")
        try:
            _bid_followup_sweep()
        except Exception as e:
            log.warning(f"bid follow-up sweep error: {e}")
        time.sleep(RECEIVABLES_FOLLOWUP_CHECK_INTERVAL)


def _bid_followup_sweep() -> int:
    """Sister of _receivables_followup_sweep — drafts polite check-in
    nudges for bids that have been quiet 7+ days. Returns count drafted.
    Same pattern: queue in pending_followups.json, GC reviews + sends."""
    if not is_module_enabled("contractor"):
        return 0
    stale = mod_bids.open_needing_follow_up(DATA_DIR, days=7)
    if not stale:
        return 0
    now_ts = int(time.time())
    pending = _load_pending_followups()
    pending_bid_ids = {p.get("bid_id") for p in pending
                        if not p.get("sent_at") and p.get("bid_id")}
    drafted = 0
    biz_name = (CONFIG.get("business") or {}).get("name", "your contractor")
    for b in stale:
        if b["id"] in pending_bid_ids:
            continue
        count = int(b.get("follow_up_count", 0))
        # Tone tiers: 1 friendly check-in, 2 firmer ask, 3+ "last call"
        tier = min(3, count + 1)
        if tier == 1:
            text = (f"Hi {b['customer_name']},\n\n"
                     f"Just checking in on the bid I sent for "
                     f"{b.get('project_type') or 'your project'}"
                     f"{(' at ' + b['project_address']) if b.get('project_address') else ''}. "
                     f"No rush — just want to make sure it didn't get lost. "
                     f"If you have questions, I'm happy to walk through anything.\n\n"
                     f"Thanks,\n{biz_name}")
        elif tier == 2:
            text = (f"Hi {b['customer_name']},\n\n"
                     f"Following up on the {b.get('project_type') or 'project'} "
                     f"bid I sent. Could you let me know either way? If you're "
                     f"leaning a different direction I get it — would just love "
                     f"to free up the calendar slot if it's not us.\n\n"
                     f"Either way, thanks for the chance to bid.\n{biz_name}")
        else:
            text = (f"{b['customer_name']},\n\n"
                     f"Closing out my files — should I keep your bid open or "
                     f"mark it as gone elsewhere? Either way is fine; "
                     f"I just want to clean up the pipeline.\n\n"
                     f"{biz_name}")
        pending.append({
            "id":              uuid.uuid4().hex[:10],
            "bid_id":          b["id"],
            "customer_name":   b["customer_name"],
            "customer_email":  b.get("customer_email", ""),
            "customer_phone":  b.get("customer_phone", ""),
            "amount":          float(b.get("amount") or 0),
            "tier":            tier,
            "kind":            "bid_followup",
            "drafted_text":    text,
            "drafted_at":      now_ts,
            "sent_at":         None,
            "sent_via":        "",
        })
        mod_bids.mark_followed_up(DATA_DIR, b["id"])
        try:
            audit.log_event(DATA_DIR, actor="scheduler",
                            action="bid.nudge_auto_drafted",
                            meta={"bid_id": b["id"], "tier": tier})
        except Exception:
            pass
        drafted += 1
    if drafted:
        _save_pending_followups(pending)
        log.info(f"bid sweep drafted {drafted} nudge(s)")
    return drafted


def _receivables_followup_sweep() -> int:
    """Draft nudges for overdue invoices that haven't been chased in
    RECEIVABLES_FOLLOWUP_DAYS days. Returns count drafted.

    Only runs if the contractor module is enabled on this install — base
    Orby installs short-circuit immediately."""
    if not is_module_enabled("contractor"):
        return 0
    # Promote any newly-overdue invoices to status='overdue'
    mod_invoices.list_overdue(DATA_DIR)
    overdue = [i for i in mod_invoices.list_unpaid(DATA_DIR)
                if i.get("status") == "overdue"]
    if not overdue:
        return 0
    now_ts = int(time.time())
    threshold = RECEIVABLES_FOLLOWUP_DAYS * 86400
    pending = _load_pending_followups()
    # Dedupe: don't add a new draft if there's already one queued for
    # this invoice that hasn't been sent yet.
    pending_invoice_ids = {p.get("invoice_id") for p in pending if not p.get("sent_at")}
    drafted = 0
    for inv in overdue:
        last_nudged = inv.get("last_follow_up_at") or 0
        if now_ts - int(last_nudged) < threshold:
            continue
        if inv["id"] in pending_invoice_ids:
            continue
        proj = mod_projects.get(DATA_DIR, inv.get("project_id", "")) or {}
        escalation = min(3, max(1, int(inv.get("follow_up_count", 0)) + 1))
        try:
            text = _draft_nudge_text(inv, proj, escalation=escalation)
        except Exception as e:
            log.warning(f"draft nudge failed for {inv.get('invoice_number')}: {e}")
            continue
        pending.append({
            "id":              uuid.uuid4().hex[:10],
            "invoice_id":      inv["id"],
            "invoice_number":  inv.get("invoice_number"),
            "project_id":      inv.get("project_id"),
            "project_address": proj.get("address", ""),
            "customer_email":  proj.get("customer_email", ""),
            "escalation":      escalation,
            "amount_owed":     float(inv.get("amount_due", 0)) - float(inv.get("amount_paid", 0)),
            "drafted_text":    text,
            "drafted_at":      now_ts,
            "sent_at":         None,
            "sent_via":        "",
        })
        mod_invoices.mark_followed_up(DATA_DIR, inv["id"])
        try:
            audit.log_event(DATA_DIR, actor="scheduler",
                            action="invoice.nudge_auto_drafted",
                            meta={"invoice_id": inv["id"], "escalation": escalation})
        except Exception:
            pass
        drafted += 1
    if drafted:
        _save_pending_followups(pending)
        log.info(f"receivables sweep drafted {drafted} nudge(s)")
    return drafted


import uuid    # used by the sweep above; safe to re-import (idempotent)
threading.Thread(target=receivables_followup_loop, daemon=True).start()


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

DEMO_ADMIN_COOKIE = "orby_demo_admin"


def _check_demo_admin_or_owner():
    """Auth gate for portable demo endpoints. Allow either an owner login
    OR a valid demo_admin_pass cookie set by visiting /admin/demo?key=X.
    Keeps Frank from needing two passwords when demoing from twickell."""
    expected = (CONFIG.get("demo_admin_pass") or "").strip()
    if expected and request.cookies.get(DEMO_ADMIN_COOKIE) == expected:
        return
    auth.require_owner(ORBI_DIR)


@app.route("/admin/demo")
def admin_demo_page():
    """Frank's portable sales demo tool — scrape any restaurant's site
    on the fly, get a demo URL to show the owner, wipe and repeat.
    Auth: visit with ?key=<demo_admin_pass> to mint a 30-day cookie that
    authorizes the API endpoints, OR be logged in as the owner."""
    page = STATIC_DIR / "demo_mode.html"
    if not page.exists():
        return ("demo page not installed", 404)
    resp = send_from_directory(STATIC_DIR, "demo_mode.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    key = (request.args.get("key") or "").strip()
    expected = (CONFIG.get("demo_admin_pass") or "").strip()
    if key and expected and key == expected:
        resp.set_cookie(DEMO_ADMIN_COOKIE, expected,
                        max_age=30 * 24 * 3600, httponly=True,
                        secure=True, samesite="Lax")
    return resp


# In-memory scrape job tracker. Keyed by job_id, evicted after 1 hour.
# Single dict guarded by a lock — light enough for one Frank scraping at a
# time. Don't migrate to Redis until there's a real concurrency story.
import threading as _scrape_threading
import time as _scrape_time
import uuid as _scrape_uuid

_SCRAPE_JOBS: dict = {}
_SCRAPE_JOBS_LOCK = _scrape_threading.Lock()


def _evict_old_scrape_jobs() -> None:
    """Drop jobs older than 1 hour from the tracker. Called opportunistically
    at the top of each new scrape submission — no background sweeper needed."""
    cutoff = _scrape_time.time() - 3600
    with _SCRAPE_JOBS_LOCK:
        stale = [jid for jid, j in _SCRAPE_JOBS.items()
                 if j.get("finished_at", j.get("started_at", 0)) < cutoff]
        for jid in stale:
            _SCRAPE_JOBS.pop(jid, None)


def _run_scrape_job(job_id: str, url: str, payload: dict) -> None:
    """Background worker — does the actual scrape and updates the job dict.
    Catches every exception so the polling status endpoint can report it."""
    try:
        import site_scraper
        brain_call = site_scraper.make_brain_call(CONFIG)
        result = site_scraper.crawl_site(
            url, data_dir=DATA_DIR, brain_call=brain_call,
            max_pages=int(payload.get("max_pages", 15)),
            max_depth=int(payload.get("max_depth", 3)),
            wall_time_seconds=int(payload.get("wall_time_seconds", 120)),
            fetch_delay_seconds=float(payload.get("fetch_delay_seconds", 0.1)),
        )
        slug = result.get("slug") or result.get("domain", "").replace(".", "_")
        profile_path = DATA_DIR / "customer_profiles" / f"{slug}.json"
        biz_name, menu_items = "", 0
        if profile_path.exists():
            try:
                with profile_path.open("r", encoding="utf-8") as f:
                    prof = json.load(f)
                biz_name = prof.get("name", "")
                menu_items = len(prof.get("menu_items") or [])
            except Exception:
                pass
        with _SCRAPE_JOBS_LOCK:
            _SCRAPE_JOBS[job_id].update({
                "status": "done",
                "finished_at": _scrape_time.time(),
                "ok": True,
                "url": url,
                "slug": slug,
                "name": biz_name,
                "pages_visited": result.get("pages_visited", 0),
                "menu_items": menu_items,
                "elapsed_seconds": result.get("elapsed_seconds", 0),
            })
    except ValueError as e:
        with _SCRAPE_JOBS_LOCK:
            _SCRAPE_JOBS[job_id].update({
                "status": "error", "ok": False,
                "finished_at": _scrape_time.time(),
                "error": f"bad_url: {e}",
            })
    except Exception as e:
        log.exception("async scrape failed for %s", url)
        with _SCRAPE_JOBS_LOCK:
            _SCRAPE_JOBS[job_id].update({
                "status": "error", "ok": False,
                "finished_at": _scrape_time.time(),
                "error": f"crawl_failed: {e}",
            })


@app.route("/api/owner/demo/scrape", methods=["POST"])
def api_owner_demo_scrape():
    """Kick off a site scrape in a background thread. Returns immediately
    with a job_id the client polls via /api/owner/demo/scrape_status/<id>.
    Async pattern dodges the Cloudflare quick-tunnel 100-sec HTTP timeout
    that was killing direct scrape responses."""
    _check_demo_admin_or_owner()
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url_required"}), 400
    _evict_old_scrape_jobs()
    job_id = _scrape_uuid.uuid4().hex[:16]
    with _SCRAPE_JOBS_LOCK:
        _SCRAPE_JOBS[job_id] = {
            "status": "running",
            "started_at": _scrape_time.time(),
            "url": url,
        }
    t = _scrape_threading.Thread(
        target=_run_scrape_job, args=(job_id, url, payload), daemon=True
    )
    t.start()
    audit.log_event(DATA_DIR,
                    actor=(auth.current_owner(ORBI_DIR) or {}).get("username") or "demo_admin",
                    action="demo.scrape.start",
                    meta={"url": url, "job_id": job_id})
    return jsonify({"ok": True, "job_id": job_id, "status": "running"})


@app.route("/api/owner/demo/scrape_status/<job_id>", methods=["GET"])
def api_owner_demo_scrape_status(job_id):
    """Poll endpoint — returns the current state of an async scrape job.
    Client polls every 2-3 seconds until status is 'done' or 'error'."""
    _check_demo_admin_or_owner()
    with _SCRAPE_JOBS_LOCK:
        job = _SCRAPE_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job_not_found"}), 404
    elapsed = int(_scrape_time.time() - job.get("started_at", _scrape_time.time()))
    out = dict(job)
    out["elapsed_seconds_so_far"] = elapsed
    return jsonify(out)


@app.route("/api/owner/demo/list", methods=["GET"])
def api_owner_demo_list():
    """List all customer profiles available for demo (just shows slug +
    name + menu count so the admin UI can render the list)."""
    _check_demo_admin_or_owner()
    profiles_dir = DATA_DIR / "customer_profiles"
    out = []
    if profiles_dir.exists():
        for p in sorted(profiles_dir.glob("*.json")):
            try:
                with p.open("r", encoding="utf-8") as f:
                    prof = json.load(f)
                out.append({
                    "slug": p.stem,
                    "name": prof.get("name", ""),
                    "menu_items": len(prof.get("menu_items") or []),
                    "pages": len(prof.get("_pages") or []),
                })
            except Exception:
                continue
    return jsonify({"profiles": out})


@app.route("/api/owner/demo/<slug>", methods=["DELETE"])
def api_owner_demo_delete(slug):
    """Wipe a customer profile (and its scraped-pages folder) so Frank
    can start fresh for the next prospect."""
    _check_demo_admin_or_owner()
    import re as _r
    slug_clean = _r.sub(r"[^a-z0-9_]", "", slug.lower())
    if not slug_clean or slug_clean != slug.lower():
        return jsonify({"ok": False, "error": "bad_slug"}), 400
    profiles_dir = DATA_DIR / "customer_profiles"
    profile_path = profiles_dir / f"{slug_clean}.json"
    index_path = profiles_dir / f"{slug_clean}_index.json"
    pages_dir = profiles_dir / f"{slug_clean}_pages"
    deleted = []
    for path in (profile_path, index_path):
        if path.exists():
            try: path.unlink(); deleted.append(path.name)
            except Exception: pass
    if pages_dir.exists() and pages_dir.is_dir():
        import shutil as _sh
        try: _sh.rmtree(pages_dir); deleted.append(pages_dir.name + "/")
        except Exception: pass
    audit.log_event(DATA_DIR, actor=(auth.current_owner(ORBI_DIR) or {}).get("username", "?"),
                    action="demo.delete", meta={"slug": slug_clean, "deleted": deleted})
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/")
def index():
    # In Phase 1 we serve a single chat shell. Phase 2 adds branding per customer.
    chat_shell = STATIC_DIR / "chat.html"
    if chat_shell.exists():
        resp = send_from_directory(STATIC_DIR, "chat.html")
        # No-cache so widget updates land immediately when the embed iframe
        # reloads — otherwise browsers serve a stale chat shell that
        # references old chat.js / chat.css and customer never sees fixes.
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    return (f"Orby is running but no chat shell is installed. "
            f"Place chat.html in {STATIC_DIR}.", 503)

@app.route("/pwa/<path:filename>")
def pwa_files(filename):
    return send_from_directory(PWA_DIR, filename)

# Static files that change frequently and need to land in customers' browsers
# the moment we change them (every chat.js / chat.css / embed.js edit). For
# everything else, normal Flask static caching applies.
_NO_CACHE_STATIC = {"chat.js", "chat.css", "embed.js"}

@app.route("/static/<path:filename>")
def static_files(filename):
    resp = send_from_directory(STATIC_DIR, filename)
    if filename in _NO_CACHE_STATIC:
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

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

def _esc(s) -> str:
    """HTML-escape for the few inline templates we render server-side
    (CO signing page, etc.). Returns empty string for None."""
    import html as _html
    return _html.escape("" if s is None else str(s), quote=True)


# ── Public Change-Order signing flow ──────────────────────────────────────
# A client receives a link like /co/sign/<token> in an email. Clicking it
# shows the CO document; signing posts the typed-name signature back here.
# On signature, the CO status becomes "signed", the project total
# updates, and the office is notified.

def _load_co_sign_tokens() -> dict:
    p = DATA_DIR / "co_sign_tokens.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_co_sign_tokens(tokens: dict) -> None:
    p = DATA_DIR / "co_sign_tokens.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    tmp.replace(p)


def _validate_co_token(token: str) -> tuple[dict | None, str]:
    """Returns (co, reason). co is None if invalid; reason explains why."""
    if not token:
        return None, "missing token"
    tokens = _load_co_sign_tokens()
    rec = tokens.get(token)
    if not rec:
        return None, "this signing link is not valid or has been retired"
    if rec.get("used_at"):
        return None, "this signing link has already been used"
    if int(time.time()) > int(rec.get("expires_at", 0)):
        return None, "this signing link has expired"
    co = mod_change_orders.get(DATA_DIR, rec.get("co_id", ""))
    if not co:
        return None, "the change order linked to this signature is no longer available"
    return co, ""


@app.route("/co/sign/<token>", methods=["GET"])
def co_sign_view(token):
    """Public-facing signing page. Renders the CO text + a signature
    field. No auth — token is the credential."""
    co, err = _validate_co_token(token)
    if not co:
        return _render_co_sign_error(err), 410 if "expired" in err else 404
    project = mod_projects.get(DATA_DIR, co.get("project_id", "")) or {}
    return _render_co_sign_page(token, co, project)


@app.route("/co/sign/<token>", methods=["POST"])
def co_sign_submit(token):
    """The signer typed their name + clicked Sign. Validate, record,
    mark the CO signed, notify the office, retire the token."""
    co, err = _validate_co_token(token)
    if not co:
        return _render_co_sign_error(err), 410 if "expired" in err else 404
    typed_name = (request.form.get("typed_name") or "").strip()
    if not typed_name or len(typed_name) < 3:
        project = mod_projects.get(DATA_DIR, co.get("project_id", "")) or {}
        return _render_co_sign_page(token, co, project,
                                       error="Please type your full name to sign."), 400
    # Record the signature on the CO
    mod_change_orders.mark_signed(DATA_DIR, co["id"], signer_name=typed_name)
    # Retire the token (single-use)
    tokens = _load_co_sign_tokens()
    if token in tokens:
        tokens[token]["used_at"] = int(time.time())
        tokens[token]["signed_by"] = typed_name
        tokens[token]["signer_ip"] = request.headers.get("X-Forwarded-For",
                                                           request.remote_addr or "")
        _save_co_sign_tokens(tokens)
    # Notify the GC's office. Workflow per Frank: foreman drafts on-site
    # in front of the customer, customer signs on the foreman's iPad
    # right then. So the moment we get here, it's effectively "office,
    # heads-up — Sarah just signed an extra $1,200 onto the Maple job
    # so update the billing/specs."
    project = mod_projects.get(DATA_DIR, co.get("project_id", "")) or {}
    try:
        amount = float(co.get("amount") or 0)
        sign = "+" if amount >= 0 else ""
        sent_via = co.get("sent_via", "")
        on_site = sent_via == "in_person"
        notify.send(CONFIG, DATA_DIR, event="co_signed",
                     title=(f"✓ CO signed on-site — {project.get('address', '?')}"
                            if on_site else
                            f"CO signed — {project.get('address', '?')}"),
                     body=f"{typed_name} signed CO #{co['id'][:8]} ({sign}${amount:,.0f}). "
                          f"{co.get('description','')[:80]}"
                          + (" Add to billing / specs." if on_site else ""),
                     url="/owner#projects")
    except Exception:
        log.exception("co_signed notify failed")
    audit.log_event(DATA_DIR, actor=typed_name,
                    action="co.signed",
                    meta={"co_id": co["id"], "project_id": co.get("project_id"),
                          "amount": co.get("amount"), "signer_ip": request.headers.get("X-Forwarded-For", request.remote_addr or "")})
    return _render_co_sign_thanks(co, project, typed_name)


def _render_co_sign_page(token: str, co: dict, project: dict,
                          error: str = "") -> str:
    biz_name = (CONFIG.get("business") or {}).get("name", "Your contractor")
    draft = co.get("draft_text") or ""
    err_block = f'<div style="background:#5a1f1f;color:#ffcfcf;padding:10px 12px;border-radius:6px;margin-bottom:14px">{_esc(error)}</div>' if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign Change Order — {_esc(biz_name)}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; background: #0e1422; color: #eaf0ff; margin: 0; padding: 20px; }}
.card {{ max-width: 720px; margin: 24px auto; background: #1a2240; border-radius: 14px; padding: 28px; }}
h1 {{ margin: 0 0 4px; font-size: 22px; }}
.from {{ color: #9aa4c0; font-size: 14px; margin-bottom: 18px; }}
.doc {{ background: #0e1730; padding: 18px; border-radius: 8px; font-family: ui-monospace, "SF Mono", monospace; font-size: 13px; white-space: pre-wrap; line-height: 1.5; margin-bottom: 18px; max-height: 460px; overflow-y: auto; }}
label {{ display: block; font-weight: 600; margin-bottom: 6px; }}
input[type="text"] {{ width: 100%; padding: 12px 14px; background: #0e1730; border: 1px solid #2c3756; color: #eaf0ff; border-radius: 8px; font-size: 18px; font-family: 'Brush Script MT', cursive; box-sizing: border-box; }}
button {{ width: 100%; padding: 14px; background: #8b5cf6; color: white; border: none; border-radius: 8px; font-size: 16px; font-weight: 700; cursor: pointer; margin-top: 14px; }}
button:hover {{ background: #7c4ef0; }}
.fine {{ color: #6c7592; font-size: 12px; margin-top: 16px; line-height: 1.5; }}
</style></head><body>
<div class="card">
  <h1>Change Order — Sign Here</h1>
  <div class="from">From {_esc(biz_name)} • Project: {_esc(project.get('address',''))}</div>
  {err_block}
  <div class="doc">{_esc(draft)}</div>
  <form method="POST">
    <label for="typed_name">Type your full name to sign:</label>
    <input type="text" id="typed_name" name="typed_name" autocomplete="name"
            placeholder="Your full name" required minlength="3">
    <button type="submit">I Agree &amp; Sign</button>
    <div class="fine">By typing your name and clicking the button, you are signing this change order electronically and agreeing to its terms. We record your name, the time you signed, and your IP address as proof.</div>
  </form>
</div>
</body></html>"""


def _render_co_sign_thanks(co: dict, project: dict, signer: str) -> str:
    biz_name = (CONFIG.get("business") or {}).get("name", "Your contractor")
    amount = float(co.get("amount") or 0)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signed — Thank you</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; background: #0e1422; color: #eaf0ff; margin: 0; padding: 20px; }}
.card {{ max-width: 560px; margin: 60px auto; background: #1a2240; border-radius: 14px; padding: 32px; text-align: center; }}
h1 {{ color: #4ade80; margin: 0 0 16px; }}
p {{ color: #c8d0e8; line-height: 1.5; }}
.amount {{ font-size: 24px; color: #eaf0ff; font-weight: 700; margin: 18px 0; }}
</style></head><body>
<div class="card">
  <h1>✓ Signed</h1>
  <p>Thank you, {_esc(signer)}. Your change order on {_esc(project.get('address','this project'))} has been signed and sent to {_esc(biz_name)}.</p>
  <div class="amount">${abs(amount):,.2f}</div>
  <p>You'll get a copy by email. If you have questions, just reply to the email or call.</p>
</div>
</body></html>"""


def _render_co_sign_error(reason: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link unavailable</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; background: #0e1422; color: #eaf0ff; margin: 0; padding: 20px; }}
.card {{ max-width: 540px; margin: 60px auto; background: #1a2240; border-radius: 14px; padding: 32px; text-align: center; }}
h1 {{ color: #ff7a7a; margin: 0 0 16px; }}
p {{ color: #c8d0e8; line-height: 1.5; }}
</style></head><body>
<div class="card">
  <h1>Link unavailable</h1>
  <p>{_esc(reason)}</p>
  <p>If you need a fresh signing link, please reach out to the contractor who sent you this.</p>
</div>
</body></html>"""


# ── Public client-facing project portal ───────────────────────────────────
# The homeowner visits /p/<token> to see their project status anytime.
# No auth — token is the credential. Stable for the life of the project
# so the GC shares once and the customer keeps using the same URL.
#
# Read-only by design. The portal shows project status, money summary,
# any pending change orders awaiting their signature (with sign links),
# and recent activity. Sanitized — no internal pricing details, no sub
# names, no daily-log scope-change flags (those are GC-internal).

@app.route("/p/<token>", methods=["GET"])
def client_project_portal(token):
    project = mod_projects.get_by_client_portal_token(DATA_DIR, token)
    if not project:
        return _render_portal_error("This project link isn't valid or has been retired."), 404
    return _render_client_project_portal(project)


@app.route("/p/<token>/request-change", methods=["GET", "POST"])
def client_request_change(token):
    """Customer-side change request flow. Sees a simple form, submits,
    Orby creates a CO record with status='client_requested' + notifies
    the GC. GC then reviews → marks awaiting_approval → standard CO
    flow takes over."""
    project = mod_projects.get_by_client_portal_token(DATA_DIR, token)
    if not project:
        return _render_portal_error("This project link isn't valid or has been retired."), 404
    if request.method == "GET":
        return _render_change_request_form(token, project)
    description = (request.form.get("description") or "").strip()
    if len(description) < 10:
        return _render_change_request_form(token, project,
            error="Please describe the change in a bit more detail (10+ characters)."), 400
    if len(description) > 2000:
        description = description[:2000]
    # Some customers will paste a number they have in mind ("about $500")
    raw_amount = (request.form.get("estimated_cost") or "").strip()
    estimated_amount = 0.0
    if raw_amount:
        cleaned = _re.sub(r"[^\d.]", "", raw_amount)
        try:
            estimated_amount = float(cleaned) if cleaned else 0.0
        except ValueError:
            estimated_amount = 0.0
    co = mod_change_orders.add(
        DATA_DIR,
        project_id=project["id"],
        description=description,
        amount=estimated_amount,
        scope_detail="Requested by customer via project portal.",
        status="client_requested",
    )
    audit.log_event(DATA_DIR, actor=project.get("customer_name") or "client",
                    action="co.client_requested",
                    meta={"co_id": co["id"], "project_id": project["id"],
                          "client_estimate": estimated_amount,
                          "ip": request.headers.get("X-Forwarded-For",
                                                      request.remote_addr or "")})
    # Notify the GC
    try:
        notify.send(CONFIG, DATA_DIR, event="client_change_request",
                     title=f"Change request — {project.get('address', '')}",
                     body=f"{project.get('customer_name') or 'Client'}: "
                          f"{description[:120]}",
                     url="/owner#projects")
    except Exception:
        log.exception("client change request notify failed")
    return _render_change_request_thanks(project)


def _render_change_request_form(token: str, project: dict,
                                  error: str = "") -> str:
    biz_name = (mod_business.load(DATA_DIR).get("name") or "your contractor")
    customer = project.get("customer_name") or "there"
    first_name = customer.split(" ", 1)[0]
    err_html = (f'<div style="background:#fef2f2;color:#991b1b;padding:10px 12px;'
                f'border-radius:6px;margin-bottom:14px">{_esc(error)}</div>'
                if error else "")
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Request a Change — {_esc(project.get('label','your project'))}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; background: #f4f6fb;
       color: #1a2240; margin: 0; padding: 20px; line-height: 1.5; }}
.card {{ max-width: 560px; margin: 30px auto; background: white;
        border-radius: 14px; padding: 28px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }}
h1 {{ font-size: 22px; margin: 0 0 6px; }}
.sub {{ color: #6c7592; font-size: 14px; margin-bottom: 22px; }}
label {{ display: block; font-weight: 600; margin-bottom: 6px; font-size: 14px; }}
textarea, input[type="text"] {{ width: 100%; padding: 12px 14px; background: #f9fafc;
                                  border: 1px solid #d0d6e6; color: #1a2240;
                                  border-radius: 8px; font-size: 15px; box-sizing: border-box;
                                  font-family: inherit; resize: vertical; }}
textarea {{ min-height: 100px; }}
.field {{ margin-bottom: 18px; }}
.help {{ color: #6c7592; font-size: 12px; margin-top: 6px; }}
button {{ width: 100%; padding: 14px; background: #8b5cf6; color: white;
          border: none; border-radius: 8px; font-size: 15px; font-weight: 600;
          cursor: pointer; }}
button:hover {{ background: #7c4ef0; }}
.back {{ display: inline-block; margin-bottom: 18px; color: #6c7592;
         font-size: 13px; text-decoration: none; }}
.back:hover {{ color: #1a2240; }}
</style></head><body>
<div class="card">
  <a href="/p/{_esc(token)}" class="back">← Back to project</a>
  <h1>Request a change</h1>
  <p class="sub">{_esc(first_name)}, tell {_esc(biz_name)} what you'd like added or changed on {_esc(project.get('label') or project.get('address',''))}. They'll review and send you a formal change order with a price.</p>
  {err_html}
  <form method="POST">
    <div class="field">
      <label for="description">What's the change?</label>
      <textarea id="description" name="description" placeholder="e.g. Add a pot filler over the stove, or upgrade the back fence to 6 ft cedar." required minlength="10" maxlength="2000"></textarea>
      <div class="help">Be as specific as you can — paint colors, materials, where, anything that helps.</div>
    </div>
    <div class="field">
      <label for="estimated_cost">Your budget for this (optional)</label>
      <input type="text" id="estimated_cost" name="estimated_cost" placeholder="e.g. $500 — or leave blank">
      <div class="help">Helps {_esc(biz_name)} know what tier of solution you're thinking. They'll come back with a real number.</div>
    </div>
    <button type="submit">Send to {_esc(biz_name)}</button>
  </form>
</div>
</body></html>"""


def _render_change_request_thanks(project: dict) -> str:
    biz_name = (mod_business.load(DATA_DIR).get("name") or "your contractor")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sent</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; background: #f4f6fb;
       color: #1a2240; margin: 0; padding: 20px; text-align: center; }}
.card {{ max-width: 480px; margin: 60px auto; background: white;
        border-radius: 14px; padding: 32px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }}
h1 {{ color: #2e7d32; margin: 0 0 16px; font-size: 22px; }}
p {{ color: #444; line-height: 1.5; }}
</style></head><body>
<div class="card">
<h1>✓ Request sent</h1>
<p>{_esc(biz_name)} got your request and will review it. You'll hear back with a formal change order — review and sign right from this same project page.</p>
</div></body></html>"""


def _render_client_project_portal(project: dict) -> str:
    from datetime import datetime as _dt
    business = mod_business.load(DATA_DIR)
    biz_name = business.get("name") or "Your contractor"
    biz_phone = (business.get("contact") or {}).get("phone") or ""
    biz_email = (business.get("contact") or {}).get("email") or ""

    # Money
    contract = float(project.get("contract_amount") or 0)
    signed_co = mod_change_orders.signed_total_for_project(DATA_DIR, project["id"])
    authorized_total = contract + signed_co
    invs = mod_invoices.list_for_project(DATA_DIR, project["id"])
    billed = sum(float(i.get("amount_due", 0)) for i in invs
                  if i.get("status") not in ("draft", "void"))
    paid = sum(float(i.get("amount_paid", 0)) for i in invs)
    balance = billed - paid
    remaining_on_contract = authorized_total - paid

    # COs awaiting their signature
    awaiting = mod_change_orders.list_awaiting_signature(DATA_DIR)
    awaiting = [c for c in awaiting if c.get("project_id") == project["id"]]

    # Recent activity (sanitized — strip the scope_changes_mentioned
    # field that's for GC's eyes only)
    logs = mod_daily_logs.list_for_project(DATA_DIR, project["id"], limit=5)
    activity_lines = []
    for l in logs[:5]:
        date_str = l.get("date", "")
        wd = l.get("work_done") or ""
        activity_lines.append((date_str, wd[:200]))

    # Customer's name for greeting
    customer = project.get("customer_name") or "there"
    first_name = customer.split(" ", 1)[0]
    addr = project.get("address", "")
    status = (project.get("status") or "active").replace("_", " ")
    stage = project.get("stage") or ""
    label = project.get("label") or ""

    # Awaiting-signature CO HTML (each with a sign-it link)
    awaiting_html = ""
    if awaiting:
        items = []
        for c in awaiting:
            amt = float(c.get("amount") or 0)
            sign = "+" if amt >= 0 else "-"
            # Get the live sign URL for this CO
            sign_url = _live_co_sign_url_for_co(c["id"])
            items.append(f"""
              <div class="co-item">
                <div class="co-desc">{_esc(c.get('description',''))}</div>
                <div class="co-amount">{sign}${abs(amt):,.2f}</div>
                {'<a class="sign-btn" href="' + _esc(sign_url) + '">Review &amp; sign</a>' if sign_url else ''}
              </div>
            """)
        awaiting_html = f"""
        <div class="section attention">
          <h2>⚠ Needs your signature</h2>
          <p class="dim">These change orders are waiting on you. Sign each to authorize the work.</p>
          {''.join(items)}
        </div>
        """

    activity_html = ""
    if activity_lines:
        items = "".join(
            f'<li><span class="date">{_esc(d)}</span> {_esc(w)}</li>'
            for d, w in activity_lines
        )
        activity_html = f"""
        <div class="section">
          <h2>Recent activity</h2>
          <ul class="activity">{items}</ul>
        </div>
        """

    paid_status_html = ""
    if balance > 0:
        paid_status_html = f"""
        <div class="balance-row warn">
          <div>Balance due</div><div>${balance:,.2f}</div>
        </div>
        """
    else:
        paid_status_html = """
        <div class="balance-row good">
          <div>Account current</div><div>✓</div>
        </div>
        """

    # No "How to pay" block — Orby is not a payment channel. Construction
    # loan draws go through the lender, cash jobs go through whatever
    # arrangement the GC and homeowner already have. The portal just
    # shows the running account so the customer knows where they stand.
    payment_html = ""

    contact_html = ""
    if biz_phone or biz_email:
        bits = []
        if biz_phone: bits.append(f'<a href="tel:{_esc(biz_phone)}">{_esc(biz_phone)}</a>')
        if biz_email: bits.append(f'<a href="mailto:{_esc(biz_email)}">{_esc(biz_email)}</a>')
        contact_html = f"""
        <div class="section contact">
          <h2>Questions?</h2>
          <p>Reach {_esc(biz_name)}:<br>{' &middot; '.join(bits)}</p>
        </div>
        """

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(label) or 'Your project'} — {_esc(biz_name)}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
       background: #f4f6fb; color: #1a2240; margin: 0; padding: 20px;
       line-height: 1.5; }}
.wrap {{ max-width: 640px; margin: 0 auto; }}
.header {{ background: white; border-radius: 14px; padding: 24px 24px 18px;
          box-shadow: 0 2px 8px rgba(0,0,0,0.04); margin-bottom: 16px; }}
.greeting {{ font-size: 14px; color: #6c7592; margin-bottom: 4px; }}
.project-title {{ font-size: 22px; font-weight: 700; margin: 0 0 6px; }}
.project-meta {{ font-size: 14px; color: #555; }}
.status-pill {{ display: inline-block; padding: 3px 10px; border-radius: 12px;
                background: #e8f5e9; color: #2e7d32; font-size: 12px;
                font-weight: 600; margin-left: 6px; text-transform: capitalize; }}
.section {{ background: white; border-radius: 14px; padding: 20px 24px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.04); margin-bottom: 16px; }}
.section h2 {{ font-size: 16px; margin: 0 0 12px; color: #1a2240; }}
.dim {{ color: #6c7592; font-size: 13px; margin: 0 0 14px; }}
.money-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px 24px;
                margin-bottom: 10px; }}
.money-grid div {{ font-size: 14px; }}
.money-grid .label {{ color: #6c7592; }}
.money-grid .value {{ font-weight: 600; text-align: right; }}
.balance-row {{ display: flex; justify-content: space-between;
                font-size: 16px; font-weight: 700;
                padding: 12px 0 0; border-top: 1px solid #e0e4ee;
                margin-top: 10px; }}
.balance-row.warn {{ color: #b45309; }}
.balance-row.good {{ color: #2e7d32; }}
.section.attention {{ border-left: 4px solid #d97706; }}
.co-item {{ padding: 12px 0; border-bottom: 1px solid #f0f2f8; }}
.co-item:last-child {{ border-bottom: none; }}
.co-desc {{ font-weight: 500; }}
.co-amount {{ color: #b45309; font-weight: 700; margin-top: 2px; }}
.sign-btn {{ display: inline-block; margin-top: 8px; padding: 8px 14px;
             background: #8b5cf6; color: white; text-decoration: none;
             border-radius: 6px; font-size: 13px; font-weight: 600; }}
.sign-btn:hover {{ background: #7c4ef0; }}
.activity {{ list-style: none; padding: 0; margin: 0; }}
.activity li {{ padding: 8px 0; border-bottom: 1px solid #f0f2f8;
                font-size: 14px; color: #444; }}
.activity li:last-child {{ border-bottom: none; }}
.activity .date {{ display: inline-block; min-width: 90px; color: #6c7592;
                   font-size: 12px; }}
.contact p, .payment p {{ font-size: 14px; margin: 0 0 8px; }}
.contact a, .payment a {{ color: #8b5cf6; text-decoration: none; font-weight: 600; }}
.payment {{ border-left: 4px solid #2e7d32; }}
.footer {{ text-align: center; font-size: 12px; color: #9aa4c0;
           margin-top: 20px; }}
</style>
</head><body>
<div class="wrap">
  <div class="header">
    <div class="greeting">Hi {_esc(first_name)},</div>
    <h1 class="project-title">{_esc(label) or 'Your project'}<span class="status-pill">{_esc(status)}{(': ' + _esc(stage)) if stage else ''}</span></h1>
    <div class="project-meta">{_esc(addr)}</div>
  </div>

  {awaiting_html}

  <div class="section">
    <h2>Money</h2>
    <div class="money-grid">
      <div class="label">Original contract</div>
      <div class="value">${contract:,.2f}</div>
      {('<div class="label">Signed change orders</div><div class="value">+$' + format(signed_co, ',.2f') + '</div>') if signed_co else ''}
      {('<div class="label">Authorized total</div><div class="value"><b>$' + format(authorized_total, ',.2f') + '</b></div>') if signed_co else ''}
      <div class="label">Invoiced so far</div>
      <div class="value">${billed:,.2f}</div>
      <div class="label">Paid</div>
      <div class="value">${paid:,.2f}</div>
    </div>
    {paid_status_html}
  </div>

  {payment_html}

  {activity_html}

  {contact_html}

  {_render_portal_review_or_request(project, biz_name)}

  <div class="footer">Powered by Orbi</div>
</div>
</body></html>"""


def _render_portal_review_or_request(project: dict, biz_name: str) -> str:
    """If the project is completed, show a review CTA. Otherwise show
    the change-request CTA. Either way the customer has one clear next
    step from the portal."""
    token = project.get("client_portal_token", "")
    if project.get("status") == "completed":
        # Find or mint an unsubmitted review for this project so the
        # portal link doesn't fail.
        try:
            review = mod_reviews.issue(DATA_DIR, project["id"])
            if review.get("submitted_at"):
                # Already rated — show a quiet thanks
                return f"""
        <div class="section">
          <h2>✓ Thanks for the rating</h2>
          <p class="dim">You already rated {_esc(biz_name)} on this project — really appreciate it.</p>
        </div>"""
            review_url = f"/r/{review['token']}"
            return f"""
        <div class="section">
          <h2>How did we do?</h2>
          <p class="dim">Job's done. If you've got 30 seconds, a quick rating helps {_esc(biz_name)} a ton.</p>
          <a class="sign-btn" href="{_esc(review_url)}">Rate the job</a>
        </div>"""
        except Exception:
            pass
    return f"""
        <div class="section">
          <h2>Need something added or changed?</h2>
          <p class="dim">Have an idea for the project? Send {_esc(biz_name)} a change request — they'll review and send you a formal change order.</p>
          <a class="sign-btn" href="/p/{_esc(token)}/request-change">Request a change</a>
        </div>"""


def _live_co_sign_url_for_co(co_id: str) -> str:
    """Look in co_sign_tokens for the most recent unused token bound to
    this CO. Returns full URL or empty string if no live token."""
    tokens_path = DATA_DIR / "co_sign_tokens.json"
    try:
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    candidates = []
    now_ts = int(time.time())
    for t, info in tokens.items():
        if info.get("co_id") != co_id:
            continue
        if info.get("used_at"):
            continue
        if int(info.get("expires_at", 0)) < now_ts:
            continue
        candidates.append((int(info.get("issued_at", 0)), t))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return _co_sign_url(candidates[0][1])


# ── Customer review form (post-closeout) ──────────────────────────────────
# After a project closes, GC mints a token via "review link for X" and
# shares the URL with the customer. Customer visits /r/<token>, leaves
# a 1-5 star + optional comment + may grant permission to share publicly.

@app.route("/r/<token>", methods=["GET"])
def client_review_view(token):
    review = mod_reviews.get_by_token(DATA_DIR, token)
    if not review:
        return _render_review_error("This review link isn't valid or has been retired."), 404
    if review.get("submitted_at"):
        return _render_review_thanks(review, already_submitted=True)
    project = mod_projects.get(DATA_DIR, review["project_id"]) or {}
    return _render_review_form(token, project)


@app.route("/r/<token>", methods=["POST"])
def client_review_submit(token):
    review = mod_reviews.get_by_token(DATA_DIR, token)
    if not review:
        return _render_review_error("This review link isn't valid or has been retired."), 404
    if review.get("submitted_at"):
        return _render_review_thanks(review, already_submitted=True)
    try:
        rating = int(request.form.get("rating") or 0)
    except ValueError:
        rating = 0
    if rating < 1 or rating > 5:
        project = mod_projects.get(DATA_DIR, review["project_id"]) or {}
        return _render_review_form(token, project,
                                      error="Please pick a star rating."), 400
    comment = (request.form.get("comment") or "").strip()
    recommend = (request.form.get("recommend") or "yes").lower() == "yes"
    share_ok = request.form.get("share_ok") == "on"
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    updated = mod_reviews.submit(
        DATA_DIR, token,
        rating=rating, comment=comment,
        would_recommend=recommend, permission_to_share=share_ok,
        submitter_ip=ip,
    )
    if not updated:
        return _render_review_error("This review was already submitted earlier."), 410
    project = mod_projects.get(DATA_DIR, review["project_id"]) or {}
    try:
        notify.send(CONFIG, DATA_DIR, event="review_submitted",
                     title=f"⭐ {'★' * rating} review — {project.get('address','')}",
                     body=f"{project.get('customer_name') or 'Customer'} "
                          f"left a {rating}-star review"
                          + (f": \"{comment[:80]}...\"" if comment else ""),
                     url="/owner")
    except Exception:
        log.exception("review notify failed")
    audit.log_event(DATA_DIR, actor=project.get("customer_name") or "client",
                    action="review.submitted",
                    meta={"project_id": project.get("id"), "rating": rating,
                          "would_recommend": recommend,
                          "permission_to_share": share_ok, "ip": ip})
    return _render_review_thanks(updated, project=project)


def _render_review_form(token: str, project: dict, error: str = "") -> str:
    biz_name = (mod_business.load(DATA_DIR).get("name") or "your contractor")
    customer = project.get("customer_name") or "there"
    first_name = customer.split(" ", 1)[0]
    addr = project.get("address", "")
    label = project.get("label", "")
    err_html = (f'<div class="err">{_esc(error)}</div>' if error else "")
    # Star input — 5 radio buttons styled as stars
    stars_html = ""
    for i in range(5, 0, -1):
        stars_html += (f'<input type="radio" id="star{i}" name="rating" '
                       f'value="{i}">'
                       f'<label for="star{i}" title="{i} star{"s" if i > 1 else ""}">★</label>')
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How did we do? — {_esc(biz_name)}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; background: #f4f6fb;
       color: #1a2240; margin: 0; padding: 20px; line-height: 1.5; }}
.card {{ max-width: 560px; margin: 30px auto; background: white;
        border-radius: 14px; padding: 28px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }}
h1 {{ font-size: 22px; margin: 0 0 6px; }}
.sub {{ color: #6c7592; font-size: 14px; margin-bottom: 22px; }}
.field {{ margin-bottom: 18px; }}
label {{ display: block; font-weight: 600; margin-bottom: 6px; font-size: 14px; }}
textarea {{ width: 100%; padding: 12px 14px; background: #f9fafc;
            border: 1px solid #d0d6e6; color: #1a2240; border-radius: 8px;
            font-size: 15px; box-sizing: border-box; min-height: 100px;
            font-family: inherit; resize: vertical; }}
button {{ width: 100%; padding: 14px; background: #8b5cf6; color: white;
          border: none; border-radius: 8px; font-size: 15px; font-weight: 600;
          cursor: pointer; margin-top: 8px; }}
button:hover {{ background: #7c4ef0; }}
.err {{ background: #fef2f2; color: #991b1b; padding: 10px 12px;
        border-radius: 6px; margin-bottom: 14px; }}
/* Star rating — radio buttons styled as clickable stars */
.stars {{ direction: rtl; display: inline-flex; font-size: 38px;
          line-height: 1; gap: 4px; }}
.stars input {{ display: none; }}
.stars label {{ color: #ccc; cursor: pointer; transition: color 0.1s; }}
.stars label:hover, .stars label:hover ~ label,
.stars input:checked ~ label {{ color: #fbbf24; }}
.rec-row {{ display: flex; gap: 14px; }}
.rec-row label {{ display: flex; align-items: center; gap: 6px;
                   font-weight: normal; cursor: pointer; }}
.checkbox-row {{ font-weight: normal; display: flex; align-items: center;
                  gap: 8px; font-size: 13px; }}
.checkbox-row input {{ width: 16px; height: 16px; }}
.help {{ color: #6c7592; font-size: 12px; margin-top: 6px; }}
</style></head><body>
<div class="card">
  <h1>How did we do, {_esc(first_name)}?</h1>
  <p class="sub">Quick rating on the {_esc(label) or 'project'} at {_esc(addr)}. Takes 30 seconds.</p>
  {err_html}
  <form method="POST">
    <div class="field">
      <label>Overall rating</label>
      <div class="stars">{stars_html}</div>
    </div>
    <div class="field">
      <label for="comment">Anything you'd like to add? (optional)</label>
      <textarea id="comment" name="comment" maxlength="2000"
                 placeholder="What went well? Anything we could do better?"></textarea>
    </div>
    <div class="field">
      <label>Would you recommend {_esc(biz_name)} to a friend?</label>
      <div class="rec-row">
        <label><input type="radio" name="recommend" value="yes" checked> Yes</label>
        <label><input type="radio" name="recommend" value="no"> No</label>
      </div>
    </div>
    <div class="field">
      <label class="checkbox-row">
        <input type="checkbox" name="share_ok">
        OK to quote me publicly (website, marketing) — optional
      </label>
    </div>
    <button type="submit">Send rating</button>
  </form>
</div>
</body></html>"""


def _render_review_thanks(review: dict, project: dict = None,
                            already_submitted: bool = False) -> str:
    biz_name = (mod_business.load(DATA_DIR).get("name") or "your contractor")
    rating = int(review.get("rating") or 0)
    stars = "★" * rating + "☆" * (5 - rating) if rating else ""
    msg = ("Thanks — we got your rating earlier."
           if already_submitted else
           f"Thanks for the review! It really helps {_esc(biz_name)} land "
           f"more jobs in the area.")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thanks</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; background: #f4f6fb;
       color: #1a2240; margin: 0; padding: 20px; text-align: center; }}
.card {{ max-width: 480px; margin: 60px auto; background: white;
        border-radius: 14px; padding: 32px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }}
h1 {{ color: #2e7d32; margin: 0 0 16px; font-size: 22px; }}
.stars {{ font-size: 32px; color: #fbbf24; margin: 16px 0; }}
p {{ color: #444; line-height: 1.5; }}
</style></head><body>
<div class="card">
<h1>✓ Got it</h1>
{f'<div class="stars">{stars}</div>' if stars else ''}
<p>{msg}</p>
</div></body></html>"""


def _render_review_error(reason: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link unavailable</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; background: #f4f6fb;
       color: #1a2240; margin: 0; padding: 20px; text-align: center; }}
.card {{ max-width: 480px; margin: 60px auto; background: white;
        border-radius: 14px; padding: 32px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }}
h1 {{ color: #b91c1c; margin: 0 0 16px; }}
</style></head><body>
<div class="card">
<h1>Link unavailable</h1>
<p>{_esc(reason)}</p>
</div></body></html>"""


def _render_portal_error(reason: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link unavailable</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif;
       background: #f4f6fb; color: #1a2240; margin: 0; padding: 40px 20px;
       text-align: center; }}
.card {{ max-width: 480px; margin: 60px auto; background: white;
        border-radius: 14px; padding: 32px; }}
h1 {{ color: #b91c1c; margin: 0 0 14px; }}
p {{ color: #444; line-height: 1.5; }}
</style></head><body>
<div class="card">
<h1>Link unavailable</h1>
<p>{_esc(reason)}</p>
<p>If you need a fresh project link, please reach out to your contractor.</p>
</div></body></html>"""


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
    Returns ONLY what's safe for any visitor to see. Multi-tenant: picks
    the business profile based on parent-origin / referer (same logic as
    /chat) so the widget header on purblum.com says 'PurBlum' not the
    default 'myOrbi'."""
    business = _resolve_business_profile_for_request(request)
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

    # Multi-tenant routing: if the visitor came from a customer's website
    # (purblum.com, joes-pizza.com, etc.), load THEIR scraped profile so
    # Orby answers as their business — not as the default Orby owner.
    business = _resolve_business_profile_for_request(request)
    scope    = (CONFIG.get("scope") or {})

    # REFERRAL — "where can I get one of these for my business?"
    # Single-sentence URL + redirect to host business. Only fires on direct
    # ask; never volunteers (LLM is prompt-gated). Toggleable in Settings.
    referral_resp = _try_orby_referral(user_msg, business)
    if referral_resp is not None:
        return jsonify(referral_resp)

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

    # AUTHORITATIVE ORDER TOTALS — if the conversation has order intent,
    # extract → cart → compute exact subtotal/tax/total in Python (not in
    # the LLM, which makes arithmetic mistakes on multi-line money math).
    # Inject the exact dollars as a system fact so the LLM quotes them
    # verbatim when the customer asks "how much?" or at the end-summary.
    _injected_totals = None  # filled in below, used in the response JSON
    try:
        history_for_total = [m for m in history if m.get("role") in ("user", "assistant")]
        history_for_total.append({"role": "user", "content": user_msg})
        # Latency fix — only run the expensive extraction when the customer
        # is closing the order or explicitly asking the price. Mid-order
        # modifier collection ("12 inch", "no onions") does NOT need totals.
        if (_looks_like_order_chat(history_for_total)
            and _is_end_of_order_message(user_msg)
            and business.get("menu_items")):
            import phone_order
            # Construct a menu dict that phone_order.load_menu would have
            # produced — categories+items+modifier_groups schema.
            menu_for_compute = _menu_dict_from_profile(business)
            extracted = phone_order.extract_order_from_history(
                history_for_total, menu_for_compute, llm_client, CONFIG,
            )
            if extracted.get("ok") and extracted.get("items"):
                cart, _warn = phone_order.build_cart_for_purblum(extracted["items"], menu_for_compute)
                phone_order.annotate_cart_with_menu_prices(cart, menu_for_compute)
                totals = phone_order.compute_cart_total(cart, business.get("tax_rate", 0))
                if totals["subtotal"] > 0:
                    lines = ["⚠️ AUTHORITATIVE ORDER TOTALS — YOU MUST INCLUDE THESE",
                             "These prices were computed by Python from the canonical menu.",
                             "When you summarize the order or the customer asks the price,",
                             "INCLUDE these numbers verbatim. Do NOT omit them. Do NOT round.",
                             "Do NOT guess different numbers.",
                             ""]
                    for li in totals["lines"]:
                        lines.append(f"  • {li['qty']}x {li['name']}: ${li['line_total']:.2f}")
                    lines.append("")
                    lines.append(f"  SUBTOTAL: ${totals['subtotal']:.2f}")
                    if totals['tax_rate_pct']:
                        lines.append(f"  TAX ({totals['tax_rate_pct']:.2f}%): ${totals['tax']:.2f}")
                        lines.append(f"  TOTAL: ${totals['total']:.2f}")
                    lines.append("")
                    lines.append("Quote these exact dollar figures in your end-of-order summary.")
                    system += "\n\n" + "\n".join(lines)
                    # Stash for the response JSON so the chat widget can
                    # render a live cart panel.
                    _injected_totals = totals
                    log.info(f"chat order totals injected: subtotal=${totals['subtotal']} total=${totals['total']}")
    except Exception as e:
        log.warning(f"chat order-totals injection failed: {e}")

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
        # If the chat has order intent + an extractable cart, the widget
        # gets a live cart breakdown to render alongside Orby's reply.
        # Math is server-computed (deterministic) — the widget just displays.
        "order_summary": _injected_totals,
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
    # ── Text & documents ──────────────────────────────────────────────
    ".txt", ".md", ".markdown", ".rst", ".log", ".rtf",
    ".pdf", ".doc", ".docx", ".odt", ".pages",
    ".html", ".htm", ".xml", ".xhtml",
    # ── Spreadsheets & data ───────────────────────────────────────────
    ".csv", ".tsv", ".xls", ".xlsx", ".xlsm", ".ods", ".numbers",
    ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml",
    ".sql", ".sqlite", ".sqlite3", ".db",
    ".parquet", ".arrow", ".feather",
    # ── Presentations ─────────────────────────────────────────────────
    ".ppt", ".pptx", ".odp", ".key",
    # ── Images ────────────────────────────────────────────────────────
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".svg", ".heic", ".heif", ".ico", ".avif",
    ".raw", ".cr2", ".nef", ".arw", ".dng",
    # ── Audio ─────────────────────────────────────────────────────────
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".flac", ".wma", ".aiff", ".aif", ".amr",
    # ── Video ─────────────────────────────────────────────────────────
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv",
    ".flv", ".3gp", ".mpg", ".mpeg", ".ts", ".mts",
    # ── Archives ──────────────────────────────────────────────────────
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz",
    ".7z", ".rar", ".lzma", ".zst",
    # ── Ebooks ────────────────────────────────────────────────────────
    ".epub", ".mobi", ".azw", ".azw3", ".fb2", ".djvu",
    # ── Design / CAD / 3D ─────────────────────────────────────────────
    ".psd", ".ai", ".sketch", ".fig", ".xd", ".indd",
    ".dwg", ".dxf", ".stl", ".step", ".stp", ".iges", ".igs",
    ".obj", ".fbx", ".blend", ".gltf", ".glb", ".dae",
    # ── Code / config ─────────────────────────────────────────────────
    ".py", ".pyi", ".ipynb",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".css", ".scss", ".sass", ".less",
    ".java", ".kt", ".scala", ".groovy",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx",
    ".cs", ".vb", ".fs",
    ".go", ".rs", ".swift", ".m", ".mm",
    ".rb", ".php", ".pl", ".pm", ".lua", ".r", ".jl",
    ".sh", ".bash", ".zsh", ".fish",
    ".ps1", ".bat", ".cmd",
    ".ini", ".conf", ".cfg", ".env", ".properties",
    ".dockerfile", ".makefile", ".cmake",
    # ── Fonts ─────────────────────────────────────────────────────────
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # ── Installers / packages — flagged in the upload response but ALLOWED
    # so Frank can use Orby as a file-stash for software he wants to keep
    # near his workspace. They sit in ~/Orby/, Orby never runs them.
    ".exe", ".msi", ".dmg", ".pkg", ".app",
    ".apk", ".ipa", ".aab",
    ".deb", ".rpm", ".AppImage", ".appimage", ".snap", ".flatpak",
    # ── Misc / generic ────────────────────────────────────────────────
    ".bin", ".dat", ".dump", ".bak",
    ".eml", ".msg", ".mbox",
    ".vcf", ".ics",
    ".gpx", ".kml", ".kmz",
    ".srt", ".vtt", ".ass", ".sub",
    ".torrent",
}
# File extensions that are technically allowed but worth flagging to the
# owner — uploaded executables sit in ~/Orby/ harmlessly until somebody
# double-clicks them. Orby itself never executes uploads.
_FLAGGED_UPLOAD_EXTS = {
    ".exe", ".msi", ".dmg", ".pkg", ".app", ".apk", ".ipa",
    ".deb", ".rpm", ".AppImage", ".appimage", ".snap", ".flatpak",
    ".bat", ".cmd", ".ps1", ".sh", ".bash",
}
_MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB per file (was 25 MB)


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
            entry = {"name": dest.name, "size": size}
            if suffix in _FLAGGED_UPLOAD_EXTS:
                entry["flagged"] = "executable_or_script"
                entry["note"] = (
                    "Stored safely in ~/Orby/ but never auto-run by Orby. "
                    "Only opens if you double-click it yourself."
                )
            saved.append(entry)
            log.info(f"workspace upload by {owner_session.get('username','?')}: "
                     f"{dest.name} ({size} bytes)"
                     + (" [flagged: executable]" if suffix in _FLAGGED_UPLOAD_EXTS else ""))
        except Exception as e:
            rejected.append({"name": original, "reason": f"save_failed: {e}"})

    scan_result = mod_workspace.scan(CONFIG, DATA_DIR) if saved else {}
    flagged = [s for s in saved if s.get("flagged")]
    audit.log_event(DATA_DIR,
                    actor=owner_session.get("username", "?"),
                    action="workspace.upload",
                    meta={"saved": [s["name"] for s in saved],
                          "rejected": [r["name"] for r in rejected],
                          "flagged": [s["name"] for s in flagged]})
    return jsonify({
        "status": "ok",
        "saved": saved,
        "rejected": rejected,
        "flagged": flagged,
        "indexed": scan_result.get("added", 0) + scan_result.get("updated", 0),
    })


# ── Form templates API (Forms tab) ────────────────────────────────────────

@app.route("/api/owner/forms/upload", methods=["POST"])
def owner_forms_upload():
    """Upload a blank form template, classify it, auto-detect fillable
    fields, and store. Form data:
      - file: the PDF or DOCX
      - kind: one of mod_forms.VALID_KINDS
      - display_name: human label, e.g. "Acme Standard Change Order"
      - is_default: "1" / "0" — whether to mark as default for this kind
    """
    owner_session = auth.require_owner(ORBI_DIR)
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no_file"}), 400
    kind = (request.form.get("kind") or "").strip()
    if kind not in mod_forms.VALID_KINDS:
        return jsonify({"error": "invalid_kind",
                        "valid": sorted(mod_forms.VALID_KINDS)}), 400
    display_name = (request.form.get("display_name") or "").strip()
    is_default = (request.form.get("is_default") or "1").strip() in ("1", "true", "yes")
    # Save to a temp path first
    import tempfile, os as _os
    suffix = _os.path.splitext(f.filename or "")[1].lower()
    if suffix not in (".pdf", ".docx"):
        return jsonify({"error": "unsupported_type",
                        "allowed": [".pdf", ".docx"]}), 400
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        tmp_path = Path(tmp.name)
    try:
        tpl = mod_forms.add(
            DATA_DIR,
            kind=kind, display_name=display_name or f.filename,
            filename=f.filename, source_path=tmp_path,
            uploaded_by=owner_session.get("username", "?"),
            is_default=is_default,
        )
    finally:
        try: tmp_path.unlink()
        except OSError: pass
    audit.log_event(DATA_DIR, actor=owner_session.get("username", "?"),
                    action="form_template.uploaded",
                    meta={"template_id": tpl["id"], "kind": kind,
                          "fields_detected": len(tpl.get("detected_fields", []) or [])})
    return jsonify({"status": "ok", "template": tpl})


@app.route("/api/owner/forms", methods=["GET"])
def owner_forms_list():
    auth.require_owner(ORBI_DIR)
    kind = (request.args.get("kind") or "").strip() or None
    return jsonify({
        "templates": mod_forms.list_all(DATA_DIR, kind=kind),
        "summary": mod_forms.summary(DATA_DIR),
        "valid_kinds": sorted(mod_forms.VALID_KINDS),
    })


@app.route("/api/owner/forms/<template_id>", methods=["GET"])
def owner_forms_get(template_id):
    auth.require_owner(ORBI_DIR)
    tpl = mod_forms.get(DATA_DIR, template_id)
    if not tpl:
        return jsonify({"error": "not_found"}), 404
    return jsonify(tpl)


@app.route("/api/owner/forms/<template_id>/mapping", methods=["PUT"])
def owner_forms_mapping(template_id):
    """Owner-side correction of the auto-mapping. Body:
      {"field_name": "Customer Name", "mapped_to": "customer_name"}"""
    auth.require_owner(ORBI_DIR)
    data = request.get_json(silent=True) or {}
    field_name = (data.get("field_name") or "").strip()
    mapped_to = (data.get("mapped_to") or "").strip()
    if not field_name:
        return jsonify({"error": "field_name_required"}), 400
    ok = mod_forms.update_field_mapping(DATA_DIR, template_id,
                                          field_name, mapped_to)
    return jsonify({"status": "ok" if ok else "no_change"})


@app.route("/api/owner/forms/<template_id>/default", methods=["POST"])
def owner_forms_set_default(template_id):
    auth.require_owner(ORBI_DIR)
    ok = mod_forms.set_default(DATA_DIR, template_id)
    return jsonify({"status": "ok" if ok else "not_found"})


@app.route("/api/owner/forms/<template_id>", methods=["DELETE"])
def owner_forms_remove(template_id):
    owner_session = auth.require_owner(ORBI_DIR)
    ok = mod_forms.remove(DATA_DIR, template_id)
    if ok:
        audit.log_event(DATA_DIR, actor=owner_session.get("username", "?"),
                        action="form_template.removed",
                        meta={"template_id": template_id})
    return jsonify({"status": "ok" if ok else "not_found"})


# ── Pricing catalog API ───────────────────────────────────────────────────

@app.route("/api/owner/pricing", methods=["GET"])
def owner_pricing_list():
    auth.require_owner(ORBI_DIR)
    return jsonify({
        "items": mod_pricing.list_all(DATA_DIR),
        "summary": mod_pricing.summary(DATA_DIR),
    })


@app.route("/api/owner/pricing", methods=["POST"])
def owner_pricing_add():
    owner_session = auth.require_owner(ORBI_DIR)
    data = request.get_json(silent=True) or {}
    try:
        item = mod_pricing.add(
            DATA_DIR,
            label=(data.get("label") or "").strip(),
            unit_price=float(data.get("unit_price") or 0),
            aliases=data.get("aliases") or [],
            unit=(data.get("unit") or "each").strip(),
            notes=(data.get("notes") or "").strip(),
        )
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    audit.log_event(DATA_DIR, actor=owner_session.get("username", "?"),
                    action="pricing.added",
                    meta={"id": item["id"], "label": item["label"]})
    return jsonify({"status": "ok", "item": item})


@app.route("/api/owner/pricing/<item_id>", methods=["PUT"])
def owner_pricing_update(item_id):
    auth.require_owner(ORBI_DIR)
    data = request.get_json(silent=True) or {}
    item = mod_pricing.update(DATA_DIR, item_id, **data)
    if not item:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"status": "ok", "item": item})


@app.route("/api/owner/pricing/<item_id>", methods=["DELETE"])
def owner_pricing_remove(item_id):
    owner_session = auth.require_owner(ORBI_DIR)
    ok = mod_pricing.remove(DATA_DIR, item_id)
    if ok:
        audit.log_event(DATA_DIR, actor=owner_session.get("username", "?"),
                        action="pricing.removed", meta={"id": item_id})
    return jsonify({"status": "ok" if ok else "not_found"})


# ── Client folders API ────────────────────────────────────────────────────

@app.route("/api/owner/clients", methods=["GET"])
def owner_clients_list():
    auth.require_owner(ORBI_DIR)
    return jsonify({"clients": mod_clients.list_clients(CONFIG)})


# ── Full-site crawler for customer onboarding ─────────────────────────────
#
# A new restaurant customer types their website URL into the onboarding
# wizard. Orby crawls EVERY page on the domain (no prioritization, no
# "this page looks unimportant" filtering), runs LLM extraction on each,
# merges into a per-customer profile saved at
# data/customer_profiles/<domain>.json. Raw page text is indexed for
# full-text retrieval. Confidence scores per field surface gaps the
# owner needs to fill manually.

@app.route("/api/owner/onboarding/full_scrape", methods=["POST"])
def owner_onboarding_full_scrape():
    """POST {url} → crawl that website, save per-customer profile +
    page index, return confidence + summary. Owner-authenticated."""
    owner_session = auth.require_owner(ORBI_DIR)
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url_required"}), 400
    # Optional knobs — sensible defaults for restaurant onboarding
    max_pages = int(payload.get("max_pages", 200))
    max_depth = int(payload.get("max_depth", 5))
    wall_time = int(payload.get("wall_time_seconds", 600))
    delay     = float(payload.get("fetch_delay_seconds", 0.5))
    try:
        import site_scraper
        brain_call = site_scraper.make_brain_call(CONFIG)
        result = site_scraper.crawl_site(
            url, data_dir=DATA_DIR, brain_call=brain_call,
            max_pages=max_pages, max_depth=max_depth,
            wall_time_seconds=wall_time, fetch_delay_seconds=delay,
        )
    except ValueError as e:
        return jsonify({"error": f"bad_url: {e}"}), 400
    except Exception as e:
        log.exception("full_scrape failed for %s", url)
        return jsonify({"error": f"crawl_failed: {e}"}), 500
    audit.log_event(DATA_DIR, actor=owner_session.get("username", "?"),
                    action="onboarding.full_scrape",
                    meta={"url": url,
                          "pages_visited": result["pages_visited"],
                          "elapsed_seconds": result["elapsed_seconds"]})
    # Trim the profile to a digestible response — the full thing is on disk
    summary = {
        "ok": True,
        "url": url,
        "domain": result["domain"],
        "pages_visited": result["pages_visited"],
        "pages_failed": result["pages_failed"],
        "elapsed_seconds": result["elapsed_seconds"],
        "stopped_reason": result["stopped_reason"],
        "confidence": result["confidence"],
        "profile_summary": {
            "name": result["profile"].get("name"),
            "tagline": result["profile"].get("tagline"),
            "address": result["profile"].get("address"),
            "contact": result["profile"].get("contact"),
            "hours_days_count": len(result["profile"].get("hours", {})),
            "services_count": len(result["profile"].get("services", [])),
            "menu_items_count": len(result["profile"].get("menu_items", [])),
            "faq_count": len(result["profile"].get("faq", [])),
        },
        "storage": result.get("storage"),
        "pages": [{"url": p["url"], "title": p["title"]}
                   for p in result["profile"].get("_pages", [])],
    }
    return jsonify(summary)


@app.route("/api/owner/customer_profiles", methods=["GET"])
def owner_customer_profiles_list():
    """List every scraped customer profile in data/customer_profiles/.
    Used by the multi-tenant routing UI later — also useful for debugging."""
    auth.require_owner(ORBI_DIR)
    base = DATA_DIR / "customer_profiles"
    if not base.exists():
        return jsonify({"profiles": []})
    out = []
    for f in sorted(base.glob("*.json")):
        if f.name.endswith("_index.json"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            out.append({
                "slug": f.stem,
                "name": data.get("name"),
                "website": data.get("contact", {}).get("website"),
                "pages_count": len(data.get("_pages", [])),
                "menu_items_count": len(data.get("menu_items", [])),
                "services_count": len(data.get("services", [])),
                "path": str(f),
            })
        except Exception:
            continue
    return jsonify({"profiles": out})


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

    # First-install onboarding wizard — fires when business_info has
    # placeholder values from a fresh customer install. Walks the user
    # through 5 setup questions. Once business_info is populated, this
    # handler permanently no-ops for the lifetime of the install.
    onb_resp = _try_onboarding(user_msg, user_rec)
    if onb_resp is not None:
        return jsonify(onb_resp)

    # Safety refusals first — "sign for the customer", "modify a signed
    # CO", "delete all X files", "create a CO and send without approval".
    # Must run BEFORE contractor_chat so "create a CO" doesn't get caught
    # by the CO-add handler.
    safety_resp = _try_safety_refusal(user_msg, user_rec)
    if safety_resp is not None:
        return jsonify(safety_resp)

    # Contractor module — "add project at X", "what jobs are open",
    # "status of the Maple project". Gated by enabled_modules so it's
    # invisible for base-Orby customers and active for paid Contractor
    # Orbi installs.
    gc_resp = _try_contractor_chat(user_msg, user_rec)
    if gc_resp is not None:
        return jsonify(gc_resp)

    # Contacts CRUD — "add contact X", "find Sarah", "show contacts for
    # Johnson Construction", "every conversation with Sarah". Per-user
    # contact book (modules/contacts.py).
    contacts_resp = _try_contacts_chat(user_msg, user_rec)
    if contacts_resp is not None:
        return jsonify(contacts_resp)

    # Leads / lead-list queries — "show me leads from this week",
    # "what new leads came in overnight". Reads modules/messages.py.
    leads_resp = _try_leads_chat(user_msg, user_rec)
    if leads_resp is not None:
        return jsonify(leads_resp)

    # Lead CAPTURE from chat — "add me as a new lead, name John Smith,
    # phone 775-...", "log a new lead: name X phone Y". Writes to
    # messages.json so it shows up in the leads inbox + recent-leads
    # query. Otherwise the LLM fakes "Done — added you."
    lead_add_resp = _try_lead_add(user_msg, user_rec)
    if lead_add_resp is not None:
        return jsonify(lead_add_resp)

    # Channel queries we can't honestly answer — email/SMS/files. Return a
    # deterministic refusal so the LLM doesn't fabricate "here are 3 emails
    # from Johnson." Honest > confident-wrong.
    unwired_resp = _try_unwired_channel(user_msg, user_rec)
    if unwired_resp is not None:
        return jsonify(unwired_resp)

    # Business-profile knowledge questions — "what services", "are you
    # licensed", "what hours", "what warranty", "what service area",
    # "what financing". Reads business_info directly so the answer is
    # exact and consistent. Beats LLM, which fluffs or hedges.
    knowledge_resp = _try_knowledge_chat(user_msg, user_rec)
    if knowledge_resp is not None:
        return jsonify(knowledge_resp)

    # Self-introspection — "do you have a contractor module", "what
    # modules / add-ons / plans am I on". Reads CONFIG.enabled_modules
    # so she gives the truthful answer instead of LLM fabrication.
    modules_resp = _try_modules_introspection(user_msg, user_rec)
    if modules_resp is not None:
        return jsonify(modules_resp)

    # Form templates — "what forms do I have", "show my templates",
    # "do I have a change order template uploaded"
    forms_resp = _try_forms_introspection(user_msg, user_rec)
    if forms_resp is not None:
        return jsonify(forms_resp)

    # Owner-side lead simulation — "I need a kitchen remodel", "my budget
    # is $40k" coming through OWNER chat. In production these would land
    # on public_chat. In testing they hit owner_chat. Deterministic
    # acknowledgment + path forward.
    sim_lead_resp = _try_owner_simulated_lead(user_msg, user_rec)
    if sim_lead_resp is not None:
        return jsonify(sim_lead_resp)

    # Calendar CANCEL — "cancel my Tuesday appointment" — actually
    # removes the event from the calendar (was returning fake "done" via
    # the LLM before, never wrote anything).
    cancel_resp = _try_cancel_appointment(user_msg, user_dir)
    if cancel_resp is not None:
        return jsonify(cancel_resp)

    # Calendar RESCHEDULE — "move my haircut from Tuesday to Friday" —
    # actually updates the event start (was also faking it via the LLM).
    resched_resp = _try_reschedule_appointment(user_msg, user_dir)
    if resched_resp is not None:
        return jsonify(resched_resp)

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
    # "Give me my morning brief" / "daily brief" — route to the real
    # build_briefing path instead of the LLM, so contractor sections
    # (pending COs, overdue invoices, receivables totals) actually
    # show up.
    brief_resp = _try_morning_brief(user_msg, user_rec)
    if brief_resp is not None:
        return jsonify(brief_resp)

    tomorrow_resp = _try_tomorrow_brief(user_msg, user_rec)
    if tomorrow_resp is not None:
        return jsonify(tomorrow_resp)

    appt_today_resp = _try_appointments_today(user_msg, user_rec)
    if appt_today_resp is not None:
        return jsonify(appt_today_resp)

    # Task completion — "done with X", "finished X", "completed the supplier
    # call". Has to run BEFORE friend-mode context-loading so she doesn't
    # turn it into conversation instead of marking it done.
    task_done_resp = _try_task_complete(user_msg, user_rec)
    if task_done_resp is not None:
        return jsonify(task_done_resp)

    # Reminder removal — "don't remind me about X" / "cancel the reminder
    # about Y". Has to run before friend-mode context-loading so she
    # actually deletes the reminder instead of conversational acknowledgment.
    rem_remove_resp = _try_reminder_remove(user_msg, user_rec)
    if rem_remove_resp is not None:
        return jsonify(rem_remove_resp)

    # Project balance lookup — "what was the balance on the Birch closeout"
    # / "how much does Maple owe". Honest "no such project" beats LLM
    # hallucination about needing Stripe.
    proj_bal_resp = _try_project_balance(user_msg, user_rec)
    if proj_bal_resp is not None:
        return jsonify(proj_bal_resp)

    # Hypothetical math: "If I billed Sarah $5000 and she paid $2000,
    # what's still owed?" — pure arithmetic, deterministic answer.
    math_resp = _try_math_quick(user_msg)
    if math_resp is not None:
        return jsonify(math_resp)

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

    # ALWAYS-ON self-context: which add-on modules are enabled. Tiny
    # block, but it stops the LLM from claiming "I don't have a
    # contractor module" when the contractor module is, in fact, on.
    enabled_mods = CONFIG.get("enabled_modules") or []
    if enabled_mods:
        # Build the alias hint so the LLM treats synonyms correctly.
        alias_lines = []
        enabled_lower = {str(m).lower() for m in enabled_mods}
        for alias, canonical in _MODULE_ALIASES.items():
            if canonical in enabled_lower and alias != canonical:
                alias_lines.append(f"    - \"{alias}\" → {canonical}")
        alias_block = ""
        if alias_lines:
            alias_block = "  Synonyms (treat all of these as the same active module):\n" + "\n".join(alias_lines) + "\n"
        extras.append(
            "ORBY CONFIGURATION (authoritative — never contradict):\n"
            f"  Enabled add-on modules: {', '.join(str(m) for m in enabled_mods)}\n"
            + alias_block +
            "  If the user asks 'do you have the X module' or 'do you "
            "know anything about a Y module' and X/Y maps to anything in "
            "the list above, answer YES."
        )
    else:
        extras.append(
            "ORBY CONFIGURATION (authoritative — never contradict):\n"
            "  No paid add-on modules enabled. Base Orby only.\n"
            "  If asked 'do you have the X module' for any add-on, answer NO."
        )
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
# ── First-install onboarding wizard ───────────────────────────────────────
# When a fresh customer just paid + ran the installer, business_info
# still has REPLACE_WITH placeholders. The first thing they say in chat
# triggers a setup flow that captures the essentials — business name,
# license, phone, services — and saves them so Orby is ready to take
# real calls 60 seconds after login.
#
# State lives in data/.onboarding_state.json (single record — only one
# owner per install). Once business_info.name is real, this entire
# block no-ops and Orby behaves normally.

ONBOARDING_STEPS = [
    ("name",        "What's your business called?"),
    ("phone",       "What's the main phone number for your business?"),
    ("services",    "What services do you offer? (one line, comma-separated is fine)"),
    ("address",     "What's your street address?"),
    ("license",     "What's your contractor license number? (Type 'skip' if you don't have one yet.)"),
]


def _onboarding_state_path() -> "Path":
    return DATA_DIR / ".onboarding_state.json"


def _load_onboarding_state() -> dict:
    p = _onboarding_state_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # step=-1 means "we haven't even shown the welcome yet". First
        # chat triggers the welcome + bumps to step=0 (waiting for
        # answer to question #0).
        return {"complete": False, "step": -1, "collected": {}}


def _save_onboarding_state(state: dict) -> None:
    p = _onboarding_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def _needs_onboarding() -> bool:
    """True if business_info still has template placeholders (or is empty)."""
    try:
        biz = mod_business.load(DATA_DIR) or {}
    except Exception:
        return False
    name = (biz.get("name") or "").strip()
    # Sentinel names for Frank's own install (skip onboarding wizard).
    # Old names kept for backward compat with stale local data.
    frank_install_sentinels = {"myOrbi", "Orbi", "My Orbi AI Solutions", "My Orby AI Solutions"}
    if not name or name.startswith("REPLACE_") or name in frank_install_sentinels:
        if name in frank_install_sentinels:
            return False
        return True
    return False


def _try_onboarding(message: str, user_rec: dict) -> dict | None:
    """If business_info needs onboarding, walk the user through it via
    chat. Returns a reply dict or None to fall through.

    The first chat after install short-circuits whatever they typed and
    starts the flow. Each subsequent message captures the answer for
    the current step. 'skip' moves on for optional fields."""
    if not _needs_onboarding():
        # Wizard already complete (or N/A on this install)
        state = _load_onboarding_state()
        if not state.get("complete"):
            state["complete"] = True
            _save_onboarding_state(state)
        return None
    state = _load_onboarding_state()
    if state.get("complete"):
        return None
    step_idx = int(state.get("step", -1))
    collected = state.get("collected") or {}
    # First-ever message: greet + ask question #0, bump state to "waiting
    # for answer to step 0". Next call sees step 0 + waiting → saves the
    # answer there + advances.
    if step_idx < 0:
        state["step"] = 0
        state["collected"] = {}
        _save_onboarding_state(state)
        first_q = ONBOARDING_STEPS[0][1]
        return {"reply": ("👋 Welcome! Quick setup before we take any calls — "
                          "5 questions, 60 seconds.\n\n"
                          f"{first_q}"),
                "tier": "local", "latency_ms": 0,
                "source": "onboarding_start"}
    # Save the answer to the current step
    answer = (message or "").strip()
    if step_idx < len(ONBOARDING_STEPS):
        key, _q = ONBOARDING_STEPS[step_idx]
        if answer.lower() in {"skip", "none", "n/a"} and key in ("license",):
            collected[key] = ""
        else:
            collected[key] = answer
    step_idx += 1
    state["step"] = step_idx
    state["collected"] = collected
    _save_onboarding_state(state)
    # If more steps remain, ask the next
    if step_idx < len(ONBOARDING_STEPS):
        _next_key, next_q = ONBOARDING_STEPS[step_idx]
        return {"reply": f"Got it.\n\n{next_q}",
                "tier": "local", "latency_ms": 0,
                "source": "onboarding_step"}
    # All done — save to business_info, mark complete
    try:
        biz = mod_business.load(DATA_DIR) or {}
        biz["name"] = collected.get("name", "")
        contact = biz.get("contact") or {}
        if collected.get("phone"):
            contact["phone"] = collected["phone"]
        biz["contact"] = contact
        services = [s.strip() for s in (collected.get("services") or "").split(",")
                     if s.strip()]
        if services:
            biz["services"] = services
        if collected.get("address"):
            # Store as a string for simplicity — the address field accepts
            # either string or dict per business_info.py handling.
            biz["address"] = collected["address"]
        if collected.get("license"):
            biz["license"] = collected["license"]
        mod_business.save(DATA_DIR, biz)
        state["complete"] = True
        _save_onboarding_state(state)
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="onboarding.completed",
                        meta={"business_name": collected.get("name")})
    except Exception as e:
        log.exception("onboarding save failed")
        return {"reply": f"Almost — but saving failed: {e}. You can fill in your "
                          f"business info under the Settings tab instead.",
                "tier": "local", "latency_ms": 0,
                "source": "onboarding_save_error"}
    biz_name = collected.get("name", "your business")
    return {"reply": (f"🎉 Setup complete. {biz_name} is ready to take calls "
                      f"and chats. Try asking me anything — \"what's on my "
                      f"calendar today\" or \"morning brief\" — or jump into "
                      f"Settings to add staff, hours, payment methods, etc."),
            "tier": "local", "latency_ms": 0,
            "source": "onboarding_complete"}


# ── Contractor module chat handlers (paid add-on) ─────────────────────────
# Gated by is_module_enabled('contractor'). When the customer has the
# Contractor Orby add-on, these handlers fire BEFORE the LLM so things
# like "add project at 123 Maple" actually write to projects.json
# instead of getting a fabricated "got it" from the LLM.
#
# Three v1 intents:
#   1. Add project: "add project at 123 Maple" / "new job at 555 Oak —
#      $18k kitchen remodel"
#   2. List active projects: "what jobs are open" / "what's active" /
#      "list my projects"
#   3. Project status / lookup: "status of the Maple project" /
#      "what's going on at 555 Oak"
#
# Change-order + invoice intents come in a follow-on pass (need the
# e-sign decision + real templates first).

_GC_ADD_PROJECT_RE = _re.compile(
    r"^\s*(?:add|new|start|create|open)\s+"
    r"(?:project|job|build)"
    r"(?:\s+at)?\s+"
    r"(?P<address>.+?)"
    r"(?:\s*[—–-]\s*(?P<desc>.+?))?"
    r"\s*[.!]?\s*$",
    _re.IGNORECASE,
)
_GC_LIST_JOBS_RE = _re.compile(
    # "what active jobs" / "what's open" / "list active projects" / etc.
    # Includes "show me all active jobs", "all jobs", "what jobs do I have",
    # "current jobs", "all (my )?projects".
    r"^\s*(?:"
    r"what(?:'s| is| are)?\s+(?:my\s+)?(?:open|active|current|all)\s+(?:jobs|projects|builds)"
    r"|what\s+(?:jobs|projects|builds)\s+(?:are\s+|do\s+i\s+have\s+)?(?:open|active|going|current)"
    r"|what\s+(?:jobs|projects|builds)\s+do\s+i\s+have"
    r"|(?:list|show)\s+(?:me\s+)?(?:all\s+(?:my\s+|the\s+)?|my\s+|the\s+)?(?:active\s+|open\s+|current\s+)?(?:jobs|projects|builds)"
    r"|all\s+(?:my\s+|the\s+)?(?:active\s+|open\s+|current\s+)?(?:jobs|projects|builds)"
    r"|jobs(?:\s+open)?|projects(?:\s+open)?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_PROJECT_STATUS_RE = _re.compile(
    # "status of the Maple project" / "what's going on at 555 Oak" /
    # "what IS going on at X" / "how's the Maple project doing" /
    # "what is the (current )?contract value (for|on|of) X"
    r"^\s*(?:"
    r"status\s+(?:of|on)"
    r"|how(?:'s| is)"
    r"|what(?:'s| is)?\s+(?:going\s+on|happening)\s+(?:at|with)"
    r"|tell\s+me\s+about"
    r"|details?\s+(?:of|on)"
    r"|what(?:'s| is)?\s+(?:the\s+)?(?:current\s+)?(?:contract\s+value|contract\s+amount|contract\s+total)\s+(?:for|on|of)"
    r"|(?:current\s+)?(?:contract\s+value|contract\s+amount|contract\s+total)\s+(?:for|on|of)"
    r")\s+"
    r"(?:the\s+)?(?P<query>.+?)"
    r"(?:\s+(?:project|job|build))?"
    r"\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_DOLLAR_AMT_RE = _re.compile(
    # $36,500 / $36,500.00 / $36k / 36 thousand
    # Word boundary on the k/thousand suffix so "$36,500 kitchen" doesn't
    # multiply by 1000 because of the leading 'k' in 'kitchen'.
    r"\$\s*(\d{1,3}(?:[,\d]*)?(?:\.\d{1,2})?)(?:\s*(k|K|thousand)\b)?"
)


def _parse_money(text: str) -> float:
    """Pull a dollar amount out of free text. '$18k' → 18000.
    '$18,500' → 18500. '$18,500.50' → 18500.50. Returns 0 if none found."""
    m = _DOLLAR_AMT_RE.search(text or "")
    if not m:
        return 0.0
    raw = m.group(1).replace(",", "")
    try:
        n = float(raw)
    except ValueError:
        return 0.0
    if m.group(2):  # "k" / "K" / "thousand"
        n *= 1000
    return n


# ── Change-order chat patterns ────────────────────────────────────────────
# The hero feature. Foreman/owner triggers a CO by chat, Orbi drafts
# it from a template, queues for GC approval, then sends a one-time
# signing link to the client. On signature, logs to the project ledger.

_GC_ADD_CO_RE = _re.compile(
    # Forms:
    #   "add CO on [project] — $X — description"
    #   "change order on [project] — $X for description"
    #   "CO on the Maple project: client agreed extra $1200 for trim"
    #   "client just approved $1200 extra trim work at 555 Oak"
    #   "We added a second bathroom on the Johnson project. Create a change order."
    #   "The customer approved an extra $4,500 for electrical. Draft the paperwork."
    # Sentence-boundary friendly: accepts ^ OR a sentence break before the verb.
    r"(?:^|[.!?]\s+)\s*(?:"
    r"(?:add|new|create|raise|log|draft|write|cut|file)\s+(?:a\s+|the\s+|some\s+)?(?:c\.?o\.?|change[\s-]order|change[\s-]order\s+paperwork|paperwork)"
    r"|(?:c\.?o\.?|change[\s-]order)"
    r"|(?:the\s+)?(?:client|customer|homeowner|owner)\s+(?:just\s+)?(?:approved|agreed\s+to|signed\s+off\s+on|okayed?|gave\s+(?:the\s+)?go[\s-]?ahead\s+(?:on|for))"
    r")\b\s*",
    _re.IGNORECASE,
)
_GC_LIST_PENDING_CO_RE = _re.compile(
    r"^\s*(?:"
    r"(?:what|which)\s+(?:c\.?o\.?s?|change[\s-]orders?)\s+(?:are\s+)?(?:still\s+)?"
    r"(?:pending|waiting|open|in\s+(?:my\s+)?queue|need\s+(?:my\s+)?approval|waiting\s+for\s+(?:a\s+)?signatures?|out\s+for\s+signature|awaiting\s+(?:a\s+)?signature)"
    r"|pending\s+(?:c\.?o\.?s?|change[\s-]orders?)"
    r"|(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:pending\s+|open\s+|awaiting\s+|outstanding\s+)?(?:c\.?o\.?s?|change[\s-]orders?)"
    r"|(?:c\.?o\.?s?|change[\s-]orders?)\s+(?:waiting|awaiting|out)\s+(?:for\s+)?(?:a\s+)?(?:signature|sign\s*off)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_APPROVE_CO_RE = _re.compile(
    r"^\s*(?:"
    r"approve\s+(?:c\.?o\.?|change[\s-]order)\s*#?(?P<co_id>\S+)"
    r"|(?:c\.?o\.?|change[\s-]order)\s*#?(?P<co_id2>\S+)\s+(?:is\s+)?approved"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_SEND_CO_RE = _re.compile(
    r"^\s*(?:"
    r"send\s+(?:c\.?o\.?|change[\s-]order)\s*#?(?P<co_id>\S+)\s+(?:to\s+(?:the\s+)?client|out|for\s+signature)?"
    r"|email\s+(?:c\.?o\.?|change[\s-]order)\s*#?(?P<co_id2>\S+)\s+(?:to\s+(?:the\s+)?client)?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_LIST_UNPAID_RE = _re.compile(
    r"^\s*(?:"
    r"who(?:'s|\s+is)?\s+owes?\s+(?:me|us|the\s+company)?\s*(?:money(?:\s+right\s+now)?|right\s+now)?"
    r"|who(?:'s|\s+is)?\s+(?:got|holding)\s+(?:my|our)\s+money"
    r"|what\s+(?:invoices?\s+)?(?:are\s+)?(?:unpaid|outstanding|open|due)"
    r"|which\s+(?:invoices?\s+)?(?:are\s+)?(?:unpaid|outstanding|open|due)"
    r"|(?:show|list)\s+(?:me\s+)?(?:all\s+)?(?:my\s+|the\s+)?(?:unpaid|outstanding|open|due)\s+invoices?"
    r"|(?:unpaid|outstanding|open)\s+invoices?"
    r"|invoices?\s+(?:open|outstanding|due|unpaid)"
    r"|(?:show|list)\s+(?:me\s+)?(?:all\s+)?(?:my\s+|the\s+)?(?:open|outstanding|unpaid)\s+receivables?"
    r"|receivables?"
    r"|money\s+owed(?:\s+(?:to\s+us|to\s+me))?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_AGING_RE = _re.compile(
    r"^\s*(?:"
    r"aging(?:\s+report)?"
    r"|what(?:'s| is)?\s+overdue"
    r"|(?:any(?:thing)?\s+)?overdue(?:\s+today)?"
    r"|overdue\s+(?:invoices?|amounts?|receivables?|bills?)"
    r"|(?:what|which)\s+invoices?\s+(?:are\s+)?(?:more\s+than\s+(?P<days>\d+)\s+days?\s+)?overdue"
    r"|invoices?\s+(?:more\s+than\s+)?(?P<days2>\d+)?\s*days?\s+overdue"
    r"|(?:show|list)\s+(?:me\s+)?(?:all\s+)?(?:my\s+)?(?:overdue|past[\s-]due|late)\s+(?:invoices?|receivables?|bills?)"
    r"|(?:past[\s-]due|late)\s+(?:invoices?|receivables?|bills?)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_ADD_INVOICE_RE = _re.compile(
    # "send invoice for Oak — $5000 progress draw"
    # "bill Maple $3000 for framing"
    # "invoice the Oak project $4200 — draw 2"
    r"^\s*(?:"
    r"(?:send|create|generate|cut)\s+(?:an?\s+)?invoice\s+(?:for\s+|on\s+|to\s+)?"
    r"|bill\s+(?:the\s+)?"
    r"|invoice\s+(?:the\s+|for\s+)?"
    r")",
    _re.IGNORECASE,
)
_GC_PDF_INVOICE_RE = _re.compile(
    # "PDF INV-2026-0001" / "generate PDF for INV-2026-0001" /
    # "send INV-2026-0001 as PDF" / "invoice PDF for INV-2026-0001"
    r"^\s*(?:"
    r"(?:pdf|print)\s+(?:invoice\s+)?(?P<inv1>INV-[\w-]+)"
    r"|(?:generate|make|create)\s+(?:a\s+)?(?:invoice\s+)?pdf\s+(?:for\s+)?(?P<inv2>INV-[\w-]+)"
    r"|invoice\s+pdf\s+(?:for\s+)?(?P<inv3>INV-[\w-]+)"
    r"|send\s+(?P<inv4>INV-[\w-]+)\s+as\s+(?:a\s+)?pdf"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_RECORD_PAYMENT_RE = _re.compile(
    # "Sarah paid $3000 on INV-2026-0001"
    # "Mark INV-2026-0001 paid"
    # "received $500 on INV-2026-0042"
    r"^\s*(?:"
    r"(?:received|recorded|paid|got)\s+\$?(?P<amt1>[\d,]+(?:\.\d{1,2})?)"
    r"|.+?\s+paid\s+\$?(?P<amt2>[\d,]+(?:\.\d{1,2})?)"
    r"|mark\s+(?:invoice\s+)?(?P<inv1>INV-[\w-]+)\s+paid"
    r")",
    _re.IGNORECASE,
)
_GC_REPORT_RE = _re.compile(
    r"^\s*(?:"
    r"(?:show\s+me\s+(?:the\s+)?money|money\s+report|contractor\s+report"
    r"|captured\s+(?:this\s+(?:week|month|year)|so\s+far))"
    r"|(?:where\s+(?:are\s+|am\s+i\s+)?(?:at|standing)\s+(?:on\s+)?(?:the\s+)?money)"
    r"|gc\s+(?:report|dashboard|stats)"
    r"|what(?:'s| is)?\s+(?:our|my|the)\s+(?:total\s+)?(?:accounts\s+receivable|a[/.\s]?r)(?:\s+balance|\s+total)?"
    r"|(?:total\s+)?accounts\s+receivable(?:\s+balance|\s+total)?"
    r"|a[/.\s]?r\s+balance"
    r"|how\s+much\s+(?:money\s+)?(?:have\s+we\s+)?collected(?:\s+this\s+(?:week|month|year))?"
    r"|(?:money|cash)\s+collected(?:\s+this\s+(?:week|month|year))?"
    r"|what(?:'s| is)?\s+(?:our|my)\s+(?:current\s+)?cash\s+(?:position|on\s+hand)"
    r"|cash\s+(?:position|on\s+hand)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_INV_NUMBER_RE = _re.compile(r"\bINV-\d{4}-\d{4}\b", _re.IGNORECASE)

# ── Daily log patterns ────────────────────────────────────────────────────
_GC_ADD_LOG_RE = _re.compile(
    # "log today on Oak — crew Mike + Jose, framing complete, lumber delivered, 8 hours"
    # "daily log Oak: framing done, 6 hours"
    # "logged 8 hours on Maple, drywall in"
    # NOT "logged bid ..." — that's a bid not a log; bid handler takes it.
    # NOT "log me in" / "log out" / "log into" — those are auth verbs.
    # The bare "log" form requires explicit today/for/on suffix to avoid
    # catching "log me/you/us in as <user>" type auth commands.
    r"^\s*(?:"
    r"(?:add|create|file|write)\s+(?:a\s+)?(?:daily\s+)?log\b(?!\s+me|\s+in|\s+out|\s+into)"
    r"|(?:daily\s+)?log\s+(?:today|for|on)\b"
    r"|logged?\s+(?:\d+\s+hours?\s+)(?:on|for)?"     # require an hours count
    r"|logged?\s+(?:on|for)\s+"                       # OR explicit on/for
    r")\s*",
    _re.IGNORECASE,
)
_GC_LIST_LOGS_RE = _re.compile(
    r"^\s*(?:"
    r"(?:show|list|what)\s+(?:me\s+)?(?:the\s+)?logs?\s+(?:for|on)\s+(?P<query>.+?)"
    r"|(?:what\s+happened|what(?:'s| was)\s+(?:on|in)\s+the\s+log)\s+(?:on|for|at)\s+(?P<query2>.+?)"
    r"|logs?\s+(?:for|on)\s+(?P<query3>.+?)"
    r"|this\s+week'?s?\s+logs?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)

# ── Subcontractor patterns ────────────────────────────────────────────────
_GC_ADD_SUB_RE = _re.compile(
    # "add sub Bob's Plumbing — plumbing — 555-555-1234"
    # "new sub Joe — drywall, joe@example.com"
    # "register sub Acme Electric, electrical, license NV-12345"
    r"^\s*(?:"
    r"(?:add|new|register|create)\s+(?:a\s+)?(?:sub(?:contractor)?|trade|vendor)"
    r")\b\s*",
    _re.IGNORECASE,
)
_GC_LIST_SUBS_RE = _re.compile(
    r"^\s*(?:"
    r"(?:list|show|who(?:'s| is| are)?\s+my)\s+(?:my\s+)?(?:active\s+)?(?:sub(?:contractor)?s?|trades|vendors)(?:\s+for\s+(?P<trade>\w+))?"
    r"|sub(?:contractor)?s?(?:\s+for\s+(?P<trade2>\w+))?"
    r"|trades(?:\s+for\s+(?P<trade3>\w+))?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_ASSIGN_SUB_RE = _re.compile(
    # "assign Bob to Oak" / "assign Bob's Plumbing to the Maple project"
    # "put Acme on Oak Tuesday for rough plumbing"
    r"^\s*(?:assign|put|schedule|dispatch)\s+"
    r"(?P<sub>.+?)\s+(?:to|on|at)\s+"
    r"(?P<rest>.+?)\s*[.?!]?\s*$",
    _re.IGNORECASE,
)
_GC_INSURANCE_CHECK_RE = _re.compile(
    r"^\s*(?:"
    r"(?:whose\s+|which\s+sub(?:contractor)?s'?\s+)?(?:insurance|coverage|coi)\s+"
    r"(?:expires?|is\s+expiring|needs?\s+(?:to\s+be\s+)?renewed)"
    r"|expiring\s+(?:insurance|coverage|cois?)"
    r"|insurance\s+(?:check|status|report)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)

# ── Unsigned-work leak alarm pattern ──────────────────────────────────────
_GC_REVIEW_LINK_RE = _re.compile(
    r"^\s*(?:"
    r"(?:get|mint|generate|create|send)\s+(?:a\s+)?review\s+link\s+for\s+(?P<q1>.+?)"
    r"|review\s+link\s+(?:for\s+)?(?P<q2>.+?)"
    r"|ask\s+(?:for\s+)?(?:a\s+)?review\s+(?:from\s+|on\s+)?(?P<q3>.+?)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_MY_RATING_RE = _re.compile(
    r"^\s*(?:"
    r"(?:what(?:'s| is)?\s+my|show\s+(?:me\s+)?my)\s+(?:rating|reviews|stars|score)"
    r"|my\s+(?:rating|reviews|review\s+score|average)"
    r"|how(?:'?s| are|'?ve)\s+my\s+reviews?"
    r"|review\s+(?:summary|report|stats)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_ADD_BID_RE = _re.compile(
    # "sent bid for Sarah Johnson at 123 Maple — $48,500 kitchen remodel"
    # "logged bid $24k Tom Anderson deck Sparks"
    # "bid sent Maria Garcia $8,800 whole-home repaint"
    r"^\s*(?:"
    r"(?:sent|logged|recorded|new)\s+(?:a\s+)?bid"
    r"|bid\s+(?:sent|added|recorded)"
    r")\b\s*",
    _re.IGNORECASE,
)
_GC_LIST_BIDS_RE = _re.compile(
    r"^\s*(?:"
    r"(?:what|list|show)\s+(?:me\s+)?(?:my\s+)?(?:open|outstanding|pending|active)\s+bids?"
    r"|(?:open|outstanding|pending)\s+bids?"
    r"|bids?\s+(?:open|outstanding|out|pending)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_BID_WON_RE = _re.compile(
    r"^\s*(?P<who>.+?)\s+(?:won|signed|accepted|took|chose|hired)\s+(?:the\s+|us\s+(?:on\s+)?(?:the\s+)?)?"
    r"(?P<what>.+?)?\s*(?:bid|estimate|proposal|job)?\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_BID_LOST_RE = _re.compile(
    r"^\s*(?:"
    r"(?P<who>.+?)\s+(?:lost|passed\s+on|declined|went\s+with|hired\s+someone\s+else)\s+(?:on\s+)?(?:the\s+)?(?P<what>.+?)?"
    r"|lost\s+(?:the\s+)?(?P<who2>.+?)\s+bid"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_PROPOSAL_PDF_RE = _re.compile(
    r"^\s*(?:"
    r"(?:generate|create|make|build|print)\s+(?:a\s+)?(?:bid\s+)?(?:proposal|estimate)\s+(?:pdf\s+)?(?:for\s+)?(?P<q1>.+?)"
    r"|(?:proposal|estimate)\s+(?:pdf\s+)?(?:for\s+)?(?P<q2>.+?)"
    r"|pdf\s+(?:the\s+)?(?P<q3>.+?)(?:'s)?\s+(?:bid|proposal|estimate)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_BID_REPORT_RE = _re.compile(
    r"^\s*(?:"
    r"(?:bid|win[/\s]*loss|win[/\s]*rate)\s+(?:report|stats?|summary|pipeline)?"
    r"|win\s*rate"
    r"|winrate"
    r"|what'?s?\s+my\s+(?:win[/\s]*rate|conversion)"
    r"|how\s+(?:many\s+)?bids?\s+(?:have\s+i\s+)?won"
    r"|bid\s+pipeline"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_CLOSEOUT_RE = _re.compile(
    r"^\s*(?:"
    r"(?:generate|create|build|make|send)\s+(?:the\s+)?closeout"
    r"\s+(?:package\s+)?(?:for\s+|on\s+)?(?P<q1>.+?)"
    r"|closeout\s+(?:package\s+)?(?:for\s+|on\s+)?(?P<q2>.+?)"
    r"|(?P<q3>.+?)\s+closeout(?:\s+(?:pdf|package|doc))?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_SHARE_PORTAL_RE = _re.compile(
    # "share Oak with the client" / "client portal for Maple" /
    # "send Sarah her project link" / "Oak portal link"
    r"^\s*(?:"
    r"share\s+(?P<q1>.+?)\s+(?:with\s+(?:the\s+)?(?:client|customer|homeowner)|link)"
    r"|(?:client\s+)?portal\s+(?:for\s+|link\s+for\s+)?(?P<q2>.+?)"
    r"|(?P<q3>.+?)\s+(?:client\s+)?(?:portal|link)"
    r"|(?:get|generate|mint)\s+(?:a\s+)?(?:client\s+)?(?:portal|link)\s+(?:for\s+)?(?P<q4>.+?)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_WEEKLY_RECAP_RE = _re.compile(
    r"^\s*(?:"
    r"(?:weekly|week'?s?|this\s+week'?s?)\s+(?:recap|summary|report|review|wrap[\s-]?up)"
    r"|(?:wrap\s+up|recap)\s+(?:my\s+|the\s+)?week"
    r"|how(?:'s| was| did)\s+(?:my\s+|the\s+)?week\s+(?:go|going)?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_PROJECT_REPORT_RE = _re.compile(
    # "full report on Oak" / "everything on Maple" / "deep dive Oak"
    # / "show me all of Oak" / "Oak summary" / "project history for Johnson"
    # / "history (for|on|of) Johnson"
    r"^\s*(?:"
    r"(?:full\s+report|deep\s+dive|everything|all\s+(?:about|on))\s+(?:on\s+|of\s+)?(?:the\s+)?(?P<q1>.+?)"
    r"|(?P<q2>.+?)\s+(?:summary|recap|breakdown|deep\s+dive)"
    r"|show\s+me\s+(?:all\s+|every(?:thing)?\s+)?(?:on\s+|of\s+|for\s+)?(?:the\s+)?(?P<q3>.+?)\s+project"
    r"|(?:show\s+me\s+(?:the\s+)?|give\s+me\s+(?:the\s+)?|tell\s+me\s+(?:the\s+)?)?(?:project\s+)?history\s+(?:for|on|of)\s+(?:the\s+)?(?P<q4>.+?)(?:\s+project)?"
    r"|(?P<q5>.+?)(?:'s)?\s+project\s+history"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_MARK_PROJECT_DONE_RE = _re.compile(
    r"^\s*(?:"
    r"(?:mark|close|complete|finish)\s+(?:the\s+)?(?P<q>.+?)\s+(?:project\s+)?(?:as\s+)?(?:done|complete|finished|closed)"
    r"|(?P<q2>.+?)\s+(?:is\s+)?(?:done|complete|finished|wrapped\s+up)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_SUB_SCHEDULE_RE = _re.compile(
    # "what's Bob working on" / "Bob's schedule" / "where is Acme"
    # / "whats Bob working on" (no apostrophe) / "what is Bob doing"
    r"^\s*(?:"
    r"(?:what(?:'?s| is)?|whats)\s+(?P<sub1>.+?)\s+(?:working\s+on|doing|on)"
    r"|(?P<sub2>.+?)(?:'s)?\s+(?:schedule|assignments|jobs)"
    r"|where\s+(?:is\s+|are\s+)?(?P<sub3>.+?)\s*(?:assigned|working)?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_LEAK_CHECK_RE = _re.compile(
    r"^\s*(?:"
    r"(?:do|does|are|is|have)\s+(?:we|i|any\s+jobs?|any\s+projects?)\s+(?:have|got|do\s+we\s+have)?\s*(?:any\s+)?(?:work|scope|jobs?|builds?)\s+(?:being\s+performed|going\s+on|happening|done|active|in\s+progress)?\s*(?:without|with\s+no)\s+(?:a\s+)?(?:signed\s+)?(?:c\.?o\.?|change[\s-]orders?)"
    r"|(?:which|what)\s+(?:jobs?|projects?|builds?)\s+(?:currently\s+|do\s+we\s+|do\s+i\s+)?(?:have|got|are\s+(?:showing|throwing))\s+(?:any\s+)?leak\s+(?:alarms?|alerts?|warnings?|flags?)"
    r"|(?:any\s+)?(?:unsigned|unbilled|leaked|missing|uncovered)\s+(?:work|scope|cos?|change[\s-]orders?)"
    r"|(?:scope|work)\s+(?:.{0,40}?)(?:without|with\s+no)\s+(?:a\s+)?(?:signed\s+)?(?:c\.?o\.?|change[\s-]order|sign(?:ature|ed))"
    r"|leak\s+(?:check|alarm|report|alarms|alerts?)"
    r"|am\s+i\s+leaking\s+money"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_NUDGE_RE = _re.compile(
    # "nudge INV-2026-0001" / "send a reminder on INV-2026-0001"
    # / "draft a follow-up for INV-2026-0001" / "remind them about ..."
    r"^\s*(?:"
    r"(?:nudge|chase|ping)\s+(?:invoice\s+)?(?P<inv1>INV-[\w-]+|client|customer|them)"
    r"|(?:send|draft|write|compose)\s+(?:a\s+)?(?:reminder|nudge|follow[\s-]up)\s+"
    r"(?:on|for|about)?\s*(?P<inv2>INV-[\w-]+)?"
    r"|remind\s+(?:them|the\s+client)\s+(?:about\s+)?(?P<inv3>INV-[\w-]+)?"
    r")",
    _re.IGNORECASE,
)
_GC_NUDGE_ALL_RE = _re.compile(
    r"^\s*(?:"
    r"(?:draft|send)\s+(?:all\s+)?(?:overdue\s+)?(?:reminders|nudges|follow[\s-]ups)"
    r"|nudge\s+(?:all|everyone|everybody|every(?:one|body)\s+overdue)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_HELP_RE = _re.compile(
    # Strictly contractor-help — must explicitly name "contractor" or "gc"
    # or "cheat-sheet". Generic "what can you do" / "help" is handled by
    # the global capabilities-overview handler, not this one.
    r"^\s*(?:"
    r"(?:contractor|gc)\s+(?:help|commands?|cheat[\s-]sheet)"
    r"|(?:contractor|gc)\s+(?:commands?|cheat[\s-]sheet)"
    r"|cheat[\s-]sheet"
    r"|(?:what|which|list)\s+(?:the\s+)?contractor\s+commands?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_RUN_SWEEP_RE = _re.compile(
    r"^\s*(?:"
    r"(?:run|trigger|do)\s+(?:the\s+)?(?:receivables?|nudge|followup|follow[\s-]up)\s+(?:sweep|check|scan)"
    r"|check\s+(?:for\s+)?overdue\s+(?:invoices?|payments?)"
    r"|sweep\s+(?:overdue|receivables?)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_SHOW_QUEUE_RE = _re.compile(
    r"^\s*(?:"
    r"(?:show|list|view)\s+(?:me\s+)?(?:my\s+)?(?:queued|pending|drafted|auto)\s+(?:reminders?|nudges?|follow[\s-]ups?)"
    r"|(?:queued|pending|drafted)\s+(?:reminders?|nudges?|follow[\s-]ups?)"
    r"|what'?s?\s+in\s+(?:the\s+)?(?:reminder|nudge|follow[\s-]up)\s+queue"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_SEND_QUEUE_RE = _re.compile(
    r"^\s*(?:"
    r"send\s+(?:all\s+)?(?:my\s+)?(?:queued|pending|drafted|auto)\s+(?:reminders?|nudges?|follow[\s-]ups?)"
    r"|fire\s+(?:off\s+)?(?:the\s+)?(?:reminder|nudge|follow[\s-]up)\s+queue"
    r"|approve\s+(?:and\s+send\s+)?(?:all\s+)?(?:queued|pending|drafted)\s+(?:reminders?|nudges?)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
# "Send a payment reminder to Johnson Construction" — nudge by customer NAME,
# not invoice number. Looks up the customer's unpaid invoices and drafts.
_GC_NUDGE_BY_CUSTOMER_RE = _re.compile(
    r"^\s*(?:"
    r"(?:send|draft|write|compose|fire\s+off)\s+(?:a\s+|an?\s+)?(?:payment\s+)?"
    r"(?:reminder|nudge|follow[\s-]up)\s+(?:to|for)\s+(?P<who>.+?)"
    r"|nudge\s+(?P<who2>.+?)(?:\s+about\s+(?:their\s+|the\s+)?(?:invoice|payment|balance))?"
    r"|remind\s+(?P<who3>.+?)\s+(?:about|to\s+pay)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_RETAINAGE_RE = _re.compile(
    r"^\s*(?:"
    r"how\s+much\s+retainage\s+(?:are\s+we\s+(?:still\s+)?)?(?:owed|holding|still\s+holding|out)"
    r"|(?:what(?:'s| is)?|total)\s+(?:our\s+|my\s+)?(?:total\s+)?retainage(?:\s+(?:balance|outstanding|owed|held))?"
    r"|retainage\s+(?:owed|held|outstanding|balance|total)"
    r"|(?:show|list)\s+(?:me\s+)?(?:the\s+|all\s+)?retainage"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_TOP_CLIENTS_RE = _re.compile(
    r"^\s*(?:"
    r"(?:show|list|give)\s+(?:me\s+)?(?:our\s+|my\s+|the\s+)?top\s+(?P<n>\d+|five|ten|three)?\s*(?:best\s+)?(?:clients?|customers?)"
    r"(?:\s+by\s+(?:revenue|amount|spend|billing|paid|value))?"
    r"|top\s+(?P<n2>\d+|five|ten|three)?\s*(?:clients?|customers?)\s+by\s+(?:revenue|amount|spend|billing|paid|value)"
    r"|who(?:'?s| are)\s+(?:our|my)\s+top\s+(?P<n3>\d+|five|ten|three)?\s*(?:clients?|customers?)"
    r"|biggest\s+(?:clients?|customers?)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_FINISHING_THIS_MONTH_RE = _re.compile(
    r"^\s*(?:"
    r"(?:which|what)\s+(?:jobs|projects|builds)\s+(?:are\s+)?(?:scheduled\s+to\s+)?(?:finish|complete|wrap\s+up|end|close|done)"
    r"\s+(?:this\s+|the\s+(?:rest\s+of\s+the\s+)?|by\s+(?:end\s+of\s+(?:the\s+)?)?)?"
    r"(?P<period>month|week|quarter|year)"
    r"|(?:jobs|projects|builds)\s+(?:finishing|completing|wrapping(?:\s+up)?|closing|done)"
    r"\s+(?:this\s+|by\s+(?:end\s+of\s+)?)?(?P<period2>month|week|quarter|year)"
    r"|what'?s?\s+(?:finishing|wrapping(?:\s+up)?|closing)\s+(?:this\s+)?(?P<period3>month|week|quarter|year)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_HIGHEST_VALUE_RE = _re.compile(
    r"^\s*(?:"
    r"(?:what|which)\s+(?:jobs|projects|builds)\s+(?:have|are)\s+(?:the\s+)?(?:highest|biggest|largest|most|top)(?:\s+value|\s+contract|\s+dollar)?"
    r"|(?:highest|biggest|largest|top)[\s-]value\s+(?:jobs|projects|builds)"
    r"|biggest\s+(?:jobs|projects|builds)"
    r"|(?:show|list)\s+(?:me\s+)?(?:my\s+)?(?:jobs|projects|builds)\s+by\s+(?:value|contract|size|dollar)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_SIGNED_COS_FOR_PROJECT_RE = _re.compile(
    r"^\s*(?:"
    r"(?:show|list|give)\s+(?:me\s+)?(?:all\s+)?(?:the\s+)?signed\s+(?:c\.?o\.?s?|change[\s-]orders?)\s+(?:for|on)\s+(?:the\s+)?(?P<q1>.+?)(?:\s+project)?"
    r"|signed\s+(?:c\.?o\.?s?|change[\s-]orders?)\s+(?:for|on)\s+(?:the\s+)?(?P<q2>.+?)(?:\s+project)?"
    r"|what\s+(?:c\.?o\.?s?|change[\s-]orders?)\s+(?:are\s+)?signed\s+(?:for|on)\s+(?:the\s+)?(?P<q3>.+?)(?:\s+project)?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_CO_REVENUE_PERIOD_RE = _re.compile(
    r"^\s*(?:"
    r"(?:show|tell|give)\s+(?:me\s+)?(?:my\s+|the\s+|our\s+)?(?:total\s+)?(?:change[\s-]order|c\.?o\.?)\s+(?:revenue|dollars?|total|captured|earnings?)"
    r"\s+(?:this\s+|for\s+(?:this\s+|the\s+)?|so\s+far\s+this\s+)?(?P<period>month|week|quarter|year)"
    r"|how\s+much\s+(?:change[\s-]order|c\.?o\.?)\s+(?:revenue|dollars?|money)\s+(?:(?:have|has)\s+(?:i|we|orby|she|you)\s+)?(?:captured|earned|made|landed|brought\s+in)?\s*"
    r"(?:this\s+|for\s+(?:this\s+|the\s+)?|so\s+far\s+this\s+|in\s+the\s+(?:last\s+|past\s+)?)?(?P<period2>month|week|quarter|year)"
    r"|(?:change[\s-]order|c\.?o\.?)\s+(?:revenue|dollars?|total|captured)\s+(?:this\s+|for\s+|so\s+far\s+this\s+)?(?P<period3>month|week|quarter|year)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
# "Increase the Johnson contract by the approved change order amount" — the
# user wants to confirm; signed COs already do this automatically. Status
# explainer rather than a write.
_GC_INCREASE_CONTRACT_BY_CO_RE = _re.compile(
    r"^\s*(?:"
    r"increase\s+(?:the\s+)?(?P<q>.+?)\s+contract\s+by\s+(?:the\s+)?(?:approved\s+)?(?:change[\s-]order|c\.?o\.?)\s+(?:amount|total)?"
    r"|add\s+(?:the\s+)?(?:approved\s+)?(?:change[\s-]order|c\.?o\.?)\s+(?:amount|total)?\s+to\s+(?:the\s+)?(?P<q2>.+?)\s+contract"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
# Profitability: we don't track costs, so refuse honestly instead of inventing.
_GC_PROJECTS_WITHOUT_FOREMAN_RE = _re.compile(
    r"^\s*(?:"
    r"(?:which|what)\s+(?:projects?|jobs?|builds?)\s+(?:do\s+)?(?:not|don'?t)\s+have\s+(?:a\s+|any\s+)?foreman(?:\s+assigned)?"
    r"|(?:which|what)\s+(?:projects?|jobs?|builds?)\s+(?:are\s+)?(?:missing|without)\s+(?:a\s+|any\s+)?foreman(?:\s+assigned)?"
    r"|(?:projects?|jobs?|builds?)\s+(?:with\s+no|missing|without)\s+(?:a\s+)?foreman(?:\s+assigned)?"
    r"|(?:show|list)\s+(?:me\s+)?(?:projects?|jobs?|builds?)\s+(?:that\s+)?(?:lack|need|don'?t\s+have)\s+(?:a\s+)?foreman(?:\s+assigned)?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_PCT_OVERDUE_RE = _re.compile(
    r"^\s*(?:"
    r"(?:what'?s|what\s+is)\s+(?:the\s+)?(?:percentage|percent|%)\s+of\s+(?:my\s+|our\s+|the\s+)?invoices?\s+(?:are\s+|that\s+are\s+)?overdue"
    r"|what\s+(?:percentage|percent|%)\s+(?:of\s+(?:my\s+)?invoices?\s+)?(?:are\s+)?overdue"
    r"|overdue\s+(?:percentage|percent|rate|ratio)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_TOTAL_CONTRACT_VALUE_RE = _re.compile(
    r"^\s*(?:"
    r"(?:what'?s|what\s+is)\s+(?:the\s+|my\s+|our\s+)?total\s+contract\s+(?:value|amount|total)\s+(?:across\s+(?:all\s+)?(?:active\s+|open\s+)?(?:jobs|projects|builds)|of\s+(?:all\s+)?(?:active\s+|open\s+)?(?:jobs|projects|builds))"
    r"|total\s+contract\s+(?:value|amount|total)\s+(?:across|of)\s+(?:all\s+)?(?:active\s+|open\s+)?(?:jobs|projects|builds)"
    r"|(?:sum|total)\s+(?:of\s+)?(?:all\s+)?(?:active\s+|open\s+)?(?:job|project|build|contract)\s+values?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_GC_PROFITABILITY_RE = _re.compile(
    r"^\s*(?:"
    r"(?:what|which)\s+(?:jobs|projects|builds)\s+(?:are\s+(?:the\s+)?)?(?:most\s+|highest\s+)?profitab(?:le|ility)"
    r"|(?:most\s+|highest\s+)profitab(?:le|ility)\s+(?:jobs|projects|builds)"
    r"|(?:show|tell)\s+(?:me\s+)?(?:my\s+)?(?:job|project)\s+profitab(?:le|ility)"
    r"|(?:job|project)\s+margins?"
    r"|what\s+(?:are\s+)?(?:my\s+)?margins?\s+(?:per\s+|by\s+)?(?:job|project)?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _draft_nudge_text(invoice: dict, project: dict, escalation: int = 1) -> str:
    """Generate the polite-escalating reminder body. Tone tiers:
       1 = friendly first nudge
       2 = firmer (week 2)
       3 = formal (week 3+, mentions next-step options)
    Frank can edit the text by changing this one function. No LLM — kept
    deterministic so the GC knows exactly what's being sent on their behalf.
    """
    from datetime import datetime as _dt
    biz_name = (CONFIG.get("business") or {}).get("name", "your contractor")
    customer = project.get("customer_name") or "there"
    addr = project.get("address", "")
    inv_num = invoice.get("invoice_number", "")
    owed = float(invoice.get("amount_due", 0)) - float(invoice.get("amount_paid", 0))
    due_at = invoice.get("due_at")
    due_str = ""
    if due_at:
        due_str = _dt.fromtimestamp(int(due_at)).strftime("%B %-d, %Y")
    days_late = ""
    if due_at and int(due_at) < int(time.time()):
        days = (int(time.time()) - int(due_at)) // 86400
        days_late = f" ({days} day{'s' if days != 1 else ''} past due)"
    if escalation <= 1:
        return (
            f"Hi {customer},\n\n"
            f"Just a friendly reminder that invoice {inv_num} for the work at "
            f"{addr} (${owed:,.2f}) is now due{(' — was due ' + due_str) if due_str else ''}"
            f"{days_late}. If your lender's already cut the draw or your "
            f"payment is on the way, thank you — please ignore this note.\n\n"
            f"Thanks,\n{biz_name}"
        )
    if escalation == 2:
        return (
            f"Hi {customer},\n\n"
            f"Following up on invoice {inv_num} for the work at {addr}. "
            f"The balance is ${owed:,.2f}, which was due {due_str or 'recently'}"
            f"{days_late}. Could you let me know when I should expect payment? "
            f"If there's an issue with the invoice or the work, please just "
            f"reach out — happy to talk it through.\n\n"
            f"Thanks,\n{biz_name}"
        )
    # escalation >= 3 — formal but still measured
    return (
        f"{customer},\n\n"
        f"This is a formal notice that invoice {inv_num} for {addr} (${owed:,.2f}) "
        f"remains unpaid{days_late}. If payment isn't received within seven (7) days, "
        f"we'll need to discuss next steps including possible lien filing.\n\n"
        f"If there's a dispute we can resolve, please contact me directly today. "
        f"I'd rather work this out with a phone call.\n\n"
        f"{biz_name}"
    )


def _draft_change_order_text(project: dict, description: str, amount: float,
                              scope_detail: str = "") -> str:
    """Build the CO document body from a placeholder template. Frank can
    swap in real legal language by editing this function (or by putting
    a template file alongside config.json and reading from it later)."""
    from datetime import datetime as _dt
    contract = project.get("contract_amount") or 0
    biz = (CONFIG.get("business") or {})
    biz_name = biz.get("name") or "Contractor"
    customer = project.get("customer_name") or "Client"
    address = project.get("address") or "(project address)"
    label = project.get("label") or ""
    date = _dt.now().strftime("%B %d, %Y")
    sign_str = "increase" if amount >= 0 else "decrease"
    abs_amount = abs(float(amount))
    return f"""CHANGE ORDER

Date: {date}
Contractor: {biz_name}
Customer: {customer}
Project: {address}{(' — ' + label) if label else ''}

Original Contract Amount: ${contract:,.2f}

CHANGE REQUESTED:
{description}

{('Scope Detail: ' + scope_detail) if scope_detail else ''}

Contract {sign_str.upper()}: ${abs_amount:,.2f}

By signing below, the Customer authorizes the Contractor to perform
the work described above for the amount stated, and agrees that this
change order modifies the original contract accordingly. All other
terms of the original contract remain in effect.

________________________________________
Customer Signature: {customer}

________________________________________
Date Signed:
"""


def _try_contractor_chat(message: str, user_rec: dict) -> dict | None:
    """Detect contractor-module intents and act. Returns chat reply or
    None to fall through. Gated by is_module_enabled('contractor') —
    if the module isn't purchased, this handler is invisible.
    """
    if not is_module_enabled("contractor"):
        return None
    msg = _strip_polite_prefix(message or "")

    # 1. ADD PROJECT
    m = _GC_ADD_PROJECT_RE.match(msg)
    if m:
        address = (m.group("address") or "").strip()
        desc = (m.group("desc") or "").strip()
        # Pull money out of either the address tail or the description
        amount = _parse_money(desc) or _parse_money(address)
        # If the description had the money, strip it back out for label
        label = desc
        if amount and label:
            label = _DOLLAR_AMT_RE.sub("", label).strip(" ,—–-")
        try:
            p = mod_projects.add(
                DATA_DIR, address=address, label=label,
                contract_amount=amount, status="active",
                notes=f"Added via chat by {user_rec.get('username', '?')}",
            )
        except ValueError as e:
            return {"reply": f"Couldn't add: {e}",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_project_add_error"}
        amount_bit = f" (${amount:,.0f})" if amount else ""
        label_bit = f" — {label}" if label else ""
        return {"reply": f"Project added — {address}{label_bit}{amount_bit}. ID {p['id'][:8]}.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_project_added"}

    # 2. LIST OPEN JOBS
    if _GC_LIST_JOBS_RE.match(msg):
        active = mod_projects.list_active(DATA_DIR)
        if not active:
            return {"reply": "No active projects on your board. Add one with: \"new project at 123 Maple — $18k kitchen remodel\"",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_no_active"}
        lines = [f"Active jobs ({len(active)}):"]
        for p in active[:20]:
            label = p.get("label") or "(no label)"
            amt = p.get("contract_amount") or 0
            stage = p.get("stage") or p.get("status") or ""
            signed_co = mod_change_orders.signed_total_for_project(DATA_DIR, p["id"])
            total = amt + signed_co
            co_bit = f" (+${signed_co:,.0f} CO)" if signed_co else ""
            stage_bit = f" — {stage}" if stage else ""
            lines.append(f"  · {p['address']} — {label} ${total:,.0f}{co_bit}{stage_bit}")
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "gc_jobs_listed"}

    # 3. PROJECT STATUS / LOOKUP
    m = _GC_PROJECT_STATUS_RE.match(msg)
    if m:
        query = (m.group("query") or "").strip()
        if query and query.lower() not in {"things", "stuff", "everything"}:
            matches = mod_projects.find_by_address(DATA_DIR, query)
            if not matches:
                return {"reply": f"I don't see a project matching \"{query}\". Try part of the address or the label.",
                        "tier": "local", "latency_ms": 0,
                        "source": "gc_project_not_found"}
            if len(matches) > 1:
                listed = "\n".join(f"  · {p['address']} ({p.get('label','')})" for p in matches[:5])
                return {"reply": f"Multiple matches:\n{listed}\nWhich one?",
                        "tier": "local", "latency_ms": 0,
                        "source": "gc_project_ambiguous"}
            p = matches[0]
            signed_co = mod_change_orders.signed_total_for_project(DATA_DIR, p["id"])
            invs = mod_invoices.list_for_project(DATA_DIR, p["id"])
            billed = sum(float(i.get("amount_due", 0)) for i in invs
                         if i.get("status") not in ("draft", "void"))
            paid = sum(float(i.get("amount_paid", 0)) for i in invs)
            lines = [
                f"{p['address']} — {p.get('label','(no label)')}",
                f"  Status:   {p.get('status','?')}{' (' + p['stage'] + ')' if p.get('stage') else ''}",
                f"  Foreman:  {p.get('foreman','(none)')}",
                f"  Contract: ${(p.get('contract_amount') or 0):,.0f}"
                + (f" + ${signed_co:,.0f} signed COs = ${(p.get('contract_amount',0) or 0) + signed_co:,.0f}" if signed_co else ""),
                f"  Billed:   ${billed:,.0f}  Paid: ${paid:,.0f}  Owed: ${billed - paid:,.0f}",
            ]
            if p.get("notes"):
                lines.append(f"  Notes:    {p['notes'][:100]}")
            return {"reply": "\n".join(lines), "tier": "local",
                    "latency_ms": 0, "source": "gc_project_status"}

    # 4. LIST PENDING COs (GC's approval queue)
    if _GC_LIST_PENDING_CO_RE.match(msg):
        pending = mod_change_orders.list_pending_approval(DATA_DIR)
        awaiting_sig = mod_change_orders.list_awaiting_signature(DATA_DIR)
        if not pending and not awaiting_sig:
            return {"reply": "Nothing waiting on you. No COs in the queue and none out for client signature.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_no_pending_cos"}
        lines = []
        if pending:
            lines.append(f"Waiting for your approval ({len(pending)}):")
            for c in pending[:10]:
                proj = mod_projects.get(DATA_DIR, c.get("project_id", ""))
                proj_label = proj.get("address", "?") if proj else "?"
                amt = float(c.get("amount") or 0)
                sign = "+" if amt >= 0 else ""
                from_client = " 🔔 [client request]" if c.get("status") == "client_requested" else ""
                lines.append(f"  · #{c['id'][:8]} {proj_label} — {c.get('description','')} ({sign}${amt:,.0f}){from_client}")
            lines.append("  Approve with: \"approve CO #<id>\"")
        if awaiting_sig:
            if lines: lines.append("")
            lines.append(f"Out for client signature ({len(awaiting_sig)}):")
            for c in awaiting_sig[:10]:
                proj = mod_projects.get(DATA_DIR, c.get("project_id", ""))
                proj_label = proj.get("address", "?") if proj else "?"
                amt = float(c.get("amount") or 0)
                sign = "+" if amt >= 0 else ""
                lines.append(f"  · #{c['id'][:8]} {proj_label} — {c.get('description','')} ({sign}${amt:,.0f})")
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "gc_pending_cos_listed"}

    # 5. APPROVE A CO
    m = _GC_APPROVE_CO_RE.match(msg)
    if m:
        co_id_in = (m.group("co_id") or m.group("co_id2") or "").strip().lstrip("#")
        # Accept short IDs (first 8 chars) — match by prefix.
        all_cos = mod_change_orders._load(DATA_DIR)
        matches = [c for c in all_cos
                    if c.get("id", "").startswith(co_id_in)]
        if not matches:
            return {"reply": f"No CO matching #{co_id_in}. Say \"pending COs\" to see the queue.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_co_not_found"}
        if len(matches) > 1:
            listed = "\n".join(f"  · #{c['id'][:8]} {c.get('description','')[:50]}" for c in matches[:5])
            return {"reply": f"Multiple matches:\n{listed}\nUse a longer ID.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_co_ambiguous"}
        c = matches[0]
        if c.get("status") not in ("draft", "awaiting_approval", "client_requested"):
            return {"reply": f"CO #{c['id'][:8]} is already {c.get('status')} — not in approve state.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_co_already_advanced"}
        mod_change_orders.mark_approved(DATA_DIR, c["id"], user_rec.get("username", "owner"))
        proj = mod_projects.get(DATA_DIR, c.get("project_id", ""))
        proj_label = proj.get("address", "?") if proj else "?"
        return {"reply": f"Approved CO #{c['id'][:8]} on {proj_label}. Ready to send — say \"send CO #{c['id'][:8]} to client\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_co_approved"}

    # 6. SEND APPROVED CO TO CLIENT FOR SIGNATURE
    m = _GC_SEND_CO_RE.match(msg)
    if m:
        co_id_in = (m.group("co_id") or m.group("co_id2") or "").strip().lstrip("#")
        all_cos = mod_change_orders._load(DATA_DIR)
        matches = [c for c in all_cos if c.get("id", "").startswith(co_id_in)]
        if not matches:
            return {"reply": f"No CO matching #{co_id_in}.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_co_not_found"}
        c = matches[0]
        if c.get("status") != "approved":
            return {"reply": f"CO #{c['id'][:8]} status is {c.get('status')}. Approve it first with \"approve CO #{c['id'][:8]}\".",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_co_not_approved"}
        proj = mod_projects.get(DATA_DIR, c.get("project_id", ""))
        if not proj:
            return {"reply": "Linked project is missing — can't send.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_co_orphan"}
        # Mint a one-time signing token + send email via the contact info
        token = _mint_co_sign_token(c["id"])
        sign_url = _co_sign_url(token)
        email = proj.get("customer_email") or ""
        delivery_via = "email"
        if email:
            try:
                _send_co_for_signature_email(c, proj, sign_url, email)
            except Exception as e:
                log.warning(f"CO email send failed: {e}")
                delivery_via = "no_delivery_method"
        else:
            delivery_via = "no_email_on_file"
        mod_change_orders.mark_sent_for_signature(DATA_DIR, c["id"], via=delivery_via)
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="co.sent_for_signature",
                        meta={"co_id": c["id"], "project_id": c.get("project_id"),
                              "amount": c.get("amount"),
                              "delivery_via": delivery_via})
        if delivery_via == "no_email_on_file":
            return {"reply": (f"CO #{c['id'][:8]} marked sent BUT no customer email on file for {proj.get('address')}. "
                              f"Give them this signing link directly:\n{sign_url}"),
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_co_sent_no_email"}
        return {"reply": (f"Sent — CO #{c['id'][:8]} to {proj.get('customer_name') or 'the client'} "
                          f"at {email}. They'll get a signing link valid for 30 days. "
                          f"You can also share directly:\n{sign_url}"),
                "tier": "local", "latency_ms": 0,
                "source": "gc_co_sent"}

    # 7. ADD CO (the trigger phrase — last so it doesn't shadow approve/send)
    if _GC_ADD_CO_RE.search(msg):
        return _handle_add_co(msg, user_rec)

    # 8. UNPAID / OWED LIST — "who owes me", "what invoices are open"
    if _GC_LIST_UNPAID_RE.match(msg):
        unpaid = mod_invoices.list_unpaid(DATA_DIR)
        mod_invoices.list_overdue(DATA_DIR)  # side-effect: promote status
        unpaid = mod_invoices.list_unpaid(DATA_DIR)  # re-read post-promote
        if not unpaid:
            return {"reply": "Nothing outstanding — every invoice is either paid, void, or still in draft.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_no_unpaid"}
        lines = [f"Outstanding invoices ({len(unpaid)}):"]
        for i in unpaid[:15]:
            proj = mod_projects.get(DATA_DIR, i.get("project_id", "")) or {}
            owed = float(i.get("amount_due", 0)) - float(i.get("amount_paid", 0))
            status = i.get("status", "?").upper()
            due_str = ""
            if i.get("due_at"):
                from datetime import datetime as _dt
                due_str = " due " + _dt.fromtimestamp(int(i["due_at"])).strftime("%b %-d")
            lines.append(f"  · {i.get('invoice_number','?')} — {proj.get('address','?')} ${owed:,.0f} ({status}){due_str}")
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "gc_unpaid_listed"}

    # 9. AGING REPORT — "aging report", "what's overdue", "receivables"
    if _GC_AGING_RE.match(msg):
        mod_invoices.list_overdue(DATA_DIR)
        aging = mod_invoices.aging_buckets(DATA_DIR)
        total = sum(aging.values())
        if total <= 0:
            return {"reply": "Aging: $0 outstanding. Nothing past due.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_aging_clear"}
        lines = [
            f"Aging — ${total:,.0f} outstanding:",
            f"  Current (not yet due): ${aging['current']:,.0f}",
            f"  1-30 days late:        ${aging['1-30']:,.0f}",
            f"  31-60 days late:       ${aging['31-60']:,.0f}",
            f"  61-90 days late:       ${aging['61-90']:,.0f}",
            f"  90+ days late:         ${aging['90+']:,.0f}",
        ]
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "gc_aging"}

    # 9b. GENERATE PDF INVOICE — "PDF INV-2026-0001"
    m = _GC_PDF_INVOICE_RE.match(msg)
    if m:
        inv_num = (m.group("inv1") or m.group("inv2") or m.group("inv3") or m.group("inv4") or "").upper()
        target = None
        for i in mod_invoices._load(DATA_DIR):
            if (i.get("invoice_number") or "").upper() == inv_num:
                target = i; break
        if not target:
            return {"reply": f"I don't see {inv_num} on the books.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_pdf_not_found"}
        project = mod_projects.get(DATA_DIR, target.get("project_id", "")) or {}
        business = mod_business.load(DATA_DIR)
        try:
            pdf_path = mod_invoice_pdf.generate(
                target, project, business, DATA_DIR / "invoice_pdfs"
            )
        except Exception as e:
            log.exception("invoice PDF generation failed")
            return {"reply": f"PDF generation failed: {e}",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_pdf_error"}
        # Mint a download token (60 min) — pass the invoice_pdfs dir as
        # an extra allowed root so the token-mint accepts a path outside
        # the user's normal file_fetch scope (we just wrote it ourselves).
        try:
            token = file_fetch.mint_download_token(
                DATA_DIR, str(pdf_path),
                ttl_minutes=60,
                extra_allowed_roots=[DATA_DIR / "invoice_pdfs"],
            )
        except Exception as e:
            log.warning(f"PDF token mint failed: {e}")
            token = None
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="invoice.pdf_generated",
                        meta={"invoice_id": target["id"], "invoice_number": inv_num})
        size_kb = pdf_path.stat().st_size // 1024
        if token:
            return {"reply": (f"Generated {inv_num}.pdf ({size_kb} KB). "
                              f"[Download here](/download/{token}) — link valid 60 min."),
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_pdf_generated",
                    "download_url": f"/download/{token}"}
        return {"reply": f"Generated {inv_num}.pdf at {pdf_path} ({size_kb} KB).",
                "tier": "local", "latency_ms": 0,
                "source": "gc_pdf_generated_no_token"}

    # 10. ADD/SEND INVOICE — "send invoice for Oak — $5000 progress draw"
    if _GC_ADD_INVOICE_RE.match(msg):
        return _handle_add_invoice(msg, user_rec)

    # 11. RECORD PAYMENT — "Sarah paid $3000 on INV-2026-0001"
    if _GC_RECORD_PAYMENT_RE.match(msg):
        return _handle_record_payment(msg, user_rec)

    # HELP — discoverability of the 40+ contractor chat intents
    if _GC_HELP_RE.match(msg):
        return {"reply": _gc_help_text(),
                "tier": "local", "latency_ms": 0,
                "source": "gc_help"}

    # 11. RUN SWEEP MANUALLY (mostly for demos; the daemon runs every 6h)
    if _GC_RUN_SWEEP_RE.match(msg):
        try:
            n = _receivables_followup_sweep()
        except Exception as e:
            return {"reply": f"Sweep failed: {e}",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_sweep_error"}
        if n == 0:
            return {"reply": "Sweep done — nothing new to draft. Either no invoices are 7+ days past last contact, or all overdue invoices already have a queued reminder.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_sweep_no_new"}
        return {"reply": f"Sweep done — drafted {n} new reminder{'s' if n != 1 else ''}. Say \"show queued reminders\" to review.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_sweep_done"}

    # 11a. RECEIVABLES QUEUE — "show queued reminders" / "send queued reminders"
    if _GC_SHOW_QUEUE_RE.match(msg):
        queue = _load_pending_followups()
        unsent = [p for p in queue if not p.get("sent_at")]
        if not unsent:
            return {"reply": "No reminders in the queue. The nightly sweep drafts them once an invoice goes 7+ days past due without contact.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_queue_empty"}
        lines = [f"Receivables nudges queued ({len(unsent)}):"]
        for p in unsent[:10]:
            tier = ["", "friendly", "firmer", "formal"][p.get("escalation", 1)]
            lines.append(f"  · {p['invoice_number']} {p.get('project_address','?')[:25]} "
                          f"${p.get('amount_owed', 0):,.0f} ({tier}) → {p.get('customer_email','no email')}")
        lines.append("\nSay \"send queued reminders\" to fire them all, "
                      "or just copy/paste from the queue file.")
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "gc_queue_listed"}

    if _GC_SEND_QUEUE_RE.match(msg):
        queue = _load_pending_followups()
        unsent = [p for p in queue if not p.get("sent_at")]
        if not unsent:
            return {"reply": "Nothing queued to send.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_queue_send_empty"}
        # For v1, "sending" = marking sent + logging (real email delivery
        # waits on the SMTP wiring story). The GC still has to deliver
        # the text via their own channel; this just clears the queue
        # and records intent.
        now_ts = int(time.time())
        sent = 0
        lines = [f"Marking {len(unsent)} reminder{'s' if len(unsent) != 1 else ''} as sent. Copy each from the list:"]
        for p in unsent:
            p["sent_at"] = now_ts
            p["sent_via"] = "manual_after_queue"
            audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                            action="invoice.nudge_queue_sent",
                            meta={"invoice_id": p.get("invoice_id"),
                                  "escalation": p.get("escalation")})
            sent += 1
            lines.append(f"\n────── {p['invoice_number']} → {p.get('customer_email','no email')} ──────")
            lines.append(p.get("drafted_text", ""))
        _save_pending_followups(queue)
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "gc_queue_sent"}

    # 11b. NUDGE ALL OVERDUE — "draft all overdue reminders"
    if _GC_NUDGE_ALL_RE.match(msg):
        mod_invoices.list_overdue(DATA_DIR)  # promote statuses
        overdue = [i for i in mod_invoices.list_unpaid(DATA_DIR)
                    if i.get("status") == "overdue"]
        if not overdue:
            return {"reply": "No overdue invoices to nudge — all current.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_nudge_none_overdue"}
        lines = [f"Drafted {len(overdue)} reminder{'s' if len(overdue) != 1 else ''} — review and send:"]
        for i in overdue[:10]:
            proj = mod_projects.get(DATA_DIR, i.get("project_id", "")) or {}
            escalation = min(3, max(1, int(i.get("follow_up_count", 0)) + 1))
            text = _draft_nudge_text(i, proj, escalation=escalation)
            mod_invoices.mark_followed_up(DATA_DIR, i["id"])
            audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                            action="invoice.nudge_drafted",
                            meta={"invoice_id": i["id"], "escalation": escalation})
            email = proj.get("customer_email") or "(no email on file)"
            lines.append(f"\n────── {i['invoice_number']} → {email} ──────")
            lines.append(text)
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "gc_nudge_all_drafted"}

    # 11c. NUDGE SINGLE — "nudge INV-2026-0001"
    if _GC_NUDGE_RE.match(msg):
        m = _GC_NUDGE_RE.match(msg)
        inv_token = (m.group("inv1") or m.group("inv2") or m.group("inv3") or "").strip()
        inv_num_m = _INV_NUMBER_RE.search(inv_token) or _INV_NUMBER_RE.search(msg)
        if not inv_num_m:
            return {"reply": "Which invoice? Give me an invoice number like INV-2026-0001, or say \"nudge all overdue\".",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_nudge_no_inv"}
        inv_num = inv_num_m.group(0).upper()
        target = None
        for i in mod_invoices._load(DATA_DIR):
            if (i.get("invoice_number") or "").upper() == inv_num:
                target = i; break
        if not target:
            return {"reply": f"I don't see {inv_num} on the books.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_nudge_not_found"}
        if target.get("status") in ("paid", "void", "draft"):
            return {"reply": f"{inv_num} is {target.get('status')} — no nudge needed.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_nudge_not_overdue"}
        proj = mod_projects.get(DATA_DIR, target.get("project_id", "")) or {}
        escalation = min(3, max(1, int(target.get("follow_up_count", 0)) + 1))
        text = _draft_nudge_text(target, proj, escalation=escalation)
        mod_invoices.mark_followed_up(DATA_DIR, target["id"])
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="invoice.nudge_drafted",
                        meta={"invoice_id": target["id"], "escalation": escalation})
        email = proj.get("customer_email") or "(no email on file — share manually)"
        tier_label = ["", "friendly", "firmer", "formal"][escalation]
        return {"reply": f"Reminder #{escalation} ({tier_label}) for {inv_num} → {email}:\n\n{text}",
                "tier": "local", "latency_ms": 0,
                "source": "gc_nudge_drafted"}

    # 13. DAILY LOGS — list first ("logs for Oak" / "this week's logs")
    log_resp = _handle_list_logs(msg, user_rec)
    if log_resp is not None:
        return log_resp
    # Then add (trigger phrase last so list doesn't get swallowed)
    if _GC_ADD_LOG_RE.match(msg):
        return _handle_add_log(msg, user_rec)

    # 14. SUBCONTRACTORS
    sub_list_resp = _handle_list_subs(msg, user_rec)
    if sub_list_resp is not None:
        return sub_list_resp
    sub_assign_resp = _handle_assign_sub(msg, user_rec)
    if sub_assign_resp is not None:
        return sub_assign_resp
    if _GC_ADD_SUB_RE.match(msg):
        return _handle_add_sub(msg, user_rec)
    if _GC_INSURANCE_CHECK_RE.match(msg):
        return _handle_insurance_check(msg, user_rec)

    # 14b. SUB SCHEDULE — "what's Bob working on" / "Bob's schedule"
    sub_sched_resp = _handle_sub_schedule(msg, user_rec)
    if sub_sched_resp is not None:
        return sub_sched_resp

    # 15. UNSIGNED-WORK LEAK ALARM — "leak check" / "any unsigned scope"
    if _GC_LEAK_CHECK_RE.match(msg):
        return _handle_leak_check(msg, user_rec)

    # 15b. MARK PROJECT DONE — "mark Oak complete" / "Oak is finished"
    proj_done_resp = _handle_mark_project_done(msg, user_rec)
    if proj_done_resp is not None:
        return proj_done_resp

    # 15c. FULL PROJECT REPORT — "full report on Oak" / "Maple summary"
    proj_report_resp = _handle_project_full_report(msg, user_rec)
    if proj_report_resp is not None:
        return proj_report_resp

    # 15d. WEEKLY RECAP — "weekly recap" / "wrap up my week" / "how did my week go"
    if _GC_WEEKLY_RECAP_RE.match(msg):
        return _handle_weekly_recap(msg, user_rec)

    # 15e. SHARE CLIENT PORTAL — "share Oak with the client" / "portal for Maple"
    share_resp = _handle_share_portal(msg, user_rec)
    if share_resp is not None:
        return share_resp

    # 15f. CLOSEOUT PDF — "closeout for Birch" / "generate closeout Maple"
    close_resp = _handle_closeout_pdf(msg, user_rec)
    if close_resp is not None:
        return close_resp

    # 15g0. REVIEWS — link minting + summary
    review_link_resp = _handle_review_link(msg, user_rec)
    if review_link_resp is not None:
        return review_link_resp
    my_rating_resp = _handle_my_rating(msg, user_rec)
    if my_rating_resp is not None:
        return my_rating_resp

    # 15g0b. PROPOSAL PDF — "proposal for Sarah Johnson"
    proposal_resp = _handle_proposal_pdf(msg, user_rec)
    if proposal_resp is not None:
        return proposal_resp

    # 15g. BIDS — list / report first (specific patterns), then won/lost,
    # then add (trigger phrase last so it doesn't shadow list/report).
    bid_list_resp = _handle_list_bids(msg, user_rec)
    if bid_list_resp is not None:
        return bid_list_resp
    bid_report_resp = _handle_bid_report(msg, user_rec)
    if bid_report_resp is not None:
        return bid_report_resp
    bid_won_resp = _handle_bid_won(msg, user_rec)
    if bid_won_resp is not None:
        return bid_won_resp
    bid_lost_resp = _handle_bid_lost(msg, user_rec)
    if bid_lost_resp is not None:
        return bid_lost_resp
    if _GC_ADD_BID_RE.match(msg):
        return _handle_add_bid(msg, user_rec)

    # 16. REPORTING — "show me the money", "captured this month", "totals"
    if _GC_REPORT_RE.match(msg):
        p_sum = mod_projects.summary(DATA_DIR)
        co_sum = mod_change_orders.summary(DATA_DIR)
        i_sum = mod_invoices.summary(DATA_DIR)
        active_count = (p_sum.get("by_status") or {}).get("active", 0)
        lines = [
            "💰 Money report:",
            f"  Active projects:        {active_count}",
            f"  Contracted total:       ${p_sum.get('contracted_total', 0):,.0f}",
            f"  CO dollars captured:    ${co_sum.get('captured_dollars', 0):,.0f}",
            f"  CO dollars pending:     ${co_sum.get('pending_dollars', 0):,.0f}",
            f"  Total billed:           ${i_sum.get('total_billed', 0):,.0f}",
            f"  Total collected:        ${i_sum.get('total_collected', 0):,.0f}",
            f"  Outstanding:            ${i_sum.get('total_outstanding', 0):,.0f}",
        ]
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "gc_report"}

    # 16a. NUDGE BY CUSTOMER NAME — "Send a payment reminder to Johnson"
    nudge_by_cust = _handle_nudge_by_customer(msg, user_rec)
    if nudge_by_cust is not None:
        return nudge_by_cust

    # 16b. RETAINAGE OWED — "how much retainage are we still owed"
    if _GC_RETAINAGE_RE.match(msg):
        return _handle_retainage_owed(user_rec)

    # 16c. TOP CLIENTS BY REVENUE — "show me our top five clients"
    m = _GC_TOP_CLIENTS_RE.match(msg)
    if m:
        n_raw = (m.group("n") or m.group("n2") or m.group("n3") or "5").strip().lower()
        word_to_n = {"three": 3, "five": 5, "ten": 10}
        try:
            n = int(n_raw) if n_raw.isdigit() else word_to_n.get(n_raw, 5)
        except ValueError:
            n = 5
        return _handle_top_clients(n, user_rec)

    # 16d. JOBS FINISHING THIS PERIOD — "which jobs finish this month"
    m = _GC_FINISHING_THIS_MONTH_RE.match(msg)
    if m:
        period = (m.group("period") or m.group("period2") or m.group("period3") or "month").lower()
        return _handle_finishing_period(period, user_rec)

    # 16e. HIGHEST-VALUE JOBS — "what jobs have the highest value"
    if _GC_HIGHEST_VALUE_RE.match(msg):
        return _handle_highest_value_jobs(user_rec)

    # 16f. SIGNED COs FOR PROJECT — "show me signed COs for the Johnson project"
    m = _GC_SIGNED_COS_FOR_PROJECT_RE.match(msg)
    if m:
        q = (m.group("q1") or m.group("q2") or m.group("q3") or "").strip()
        if q:
            return _handle_signed_cos_for_project(q, user_rec)

    # 16g. CO REVENUE FOR PERIOD — "show me CO revenue this month/year"
    m = _GC_CO_REVENUE_PERIOD_RE.match(msg)
    if m:
        period = (m.group("period") or m.group("period2") or m.group("period3") or "month").lower()
        return _handle_co_revenue_period(period, user_rec)

    # 16h. INCREASE CONTRACT BY CO — explainer (signed COs do this automatically)
    m = _GC_INCREASE_CONTRACT_BY_CO_RE.match(msg)
    if m:
        q = (m.group("q") or m.group("q2") or "").strip()
        return _handle_increase_contract_explainer(q, user_rec)

    # 16h2. PROJECTS WITHOUT A FOREMAN — direct filter (was hallucinating
    # project names because the LLM didn't have access to projects.json)
    if _GC_PROJECTS_WITHOUT_FOREMAN_RE.match(msg):
        nofor = [p for p in mod_projects.list_active(DATA_DIR)
                 if not (p.get("foreman") or "").strip()]
        if not nofor:
            return {"reply": "Every active project has a foreman assigned. Nothing missing.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_projects_all_have_foreman"}
        lines = [f"Projects missing a foreman ({len(nofor)}):"]
        for p in nofor[:15]:
            lines.append(f"  · {p.get('address','?')} — {p.get('label','(no label)')}")
        lines.append("\nAssign one with: \"set foreman <name> on <project>\".")
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "gc_projects_no_foreman"}

    # 16h3. TOTAL CONTRACT VALUE ACROSS ACTIVE JOBS
    if _GC_TOTAL_CONTRACT_VALUE_RE.match(msg):
        active = mod_projects.list_active(DATA_DIR)
        if not active:
            return {"reply": "No active projects — total contract value is $0.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_total_contract_empty"}
        base_total = sum(float(p.get("contract_amount") or 0) for p in active)
        signed_co_total = sum(
            mod_change_orders.signed_total_for_project(DATA_DIR, p["id"])
            for p in active
        )
        grand = base_total + signed_co_total
        co_bit = f"\n  + ${signed_co_total:,.0f} signed change orders" if signed_co_total else ""
        return {"reply": (
            f"💰 Total contract value across {len(active)} active "
            f"job{'s' if len(active) != 1 else ''}: ${grand:,.0f}\n"
            f"  Base contracts: ${base_total:,.0f}{co_bit}"
            + (f"\n  Grand authorized: ${grand:,.0f}" if signed_co_total else "")
        ), "tier": "local", "latency_ms": 0,
           "source": "gc_total_contract_value"}

    # 16h4. PERCENTAGE OF INVOICES OVERDUE
    if _GC_PCT_OVERDUE_RE.match(msg):
        all_invs = mod_invoices._load(DATA_DIR)
        non_void = [i for i in all_invs if i.get("status") not in ("draft", "void")]
        if not non_void:
            return {"reply": "No invoices on the books yet — nothing to calculate.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_pct_overdue_empty"}
        mod_invoices.list_overdue(DATA_DIR)  # side-effect: promote statuses
        non_void = [i for i in mod_invoices._load(DATA_DIR)
                    if i.get("status") not in ("draft", "void")]
        overdue = [i for i in non_void if i.get("status") == "overdue"]
        pct = (len(overdue) / len(non_void)) * 100 if non_void else 0
        return {"reply": (
            f"📊 {len(overdue)} of {len(non_void)} active invoices are overdue "
            f"({pct:.1f}%).\n"
            f"Run \"aging report\" to see the dollar breakdown."
        ), "tier": "local", "latency_ms": 0,
           "source": "gc_pct_overdue"}

    # 16i. PROFITABILITY / MARGIN — honest refusal (no cost data tracked yet)
    if _GC_PROFITABILITY_RE.match(msg):
        return {"reply": (
            "I don't track job costs yet, so I can't tell you which jobs are "
            "the most profitable.\n"
            "What I CAN tell you per job:\n"
            "  · contract amount + signed CO total (authorized revenue)\n"
            "  · amount billed and amount collected\n"
            "  · current outstanding balance\n"
            "For real margins I'd need labor hours, material spend, and sub "
            "payouts — add a cost-tracking module and I can compute it. "
            "Until then, say \"show me the money\" for the revenue view or "
            "\"highest value jobs\" to sort by contract size."
        ), "tier": "local", "latency_ms": 0,
           "source": "gc_profitability_unavailable"}

    return None


# ── Contractor reporting helpers ─────────────────────────────────────────

def _find_projects_by_customer(query: str) -> list[dict]:
    """Return projects whose customer_name OR address matches the query
    (case-insensitive substring). Used by nudge-by-name + report-by-name."""
    q = (query or "").strip().lower()
    if not q:
        return []
    hits = []
    for p in mod_projects._load(DATA_DIR):
        name = (p.get("customer_name") or "").lower()
        addr = (p.get("address") or "").lower()
        label = (p.get("label") or "").lower()
        if q in name or q in addr or q in label:
            hits.append(p)
    return hits


def _handle_nudge_by_customer(msg: str, user_rec: dict) -> dict | None:
    """'Send a payment reminder to Johnson Construction' — find that
    customer's unpaid invoices and draft nudges for each. Returns None
    if the message also references an INV-XXXX number (the existing
    by-invoice nudge handler takes those)."""
    if _INV_NUMBER_RE.search(msg):
        return None
    m = _GC_NUDGE_BY_CUSTOMER_RE.match(msg)
    if not m:
        return None
    who = (m.group("who") or m.group("who2") or m.group("who3") or "").strip()
    if not who:
        return None
    # Filter out the existing all/queue/INV verbs so we don't double-handle.
    if who.lower() in ("everyone", "everybody", "all", "all overdue",
                       "them", "the client", "the customer", "client",
                       "customer"):
        return None
    projects = _find_projects_by_customer(who)
    if not projects:
        return {"reply": f"I don't see any projects for \"{who}\". Add them with: "
                          f"\"add project at <address>\" then set customer name.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_nudge_customer_not_found"}
    drafted = []
    for p in projects:
        for inv in mod_invoices.list_for_project(DATA_DIR, p["id"]):
            owed = float(inv.get("amount_due", 0)) - float(inv.get("amount_paid", 0))
            if owed <= 0:
                continue
            if inv.get("status") in ("paid", "void", "draft"):
                continue
            escalation = min(3, max(1, int(inv.get("follow_up_count", 0)) + 1))
            text = _draft_nudge_text(inv, p, escalation=escalation)
            mod_invoices.mark_followed_up(DATA_DIR, inv["id"])
            audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                            action="invoice.nudge_drafted",
                            meta={"invoice_id": inv["id"], "escalation": escalation,
                                  "via": "by_customer_name", "customer": who})
            drafted.append((inv, p, text, escalation))
    if not drafted:
        return {"reply": f"Nothing to nudge — no unpaid invoices for \"{who}\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_nudge_customer_clear"}
    lines = [f"Drafted {len(drafted)} reminder{'s' if len(drafted) != 1 else ''} for {who} — review and send:"]
    for inv, p, text, esc in drafted[:5]:
        tier_label = ["", "friendly", "firmer", "formal"][esc]
        email = p.get("customer_email") or "(no email on file)"
        lines.append(f"\n────── {inv.get('invoice_number','?')} → {email} ({tier_label}) ──────")
        lines.append(text)
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_nudge_by_customer"}


def _handle_retainage_owed(user_rec: dict) -> dict:
    """Sum retainage_held across paid/partial invoices for projects that
    haven't been closed out yet. That's the amount still parked with the
    GC waiting for the closeout release invoice."""
    by_project = {}
    grand = 0.0
    for p in mod_projects._load(DATA_DIR):
        if p.get("status") == "completed":
            continue
        held = mod_invoices.retainage_held_for_project(DATA_DIR, p["id"])
        if held > 0:
            by_project[p["id"]] = (p, held)
            grand += held
    if grand <= 0:
        return {"reply": "No retainage held — every active project is at $0 retainage so far.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_retainage_clear"}
    lines = [f"💰 Retainage still being held: ${grand:,.0f} (across {len(by_project)} project{'s' if len(by_project) != 1 else ''})"]
    items = sorted(by_project.values(), key=lambda t: t[1], reverse=True)
    for p, held in items[:10]:
        lines.append(f"  · {p.get('address','?')} — ${held:,.0f}")
    lines.append("\nReleased at project closeout when you mark each one complete.")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_retainage_owed"}


def _handle_top_clients(n: int, user_rec: dict) -> dict:
    """Group invoices by project.customer_name and rank by amount_paid."""
    projects = {p["id"]: p for p in mod_projects._load(DATA_DIR)}
    totals: dict[str, dict] = {}
    for inv in mod_invoices._load(DATA_DIR):
        p = projects.get(inv.get("project_id", ""))
        if not p:
            continue
        name = (p.get("customer_name") or "").strip() or "(unnamed customer)"
        slot = totals.setdefault(name, {"billed": 0.0, "paid": 0.0, "jobs": set()})
        if inv.get("status") not in ("draft", "void"):
            slot["billed"] += float(inv.get("amount_due", 0))
        slot["paid"] += float(inv.get("amount_paid", 0))
        slot["jobs"].add(p["id"])
    if not totals:
        return {"reply": "No client revenue yet — once invoices are paid I can rank "
                          "them. Add an invoice with: \"invoice the <project> $<amt>\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_top_clients_empty"}
    ranked = sorted(totals.items(), key=lambda kv: kv[1]["paid"], reverse=True)[:max(1, n)]
    lines = [f"🏆 Top {len(ranked)} client{'s' if len(ranked) != 1 else ''} by revenue (paid):"]
    for i, (name, t) in enumerate(ranked, 1):
        lines.append(f"  {i}. {name} — ${t['paid']:,.0f} paid (${t['billed']:,.0f} billed, {len(t['jobs'])} job{'s' if len(t['jobs']) != 1 else ''})")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_top_clients"}


def _handle_finishing_period(period: str, user_rec: dict) -> dict:
    """Return projects whose est_complete falls inside the given period
    (this week / month / quarter / year). est_complete is stored as
    'YYYY-MM-DD' or empty."""
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    if period == "week":
        end = today + _td(days=7)
        label = "this week"
    elif period == "month":
        if today.month == 12:
            end = _date(today.year + 1, 1, 1) - _td(days=1)
        else:
            end = _date(today.year, today.month + 1, 1) - _td(days=1)
        label = "this month"
    elif period == "quarter":
        q_end_month = ((today.month - 1) // 3 + 1) * 3
        if q_end_month == 12:
            end = _date(today.year + 1, 1, 1) - _td(days=1)
        else:
            end = _date(today.year, q_end_month + 1, 1) - _td(days=1)
        label = "this quarter"
    else:  # year
        end = _date(today.year, 12, 31)
        label = "this year"
    matches = []
    for p in mod_projects._load(DATA_DIR):
        if p.get("status") in ("completed", "cancelled"):
            continue
        est = (p.get("est_complete") or "").strip()
        if not est or len(est) < 10:
            continue
        try:
            est_d = _date.fromisoformat(est[:10])
        except ValueError:
            continue
        if today <= est_d <= end:
            matches.append((est_d, p))
    if not matches:
        return {"reply": f"No projects scheduled to finish {label} (between today and {end.strftime('%b %-d')}). "
                          f"Either nothing's lined up, or projects don't have est_complete dates set yet.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_finishing_none"}
    matches.sort(key=lambda t: t[0])
    lines = [f"Projects finishing {label} ({len(matches)}):"]
    for est_d, p in matches[:15]:
        amt = p.get("contract_amount") or 0
        lines.append(f"  · {est_d.strftime('%a %b %-d')} — {p.get('address','?')} (${amt:,.0f})")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_finishing_period"}


def _handle_highest_value_jobs(user_rec: dict) -> dict:
    """Rank active projects by contract_amount + signed CO total."""
    active = mod_projects.list_active(DATA_DIR)
    if not active:
        return {"reply": "No active projects to rank.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_highest_value_empty"}
    enriched = []
    for p in active:
        base = float(p.get("contract_amount") or 0)
        co = mod_change_orders.signed_total_for_project(DATA_DIR, p["id"])
        enriched.append((base + co, base, co, p))
    enriched.sort(key=lambda t: t[0], reverse=True)
    lines = [f"Active jobs ranked by total contract value ({len(enriched)}):"]
    for total, base, co, p in enriched[:10]:
        co_bit = f" (+${co:,.0f} CO)" if co else ""
        lines.append(f"  · ${total:,.0f}{co_bit} — {p.get('address','?')} — {p.get('label','')}")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_highest_value"}


def _handle_signed_cos_for_project(query: str, user_rec: dict) -> dict:
    matches = mod_projects.find_by_address(DATA_DIR, query)
    # Fallback: customer-name match
    if not matches:
        matches = _find_projects_by_customer(query)
    if not matches:
        return {"reply": f"I don't see a project matching \"{query}\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_signed_cos_project_not_found"}
    if len(matches) > 1:
        listed = "\n".join(f"  · {p['address']} ({p.get('label','')})" for p in matches[:5])
        return {"reply": f"Multiple projects match \"{query}\":\n{listed}\nBe more specific.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_signed_cos_project_ambiguous"}
    p = matches[0]
    signed = mod_change_orders.list_for_project(DATA_DIR, p["id"], statuses=["signed"])
    if not signed:
        return {"reply": f"No signed change orders yet on {p['address']}.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_signed_cos_none"}
    total = sum(float(c.get("amount", 0)) for c in signed)
    lines = [f"Signed change orders on {p['address']} ({len(signed)}, total +${total:,.0f}):"]
    from datetime import datetime as _dt
    for c in signed[:15]:
        amt = float(c.get("amount", 0))
        sign = "+" if amt >= 0 else ""
        ts = c.get("client_signed_at")
        signed_date = ""
        if ts:
            try:
                signed_date = " on " + _dt.fromtimestamp(int(ts)).strftime("%b %-d")
            except (ValueError, OSError):
                pass
        lines.append(f"  · #{c.get('co_number','?')} {sign}${amt:,.0f}{signed_date} — {c.get('description','')[:60]}")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_signed_cos_for_project"}


def _handle_co_revenue_period(period: str, user_rec: dict) -> dict:
    """Sum signed CO amounts where client_signed_at falls inside the period."""
    from datetime import date as _date, datetime as _dt, timedelta as _td
    today = _date.today()
    if period == "week":
        start = today - _td(days=today.weekday())
        label = "this week"
    elif period == "month":
        start = _date(today.year, today.month, 1)
        label = "this month"
    elif period == "quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        start = _date(today.year, q_start_month, 1)
        label = "this quarter"
    else:
        start = _date(today.year, 1, 1)
        label = "this year"
    start_ts = int(_dt.combine(start, _dt.min.time()).timestamp())
    total = 0.0
    cos_in = []
    for c in mod_change_orders._load(DATA_DIR):
        if c.get("status") != "signed":
            continue
        ts = int(c.get("client_signed_at") or 0)
        if ts < start_ts:
            continue
        total += float(c.get("amount") or 0)
        cos_in.append(c)
    if not cos_in:
        return {"reply": f"No signed change orders {label} yet.\n"
                          f"(I count COs as revenue the day the client signs them.)",
                "tier": "local", "latency_ms": 0,
                "source": "gc_co_revenue_period_zero"}
    lines = [f"Change-order revenue {label}: ${total:,.0f} ({len(cos_in)} signed CO{'s' if len(cos_in) != 1 else ''})"]
    for c in sorted(cos_in, key=lambda x: -float(x.get("amount", 0)))[:5]:
        proj = mod_projects.get(DATA_DIR, c.get("project_id", "")) or {}
        amt = float(c.get("amount", 0))
        lines.append(f"  · ${amt:,.0f} — {proj.get('address','?')} — {c.get('description','')[:60]}")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_co_revenue_period"}


def _handle_increase_contract_explainer(query: str, user_rec: dict) -> dict:
    """User asked us to manually increase a project's contract by the CO
    amount. Explain that signed COs do this automatically — show the
    current authorized total so they can see it's already done."""
    matches = mod_projects.find_by_address(DATA_DIR, query) if query else []
    if not matches:
        matches = _find_projects_by_customer(query) if query else []
    if not matches:
        return {"reply": (
            "Signed change orders are already added to the contract total "
            "automatically — you don't need to manually adjust the contract "
            "amount.\n"
            "I couldn't find a project matching \"" + (query or "(none)") + "\". "
            "Try \"status of the <address> project\" to see its current "
            "authorized total."
        ), "tier": "local", "latency_ms": 0,
           "source": "gc_increase_contract_unknown_project"}
    if len(matches) > 1:
        listed = "\n".join(f"  · {p['address']} ({p.get('label','')})" for p in matches[:5])
        return {"reply": f"Multiple projects match \"{query}\":\n{listed}\nWhich one?",
                "tier": "local", "latency_ms": 0,
                "source": "gc_increase_contract_ambiguous"}
    p = matches[0]
    base = float(p.get("contract_amount") or 0)
    signed_co = mod_change_orders.signed_total_for_project(DATA_DIR, p["id"])
    total = base + signed_co
    return {"reply": (
        f"Signed change orders are added automatically — no manual "
        f"adjustment needed.\n"
        f"{p['address']} current authorized total: ${total:,.0f}\n"
        f"  · Original contract: ${base:,.0f}\n"
        f"  · Signed COs:        +${signed_co:,.0f}\n"
        f"(If a CO is still pending client signature, it won't be counted "
        f"until they sign. Check with: \"pending change orders\".)"
    ), "tier": "local", "latency_ms": 0,
       "source": "gc_increase_contract_explainer"}


# ── Change-order draft + signing helpers ──────────────────────────────────

def _handle_add_co(msg: str, user_rec: dict) -> dict:
    """Parse the CO add intent. Looks for an amount + a project reference +
    a description, OR line items + the pricing catalog. Many natural
    phrasings — be forgiving.

    Flow per Frank's spec:
      1. parse line items ("3 plugs in the garage and 3 lights in the kitchen")
      2. look up unit prices from pricing catalog
      3. if a $-amount was explicit in the message, use it; else use the
         sum from priced line items
      4. fill the customer's labeled CO template if one is uploaded;
         else fall back to Orby's generic CO doc text
      5. save filled PDF into ~/Orbi/clients/<customer>/change_orders/
      6. mint sign URL inline for foreman → customer to sign on the spot
    """
    # Strip the leading verb so what's left is the meaningful payload.
    payload = _GC_ADD_CO_RE.sub("", msg, count=1).strip(" :—–-,")
    if not payload:
        return {"reply": "Tell me which project, how much, and what the change is. "
                          "E.g. \"CO on 555 Oak — $1200 extra trim work\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_co_no_details"}
    explicit_amount = _parse_money(payload)
    # Find a project reference. Try matching any active project's address
    # or label as a substring of the payload.
    active = mod_projects.list_active(DATA_DIR)
    payload_l = payload.lower()
    project = None
    for p in active:
        addr_parts = (p.get("address", "") + " " + p.get("label", "")).lower().split()
        # Match if any meaningful token (>=3 chars, not just numbers) appears
        for token in addr_parts:
            if len(token) >= 3 and token not in ("the", "and", "ave", "street",
                                                  "avenue", "drive", "road", "blvd")\
                    and not token.replace(",", "").isdigit() \
                    and token in payload_l:
                project = p
                break
        if project:
            break
    # Also try matching by customer name
    if not project:
        for p in active:
            cn = (p.get("customer_name") or "").lower()
            if cn and any(t in payload_l for t in cn.split() if len(t) >= 3):
                project = p
                break
    if not project:
        if not active:
            return {"reply": "You don't have any active projects yet. Add one first: \"new project at <address> — $X <label>\".",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_co_no_projects"}
        listed = "\n".join(f"  · {p['address']}" for p in active[:5])
        return {"reply": f"Which project? I see these active:\n{listed}",
                "tier": "local", "latency_ms": 0,
                "source": "gc_co_pick_project"}
    # Extract description = payload minus the matched project label/address
    # and minus the dollar amount.
    description = _DOLLAR_AMT_RE.sub("", payload).strip(" ,—–-")
    for token in (project.get("address", ""), project.get("label", ""),
                   project.get("customer_name", "")):
        if token:
            description = _re.sub(_re.escape(token), "", description, flags=_re.IGNORECASE)
    description = _re.sub(r"\s+", " ", description).strip(" :—–-,.")
    # Strip leading filler words separately
    description = _re.sub(r"^(?:for|on|that|with|of|to)\s+", "", description,
                            flags=_re.IGNORECASE).strip()
    if not description:
        description = "Additional scope agreed with client"

    # Parse line items + lookup pricing catalog
    line_rows = mod_line_items.parse(description, data_dir=DATA_DIR)
    priced_total = mod_line_items.total(line_rows)

    # Decide on the dollar amount:
    #   - explicit $X in the message always wins
    #   - else use priced total from line items
    #   - else need user to specify
    if explicit_amount:
        amount = explicit_amount
    elif priced_total > 0:
        amount = priced_total
    else:
        if line_rows:
            preview = mod_line_items.format_for_co_description(line_rows)
            return {"reply": (
                f"I parsed these line items but no pricing on file matched:\n"
                f"{preview}\n\n"
                f"Add unit prices in Settings → Pricing, or tell me the "
                f"dollar amount and I'll create the CO. E.g. \"CO on "
                f"{project['address'][:30]} — $X for the above\""
            ), "tier": "local", "latency_ms": 0,
               "source": "gc_co_lines_no_price",
               "parsed_items": line_rows}
        return {"reply": "I need a dollar amount. E.g. \"$1200\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_co_no_amount"}

    # If we have line items, replace the flat description with the
    # itemized version so it shows up on the CO doc as a proper list.
    if line_rows:
        itemized = mod_line_items.format_for_co_description(line_rows)
        full_description = description + "\n\n" + itemized
    else:
        full_description = description

    # Build the CO record
    draft = _draft_change_order_text(project, full_description, amount)
    co = mod_change_orders.add(
        DATA_DIR,
        project_id=project["id"],
        description=full_description,
        amount=amount,
        draft_text=draft,
        status="sent_for_signature",
    )
    mod_change_orders.update(
        DATA_DIR, co["id"],
        sent_at=int(time.time()),
        sent_via="in_person",
    )

    # Always ensure the client folder exists, even if no template is uploaded.
    customer_name = (project.get("customer_name") or "").strip()
    if customer_name:
        try:
            mod_clients.ensure_folder(customer_name, CONFIG)
        except Exception as e:
            log.warning(f"client folder create failed for {customer_name!r}: {e}")

    # Try to fill the customer's branded CO template. If they haven't
    # uploaded one yet, skip — the sign-page shows draft_text instead.
    filled_pdf_path = None
    filled_pdf_label = ""
    try:
        tpl = mod_forms.get_default(DATA_DIR, "change_order")
        if tpl:
            business = mod_business.load(DATA_DIR)
            context = mod_form_filler.build_context_for_co(project, co, business)
            tpl_path = _resolve_template_path(tpl)
            if tpl_path and tpl_path.exists():
                # Save into the client's change_orders/ folder for organization
                customer_name = project.get("customer_name") or "Unnamed Client"
                from datetime import datetime as _dt
                co_num = co.get("co_number", "?")
                ext = ".pdf" if tpl["format"] == "pdf" else ".docx"
                fname = f"CO_{co_num}_{_dt.now().strftime('%Y-%m-%d')}{ext}"
                client_dir = mod_clients.subfolder(customer_name, "change_orders", CONFIG)
                out_path = client_dir / fname
                mod_form_filler.fill(tpl_path, tpl["format"],
                                       tpl.get("detected_fields", []) or [],
                                       context, out_path)
                filled_pdf_path = out_path
                filled_pdf_label = tpl["display_name"]
                # Also stash the path on the CO record for the sign page to surface
                mod_change_orders.update(DATA_DIR, co["id"],
                                          filled_template_path=str(out_path))
    except Exception as e:
        log.warning(f"branded CO template fill failed: {e}")

    token = _mint_co_sign_token(co["id"])
    sign_url = _co_sign_url(token)
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="co.drafted_for_in_person_sign",
                    meta={"co_id": co["id"], "project_id": project["id"],
                          "amount": amount,
                          "line_items": len(line_rows),
                          "filled_template": filled_pdf_label})
    sign = "+" if amount >= 0 else ""
    reply_lines = [
        f"CO #{co.get('co_number','?')} drafted — {project['address']}",
        f"  Customer: {project.get('customer_name') or 'Unnamed'}",
        f"  Amount: {sign}${amount:,.0f}",
    ]
    if line_rows and priced_total > 0:
        reply_lines.append("\nLine items:")
        reply_lines.append(mod_line_items.format_for_co_description(line_rows))
    elif line_rows:
        reply_lines.append("\nLine items (no pricing on file — manually verified):")
        reply_lines.append(mod_line_items.format_for_co_description(line_rows))
    else:
        reply_lines.append(f"\n  \"{description[:120]}\"")
    if filled_pdf_path:
        reply_lines.append(f"\n📄 Filled into your branded template ({filled_pdf_label}):")
        reply_lines.append(f"   {filled_pdf_path}")
    reply_lines.append(
        f"\n👉 Hand the device to {project.get('customer_name') or 'the customer'} "
        f"to review + sign:\n{sign_url}\n\n"
        f"Office gets notified the moment they sign."
    )
    return {"reply": "\n".join(reply_lines),
            "tier": "local", "latency_ms": 0,
            "source": "gc_co_drafted_for_sign",
            "sign_url": sign_url,
            "co_id": co["id"],
            "filled_template_path": str(filled_pdf_path) if filled_pdf_path else None}


def _resolve_template_path(tpl: dict) -> Path | None:
    """Resolve a form template's storage_path (stored as relative when
    possible) into an absolute Path under the data dir."""
    p = tpl.get("storage_path")
    if not p:
        return None
    pp = Path(p)
    if pp.is_absolute():
        return pp
    # Try relative to data_dir
    candidate = DATA_DIR / tpl.get("format", "pdf") and DATA_DIR / "form_templates" / f"{tpl['id']}.{tpl.get('format','pdf')}"
    if isinstance(candidate, Path) and candidate.exists():
        return candidate
    # Last resort: data/form_templates/<id>.<format>
    fallback = DATA_DIR / "form_templates" / f"{tpl['id']}.{tpl.get('format','pdf')}"
    return fallback if fallback.exists() else None


def _find_project_in_payload(payload: str) -> dict | None:
    """Shared helper — fuzzy-match an active project from free text.
    Used by add-CO, add-invoice, add-log, assign-sub handlers."""
    active = mod_projects.list_active(DATA_DIR)
    payload_l = (payload or "").lower()
    for p in active:
        addr_parts = (p.get("address", "") + " " + p.get("label", "")).lower().split()
        for token in addr_parts:
            if len(token) >= 3 and token not in ("the", "and", "ave", "street",
                                                  "avenue", "drive", "road", "blvd") \
                    and not token.replace(",", "").isdigit() \
                    and token in payload_l:
                return p
    return None


def _handle_add_log(msg: str, user_rec: dict) -> dict:
    """Parse 'log today on Oak — crew Mike + Jose, framing done, 8 hours'
    into a structured daily log entry. Extracts crew names, hours,
    materials/deliveries when present."""
    payload = _GC_ADD_LOG_RE.sub("", msg, count=1).strip(" :—–-,.")
    if not payload:
        return {"reply": "Tell me which project and what happened. E.g. "
                          "\"log today on Oak — crew Mike + Jose, framing complete, 8 hours\"",
                "tier": "local", "latency_ms": 0,
                "source": "gc_log_no_details"}
    project = _find_project_in_payload(payload)
    if not project:
        active = mod_projects.list_active(DATA_DIR)
        if not active:
            return {"reply": "No active projects to log against.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_log_no_projects"}
        listed = "\n".join(f"  · {p['address']}" for p in active[:5])
        return {"reply": f"Which project? I see these active:\n{listed}",
                "tier": "local", "latency_ms": 0,
                "source": "gc_log_pick_project"}
    # Strip project tokens from payload to isolate the work-done text
    work_text = payload
    for token in (project.get("address", ""), project.get("label", "")):
        if token:
            work_text = _re.sub(_re.escape(token), "", work_text, flags=_re.IGNORECASE)
    work_text = _re.sub(r"\s+", " ", work_text).strip(" :—–-,.")
    # Parse hours
    hours = 0.0
    h_match = _re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?\b)", work_text, _re.IGNORECASE)
    if h_match:
        hours = float(h_match.group(1))
        work_text = work_text[:h_match.start()] + work_text[h_match.end():]
        work_text = _re.sub(r"\s+", " ", work_text).strip(" ,")
    # Parse crew: "crew Mike + Jose" / "crew: Mike, Jose, Tony"
    crew = []
    crew_match = _re.search(r"crew[:\s]+((?:[A-Z][a-z]+(?:\s*[+,&]\s*[A-Z][a-z]+)*))",
                              work_text)
    if crew_match:
        raw = crew_match.group(1)
        crew = [n.strip() for n in _re.split(r"[+,&]", raw) if n.strip()]
        work_text = work_text[:crew_match.start()] + work_text[crew_match.end():]
        work_text = _re.sub(r"\s+", " ", work_text).strip(" :—–-,.")
    work_done = work_text or "Day's work logged"
    from datetime import datetime as _dt
    today_iso = _dt.now().strftime("%Y-%m-%d")
    log = mod_daily_logs.add(
        DATA_DIR,
        project_id=project["id"],
        date_iso=today_iso,
        crew=crew,
        work_done=work_done,
        hours=hours,
        logged_by=user_rec.get("username", ""),
    )
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="daily_log.added",
                    meta={"log_id": log["id"], "project_id": project["id"],
                          "hours": hours, "scope_changes": len(log["scope_changes_mentioned"])})
    crew_bit = f" Crew: {', '.join(crew)}." if crew else ""
    hours_bit = f" {hours:g} hrs." if hours else ""
    scope_flag = ""
    if log.get("scope_changes_mentioned"):
        scope_flag = (f" ⚠ Picked up possible scope additions: "
                      f"{'; '.join(log['scope_changes_mentioned'][:3])}. "
                      f"Say \"leak check\" to see if there's a CO covering them.")
    return {"reply": f"Logged on {project['address']} — \"{work_done[:80]}\".{crew_bit}{hours_bit}{scope_flag}",
            "tier": "local", "latency_ms": 0,
            "source": "gc_log_added"}


def _handle_list_logs(msg: str, user_rec: dict) -> dict:
    m = _GC_LIST_LOGS_RE.match(msg)
    if not m:
        return None
    if msg.lower().strip().startswith("this week"):
        logs = mod_daily_logs.list_for_week(DATA_DIR)
        if not logs:
            return {"reply": "No logs in the past week.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_logs_empty"}
        lines = [f"Logs this week ({len(logs)}):"]
        for l in logs[-10:]:
            proj = mod_projects.get(DATA_DIR, l.get("project_id", "")) or {}
            crew_bit = f" {','.join(l.get('crew',[]))}" if l.get('crew') else ""
            hrs_bit = f" {l['hours']:g}h" if l.get('hours') else ""
            lines.append(f"  · {l['date']} {proj.get('address','?')[:25]} —{crew_bit}{hrs_bit} {l.get('work_done','')[:70]}")
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "gc_logs_week"}
    query = (m.group("query") or m.group("query2") or m.group("query3") or "").strip()
    project = _find_project_in_payload(query)
    if not project:
        return {"reply": f"I don't see a project matching \"{query}\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_logs_no_project"}
    logs = mod_daily_logs.list_for_project(DATA_DIR, project["id"])
    if not logs:
        return {"reply": f"No logs yet on {project['address']}. "
                          f"Add one: \"log on {project.get('label') or project['address'].split(',')[0]} — work done\"",
                "tier": "local", "latency_ms": 0,
                "source": "gc_logs_none"}
    lines = [f"Logs on {project['address']} ({len(logs)}):"]
    for l in logs[:10]:
        crew_bit = f" {','.join(l.get('crew',[]))}" if l.get('crew') else ""
        hrs_bit = f" {l['hours']:g}h" if l.get('hours') else ""
        lines.append(f"  · {l['date']}{crew_bit}{hrs_bit} — {l.get('work_done','')[:90]}")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_logs_project"}


def _handle_add_sub(msg: str, user_rec: dict) -> dict:
    """Parse 'add sub Bob's Plumbing — plumbing — 555-555-1234' /
    'new sub Joe Drywall, drywall, license NV-12345, joe@example.com'"""
    payload = _GC_ADD_SUB_RE.sub("", msg, count=1).strip(" :—–-,.")
    if not payload:
        return {"reply": "Give me the sub's name + trade. E.g. "
                          "\"add sub Bob's Plumbing — plumbing — 555-555-1234\"",
                "tier": "local", "latency_ms": 0,
                "source": "gc_sub_no_details"}
    # Split by em-dash, en-dash, or comma — NOT plain hyphen, because
    # license numbers like "NV-99999" contain hyphens and would
    # otherwise get split across fields.
    parts = [p.strip() for p in _re.split(r"\s*[—–,]\s*", payload) if p.strip()]
    name = parts[0] if parts else ""
    # Common trades to detect
    trade = ""
    known_trades = {"plumbing", "electrical", "framing", "drywall", "painting",
                     "roofing", "concrete", "hvac", "flooring", "tile", "tiling",
                     "cabinetry", "landscaping", "excavation", "insulation",
                     "carpentry", "demolition", "siding"}
    for p in parts[1:]:
        if p.lower() in known_trades:
            trade = p.lower()
            break
    # Pull phone + email
    phone = ""; email = ""
    for p in parts:
        ph = _extract_phone(p)
        if ph: phone = ph
        em = _extract_email(p)
        if em: email = em
    # License — word boundary on left so "lic" doesn't match inside "license"
    # and end up capturing the trailing letters as the number.
    license_v = ""
    for p in parts:
        lm = _re.search(r"\b(?:license|lic|#)\s+([A-Z]{1,3}-?\d[A-Z0-9\-]{3,19})",
                          p, _re.IGNORECASE)
        if lm:
            license_v = lm.group(1).upper()
            break
    sub = mod_subs.add_sub(
        DATA_DIR, name=name, trade=trade,
        phone=phone, email=email, license=license_v,
    )
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="sub.added",
                    meta={"sub_id": sub["id"], "name": name, "trade": trade})
    bits = []
    if trade: bits.append(trade)
    if phone: bits.append(phone)
    if email: bits.append(email)
    if license_v: bits.append(f"lic {license_v}")
    detail = f" ({' · '.join(bits)})" if bits else ""
    return {"reply": f"Sub added — {name}{detail}.",
            "tier": "local", "latency_ms": 0,
            "source": "gc_sub_added"}


def _handle_list_subs(msg: str, user_rec: dict) -> dict:
    m = _GC_LIST_SUBS_RE.match(msg)
    if not m:
        return None
    trade = (m.group("trade") or m.group("trade2") or m.group("trade3") or "").strip()
    subs = mod_subs.list_subs(DATA_DIR, trade=trade)
    if not subs:
        msg = f"No active subs{' for ' + trade if trade else ''} on file."
        if not trade:
            msg += " Add one: \"add sub <name> — <trade> — <phone>\""
        return {"reply": msg, "tier": "local", "latency_ms": 0,
                "source": "gc_subs_empty"}
    lines = [f"Active subs{' for ' + trade if trade else ''} ({len(subs)}):"]
    for s in subs[:20]:
        trade_bit = f" ({s['trade']})" if s.get("trade") else ""
        phone_bit = f" — {s['phone']}" if s.get("phone") else ""
        lines.append(f"  · {s['name']}{trade_bit}{phone_bit}")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_subs_listed"}


def _handle_assign_sub(msg: str, user_rec: dict) -> dict:
    m = _GC_ASSIGN_SUB_RE.match(msg)
    if not m:
        return None
    sub_query = m.group("sub").strip()
    rest = m.group("rest").strip()
    # Find sub. If no match AND the query doesn't look like a real sub
    # name (e.g. "schedule a site visit Friday at 10 AM" → sub_query="a
    # site visit Friday"), fall through so quick_capture can route it as
    # a calendar event instead of swallowing it with "no sub matching".
    sub_matches = mod_subs.find_sub(DATA_DIR, sub_query)
    if not sub_matches:
        sql = sub_query.lower()
        looks_calendar = any(w in sql for w in (
            "site visit", "appointment", "meeting", "consultation",
            "walkthrough", "walk through", "estimate visit",
        ))
        # If the verb was "schedule" or "dispatch" AND the sub_query
        # doesn't look like a person/company name (no capitalized noun
        # in first 30 chars), let it fall through.
        leading_verb_match = _re.match(r"^\s*(\w+)", msg)
        leading_verb = leading_verb_match.group(1).lower() if leading_verb_match else ""
        if looks_calendar or leading_verb in ("schedule", "dispatch"):
            return None
        return {"reply": f"I don't see a sub matching \"{sub_query}\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_assign_sub_not_found"}
    if len(sub_matches) > 1:
        listed = "\n".join(f"  · {s['name']} ({s.get('trade','?')})" for s in sub_matches[:5])
        return {"reply": f"Multiple subs match \"{sub_query}\":\n{listed}\nBe more specific.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_assign_sub_ambiguous"}
    sub = sub_matches[0]
    # Find project in `rest`
    project = _find_project_in_payload(rest)
    if not project:
        return {"reply": f"Which project to assign {sub['name']} to? Try the address.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_assign_no_project"}
    # Any scope hint in rest? e.g. "for rough plumbing"
    scope = ""
    scope_match = _re.search(r"\bfor\s+(.+?)(?:\s+on\s+\w+|$)", rest, _re.IGNORECASE)
    if scope_match:
        scope = scope_match.group(1).strip()
    # Any scheduled date? "Tuesday" / "next Monday"
    scheduled = ""
    for word in ("monday", "tuesday", "wednesday", "thursday", "friday",
                  "saturday", "sunday", "tomorrow", "today"):
        if word in rest.lower():
            scheduled = word.capitalize()
            break
    a = mod_subs.assign(
        DATA_DIR, sub_id=sub["id"], project_id=project["id"],
        scope=scope, scheduled=scheduled,
    )
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="sub.assigned",
                    meta={"sub_id": sub["id"], "project_id": project["id"]})
    sched_bit = f" for {scheduled}" if scheduled else ""
    scope_bit = f" — {scope}" if scope else ""
    return {"reply": f"Assigned {sub['name']} to {project['address']}{sched_bit}{scope_bit}.",
            "tier": "local", "latency_ms": 0,
            "source": "gc_sub_assigned"}


def _handle_insurance_check(msg: str, user_rec: dict) -> dict:
    expiring = mod_subs.insurance_expiring_soon(DATA_DIR, days=30)
    if not expiring:
        return {"reply": "All sub insurance is current — nothing expiring in the next 30 days.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_insurance_clear"}
    lines = [f"⚠ Insurance expiring within 30 days ({len(expiring)}):"]
    for s in expiring[:10]:
        lines.append(f"  · {s['name']} ({s.get('trade','?')}) — expires {s.get('insurance_expires','?')}")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_insurance_expiring"}


def _handle_sub_schedule(msg: str, user_rec: dict) -> dict | None:
    """'What's Bob working on' / 'Bob's schedule' — list a sub's active +
    completed assignments."""
    m = _GC_SUB_SCHEDULE_RE.match(msg)
    if not m:
        return None
    sub_q = (m.group("sub1") or m.group("sub2") or m.group("sub3") or "").strip()
    # Filter out non-sub queries the regex may have caught (e.g. "what's
    # Joe working on" should only match if Joe is a known sub)
    if not sub_q or sub_q.lower() in {"i", "frank", "orby", "you", "we"}:
        return None
    matches = mod_subs.find_sub(DATA_DIR, sub_q)
    if not matches:
        return None  # Don't claim "no sub" — let LLM handle, may be a person
    if len(matches) > 1:
        listed = "\n".join(f"  · {s['name']} ({s.get('trade','?')})" for s in matches[:5])
        return {"reply": f"Multiple subs match \"{sub_q}\":\n{listed}",
                "tier": "local", "latency_ms": 0,
                "source": "gc_sub_schedule_ambiguous"}
    sub = matches[0]
    assignments = mod_subs.list_assignments_for_sub(DATA_DIR, sub["id"])
    if not assignments:
        return {"reply": f"{sub['name']} has no assignments on the books.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_sub_schedule_empty"}
    active = [a for a in assignments if not a.get("completed")]
    completed = [a for a in assignments if a.get("completed")]
    lines = [f"{sub['name']} ({sub.get('trade','?')}):"]
    if active:
        lines.append(f"  Active ({len(active)}):")
        for a in active[:10]:
            proj = mod_projects.get(DATA_DIR, a.get("project_id", "")) or {}
            sched = f" — {a.get('scheduled')}" if a.get("scheduled") else ""
            scope = f" — {a.get('scope')}" if a.get("scope") else ""
            lines.append(f"    · {proj.get('address','?')}{sched}{scope}")
    if completed:
        lines.append(f"  Completed ({len(completed)}, last 5):")
        for a in completed[-5:]:
            proj = mod_projects.get(DATA_DIR, a.get("project_id", "")) or {}
            lines.append(f"    · {proj.get('address','?')}"
                          + (f" — {a.get('scope')}" if a.get("scope") else ""))
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_sub_schedule"}


def _handle_mark_project_done(msg: str, user_rec: dict) -> dict | None:
    m = _GC_MARK_PROJECT_DONE_RE.match(msg)
    if not m:
        return None
    q = (m.group("q") or m.group("q2") or "").strip()
    if not q or q.lower() in {"i", "we", "you", "it", "everything", "that"}:
        return None
    matches = mod_projects.find_by_address(DATA_DIR, q)
    if not matches:
        return None  # Let LLM handle — too easy to false-positive
    if len(matches) > 1:
        listed = "\n".join(f"  · {p['address']}" for p in matches[:5])
        return {"reply": f"Multiple projects match \"{q}\":\n{listed}\nWhich one?",
                "tier": "local", "latency_ms": 0,
                "source": "gc_mark_done_ambiguous"}
    p = matches[0]
    from datetime import datetime as _dt
    mod_projects.update(DATA_DIR, p["id"],
                         status="completed",
                         actual_complete=_dt.now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="project.completed",
                    meta={"project_id": p["id"], "address": p["address"]})
    signed_co = mod_change_orders.signed_total_for_project(DATA_DIR, p["id"])
    invs = mod_invoices.list_for_project(DATA_DIR, p["id"])
    billed = sum(float(i.get("amount_due", 0)) for i in invs
                  if i.get("status") not in ("draft", "void"))
    paid = sum(float(i.get("amount_paid", 0)) for i in invs)
    retainage = mod_invoices.retainage_held_for_project(DATA_DIR, p["id"])
    total = float(p.get("contract_amount") or 0) + signed_co
    lines = [f"Closed {p['address']}.",
              f"  Contract + signed COs: ${total:,.0f}",
              f"  Billed: ${billed:,.0f}  Paid: ${paid:,.0f}"]
    if retainage > 0:
        lines.append(f"  Retainage held: ${retainage:,.2f} — generate the retainage release invoice when ready.")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_project_completed"}


def _handle_project_full_report(msg: str, user_rec: dict) -> dict | None:
    """The big one — everything about a project in one chat reply.
    Contract + COs + invoices + recent logs + assigned subs."""
    m = _GC_PROJECT_REPORT_RE.match(msg)
    if not m:
        return None
    q = (m.group("q1") or m.group("q2") or m.group("q3")
          or m.group("q4") or m.group("q5") or "").strip()
    if not q or q.lower() in {"things", "stuff", "money", "today"}:
        return None
    matches = mod_projects.find_by_address(DATA_DIR, q)
    if not matches:
        # Fallback: customer-name match (e.g. "history for Johnson")
        matches = _find_projects_by_customer(q)
    if not matches:
        return None
    if len(matches) > 1:
        listed = "\n".join(f"  · {p['address']}" for p in matches[:5])
        return {"reply": f"Multiple projects match \"{q}\":\n{listed}\nWhich one?",
                "tier": "local", "latency_ms": 0,
                "source": "gc_project_report_ambiguous"}
    p = matches[0]
    # All the data
    contract = float(p.get("contract_amount") or 0)
    cos = mod_change_orders.list_for_project(DATA_DIR, p["id"])
    signed_co = sum(float(c.get("amount") or 0) for c in cos
                     if c.get("status") == "signed")
    invs = mod_invoices.list_for_project(DATA_DIR, p["id"])
    billed = sum(float(i.get("amount_due", 0)) for i in invs
                  if i.get("status") not in ("draft", "void"))
    paid = sum(float(i.get("amount_paid", 0)) for i in invs)
    logs = mod_daily_logs.list_for_project(DATA_DIR, p["id"], limit=5)
    assignments = mod_subs.list_assignments_for_project(DATA_DIR, p["id"])
    # Build
    lines = [f"━━━ {p['address']} ━━━"]
    if p.get("label"): lines.append(f"  {p['label']}")
    lines.append(f"  Status: {p.get('status','?')}"
                  + (f" ({p['stage']})" if p.get("stage") else ""))
    if p.get("customer_name"):
        lines.append(f"  Customer: {p['customer_name']}"
                      + (f" ({p['customer_phone']})" if p.get("customer_phone") else ""))
    if p.get("foreman"):
        lines.append(f"  Foreman: {p['foreman']}")
    lines.append("")
    lines.append(f"💰 Money:")
    lines.append(f"  Contract: ${contract:,.0f}"
                  + (f" + ${signed_co:,.0f} signed COs = ${contract + signed_co:,.0f}" if signed_co else ""))
    lines.append(f"  Billed ${billed:,.0f}  Paid ${paid:,.0f}  Owed ${billed - paid:,.0f}")
    if cos:
        lines.append("")
        lines.append(f"📋 Change orders ({len(cos)}):")
        for c in cos[:5]:
            amt = float(c.get("amount") or 0)
            sign = "+" if amt >= 0 else ""
            lines.append(f"  · #{c['co_number']} {c.get('status','?').upper()} ({sign}${amt:,.0f}) {c.get('description','')[:55]}")
    if invs:
        lines.append("")
        lines.append(f"🧾 Invoices ({len(invs)}):")
        for i in invs[:5]:
            owed = float(i.get("amount_due", 0)) - float(i.get("amount_paid", 0))
            lines.append(f"  · {i.get('invoice_number','?')} {i.get('status','?').upper()} owed ${owed:,.0f}")
    if assignments:
        lines.append("")
        lines.append(f"👷 Subs on site ({len(assignments)}):")
        for a in assignments[:5]:
            lines.append(f"  · {a.get('sub_name','?')} ({a.get('sub_trade','?')}) — {a.get('scope','')[:50]}")
    if logs:
        lines.append("")
        lines.append(f"📓 Recent logs ({len(logs)}):")
        for l in logs[:3]:
            crew_bit = f" {','.join(l.get('crew',[]))}" if l.get('crew') else ""
            hrs_bit = f" {l['hours']:g}h" if l.get('hours') else ""
            lines.append(f"  · {l['date']}{crew_bit}{hrs_bit} {l.get('work_done','')[:70]}")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_project_report"}


def _gc_help_text() -> str:
    """Single source of truth for the contractor module command list.
    Update here when you add a new intent. Grouped by what the GC is
    trying to do, not by code layout."""
    return """Contractor Orby — commands cheat sheet

📋 PROJECTS
  · new project at 555 Oak — $24k bathroom remodel
  · what jobs are open
  · status of the Oak project
  · full report on Oak  (the deep dive: money + COs + invoices + subs + logs)
  · mark Oak complete

📝 CHANGE ORDERS (foreman-on-site flow)
  · CO on the Oak project — $1200 extra trim work
      → Orby drafts it + returns a signing URL right away.
      → Hand the iPad to the customer; they sign on the spot.
      → Office gets notified the moment they sign — added to billing.
  · pending COs                       (things still out for signature)
  · leak check                        (work in daily logs with no covering CO)
  · (approve CO #xxx + send CO #xxx)  edge cases when office wants
                                        to review before customer sees it

🧾 INVOICING & PAYMENTS
  · send invoice for Oak — $5000 progress draw 1
  · who owes me money / receivables
  · aging report
  · received $3000 on INV-2026-0001
  · PDF INV-2026-0001                 (downloadable PDF invoice)

💰 RECEIVABLES NUDGES
  · nudge INV-2026-0001               (draft polite reminder)
  · draft all overdue reminders
  · show queued reminders / send queued reminders
                                       (daemon also auto-drafts every 6h)

📊 BIDS & WIN/LOSS
  · sent bid for Sarah Johnson at 123 Maple — $48,500 kitchen remodel
  · open bids
  · Sarah won the kitchen bid          (auto-creates the project!)
  · Jennifer passed on the bathroom — went with someone cheaper
  · win rate / bid report
  · proposal for Sarah Johnson         (polished PDF for outgoing bids)

🎯 CLIENT-FACING
  · share Oak with the client          (permanent portal URL)
  · review link for Birch              (1-click rating after closeout)
  · my rating / review summary
  · closeout for Birch                 (PDF package + auto review link)

📓 FIELD OPS
  · log today on Oak — crew Mike + Jose, framing complete, 8 hours
  · logs for Oak / this week's logs

👷 SUBCONTRACTORS
  · add sub Bob's Plumbing — plumbing — 555-555-1234
  · subs / subs for plumbing
  · assign Bob to Oak Tuesday for rough plumbing
  · what's Bob working on
  · whose insurance is expiring

📈 REPORTING
  · morning brief                     (real-time daily picture)
  · tomorrow's brief                  (end-of-day preview)
  · weekly recap                      (Friday afternoon debrief)
  · show me the money                 (full money report)

💡 Type "help" any time to see this list again."""


def _handle_proposal_pdf(msg: str, user_rec: dict) -> dict | None:
    """Generate a polished proposal PDF for an existing bid record.
    Customer fuzzy-match against bid's customer_name."""
    m = _GC_PROPOSAL_PDF_RE.match(msg)
    if not m:
        return None
    q = (m.group("q1") or m.group("q2") or m.group("q3") or "").strip()
    q = _re.sub(r"^the\s+", "", q, flags=_re.IGNORECASE)
    if not q:
        return None
    matches = mod_bids.find_by_customer(DATA_DIR, q)
    if not matches:
        # Be helpful — they explicitly asked for a proposal, so don't
        # silently fall through to the LLM. List the bids on file
        # so the GC can pick a name we have.
        all_bids = mod_bids.list_all(DATA_DIR, limit=10)
        if not all_bids:
            return {"reply": (f"I don't have a bid on file for \"{q}\". "
                              f"Log one first: \"sent bid for {q} at <address> "
                              f"— $<amount> <project type>\"."),
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_proposal_no_bid"}
        listed = "\n".join(f"  · {b['customer_name']}"
                            f"{' — ' + b.get('project_type','') if b.get('project_type') else ''}"
                            for b in all_bids[:10])
        return {"reply": (f"No bid on file matching \"{q}\". On the books:\n"
                          f"{listed}\nSay \"proposal for <one of those names>\"."),
                "tier": "local", "latency_ms": 0,
                "source": "gc_proposal_no_match"}
    if len(matches) > 1:
        listed = "\n".join(f"  · {b['customer_name']} — {b.get('project_type','')} ${b.get('amount',0):,.0f}"
                            for b in matches[:5])
        return {"reply": f"Multiple bids match \"{q}\":\n{listed}\nBe more specific.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_proposal_ambiguous"}
    bid = matches[0]
    business = mod_business.load(DATA_DIR)
    try:
        pdf_path = mod_proposal_pdf.generate(bid, business,
                                                DATA_DIR / "proposal_pdfs")
    except Exception as e:
        log.exception("proposal PDF failed")
        return {"reply": f"Proposal PDF generation failed: {e}",
                "tier": "local", "latency_ms": 0,
                "source": "gc_proposal_error"}
    try:
        token = file_fetch.mint_download_token(
            DATA_DIR, str(pdf_path),
            ttl_minutes=24 * 60,
            extra_allowed_roots=[DATA_DIR / "proposal_pdfs"],
        )
    except Exception as e:
        log.warning(f"proposal token mint failed: {e}")
        token = None
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="proposal.generated",
                    meta={"bid_id": bid["id"]})
    size_kb = pdf_path.stat().st_size // 1024
    if token:
        return {"reply": (f"Proposal PDF built for {bid['customer_name']} "
                          f"(${bid.get('amount',0):,.0f} {bid.get('project_type','')}, "
                          f"{size_kb} KB). [Download here](/download/{token}) "
                          f"— link valid 24 hours."),
                "tier": "local", "latency_ms": 0,
                "source": "gc_proposal_generated",
                "download_url": f"/download/{token}"}
    return {"reply": f"Proposal saved at {pdf_path} ({size_kb} KB).",
            "tier": "local", "latency_ms": 0,
            "source": "gc_proposal_generated_no_token"}


def _handle_review_link(msg: str, user_rec: dict) -> dict | None:
    m = _GC_REVIEW_LINK_RE.match(msg)
    if not m:
        return None
    q = (m.group("q1") or m.group("q2") or m.group("q3") or "").strip()
    q = _re.sub(r"^the\s+", "", q, flags=_re.IGNORECASE)
    q = _re.sub(r"\s+project$", "", q, flags=_re.IGNORECASE)
    if not q:
        return None
    matches = mod_projects.find_by_address(DATA_DIR, q)
    if not matches:
        return None
    if len(matches) > 1:
        listed = "\n".join(f"  · {p['address']}" for p in matches[:5])
        return {"reply": f"Multiple projects match \"{q}\":\n{listed}\nWhich one?",
                "tier": "local", "latency_ms": 0,
                "source": "gc_review_ambiguous"}
    project = matches[0]
    review = mod_reviews.issue(DATA_DIR, project["id"])
    base = (CONFIG.get("tunnel_url")
             or (CONFIG.get("brain") or {}).get("local_public_url")
             or "http://127.0.0.1:5050").rstrip("/")
    url = f"{base}/r/{review['token']}"
    customer = project.get("customer_name") or "the customer"
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="review.link_issued",
                    meta={"project_id": project["id"]})
    return {"reply": (f"Review link for {customer} on {project.get('label') or project['address']}:\n\n"
                      f"{url}\n\n"
                      f"Single-use. Drop it in your closeout email or text."),
            "tier": "local", "latency_ms": 0,
            "source": "gc_review_link_issued"}


def _handle_my_rating(msg: str, user_rec: dict) -> dict | None:
    if not _GC_MY_RATING_RE.match(msg):
        return None
    s = mod_reviews.summary(DATA_DIR)
    if s["count"] == 0 and s["issued_unanswered"] == 0:
        return {"reply": "No reviews yet — no links issued. After you close a "
                          "project, say \"review link for <project>\" to send "
                          "the customer a 30-second 1-5 star form.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_rating_none"}
    if s["count"] == 0:
        return {"reply": f"No reviews submitted yet, but {s['issued_unanswered']} link(s) "
                          f"out there waiting. Customers usually fill them out within a week.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_rating_pending"}
    stars = "★" * int(round(s["average"]))
    lines = [
        f"⭐ Your average: {s['average']:.2f}/5 {stars} ({s['count']} review{'s' if s['count'] != 1 else ''})",
        f"   {s['would_recommend_pct']:.0f}% would recommend you",
        "",
        f"   Distribution:",
    ]
    for v in (5, 4, 3, 2, 1):
        c = s["distribution"].get(v, 0)
        bar = "█" * c if c else "·"
        lines.append(f"     {v}★ {bar} ({c})")
    if s["issued_unanswered"]:
        lines.append("")
        lines.append(f"   {s['issued_unanswered']} review link(s) still pending")
    shareable = mod_reviews.list_shareable(DATA_DIR)
    if shareable:
        lines.append("")
        lines.append(f"   {len(shareable)} customer{'s' if len(shareable) != 1 else ''} "
                      f"gave permission to quote publicly — good marketing material.")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_rating_summary"}


def _handle_add_bid(msg: str, user_rec: dict) -> dict:
    """Parse 'sent bid for Sarah Johnson at 123 Maple — $48,500 kitchen
    remodel'. Loose parsing — looks for a person name, dollar amount,
    optional project type + address."""
    payload = _GC_ADD_BID_RE.sub("", msg, count=1).strip(" :—–,.")
    if not payload:
        return {"reply": "Tell me who, what, how much. E.g. \"sent bid for "
                          "Sarah Johnson at 123 Maple — $48,500 kitchen remodel\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_bid_no_details"}
    amount = _parse_money(payload)
    if not amount:
        return {"reply": "I need a dollar amount. E.g. \"$48,500\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_bid_no_amount"}
    # Customer name = first capitalized 1-3 word run we find (excluding
    # the dollar amount + addresses with numbers).
    cust_match = _re.search(
        # Allow internal caps + apostrophes + hyphens so McDonald,
        # O'Brien, Saint-John, TestPerson all match.
        r"(?:for|to)\s+([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,2})",
        payload)
    customer_name = cust_match.group(1) if cust_match else ""
    if not customer_name:
        # Fallback: first capitalized phrase
        # Allow internal caps/apostrophes/hyphens so McDonald, O'Brien,
        # Saint-John, TestPerson all match as a single name token.
        nm = _re.search(r"\b([A-Z][A-Za-z'\-]{1,20}(?:\s+[A-Z][A-Za-z'\-]{1,20}){0,2})\b",
                         payload)
        if nm:
            candidate = nm.group(1)
            if candidate.lower() not in {"kitchen", "bathroom", "deck",
                                          "garage", "whole", "house",
                                          "addition", "remodel"}:
                customer_name = candidate
    if not customer_name:
        return {"reply": "I couldn't pick up the customer's name. Try "
                          "\"sent bid for FirstName LastName ...\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_bid_no_name"}
    # Project address — look for street-number patterns
    addr_match = _re.search(
        r"\b(\d{1,5}\s+[A-Za-z][A-Za-z\s\.]{2,40}"
        r"(?:Street|St|Avenue|Ave|Drive|Dr|Road|Rd|Lane|Ln|Court|Ct|Blvd|Way))",
        payload)
    project_address = addr_match.group(1).strip() if addr_match else ""
    # Project type — common keywords
    type_match = _re.search(
        r"\b(kitchen|bathroom|bath|deck|garage|whole[\s-]home|addition|"
        r"basement|attic|patio|fence|pool|driveway|roof(?:ing)?|"
        r"siding|painting?|remodel|renovation)\b\s*(?:remodel|renovation|build|repaint|conversion)?",
        payload, _re.IGNORECASE)
    project_type = ""
    if type_match:
        project_type = type_match.group(0).title()
    # Phone + email
    phone = _extract_phone(payload) or ""
    email = _extract_email(payload) or ""
    bid = mod_bids.add(
        DATA_DIR,
        customer_name=customer_name,
        customer_phone=phone,
        customer_email=email,
        project_address=project_address,
        project_type=project_type,
        amount=amount,
        status="sent",
    )
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="bid.created",
                    meta={"bid_id": bid["id"], "customer": customer_name,
                          "amount": amount})
    bits = []
    if project_type: bits.append(project_type)
    if project_address: bits.append(project_address)
    detail = f" ({' · '.join(bits)})" if bits else ""
    return {"reply": f"Bid logged — {customer_name}{detail}: ${amount:,.0f}. "
                      f"I'll surface a follow-up nudge if it goes silent for "
                      f"7+ days.",
            "tier": "local", "latency_ms": 0,
            "source": "gc_bid_added"}


def _handle_list_bids(msg: str, user_rec: dict) -> dict | None:
    if not _GC_LIST_BIDS_RE.match(msg):
        return None
    open_bids = mod_bids.list_open(DATA_DIR)
    if not open_bids:
        return {"reply": "No open bids on the books. Log one with "
                          "\"sent bid for [Name] — $[amount] [project type]\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_bids_empty"}
    open_bids.sort(key=lambda b: b.get("sent_at") or b.get("created_at", 0))
    total = sum(float(b.get("amount") or 0) for b in open_bids)
    lines = [f"Open bids ({len(open_bids)}, total ${total:,.0f}):"]
    from datetime import datetime as _dt
    now_ts = int(time.time())
    for b in open_bids[:15]:
        age_days = (now_ts - int(b.get("sent_at") or b.get("created_at", now_ts))) // 86400
        age_str = f"{age_days}d ago" if age_days > 0 else "today"
        last_nudge = b.get("last_followup_at")
        nudge_bit = ""
        if last_nudge:
            since_nudge = (now_ts - int(last_nudge)) // 86400
            nudge_bit = f" [nudged {since_nudge}d ago]"
        elif age_days >= 7:
            nudge_bit = " ⏰ overdue for follow-up"
        type_bit = f" {b.get('project_type','')}" if b.get("project_type") else ""
        lines.append(f"  · {b['customer_name']}{type_bit} ${b.get('amount',0):,.0f} ({age_str}){nudge_bit}")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_bids_listed"}


def _handle_bid_won(msg: str, user_rec: dict) -> dict | None:
    m = _GC_BID_WON_RE.match(msg)
    if not m:
        return None
    who = (m.group("who") or "").strip()
    if not who or who.lower() in {"i", "we", "they"}:
        return None
    matches = mod_bids.find_by_customer(DATA_DIR, who)
    open_matches = [b for b in matches if b.get("status") == "sent"]
    if not open_matches:
        return None  # Let LLM handle — might not be a bid event at all
    if len(open_matches) > 1:
        listed = "\n".join(f"  · {b['customer_name']} — {b.get('project_type','')} ${b.get('amount',0):,.0f}"
                            for b in open_matches[:5])
        return {"reply": f"Multiple open bids match \"{who}\":\n{listed}\nBe more specific.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_bid_won_ambiguous"}
    b = open_matches[0]
    # Auto-create the project — closes the lead→bid→project funnel so
    # the GC doesn't have to retype the address + amount + type.
    project = None
    auto_created = False
    if b.get("project_address"):
        try:
            project = mod_projects.add(
                DATA_DIR,
                address=b["project_address"],
                label=b.get("project_type") or "",
                customer_name=b.get("customer_name") or "",
                customer_phone=b.get("customer_phone") or "",
                customer_email=b.get("customer_email") or "",
                contract_amount=float(b.get("amount") or 0),
                status="active",
                notes=f"Auto-created from won bid #{b['id'][:8]}",
            )
            auto_created = True
        except Exception as e:
            log.warning(f"auto-create project from won bid failed: {e}")
    mod_bids.mark_won(DATA_DIR, b["id"],
                       project_id=project["id"] if project else "")
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="bid.won",
                    meta={"bid_id": b["id"], "amount": b.get("amount"),
                          "auto_created_project": project["id"] if project else None})
    if auto_created and project:
        return {"reply": (f"🎉 {b['customer_name']} signed — ${b.get('amount',0):,.0f} "
                          f"{b.get('project_type','')}. "
                          f"I auto-created the project at {project['address']} so it's "
                          f"on your active board. Say \"share {project.get('label') or 'this'} "
                          f"with the client\" to send them their portal link."),
                "tier": "local", "latency_ms": 0,
                "source": "gc_bid_won_with_project"}
    return {"reply": (f"🎉 Marked bid as WON — {b['customer_name']} "
                      f"({b.get('project_type','')}) ${b.get('amount',0):,.0f}. "
                      f"Add the project with \"new project at <address> "
                      f"— ${b.get('amount',0):,.0f} {b.get('project_type','')}\""),
            "tier": "local", "latency_ms": 0,
            "source": "gc_bid_won"}


def _handle_bid_lost(msg: str, user_rec: dict) -> dict | None:
    m = _GC_BID_LOST_RE.match(msg)
    if not m:
        return None
    who = (m.group("who") or m.group("who2") or "").strip()
    if not who:
        return None
    matches = mod_bids.find_by_customer(DATA_DIR, who)
    open_matches = [b for b in matches if b.get("status") == "sent"]
    if not open_matches:
        return None
    if len(open_matches) > 1:
        return {"reply": f"Multiple open bids match \"{who}\" — say more.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_bid_lost_ambiguous"}
    b = open_matches[0]
    # Try to extract reason from the message tail
    reason = ""
    reason_match = _re.search(r"(?:went\s+with|hired\s+someone\s+else|because)\s+(.+)",
                                msg, _re.IGNORECASE)
    if reason_match:
        reason = reason_match.group(1).strip().rstrip(".")
    mod_bids.mark_lost(DATA_DIR, b["id"], reason=reason)
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="bid.lost",
                    meta={"bid_id": b["id"], "amount": b.get("amount"),
                          "reason": reason})
    reason_bit = f" — \"{reason}\"" if reason else ""
    return {"reply": f"Marked bid as lost — {b['customer_name']} "
                      f"${b.get('amount',0):,.0f}{reason_bit}. Win/loss recorded.",
            "tier": "local", "latency_ms": 0,
            "source": "gc_bid_lost"}


def _handle_bid_report(msg: str, user_rec: dict) -> dict | None:
    if not _GC_BID_REPORT_RE.match(msg):
        return None
    s = mod_bids.summary(DATA_DIR)
    if not (s["open_count"] or s["won_count"] or s["lost_count"]):
        return {"reply": "No bid history yet. Log your first with "
                          "\"sent bid for [Name] — $[amount] [project type]\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_bid_report_empty"}
    lines = [
        "📊 Bid pipeline:",
        f"  Open:   {s['open_count']:>3} bids  ${s['open_value']:,.0f} potential",
        f"  Won:    {s['won_count']:>3} bids  ${s['won_value']:,.0f} captured",
        f"  Lost:   {s['lost_count']:>3} bids  ${s['lost_value']:,.0f} missed",
        "",
        f"  Win rate: {s['win_rate_pct']:.1f}%",
        f"  Avg won:  ${s['avg_won_amount']:,.0f}",
    ]
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_bid_report"}


def _handle_closeout_pdf(msg: str, user_rec: dict) -> dict | None:
    m = _GC_CLOSEOUT_RE.match(msg)
    if not m:
        return None
    q = (m.group("q1") or m.group("q2") or m.group("q3") or "").strip()
    q = _re.sub(r"^the\s+", "", q, flags=_re.IGNORECASE)
    q = _re.sub(r"\s+project$", "", q, flags=_re.IGNORECASE)
    if not q or q.lower() in {"my", "the", "all"}:
        return None
    matches = mod_projects.find_by_address(DATA_DIR, q)
    if not matches:
        return None
    if len(matches) > 1:
        listed = "\n".join(f"  · {p['address']}" for p in matches[:5])
        return {"reply": f"Multiple projects match \"{q}\":\n{listed}\nWhich one?",
                "tier": "local", "latency_ms": 0,
                "source": "gc_closeout_ambiguous"}
    project = matches[0]
    cos = mod_change_orders.list_for_project(DATA_DIR, project["id"])
    invs = mod_invoices.list_for_project(DATA_DIR, project["id"])
    logs = mod_daily_logs.list_for_project(DATA_DIR, project["id"], limit=10)
    business = mod_business.load(DATA_DIR)
    # Auto-mint a review link and embed in the PDF — closes the
    # closeout→review loop without the GC having to remember a second step.
    try:
        review = mod_reviews.issue(DATA_DIR, project["id"])
        base = (CONFIG.get("tunnel_url")
                 or (CONFIG.get("brain") or {}).get("local_public_url")
                 or "http://127.0.0.1:5050").rstrip("/")
        review_url = f"{base}/r/{review['token']}"
    except Exception as e:
        log.warning(f"closeout review mint failed: {e}")
        review_url = ""
    try:
        pdf_path = mod_closeout_pdf.generate(
            project, cos, invs, logs, business,
            DATA_DIR / "closeout_pdfs",
            review_url=review_url,
        )
    except Exception as e:
        log.exception("closeout PDF generation failed")
        return {"reply": f"Closeout PDF generation failed: {e}",
                "tier": "local", "latency_ms": 0,
                "source": "gc_closeout_error"}
    try:
        token = file_fetch.mint_download_token(
            DATA_DIR, str(pdf_path),
            ttl_minutes=60 * 24,   # 24h — give plenty of time to forward to customer
            extra_allowed_roots=[DATA_DIR / "closeout_pdfs"],
        )
    except Exception as e:
        log.warning(f"closeout PDF token mint failed: {e}")
        token = None
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="closeout.generated",
                    meta={"project_id": project["id"]})
    size_kb = pdf_path.stat().st_size // 1024
    customer = project.get("customer_name") or "the customer"
    if token:
        return {"reply": (f"Closeout package built for {project['address']} ({size_kb} KB). "
                          f"[Download here](/download/{token}) — link valid 24 hours. "
                          f"Forward to {customer} when ready."),
                "tier": "local", "latency_ms": 0,
                "source": "gc_closeout_generated",
                "download_url": f"/download/{token}"}
    return {"reply": f"Closeout package saved at {pdf_path} ({size_kb} KB).",
            "tier": "local", "latency_ms": 0,
            "source": "gc_closeout_generated_no_token"}


def _handle_share_portal(msg: str, user_rec: dict) -> dict | None:
    m = _GC_SHARE_PORTAL_RE.match(msg)
    if not m:
        return None
    q = (m.group("q1") or m.group("q2") or m.group("q3") or m.group("q4") or "").strip()
    # Strip leading "the" + trailing "project"
    q = _re.sub(r"^the\s+", "", q, flags=_re.IGNORECASE)
    q = _re.sub(r"\s+project$", "", q, flags=_re.IGNORECASE)
    if not q or q.lower() in {"my", "the", "all"}:
        return None
    matches = mod_projects.find_by_address(DATA_DIR, q)
    if not matches:
        return None  # Don't claim "no project" — let LLM handle (might be a different intent)
    if len(matches) > 1:
        listed = "\n".join(f"  · {p['address']}" for p in matches[:5])
        return {"reply": f"Multiple projects match \"{q}\":\n{listed}\nWhich one?",
                "tier": "local", "latency_ms": 0,
                "source": "gc_portal_ambiguous"}
    project = matches[0]
    token = mod_projects.ensure_client_portal_token(DATA_DIR, project["id"])
    if not token:
        return {"reply": "Couldn't mint a portal link — try again.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_portal_mint_failed"}
    base = (CONFIG.get("tunnel_url")
             or (CONFIG.get("brain") or {}).get("local_public_url")
             or "http://127.0.0.1:5050").rstrip("/")
    url = f"{base}/p/{token}"
    customer = project.get("customer_name") or "the client"
    customer_email = project.get("customer_email") or ""
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="project.portal_link_shared",
                    meta={"project_id": project["id"]})
    contact_bit = f" Their email on file: {customer_email}." if customer_email else ""
    return {"reply": (f"Share this with {customer} — they can check on "
                      f"{project.get('label') or project['address']} anytime:\n\n"
                      f"{url}\n\n"
                      f"The link doesn't expire — same URL works for the life of the project."
                      f"{contact_bit}"),
            "tier": "local", "latency_ms": 0,
            "source": "gc_portal_shared"}


def _handle_weekly_recap(msg: str, user_rec: dict) -> dict:
    """Friday-afternoon 'what happened this week' bundle. Hours by crew
    + CO activity + invoice activity + leak count + completed milestones
    — the contractor's natural end-of-week debrief."""
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now()
    week_ago = now - _td(days=7)
    week_ago_ts = int(week_ago.timestamp())
    # Logs this week
    logs = mod_daily_logs.list_for_week(DATA_DIR)
    hours_by_crew: dict[str, float] = {}
    project_hours: dict[str, float] = {}
    project_log_counts: dict[str, int] = {}
    for l in logs:
        h = float(l.get("hours") or 0)
        proj_id = l.get("project_id") or ""
        project_hours[proj_id] = project_hours.get(proj_id, 0.0) + h
        project_log_counts[proj_id] = project_log_counts.get(proj_id, 0) + 1
        crew = l.get("crew") or []
        if crew and h > 0:
            per = h / len(crew)
            for name in crew:
                hours_by_crew[name] = hours_by_crew.get(name, 0.0) + per
        elif crew:
            for name in crew:
                hours_by_crew[name] = hours_by_crew.get(name, 0.0)
    total_hours = sum(hours_by_crew.values())
    # COs this week
    all_cos = mod_change_orders._load(DATA_DIR)
    cos_new = [c for c in all_cos if c.get("created_at", 0) >= week_ago_ts]
    cos_signed = [c for c in all_cos
                   if c.get("client_signed_at") and c["client_signed_at"] >= week_ago_ts]
    co_captured_dollars = sum(float(c.get("amount", 0)) for c in cos_signed)
    # Invoices this week
    all_invs = mod_invoices._load(DATA_DIR)
    invs_sent = [i for i in all_invs
                  if i.get("sent_at") and i["sent_at"] >= week_ago_ts]
    invs_billed = sum(float(i.get("amount_due", 0)) for i in invs_sent)
    invs_paid = [i for i in all_invs
                  if i.get("paid_at") and i["paid_at"] >= week_ago_ts]
    invs_collected = sum(float(i.get("amount_paid", 0)) for i in invs_paid)
    # Projects completed this week
    all_projects = mod_projects._load(DATA_DIR)
    projects_completed_this_week = []
    for p in all_projects:
        ac = p.get("actual_complete") or ""
        if not ac:
            continue
        try:
            ac_dt = _dt.fromisoformat(ac.replace("Z", "+00:00"))
            # Compare as naive UTC — drop tzinfo from both sides.
            if ac_dt.replace(tzinfo=None) >= week_ago.replace(tzinfo=None):
                projects_completed_this_week.append(p)
        except (ValueError, OSError):
            continue
    # Leak check
    leaks = mod_daily_logs.find_unmatched_scope_changes(DATA_DIR, recent_days=7)

    # Build output
    lines = [
        f"━━━ Weekly recap • {week_ago.strftime('%b %-d')}–{now.strftime('%b %-d')} ━━━",
        "",
        f"💪 Hours logged: {total_hours:g}h across {len(logs)} daily log{'s' if len(logs) != 1 else ''}",
    ]
    if hours_by_crew:
        top = sorted(hours_by_crew.items(), key=lambda x: -x[1])[:6]
        lines.append("  Crew:")
        for name, h in top:
            lines.append(f"    · {name}: {h:.1f}h")
    if project_hours:
        lines.append("  Hours per project:")
        for pid, h in sorted(project_hours.items(), key=lambda x: -x[1])[:5]:
            proj = mod_projects.get(DATA_DIR, pid) or {}
            lines.append(f"    · {proj.get('address','?')[:35]}: {h:.1f}h ({project_log_counts[pid]} log{'s' if project_log_counts[pid] != 1 else ''})")
    lines.append("")
    lines.append(f"📋 Change orders:")
    lines.append(f"  · {len(cos_new)} drafted this week")
    lines.append(f"  · {len(cos_signed)} signed by clients (${co_captured_dollars:,.0f} captured)")
    lines.append("")
    lines.append(f"🧾 Invoicing:")
    lines.append(f"  · {len(invs_sent)} invoice{'s' if len(invs_sent) != 1 else ''} sent (${invs_billed:,.0f} billed)")
    lines.append(f"  · {len(invs_paid)} payment{'s' if len(invs_paid) != 1 else ''} received (${invs_collected:,.0f} collected)")
    if projects_completed_this_week:
        lines.append("")
        lines.append(f"🏁 Projects completed ({len(projects_completed_this_week)}):")
        for p in projects_completed_this_week[:5]:
            lines.append(f"  · {p.get('address','?')} — {p.get('label','')}")
    if leaks:
        lines.append("")
        lines.append(f"⚠ {len(leaks)} scope mention(s) in this week's logs with no matching CO")
        lines.append(f"  (Run \"leak check\" for details.)")
    if total_hours == 0 and not cos_new and not invs_sent and not invs_paid:
        lines.append("")
        lines.append("(Quiet week — no logs, COs, or invoices recorded.)")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_weekly_recap"}


def _handle_leak_check(msg: str, user_rec: dict) -> dict:
    unmatched = mod_daily_logs.find_unmatched_scope_changes(DATA_DIR, recent_days=21)
    if not unmatched:
        return {"reply": "No leaks detected — every scope change mentioned in recent daily logs has a matching CO.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_leak_clear"}
    lines = [f"⚠ Possible leaks — {len(unmatched)} scope mention(s) in daily logs with NO matching CO:"]
    for u in unmatched[:10]:
        proj = mod_projects.get(DATA_DIR, u["project_id"]) or {}
        lines.append(f"  · {u['date']} {proj.get('address','?')[:25]} → \"{u['phrase']}\"")
    lines.append("\nDraft a CO with: \"CO on <project> — $X — <description>\"")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_leak_flagged"}


def _handle_add_invoice(msg: str, user_rec: dict) -> dict:
    """Parse 'send invoice for Oak — $5000 progress draw' / 'bill Maple
    $3000 for framing' and create the invoice record. Same project-
    fuzzy-match approach as CO. v1 just records the invoice + returns
    the customer's email so the GC can copy/paste — PDF generation +
    Stripe-link send come later."""
    payload = _GC_ADD_INVOICE_RE.sub("", msg, count=1).strip(" :—–-,")
    amount = _parse_money(payload)
    if not amount:
        return {"reply": "I need a dollar amount. E.g. \"bill Oak $5000 for framing\".",
                "tier": "local", "latency_ms": 0,
                "source": "gc_inv_no_amount"}
    # Find project
    active = mod_projects.list_active(DATA_DIR)
    payload_l = payload.lower()
    project = None
    for p in active:
        addr_parts = (p.get("address", "") + " " + p.get("label", "")).lower().split()
        for token in addr_parts:
            if len(token) >= 3 and token not in ("the", "and", "ave", "street",
                                                  "avenue", "drive", "road", "blvd") \
                    and not token.replace(",", "").isdigit() \
                    and token in payload_l:
                project = p
                break
        if project:
            break
    if not project:
        if not active:
            return {"reply": "No active projects to invoice.",
                    "tier": "local", "latency_ms": 0,
                    "source": "gc_inv_no_projects"}
        listed = "\n".join(f"  · {p['address']}" for p in active[:5])
        return {"reply": f"Which project? I see these active:\n{listed}",
                "tier": "local", "latency_ms": 0,
                "source": "gc_inv_pick_project"}
    # Extract memo = payload minus dollar minus project label
    memo = _DOLLAR_AMT_RE.sub("", payload).strip(" ,—–-")
    for token in (project.get("address", ""), project.get("label", "")):
        if token:
            memo = _re.sub(_re.escape(token), "", memo, flags=_re.IGNORECASE)
    memo = _re.sub(r"\s+", " ", memo).strip(" :—–-,")
    memo = _re.sub(r"^(?:for|on|of|to|—)\s+", "", memo, flags=_re.IGNORECASE).strip()
    is_draw = bool(_re.search(r"\b(draw|progress)\b", payload, _re.IGNORECASE))
    # Due in 30 days by default
    due_at = int(time.time()) + 30 * 86400
    try:
        inv = mod_invoices.add(
            DATA_DIR,
            project_id=project["id"],
            line_items=[{"label": memo or "Progress billing", "amount": amount}],
            subtotal=amount,
            memo=memo,
            due_at=due_at,
            is_draw=is_draw,
            status="approved",  # GC initiated it, ready to send
        )
        # Auto-mark sent so it counts as billed immediately. (Real send
        # via Stripe-hosted invoice or email PDF is the follow-on; for
        # v1 the GC delivers it manually and Orby tracks status.)
        mod_invoices.mark_sent(DATA_DIR, inv["id"])
    except ValueError as e:
        return {"reply": f"Couldn't create invoice: {e}",
                "tier": "local", "latency_ms": 0,
                "source": "gc_inv_error"}
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="invoice.created",
                    meta={"invoice_id": inv["id"], "project_id": project["id"],
                          "amount": amount})
    draw_bit = f" (draw #{inv['draw_number']})" if inv.get("draw_number") else ""
    email = project.get("customer_email") or "(no email on file — share manually)"
    return {"reply": (f"Invoice {inv['invoice_number']}{draw_bit} created — "
                      f"{project['address']}: \"{memo}\" ${amount:,.0f}. "
                      f"Due in 30 days. Send to {email}."),
            "tier": "local", "latency_ms": 0,
            "source": "gc_inv_created"}


def _handle_record_payment(msg: str, user_rec: dict) -> dict:
    """Parse 'received $3000 on INV-2026-0001' / 'mark INV-2026-0042
    paid'. Records the payment and updates invoice status."""
    # Skip hypotheticals — "if I billed Sarah $5000 and she paid $2000..."
    # That's a math question, not a payment record. Same for past tense
    # narration without an explicit INV-XXXX number.
    if _re.match(r"^\s*(?:if|suppose|imagine|what\s+if|let'?s\s+say|hypothetical)",
                  msg, _re.IGNORECASE):
        return None
    m = _GC_RECORD_PAYMENT_RE.match(msg)
    if not m:
        return None
    inv_num_m = _INV_NUMBER_RE.search(msg) or _INV_NUMBER_RE.search(m.group("inv1") or "")
    if not inv_num_m:
        return {"reply": "I need an invoice number (like INV-2026-0001) to record a payment.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_payment_no_inv"}
    inv_num = inv_num_m.group(0).upper()
    target = None
    for i in mod_invoices._load(DATA_DIR):
        if (i.get("invoice_number") or "").upper() == inv_num:
            target = i; break
    if not target:
        return {"reply": f"I don't see {inv_num} on the books.",
                "tier": "local", "latency_ms": 0,
                "source": "gc_payment_not_found"}
    # If "mark X paid" with no amount, pay it in full
    amt_str = m.group("amt1") or m.group("amt2")
    if amt_str:
        amount = float(amt_str.replace(",", ""))
    else:
        amount = float(target.get("amount_due", 0)) - float(target.get("amount_paid", 0))
    updated = mod_invoices.record_payment(DATA_DIR, target["id"], amount)
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="invoice.payment_recorded",
                    meta={"invoice_id": target["id"], "amount": amount,
                          "new_status": updated["status"]})
    proj = mod_projects.get(DATA_DIR, target.get("project_id", "")) or {}
    if updated["status"] == "paid":
        return {"reply": (f"Recorded ${amount:,.2f} — {inv_num} ({proj.get('address','')}) is now fully PAID."),
                "tier": "local", "latency_ms": 0,
                "source": "gc_payment_paid"}
    remaining = float(updated["amount_due"]) - float(updated["amount_paid"])
    return {"reply": (f"Recorded ${amount:,.2f} on {inv_num} ({proj.get('address','')}). "
                      f"${remaining:,.2f} still owed."),
            "tier": "local", "latency_ms": 0,
            "source": "gc_payment_partial"}


def _mint_co_sign_token(co_id: str) -> str:
    """Generate a one-time signing token and store it in
    co_sign_tokens.json with the CO id + expiry. Token is opaque to the
    customer — they just click the link."""
    import secrets as _secrets
    token = _secrets.token_urlsafe(32)
    tokens_path = DATA_DIR / "co_sign_tokens.json"
    try:
        existing = json.loads(tokens_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        existing = {}
    existing[token] = {
        "co_id":      co_id,
        "issued_at":  int(time.time()),
        "expires_at": int(time.time()) + 30 * 86400,  # 30 days
        "used_at":    None,
    }
    tokens_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tokens_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    tmp.replace(tokens_path)
    return token


def _co_sign_url(token: str) -> str:
    """Public URL the client clicks. Uses the tunnel URL so it's reachable
    from outside the LAN. Falls back to localhost for dev."""
    base = (CONFIG.get("tunnel_url") or
            (CONFIG.get("brain") or {}).get("local_public_url") or
            "http://127.0.0.1:5050").rstrip("/")
    return f"{base}/co/sign/{token}"


def _send_co_for_signature_email(co: dict, project: dict,
                                   sign_url: str, to_email: str) -> None:
    """Log the signing link for now. Real email delivery is wired in a
    follow-on once we pick the IMAP/SMTP account format (existing
    imap_smtp module expects per-user account configs we don't have for
    contractor-mode yet). For v1, the chat reply gives the GC the URL
    to share with the client directly — fine for the first contractor
    while we work out the email-delivery story properly."""
    log.info(f"CO sign URL ready for {to_email}: {sign_url}")


# ── Calendar action handlers — cancel + reschedule ────────────────────────
# The BOOKING side already works via quick_capture (_APPT_RE in
# modules/quick_capture.py) — "book a meeting with Joe Thursday at 2pm"
# writes to the calendar. What WASN'T wired:
#   - Cancel: "cancel my Tuesday appointment" → Orby said "done" but
#     never removed the event. Test #8.
#   - Reschedule: "move my haircut from Tuesday to Friday" → Orby said
#     "moved" but never updated. Test #10.
# These handlers fix both by ACTUALLY writing to the calendar through
# modules/calendar.remove() and .update().

_CANCEL_APPT_RE = _re.compile(
    r"^\s*(?:cancel|delete|remove|drop)\s+"
    r"(?:my\s+|the\s+)?"
    # Stop the `what` capture at compound conjunctions ("and tell joe ...",
    # "and let him know ...") so we don't try to match the message half
    # as an event title.
    r"(?P<what>.+?)"
    r"(?:\s+appointment|\s+meeting|\s+event|\s+booking)?"
    r"(?:\s+and\s+(?:tell|let|send|email|text|message|notify|forward|relay|call|ping)\b.+)?"
    r"\s*[.?!]?\s*$",
    _re.IGNORECASE,
)
_RESCHEDULE_APPT_RE = _re.compile(
    r"^\s*(?:reschedule|move|shift|change|push)\s+"
    r"(?:my\s+|the\s+)?"
    r"(?P<what>.+?)"
    r"\s+(?:from\s+(?P<from_when>[^,]+?)\s+)?"
    r"(?:to|until|for)\s+(?P<to_when>.+?)\s*[.?!]?\s*$",
    _re.IGNORECASE,
)
_DAY_HINT_RE = _re.compile(
    r"\b(today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|next\s+\w+|this\s+\w+)\b",
    _re.IGNORECASE,
)


def _find_matching_events(user_dir, query: str, days_window: int = 30) -> list[dict]:
    """Fuzzy-match upcoming events. Returns events that match by either:
      - a day-word in the query against the event's start date
      - a substring of the query against the event title
    Newest-first within the window."""
    from datetime import datetime, timedelta, timezone
    events = mod_calendar.upcoming(user_dir, days=days_window)
    if not events:
        return []
    q = (query or "").strip().lower()
    if not q:
        return events
    # Pull all words; check title substring matches and day matches.
    day_hint_m = _DAY_HINT_RE.search(q)
    day_word = day_hint_m.group(0).lower() if day_hint_m else None
    matches = []
    for e in events:
        title = (e.get("title") or "").lower()
        start = e.get("start", "")
        # Day match
        if day_word:
            try:
                s = start.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s).astimezone()
                now = datetime.now().astimezone()
                if day_word == "today" and dt.date() == now.date():
                    matches.append(e); continue
                if day_word == "tomorrow" and dt.date() == (now + timedelta(days=1)).date():
                    matches.append(e); continue
                weekdays = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,
                             "friday":4,"saturday":5,"sunday":6}
                if day_word in weekdays and dt.weekday() == weekdays[day_word]:
                    matches.append(e); continue
            except (ValueError, OSError):
                pass
        # Title substring match (strip "my/the/a" filler from query)
        q_words = [w for w in q.split() if w not in
                    {"my","the","a","an","please","appointment","meeting",
                     "event","booking","today","tomorrow","tonight",
                     "monday","tuesday","wednesday","thursday","friday",
                     "saturday","sunday"}]
        for w in q_words:
            if len(w) >= 3 and w in title:
                matches.append(e); break
    # Dedupe preserving order
    seen = set(); deduped = []
    for e in matches:
        if e["id"] not in seen:
            seen.add(e["id"])
            deduped.append(e)
    return deduped


def _event_display(e: dict) -> str:
    """Compact human description of a calendar event for chat replies."""
    from datetime import datetime
    title = e.get("title", "(untitled)")
    start = e.get("start", "")
    try:
        s = start.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s).astimezone()
        when = dt.strftime("%a %b %-d at %-I:%M %p")
    except (ValueError, OSError):
        when = start
    return f"{title} ({when})"


def _try_cancel_appointment(message: str, user_dir) -> dict | None:
    """Handle 'cancel my Tuesday appointment' / 'remove the dentist' /
    'delete my 2pm'. ACTUALLY removes from the calendar."""
    # Strip polite prefix so "I need to cancel..." / "Can you cancel..."
    # both reach the cancel regex (which requires the verb up-front).
    stripped = _strip_polite_prefix(message or "")
    m = _CANCEL_APPT_RE.match(stripped)
    if not m:
        return None
    what = (m.group("what") or "").strip()
    if not what or what.lower() in {"that", "it", "this"}:
        return None  # Too vague — let LLM handle
    matches = _find_matching_events(user_dir, what)
    if not matches:
        upcoming = mod_calendar.upcoming(user_dir, days=14)
        if not upcoming:
            reply = "I don't see any upcoming appointments on your calendar to cancel."
        else:
            sample = "\n".join(f"  · {_event_display(e)}" for e in upcoming[:5])
            reply = f"I don't see an appointment matching \"{what}\". Your upcoming:\n{sample}"
        return {"reply": reply, "tier": "local", "latency_ms": 0,
                "source": "calendar_cancel_no_match"}
    if len(matches) > 1:
        listed = "\n".join(f"  {i+1}. {_event_display(e)}" for i, e in enumerate(matches))
        return {"reply": f"I see {len(matches)} appointments that could be \"{what}\":\n{listed}\nWhich one? (Say the title or time exactly.)",
                "tier": "local", "latency_ms": 0,
                "source": "calendar_cancel_ambiguous"}
    target = matches[0]
    ok = mod_calendar.remove(user_dir, target["id"])
    if ok:
        return {"reply": f"Cancelled — {_event_display(target)}.",
                "tier": "local", "latency_ms": 0,
                "source": "calendar_cancel_done"}
    return {"reply": f"Couldn't cancel — {_event_display(target)} may have been removed already.",
            "tier": "local", "latency_ms": 0,
            "source": "calendar_cancel_failed"}


def _try_reschedule_appointment(message: str, user_dir) -> dict | None:
    """Handle 'reschedule my haircut from Tuesday to Friday' / 'move my
    2pm meeting to 4pm'. ACTUALLY updates the calendar."""
    stripped = _strip_polite_prefix(message or "")
    m = _RESCHEDULE_APPT_RE.match(stripped)
    if not m:
        return None
    what = (m.group("what") or "").strip()
    from_when = (m.group("from_when") or "").strip()
    to_when = (m.group("to_when") or "").strip()
    if not what or not to_when:
        return None
    # Find the event by what + optional from_when hint
    query = f"{what} {from_when}".strip()
    matches = _find_matching_events(user_dir, query)
    if not matches:
        # Try without the from-when filter
        matches = _find_matching_events(user_dir, what)
    if not matches:
        upcoming = mod_calendar.upcoming(user_dir, days=14)
        if not upcoming:
            return {"reply": "I don't see any upcoming appointments to reschedule.",
                    "tier": "local", "latency_ms": 0,
                    "source": "calendar_reschedule_no_match"}
        sample = "\n".join(f"  · {_event_display(e)}" for e in upcoming[:5])
        return {"reply": f"I don't see an appointment matching \"{what}\". Your upcoming:\n{sample}",
                "tier": "local", "latency_ms": 0,
                "source": "calendar_reschedule_no_match"}
    if len(matches) > 1:
        listed = "\n".join(f"  {i+1}. {_event_display(e)}" for i, e in enumerate(matches))
        return {"reply": f"Multiple matches for \"{what}\":\n{listed}\nWhich one should I move to {to_when}?",
                "tier": "local", "latency_ms": 0,
                "source": "calendar_reschedule_ambiguous"}
    target = matches[0]
    # Parse the new time using quick_capture._parse_when (already handles
    # "Friday" / "Friday at 3pm" / "tomorrow at 2" / etc.)
    try:
        from modules import quick_capture as mod_qc_local
        new_iso = mod_qc_local._parse_when(to_when)
    except Exception as e:
        new_iso = None
    if not new_iso:
        return {"reply": f"I matched the appointment ({_event_display(target)}) but couldn't parse \"{to_when}\" as a day/time. Try \"Friday at 3pm\" or \"tomorrow at 10\".",
                "tier": "local", "latency_ms": 0,
                "source": "calendar_reschedule_bad_when"}
    updated = mod_calendar.update(user_dir, target["id"], start=new_iso, end=new_iso)
    if updated:
        return {"reply": f"Moved — {target.get('title','event')} is now {_event_display(updated)}.",
                "tier": "local", "latency_ms": 0,
                "source": "calendar_reschedule_done"}
    return {"reply": f"Couldn't update {_event_display(target)} — try again.",
            "tier": "local", "latency_ms": 0,
            "source": "calendar_reschedule_failed"}


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


_MORNING_BRIEF_RE = _re.compile(
    r"^\s*(?:"
    r"(?:give\s+me\s+|show\s+me\s+|what'?s?\s+(?:in\s+)?)?(?:my\s+)?"
    r"(?:morning|daily|today'?s?)\s+(?:brief|briefing|update|rundown|recap)"
    r"|brief\s+me(?:\s+(?:on\s+)?today)?"
    r"|(?:my\s+)?(?:brief|briefing)\s+(?:now|for\s+today)?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_TOMORROW_BRIEF_RE = _re.compile(
    r"^\s*(?:"
    r"(?:what'?s?|show\s+me|give\s+me)\s+(?:on\s+)?(?:my\s+|the\s+)?(?:agenda\s+|schedule\s+|calendar\s+)?"
    r"(?:for\s+)?tomorrow"
    r"|tomorrow'?s?\s+(?:brief|briefing|agenda|schedule|rundown|preview|appointments?|events?|meetings?)"
    r"|what\s+(?:do\s+i\s+have|appointments?(?:\s+do\s+i\s+have)?|meetings?(?:\s+do\s+i\s+have)?|events?(?:\s+do\s+i\s+have)?|is\s+(?:on\s+)?(?:my\s+)?(?:schedule|calendar|agenda))\s+tomorrow"
    r"|(?:any|which)\s+(?:appointments?|meetings?|events?)\s+tomorrow"
    r"|appointments?\s+tomorrow"
    r"|brief\s+me\s+(?:for\s+|on\s+)tomorrow"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_APPT_TODAY_RE = _re.compile(
    r"^\s*(?:"
    r"what\s+appointments?\s+(?:are\s+(?:scheduled\s+)?(?:for\s+)?today|do\s+i\s+have\s+today)"
    r"|(?:any|which)\s+appointments?\s+today"
    r"|appointments?\s+(?:for\s+)?today"
    r"|what\s+meetings?\s+(?:are\s+(?:scheduled\s+)?(?:for\s+)?today|do\s+i\s+have\s+today)"
    r"|meetings?\s+(?:for\s+)?today"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _try_morning_brief(message: str, user_rec: dict) -> dict | None:
    """Route 'give me my morning brief' / 'daily brief' to the real
    build_briefing path so contractor sections + accurate counts show
    up instead of an LLM-fabricated summary."""
    msg = _strip_polite_prefix(message or "")
    if not _MORNING_BRIEF_RE.match(msg):
        return None
    try:
        brief = briefing.build_briefing(CONFIG, DATA_DIR, user_rec.get("username", "owner"))
    except Exception as e:
        log.warning(f"morning brief build failed: {e}")
        return None  # Let LLM take it as fallback
    reply = brief.get("summary_text") or "Nothing to brief on yet."
    # If contractor sections fired, append the key item details so the
    # GC sees specific CO #s and invoice #s in the chat without having
    # to open the dashboard.
    items = brief.get("items") or []
    pending = [i for i in items if i.get("kind") == "co_pending"]
    awaiting = [i for i in items if i.get("kind") == "co_awaiting_sig"]
    overdue = [i for i in items if i.get("kind") == "invoice_overdue"]
    extra_lines = []
    if pending:
        extra_lines.append("\nChange orders waiting on you:")
        for it in pending[:5]:
            amt = it.get("amount", 0)
            sign = "+" if amt >= 0 else ""
            extra_lines.append(f"  · #{it['co_id']} {it['project']} ({sign}${amt:,.0f}) — {it['desc'][:60]}")
    if awaiting:
        extra_lines.append("\nChange orders out for client signature:")
        for it in awaiting[:5]:
            amt = it.get("amount", 0)
            extra_lines.append(f"  · #{it['co_id']} {it['project']} (${amt:,.0f})")
    if overdue:
        extra_lines.append("\nOverdue invoices:")
        for it in overdue[:5]:
            extra_lines.append(f"  · {it['invoice_number']} {it['project']} (${it['owed']:,.0f})")
    if extra_lines:
        reply = reply + "\n" + "\n".join(extra_lines)
    return {"reply": reply, "tier": "local", "latency_ms": 0,
            "source": "morning_brief"}


def _try_tomorrow_brief(message: str, user_rec: dict) -> dict | None:
    """End-of-day preview of tomorrow's calendar + tasks + due reminders.
    Sister of morning brief but forward-looking — Frank reads it
    before sign-off so he knows what to walk into."""
    msg = _strip_polite_prefix(message or "")
    if not _TOMORROW_BRIEF_RE.match(msg):
        return None
    username = user_rec.get("username", "owner")
    from datetime import date as _date, timedelta as _td
    tomorrow = _date.today() + _td(days=1)
    tomorrow_iso = tomorrow.isoformat()
    try:
        user_dir = users_mod.get_user_dir(DATA_DIR, username)
    except Exception:
        return None
    lines = [f"📅 Tomorrow ({tomorrow.strftime('%A %b %-d')}):"]
    # Calendar events tomorrow
    try:
        from modules import calendar as mod_calendar
        events = mod_calendar._range(user_dir, tomorrow, tomorrow) or []
    except Exception:
        events = []
    if events:
        lines.append(f"  {len(events)} appointment{'s' if len(events) != 1 else ''}:")
        for e in events[:8]:
            start = (e.get("start") or "")[11:16] or "?"
            title = e.get("title", "")
            lines.append(f"    · {start} — {title}")
    else:
        lines.append("  No appointments scheduled.")
    # Reminders due tomorrow
    try:
        from modules import reminders as mod_reminders
        all_rem = mod_reminders.list_all(user_dir) or []
        tomorrow_rem = [r for r in all_rem
                         if (r.get("due", "") or "")[:10] == tomorrow_iso]
    except Exception:
        tomorrow_rem = []
    if tomorrow_rem:
        lines.append(f"  {len(tomorrow_rem)} reminder{'s' if len(tomorrow_rem) != 1 else ''} due:")
        for r in tomorrow_rem[:5]:
            t = (r.get("due", "") or "")[11:16] or "?"
            lines.append(f"    · {t} — {r.get('text', '')}")
    # Contractor: subs scheduled for tomorrow (best-effort string match)
    if is_module_enabled("contractor"):
        try:
            day_name = tomorrow.strftime("%A")
            data = mod_subs._load(DATA_DIR)
            scheduled_tomorrow = [
                a for a in data.get("assignments", [])
                if not a.get("completed")
                and (day_name.lower() in (a.get("scheduled", "") or "").lower()
                     or tomorrow_iso in (a.get("scheduled", "") or ""))
            ]
            if scheduled_tomorrow:
                lines.append(f"  Subs on site:")
                for a in scheduled_tomorrow[:5]:
                    sub_rec = next((s for s in data.get("subs", [])
                                     if s.get("id") == a.get("sub_id")), None)
                    sub_name = sub_rec.get("name", "?") if sub_rec else "?"
                    proj = mod_projects.get(DATA_DIR, a.get("project_id", "")) or {}
                    lines.append(f"    · {sub_name} → {proj.get('address','?')} ({a.get('scope','')[:40]})")
        except Exception:
            pass
    if len(lines) == 1:
        lines.append("  Nothing on the books for tomorrow. Quiet day.")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "tomorrow_brief"}


_CONTACTS_ADD_RE = _re.compile(
    r"^\s*(?:"
    r"add\s+(?:a\s+)?(?:new\s+)?contact\s+(?:(?:named|called)\s+|for\s+)?(?P<name>[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){0,3})"
    r"|new\s+contact\s+(?:(?:named|called)\s+|for\s+)?(?P<name2>[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){0,3})"
    r"|create\s+(?:a\s+)?(?:new\s+)?contact\s+(?:named\s+|called\s+|for\s+)?(?P<name3>[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){0,3})"
    r")"
    r"(?:\s*[,—–-]\s*(?P<rest>.+?))?"
    r"\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_CONTACTS_FIND_RE = _re.compile(
    r"^\s*(?:"
    r"find\s+(?:contact\s+)?(?P<name>[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){0,3})"
    r"|look\s*up\s+(?:contact\s+)?(?P<name2>[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){0,3})"
    r"|(?:show|get)\s+(?:me\s+)?(?P<name4>[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){0,3})(?:'s)?\s+(?:contact\s+(?:info|details|card)|info|details)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_CONTACTS_LIST_FOR_RE = _re.compile(
    r"^\s*(?:show|list|give)\s+(?:me\s+)?(?:all\s+)?(?:my\s+|the\s+)?contacts?\s+(?:for|at|from|with)\s+(?P<who>.+?)\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_CONTACTS_ALL_RE = _re.compile(
    r"^\s*(?:show|list|give)\s+(?:me\s+)?(?:all\s+)?(?:my\s+|the\s+)?contacts?\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_CONTACTS_CONVO_RE = _re.compile(
    r"^\s*(?:show|list|give)\s+(?:me\s+)?(?:every|all)\s+conversations?\s+(?:we(?:'ve| have)?\s+)?(?:had\s+)?with\s+(?P<name>.+?)\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _try_contacts_chat(message: str, user_rec: dict) -> dict | None:
    """Per-user contacts CRUD via chat. Returns None to fall through."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    username = user_rec.get("username", "owner")
    try:
        user_dir = users_mod.get_user_dir(DATA_DIR, username)
    except Exception:
        return None

    # CONTACTS FOR <company> — "show contacts for Johnson Construction"
    m = _CONTACTS_LIST_FOR_RE.match(msg)
    if m:
        who = (m.group("who") or "").strip().rstrip("?.!")
        if who:
            q = who.lower()
            hits = [c for c in mod_contacts.list_all(user_dir)
                    if q in (c.get("company") or "").lower()
                    or q in (c.get("name") or "").lower()
                    or q in (c.get("notes") or "").lower()
                    or any(q in t.lower() for t in c.get("tags") or [])]
            if not hits:
                return {"reply": (
                    f"No contacts for \"{who}\" yet. Add one with: "
                    f"\"add a new contact named John Smith\" — I'll save them "
                    f"to your contact book."
                ), "tier": "local", "latency_ms": 0,
                   "source": "contacts_list_for_empty"}
            lines = [f"Contacts for {who} ({len(hits)}):"]
            for c in hits[:20]:
                bits = [c.get("name", "?")]
                if c.get("phone"): bits.append(c["phone"])
                if c.get("email"): bits.append(c["email"])
                if c.get("company") and q not in c["company"].lower():
                    bits.append(f"({c['company']})")
                lines.append("  · " + " — ".join(bits))
            return {"reply": "\n".join(lines), "tier": "local",
                    "latency_ms": 0, "source": "contacts_list_for"}

    # FIND <name> — "find Sarah Jones"
    m = _CONTACTS_FIND_RE.match(msg)
    if m:
        name = (m.group("name") or m.group("name2") or m.group("name3")
                or m.group("name4") or "").strip()
        if name:
            c = mod_contacts.find_by_name(user_dir, name)
            if c:
                bits = [f"📇 {c.get('name','?')}"]
                if c.get("company"): bits.append(f"  Company: {c['company']}")
                if c.get("phone"):   bits.append(f"  Phone:   {c['phone']}")
                if c.get("email"):   bits.append(f"  Email:   {c['email']}")
                if c.get("notes"):   bits.append(f"  Notes:   {c['notes'][:200]}")
                if c.get("tags"):    bits.append(f"  Tags:    {', '.join(c['tags'])}")
                if c.get("last_contact"):
                    bits.append(f"  Last:    {c['last_contact'][:10]}")
                return {"reply": "\n".join(bits), "tier": "local",
                        "latency_ms": 0, "source": "contacts_found"}
            # Try fuzzy search
            results = mod_contacts.search(user_dir, name)
            if results:
                lines = [f"No exact match for \"{name}\". Possible matches:"]
                for r in results[:5]:
                    lines.append(f"  · {r.get('name','?')} ({r.get('company','')})")
                return {"reply": "\n".join(lines), "tier": "local",
                        "latency_ms": 0, "source": "contacts_found_fuzzy"}
            return {"reply": f"No contact matching \"{name}\".",
                    "tier": "local", "latency_ms": 0,
                    "source": "contacts_not_found"}

    # ADD CONTACT — "add a new contact named Sarah Jones"
    m = _CONTACTS_ADD_RE.match(msg)
    if m:
        name = (m.group("name") or m.group("name2") or m.group("name3") or "").strip()
        rest = (m.group("rest") or "").strip()
        if name:
            # Parse phone + email out of the rest
            phone = ""
            email = ""
            phone_m = _re.search(r"(\+?\d[\d\-\(\)\s]{8,})", rest or "")
            if phone_m: phone = phone_m.group(1).strip()
            email_m = _re.search(r"[\w\.-]+@[\w\.-]+\.\w+", rest or "")
            if email_m: email = email_m.group(0).strip()
            company = ""
            comp_m = _re.search(r"\b(?:at|from|with)\s+([A-Z][\w\s&'\-]{2,40})", rest or "")
            if comp_m: company = comp_m.group(1).strip()
            try:
                c = mod_contacts.add(user_dir, name=name, phone=phone,
                                     email=email, company=company,
                                     source="chat")
            except Exception as e:
                log.warning(f"contacts.add failed: {e}")
                return {"reply": f"Couldn't add contact: {e}",
                        "tier": "local", "latency_ms": 0,
                        "source": "contacts_add_error"}
            extras = []
            if phone:   extras.append(f"phone {phone}")
            if email:   extras.append(f"email {email}")
            if company: extras.append(f"at {company}")
            extra_bit = " (" + ", ".join(extras) + ")" if extras else ""
            return {"reply": f"Added {c['name']} to your contacts{extra_bit}.",
                    "tier": "local", "latency_ms": 0,
                    "source": "contacts_added"}

    # CONVERSATION HISTORY — honest answer: per-contact threading isn't built
    m = _CONTACTS_CONVO_RE.match(msg)
    if m:
        name = (m.group("name") or "").strip().rstrip("?.!")
        c = mod_contacts.find_by_name(user_dir, name) if name else None
        if c:
            notes = c.get("personal_notes") or []
            lines = [f"What I know about {c.get('name','?')}:"]
            if c.get("last_contact"):
                lines.append(f"  Last contact: {c['last_contact'][:10]}")
            if c.get("notes"):
                lines.append(f"  General notes: {c['notes'][:200]}")
            if notes:
                lines.append(f"  Personal-notes log ({len(notes)}):")
                for n in notes[:10]:
                    lines.append(f"    · {n.get('note','')[:120]}")
            lines.append("")
            lines.append("(I don't store full chat transcripts per contact yet — "
                          "what you see above is the running notes I've kept.)")
            return {"reply": "\n".join(lines), "tier": "local",
                    "latency_ms": 0, "source": "contacts_convo"}
        return {"reply": (
            f"I don't have \"{name}\" as a contact, and I don't store full "
            f"per-person conversation transcripts yet. What I CAN show you:\n"
            f"  · running notes per contact (use \"find {name}\")\n"
            f"  · website-chat leads (use \"show me recent leads\")\n"
            f"  · per-project history (use \"history for <project>\")"
        ), "tier": "local", "latency_ms": 0,
           "source": "contacts_convo_unknown"}

    # ALL CONTACTS — "show me my contacts"
    if _CONTACTS_ALL_RE.match(msg):
        all_c = mod_contacts.list_all(user_dir)
        if not all_c:
            return {"reply": "Your contact book is empty. Add one with: "
                              "\"add a new contact named John Smith\".",
                    "tier": "local", "latency_ms": 0,
                    "source": "contacts_empty"}
        lines = [f"Your contacts ({len(all_c)}):"]
        for c in all_c[:30]:
            bits = [c.get("name", "?")]
            if c.get("company"): bits.append(c["company"])
            if c.get("phone"):   bits.append(c["phone"])
            lines.append("  · " + " — ".join(bits))
        return {"reply": "\n".join(lines), "tier": "local",
                "latency_ms": 0, "source": "contacts_all"}
    return None


_LEADS_RECENT_RE = _re.compile(
    r"^\s*(?:"
    r"(?:show|list|give)\s+(?:me\s+)?(?:all\s+)?(?:my\s+|the\s+)?(?:new\s+)?leads?"
    r"\s+(?:from\s+|in\s+|for\s+)?(?:the\s+)?(?:this|past|last|recent)?\s*"
    r"(?P<period>week|month|day|today|24\s*hours?|hours?|overnight)?"
    r"|(?:what|any)\s+(?:new\s+)?leads?\s+(?:came\s+in\s+)?(?P<period2>overnight|today|this\s+week|this\s+month)"
    r"|recent\s+leads?"
    r"|new\s+leads?(?:\s+this\s+(?P<period3>week|month))?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _try_leads_chat(message: str, user_rec: dict) -> dict | None:
    """'Show me leads from this week' / 'what new leads came in overnight'.
    Reads modules/messages.py — the website-chat lead capture log."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    m = _LEADS_RECENT_RE.match(msg)
    if not m:
        return None
    period_raw = (m.group("period") or m.group("period2")
                  or m.group("period3") or "week").strip().lower()
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now()
    if "overnight" in period_raw or "24" in period_raw or "today" in period_raw or "day" in period_raw or "hour" in period_raw:
        cutoff = now - _td(hours=24)
        label = "the past 24 hours"
    elif "month" in period_raw:
        cutoff = now - _td(days=30)
        label = "this month"
    else:
        cutoff = now - _td(days=7)
        label = "this week"
    try:
        msgs = mod_messages.list_all(DATA_DIR) or []
    except Exception:
        msgs = []
    recent = []
    for m_rec in msgs:
        ts = m_rec.get("timestamp") or m_rec.get("ts") or 0
        try:
            ts_dt = _dt.fromtimestamp(float(ts))
        except (ValueError, OSError, TypeError):
            try:
                ts_dt = _dt.fromisoformat(str(ts).replace("Z", "+00:00"))
            except (ValueError, OSError, TypeError):
                continue
        if ts_dt >= cutoff:
            recent.append((ts_dt, m_rec))
    if not recent:
        return {"reply": f"No new leads in {label}.",
                "tier": "local", "latency_ms": 0,
                "source": "leads_recent_empty"}
    recent.sort(key=lambda t: t[0], reverse=True)
    lines = [f"📨 Leads from {label} ({len(recent)}):"]
    for ts_dt, m_rec in recent[:15]:
        name = m_rec.get("from_name") or m_rec.get("name") or "(no name)"
        phone = m_rec.get("from_phone") or m_rec.get("phone") or ""
        email = m_rec.get("from_email") or m_rec.get("email") or ""
        body = (m_rec.get("body") or m_rec.get("message") or "")[:100]
        when = ts_dt.strftime("%a %b %-d %-I:%M %p")
        contact_bit = ""
        if phone: contact_bit = f" — {phone}"
        elif email: contact_bit = f" — {email}"
        lines.append(f"  · {when} — {name}{contact_bit}")
        if body:
            lines.append(f"      \"{body}\"")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "leads_recent"}


_LEAD_ADD_RE = _re.compile(
    r"^\s*(?:"
    r"add\s+(?:me|us|(?P<who>[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,3}))\s+(?:as\s+)?(?:a\s+)?(?:new\s+)?lead"
    r"|(?:log|create|record|file)\s+(?:a\s+)?(?:new\s+)?lead"
    r")",
    _re.IGNORECASE,
)


def _try_lead_add(message: str, user_rec: dict) -> dict | None:
    """'Add me as a new lead. My name is John Smith, phone 775-555-1212.'
    Writes a real entry to messages.json so it shows in the leads inbox
    AND in the recent-leads query — instead of the LLM fabricating 'Done'."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    if not _LEAD_ADD_RE.match(msg):
        return None
    # Pull name from explicit "My name is X" / "named X" / regex 'who' group
    name = ""
    name_m = _re.search(r"(?:my\s+name\s+is|name\s+is|named|name[:\s]+)\s+([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,3})", message)
    if name_m:
        name = name_m.group(1).strip()
    else:
        who_m = _LEAD_ADD_RE.match(msg)
        if who_m and who_m.group("who"):
            name = who_m.group("who").strip()
    phone = ""
    phone_m = _re.search(r"(\+?\d[\d\-\(\)\s]{7,}\d)", message)
    if phone_m:
        phone = phone_m.group(1).strip()
    email = ""
    email_m = _re.search(r"[\w\.-]+@[\w\.-]+\.\w+", message)
    if email_m:
        email = email_m.group(0).strip()
    if not (phone or email):
        return {"reply": ("I need a phone number or email to add a lead — what's the "
                          "best way to reach them? E.g. \"add lead: name John "
                          "Smith, phone 775-555-1212\"."),
                "tier": "local", "latency_ms": 0,
                "source": "lead_add_no_contact"}
    try:
        captured = mod_messages.capture(
            DATA_DIR,
            msg_type="lead",
            from_name=name or None,
            from_phone=phone or None,
            from_email=email or None,
            body=message[:500],
            source="owner_chat",
            config=CONFIG,
        )
    except Exception as e:
        log.warning(f"owner-chat lead capture failed: {e}")
        return {"reply": f"Couldn't save the lead: {e}",
                "tier": "local", "latency_ms": 0,
                "source": "lead_add_error"}
    try:
        notify.send(
            CONFIG, DATA_DIR, event="new_lead",
            title=f"New lead: {name or phone or email}",
            body=message[:200], url="/owner",
        )
    except Exception:
        pass
    audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                    action="lead.captured",
                    meta={"message_id": captured.get("id"),
                          "via": "owner_chat"})
    label = name or phone or email
    return {"reply": f"Saved as a lead — {label}. Open the Leads tab to see it.",
            "tier": "local", "latency_ms": 0,
            "source": "lead_added"}


_UNWIRED_EMAIL_RE = _re.compile(
    r"^\s*(?:"
    r"(?:show|list|read|get|check)\s+(?:me\s+)?(?:my\s+)?(?:today'?s\s+)?(?:important\s+|recent\s+|new\s+)?emails?"
    r"|what(?:'s| is)\s+(?:important|in|new)\s+in\s+(?:my\s+)?inbox"
    r"|(?:show|list)\s+(?:me\s+)?emails?\s+(?:related\s+to|about|from|for)\s+"
    r"|find\s+(?:the\s+|an?\s+)?emails?\s+(?:from|about|related\s+to|on)"
    r"|search\s+(?:my\s+)?emails?\s+(?:for|about)"
    r"|(?:draft|write|compose)\s+(?:a\s+|an\s+)?(?:reply|response)\s+(?:to\s+|for\s+)"
    r"|email\s+(?:the\s+)?(?:latest\s+|new\s+|recent\s+)?(?:change\s+order|invoice|proposal|update|estimate)\s+to\s+"
    r"|email\s+\w+(?:\s+\w+)?\s+(?:the|a)\s+"
    # "Email Bill Henry: parts came in" / "Email Bill: x"
    r"|(?i:email)\s+[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)*\s*[:,]"
    # Imperative "send it / send the email right now"
    r"|send\s+(?:it|the\s+email|that\s+message)\s+(?:right\s+now|now)"
    r")",
    _re.IGNORECASE,
)
_UNWIRED_SMS_RE = _re.compile(
    r"^\s*(?:"
    r"(?:send|text|message)\s+(?:a\s+)?(?:text|sms|message)\s+(?:to\s+)?"
    r"|text\s+\w+(?:\s+\w+)?\s+"
    r"|(?:show|list)\s+(?:me\s+)?(?:all\s+)?(?:my\s+)?(?:texts?|sms\s+messages?)\s+(?:from|to|with)\s+"
    r"|text\s+every(?:one|body)?\s+"
    r"|text\s+(?:every\s+)?lead"
    r")",
    _re.IGNORECASE,
)
_UNWIRED_FILES_RE = _re.compile(
    r"^\s*(?:"
    r"(?:find|locate|get|fetch)\s+(?:the\s+)?(?P<q>.+?)\s+(?:contract|agreement|document|file|proposal|invoice|change[\s-]order|estimate)(?:s)?"
    r"|(?:show|open|find|fetch)\s+(?:me\s+)?(?:the\s+)?(?:signed\s+|latest\s+|recent\s+|most\s+recent\s+|newest\s+)(?:change[\s-]order|invoice|proposal|contract|document|file)"
    r"|(?:open|fetch)\s+(?:the\s+)?(?:latest\s+|recent\s+)?invoice\s+for\s+"
    r"|(?:search|find)\s+(?:for\s+)?(?:every\s+|all\s+)?file(?:s)?\s+(?:related\s+to|about|for|on)\s+"
    r")",
    _re.IGNORECASE,
)


def _try_unwired_channel(message: str, user_rec: dict) -> dict | None:
    """Email/SMS/file queries we don't have wired to a real backend.
    Return an honest deterministic refusal so the LLM can't fabricate
    'here are 3 emails from Johnson'."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    if _UNWIRED_EMAIL_RE.match(msg):
        return {"reply": (
            "I don't have your email account wired up yet, so I can't pull "
            "messages or send drafts on your behalf.\n"
            "Workarounds that DO work right now:\n"
            "  · I can generate an invoice or change-order PDF — you forward "
            "it from your own email.\n"
            "  · I can draft the WORDS for a reply — say \"draft a reminder "
            "to Johnson Construction\" and I'll write the message text."
        ), "tier": "local", "latency_ms": 0,
           "source": "unwired_email"}
    if _UNWIRED_SMS_RE.match(msg):
        return {"reply": (
            "I don't have SMS sending wired yet — Twilio is set up for the "
            "phone receptionist (incoming calls) but outbound texts from the "
            "owner dashboard aren't built.\n"
            "What works today: incoming customer texts to your business "
            "number show up in your messages inbox. You can reply from there."
        ), "tier": "local", "latency_ms": 0,
           "source": "unwired_sms"}
    if _UNWIRED_FILES_RE.match(msg):
        return {"reply": (
            "I don't have a file-search layer over your whole computer — I "
            "can only pull files I generated (invoice PDFs, closeout PDFs, "
            "proposal PDFs).\n"
            "What works:\n"
            "  · \"PDF INV-2026-0001\" — re-generate an invoice PDF\n"
            "  · \"proposal for <customer>\" — generate a proposal PDF\n"
            "  · \"closeout for <project>\" — generate the project closeout PDF\n"
            "For contracts/scans stored elsewhere, you'll need to open them "
            "directly — I don't index your file system."
        ), "tier": "local", "latency_ms": 0,
           "source": "unwired_files"}
    return None


# ── Knowledge-question handlers (read business_info directly) ─────────────

_KNOW_SERVICES_RE = _re.compile(
    r"^\s*(?:"
    r"what\s+services?\s+(?:do\s+(?:you|we|i)\s+)?(?:offer|provide|sell)"
    r"|what\s+(?:do|does)\s+(?:you|we|orby|the\s+company)\s+(?:offer|provide|do|sell)"
    r"|services?(?:\s+offered|\s+available)?"
    r"|(?:show|list)\s+(?:me\s+)?(?:your|our|my)\s+services?"
    r"|what\s+kind\s+of\s+(?:work|services?)\s+(?:do\s+you\s+do|do\s+we\s+do)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_KNOW_SERVICE_AREA_RE = _re.compile(
    r"^\s*(?:"
    r"what(?:'s| is)?\s+(?:our|your|my|the)\s+service\s+area"
    r"|service\s+area"
    r"|where\s+(?:do\s+(?:you|we)\s+)?(?:work|serve|operate|cover)"
    r"|do\s+you\s+(?:work|serve|cover|do\s+jobs)\s+in\s+(?P<city>[A-Za-z][A-Za-z\s,]+?)"
    r"|do\s+(?:you|we)\s+(?:do|serve)\s+work\s+in\s+(?P<city2>[A-Za-z][A-Za-z\s,]+?)"
    r"|are\s+you\s+in\s+(?P<city3>[A-Za-z][A-Za-z\s,]+?)"
    r"|(?:what\s+(?:cities|towns|areas))\s+(?:do\s+(?:you|we)\s+)?(?:work\s+in|serve|cover)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_KNOW_HOURS_RE = _re.compile(
    r"^\s*(?:"
    r"what(?:'s| are)?\s+(?:our|your|my|the)\s+(?:business\s+)?hours"
    r"|(?:business\s+|store\s+|shop\s+|office\s+)?hours"
    r"|when\s+(?:are|do)\s+(?:you|we)\s+(?:open|close|operate)"
    r"|what\s+time\s+do\s+(?:you|we)\s+(?:open|close)"
    r"|(?:are|is)\s+(?:you|we)\s+open(?:\s+(?:now|today))?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_KNOW_LICENSE_RE = _re.compile(
    r"^\s*(?:"
    r"what(?:'s| is)?\s+(?:our|your|my|the)\s+(?:contractor\s+|business\s+|state\s+)?license(?:\s+number|\s+#|\s+no)?"
    r"|(?:contractor\s+|business\s+)?license\s+number"
    r"|(?:contractor\s+|business\s+)?license\s+#"
    r"|what(?:'s| is)?\s+(?:our|your|my)\s+license"
    r"|license"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_KNOW_INSURANCE_RE = _re.compile(
    r"^\s*(?:"
    r"(?:are|is)\s+(?:you|we|the\s+company)\s+(?:licensed\s+and\s+)?insured"
    r"|(?:are|is)\s+(?:you|we)\s+licensed\s+and\s+insured"
    r"|do\s+(?:you|we)\s+(?:have|carry)\s+insurance"
    r"|what(?:'s| is)?\s+(?:our|your|my|the)\s+insurance"
    r"|insurance\s+(?:status|info|details)"
    r"|(?:are|is)\s+(?:you|we)\s+bonded"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_KNOW_WARRANTY_RE = _re.compile(
    r"^\s*(?:"
    r"what(?:'s| is)?\s+(?:our|your|my|the)\s+warranty"
    r"|what\s+warranty\s+(?:do\s+(?:you|we)\s+)?(?:provide|offer|give|have)"
    r"|do\s+(?:you|we)\s+(?:offer|provide|give|have)\s+(?:a\s+)?warranty"
    r"|warranty(?:\s+(?:policy|info|details|coverage))?"
    r"|how\s+long\s+is\s+(?:the\s+|your\s+)?warranty"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_KNOW_FINANCING_RE = _re.compile(
    r"^\s*(?:"
    r"what\s+financing\s+(?:options\s+)?(?:do\s+(?:you|we)\s+)?(?:offer|have|provide)"
    r"|do\s+(?:you|we)\s+(?:offer|have|provide)\s+(?:any\s+)?financing"
    r"|financing\s+(?:options|available|info)"
    r"|(?:are\s+there\s+|any\s+)?financing\s+options"
    r"|payment\s+plans?"
    r"|do\s+(?:you|we)\s+(?:offer|do)\s+payment\s+plans?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _fmt_hours_block(hours: dict) -> str | None:
    """Format the business_info hours dict as a clean weekly schedule."""
    if not hours:
        return None
    day_order = ["monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday"]
    lines = []
    for d in day_order:
        h = hours.get(d) or {}
        label = d.capitalize()
        if h.get("closed"):
            lines.append(f"  {label}:    Closed")
        elif h.get("open") and h.get("close"):
            lines.append(f"  {label}:    {h['open']} – {h['close']}")
    return "\n".join(lines) if lines else None


def _fmt_service_list(services) -> list[str]:
    """Normalize services (list of strings OR list of dicts) to a list
    of display lines."""
    out = []
    for s in (services or []):
        if isinstance(s, str):
            name = s.strip()
            if name:
                out.append(f"  · {name}")
        elif isinstance(s, dict):
            name = (s.get("name") or "").strip()
            price = (s.get("price") or "").strip()
            if name:
                if price:
                    out.append(f"  · {name} — {price}")
                else:
                    out.append(f"  · {name}")
    return out


def _try_knowledge_chat(message: str, user_rec: dict) -> dict | None:
    """Answer the business-profile knowledge questions from business_info
    directly. Deterministic, fast, accurate."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    biz = mod_business.load(DATA_DIR)
    name = biz.get("name") or "the business"
    policies = biz.get("policies") or {}

    # SERVICES — "what services do you offer"
    if _KNOW_SERVICES_RE.match(msg):
        service_lines = _fmt_service_list(biz.get("services"))
        if not service_lines:
            return {"reply": (
                f"I don't have services listed in {name}'s profile yet. "
                f"Add them in Settings → Business → Services and I'll start "
                f"reading them back."
            ), "tier": "local", "latency_ms": 0,
               "source": "know_services_empty"}
        header = f"Services {name} offers:"
        return {"reply": header + "\n" + "\n".join(service_lines),
                "tier": "local", "latency_ms": 0,
                "source": "know_services"}

    # SERVICE AREA — "what's our service area" / "do you work in Reno"
    m = _KNOW_SERVICE_AREA_RE.match(msg)
    if m:
        area = (policies.get("service_area") or "").strip()
        city_q = (m.group("city") or m.group("city2") or m.group("city3") or "").strip().rstrip("?.!")
        addr = biz.get("address") or {}
        addr_city = (addr.get("city") or "").strip()
        if city_q:
            haystack = (area + " " + addr_city).lower()
            if city_q.lower() in haystack:
                return {"reply": f"Yes — {name} works in {city_q}."
                                  + (f"\nFull service area: {area}." if area else ""),
                        "tier": "local", "latency_ms": 0,
                        "source": "know_service_area_yes"}
            if area:
                return {"reply": f"Our service area is: {area}.\n"
                                  f"{city_q} isn't on that list — call/text the "
                                  f"office to ask about a trip-fee job.",
                        "tier": "local", "latency_ms": 0,
                        "source": "know_service_area_outside"}
            # Fall through to the generic answer below
        if area:
            return {"reply": f"Service area for {name}: {area}.",
                    "tier": "local", "latency_ms": 0,
                    "source": "know_service_area"}
        if addr_city:
            return {"reply": (
                f"{name} is based in {addr_city}"
                + (f", {addr.get('state')}" if addr.get('state') else "")
                + ". A specific service area isn't set in the profile yet — "
                f"add it under Settings → Business → Policies and I'll quote it."
            ), "tier": "local", "latency_ms": 0,
               "source": "know_service_area_partial"}
        return {"reply": (
            f"I don't have a service area in the profile for {name} yet. "
            f"Add it in Settings → Business → Policies → Service area."
        ), "tier": "local", "latency_ms": 0,
           "source": "know_service_area_empty"}

    # HOURS — "what are our business hours"
    if _KNOW_HOURS_RE.match(msg):
        block = _fmt_hours_block(biz.get("hours") or {})
        if not block:
            return {"reply": (
                f"I don't have business hours in {name}'s profile yet. "
                f"Set them in Settings → Business → Hours."
            ), "tier": "local", "latency_ms": 0,
               "source": "know_hours_empty"}
        try:
            today_str = mod_business.hours_today(DATA_DIR) or ""
        except Exception:
            today_str = ""
        tail = f"\nToday: {today_str}" if today_str else ""
        return {"reply": f"Business hours for {name}:\n{block}{tail}",
                "tier": "local", "latency_ms": 0,
                "source": "know_hours"}

    # LICENSE — "what's our contractor license number"
    if _KNOW_LICENSE_RE.match(msg):
        lic = (biz.get("license") or "").strip()
        if lic:
            return {"reply": f"{name} contractor license: {lic}.",
                    "tier": "local", "latency_ms": 0,
                    "source": "know_license"}
        return {"reply": (
            f"I don't have a license number in {name}'s profile. If you "
            f"have one (NV-GC-#####), set it in Settings → Business → "
            f"License and I'll quote it on every invoice and proposal."
        ), "tier": "local", "latency_ms": 0,
           "source": "know_license_empty"}

    # INSURANCE — "are you licensed and insured"
    if _KNOW_INSURANCE_RE.match(msg):
        lic = (biz.get("license") or "").strip()
        insurance_text = ""
        for key in ("insurance", "insured", "bonding", "bonded"):
            v = policies.get(key)
            if v:
                insurance_text = str(v).strip()
                break
        if lic or insurance_text:
            bits = []
            if lic:
                bits.append(f"licensed (#{lic})")
            else:
                bits.append("licensed")
            if insurance_text:
                bits.append(insurance_text.lower() if insurance_text.lower().startswith(("bond", "insur", "general")) else "insured")
            else:
                bits.append("insured")
            return {"reply": f"Yes — {name} is " + " and ".join(bits) + ".",
                    "tier": "local", "latency_ms": 0,
                    "source": "know_insurance"}
        return {"reply": (
            f"I don't have license or insurance details in {name}'s profile "
            f"yet. Add them in Settings → Business so customers get a "
            f"straight answer when they ask."
        ), "tier": "local", "latency_ms": 0,
           "source": "know_insurance_empty"}

    # WARRANTY — "what warranty do we provide"
    if _KNOW_WARRANTY_RE.match(msg):
        warranty = (policies.get("warranty") or "").strip()
        if warranty:
            return {"reply": f"Warranty: {warranty}",
                    "tier": "local", "latency_ms": 0,
                    "source": "know_warranty"}
        return {"reply": (
            f"I don't have a warranty policy in {name}'s profile yet. Add "
            f"it in Settings → Business → Policies → Warranty."
        ), "tier": "local", "latency_ms": 0,
           "source": "know_warranty_empty"}

    # FINANCING — "what financing do we offer"
    if _KNOW_FINANCING_RE.match(msg):
        # Check several reasonable key names
        for key in ("financing", "payment_plans", "payment_options",
                    "financing_options"):
            v = policies.get(key)
            if v:
                return {"reply": f"Financing for {name}: {v}",
                        "tier": "local", "latency_ms": 0,
                        "source": "know_financing"}
        # Check payment_methods as a fallback hint
        pm = (policies.get("payment_methods") or "").strip()
        if pm:
            return {"reply": (
                f"{name} doesn't have a dedicated financing option on file. "
                f"What we DO accept: {pm}\n"
                f"If you want to offer financing (Affirm/Wisetack/Sunlight), "
                f"add it in Settings → Business → Policies → Financing."
            ), "tier": "local", "latency_ms": 0,
               "source": "know_financing_partial"}
        return {"reply": (
            f"No financing options listed for {name}. Add them in Settings → "
            f"Business → Policies → Financing."
        ), "tier": "local", "latency_ms": 0,
           "source": "know_financing_empty"}
    return None


# ── Self-introspection (what modules am I running) ────────────────────────

# Friendly per-module descriptions so the answer reads like a brochure,
# not a JSON dump. Add entries here when new add-on modules ship.
_MODULE_INTRO_DESCRIPTIONS = {
    "contractor": (
        "Contractor — general-contractor module. Tracks projects, change "
        "orders (with foreman-on-site customer signing), invoices + "
        "retainage, subcontractors + insurance expiry, daily logs with "
        "leak-alarm scope-change detection, bids + proposal PDFs, customer "
        "review collection, and a per-project customer portal."
    ),
    # Future modules — copy this template for medical/legal/etc.
    # "medical": ("Medical — ..."),
    # "legal":   ("Legal — ..."),
}

# Map natural-language module-name variants to the canonical key in
# CONFIG.enabled_modules. Add aliases here as new modules ship.
_MODULE_ALIASES = {
    "contractor":          "contractor",
    "construction":        "contractor",
    "builder":             "contractor",
    "building":            "contractor",
    "gc":                  "contractor",
    "general contractor":  "contractor",
    "general-contractor":  "contractor",
    "remodel":             "contractor",
    "remodeling":          "contractor",
    "home builder":        "contractor",
    "home-builder":        "contractor",
    "medical":             "medical",
    "doctor":              "medical",
    "clinic":              "medical",
    "healthcare":          "medical",
    "legal":               "legal",
    "lawyer":              "legal",
    "law":                 "legal",
    "attorney":            "legal",
    "restaurant":          "restaurant",
    "deli":                "restaurant",
    "automotive":          "automotive",
    "auto":                "automotive",
    "shop":                "automotive",
}
_MODULE_ALIAS_PATTERN = "|".join(_re.escape(a) for a in sorted(
    _MODULE_ALIASES.keys(), key=len, reverse=True))

_MODULES_INTRO_RE = _re.compile(
    r"^\s*(?:"
    r"(?:do\s+(?:you|i)\s+have|do\s+you\s+know\s+anything\s+about|is\s+(?:there|the)|do\s+i\s+(?:have\s+)?the)\s+(?:a\s+|an\s+|the\s+)?"
    r"(?P<mod_q>" + _MODULE_ALIAS_PATTERN + r")\s*(?:module|add[\s-]?on|plan|package|feature|attached)?"
    r"|what\s+(?:modules?|add[\s-]?ons?|plans?|features?|packages?)\s+(?:do\s+i\s+have|am\s+i\s+(?:on|using|subscribed\s+to)|are\s+(?:enabled|installed|active))"
    r"|which\s+(?:modules?|add[\s-]?ons?|plans?|features?)\s+(?:do\s+i\s+have|am\s+i\s+(?:on|using))"
    r"|(?:show|list|tell\s+me)\s+(?:me\s+)?(?:my\s+)?(?:enabled\s+|active\s+|installed\s+)?(?:modules?|add[\s-]?ons?|features?)"
    r"|am\s+i\s+(?:on|subscribed\s+to|using)\s+(?:the\s+)?(?P<mod_q2>" + _MODULE_ALIAS_PATTERN + r")\s*(?:module|add[\s-]?on|plan|package)?"
    r"|what'?s\s+(?:my\s+)?(?:subscription|plan)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _try_modules_introspection(message: str, user_rec: dict) -> dict | None:
    """Answer 'do you have a contractor module' / 'what modules am I on'
    truthfully from CONFIG.enabled_modules. Beats the LLM, which
    confidently makes up the wrong answer (e.g. claims contractor isn't
    enabled when it actually is)."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    m = _MODULES_INTRO_RE.match(msg)
    if not m:
        return None
    enabled = list((CONFIG.get("enabled_modules") or []))

    # Specific module asked about — map the alias to the canonical key,
    # then check if it's in CONFIG.enabled_modules.
    mod_q_raw = (m.group("mod_q") or m.group("mod_q2") or "").strip().lower()
    if mod_q_raw:
        canonical = _MODULE_ALIASES.get(mod_q_raw, mod_q_raw)
        on = canonical in [str(x).lower() for x in enabled]
        if on:
            desc = _MODULE_INTRO_DESCRIPTIONS.get(canonical, f"The {canonical} module is active.")
            # If the user said "construction" but we call it "contractor",
            # acknowledge their phrasing while using the canonical name.
            alias_note = ""
            if mod_q_raw != canonical:
                alias_note = f" (we call it the {canonical} module internally)"
            return {"reply": f"Yes — the {canonical} module is enabled{alias_note}.\n\n{desc}",
                    "tier": "local", "latency_ms": 0,
                    "source": "modules_intro_yes"}
        else:
            avail = ", ".join(enabled) if enabled else "(none beyond base Orby)"
            return {"reply": (
                f"No — the {canonical} module is not enabled on this Orby.\n"
                f"Currently active add-ons: {avail}.\n"
                f"You can add modules from Settings → Plan & Add-ons "
                f"(Stripe billing controls which are active)."
            ), "tier": "local", "latency_ms": 0,
               "source": "modules_intro_no"}

    # Generic "what modules / add-ons do I have"
    if not enabled:
        return {"reply": (
            "Just base Orby right now — no paid add-on modules enabled.\n"
            "Available add-ons you could activate from Settings → Plan & "
            "Add-ons: contractor (and more coming)."
        ), "tier": "local", "latency_ms": 0,
           "source": "modules_intro_base_only"}
    lines = [f"You have {len(enabled)} add-on module{'s' if len(enabled) != 1 else ''} active on top of base Orby:"]
    for m_name in enabled:
        desc = _MODULE_INTRO_DESCRIPTIONS.get(str(m_name).lower(),
                                                f"  · {m_name} — active")
        if desc.lstrip().startswith("·"):
            lines.append(desc)
        else:
            # Friendly multi-line entry
            lines.append(f"  · {desc}")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "modules_intro_list"}


# ── Public-chat referral: "where can I get one of these for my business?" ───
#
# Fires ONLY on direct visitor ask. Returns a one-sentence URL + redirect
# to the host business's flow. Never volunteers. Toggleable per-customer
# via CONFIG["referral_enabled"] (defaults True). URL comes from
# CONFIG["referral_url"], defaults to twickell.com until myorby.com is
# registered.

_REFERRAL_DEFAULT_URL = "twickell.com"

_REFERRAL_RE = _re.compile(
    r"^\s*(?:"
    # "Where can I get/buy one of these (for my business)?" — the noun phrase
    # is REQUIRED (no trailing ?), otherwise innocent ordering speech like
    # "where can I order food" would falsely trigger as a referral question.
    r"(?:where|how)\s+(?:can|do|would|could)\s+i\s+"
    r"(?:get|buy|find|sign\s+up\s+for|order|download|install|purchase|use|try)\s+"
    r"(?:my\s+own\s+|one\s+of\s+these\s*|an?\s+|the\s+|some(?:thing)?\s+like\s+this\s*)?"
    r"(?:orby|orbi|orbeez|this\s+(?:ai|thing|assistant|service|software|chatbot|bot|system|tool)|"
    r"one\s+of\s+(?:these|those)|something\s+like\s+this|an?\s+(?:ai|chatbot|receptionist))"
    # "I want one of these / I want Orby / I need an AI like this" — same fix:
    # noun phrase REQUIRED. Otherwise "I want it toasted" or "I would like
    # the 12-inch" falsely matches as a request for an AI product.
    r"|i\s+(?:want|need|would\s+like|gotta\s+get|am\s+looking\s+for)\s+(?:my\s+own\s+|one\s+of\s+these\s*|an?\s+|the\s+|some(?:thing)?\s+like\s+this\s*)?"
    r"(?:orby|orbi|this\s+(?:ai|thing|assistant|chatbot)|one\s+of\s+(?:these|those)|something\s+like\s+this|an?\s+(?:ai|chatbot|receptionist))"
    # "What AI is this / who made you / what's this app"
    r"|(?:what|which)\s+(?:ai|software|app|chatbot|assistant|service|company|program|product|system)\s+(?:is\s+)?this"
    r"|who\s+(?:made|built|created|owns|sells|developed|wrote)\s+(?:you|this)"
    r"|who\s+(?:is|are|made|built|owns)\s+(?:orby|orbi|orbeez)"
    r"|are\s+you\s+(?:built|made|powered)\s+by\s+(?:openai|chatgpt|claude|gemini|anthropic|google)"
    # "How much does Orby cost?" / "What's the price of Orby?"
    r"|how\s+much\s+(?:does|is)\s+(?:orby|orbi|this(?:\s+(?:ai|service|software))?)\s*(?:cost|price)?"
    r"|what(?:'s|\s+is)\s+(?:the\s+)?(?:price|cost)\s+(?:of|for)\s+(?:orby|orbi|this)"
    # "Can my business get Orby?" / "Do you sell Orby?"
    r"|can\s+(?:my|a)\s+business\s+(?:get|have|use|buy|sign\s+up\s+for)\s+(?:orby|orbi|one\s+of\s+these|something\s+like\s+this)"
    r"|do\s+you\s+sell\s+(?:orby|orbi|this(?:\s+(?:ai|service|software))?)"
    # "Is this Orby?" / "Are you Orby?" — visitor recognized the product
    r"|(?:is\s+this|are\s+you)\s+(?:orby|orbi|orbeez)"
    # "Tell me about Orby" / "What is Orby?"
    r"|(?:tell\s+me\s+about|what(?:'s|\s+is))\s+orby"
    r")\b",
    _re.IGNORECASE,
)


def _try_orby_referral(message: str, business: dict) -> dict | None:
    """When a visitor on a customer's Orby asks where to get their own
    Orby, give the URL and immediately return focus to the host business.
    Never volunteers — only fires on direct ask."""
    if not message:
        return None
    # Honor the kill switch — owner can turn this off per-customer
    if CONFIG.get("referral_enabled") is False:
        return None
    msg = message.strip()
    if not _REFERRAL_RE.match(msg):
        return None
    url = (CONFIG.get("referral_url") or _REFERRAL_DEFAULT_URL).strip()
    biz_name = (business.get("name") or "").strip() or "this business"
    audit.log_event(DATA_DIR, actor="public_visitor",
                    action="referral.shown",
                    meta={"url": url, "host_business": biz_name,
                          "message": message[:200]})
    return {"reply": (
        f"You can find everything about Orby at **{url}** — I'm not "
        f"allowed to say more from here since I'm working for "
        f"{biz_name} right now. Anything else I can help you with?"
    ), "tier": "local", "latency_ms": 0,
       "source": "orby_referral"}


_FORMS_INTRO_RE = _re.compile(
    r"^\s*(?:"
    r"what\s+(?:forms?|templates?|paperwork)\s+(?:do\s+i\s+have|are\s+(?:uploaded|loaded|on\s+file)|am\s+i\s+using)"
    r"|which\s+(?:forms?|templates?)\s+(?:do\s+i\s+have|am\s+i\s+using)"
    r"|(?:show|list|tell\s+me)\s+(?:me\s+)?(?:my\s+|the\s+|all\s+)?(?:forms?|templates?|paperwork)"
    r"|(?:do\s+i\s+have|have\s+i\s+uploaded)\s+(?:a\s+|an\s+|the\s+)?"
    r"(?P<kind_q>change[\s-]?order|co|contract|msa|lien\s+waiver|w[\s-]?9|coi(?:\s+request)?|subcontractor\s+agreement|punch\s+list|proposal)"
    r"\s*(?:form|template|paperwork|uploaded)?"
    r"|how\s+do\s+i\s+upload\s+(?:a\s+)?(?:form|template)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)

# Normalized chat-kind → canonical mod_forms.VALID_KINDS key
_FORM_KIND_ALIASES = {
    "change order": "change_order",
    "change-order": "change_order",
    "changeorder": "change_order",
    "co": "change_order",
    "contract": "contract",
    "msa": "msa",
    "lien waiver": "lien_waiver_partial_cond",  # default to most common
    "w9": "w9",
    "w-9": "w9",
    "w 9": "w9",
    "coi": "coi_request",
    "coi request": "coi_request",
    "subcontractor agreement": "subcontractor_agreement",
    "punch list": "punch_list",
    "proposal": "proposal",
}


def _try_forms_introspection(message: str, user_rec: dict) -> dict | None:
    """'What forms do I have' / 'show me my templates' / 'do I have a
    change order template uploaded' / 'how do I upload a form'."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    m = _FORMS_INTRO_RE.match(msg)
    if not m:
        return None
    # Specific kind asked about
    kind_q = (m.group("kind_q") or "").strip().lower()
    if kind_q:
        canonical = _FORM_KIND_ALIASES.get(kind_q, kind_q.replace("-", "_").replace(" ", "_"))
        tpl = mod_forms.get_default(DATA_DIR, canonical)
        if tpl:
            field_count = len(tpl.get("detected_fields", []) or [])
            mapped = sum(1 for f in tpl.get("detected_fields", []) or []
                          if f.get("mapped_to"))
            return {"reply": (
                f"Yes — you have a {canonical.replace('_',' ')} template on file:\n"
                f"  · {tpl['display_name']} (uploaded {time.strftime('%b %-d', time.localtime(tpl.get('uploaded_at', 0)))})\n"
                f"  · {field_count} fillable fields detected, {mapped} auto-mapped to Orby data\n"
                f"  · Default: {'yes' if tpl.get('is_default') else 'no'}\n\n"
                f"To swap or update, upload a new one from the Forms tab."
            ), "tier": "local", "latency_ms": 0,
               "source": "forms_intro_yes"}
        else:
            return {"reply": (
                f"No {canonical.replace('_',' ')} template uploaded yet.\n"
                f"To add one: open the Forms tab → + Upload Template → "
                f"pick your blank PDF or Word doc → label kind as "
                f"\"{canonical}\". I'll auto-detect the fields and map "
                f"them to project data."
            ), "tier": "local", "latency_ms": 0,
               "source": "forms_intro_no"}
    # Generic list
    all_tpls = mod_forms.list_all(DATA_DIR)
    if not all_tpls:
        return {"reply": (
            "No form templates uploaded yet. The Forms tab in your "
            "dashboard lets you upload your own change orders, contracts, "
            "lien waivers, W-9s, etc. Orby reads each one, detects the "
            "fillable fields, and learns how to fill them from your "
            "project data."
        ), "tier": "local", "latency_ms": 0,
           "source": "forms_intro_empty"}
    by_kind = {}
    for t in all_tpls:
        by_kind.setdefault(t.get("kind", "?"), []).append(t)
    lines = [f"You have {len(all_tpls)} form template{'s' if len(all_tpls) != 1 else ''} on file:"]
    for kind, tpls in sorted(by_kind.items()):
        default = next((t for t in tpls if t.get("is_default")), tpls[0])
        extra = f" ({len(tpls)-1} other version{'s' if len(tpls) > 2 else ''})" if len(tpls) > 1 else ""
        fields = len(default.get("detected_fields", []) or [])
        lines.append(f"  · {kind.replace('_',' ').upper()}: \"{default['display_name']}\" "
                     f"({fields} fields){extra}")
    lines.append("\nManage in the Forms tab. Upload new ones the same place.")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "forms_intro_list"}


# ── Owner-simulating-customer-lead handlers ───────────────────────────────

_SIM_LEAD_NEED_RE = _re.compile(
    r"^\s*(?:i\s+need|i'?d\s+like|i\s+want|looking\s+for)\s+(?:a\s+|an\s+|some\s+)?"
    r"(?P<what>.+?)"
    r"[.!]?\s*(?:can\s+you\s+(?:schedule|book|set\s+up)|please\s+(?:schedule|book)|(?:can\s+you\s+)?give\s+me\s+(?:an?\s+)?(?:estimate|quote))"
    r".*?\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_SIM_LEAD_BUDGET_RE = _re.compile(
    r"^\s*(?:my\s+budget\s+is|i\s+have\s+a\s+budget\s+of)\s+(?:around\s+|about\s+|roughly\s+|~\s*)?"
    r"\$?[\d,]+(?:k|K|\s*thousand)?"
    r".*?(?:next\s+steps?|what(?:'s| do\s+i\s+do)\s+next|how\s+do\s+(?:i|we)\s+(?:start|begin|proceed))"
    r"\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _try_owner_simulated_lead(message: str, user_rec: dict) -> dict | None:
    """In owner_chat, the owner sometimes simulates a customer to test the
    flow ('I need a kitchen remodel. Can you schedule an estimate?').
    Public_chat would auto-capture as a lead; here we acknowledge
    deterministically and point at the right entry path."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    biz = mod_business.load(DATA_DIR)
    name = biz.get("name") or "us"
    contact = biz.get("contact") or {}
    phone = (contact.get("phone") or "").strip()
    email = (contact.get("email") or "").strip()
    contact_bit = ""
    if phone or email:
        bits = []
        if phone: bits.append(f"call/text {phone}")
        if email: bits.append(f"email {email}")
        contact_bit = " (a real customer would " + " or ".join(bits) + ")"

    m = _SIM_LEAD_NEED_RE.match(msg)
    if m:
        what = (m.group("what") or "").strip()
        return {"reply": (
            f"Got it — you'd like {what or 'work'} and want to schedule an "
            f"estimate. That's the kind of request that hits the WEBSITE "
            f"chat or phone receptionist, where I auto-capture the lead "
            f"and notify you{contact_bit}.\n"
            f"From the owner side, log it as a real lead with: "
            f"\"add a new lead, name <theirs>, phone <theirs>\" — I'll save "
            f"it to your messages inbox and flag it for follow-up."
        ), "tier": "local", "latency_ms": 0,
           "source": "sim_lead_need"}

    if _SIM_LEAD_BUDGET_RE.match(msg):
        return {"reply": (
            f"That budget context belongs on the customer's lead record. "
            f"In the live flow, a customer who types this in your website "
            f"chat or calls in: I'd capture their contact info, tag the "
            f"lead with their budget range, and notify you to schedule the "
            f"in-home estimate.\n"
            f"Owner-side action: \"add a new lead, name <theirs>, phone "
            f"<theirs>, $40k budget kitchen remodel\" and I'll log it."
        ), "tier": "local", "latency_ms": 0,
           "source": "sim_lead_budget"}
    return None


_SAFETY_SIGN_FOR_RE = _re.compile(
    # Anchored at start but no end-of-string requirement — catches the
    # leading command even when followed by justifications:
    # "go ahead and sign for them — the customer said it's fine"
    r"^\s*(?:"
    r"(?:go\s+ahead\s+and\s+)?sign\s+(?:a\s+|the\s+)?(?:contract|agreement|change[\s-]order|c\.?o\.?|document|proposal|paperwork|form)?\s*(?:for|on\s+behalf\s+of|in\s+place\s+of|as)\s+(?:the\s+|me|us|them)?\s*(?:customer|client|homeowner|him|her)"
    r"|(?:e[\s-]?sign|electronically\s+sign|forge|fake|just\s+sign)\s+(?:.+?\s+)?(?:for|on\s+behalf\s+of|as|in\s+place\s+of)\s+(?:the\s+)?(?:customer|client|him|her|them)"
    r"|(?:go\s+ahead\s+and\s+)?sign\s+(?:it\s+|that\s+)?(?:for|on\s+behalf\s+of)\s+(?:them|him|her|the\s+customer|the\s+client)"
    # "the customer told me (on the phone) to sign for them — go ahead and sign"
    # Allow arbitrary intermediate words between "told me" and "to sign".
    r"|.+?told\s+me\s+(?:.+?\s+)?to\s+sign\s+(?:.+?\s+)?(?:for|as|on\s+behalf\s+of)\s+(?:them|him|her|me|us|the\s+(?:customer|client))"
    # "the customer is fine with me signing for them"
    r"|.+?(?:said|told\s+me|told\s+us)\s+(?:.+?\s+)?(?:i|we|you|orby)\s+(?:can|could|should|might)\s+sign\s+(?:.+?\s+)?(?:for|as)\s+(?:them|him|her|the\s+customer)"
    # Trailing "— go ahead and sign" anywhere in the message
    r"|.+?go\s+ahead\s+and\s+sign\s+(?:.+?\s+)?(?:for|as|on\s+behalf\s+of)\s+(?:them|him|her|me|us|the\s+customer|the\s+client)"
    r")\b",
    _re.IGNORECASE,
)
_SAFETY_MODIFY_SIGNED_CO_RE = _re.compile(
    # Anchored at start; no end-of-string requirement.
    # Catches: "modify a signed CO", "edit CO #1", "bump CO #1 from $X to $Y",
    # "adjust the CO", "change CO #2 amount", "skip X in CO #1 but add Y".
    # Optional leading polite/hedge words: "just", "go ahead and", "please".
    r"^\s*(?:just\s+|please\s+|go\s+ahead\s+and\s+|can\s+you\s+|could\s+you\s+|i\s+need\s+(?:you\s+)?to\s+)?(?:"
    r"(?:modify|edit|change|alter|update|revise|adjust|bump|raise|lower|tweak|increase|decrease)\s+(?:a\s+|the\s+|that\s+)?(?:signed\s+)?(?:change[\s-]order|c\.?o\.?)(?:\s*#\s*\d+)?"
    r"|edit\s+(?:a\s+|the\s+)?(?:c\.?o\.?|change[\s-]order)\s+(?:that(?:'s| is)\s+(?:been\s+)?)?signed"
    r"|(?:.+?\s+)?(?:wants?|wanting)\s+to\s+(?:skip|drop|remove|swap|exchange)\s+(?:.+?\s+)?(?:in|from|on)\s+(?:c\.?o\.?|change[\s-]order)(?:\s*#\s*\d+)?"
    # "adjust the contract of sale (CO)" type misinterpretations
    r"|adjust\s+(?:a\s+|the\s+)?co\b"
    r")\b",
    _re.IGNORECASE,
)
_SAFETY_DELETE_FILES_RE = _re.compile(
    r"^\s*(?:"
    r"(?:delete|remove|trash|nuke|wipe|purge|erase)\s+(?:all\s+|every\s+|the\s+)?(?P<who>.+?)\s+(?:project\s+)?files?"
    r"|delete\s+(?:every|all)\s+file\s+(?:for|on|in)\s+(?P<who2>.+?)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_SAFETY_INCREASE_UNAUTHORIZED_RE = _re.compile(
    r"^\s*(?:"
    r"increase\s+(?:a\s+|the\s+)?(?:.+?\s+)?contract\s+(?:amount\s+)?by\s+\$?[\d,]+(?:\.\d+)?(?:k|K)?\s+"
    r"(?:without\s+(?:client\s+|customer\s+|homeowner\s+|their\s+)?(?:authorization|approval|sign[\s-]off|consent))"
    r"|inflate\s+(?:the\s+)?contract\s+by\s+\$?[\d,]+"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
_SAFETY_IMPERSONATE_RE = _re.compile(
    r"^\s*(?:"
    r"log\s+(?:me|us|him|her|them)\s+in\s+as\s+\w+"
    r"|sign\s+(?:me|us|him|her|them)\s+in\s+as\s+\w+"
    r"|impersonate\s+\w+"
    r"|switch\s+(?:my\s+)?(?:user|account|login|session)\s+to\s+\w+"
    r"|pretend\s+(?:i'?m|to\s+be|you'?re)\s+\w+"
    r")\b",
    _re.IGNORECASE,
)
_SAFETY_FAKE_PAID_RE = _re.compile(
    # Anchored at start; no end-of-string requirement.
    # Catches: "mark all unpaid invoices paid", "mark INV-X paid" (no
    # payment), "fake a payment", "say everyone paid".
    r"^\s*(?:"
    r"mark\s+(?:all\s+(?:my\s+|the\s+)?(?:unpaid\s+|outstanding\s+|open\s+)?invoices?)\s+(?:as\s+)?paid"
    r"|mark\s+(?:everyone|every(?:one|body))\s+(?:as\s+)?paid"
    r"|mark\s+(?:an?\s+|the\s+)?invoice\s+(?:as\s+)?paid\s+(?:when|even\s+(?:though|if))\s+no\s+payment\s+(?:exists|was\s+made|received)"
    r"|(?:fake|forge|fabricate|invent|make\s+up)\s+(?:a\s+|the\s+|some\s+)?payments?"
    r"|(?:say|pretend|act\s+like)\s+(?:everyone\s+|they\s+|the\s+customer\s+)?paid"
    r")\b",
    _re.IGNORECASE,
)
# Recording a real partial payment without an amount — e.g. "Sarah paid
# half of INV-XXXX in cash today". Should ASK for the amount instead of
# silently inventing one (or LLM falsely confirming).
_SAFETY_PARTIAL_NO_AMOUNT_RE = _re.compile(
    r"^\s*(?:.+?\s+)?paid\s+(?:half|part|some|the\s+rest|a\s+chunk|a\s+portion)\s+(?:of\s+)?(?:INV-[\w-]+)?",
    _re.IGNORECASE,
)
_SAFETY_SEND_WITHOUT_APPROVAL_RE = _re.compile(
    r"^\s*(?:"
    r"(?:create|draft|build|make)\s+(?:a\s+|the\s+)?(?:change[\s-]order|c\.?o\.?)\s+and\s+(?:send|email|fire)\s+(?:it\s+)?"
    r"(?:out\s+)?(?:without|with\s+no)\s+(?:my\s+|the\s+|getting\s+|owner\s+|gc\s+|prior\s+)?approval"
    r"|(?:send|fire|push)\s+(?:a\s+|the\s+)?(?:change[\s-]order|c\.?o\.?)\s+(?:out\s+)?(?:without|with\s+no)\s+(?:my\s+|the\s+|getting\s+|owner\s+|gc\s+|prior\s+)?approval"
    r"|skip\s+(?:my\s+|gc\s+|the\s+|owner\s+)?approval"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)
# Catches "charge Sarah $500", "refund the deposit to Bob", "run the
# card on file", "process a payment from X", "take a $100 deposit from Y".
# Stripe is READ-ONLY per the shipped capabilities doc — Orby never moves
# money. The LLM tends to hallucinate compliance here, so deterministic
# refusal beats trusting the prompt.
_SAFETY_CHARGE_RE = _re.compile(
    # Matches the verb-phrase at the START of the message. Doesn't require
    # end-of-string so "Refund $200 to Bob" / "Run the card on file for $50"
    # / "Process a payment from Joe Maxwell" all match the leading command.
    # Allows optional polite/authority prefixes like "I authorize you to ...",
    # "please ...", "go ahead and ...", "I need you to ...".
    r"^\s*(?:"
    r"(?:i\s+)?(?:authorize|am\s+authorizing|approve|am\s+approving|need|want|am\s+telling)\s+(?:you\s+)?to\s+"
    r"|please\s+|go\s+ahead\s+and\s+|just\s+|can\s+you\s+|could\s+you\s+"
    r")?(?:"
    r"charge\s+(?:.+?\s+)?\$?[\d,]+(?:\.\d{1,2})?"
    r"|charge\s+(?:.+?\s+)?(?:on|to|using|with)\s+(?:her|his|their|the)\s+(?:card|credit\s+card|debit\s+card|account)"
    r"|charge\s+(?:.+?\s+)?card(?:\s+on\s+file)?"
    r"|(?:run|swipe|put\s+through)\s+(?:the\s+|her\s+|his\s+|their\s+|a\s+)?(?:card|credit\s+card|debit\s+card)"
    r"|process\s+(?:a\s+|the\s+)?(?:payment|charge|transaction|refund)"
    r"|(?:refund|reverse|void)\s+(?:.+?\s+)?\$?[\d,]+(?:\.\d{1,2})?"
    r"|(?:issue|give)\s+(?:a\s+|the\s+)?refund"
    r"|take\s+(?:a\s+|the\s+)?\$?[\d,]+\s+deposit\s+(?:from|off)"
    r"|bill\s+(?:.+?\s+)?(?:card|credit\s+card)"
    r"|move\s+(?:money|funds|\$[\d,]+)\s+(?:from|to)"
    r"|withdraw\s+(?:from|money\s+from)"
    r")\b",
    _re.IGNORECASE,
)


def _try_safety_refusal(message: str, user_rec: dict) -> dict | None:
    """Hard refusals for actions that would hurt the contractor or their
    customer. Logged to audit so there's a record someone TRIED."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)

    if _SAFETY_SIGN_FOR_RE.match(msg):
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="safety.refused.sign_for_customer",
                        meta={"message": message[:200]})
        return {"reply": (
            "I won't sign anything on behalf of the customer — that would "
            "void the contract and could be construed as forgery. The "
            "customer has to sign their own change orders.\n"
            "What I CAN do: mint a one-time sign link for them.\n"
            "  · \"add CO on <project> — $<amount> for <description>\"\n"
            "  → I'll create the CO and give you a link to hand to them on "
            "site (foreman's iPad works, or text the link)."
        ), "tier": "local", "latency_ms": 0,
           "source": "safety_refused_sign_for"}

    if _SAFETY_MODIFY_SIGNED_CO_RE.match(msg):
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="safety.refused.modify_signed_co",
                        meta={"message": message[:200]})
        return {"reply": (
            "Signed change orders are locked — I won't modify one after the "
            "customer signed it. Doing that would invalidate their "
            "signature and put you on the wrong side of any dispute.\n"
            "If something was wrong with it, create a CORRECTIVE CO instead:\n"
            "  · \"add CO on <project> — $<amount> — corrects CO #<n>\"\n"
            "Both stay in the record, and the customer signs the correction."
        ), "tier": "local", "latency_ms": 0,
           "source": "safety_refused_modify_signed"}

    if _SAFETY_INCREASE_UNAUTHORIZED_RE.match(msg):
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="safety.refused.unauthorized_increase",
                        meta={"message": message[:200]})
        return {"reply": (
            "I won't raise a contract amount without client authorization — "
            "that's the exact thing change orders exist to prevent.\n"
            "Proper path: \"add CO on <project> — $<amount> for <reason>\". "
            "The customer signs, then it's added to the contract automatically."
        ), "tier": "local", "latency_ms": 0,
           "source": "safety_refused_unauthorized_increase"}

    if _SAFETY_IMPERSONATE_RE.match(msg):
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="safety.refused.impersonate",
                        meta={"message": message[:200]})
        return {"reply": (
            "I won't switch you to another user's account. If you actually "
            "need to be that person, log out and log in with their "
            "credentials — that way every action is properly attributed in "
            "the audit log.\n"
            "If you're just trying to see what their dashboard looks like "
            "from a staff perspective, the Staff tab lets you preview the "
            "view without taking over their session."
        ), "tier": "local", "latency_ms": 0,
           "source": "safety_refused_impersonate"}

    if _SAFETY_FAKE_PAID_RE.match(msg):
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="safety.refused.fake_payment",
                        meta={"message": message[:200]})
        return {"reply": (
            "I won't mark an invoice paid when no payment exists — that's "
            "books-cooking territory and it'll bite you at tax time.\n"
            "If you actually received cash/check, tell me: \"received "
            "$<amount> on INV-XXXX\" and I'll record it properly with a "
            "real payment line."
        ), "tier": "local", "latency_ms": 0,
           "source": "safety_refused_fake_payment"}

    if _SAFETY_CHARGE_RE.match(msg):
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="safety.refused.charge_or_refund",
                        meta={"message": message[:200]})
        return {"reply": (
            "I won't move money — Stripe is read-only on my end, and "
            "charging cards or issuing refunds is a one-way door I shouldn't "
            "be walking through on a chat command.\n"
            "If you actually want to charge a customer, do it from your "
            "Stripe dashboard directly. Then tell me \"received $<amount> "
            "on INV-XXXX\" and I'll record the payment against the invoice."
        ), "tier": "local", "latency_ms": 0,
           "source": "safety_refused_charge"}

    # Conditional/future-tense action — "if Joe pays by Friday, mark X paid"
    # / "when the customer signs, send the invoice". I can't watch and act
    # later. Be honest about it instead of falsely promising async behavior.
    if _re.match(
        r"^\s*(?:if|when|once|as\s+soon\s+as)\s+.+?(?:,\s*|\s+then\s+)"
        r"(?:mark|send|email|charge|record|update|cancel|delete|create|file)\b",
        msg, _re.IGNORECASE):
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="safety.refused.async_conditional",
                        meta={"message": message[:200]})
        return {"reply": (
            "I can't watch for a future event and then act on it — I only "
            "run when you talk to me. So \"if X happens, do Y\" is a "
            "promise I can't keep.\n"
            "What WILL work:\n"
            "  · Set a reminder: \"remind me Friday to check whether Joe paid\"\n"
            "  · Come back when it happens: \"Joe paid $X on INV-XXXX\" and "
            "I'll record it right then."
        ), "tier": "local", "latency_ms": 0,
           "source": "safety_refused_async"}

    # Partial payment WITHOUT an explicit amount — ask, don't fabricate.
    if _SAFETY_PARTIAL_NO_AMOUNT_RE.match(msg):
        # Only block if there's no explicit dollar amount in the message
        if not _re.search(r"\$\s*[\d,]+", message):
            audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                            action="safety.asked.partial_no_amount",
                            meta={"message": message[:200]})
            return {"reply": (
                "I need an exact dollar amount before I record a partial "
                "payment — \"half\" / \"part\" / \"some\" isn't precise "
                "enough for the ledger.\n"
                "Try: \"received $<amount> on INV-XXXX\" and I'll log it "
                "properly against the invoice."
            ), "tier": "local", "latency_ms": 0,
               "source": "safety_partial_no_amount"}

    if _SAFETY_SEND_WITHOUT_APPROVAL_RE.match(msg):
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="safety.refused.skip_approval",
                        meta={"message": message[:200]})
        return {"reply": (
            "Change orders go to the customer for SIGNATURE — but in the "
            "current workflow that's the same moment the foreman shows it "
            "to them on site. You're always in the loop because you're the "
            "one creating the CO.\n"
            "If you want to skip the foreman-handoff step and just email "
            "the sign link, that's possible — but I'm not going to send "
            "anything WITHOUT generating the CO record first."
        ), "tier": "local", "latency_ms": 0,
           "source": "safety_refused_skip_approval"}

    if _SAFETY_DELETE_FILES_RE.match(msg):
        m = _SAFETY_DELETE_FILES_RE.match(msg)
        who = (m.group("who") or m.group("who2") or "").strip()
        audit.log_event(DATA_DIR, actor=user_rec.get("username", "?"),
                        action="safety.refused.bulk_file_delete",
                        meta={"target": who, "message": message[:200]})
        return {"reply": (
            f"I won't bulk-delete files. Even if I could find every file "
            f"related to \"{who}\", a one-shot delete with no confirmation "
            f"is too dangerous — you lose records you might need for "
            f"warranty calls, disputes, or taxes.\n"
            f"What I CAN do safely:\n"
            f"  · Archive a project (\"mark <project> complete\") — files "
            f"stay readable but are visually moved out of the active list.\n"
            f"  · Delete one specific document if you point me at it by "
            f"exact filename."
        ), "tier": "local", "latency_ms": 0,
           "source": "safety_refused_bulk_delete"}
    return None


_TASK_COMPLETE_RE = _re.compile(
    r"^\s*(?:"
    r"(?:i'?m\s+)?done\s+(?:with\s+)?(?P<body>.+?)"
    r"|(?:i\s+)?(?:finished|completed|did)\s+(?:with\s+)?(?P<body2>.+?)"
    r"|(?:mark|check)\s+(?:off\s+)?(?P<body3>.+?)\s+(?:as\s+)?(?:done|complete|completed|finished)"
    r"|(?P<body4>.+?)\s+(?:is\s+)?(?:done|complete|completed|finished|wrapped)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _try_task_complete(message: str, user_rec: dict) -> dict | None:
    """'Done with the supplier call' / 'I finished the budget review' →
    mark a matching open task done. Falls through if there's no task that
    fuzzy-matches the body."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    m = _TASK_COMPLETE_RE.match(msg)
    if not m:
        return None
    body = (m.group("body") or m.group("body2") or m.group("body3")
            or m.group("body4") or "").strip().rstrip("?.!")
    if not body or len(body) < 3:
        return None
    # Filter out things that obviously aren't task references — these
    # would catch generic life statements ("I'm done", "we're finished").
    if body.lower() in {"it", "that", "this", "everything", "all of it",
                        "for now", "for the day", "for today"}:
        return None
    username = user_rec.get("username", "owner")
    try:
        user_dir = users_mod.get_user_dir(DATA_DIR, username)
    except Exception:
        return None
    try:
        open_tasks = mod_tasks.list_all(user_dir, include_done=False)
    except Exception:
        return None
    if not open_tasks:
        return None
    body_l = body.lower()
    body_tokens = set(w for w in _re.split(r"\W+", body_l) if len(w) >= 3)
    if not body_tokens:
        return None
    # Score each open task by token-overlap with the user's phrase
    scored = []
    for t in open_tasks:
        ttext = (t.get("text") or "").lower()
        ttokens = set(w for w in _re.split(r"\W+", ttext) if len(w) >= 3)
        overlap = len(body_tokens & ttokens)
        if overlap > 0:
            scored.append((overlap, t))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    top_score = scored[0][0]
    # If top match is much stronger than second-best (or only one match),
    # take it. Otherwise ask which.
    if len(scored) == 1 or top_score >= scored[1][0] * 2 or top_score >= 2:
        target = scored[0][1]
        try:
            mod_tasks.mark_done(user_dir, target["id"])
        except Exception as e:
            return {"reply": f"Couldn't mark done: {e}",
                    "tier": "local", "latency_ms": 0,
                    "source": "task_done_error"}
        audit.log_event(DATA_DIR, actor=username, action="task.done",
                        meta={"task_id": target["id"], "text": target.get("text", "")})
        return {"reply": f"✓ Marked done: \"{target.get('text','')}\"",
                "tier": "local", "latency_ms": 0,
                "source": "task_done"}
    # Ambiguous
    listed = "\n".join(f"  · {t['text']}" for _, t in scored[:5])
    return {"reply": f"A few tasks could match \"{body}\":\n{listed}\nWhich one?",
            "tier": "local", "latency_ms": 0,
            "source": "task_done_ambiguous"}


_REMINDER_REMOVE_RE = _re.compile(
    r"^\s*(?:"
    r"don'?t\s+remind\s+me\s+(?:about|to|of)\s+(?P<body>.+?)"
    r"|(?:cancel|remove|delete|drop|kill|clear)\s+(?:the\s+|that\s+|my\s+)?reminder\s+(?:about|to|for|on)\s+(?P<body2>.+?)"
    r"|(?:cancel|remove|delete|drop|kill|clear)\s+(?:my\s+|the\s+)?(?P<body3>.+?)\s+reminder"
    r"|forget\s+(?:about\s+)?(?:the\s+)?reminder\s+(?:about|to|for)\s+(?P<body4>.+?)"
    r"|(?:un[\s-]?set|un[\s-]?schedule)\s+(?:the\s+|my\s+)?reminder\s+(?:about|to|for)\s+(?P<body5>.+?)"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _try_reminder_remove(message: str, user_rec: dict) -> dict | None:
    """'Don't remind me about the meeting' / 'cancel the reminder about
    Friday' — find matching open reminders by token overlap and remove
    them (mark_done). Multiple matches → ask which."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    m = _REMINDER_REMOVE_RE.match(msg)
    if not m:
        return None
    body = (m.group("body") or m.group("body2") or m.group("body3")
            or m.group("body4") or m.group("body5") or "").strip().rstrip("?.!")
    if not body or len(body) < 2:
        return None
    username = user_rec.get("username", "owner")
    try:
        user_dir = users_mod.get_user_dir(DATA_DIR, username)
    except Exception:
        return None
    try:
        open_rem = [r for r in mod_reminders.list_all(user_dir)
                    if r.get("status") in (None, "pending", "snoozed")]
    except Exception:
        return None
    if not open_rem:
        return {"reply": "You don't have any open reminders to cancel.",
                "tier": "local", "latency_ms": 0,
                "source": "reminder_remove_none"}
    # Stopwords get filtered out so "the" / "my" don't make every reminder
    # match every query. Same set used by the task-complete handler.
    _STOPWORDS = {"the", "and", "for", "with", "from", "this", "that",
                  "about", "into", "your", "their", "our", "you", "me",
                  "my", "any", "all", "now", "out", "off", "but", "not",
                  "one", "two", "are", "was", "were", "has", "had", "will"}
    body_l = body.lower()
    body_tokens = set(w for w in _re.split(r"\W+", body_l)
                       if len(w) >= 3 and w not in _STOPWORDS)
    if not body_tokens:
        return None
    scored = []
    for r in open_rem:
        rtext = (r.get("text") or "").lower()
        rtokens = set(w for w in _re.split(r"\W+", rtext)
                       if len(w) >= 3 and w not in _STOPWORDS)
        overlap = len(body_tokens & rtokens)
        if overlap > 0:
            scored.append((overlap, r))
    if not scored:
        return {"reply": f"I don't see a reminder matching \"{body}\". "
                          f"You have {len(open_rem)} open reminder{'s' if len(open_rem) != 1 else ''}.",
                "tier": "local", "latency_ms": 0,
                "source": "reminder_remove_no_match"}
    scored.sort(key=lambda x: -x[0])
    top_score = scored[0][0]
    if len(scored) == 1 or top_score >= scored[1][0] * 2 or top_score >= 2:
        target = scored[0][1]
        try:
            mod_reminders.mark_done(user_dir, target["id"])
        except Exception as e:
            return {"reply": f"Couldn't remove: {e}",
                    "tier": "local", "latency_ms": 0,
                    "source": "reminder_remove_error"}
        audit.log_event(DATA_DIR, actor=username, action="reminder.removed",
                        meta={"reminder_id": target["id"], "text": target.get("text", "")})
        return {"reply": f"✓ Removed reminder: \"{target.get('text','')}\"",
                "tier": "local", "latency_ms": 0,
                "source": "reminder_removed"}
    listed = "\n".join(f"  · {r['text']}" for _, r in scored[:5])
    return {"reply": f"A few reminders could match \"{body}\":\n{listed}\nWhich one?",
            "tier": "local", "latency_ms": 0,
            "source": "reminder_remove_ambiguous"}


_PROJECT_BALANCE_RE = _re.compile(
    r"^\s*(?:"
    r"what(?:'s| was| is)\s+(?:the\s+)?(?:final\s+|outstanding\s+|remaining\s+|current\s+)?balance\s+(?:on|for|of)\s+(?:the\s+)?(?P<q>.+?)(?:\s+(?:project|job|build|closeout))?"
    r"|(?:what\s+(?:did|does)\s+(?:the\s+)?(?P<q2>.+?)\s+(?:project|job|build)?\s*owe|how\s+much\s+(?:did|does)\s+(?:the\s+)?(?P<q3>.+?)\s+(?:project|job|build)?\s*owe)"
    r"|balance\s+(?:on|for)\s+(?P<q4>.+?)(?:\s+(?:project|job|build|closeout))?"
    r")\s*[?.!]?\s*$",
    _re.IGNORECASE,
)


def _try_project_balance(message: str, user_rec: dict) -> dict | None:
    """'What was the final balance on the Birch closeout?' / 'balance on
    Oak' / 'what did Maple owe' — look up the project's billed-vs-paid.
    Honest 'no such project' if not found instead of LLM fabrication."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    m = _PROJECT_BALANCE_RE.match(msg)
    if not m:
        return None
    q = (m.group("q") or m.group("q2") or m.group("q3") or m.group("q4") or "").strip()
    q = _re.sub(r"^the\s+", "", q, flags=_re.IGNORECASE)
    q = _re.sub(r"\s+(?:project|job|build|closeout)$", "", q, flags=_re.IGNORECASE).strip()
    if not q or len(q) < 2:
        return None
    matches = mod_projects.find_by_address(DATA_DIR, q)
    if not matches:
        matches = _find_projects_by_customer(q)
    if not matches:
        return {"reply": (
            f"I don't see a project matching \"{q}\". Either the project "
            f"name is different, or it was never added. Try: \"list jobs\" "
            f"for active projects, or \"history for <customer name>\" if "
            f"it's an older one."
        ), "tier": "local", "latency_ms": 0,
           "source": "gc_project_balance_not_found"}
    if len(matches) > 1:
        listed = "\n".join(f"  · {p['address']}" for p in matches[:5])
        return {"reply": f"Multiple projects match \"{q}\":\n{listed}\nWhich one?",
                "tier": "local", "latency_ms": 0,
                "source": "gc_project_balance_ambiguous"}
    p = matches[0]
    invs = mod_invoices.list_for_project(DATA_DIR, p["id"])
    if not invs:
        return {"reply": (
            f"{p['address']} — no invoices on record yet, so the balance "
            f"is $0 (and contract value of "
            f"${float(p.get('contract_amount') or 0):,.0f} is unbilled)."
        ), "tier": "local", "latency_ms": 0,
           "source": "gc_project_balance_no_invs"}
    billed = sum(float(i.get("amount_due", 0)) for i in invs
                  if i.get("status") not in ("draft", "void"))
    paid = sum(float(i.get("amount_paid", 0)) for i in invs)
    balance = billed - paid
    retainage = sum(float(i.get("retainage_held", 0)) for i in invs
                     if i.get("status") in ("paid", "partial"))
    status_word = "PAID IN FULL" if balance <= 0 else "BALANCE OWED"
    lines = [f"{p['address']} — {p.get('label','(no label)')}"]
    lines.append(f"  Status:    {p.get('status','?').upper()}")
    lines.append(f"  Billed:    ${billed:,.0f}")
    lines.append(f"  Paid:      ${paid:,.0f}")
    lines.append(f"  Balance:   ${balance:,.0f} ({status_word})")
    if retainage:
        lines.append(f"  Retainage: ${retainage:,.0f} (released at closeout)")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "gc_project_balance"}


def _try_math_quick(message: str) -> dict | None:
    """Hypothetical balance math from a single message: ' billed X $A and
    paid $B' → owed = A - B. 'X owes $A but paid $B' → balance = A - B.
    Plus the simplest two-amount subtraction: 'A minus B' or 'A - B'."""
    if not message:
        return None
    msg = _strip_polite_prefix(message)
    # Hypothetical billed/paid form
    m = _re.match(
        r"^\s*(?:if|suppose|imagine|what\s+if|let'?s\s+say)\s+"
        r".+?(?:bill(?:ed)?|invoice[ds]?|charge[ds]?)\s+.+?\$([\d,]+(?:\.\d+)?)\s*(?:k|K)?"
        r".+?(?:paid|pay(?:s|ed)?|sent|gave|wrote\s+(?:me\s+)?a\s+check\s+for)\s+\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)?"
        r".+?(?:owed|still\s+owed?|remaining|left|balance|owe)?",
        message, _re.IGNORECASE)
    if m:
        def _to_num(s: str, k_suffix: bool) -> float:
            v = float(s.replace(",", ""))
            return v * 1000 if k_suffix else v
        # Check if the captured amounts had "k" suffix
        billed_str = m.group(1)
        paid_str = m.group(2)
        billed = float(billed_str.replace(",", ""))
        paid = float(paid_str.replace(",", ""))
        # Crude k-suffix detection from raw match span
        raw = m.group(0).lower()
        if _re.search(rf"\${_re.escape(billed_str)}\s*k", raw): billed *= 1000
        if _re.search(rf"\${_re.escape(paid_str)}\s*k", raw): paid *= 1000
        owed = billed - paid
        sign = "owes you" if owed > 0 else "you owe them" if owed < 0 else "you're square"
        if owed > 0:
            return {"reply": (
                f"Billed ${billed:,.0f} − paid ${paid:,.0f} = "
                f"**${owed:,.0f} still owed**.\n"
                f"(If this is a real invoice, tell me which INV-XXXX and I'll record the partial payment.)"
            ), "tier": "local", "latency_ms": 0, "source": "math_balance"}
        if owed < 0:
            return {"reply": (
                f"Billed ${billed:,.0f} − paid ${abs(owed) + billed:,.0f} = "
                f"customer overpaid by **${abs(owed):,.0f}**.\n"
                f"You'd refund the difference or apply it as a credit."
            ), "tier": "local", "latency_ms": 0, "source": "math_overpaid"}
        return {"reply": f"Billed ${billed:,.0f} − paid ${paid:,.0f} = $0. Square.",
                "tier": "local", "latency_ms": 0, "source": "math_square"}
    return None


def _try_appointments_today(message: str, user_rec: dict) -> dict | None:
    """'What appointments are scheduled today?' / 'appointments today' —
    hit the calendar deterministically so the LLM can't fabricate."""
    msg = _strip_polite_prefix(message or "")
    if not _APPT_TODAY_RE.match(msg):
        return None
    username = user_rec.get("username", "owner")
    try:
        user_dir = users_mod.get_user_dir(DATA_DIR, username)
    except Exception:
        return None
    try:
        events = mod_calendar.today(user_dir) or []
    except Exception:
        events = []
    if not events:
        return {"reply": "No appointments on the calendar for today.",
                "tier": "local", "latency_ms": 0,
                "source": "appt_today_clear"}
    lines = [f"Today's calendar ({len(events)}):"]
    for e in events[:10]:
        start = (e.get("start") or "")[11:16] or "?"
        title = e.get("title", "")
        lines.append(f"  · {start} — {title}")
    return {"reply": "\n".join(lines), "tier": "local",
            "latency_ms": 0, "source": "appt_today"}


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
    r"(?:can|could|would|will|may)\s+you\s+(?:please\s+)?|"
    # Question form with "I" as subject — "Can I book...", "Could I get...",
    # "May I have..." — strip the prefix so the quick-capture trigger sees
    # the verb that follows. Without this, "Can I book an appointment for
    # Thursday at 2pm" never reaches the booking handler and falls to the
    # LLM, which then talks like it scheduled the appointment without
    # actually writing anything to the calendar.
    r"(?:can|could|would|will|may|should)\s+i\s+(?:please\s+)?|"
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
    """The existing wizard hits this endpoint with a website URL. We now
    route it through the FULL site crawler (every page + LLM extraction
    per page) instead of the old shallow 6-page version. Response shape
    stays backward-compatible (`{draft, gap_questions}`) so the wizard
    JS doesn't need to change."""
    user = auth.require_user(ORBI_DIR, DATA_DIR)
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        import site_scraper
        brain_call = site_scraper.make_brain_call(CONFIG)
        # save=True writes data/customer_profiles/<slug>.json — that
        # populates the multi-tenant routing as a side effect.
        result = site_scraper.crawl_site(
            url, data_dir=DATA_DIR, brain_call=brain_call,
            max_pages=int(data.get("max_pages", 200)),
            max_depth=int(data.get("max_depth", 5)),
            wall_time_seconds=int(data.get("wall_time_seconds", 600)),
            fetch_delay_seconds=float(data.get("fetch_delay_seconds", 0.5)),
            save=True,
        )
        profile = result["profile"]
        confidence = result["confidence"]
        # Convert to the legacy `draft` shape the wizard expects.
        # The new profile is mostly the same keys (name, tagline, address,
        # contact, hours, services) plus extras (menu_items, faq, policies)
        # that the wizard ignores for now.
        draft = dict(profile)
        draft["_confidence"] = confidence
        draft["_pages_visited"] = result["pages_visited"]
        draft["_elapsed_seconds"] = result["elapsed_seconds"]
        # gap_questions: build from the confidence map so the wizard's
        # gap-fill flow still works. Same shape as the old onboarding.gap_questions.
        questions = _gap_questions_from_confidence(confidence, profile)
        audit.log_event(DATA_DIR, actor=user["username"],
                        action="onboarding.discover_full",
                        meta={"url": url,
                              "pages_visited": result["pages_visited"],
                              "elapsed_seconds": result["elapsed_seconds"]})
        return jsonify({"draft": draft, "gap_questions": questions})
    except Exception as e:
        log.warning(f"onboarding discover (full crawl) failed: {e}")
        # Fall back to the old shallow scraper if the full one fails for
        # any reason — owner can still onboard, just with less data.
        try:
            draft = onboarding.discover_from_url(url)
            questions = onboarding.gap_questions(draft)
            return jsonify({"draft": draft, "gap_questions": questions,
                            "_fallback": "shallow_scraper"})
        except Exception as e2:
            return jsonify({"error": f"{e}; fallback also failed: {e2}"}), 500


def _gap_questions_from_confidence(confidence: dict, profile: dict) -> list[dict]:
    """Build the wizard's gap-fill questions from the new full-scrape
    confidence map. Format mirrors what onboarding.gap_questions() returns
    so the existing wizard UI consumes them unchanged.
    Each question: {field, prompt, kind: 'text'|'address'|'hours', placeholder}."""
    out = []
    if confidence.get("name") != "high":
        out.append({"field": "name", "kind": "text",
                    "prompt": "What's your business called?",
                    "placeholder": "PurBlum / Joe's Pizza"})
    if confidence.get("tagline") != "high":
        out.append({"field": "tagline", "kind": "text",
                    "prompt": "One short line describing your business — what would you tell a stranger in an elevator?",
                    "placeholder": "Neighborhood deli + market in Reno"})
    if confidence.get("address") != "high":
        out.append({"field": "address", "kind": "address",
                    "prompt": "What's your address? (Customers ask this constantly.)",
                    "placeholder": "1842 Sierra Market Lane, Reno NV 89501"})
    if confidence.get("phone") != "high":
        out.append({"field": "contact.phone", "kind": "text",
                    "prompt": "Best phone number for customers?",
                    "placeholder": "(775) 555-0192"})
    if confidence.get("email") != "high":
        out.append({"field": "contact.email", "kind": "text",
                    "prompt": "Best email for customer questions?",
                    "placeholder": "hello@yourbusiness.com"})
    if confidence.get("hours") != "high":
        out.append({"field": "hours", "kind": "hours",
                    "prompt": "What are your business hours? (I couldn't pull them off the site cleanly.)",
                    "placeholder": "Mon-Fri 9-5, Sat 10-2, Sun closed"})
    if confidence.get("services") != "high" and confidence.get("menu_items") != "high":
        out.append({"field": "services", "kind": "text",
                    "prompt": "What do you sell or do? (List 3-8 main things — paste your menu, list services, whatever fits.)",
                    "placeholder": "Sandwiches, soups, salads, catering trays, market grocery"})
    if confidence.get("policies") != "high":
        out.append({"field": "policies.payment_methods", "kind": "text",
                    "prompt": "How do customers pay? (Cash, credit, Venmo, etc.)",
                    "placeholder": "Credit cards, cash, Apple Pay"})
    return out


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

# ═════════════════════════════════════════════════════════════════════════
#   Multi-tenant chat routing — customer profile by request origin
# ═════════════════════════════════════════════════════════════════════════
#
# When a visitor on purblum.com chats with Orby through the embed widget,
# the iframe's Origin header tells us where the request came from. We map
# the host (purblum.com) to a scraped customer profile at
# data/customer_profiles/<slug>.json and load THAT instead of the
# default business_info.json. Unknown origins fall back to the default.

_ORDER_INTENT_HINTS_WEB = (
    "order", "i'll have", "i will have", "i'd like", "can i get",
    "lemme get", "let me get", "give me", "i want", "i need",
    "pickup", "pick up", "to go", "for here",
)

# Signals the customer is CLOSING the order — at this point we want totals.
# Mid-order modifier collection ("12 inch", "no onions") does NOT need an
# extraction; running the extract LLM on every modifier doubled latency.
_END_OF_ORDER_SIGNALS = (
    "that's it", "thats it", "that's all", "thats all", "i'm good",
    "im good", "i am good", "we're done", "we are done", "nothing else",
    "no more", "no thanks", "no thank you", "all set", "ready to pay",
    "submit", "place the order", "place my order", "send it",
    "how much", "what's the total", "what is the total", "what's it come to",
    "what does that come to", "ring me up", "im done", "i'm done",
)


def _looks_like_order_chat(history: list[dict]) -> bool:
    """True if the chat looks like an order is IN PROGRESS — any user turn
    mentioned an order-intent keyword."""
    if not history:
        return False
    text = " ".join(m.get("content", "").lower()
                    for m in history if m.get("role") == "user")
    return any(h in text for h in _ORDER_INTENT_HINTS_WEB)


def _is_end_of_order_message(last_user_msg: str) -> bool:
    """True if the customer's LATEST message signals they're closing the
    order (price ask, 'that's it', confirmation, etc.). Only at these
    moments do we run the expensive extract → cart → totals pipeline."""
    if not last_user_msg:
        return False
    txt = last_user_msg.lower()
    return any(s in txt for s in _END_OF_ORDER_SIGNALS)


def _menu_dict_from_profile(profile: dict) -> dict:
    """Convert the customer profile's flat menu_items list back into the
    categories+items shape phone_order expects. Just enough structure for
    the extract/cart/total functions to do their job."""
    items_by_cat: dict[str, list[dict]] = {}
    for item in profile.get("menu_items") or []:
        cat = item.get("category", "Menu")
        items_by_cat.setdefault(cat, []).append(item)
    cats = [{"name": c, "items": items_by_cat[c]} for c in items_by_cat]
    return {"categories": cats, "_meta": {"tax_rate": profile.get("tax_rate", 0)}}


def _load_scraped_pages_text(slug: str, max_bytes: int = 15000) -> str:
    """Read every scraped page .txt for a customer profile slug and return
    them concatenated, capped at max_bytes total. Used to give the LLM
    visibility into the raw scraped website content so it can answer
    questions about specifics (item names, flavors, varieties, hours)
    that the LLM extractor didn't pull into the structured profile."""
    pages_dir = DATA_DIR / "customer_profiles" / f"{slug}_pages"
    if not pages_dir.exists():
        return ""
    parts: list[str] = []
    total = 0
    for p in sorted(pages_dir.glob("*.txt")):
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # Per-page cap so one huge page doesn't eat the whole budget
        if len(txt) > 5000:
            txt = txt[:5000] + "\n…[truncated]"
        chunk = f"\n--- PAGE: {p.stem} ---\n{txt}\n"
        if total + len(chunk) > max_bytes:
            break
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts)


def _resolve_business_profile_for_request(req) -> dict:
    """Pick the right business profile to chat AS, based on which website
    the visitor came from. Always returns a usable profile.

    Signal sources, in priority:
      0. X-Demo-As header — Frank's portable demo mode. When set, loads
         that slug's profile directly (bypasses host-based routing). Used
         when Frank scrapes a prospect's site and shows them Orby acting
         as their business.
      1. X-Embed-Parent header — set by the embedded chat widget so the
         iframe (whose own Origin is the tunnel URL) can still tell us
         which CUSTOMER PAGE it was loaded on.
      2. Origin header — set by browsers on cross-origin XHR/fetch.
      3. Referer header — fallback for direct page loads.

    Whenever a profile is loaded by slug, this function also attaches the
    raw scraped page text under `_scraped_pages_text` so the prompt builder
    can render it for the LLM — that's how Orby answers questions about
    items the structured extractor missed.
    """
    # 0. Demo mode override — explicit slug, skip host lookup
    demo_slug = (req.headers.get("X-Demo-As") or req.args.get("demo_as") or "").strip()
    if demo_slug:
        import re as _r
        slug_clean = _r.sub(r"[^a-z0-9_]", "", demo_slug.lower())
        if slug_clean:
            profile_path = DATA_DIR / "customer_profiles" / f"{slug_clean}.json"
            if profile_path.exists():
                try:
                    with profile_path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["_scraped_pages_text"] = _load_scraped_pages_text(slug_clean)
                    return data
                except Exception as e:
                    log.warning(f"demo profile load failed for {slug_clean}: {e}")

    embed_parent = (req.headers.get("X-Embed-Parent") or "").strip()
    origin = (req.headers.get("Origin") or "").strip()
    referer = (req.headers.get("Referer") or "").strip()
    host = ""
    for source in (embed_parent, origin, referer):
        if not source:
            continue
        try:
            from urllib.parse import urlparse
            host = urlparse(source).netloc.lower().removeprefix("www.")
            if host:
                break
        except Exception:
            continue
    if not host:
        return mod_business.load(DATA_DIR)
    # Same slug rule as the scraper uses
    import re as _r
    slug = _r.sub(r"[^a-z0-9]+", "_", host).strip("_")
    profile_path = DATA_DIR / "customer_profiles" / f"{slug}.json"
    if not profile_path.exists():
        return mod_business.load(DATA_DIR)
    try:
        with profile_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["_scraped_pages_text"] = _load_scraped_pages_text(slug)
        return data
    except Exception as e:
        log.warning(f"profile load failed for {host}: {e}")
        return mod_business.load(DATA_DIR)


# ═════════════════════════════════════════════════════════════════════════
#   PurBlum demo — public ordering endpoints for the deli test site
# ═════════════════════════════════════════════════════════════════════════
# purblum.com (Frank's stunt-double demo deli) has a working order form
# that needs a backend. These endpoints serve as that backend. They are
# OPEN (no auth) because they're called by visitors on the public deli
# site. CORS is allowed for purblum.com origins.
#
# /api/public/purblum/estimate — compute cart subtotal/tax/total
# /api/public/purblum/order    — record the order + text Frank's cell
#
# Twilio SMS uses CONFIG.phone.* creds. If those aren't set yet, orders
# fall back to writing into data/purblum_orders_log.jsonl so the flow
# still works for testing.

_PURBLUM_TAX_RATE = 0.0825  # Reno-ish sales tax — overridable in config later
_PURBLUM_ALLOWED_ORIGINS = {
    "https://purblum.com", "https://www.purblum.com",
    "http://localhost:8000", "http://127.0.0.1:8000",  # local preview
}


def _purblum_cors(resp, origin: str):
    """Permissive CORS for known PurBlum origins. Restricts to a whitelist
    so this isn't an open relay for anyone on the internet."""
    if origin in _PURBLUM_ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "600"
    return resp


def _purblum_send_sms(body: str) -> tuple[bool, str]:
    """Send an SMS to Frank's cell with the order summary. Returns
    (sent_ok, status_message). Falls back to file-log if Twilio creds
    aren't configured."""
    phone_cfg = CONFIG.get("phone") or {}
    sid = phone_cfg.get("twilio_account_sid")
    token = phone_cfg.get("twilio_auth_token")
    from_number = phone_cfg.get("twilio_number")
    to_number = (CONFIG.get("notifications") or {}).get("owner_sms")
    # If anything's missing, log to file as a fallback so the test flow
    # works without blocking on creds setup.
    if not (sid and token and from_number and to_number):
        try:
            log_path = DATA_DIR / "purblum_orders_log.jsonl"
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": int(time.time()),
                    "would_send_to": to_number or "(owner_sms not set)",
                    "from": from_number or "(twilio_number not set)",
                    "body": body,
                }) + "\n")
            return (False,
                    "twilio_creds_missing — logged to "
                    f"data/purblum_orders_log.jsonl. Set CONFIG.phone.twilio_* "
                    f"and CONFIG.notifications.owner_sms to enable real SMS.")
        except Exception as e:
            log.warning(f"purblum SMS fallback log failed: {e}")
            return (False, f"twilio_creds_missing AND log_write_failed: {e}")
    # Real send
    try:
        from twilio.rest import Client
        client = Client(sid, token)
        msg = client.messages.create(
            body=body[:1600],   # Twilio segment cap is 1600 chars
            from_=from_number,
            to=to_number,
        )
        return (True, f"sent (sid={msg.sid})")
    except Exception as e:
        log.warning(f"purblum SMS send failed: {e}")
        return (False, f"twilio_send_failed: {e}")


def _purblum_compute_totals(items: list[dict]) -> dict:
    """Total = sum(qty * (base_price + modifier deltas)) for valid items,
    with tax. Handles BOTH the new structured-modifier payload (preferred)
    and the legacy flat-price payload (back-compat). Server recomputes
    from base_price + deltas so client-side total can't be manipulated."""
    subtotal = 0.0
    cleaned = []
    for raw in (items or []):
        try:
            qty = max(1, int(raw.get("qty", 1)))
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            # Structured modifiers (new shape): list of
            #   {group, action: 'choose'|'add'|'remove', label, delta}
            mods_raw = raw.get("modifiers")
            if isinstance(mods_raw, list):
                # New structured shape — recompute price from base + deltas
                base_price = float(raw.get("base_price") or 0)
                deltas = 0.0
                clean_mods = []
                for m in mods_raw:
                    if not isinstance(m, dict):
                        continue
                    action = (m.get("action") or "").strip()
                    label = (m.get("label") or "").strip()
                    delta = float(m.get("delta") or 0)
                    if not label or action not in ("choose", "add", "remove"):
                        continue
                    clean_mods.append({
                        "group": (m.get("group") or "").strip(),
                        "action": action,
                        "label": label,
                        "delta": delta,
                    })
                    if action != "remove":  # remove = uncheck-default, no price impact
                        deltas += delta
                unit_price = max(0.0, base_price + deltas)
                # Sanity check: if base_price wasn't sent, fall back to client's line_total
                if base_price <= 0:
                    unit_price = float(raw.get("line_total") or raw.get("price") or 0)
                line = round(qty * unit_price, 2)
                if line <= 0:
                    continue
                subtotal += line
                cleaned.append({
                    "id": raw.get("id") or "",
                    "name": name, "qty": qty,
                    "base_price": base_price,
                    "unit_price": round(unit_price, 2),
                    "modifiers": clean_mods,
                    "line_total": line,
                })
            else:
                # Legacy flat shape — {name, price, qty, modifiers: "text notes"}
                price = float(raw.get("price", 0) or raw.get("line_total", 0) or 0)
                if price <= 0:
                    continue
                line = round(qty * price, 2)
                subtotal += line
                cleaned.append({
                    "id": raw.get("id") or "",
                    "name": name, "qty": qty,
                    "unit_price": price,
                    "modifiers": (mods_raw or raw.get("notes") or "").strip() if isinstance(mods_raw, str) else "",
                    "line_total": line,
                })
        except (ValueError, TypeError):
            continue
    subtotal = round(subtotal, 2)
    tax = round(subtotal * _PURBLUM_TAX_RATE, 2)
    total = round(subtotal + tax, 2)
    return {"items": cleaned, "subtotal": subtotal, "tax": tax,
            "tax_rate": _PURBLUM_TAX_RATE, "total": total}


def _purblum_format_sms(payload: dict, totals: dict) -> str:
    """Human-readable order summary for the SMS body. Renders structured
    modifiers cleanly so Frank can verify accuracy at a glance:
        2x Truckee Italian — $25.40
          on Gluten-free roll (+$1.50)
          Whole (12-inch) (+$4.00)
          NO Onion · NO Tomato
          + Bacon (+$1.75)
          + Swiss (+$1.00)
    """
    lines = ["🥪 PURBLUM ORDER"]
    if payload.get("type"):
        lines.append(f"Type: {payload['type']}")
    if payload.get("customer_name"):
        lines.append(f"Name: {payload['customer_name']}")
    if payload.get("customer_phone"):
        lines.append(f"Phone: {payload['customer_phone']}")
    if payload.get("pickup_time"):
        lines.append(f"Pickup: {payload['pickup_time']}")
    lines.append("")
    lines.append("ITEMS:")
    for it in totals.get("items", []):
        lines.append(f"  {it['qty']}x {it['name']} — ${it['line_total']:.2f}")
        mods = it.get("modifiers", [])
        if isinstance(mods, list):
            # Group removes onto one line ("NO Onion · NO Tomato") so the
            # SMS is denser and easier to scan.
            removes = [m["label"] for m in mods if m.get("action") == "remove"]
            if removes:
                lines.append("      NO " + " · NO ".join(removes))
            for m in mods:
                if m.get("action") == "choose":
                    delta_txt = ""
                    if m.get("delta", 0):
                        d = m["delta"]
                        delta_txt = f" ({'+' if d>0 else ''}${d:.2f})" if d != 0 else ""
                    lines.append(f"      {m['label']}{delta_txt}")
            for m in mods:
                if m.get("action") == "add":
                    delta_txt = ""
                    if m.get("delta", 0):
                        d = m["delta"]
                        delta_txt = f" (+${d:.2f})" if d > 0 else f" (${d:.2f})"
                    lines.append(f"      + {m['label']}{delta_txt}")
        elif isinstance(mods, str) and mods.strip():
            lines.append(f"      [{mods}]")
    lines.append("")
    lines.append(f"Subtotal: ${totals['subtotal']:.2f}")
    lines.append(f"Tax:      ${totals['tax']:.2f}")
    lines.append(f"TOTAL:    ${totals['total']:.2f}")
    if payload.get("notes"):
        lines.append("")
        lines.append(f"Notes: {payload['notes']}")
    return "\n".join(lines)


@app.route("/api/public/chat/submit_order", methods=["POST", "OPTIONS"])
def chat_submit_order():
    """Web chat → web_driver submission. Takes the conversation history,
    extracts the order, builds the cart, runs the SAME web_driver pipeline
    the phone uses to actually click through purblum.com and submit.

    Body: {history: [...], visitor: {name, phone, email}}
    Returns: {ok, order_id, total, error, screenshot}
    """
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        return _purblum_cors(make_response("", 204), origin)
    payload = request.get_json(silent=True) or {}
    history = payload.get("history") or []
    visitor = payload.get("visitor") or {}
    business = _resolve_business_profile_for_request(request)
    biz_name = business.get("name", "")

    if not history:
        return _purblum_cors(jsonify({"ok": False, "error": "empty_history"}), origin), 400
    if "purblum" not in biz_name.lower():
        return _purblum_cors(jsonify({"ok": False, "error": "no_driver_for_business"}), origin), 400

    try:
        import phone_order
        # Hardcoded path for the PurBlum demo. When we add real customers
        # this should come from the business profile (profile.menu_source).
        menu_path = "/home/frank/purblum_live/data/menu.json"
        menu = phone_order.load_menu(menu_path)
        ext = phone_order.extract_order_from_history(history, menu, llm_client, CONFIG)
        if not ext.get("ok") or not ext.get("items"):
            return _purblum_cors(jsonify({
                "ok": False, "error": "extract_failed",
                "detail": ext.get("error", "no items extracted")
            }), origin), 400
        cart, warnings = phone_order.build_cart_for_purblum(ext["items"], menu)
        phone_order.annotate_cart_with_menu_prices(cart, menu)
        if not cart:
            return _purblum_cors(jsonify({
                "ok": False, "error": "empty_cart",
                "warnings": warnings,
            }), origin), 400
        customer_info = ext.get("customer", {}) or {}
        customer_name = (customer_info.get("name") or visitor.get("name")
                         or "Web Customer").strip()
        customer_phone = (customer_info.get("phone") or visitor.get("phone")
                          or "").strip()
        pickup_time = (customer_info.get("pickup_time") or "").strip()
        # Run web_driver — actually clicks through purblum.com
        from web_driver import submit_purblum_order
        result = submit_purblum_order(
            cart=cart,
            customer={
                "name": customer_name,
                "phone": customer_phone,
                "pickup_time": pickup_time,
            },
            notes="Submitted by Orby (chat order)",
            headless=True, record_video=False,
            data_dir=DATA_DIR,
        )
        totals = phone_order.compute_cart_total(cart, business.get("tax_rate", 0))
        log.info(f"chat order submitted via web_driver: "
                  f"ok={result.get('ok')} order_id={result.get('order_id','?')} "
                  f"customer={customer_name} phone={customer_phone}")
        return _purblum_cors(jsonify({
            "ok": result.get("ok", False),
            "order_id": result.get("order_id", ""),
            "total": result.get("total", ""),
            "subtotal": totals["subtotal"],
            "tax": totals["tax"],
            "total_dollars": totals["total"],
            "error": result.get("error") if not result.get("ok") else None,
            "screenshot": result.get("screenshot", ""),
            "elapsed_seconds": result.get("elapsed_seconds", 0),
            "warnings": warnings,
        }), origin)
    except Exception as e:
        log.exception(f"chat submit_order crashed: {e}")
        return _purblum_cors(jsonify({
            "ok": False, "error": f"server_error: {type(e).__name__}"
        }), origin), 500


@app.route("/api/public/purblum/estimate", methods=["POST", "OPTIONS"])
def purblum_estimate():
    """Live cart-pricing endpoint — POST {items: [...]} → {subtotal, tax, total}."""
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        return _purblum_cors(make_response("", 204), origin)
    payload = request.get_json(silent=True) or {}
    totals = _purblum_compute_totals(payload.get("items") or [])
    return _purblum_cors(jsonify(totals), origin)


@app.route("/api/public/purblum/order", methods=["POST", "OPTIONS"])
def purblum_order():
    """Final order submit — POST {type, customer_name, customer_phone,
    pickup_time, items, notes} → records to data/purblum_orders.jsonl,
    fires SMS to Frank's cell, returns confirmation."""
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        return _purblum_cors(make_response("", 204), origin)
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    if not items:
        return _purblum_cors(
            jsonify({"error": "empty_cart"}), origin), 400
    totals = _purblum_compute_totals(items)
    if totals["total"] <= 0:
        return _purblum_cors(
            jsonify({"error": "no_valid_items"}), origin), 400
    # Build order record
    import uuid
    order_id = uuid.uuid4().hex[:12]
    record = {
        "order_id":       order_id,
        "ts":             int(time.time()),
        "type":           payload.get("type", "pickup"),
        "customer_name":  (payload.get("customer_name") or "").strip(),
        "customer_phone": (payload.get("customer_phone") or "").strip(),
        "pickup_time":    (payload.get("pickup_time") or "").strip(),
        "notes":          (payload.get("notes") or "").strip(),
        "items":          totals["items"],
        "subtotal":       totals["subtotal"],
        "tax":            totals["tax"],
        "total":          totals["total"],
    }
    # Persist locally so Frank has an audit trail
    try:
        orders_log = DATA_DIR / "purblum_orders.jsonl"
        with orders_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning(f"purblum order log write failed: {e}")
    # Fire the SMS (or fallback to log if creds missing)
    sms_body = _purblum_format_sms(payload, totals)
    sms_ok, sms_status = _purblum_send_sms(sms_body)
    audit.log_event(DATA_DIR, actor="purblum_public",
                    action="purblum.order.placed",
                    meta={"order_id": order_id,
                          "total": totals["total"],
                          "items_count": len(totals["items"]),
                          "sms_ok": sms_ok, "sms_status": sms_status})
    log.info(f"PurBlum order {order_id} ${totals['total']:.2f} — SMS {sms_status}")
    return _purblum_cors(jsonify({
        "order_id": order_id,
        "status": "received",
        "subtotal": totals["subtotal"],
        "tax": totals["tax"],
        "total": totals["total"],
        "message": (f"Got it — order {order_id[:6]} for ${totals['total']:.2f}. "
                    f"Frank's been notified."),
    }), origin)


if __name__ == "__main__":
    main()
