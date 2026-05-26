"""
Twilio Voice integration for Orbi.

Simple pattern: <Gather input="speech"> → Twilio speech-to-text → we respond
with <Say> using the same brain. No streaming, no media server, just a clean
turn-taking conversation.

Each call gets a `CallSid`. We track per-call conversation history in memory
(cleared after 1 hour of inactivity). When the call ends, we persist a summary
to messages.json as a 'voicemail' so the owner can see it in the dashboard.

Endpoints attached to the Flask app:
  POST /voice/incoming   — Twilio webhook when a call rings
  POST /voice/gather     — Twilio webhook after each spoken turn
  POST /voice/status     — Twilio webhook on call status changes (ended, etc.)
"""

from __future__ import annotations

import html
import logging
import threading
import time
from collections import deque
from pathlib import Path

from flask import request, Response

import llm_client
import prompts
from modules import business_info as mod_business
from modules import messages as mod_messages

log = logging.getLogger("orbi.voice")

# ---------------------------------------------------------------------------
# In-memory call state
# ---------------------------------------------------------------------------
# Maps CallSid → {"history": deque, "started": ts, "from": phone, "turns": int}
_CALLS: dict[str, dict] = {}
_CALLS_LOCK = threading.Lock()
_CALL_TTL_SECONDS = 60 * 60  # forget calls inactive for 1 hour

def _prune_calls():
    cutoff = time.time() - _CALL_TTL_SECONDS
    with _CALLS_LOCK:
        for sid in list(_CALLS.keys()):
            if _CALLS[sid].get("last_touch", 0) < cutoff:
                _CALLS.pop(sid, None)

def _call_state(sid: str, from_phone: str | None = None) -> dict:
    with _CALLS_LOCK:
        state = _CALLS.get(sid)
        if not state:
            state = {
                "history": deque(maxlen=20),
                "started": time.time(),
                "last_touch": time.time(),
                "from": from_phone,
                "turns": 0,
            }
            _CALLS[sid] = state
        else:
            state["last_touch"] = time.time()
            if from_phone and not state.get("from"):
                state["from"] = from_phone
        return state

# ---------------------------------------------------------------------------
# TwiML helpers
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    return html.escape(text or "")

def _say(text: str, voice: str = "Polly.Joanna") -> str:
    return f'<Say voice="{voice}">{_esc(text)}</Say>'

def _gather(prompt_text: str, action_url: str,
            voice: str = "Polly.Joanna", timeout: int = 5) -> str:
    return f"""
<Response>
  <Gather input="speech" action="{action_url}" method="POST"
          speechTimeout="auto" timeout="{timeout}" language="en-US">
    {_say(prompt_text, voice)}
  </Gather>
  {_say("I didn't catch that. Goodbye.", voice)}
  <Hangup/>
</Response>""".strip()

def _hangup(text: str | None = None, voice: str = "Polly.Joanna") -> str:
    parts = [_say(text, voice)] if text else []
    return f"<Response>{''.join(parts)}<Hangup/></Response>"

# ---------------------------------------------------------------------------
# Voice → LLM → speech turn
# ---------------------------------------------------------------------------

def _build_voice_prompt(business: dict, scope: dict) -> str:
    """Same as the public chat prompt but with voice-specific guardrails."""
    base = prompts.build_public_prompt(business, scope)
    voice_extras = """

VOICE CALL SPECIFICS
This conversation is happening on the phone. Adapt:
- Keep replies to 1-2 sentences. Phone callers can't read long text.
- Avoid bullet points, lists, or markdown. Just speak naturally.
- If the caller seems to be done ("thanks, goodbye, that's all"), say goodbye and end.
- If the caller wants to leave a message for the owner, confirm their name and number and tell them you'll pass it along.
- If you don't know something, offer to take a message instead of guessing.
"""
    return base + voice_extras

def _ai_reply(config: dict, business: dict, scope: dict,
              call_state: dict, user_speech: str) -> str:
    system = _build_voice_prompt(business, scope)
    history = list(call_state["history"])
    messages = history[-12:]  # last 6 turns
    messages.append({"role": "user", "content": user_speech})

    resp = llm_client.generate(config, system, messages)
    text = resp.text or "I'm sorry, I'm having trouble right now. Please call back in a moment."

    call_state["history"].append({"role": "user", "content": user_speech})
    call_state["history"].append({"role": "assistant", "content": text})
    call_state["turns"] += 1
    return text

