#!/usr/bin/env python3
"""
Orbi — customer-side service.

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

from flask import (Flask, abort, jsonify, make_response, request,
                   send_from_directory)

import audit
import auth
import backup
import cross_search as xs
import doc_convert
import file_fetch
import gcal
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
from tools import web_search as tool_web_search

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

def _default_orbi_dir() -> Path:
    """Platform-appropriate default if ORBI_DIR isn't set."""
    import platform as _p
    if _p.system() == "Windows":
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Orbi"
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
    return (f"Orbi is running but no chat shell is installed. "
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

@app.route("/tts", methods=["GET", "POST"])
def tts():
    """Generate MP3 audio for text using Edge TTS neural voices.
    Same engine as twickell.com. Streams chunks as edge_tts produces them
    so the browser can start playing within ~300ms instead of waiting 1-2s
    for the full file. GET form lets <audio src="/tts?text=..."> work directly."""
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
    if len(text) > 1500:
        text = text[:1500]

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
    which columns did Orbi map. Powers the 'Catalog' widget in the
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
    # system-prompt context so Orbi handles the moment with care instead
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
    # Orbi's knowledge compound over time and never invent facts about
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
    # promotions, menus, FAQs the owner dropped into ~/Orbi/) — these are
    # AUTHORITATIVE. Web search only fires if workspace had no strong match
    # AND the query smells like it needs current info.
    extras = []

    # PRIORITY 1 — PRODUCT CATALOG (highest authority).
    # If the owner has dropped a CSV / Excel into Orby/Catalog/, surface
    # matching items BEFORE workspace and web. Catalog data is the most
    # specific real-world info Orbi can use (real SKUs, real prices, real
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
            # On fresh queries we ALSO want workspace context if there's a match,
            # but secondary to the live web data
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

    # LEARNING-LOOP TRIGGER — if Orbi's reply reads like "I don't know"
    # AND the visitor was actually asking a question, kick off the
    # learning loop: capture the question for the owner to answer, ask
    # the visitor for their contact info, and override Orbi's bluff
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
            # Override Orbi's reply with the standard "I'll find out" ask.
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
    # for these tokens since Orbi just wrote the file there itself — we pass
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
    # READ patterns: answer directly from the user's per-user data.
    pa_direct = _try_personal_assistant_read(user_msg, user_dir)
    if pa_direct is not None:
        return jsonify({"reply": pa_direct, "tier": "local", "latency_ms": 0,
                        "source": "personal_assistant"})

    # FILE FETCH: "send me the Maxwell estimate from my computer"
    ff = _try_file_fetch(user_msg, username)
    if ff is not None:
        return jsonify(ff)

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
    r"\b(?:show|list|what(?:'s|s| are))\s+(?:my\s+)?(?:open\s+)?(?:todo|to[\s-]?do|tasks?)(?:\s+list)?\b",
    _re.IGNORECASE,
)
_PA_REMINDERS_RE = _re.compile(
    r"\b(?:show|list|what(?:'s|s| are))\s+(?:my\s+)?(?:pending\s+)?reminders?\b|"
    r"\bwhat\s+do\s+i\s+need\s+to\s+be\s+reminded\s+(?:about|of)\b",
    _re.IGNORECASE,
)
_PA_WHO_IS_RE = _re.compile(
    r"\bwho\s+is\s+(?P<name>[A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+){0,2})\b",
)
_PA_PHONE_OF_RE = _re.compile(
    r"\b(?:what(?:'s|s| is)?|find|get|look\s*up)\s+"
    r"(?P<name>[A-Z][a-zA-Z\-]+(?:\s+[A-Z][a-zA-Z\-]+){0,2})"
    r"(?:'s|s')?\s+"
    r"(?:phone|number|email|contact)\b",
)


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
            f"  - {e.get('start','')[11:16]}  {e.get('title','')}" for e in events
        )

    if _PA_WEEK_RE.search(message):
        events = mod_calendar.upcoming(user_dir, days=7)
        if not events:
            return "Nothing on your calendar this week."
        return "Upcoming this week:\n" + "\n".join(
            f"  - {e.get('start','')[:16].replace('T',' ')}  {e.get('title','')}" for e in events
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
            f"  - {r.get('due','')[:16].replace('T',' ')}  {r.get('text','')}" for r in items
        )

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


def _try_quick_capture(message: str, user_dir: Path) -> dict | None:
    """If the message starts with a quick-capture trigger word, run it
    through quick_capture.capture() and return the result dict. Otherwise None."""
    if not message or not _QC_TRIGGER_RE.match(message.strip()):
        return None
    try:
        return mod_qc.capture(user_dir, message)
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
    installer (one Cloud project per Orbi deployment) or set via dashboard."""
    g = CONFIG.get("gcal_oauth") or {}
    return g.get("client_id", ""), g.get("client_secret", "")


def _gcal_redirect_uri() -> str:
    """Loopback redirect for Desktop-app OAuth client type. Customer connects
    Google while on the same machine as their Orbi install (first-time setup)."""
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
# Phone PWA → cloudflared tunnel → home Orbi → scoped file resolution
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

@app.route("/api/internal/notify", methods=["POST"])
def internal_notify():
    if request.headers.get("X-Watchdog") != "1":
        abort(403)
    data = request.get_json(silent=True) or {}
    title = data.get("title") or "Orbi alert"
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
                title="Orbi test notification",
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
    log.info(f"Orbi starting on {host}:{port}")
    log.info(f"  Brain:       {(CONFIG.get('brain') or {}).get('url', 'not configured')}")
    log.info(f"  HuggingFace: {'enabled' if (CONFIG.get('huggingface') or {}).get('enabled') else 'disabled'}")
    log.info(f"  Local LLM:   {'enabled' if (CONFIG.get('local_llm') or {}).get('enabled') else 'disabled'}")
    log.info(f"  Data dir:    {DATA_DIR}")
    app.run(host=host, port=port, threaded=True)

if __name__ == "__main__":
    main()
