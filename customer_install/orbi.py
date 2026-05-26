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
import threading
import time
from pathlib import Path

from flask import (Flask, abort, jsonify, make_response, request,
                   send_from_directory)

import audit
import auth
import backup
import llm_client
import notifications as notify
import prompts
import voice
from modules import business_info as mod_business
from modules import catalog as mod_catalog
from modules import learning_loop as mod_learning
from modules import memory as mod_memory
from modules import messages as mod_messages
from modules import notes as mod_notes
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

@app.route("/tts", methods=["POST"])
def tts():
    """Generate MP3 audio for text using Edge TTS neural voices.
    Same engine as your personal Orby on twickell.com — sounds natural."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    voice = data.get("voice") or _DEFAULT_VOICE
    rate  = data.get("rate", "+0%")
    if not text:
        return jsonify({"error": "empty_text"}), 400
    # Cap length to keep things fast
    if len(text) > 1500:
        text = text[:1500]

    import asyncio
    import io as _io
    try:
        import edge_tts
    except ImportError:
        return jsonify({"error": "edge_tts_not_installed"}), 503

    async def synthesize():
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        buf = _io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    try:
        audio = asyncio.run(synthesize())
    except Exception as e:
        log.warning(f"tts failed: {e}")
        return jsonify({"error": f"tts_failed: {e}"}), 500

    from flask import Response
    return Response(audio, mimetype="audio/mpeg",
                    headers={"Cache-Control": "no-cache",
                             "Content-Length": str(len(audio))})

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

    # PRIORITY 0 — LEARNED ANSWERS (never-guess pattern). If the OWNER has
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
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    owner = CONFIG.get("owner", {})
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    if email != (owner.get("email") or "").lower():
        audit.log_event(DATA_DIR, actor=email, action="owner.login.failed",
                        ip=ip, meta={"reason": "unknown_email"})
        return jsonify({"error": "Invalid email or password"}), 401
    if not auth.verify_password(password, owner.get("_password_hash", "")):
        audit.log_event(DATA_DIR, actor=email, action="owner.login.failed",
                        ip=ip, meta={"reason": "bad_password"})
        return jsonify({"error": "Invalid email or password"}), 401
    token = auth.issue_session(ORBI_DIR, email)
    resp = make_response(jsonify({"status": "ok"}))
    auth.set_session_cookie(resp, token)
    audit.log_event(DATA_DIR, actor=email, action="owner.login.success", ip=ip)
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
    auth.require_owner(ORBI_DIR)
    business = mod_business.load(DATA_DIR)
    return jsonify({
        "business_name": business.get("name") or CONFIG.get("business", {}).get("name", ""),
        "tier": BILLING_STATUS.get("tier"),
        "active": BILLING_STATUS.get("active"),
        "warning": BILLING_STATUS.get("warning"),
        "period_end": None,  # filled from billing once endpoint returns it
        "connection": llm_client.current_connection_state(CONFIG),
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
    auth.require_owner(ORBI_DIR)
    data = request.get_json(silent=True) or {}
    user_msg = (data.get("message") or "").strip()
    history  = data.get("history") or []
    if not user_msg:
        return jsonify({"error": "empty message"}), 400

    business = mod_business.load(DATA_DIR)
    system = prompts.build_owner_prompt(business)

    # Owner mode gets memory + notes + workspace files as extra context
    extras = []
    notes_ctx = mod_notes.context_block(DATA_DIR)
    if notes_ctx:
        extras.append(notes_ctx)
    memory_ctx = mod_memory.context_block(DATA_DIR)
    if memory_ctx:
        extras.append(memory_ctx)
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