def _detect_goodbye(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in (
        "goodbye", "good bye", "bye now", "see ya", "thanks bye",
        "that's all", "thats all", "thanks for your help",
        "i'm good thanks", "im good thanks", "have a good one"
    ))

# ---------------------------------------------------------------------------
# Save voicemail summary at end of call
# ---------------------------------------------------------------------------

def _save_call_summary(data_dir: Path, call_state: dict) -> None:
    history = list(call_state["history"])
    if not history:
        return
    # Build a brief transcript
    lines = []
    for msg in history:
        role = "Caller" if msg["role"] == "user" else "Orbi"
        lines.append(f"{role}: {msg['content']}")
    transcript = "\n".join(lines)

    duration = int(time.time() - call_state.get("started", time.time()))

    # Classify whether it was a real lead or just a question
    user_turns = [m["content"] for m in history if m["role"] == "user"]
    combined = " ".join(user_turns).lower()
    msg_type = "question"
    if any(kw in combined for kw in (
        "call back", "leave a message", "name is", "phone is", "schedule",
        "appointment", "book", "order", "quote", "estimate"
    )):
        msg_type = "voicemail"

    try:
        mod_messages.capture(
            data_dir,
            msg_type=msg_type,
            from_name=None,
            from_phone=call_state.get("from"),
            from_email=None,
            body=transcript,
            source="phone",
            meta={"duration_seconds": duration, "turns": call_state.get("turns", 0)},
        )
    except Exception as e:
        log.warning(f"voicemail save failed: {e}")

# ---------------------------------------------------------------------------
# Flask endpoint registration
# ---------------------------------------------------------------------------

def register(app, CONFIG: dict, DATA_DIR: Path) -> None:
    """Call from orbi.py:  voice.register(app, CONFIG, DATA_DIR)"""

    @app.route("/voice/incoming", methods=["POST"])
    def voice_incoming():
        sid = request.form.get("CallSid", "unknown")
        from_phone = request.form.get("From", "")
        log.info(f"[call {sid[:8]}] incoming from {from_phone}")

        business = mod_business.load(DATA_DIR)
        biz_name = business.get("name") or CONFIG.get("business", {}).get("name", "this business")
        state = _call_state(sid, from_phone)

        greeting = f"Hi, thanks for calling {biz_name}. This is Orbi. How can I help you today?"
        twiml = _gather(greeting, action_url="/voice/gather")
        return Response(twiml, mimetype="application/xml")

    @app.route("/voice/gather", methods=["POST"])
    def voice_gather():
        sid = request.form.get("CallSid", "unknown")
        from_phone = request.form.get("From", "")
        speech = (request.form.get("SpeechResult") or "").strip()
        confidence = float(request.form.get("Confidence", "0") or 0)

        state = _call_state(sid, from_phone)

        if not speech:
            twiml = _gather("Sorry, I didn't catch that. Could you say it again?",
                            action_url="/voice/gather")
            return Response(twiml, mimetype="application/xml")

        log.info(f"[call {sid[:8]}] heard: {speech!r} (conf={confidence:.2f})")

        business = mod_business.load(DATA_DIR)
        scope = CONFIG.get("scope", {}) or {}

        reply = _ai_reply(CONFIG, business, scope, state, speech)
        log.info(f"[call {sid[:8]}] reply: {reply!r}")

        # End the call if the caller said goodbye
        if _detect_goodbye(speech) or state["turns"] >= 15:
            _save_call_summary(DATA_DIR, state)
            with _CALLS_LOCK:
                _CALLS.pop(sid, None)
            return Response(_hangup(reply), mimetype="application/xml")

        twiml = _gather(reply, action_url="/voice/gather")
        return Response(twiml, mimetype="application/xml")

    @app.route("/voice/status", methods=["POST"])
    def voice_status():
        sid = request.form.get("CallSid", "unknown")
        status = request.form.get("CallStatus", "")
        log.info(f"[call {sid[:8]}] status={status}")
        if status in ("completed", "failed", "busy", "no-answer", "canceled"):
            with _CALLS_LOCK:
                state = _CALLS.pop(sid, None)
            if state:
                _save_call_summary(DATA_DIR, state)
        return Response("<Response/>", mimetype="application/xml")

    log.info("Voice endpoints registered: /voice/incoming /voice/gather /voice/status")

    # Periodic cleanup of stale call state (in case status callback missed)
    def _cleanup_loop():
        while True:
            time.sleep(300)  # every 5 minutes
            try:
                _prune_calls()
            except Exception:
                pass
    threading.Thread(target=_cleanup_loop, daemon=True).start()
