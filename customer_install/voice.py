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

from flask import request, Response, jsonify

import auth
import llm_client
import prompts
import wellbeing
import voicemail as vm
import phone_order
import sms_sender
import caller_history
from modules import business_info as mod_business
from modules import catalog as mod_catalog
from modules import learning_loop as mod_learning
from modules import messages as mod_messages

log = logging.getLogger("orbi.voice")

# Set by register() at startup so the helper functions (which run inside
# Flask request handlers without explicit data_dir args) can reach the
# catalog + learning_loop modules.
_DATA_DIR: Path | None = None

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

_PENDING_LOCK = threading.Lock()
_PENDING: dict[str, dict] = {}


def _render_reply_in_background(sid, config, business, scope, state, user_speech):
    """Run _ai_reply, strip flow markers, apply voicemail offer, and pre-render
    the audio. Result lands in _PENDING[sid] for /voice/wait to pick up."""
    import re as _re_local
    try:
        reply = _ai_reply(config, business, scope, state, user_speech)
        if reply and ("<<SCRAPE:" in reply or "<<NAV:" in reply):
            reply = _re_local.sub(r"\s*<<SCRAPE:\s*.+?\s*>>\s*", " ", reply)
            reply = _re_local.sub(r"\s*<<NAV:\s*.+?\s*>>\s*", " ", reply)
            reply = _re_local.sub(r"\s+", " ", reply).strip()
        log.info(f"[call {sid[:8]}] reply (raw, async): {reply!r}")

        if _should_offer_voicemail(state, user_speech, reply):
            state["voicemail_offered"] = True
            log.info(f"[call {sid[:8]}] offering voicemail (async)")
            reply = (f"{reply} "
                     "Would you like to leave a voicemail for the owner? "
                     "Just say yes or no.")

        # Polly Generative is server-side and instant — no audio pre-render
        # needed. (Kokoro pre-render was here when we owned the TTS pipeline.)

        with _PENDING_LOCK:
            _PENDING[sid] = {"ready": True, "text": reply, "error": None}
    except Exception as e:
        log.exception(f"[call {sid[:8]}] background reply crashed: {e}")
        with _PENDING_LOCK:
            _PENDING[sid] = {"ready": True, "text": None, "error": str(e)}


def _call_state(sid: str, from_phone: str | None = None) -> dict:
    with _CALLS_LOCK:
        state = _CALLS.get(sid)
        if not state:
            state = {
                "history": deque(maxlen=20),
                "started": time.time(),
                "last_touch": time.time(),
                "sid": sid,
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

def _extract_name_from_speech(speech: str) -> str | None:
    """Pull a first name out of a phone-STT-transcribed reply to "Can I
    get your name?". Tolerates STT mis-hears like "Prank" → still treats
    it as a name. Returns None for clearly-not-a-name replies (full
    questions, multi-sentence statements, profanity, etc.)."""
    if not speech:
        return None
    s = speech.strip().rstrip(".,!?")
    low = s.lower()
    # Trim leading conversational filler ("yes, my name is Frank" -> "my name is Frank")
    for filler in ("yes, ", "yeah, ", "yep, ", "sure, ", "ok, ", "okay, ",
                   "uh, ", "um, ", "well, ", "so, ", "hi, ", "hello, ", "hey, "):
        if low.startswith(filler):
            s = s[len(filler):]
            low = s.lower()
            break
    # Trim common preambles
    for prefix in ("my name is ", "i'm ", "i am ", "this is ", "it's ",
                   "name is ", "the name is "):
        if low.startswith(prefix):
            s = s[len(prefix):]
            low = s.lower()
            break
    # Drop trailing "please" / "thank you"
    for suffix in (" please", " thanks", " thank you"):
        if low.endswith(suffix):
            s = s[: -len(suffix)]
            low = s.lower()
    # Reject obvious non-names
    if len(s) > 40 or len(s) < 2:
        return None
    if any(ch in s for ch in "?!"):
        return None  # question, not a name
    # Reject if it's clearly multiple sentences
    if s.count(" ") > 4:
        return None
    # Reject greetings + polite acknowledgements that aren't names
    _NOT_A_NAME = {
        "hi", "hello", "hey", "yo", "howdy", "greetings", "sup",
        "good morning", "good afternoon", "good evening", "good day",
        "morning", "evening", "afternoon",
        "yes", "yeah", "yep", "no", "nope", "ok", "okay", "sure",
        "thanks", "thank you", "ma'am", "sir", "miss",
        "nothing", "nobody", "anyone", "someone",
    }
    if low in _NOT_A_NAME:
        return None
    # Title-case the result so "frank" → "Frank", "PRANK" → "Prank"
    return s.title()


def _esc(text: str) -> str:
    return html.escape(text or "")


def _strip_for_speech(text: str) -> str:
    """Clean LLM output BEFORE handing it to Twilio Polly. Without this,
    Polly reads markdown marks literally ("hashtag hashtag pricing") and
    reads "$129" as "dollar one twenty nine" instead of "one hundred
    twenty-nine dollars". Mirror of stripForSpeech() in static/chat.js."""
    import re as _re
    if not text:
        return ""
    s = str(text)
    # Markdown bold/italic
    s = _re.sub(r"\*\*?", "", s)
    # Line-start bullets
    s = _re.sub(r"(?m)^[-•]\s+", "", s)
    # ALL hashes (## headings + #tags) — Polly reads each as "hashtag"
    s = _re.sub(r"#+", "", s)
    # Inline code backticks
    s = _re.sub(r"`+", "", s)
    # System tier hints
    s = _re.sub(r"—\s*backup mode\s*—", "", s, flags=_re.IGNORECASE)
    s = _re.sub(r"—\s*offline mode\s*—", "", s, flags=_re.IGNORECASE)
    # ── Domain spell-out: twickell.com is an unusual spelling that Kokoro
    # phonemizes as "twico, com" (dropping the -kell). Spell it letter-by-
    # letter with periods + commas so Kokoro pauses between each letter —
    # callers need time to write it down. Hyphens (the previous form)
    # rendered as a fast continuous blur. Run BEFORE the generic URL
    # substitution so the literal hostname gets caught.
    _SPELLED = "T. W. I. C. K. E. L. L."
    s = _re.sub(r"\btwickell\.com/orbi\b", f"{_SPELLED} dot com slash O. R. B. I.", s, flags=_re.IGNORECASE)
    s = _re.sub(r"\btwickell\.com\b", f"{_SPELLED} dot com", s, flags=_re.IGNORECASE)
    s = _re.sub(r"\btwickell\b", _SPELLED, s, flags=_re.IGNORECASE)
    # ── Brand pronunciation fix: "Orbi" → "Or-bee" ──────────────────
    # Twilio Polly Generative pronounces "Orbi" as "Orbeez" (plural) on
    # the phone. The chat widget (different TTS engine) doesn't have this
    # issue. Replacing with a phonetic spelling forces Polly to say it
    # right without needing SSML (which the rest of this pipeline
    # html-escapes anyway). Frank caught this on a live call test.
    # Polly Generative reads "Or-bee" as "or B" (the hyphen makes it speak
    # the letter). Use "Orby" — Polly pronounces -by as "bee" naturally and
    # the spelling is clean for Twilio's logs too.
    s = _re.sub(r"\b[Oo]rbie?\b", "Orby", s)
    s = _re.sub(r"\b[Oo]rbey\b", "Orby", s)
    s = _re.sub(r"\bmyOrbi\b", "my Orby", s, flags=_re.IGNORECASE)
    # ── URL pronunciation fix: spell out acronyms in domains ────────
    # Polly reads "scsplanroom.com" as "XES Plenum dot com". The fix
    # is to insert spaces between the leading consonant cluster of a
    # URL's hostname so Polly reads letters individually. Conservative:
    # only fire when we see a known acronym pattern before "plan",
    # "room", etc. For v1 just convert URLs to spelled-out form.
    def _spell_url(m):
        url = m.group(0)
        # Strip protocol so we don't say "https colon slash slash"
        url = _re.sub(r"^https?://", "", url, flags=_re.IGNORECASE)
        # Replace dots with " dot " and dashes with " dash "
        url = url.replace(".", " dot ").replace("-", " dash ")
        return url
    s = _re.sub(r"https?://[^\s)]+", _spell_url, s)
    # Currency — "$129" → "129 dollars", "$29.99" → "29 dollars and 99 cents"
    def _money_with_cents(m):
        d = int(m.group(1))
        c = int(m.group(2))
        if d == 0:
            return f"{c} cents"
        if c == 0:
            return f"{d} dollars"
        return f"{d} dollars and {c} cents"
    s = _re.sub(r"\$(\d+)\.(\d{2})\b", _money_with_cents, s)
    s = _re.sub(r"\$(\d+)\b", lambda m: f"{m.group(1)} dollars", s)
    # Per-unit slashes — "$29/mo" → "29 dollars per month", etc.
    units = {
        r"\s*/\s*mo\b": " per month",
        r"\s*/\s*month\b": " per month",
        r"\s*/\s*yr\b": " per year",
        r"\s*/\s*year\b": " per year",
        r"\s*/\s*seat\b": " per seat",
        r"\s*/\s*user\b": " per user",
        r"\s*/\s*day\b": " per day",
        r"\s*/\s*hr\b": " per hour",
        r"\s*/\s*hour\b": " per hour",
        r"\s*/\s*wk\b": " per week",
        r"\s*/\s*week\b": " per week",
    }
    for pat, repl in units.items():
        s = _re.sub(pat, repl, s, flags=_re.IGNORECASE)
    # Collapse double-spaces from substitutions
    s = _re.sub(r"\s{2,}", " ", s).strip()
    return s

# ---------------------------------------------------------------------------
# edge_tts audio cache — gives the phone the SAME voice the dashboard uses
# (en-US-AvaNeural) instead of the broadcast-news-anchor Polly default.
# Each rendered MP3 is keyed by a content-hash so identical replies hit cache.
# ---------------------------------------------------------------------------
import hashlib
import os
import tempfile

_AUDIO_CACHE_DIR = Path(tempfile.gettempdir()) / "orby_voice_cache"
_AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_PHONE_VOICE_DEFAULT = "en-US-AvaNeural"
# Twilio's <Play> verb fetches the file from our public tunnel URL. The
# tunnel URL is captured at register() time from CONFIG.server.tunnel_url
_TUNNEL_URL_REF: list[str] = [""]


def _phone_voice() -> str:
    cfg = (_CONFIG_REF[0] or {}) if "_CONFIG_REF" in globals() else {}
    return (cfg.get("phone") or {}).get("voice") or _PHONE_VOICE_DEFAULT


def _render_audio_kokoro(text: str, voice_id: str = "af") -> str | None:
    """Render `text` to MP3 via Kokoro (Apache 2.0, our owned voice engine).
    Returns the cache filename so it can be served by /voice/audio/<file>.
    On failure returns None so the caller can fall back to Polly <Say>.

    This is the v1 cloud phone path — gives the caller the same Orbi voice
    they hear on the dashboard + web chat, with no third-party API cost.
    """
    if not text:
        return None
    key = hashlib.sha1(f"kokoro|{voice_id}|{text}".encode("utf-8")).hexdigest()[:16]
    fname = f"kokoro_{key}.mp3"
    fpath = _AUDIO_CACHE_DIR / fname
    if fpath.exists() and fpath.stat().st_size > 0:
        return fname
    try:
        import kokoro_tts
        if not kokoro_tts.is_available():
            log.warning("kokoro_tts not available — falling through to edge_tts")
            return None
        mp3_bytes = kokoro_tts.render(text, voice=voice_id, format="mp3")
        if not mp3_bytes:
            return None
        tmp_path = fpath.with_suffix(".mp3.tmp")
        with open(tmp_path, "wb") as fh:
            fh.write(mp3_bytes)
        os.replace(tmp_path, fpath)
        return fname
    except Exception as e:
        log.warning(f"kokoro render failed for text={text!r}: {e}")
        return None


def _render_audio(text: str) -> str | None:
    """Render `text` to MP3. Tries Kokoro first (our owned voice, $0 cost).
    Falls back to edge_tts on failure. Returns cache filename or None."""
    if not text:
        return None
    # Try Kokoro first — customer-tenant voice preference if available
    cfg = (_CONFIG_REF[0] or {}) if "_CONFIG_REF" in globals() else {}
    voice_pref = (cfg.get("phone") or {}).get("kokoro_voice") or \
                  (cfg.get("voice") or {}).get("kokoro_id") or "af"
    kokoro_fname = _render_audio_kokoro(text, voice_id=voice_pref)
    if kokoro_fname:
        return kokoro_fname
    # Fall back to edge_tts (legacy path — Microsoft Ava)
    voice = _phone_voice()
    key = hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()[:16]
    fname = f"{key}.mp3"
    fpath = _AUDIO_CACHE_DIR / fname
    if fpath.exists() and fpath.stat().st_size > 0:
        return fname
    try:
        import edge_tts
        import asyncio
        chunks: list[bytes] = []
        async def _collect():
            comm = edge_tts.Communicate(text, voice)
            async for ch in comm.stream():
                if ch.get("type") == "audio":
                    data = ch.get("data") or b""
                    if data:
                        chunks.append(data)
        asyncio.run(_collect())
        if not chunks:
            return None
        # Write atomically so Twilio never fetches a half-written file
        tmp_path = fpath.with_suffix(".mp3.tmp")
        with open(tmp_path, "wb") as fh:
            for c in chunks:
                fh.write(c)
        os.replace(tmp_path, fpath)
        return fname
    except Exception as e:
        log.warning(f"edge_tts render failed for text={text!r}: {e}")
        return None


def _audio_url(fname: str) -> str:
    base = _TUNNEL_URL_REF[0] or ""
    return f"{base}/voice/audio/{fname}"


_PHONE_VOICE = "Polly.Joanna-Generative"


def _say(text: str, voice: str = _PHONE_VOICE) -> str:
    """Speak via Twilio's Polly Generative voice — server-rendered, instant,
    no MP3 fetch round-trip. Swapped in 2026-06-22 after Kokoro on WSL CPU
    was pushing per-turn time to 6-8 seconds (caller-unfriendly). Polly is
    free (rolled into Twilio per-minute call cost) and ships now.

    _strip_for_speech handles pronunciation: 'Orbi' -> 'Or-bee' (Polly was
    saying 'Orbeez' otherwise), 'twickell.com' -> 'T. W. I. C. K. E. L. L.
    dot com', currency spelled out. Those rules still apply with Polly.

    To swap to another TTS later (Cartesia, ElevenLabs, etc.), this single
    function is the seam — the rest of the voice path treats it as a
    black box that returns TwiML."""
    clean_text = _strip_for_speech(text)
    return f'<Say voice="{voice}">{_esc(clean_text)}</Say>'


def _gather(prompt_text: str, action_url: str,
            voice: str = _PHONE_VOICE, timeout: int = 6,
            speech_timeout: str = "auto",
            hints: str = "") -> str:
    """timeout=6s for the caller to START speaking — tightened from 10s
    on 2026-06-21 because the 10s window made "Are you there?" prompts
    fire too often during natural conversational pauses.
    speech_timeout="auto" lets Twilio's recognizer decide when the caller
    is done — safer than a fixed "1" which caused some carriers to flag
    calls for unusual response patterns. Trade-off: slightly more lag,
    but no carrier issues.

    hints: comma-separated menu items, modifiers, and common phrases the
    caller is likely to say (max 500 chars per hint, ~100 hints total).
    Dramatically improves recognition accuracy in restaurant ordering
    context — without hints Twilio routinely mishears 'Truckee Italian'
    as random words. enhanced='true' uses Google's enhanced phone model
    instead of the default."""
    spoken = _say(prompt_text, voice)
    bail = _say("You still there? Take your time.", voice)
    hints_attr = f' hints="{_esc(hints[:1500])}"' if hints else ""
    return f"""
<Response>
  <Gather input="speech" action="{action_url}" method="POST"
          speechTimeout="{speech_timeout}" timeout="{timeout}" language="en-US"
          speechModel="phone_call" enhanced="true"{hints_attr}>
    {spoken}
  </Gather>
  <Gather input="speech" action="{action_url}" method="POST"
          speechTimeout="{speech_timeout}" timeout="{timeout}" language="en-US"
          speechModel="phone_call" enhanced="true"{hints_attr}>
    {bail}
  </Gather>
  <Hangup/>
</Response>""".strip()


def _hangup(text: str | None = None, voice: str = _PHONE_VOICE) -> str:
    parts = [_say(text, voice)] if text else []
    return f"<Response>{''.join(parts)}<Hangup/></Response>"

# ---------------------------------------------------------------------------
# Voice → LLM → speech turn
# ---------------------------------------------------------------------------

_PHONE_SALES_BRIEF = """You are Orbi, the AI sales agent for myOrbi (the company). You're on the PHONE — be CONVERSATIONAL, WARM, and actually answer the caller's question. Don't ask permission to share information; just share it.

PERSONALITY — IMPORTANT:
You're a sharp, friendly, real person — not a corporate FAQ bot. Think: someone in their late-20s/early-30s who actually likes what they do. Warm, easygoing, a little playful when the moment calls for it. Use contractions ("I'll", "we've got", "lemme", "gotcha", "totally"). Drop in natural conversational beats ("yeah", "for sure", "honestly", "good question") where they fit. React to what the caller says — if they ask something cool, sound interested. If they're curious, sound enthusiastic. Sound like an actual person, NOT a script. Never robotic. Never cold. Never lecture-tone.

BANNED OPENERS (these scream "AI bot"):
"Absolutely!", "Certainly!", "Great question!", "Wonderful!", "Excellent!", "Perfect!" — skip them. Just answer warmly.

PRONUNCIATION — IMPORTANT:
The caller's phone STT may transcribe "Orbi" as "Orbee", "Orbeez", "or B", "RV", or other variants. DO NOT correct them ("It's Orbi, not Orbee" — never do that). Just continue normally using "Orbi" yourself. Correcting their pronunciation is rude and breaks the flow.

REPLY LENGTH: 2-4 sentences, longer when the question warrants real depth (pricing breakdown, comparing modules, explaining how a feature works). End with a friendly invite ("anything else?", "what else can I tell you?", "make sense?", "sound good?"). NEVER substitute a permission-ask for the actual answer. If they ask the price, give the price. If they ask features, list features.

BE FORTHCOMING — anticipate what the caller probably wants to know next and offer it without making them ask:
- "Tell me about Orbi" → answer with what she is + her three main jobs (phone, website chat, personal assistant) + a price anchor ("starts at $49.99/mo") + an offer to dig in. Not just the headline.
- "How much?" → give the BASE price AND mention the most common bundle (Base+Receptionist for $129.98/mo) so they have a real number to compare. Not just "starts at $49.99".
- "What can she do?" → give the key capabilities AND mention which module covers which. Connect features to modules so they see how it fits together.
- "How do I sign up?" → tell them the link AND offer to text it AND mention the magic-link experience (minutes to start, no install). Not just "head to twickell.com".
- After any answer, offer the obvious next-step thing they'd want — "Want me to walk you through the bundles?", "Want me to text you the link?", "Want the price for everything together?".

Don't dump a brochure. But always be the one offering info, not waiting to be asked.

WHAT ORBI IS
Orbi is one AI brain that lives across three surfaces for a small business: (1) the business phone — answers 24/7, takes orders, captures leads, books appointments; (2) the website chat widget — answers visitor questions about services and hours, captures leads; (3) a personal assistant — calendar, email drafting, document work, reminders. ONE brain across all three is the differentiator — phone-only tools, chat-only tools, and general chatbots can't match it. (Never name specific competitor companies — refer to categories.)

PRICING (these are the only correct numbers — never invent)
- Orbi Base: $49.99/mo (first seat), $499.90/yr — required for everything.
- Additional seat: +$29.99/mo each (1 brain shared, just more devices on it).
- Receptionist module: +$79.99/mo. 1,000 minutes of calls included. Then $20 per 500-minute block.
- Website Controller module: +$49.99/mo. 20,000 website chats included.
- Restaurant module: +$49.99/mo. Menu lookups, order taking, daily specials.
- Marketing module: +$29.99/mo. Image Generation add-on: +$19.99/mo on top of Marketing.
- Annual = monthly × 10 (pay 10, get 2 free).
- Common bundles: Base+Receptionist = $129.98/mo. Base+Website = $99.98. Base+Receptionist+Website = $179.97. Restaurant full stack (Base+Receptionist+Website+Restaurant) = $229.96.

SEATS / DEVICES
- 1 seat = 1 Orbi brain on 1 main computer + up to 2 linked devices (phone/tablet/etc).
- More humans need their own seat (+$29.99/mo each).
- SEATS MATH: "N seats" = 1 Base ($49.99) + (N − 1) Additional Seats ($29.99 each).
  Example: 3 seats monthly = $49.99 + 2 × $29.99 = $109.97/mo. Multiply by 10 for annual.

PRIVACY
- Customer's business data (chats, leads, contacts) stays on their own computer in the local-install version. Cloud-hosted v1 keeps your data tied to your account, not sold or shared.

HOW TO SIGN UP — WHITE-GLOVE ONBOARDING (founding-member phase)
- Tell them: head to twickell.com, pick what you want, check out with Stripe.
- After they pay, they'll get a "welcome, founding member" email immediately, and then within 24 hours Frank personally emails them with their dashboard sign-in link plus a walkthrough of getting Orbi onto their website and phones.
- Frame it as a premium experience, NOT a delay: "While we're still in founding-member mode, Frank does each onboarding personally so everything's set up right before Orbi goes live."
- If they ask why 24 hours, the honest answer: he checks each setup by hand, makes sure their phone number / website hook-up is solid, and is a real human on the other end if anything's confusing.
- If they say "yes" to "want me to text you the link?", the system fires the SMS — you don't have to do anything else. Confirm aloud after.

SCOPE LIMITS (be honest about these)
- v1 does NOT include healthcare/HIPAA businesses or lawyers as client-facing receptionist (coming in v1.1).
- v1 is cloud-hosted browser access. A local-install v2 is coming for customers who want everything on their own computer.

DELIVERY RULES (phone-specific)
- Sound like a real person — contractions, natural rhythm, light playful tone OK.
- Give the substantive answer up front; don't bury it behind a permission-ask.
- NEVER open with "Great!"/"Awesome!"/"Perfect!" — just answer.
- NEVER ask for the caller's website URL (phone STT mangles URLs and the caller can't type).
- NEVER emit <<SCRAPE:>> or <<NAV:>> markers — those are for the chat widget.
- If you don't know: "Honestly, not sure — let me have someone follow up. What's the best email?"
"""


def _build_voice_prompt(business: dict, scope: dict) -> str:
    """Phone uses a compact ~2-3KB system prompt so the LLM round-trip stays
    in the 1-3s range. The full chat prompt was 22KB and routinely pushed
    Qwen 72B past 10s — too slow for phone callers.

      - myOrbi sales bot: _PHONE_SALES_BRIEF (price list + pitch + scope)
      - Customer business: prompts.build_phone_brief (name, hours, menu,
        capability scope, voice delivery rules)
    Both omit the sales-flow phases, FORBIDDEN BEHAVIORS blocks, and
    multi-paragraph rules that bloat the chat prompt."""
    is_sales = bool(business and (business.get("is_sales_bot")
            or str(business.get("name","")).strip().lower().replace(" ","") == "myorbi"))
    if is_sales:
        return _PHONE_SALES_BRIEF
    return prompts.build_phone_brief(business, scope)
    # Legacy path (kept for reference if we ever need to fall back):
    base = prompts.build_public_prompt(business, scope, channel="phone")
    voice_extras = """

VOICE CALL SPECIFICS
This conversation is happening on the phone. You're the front-of-house person.

THE CALLER CANNOT SEE A SCREEN. They are on the phone, not on the website.
NEVER suggest they "visit the order page," "go to our website," "use the
online ordering form," or anything similar. They picked up the PHONE
specifically to order BY VOICE — your job is to take the order verbally,
right now, in this call. Do NOT redirect them to any website, app, or
self-service tool. Take the order yourself, item by item.

END EVERY TURN WITH A CLEAR HANDOFF (CRITICAL — phone-specific):
- On the phone, the caller can't see anything. After you finish speaking,
  they hear silence — and silence sounds like the call disconnected. You
  MUST end every turn with something that obviously invites them to speak.
- ALWAYS end with a question or invitation: "anything else?", "what else?",
  "go ahead", "your name?", "what time?", "sound good?", "yeah?"
- NEVER end a turn with a flat statement like "Got it." or "Okay." — those
  sound like a period and make the caller think you hung up. If you have
  no follow-up question, add a brief invite: "Got it — anything else?"
- The handoff is the LAST few words of your reply. Make it crisp and
  obviously interrogative.

PERSONALITY — IMPORTANT:
- You're a sharp, fun, late-20s/early-30s personality. Not corporate. Not a
  newscaster. Not "warm but professional." You sound like an actual cool person
  who works at the place.
- Use contractions ("I'll", "we've got", "lemme", "gotcha"), light slang where it
  fits ("for sure", "totally", "no problem"), and natural rhythm. Sound like a
  real human, not a script.
- It's OK to be playful — a little wit, a small joke if the moment calls for it.
  But never sarcastic toward the caller.
- KEEP IT TIGHT. 1-2 sentences per turn. Phone callers want food, not chitchat.

NEVER OPEN A REPLY WITH FILLER:
- Banned openers: "Great!", "Awesome!", "Sure!", "Perfect!", "Wonderful!",
  "Of course!", "Absolutely!", "Excellent!"
- These are robot-tells. Just answer. Example — instead of "Great! What would
  you like?" say "What can I get going for you?" or "Cool, hit me with it."

VARY YOUR PHRASING:
- Never repeat the same exact sentence twice in a single call. If the caller
  asks the same thing twice, rephrase your answer.

CALL FLOW — start polite, get name first, then take the order:
- Your opening was "Good [morning/afternoon/evening], thanks for calling
  [BIZ]. This is Orby. Can I get your name?" — so the caller's NEXT
  utterance is most likely their name. Capture it warmly: "Thanks, [name]
  — what can I get for you today?" Then move on to the order.
- If they SKIP the name and just start ordering ("yeah I want a Truckee
  Italian"), roll with it. Take the order, then loop back at the end:
  "Got it — and can I get a name for the order?"

ORDER FLOW — per-item grouping, NOT line-by-line interrogation:
- The moment the caller says they want to order, you ARE taking the order.
  Do not offer to take a message. Do not hand off to the owner.
- Capture sequence: NAME (from the opening) → ITEMS → CHANGES PER ITEM →
  "anything else?" → PICKUP TIME.
- When the caller names a NEW item, briefly acknowledge it. If you KNOW
  what comes ON that item (the menu description is in your context above —
  the ingredients list), give a quick one-line rundown so the caller knows
  what they're getting, then ask "Any changes?". Example:
    "Got it — Truckee Italian. That's capicola, salami, mortadella,
    provolone, lettuce, tomato, onion. Any changes?"
  If the menu data doesn't have a description for the item, skip the
  rundown and just say "Got it. Any changes to that?"
- DO NOT list modifier categories ("size, bread, anything to add or take
  off") — that's a leading form question that makes the call feel robotic
  and overwhelms the caller with options they didn't ask about. Just ask
  "Any changes?" and let them volunteer what they want different.
- While they're listing changes to the SAME item ("12-inch", "no onions",
  "extra meat", "toasted"), give MINIMAL ack: "got it", "mm-hm", "sure".
  DO NOT re-summarize the whole order after every modifier. DO NOT ask
  "anything else?" mid-item. Wait until the caller pauses for a real beat
  OR explicitly closes the item ("that's it for the sandwich", "okay",
  "yeah that's it for that one", "and...").
- ONLY THEN do a tight summary + "anything else?": "OK so a 12-inch toasted
  Truckee Italian, extra meat, no onions. Anything else for you?"
- If they introduce a new item ("and also a cookie"), go back to the per-item
  pattern: brief ack, ask about changes, shut up.
- If they say "wait", "actually", "hold on", "one more thing", "also", "oh
  and", "I forgot" — they are NOT done. Don't confirm. Don't move on.
- Mid-sentence pauses are normal — if their last utterance ends with "and",
  "uh", "with...", "for..." they're still thinking. Just say "mm-hm" or
  nothing and wait.
- NEVER invent a pickup time. The caller MUST tell you when they want it.
- After you've captured name AND pickup time AND confirmed the full order is
  complete, STATE that you're sending the order confirmation by text (don't
  ask permission — it's the default, like DoorDash/Uber). Say something like:
  "I'll text the confirmation to this number — sound good?" or "Sending the
  order confirmation to this
  number now." The caller usually just confirms ("yes", "great", "thanks"
  or just goes on to say bye). ONLY skip the SMS if the caller affirmatively
  pushes back ("no don't text me", "no SMS please", "no thanks on the text").
  If they say anything other than an explicit "no text", the SMS is sent.

KEEP REPLIES SHORT during order capture — every extra word adds 100-200ms of
audio playback before the caller can speak again. Long replies make the call
feel slow. Save the full order summary for the END.
  Ask explicitly: "What time would you like to pick this up?" or "When would
  you like it ready?" Accept anything they say — "in 15 minutes", "around
  6:30", "as soon as possible", "no rush, an hour", etc.
- NEVER ask the caller to dictate their phone number. You already see it in
  the CALLER CONTEXT section below. If you want to confirm where to send the
  confirmation, READ THE NUMBER ALOUD ("I have 775-528-0574 here — is that
  the best number to text the order confirmation to?") and let them just
  say yes/no. Only if
  they say "use a different number" should you ask them to read off a new one
  (and even then, ask them to say it slowly with pauses between groups of 3-4
  digits so Twilio's transcription catches it).
- For modifications the menu doesn't list (salt/pepper, "extra mayo on the
  side", "cut in half", "no ice"), say "Got it, I'll note that" and treat it
  as a freeform note. Don't try to map it to a checkbox.

OTHER RULES:
- No bullet points, lists, or markdown — you're speaking, not writing.
- If the caller says they're done ("thanks, goodbye, that's all"), say bye and end.
- If they want to leave a message for the owner, confirm their name + number.
- If you don't know something specific about the business, offer to find out
  and call them back — but ONLY for questions, NEVER for ordering.
"""
    return base + voice_extras

def _ai_reply(config: dict, business: dict, scope: dict,
              call_state: dict, user_speech: str) -> str:
    """Phone-side brain. Mirrors /chat in orbi.py — same priority order
    so the caller gets the SAME quality of answer as a website visitor:

      Priority 0:  Learned answers (instant, never-guess)
      Priority 1:  Catalog match (injected into LLM context as authoritative)
      LLM:         Wraps the data in a natural conversational reply
      Post-LLM:    If reply indicates 'I don't know' on a real question,
                   capture into the learning loop and ask for callback.
    """
    # Data dir is in the closure of register() — we don't have it here;
    # the call_state has the from_phone we use to address the asker, and
    # the data dir is referenced via the module-level _DATA_DIR set by
    # register() so this helper can stay synchronous.
    data_dir = _DATA_DIR
    caller_phone = call_state.get("from", "")
    call_sid = call_state.get("sid", "unknown")

    # NOTE: Canned-reply fast-path intentionally removed 2026-06-21.
    # Per Frank: every question must go to the LLM so the SAME architecture
    # works for every business — not just myOrbi sales. The compact phone
    # brief (~705 tokens) keeps the round-trip in the 1-3s range, fast
    # enough that canning isn't needed. Learned answers and order finalize
    # still use the local fast paths below — those aren't canned content,
    # they're captured data from the owner / order pipeline.

    # PRIORITY 0 — learned answers (instant, no LLM call)
    if data_dir:
        learned = mod_learning.find_learned(data_dir, user_speech)
        if learned:
            log.info(f"[call {call_sid[:8]}] learned-answer hit "
                     f"(asked {learned.get('asked_count', 1)}x)")
            call_state["history"].append({"role": "user", "content": user_speech})
            call_state["history"].append({"role": "assistant", "content": learned["answer"]})
            call_state["turns"] += 1
            return learned["answer"]

    # FAST-PATH — short modifier acks during an order (skips LLM entirely).
    # When the caller's input is a short, recognizable modifier ("extra meat",
    # "no onions", "12 inch", "sourdough", "toasted") AND we're mid-order
    # (Orby just asked something like "anything else?"), we don't need the
    # LLM to think — just acknowledge and keep going. Saves ~1.5 sec per
    # turn (LLM call + novel TTS generation), since the ack phrases are
    # already in the audio cache.
    # _fast_path_ack disabled 2026-06-22 per Frank: it was matching "yes"
    # on the sales-bot path ("Yes, how much is she?" -> "Got it, anything else?")
    # which is the canned-reply anti-pattern. Only re-enable inside an active
    # order flow if we ever ship a restaurant-only fast path. LLM handles
    # everything now.

    # WELLBEING — scan caller speech for crisis / distress signals BEFORE
    # routing. People in crisis sometimes call any business they know.
    # Log for the owner's dashboard AND inject crisis context into the
    # system prompt so Orbi handles the moment with care.
    system = _build_voice_prompt(business, scope)
    if data_dir:
        try:
            _wb = wellbeing.check_message(user_speech)
            if _wb["level"] != "ok":
                wellbeing.log_flag(data_dir, _wb["level"], _wb["signal"],
                                    user_speech, source="phone")
                log.warning(f"[call {call_sid[:8]}] wellbeing flag: level={_wb['level']}")
                if _wb["level"] == "crisis":
                    system += "\n\n" + wellbeing.get_crisis_context()
                else:
                    system += "\n\n" + wellbeing.get_distress_context()
        except Exception as e:
            log.warning(f"voice wellbeing check failed: {e}")

    # PRIORITY 1 — catalog hits, injected as authoritative system context
    if data_dir:
        try:
            cat_matches = mod_catalog.search(data_dir, user_speech, limit=5)
            strong = [m for m in cat_matches if m.get("score", 0) >= 10]
            if strong:
                lines = ["PRODUCT CATALOG MATCHES (AUTHORITATIVE — quote name, "
                         "SKU, price, stock EXACTLY. This is a PHONE call so "
                         "name only the TOP one or two, don't list everything):"]
                for m in strong[:3]:
                    bits = [m.get("name", "")]
                    if m.get("sku"):     bits.append(f"part number {m['sku']}")
                    if m.get("price") is not None: bits.append(f"{m['price']:.2f} dollars")
                    if m.get("stock") is not None: bits.append(f"{m['stock']} in stock")
                    lines.append("  - " + " — ".join(b for b in bits if b))
                system += "\n\n" + "\n".join(lines)
                log.info(f"[call {call_sid[:8]}] catalog hit: {len(strong)} strong matches")
        except Exception as e:
            log.warning(f"voice catalog lookup failed: {e}")

    # Caller context — gives Orby the From number so she can reference it
    # naturally ("I have 775-555-0123 — is that the best number?") instead of
    # asking the caller to dictate it (Twilio speech recognition mangles
    # spoken phone numbers badly).
    pretty_phone = _format_us_phone(caller_phone) if caller_phone else "unknown"
    system += (f"\n\nCALLER CONTEXT:\n"
                f"- Calling from: {pretty_phone}\n"
                f"- Use this number for the order confirmation text unless they ask to use a different one.\n"
                f"- When confirming the contact number, READ THIS NUMBER ALOUD — don't ask them to repeat their own number.")

    # If this caller has history with this business, inject what we know.
    # The greeting on /voice/incoming already personalized — this just gives
    # Orby the data to reference naturally during the rest of the call.
    caller_record = call_state.get("caller_record")
    if caller_record:
        system += "\n\n" + caller_history.build_prompt_context_for_caller(caller_record)

    history = list(call_state["history"])
    messages = history[-12:]  # last 6 turns
    messages.append({"role": "user", "content": user_speech})

    # AUTHORITATIVE ORDER TOTALS — same trick we use on /chat. Only run
    # when the caller's LATEST utterance signals end-of-order ("that's it",
    # "how much", "im good", etc.) — not on every mid-order modifier.
    # Mid-order extraction doubled latency for no benefit.
    _end_signals = ("that's it", "thats it", "that's all", "thats all",
                    "i'm good", "im good", "we're done", "nothing else",
                    "no more", "no thanks", "all set", "how much",
                    "what's the total", "im done", "i'm done", "submit")
    _last_lower = (user_speech or "").lower()
    _is_eo = any(s in _last_lower for s in _end_signals)
    try:
        if _is_eo and _looks_like_order(history + [{"role": "user", "content": user_speech}]):
            menu_path = _menu_path_for_profile(business)
            if menu_path:
                menu = phone_order.load_menu(menu_path)
                ext = phone_order.extract_order_from_history(
                    history + [{"role": "user", "content": user_speech}],
                    menu, llm_client, config,
                )
                if ext.get("ok") and ext.get("items"):
                    cart, _w = phone_order.build_cart_for_purblum(ext["items"], menu)
                    phone_order.annotate_cart_with_menu_prices(cart, menu)
                    totals = phone_order.compute_cart_total(cart, business.get("tax_rate", 0))
                    if totals["subtotal"] > 0:
                        tlines = ["⚠️ AUTHORITATIVE ORDER TOTALS — USE THESE EXACT NUMBERS",
                                  "Python computed these from the canonical menu."]
                        for li in totals["lines"]:
                            tlines.append(f"  • {li['qty']}x {li['name']}: ${li['line_total']:.2f}")
                        tlines.append(f"  SUBTOTAL: ${totals['subtotal']:.2f}")
                        if totals['tax_rate_pct']:
                            tlines.append(f"  TAX ({totals['tax_rate_pct']:.2f}%): ${totals['tax']:.2f}")
                            tlines.append(f"  TOTAL: ${totals['total']:.2f}")
                        tlines.append("Quote these EXACT numbers in your end-of-order summary.")
                        system += "\n\n" + "\n".join(tlines)
                        log.info(f"[call {call_sid[:8]}] phone totals injected: "
                                  f"subtotal=${totals['subtotal']} total=${totals['total']}")
    except Exception as e:
        log.warning(f"[call {call_sid[:8]}] phone totals injection failed: {e}")

    # Voice = length cap. The HF tier with max_tokens=4096 streams 85-132
    # tokens (8-13 seconds) which is too long for phone callers. But the
    # original 60-token cap was so tight it cut off mid-sentence on
    # multi-item answers (Frank caught: "...website controller module,
    # dollar." — out of tokens at "dollar"). 200 tokens gives ~150 words
    # of headroom for itemized prices and short recaps without making her
    # sound long-winded.
    voice_brevity = (
        "\n\nABSOLUTE LENGTH RULE FOR THIS TURN:\n"
        "- Reply in 1-2 SHORT sentences. Maximum 35 words / 220 characters.\n"
        "- Phone webhook hard-times-out at 15 seconds. Long replies KILL the call.\n"
        "- For 'tell me about X' questions, give a one-sentence summary then\n"
        "  invite a follow-up ('Want me to go deeper on the pricing or the features?').\n"
        "- When listing prices, finish the full list AND give the total\n"
        "  in plain words ('one hundred seventy-nine dollars and ninety-\n"
        "  seven cents per month'). Do NOT stop mid-list.\n"
        "- End with a brief invitation (question, or 'anything else?').\n"
        "\n"
        "🚨 PHONE-SPECIFIC RULES (PHONE STT MANGLES URLS — NO URL CAPTURE):\n"
        "- NEVER ask the caller for their website address. Phone speech-to-text\n"
        "  cannot hear URLs reliably — 'scsplanroom.com' was heard as\n"
        "  'XES Plenum dot com' and 'f c f dot p l a n r o o m'. Asking just\n"
        "  wastes the caller's time and ours.\n"
        "- NEVER emit <<SCRAPE:...>> or <<NAV:...>> markers on the phone.\n"
        "  The phone path strips them anyway. They're for the chat widget.\n"
        "- If the SALES FLOW prompt tells you to ask for the website, SKIP\n"
        "  that step on the phone. Ask only for business name + city if you\n"
        "  need context. Otherwise jump straight to the pitch from your\n"
        "  general Orbi product knowledge.\n"
        "- For full details (pricing tables, side-by-side comparisons, sign-up\n"
        "  flow), refer callers to the website: 'Head over to twickell.com\n"
        "  slash orbi for the full breakdown — or I can email or text you the\n"
        "  link, just say the word.'\n"
    )
    resp = llm_client.generate(config, system + voice_brevity, messages,
                                 max_tokens=200, channel="phone")
    text = resp.text or "Sorry, my brain is being slow right now. Could I email you the answer instead?"

    # Safety net only — should rarely fire now that the prompt allows
    # 2-4 sentences naturally. 500 chars ~ 80 words = ~30s Polly playback.
    if len(text) > 500:
        cut = text[:500]
        for marker in (". ", "! ", "? "):
            idx = cut.rfind(marker)
            if idx > 200:
                text = cut[: idx + 1].rstrip() + " Anything else?"
                break
        else:
            text = cut.rstrip(",;: ").rstrip() + "… anything else?"

    # Post-LLM: learning-loop trigger when Orbi bluffs on a real question.
    if (data_dir
        and mod_learning.reply_indicates_unknown(text)
        and mod_learning.is_question_form(user_speech)):
        try:
            asker = {"phone": caller_phone, "preferred_channel": "sms"}
            pending = mod_learning.capture_pending(
                data_dir, question=user_speech, asker=asker,
                session_id=f"call:{call_sid}",
            )
            log.info(f"[call {call_sid[:8]}] learning-loop captured: "
                     f"token={pending['token']}")
            # Notify owner (best-effort)
            try:
                import notifications as notify
                notify.send(
                    config, data_dir,
                    event="new_question",
                    title=f"Phone caller asked a question",
                    body=(f"{pending['token']}: {user_speech[:200]}\n\n"
                          f"Caller: {caller_phone}"),
                    url="/owner#learning",
                )
                mod_learning.mark_owner_notified(
                    data_dir, pending["token"], channel="auto",
                )
            except Exception as e:
                log.warning(f"voice owner notify failed: {e}")
            # Override the bluff reply with a callback-promise
            text = ("That's a great question — I'm not sure about that one, "
                    "so I'm going to find out and call you back at this "
                    "number. Anything else I can help with?")
        except Exception as e:
            log.warning(f"voice learning-loop capture failed: {e}")

    call_state["history"].append({"role": "user", "content": user_speech})
    call_state["history"].append({"role": "assistant", "content": text})
    call_state["turns"] += 1
    return text

def _time_of_day_greeting() -> str:
    """Return 'Good morning' / 'Good afternoon' / 'Good evening' based on the
    server's local time. Restaurants prefer this over a generic 'Hi' — it
    sounds more like a real front-of-house person."""
    from datetime import datetime
    hr = datetime.now().hour
    if hr < 12:
        return "Good morning"
    if hr < 17:
        return "Good afternoon"
    return "Good evening"


# Short modifier patterns the caller is likely to say one-at-a-time during
# an order. If their entire utterance matches one of these, we can skip
# the LLM and reply with a cached "Got it." — that drops ~1.5 sec of
# perceived latency per modifier turn.
_FAST_MOD_KEYWORDS = (
    # Sizes
    "12 inch", "12-inch", "12in", "12\"", "twelve inch", "six inch",
    "6 inch", "6-inch", "6in", "6\"", "half", "whole", "full",
    "small", "medium", "large", "extra large", "regular",
    # Bread
    "sourdough", "wheat", "italian roll", "italian", "gluten free",
    "gluten-free", "french roll", "white", "croissant", "white bread",
    "wheat bread", "rye", "honey wheat", "ciabatta", "baguette",
    # Sandwich done-ness
    "toasted", "warm", "cold", "warmed", "toasted please", "not toasted",
    "cold please", "warm it up", "warmed up",
    # Cheese
    "provolone", "swiss", "cheddar", "pepper jack", "no cheese",
    "american", "feta", "mozzarella", "extra cheese please",
    # Add-ons (positive)
    "bacon", "avocado", "jalapeno", "jalapenos", "extra meat",
    "extra cheese", "extra meat please", "extra cheese please",
    "pesto", "extra bacon", "extra turkey", "extra avocado",
    "add bacon", "add avocado", "add cheese", "add mayo", "add mustard",
    "with bacon", "with avocado", "with cheese", "with pesto",
    # Removes (no X / hold the X)
    "no onion", "no onions", "no tomato", "no tomatoes", "no lettuce",
    "no pickle", "no pickles", "no oil", "no oil and vinegar",
    "no oil & vinegar", "no banana peppers", "no peppers", "no mayo",
    "no mustard", "no mayonnaise", "no ketchup", "no dressing",
    "hold the onions", "hold the tomatoes", "hold the mayo",
    "hold the cheese", "hold the lettuce", "without onions",
    "without tomatoes", "without cheese", "without mayo",
    # Drinks
    "sprite", "coke", "diet coke", "coke zero", "water", "bottled water",
    "fountain drink", "iced tea", "lemonade", "coffee", "orange juice",
    "dr pepper", "ginger ale", "root beer", "milk", "apple juice",
    # Sides / extras
    "chips", "cookie", "side salad", "fries", "a cookie",
    # Common short answers during order capture
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "alright",
    "for pickup", "for here", "to go", "pickup", "takeout",
    "that's good", "sounds good", "perfect",
)

_FAST_ACK_REPLIES = (
    "Got it — anything else?",
    "Mm-hm — what else?",
    "Sure — anything else?",
    "No problem — what else can I get for you?",
)


# ---------------------------------------------------------------------------
# Canned sales-bot replies — bypass the LLM entirely for the most common
# phone questions. Each entry has trigger phrases and a 1-2 sentence answer
# crafted for phone delivery (short, ends with an invitation). Audio is
# pre-baked at startup so first-call latency is also tiny.
# ---------------------------------------------------------------------------
_CANNED_SALES_REPLIES: list[tuple[tuple[str, ...], str]] = [
    # "What is Orbi?" / "Tell me about her" — include common STT mishears
    (
        ("what is orbi", "what is or-bee", "what is orbie", "what is orbee", "what's orbi",
         "what's orbee", "tell me about orbi", "tell me about orbee",
         "tell me about her", "tell me about or-bee", "what can you do",
         "what does she do", "what does orbi do", "what does orbee do",
         "what are your capabilities", "capabilities",
         "what's she good at", "what can orbi do", "what can orbee do",
         "interested in orbi", "interested in orbee",
         "i want to know about orbi", "i want to know about orbee",
         "interested in or-bee", "i'd like to know about orbi",
         "i'd like to know about orbee", "what are you"),
        "Orbi is your AI for business and personal life — she answers your business phone, "
        "runs the chat widget on your website, and handles personal things like calendar "
        "and email. Want the pricing, or more about what she does on the phone and website?",
    ),
    # Module-specific pricing — these have to come BEFORE the generic
    # pricing entry so "how much is the receptionist" doesn't fall into
    # the generic Base+Receptionist top-line answer.
    (
        ("receptionist module", "how much is the receptionist",
         "how much is receptionist", "what's the receptionist cost",
         "what does the receptionist cost", "price of the receptionist",
         "how much for the receptionist", "receptionist price",
         "phone receptionist cost", "phone receptionist price"),
        "The Receptionist module is seventy-nine ninety-nine a month, with "
        "one thousand minutes of calls included. After that it's twenty "
        "dollars per five hundred minutes. Anything else you want to know?",
    ),
    (
        ("how much is the website", "how much is the website controller",
         "what does the website controller cost", "website controller cost",
         "website controller price", "price of the website",
         "how much for the website", "website module cost",
         "how much is the chat widget"),
        "The Website Controller module is forty-nine ninety-nine a month, "
        "with twenty thousand chats included. It runs the chat widget on "
        "your site. Want the full bundle math?",
    ),
    (
        ("how much is the restaurant", "restaurant module cost",
         "restaurant module price", "how much for the restaurant"),
        "The Restaurant module is forty-nine ninety-nine a month — handles "
        "menu lookups, order taking, daily specials. Anything else?",
    ),
    (
        ("how much is marketing", "marketing module cost",
         "marketing module price", "how much for marketing"),
        "The Marketing module is twenty-nine ninety-nine a month. Image "
        "generation is a nineteen ninety-nine add-on on top. Anything else?",
    ),
    # Seats / multiple devices / team
    (
        ("more than one computer", "multiple computers", "two computers",
         "how many computers", "multiple devices", "additional seat",
         "extra seat", "extra user", "more than one user", "more than one person",
         "how many seats", "how many people", "team", "for my whole team",
         "for my team", "for everyone", "second computer", "another computer"),
        "One seat means one login plus up to two linked devices — your phone, "
        "a tablet, whatever. Your whole team shares one business memory "
        "folder for services, hours, and customer history, but each user "
        "gets their own private folder for calendar, drafts, and personal "
        "data. Each extra seat is twenty-nine ninety-nine a month. How many "
        "seats do you need?",
    ),
    # Generic pricing — cover the contractions AND the spelled-out forms STT
    # may emit. Module-specific entries above must catch their case first.
    (
        ("how much", "what's the price", "what is the price", "what is your price",
         "tell me the price", "tell me the prices", "what are the prices",
         "what's the cost", "what is the cost", "what's the pricing",
         "what is the pricing", "pricing", "how much does she cost",
         "what does it cost", "what does she cost", "how much does it cost",
         "how much is it", "how much is orbi", "how much is orbee",
         "how much is or-bee", "what's it cost", "what is it cost",
         "price please", "give me the price", "i'd like the price"),
        "Orbi Base is forty-nine ninety-nine a month. The phone receptionist adds seventy-nine "
        "ninety-nine — that's a thousand minutes of calls included. Want me to break down what "
        "fits your business?",
    ),
    # Privacy / data
    (
        ("is it secure", "is it private", "where does my data go", "where's my data",
         "is my data safe", "data privacy", "are you secure",
         "what about privacy"),
        "Your business data — chats, leads, contacts — lives on your computer, not in the cloud. "
        "I'm cloud-hosted on the brain side, but your customer info stays yours. Want more on "
        "how that works?",
    ),
    # Sign-up
    (
        ("how do i sign up", "how do i buy", "how do i get started", "how do i start",
         "how do i get it", "where do i sign up", "how do i purchase"),
        "Easiest way: head to twickell.com, pick what you want, and you'll get a "
        "magic link to start in minutes. Want me to text or email you the link right now?",
    ),
    # What surfaces / channels
    (
        ("phone and website", "phone or website", "all of it", "everything",
         "the whole thing", "all of the above"),
        "Perfect — Base plus the Receptionist plus the Website Controller. What kind of "
        "business are you in? Restaurant, contractor, salon, retail, anything's fine.",
    ),
]


def _canned_sales_reply(user_speech: str) -> str | None:
    """Substring match against pre-written sales-bot answers. Returns the
    canned answer on hit, None otherwise. Case-insensitive."""
    txt = (user_speech or "").strip().lower()
    if not txt or len(txt) > 120:
        return None
    for triggers, reply in _CANNED_SALES_REPLIES:
        for trigger in triggers:
            if trigger in txt:
                return reply
    return None


def _fast_path_ack(user_speech: str, call_state: dict) -> str | None:
    """If the caller's whole utterance is a short modifier (e.g. 'extra meat',
    '12 inch', 'no onions'), return a pre-cached ack like 'Got it.' and
    skip the LLM. Returns None when the LLM should still handle the turn.

    Heuristic guards:
      - input must be short (< 40 chars)
      - must match a known modifier keyword (substring match)
      - we must NOT be in the opening turn (call_state turns > 0) — opening
        utterance needs the LLM to detect order intent
      - the previous assistant message must NOT be a specific question that
        needs a real answer (name, pickup time, yes/no questions)
    """
    txt = (user_speech or "").strip().lower()
    if not txt or len(txt) > 40:
        return None
    if call_state.get("turns", 0) < 1:
        return None  # opening turn — let LLM detect order intent
    # If the LAST assistant message was a specific question that needs a
    # real answer (name, time, yes/no, what), don't fast-path.
    history = list(call_state.get("history", []))
    last_asst = next((m["content"] for m in reversed(history)
                      if m.get("role") == "assistant"), "")
    if last_asst:
        la = last_asst.lower()
        needs_real_answer = any(p in la for p in (
            "your name", "what time", "what's your name", "what is your name",
            "what kind", "what size", "what bread", "would you like",
            "do you want", "is that the best", "best number",
            "anything else for you",  # this opens "anything else" — we want LLM here
            "any changes",  # opens modifier list — we want LLM here too
            "what can i get",
        ))
        if needs_real_answer:
            return None
    # Does this utterance look like a modifier?
    matched = False
    for kw in _FAST_MOD_KEYWORDS:
        if kw in txt or txt in kw:
            matched = True
            break
    if not matched:
        return None
    # Pick the next ack in rotation so it doesn't sound robotic
    idx = call_state.get("fast_ack_idx", 0)
    reply = _FAST_ACK_REPLIES[idx % len(_FAST_ACK_REPLIES)]
    call_state["fast_ack_idx"] = idx + 1
    return reply


def _format_us_phone(raw: str) -> str:
    """Format +17755280574 → 775-528-0574 for natural readback over TTS."""
    if not raw:
        return ""
    import re as _re
    digits = _re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
    return raw


def _detect_goodbye(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in (
        "goodbye", "good bye", "bye now", "see ya", "thanks bye",
        "that's all", "thats all", "thanks for your help",
        "i'm good thanks", "im good thanks", "have a good one"
    ))


# ---------------------------------------------------------------------------
# Voicemail offer / handoff
# ---------------------------------------------------------------------------
# Phrases the caller might say that mean "yes, take a voicemail for me"
_VM_YES = (
    "yes", "yeah", "yep", "sure", "ok", "okay", "please", "go ahead",
    "leave a voicemail", "leave a message", "take a message",
    "leave message", "voicemail",
)
_VM_NO = ("no", "nope", "no thanks", "no thank you", "not now", "nevermind",
          "never mind")


_YES_TOKENS = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "yes please",
    "do it", "go ahead", "send it", "text me", "text it",
    "absolutely", "definitely", "of course", "sounds good",
}


def _is_yes(text: str) -> bool:
    """Light yes detector for confirmation prompts. False on ambiguous input."""
    t = (text or "").strip().lower().rstrip(".!?,")
    if not t:
        return False
    if t in _YES_TOKENS:
        return True
    # Short phrases starting with yes (e.g., "yes please send it")
    if any(t.startswith(p + " ") or t.startswith(p + ",") for p in _YES_TOKENS):
        return True
    return False


def _wants_voicemail(text: str) -> bool:
    """True if the caller said something that means 'yes take a voicemail'."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if any(t == p or t.startswith(p + " ") or t.startswith(p + ",") for p in _VM_NO):
        return False
    return any(p in t for p in _VM_YES)


def _should_offer_voicemail(state: dict, speech: str, reply: str) -> bool:
    """Offer voicemail ONLY when the caller EXPLICITLY asks for the owner
    or to leave a message. Never proactively — Orby is the agent doing the
    work, not a phone tree that punts to a human on every other turn."""
    if state.get("voicemail_offered"):
        return False
    low_speech = (speech or "").lower()
    explicit_asks = (
        "talk to the owner", "speak to the owner",
        "talk to a human", "speak to a human", "real person",
        "call me back", "have him call", "have her call",
        "leave a message", "leave a voicemail", "take a message",
        "is the owner there", "is the manager there", "manager please",
    )
    return any(p in low_speech for p in explicit_asks)

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

def _resolve_business_slug(to_number: str) -> str | None:
    """Return the business slug (e.g. 'purblum_com') the inbound number
    maps to, or None if unmapped. Used both for profile loading AND for
    locating per-caller history under that business."""
    mapping = (_CONFIG_REF[0] or {}).get("customer_profile_by_phone") or {}
    if not mapping or not to_number:
        return None
    candidates = {to_number,
                   to_number.replace(" ", "").replace("-", "").replace("(", "").replace(")", ""),
                   to_number.lstrip("+")}
    for k in mapping:
        normalized_k = k.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if normalized_k in candidates or normalized_k.lstrip("+") in candidates:
            return mapping[k]
    return None


def _load_profile_for_inbound_call(to_number: str) -> dict:
    """Multi-tenant phone routing — same idea as the web-side one. If
    the Twilio number this call came IN to is mapped to a customer
    profile in config.customer_profile_by_phone, load that profile.
    Otherwise fall back to the default business_info.json."""
    slug = _resolve_business_slug(to_number)
    if not slug:
        return mod_business.load(_DATA_DIR)
    profile_path = _DATA_DIR / "customer_profiles" / f"{slug}.json"
    if not profile_path.exists():
        log.warning(f"phone profile {slug!r} mapped but file missing — falling back")
        return mod_business.load(_DATA_DIR)
    try:
        import json as _json
        return _json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"phone profile load failed for {slug}: {e}")
        return mod_business.load(_DATA_DIR)


# Stash a reference to CONFIG so the helper above can read mapping after register() exits
_CONFIG_REF: list[dict] = [None]


# ---------------------------------------------------------------------------
# Order finalization — when the call ends after an order, run the pipeline:
#   transcript → LLM extract → cart → web_driver submit → SMS receipt
# ---------------------------------------------------------------------------

_ORDER_HINTS = (
    "order", "i'll have", "i will have", "i'd like", "can i get",
    "lemme get", "let me get", "give me", "i want", "pickup",
    "pick up", "to go", "for here",
)


def _build_speech_hints_for_business(business_profile: dict) -> str:
    """Return a comma-separated list of menu items + modifier labels to
    pass as `hints` to Twilio's <Gather>. Hugely improves recognition
    accuracy for restaurant ordering. Caps at ~1500 chars (Twilio's limit
    per attribute is generous but we want to stay safe)."""
    menu_path = _menu_path_for_profile(business_profile)
    if not menu_path:
        return ""
    try:
        menu = phone_order.load_menu(menu_path)
    except Exception:
        return ""
    import re as _re
    out: set[str] = set()
    for cat in menu.get("categories", []) or []:
        for item in cat.get("items", []) or []:
            if item.get("name"):
                out.add(item["name"])
            for grp in item.get("modifier_groups", []) or []:
                for opt in grp.get("options", []) or []:
                    if opt.get("label"):
                        # Strip parenthesized variants — speak the bare label
                        out.add(_re.sub(r"\s*\(.*?\)", "", opt["label"]).strip())
                for d in grp.get("defaults_on", []) or []:
                    if d:
                        out.add(d)
    # Pickup time / generic ordering vocabulary that's NOT in the menu
    out.update([
        "pickup", "for here", "to go", "extra", "no", "side",
        "small", "medium", "large", "twelve inch", "six inch",
        "half", "whole", "in fifteen minutes", "in twenty minutes",
        "in half an hour", "around six thirty", "around seven",
        "yes", "no thanks", "that's it", "anything else",
    ])
    hints = ",".join(sorted(out))
    return hints[:1500]


def _looks_like_order(history: list[dict]) -> bool:
    """Cheap heuristic: did the caller actually try to order something?
    Avoids running the extraction LLM on pure Q&A calls."""
    if not history:
        return False
    caller_text = " ".join(m.get("content", "").lower()
                           for m in history if m.get("role") == "user")
    return any(h in caller_text for h in _ORDER_HINTS)


def _menu_path_for_profile(business_profile: dict) -> str | None:
    """Find the menu file for a given business profile. Looks at
    profile.menu_source first; falls back to a hardcoded path for purblum."""
    src = (business_profile or {}).get("menu_source")
    if src:
        return src
    name = (business_profile or {}).get("name", "").lower()
    if "purblum" in name:
        return "/home/frank/purblum_live/data/menu.json"
    return None


def _submitter_for_profile(business_profile: dict):
    """Returns an order-submission callable for this business. Routes
    through the generic dispatch layer so non-PurBlum restaurants with
    a configured `order_submission.kitchen_email` can use the email
    backend without needing a per-restaurant Playwright driver.

    Falls back to the original PurBlum-specific submitter for backward
    compatibility with the existing call signature (cart, customer)."""
    name = (business_profile or {}).get("name", "").lower()
    cfg = (business_profile or {}).get("order_submission") or {}
    has_email_path = bool(cfg.get("kitchen_email"))
    has_web_driver = ("purblum" in name)

    if not (has_email_path or has_web_driver):
        return None

    try:
        from web_driver import dispatch_order
        import phone_order as _po
    except Exception as e:
        log.warning(f"web_driver import failed: {e}")
        return None

    # Return a callable matching the existing signature voice.py expects:
    # submit(cart=..., customer=..., **kwargs) → result dict.
    def _submit(cart, customer, notes="", headless=True,
                 record_video=False, data_dir=None):
        tax_rate = (business_profile or {}).get("tax_rate", 0)
        totals = _po.compute_cart_total(cart, tax_rate)
        # Slug stamp so dispatch_order can route web_driver path
        try:
            from site_scraper.storage import domain_from_url
            host = (business_profile.get("contact") or {}).get("website") or ""
            business_profile["_slug"] = (
                domain_from_url(host) if host
                else ("purblum_com" if "purblum" in name else "")
            )
        except Exception:
            business_profile["_slug"] = "purblum_com" if "purblum" in name else ""
        # Stamp owner user_dir so email_dispatch can fall back to the
        # owner's connected SMTP credentials (Settings → Email Accounts)
        # if ORBI_SMTP_* env vars aren't set.
        try:
            from pathlib import Path as _Path
            import users as _users_mod
            from flask import current_app
            data_dir = _Path(current_app.config.get("DATA_DIR",
                "/home/frank/orbi_web/customer_install/data"))
            # Use the install's primary owner. Single-owner installs
            # have exactly one matching user folder.
            owner_users = _users_mod.list_users(data_dir,
                                                  include_archived=False)
            owner = next((u for u in owner_users
                          if (u.get("role") or "").lower() == "owner"), None)
            if owner:
                business_profile["_owner_user_dir"] = str(
                    _users_mod.get_user_dir(data_dir, owner["username"]))
        except Exception:
            pass  # non-fatal — falls back to env-var SMTP if available
        return dispatch_order(cart, customer, totals, business_profile)

    return _submit


def _build_confirmation_speech(result: dict, customer: dict, cart: list[dict]) -> str:
    """Verbal readback when the order lands successfully — kept short for
    the phone."""
    name = customer.get("name") or "you"
    pickup = customer.get("pickup_time") or "when you get here"
    if not result.get("ok"):
        return (f"I hit a snag submitting that, {name} — someone'll call you back "
                "in a minute to confirm. Thanks for calling PurBlum.")
    # Don't speak the order ID — it's an alphanumeric hash that sounds awful
    # over Polly TTS ("zero a seven f zero eight five d").
    # SMS receipt is gated on 10DLC registration, so don't promise a text
    # we can't deliver. Once 10DLC is approved, swap the closing line back to
    # mention the SMS receipt.
    return (f"You're all set, {name}. We'll have it ready {pickup}. "
            "Thanks for calling PurBlum!")


def _build_sms_receipt(result: dict, customer: dict, cart: list[dict],
                         business_name: str) -> str:
    """SMS body. Plain text — Twilio SMS doesn't render anything fancy."""
    lines = [f"{business_name} — order confirmation"]
    if result.get("order_id"):
        lines.append(result["order_id"])
    lines.append("")  # blank line
    summary = phone_order.render_order_summary(cart, customer)
    lines.append(summary)
    if result.get("total"):
        total_clean = " | ".join(s.strip() for s in result["total"].splitlines() if s.strip())
        lines.append("")
        lines.append(total_clean)
    lines.append("")
    lines.append(f"Pickup: {customer.get('pickup_time','—')}")
    lines.append(f"Thanks for ordering with {business_name}.")
    return "\n".join(lines)


def _finalize_order_in_background(sid: str, history: list[dict],
                                     business_profile: dict,
                                     caller_phone: str,
                                     config: dict) -> None:
    """Run the extract → cart → submit → SMS pipeline. Writes result to
    _CALLS[sid]['finalize_result'] for /voice/finalize to read."""
    state = _CALLS.get(sid)
    if state is None:
        return

    def _set(payload):
        state["finalize_result"] = payload

    try:
        menu_path = _menu_path_for_profile(business_profile)
        submitter = _submitter_for_profile(business_profile)
        if not menu_path or not submitter:
            log.warning(f"[call {sid[:8]}] no menu/submitter for "
                          f"profile={business_profile.get('name','?')}")
            _set({"stage": "no_driver", "ok": False})
            return

        menu = phone_order.load_menu(menu_path)
        extracted = phone_order.extract_order_from_history(
            list(history), menu, llm_client, config,
        )
        if not extracted.get("ok"):
            log.warning(f"[call {sid[:8]}] extract failed: {extracted}")
            _set({"stage": "extract_failed", "ok": False, **extracted})
            return

        items = extracted.get("items") or []
        customer = extracted.get("customer") or {}
        if not items:
            log.info(f"[call {sid[:8]}] no items in extracted order — skipping submit")
            _set({"stage": "no_items", "ok": False,
                  "items": [], "customer": customer})
            return

        cart, warnings = phone_order.build_cart_for_purblum(items, menu)
        if not cart:
            log.warning(f"[call {sid[:8]}] cart build empty (warnings={warnings})")
            _set({"stage": "empty_cart", "ok": False, "warnings": warnings})
            return

        # Submission target — use the caller's phone as the contact
        customer_for_submit = {
            "name": customer.get("name") or "Phone Customer",
            "phone": caller_phone or "",
            "pickup_time": customer.get("pickup_time") or "",
        }
        log.info(f"[call {sid[:8]}] submitting cart: {[c['name'] for c in cart]}")
        result = submitter(
            cart=cart, customer=customer_for_submit,
            notes="Phone order submitted by Orby",
            headless=True, record_video=False,
            data_dir=_DATA_DIR,
        )
        log.info(f"[call {sid[:8]}] submission result: ok={result.get('ok')} "
                  f"order_id={result.get('order_id','?')}")

        # SMS receipt to the caller — gated on:
        #   (a) submission succeeded, AND
        #   (b) we have the caller's phone, AND
        #   (c) config.phone.sms_enabled is true (flipped after 10DLC approval), AND
        #   (d) the caller explicitly said YES when Orby asked
        #       "Would you like a text receipt?" (captured by extract as
        #       extracted["sms_receipt_consent"])
        sms_status = None
        sms_enabled = bool((config.get("phone") or {}).get("sms_enabled", False))
        caller_consented = bool(extracted.get("sms_receipt_consent"))
        if result.get("ok") and caller_phone and sms_enabled and caller_consented:
            biz_name = business_profile.get("name", "your order")
            body = _build_sms_receipt(result, customer_for_submit, cart, biz_name)
            sms_status = sms_sender.send(config, caller_phone, body)
            log.info(f"[call {sid[:8]}] sms receipt: {sms_status}")
        elif result.get("ok") and caller_phone:
            reason = []
            if not sms_enabled: reason.append("sms_disabled (10DLC pending)")
            if not caller_consented: reason.append("caller did not consent to SMS receipt")
            log.info(f"[call {sid[:8]}] skipping SMS: {', '.join(reason)}")

        # Persist the caller's history so the NEXT time they call we can
        # greet them by name + recall what they ordered.
        if result.get("ok"):
            try:
                business_slug = (state.get("business_slug")
                                  or (business_profile or {}).get("_slug")
                                  or "")
                if business_slug and caller_phone:
                    summary = phone_order.render_order_summary(cart, customer_for_submit)
                    caller_history.upsert_order(
                        _DATA_DIR, business_slug,
                        caller_phone=caller_phone,
                        caller_name=customer_for_submit.get("name", ""),
                        order_id=result.get("order_id", ""),
                        cart=cart,
                        pickup_time=customer_for_submit.get("pickup_time", ""),
                        total=result.get("total", ""),
                        summary=summary,
                    )
                    log.info(f"[call {sid[:8]}] caller history updated for "
                              f"{caller_phone} at {business_slug}")
            except Exception as e:
                log.warning(f"caller_history.upsert failed: {e}")

        _set({
            "stage": "done",
            "ok": result.get("ok", False),
            "submit_result": result,
            "cart": cart,
            "customer": customer_for_submit,
            "warnings": warnings,
            "sms_status": sms_status,
        })
    except Exception as e:
        log.exception(f"[call {sid[:8]}] finalize crashed: {e}")
        _set({"stage": "crashed", "ok": False, "error": str(e)})


def _say_then_pause_then_redirect(text: str, pause_seconds: int,
                                       redirect_url: str,
                                       voice: str = "Polly.Joanna-Neural") -> str:
    """TwiML that speaks `text`, waits, then Twilio POSTs to redirect_url.
    Used to bridge the gap while the order-submission thread runs in the
    background."""
    spoken = _say(text, voice)
    return f"""
<Response>
  {spoken}
  <Pause length="{int(pause_seconds)}"/>
  <Redirect method="POST">{redirect_url}</Redirect>
</Response>""".strip()


def register(app, CONFIG: dict, DATA_DIR: Path) -> None:
    """Call from orbi.py:  voice.register(app, CONFIG, DATA_DIR)"""
    global _DATA_DIR
    _DATA_DIR = DATA_DIR
    _CONFIG_REF[0] = CONFIG
    _TUNNEL_URL_REF[0] = (CONFIG.get("server") or {}).get("tunnel_url") or ""

    # Pre-bake common short replies into the audio cache in a background
    # thread so the first call after restart isn't slow. Each phrase takes
    # ~1s to generate but only ONCE per startup; after that, cache hits
    # serve instantly. Big perceived-latency win during a quick order flow.
    # Resolve whatever profile +16812529085 currently routes to (now myOrbi
    # default, since customer_profile_by_phone is empty and the per-slug
    # PurBlum profile is absent) and pre-bake greetings using THAT business
    # name. If the routing changes later, warmups still help — the slow bit
    # is the time-of-day prefix, and any partial cache hit is a win.
    try:
        _default_profile = _load_profile_for_inbound_call("+16812529085")
        _biz = _default_profile.get("name", "myOrbi") or "myOrbi"
    except Exception:
        _biz = "myOrbi"
    _WARMUP_PHRASES = [
        # Fast-path acks — must end with a clear handoff phrase
        "Got it — anything else?",
        "Mm-hm — what else?",
        "Sure — anything else?",
        "No problem — what else can I get for you?",
        # Other common cached phrases
        "Anything else?", "Anything else for you?",
        "Any changes to that?", "Got it, what else?",
        "What time would you like to pick this up?",
        "Cool, hold on a sec while I send this to the kitchen.",
        "You still there? Take your time.",
        "Sorry, the line cut out for a sec — could you say that again?",
        "Sorry, I didn't catch that. Could you say it again?",
        "Just one more second...",
        # New restaurant-style greetings (one per time of day × first/return)
        f"Good morning, thanks for calling {_biz}. This is Orby. Can I get your name?",
        f"Good afternoon, thanks for calling {_biz}. This is Orby. Can I get your name?",
        f"Good evening, thanks for calling {_biz}. This is Orby. Can I get your name?",
        # Universal failure message — pre-bake so brain-down replies still play fast.
        "Sorry, my brain is being slow right now. Could I email you the answer instead?",
    ]
    def _warmup():
        try:
            import kokoro_tts
            kokoro_tts.render("Warming up.", voice="af", format="mp3")
            log.info("voice: kokoro model warm")
        except Exception as e:
            log.warning(f"voice: kokoro warmup failed: {e}")
        for phrase in _WARMUP_PHRASES:
            try:
                _render_audio(phrase)
            except Exception as e:
                log.debug(f"warmup phrase failed for {phrase!r}: {e}")
        log.info(f"voice: audio cache pre-baked ({len(_WARMUP_PHRASES)} phrases)")
    threading.Thread(target=_warmup, daemon=True).start()

    @app.route("/voice/audio/<fname>", methods=["GET"])
    def voice_audio(fname):
        """Serve a cached MP3 to Twilio's <Play> verb.
        Tight allowlist: filename is either
          - 16 hex chars + .mp3 (legacy edge_tts cache), or
          - kokoro_ + 16 hex chars + .mp3 (cloud v1 Kokoro cache)
        Prevents path traversal."""
        import re
        if not (
            re.fullmatch(r"[0-9a-f]{16}\.mp3", fname or "")
            or re.fullmatch(r"kokoro_[0-9a-f]{16}\.mp3", fname or "")
        ):
            return Response("not found", status=404)
        fpath = _AUDIO_CACHE_DIR / fname
        if not fpath.exists():
            return Response("not found", status=404)
        return Response(fpath.read_bytes(), mimetype="audio/mpeg",
                         headers={"Cache-Control": "public, max-age=3600"})

    @app.route("/voice/incoming", methods=["POST"])
    def voice_incoming():
        sid = request.form.get("CallSid", "unknown")
        from_phone = request.form.get("From", "")
        to_number = request.form.get("To", "")
        log.info(f"[call {sid[:8]}] incoming from {from_phone} to {to_number}")

        # Multi-tenant: pick the business profile based on which Twilio
        # number the call came in to.
        business = _load_profile_for_inbound_call(to_number)
        biz_slug = _resolve_business_slug(to_number)
        biz_name = business.get("name") or CONFIG.get("business", {}).get("name", "this business")
        state = _call_state(sid, from_phone)
        state["business_profile"] = business
        state["business_slug"] = biz_slug
        state["to_number"] = to_number

        # Per-caller recognition — if this phone number has called this
        # business before, look up their history. Customizes greeting +
        # gives Orby context she can reference naturally.
        caller_record = None
        if biz_slug and from_phone:
            caller_record = caller_history.load(DATA_DIR, biz_slug, from_phone)
            if caller_record:
                log.info(f"[call {sid[:8]}] returning caller — "
                          f"name={caller_record.get('remembered_name','?')!r} "
                          f"prior_calls={caller_record.get('call_count', 0)}")
        state["caller_record"] = caller_record

        # Pre-compute the menu-specific speech hints once per call and stash
        # them on state — every subsequent /voice/gather reuses them so
        # Twilio's recognizer is primed for menu items + modifier vocabulary.
        state["speech_hints"] = _build_speech_hints_for_business(business)

        # Greeting: personalized for returning callers, default for new ones.
        # New callers get a polite time-of-day greeting + immediate name ask
        # (standard front-of-house pattern). Returning callers already known
        # by name, so we skip the ask.
        tod = _time_of_day_greeting()
        if caller_record:
            personalized = caller_history.build_greeting_for_returning_caller(biz_name, caller_record)
            greeting = personalized or f"{tod}! This is Orby at {biz_name}. What can I get for you?"
        else:
            greeting = (f"{tod}, thanks for calling {biz_name}. This is Orby. "
                        "Can I get your name?")
        twiml = _gather(greeting, action_url="/voice/gather",
                          hints=state["speech_hints"])
        return Response(twiml, mimetype="application/xml")

    @app.route("/voice/gather", methods=["POST"])
    def voice_gather():
        sid = request.form.get("CallSid", "unknown")
        from_phone = request.form.get("From", "")
        to_number = request.form.get("To", "")
        speech = (request.form.get("SpeechResult") or "").strip()
        confidence = float(request.form.get("Confidence", "0") or 0)

        state = _call_state(sid, from_phone)

        if not speech:
            twiml = _gather("Sorry, I didn't catch that. Could you say it again?",
                            action_url="/voice/gather",
                            hints=state.get("speech_hints", ""))
            return Response(twiml, mimetype="application/xml")

        log.info(f"[call {sid[:8]}] heard: {speech!r} (conf={confidence:.2f})")

        # FAST-PATH for the very first turn: Orby just asked the caller's
        # name, so a multi-second LLM call to reply "Hi <name>" is wasteful
        # AND too slow — phone callers hung up before the reply came back.
        # Template the response so it lands in <500ms.
        if state.get("turns", 0) == 0 and not state.get("caller_record"):
            from_name = _extract_name_from_speech(speech)
            if from_name:
                state["caller_name"] = from_name
                state.setdefault("history", []).append(
                    {"role": "user", "content": speech})
                state["turns"] = 1
                greeting = f"Hi {from_name}, what can I get for you today?"
                state["history"].append(
                    {"role": "assistant", "content": greeting})
                log.info(f"[call {sid[:8]}] reply (fast-path name greeting): "
                          f"{greeting!r}")
                twiml = _gather(greeting, action_url="/voice/gather",
                                  hints=state.get("speech_hints", ""))
                return Response(twiml, mimetype="application/xml")

        # Low-confidence transcription = background noise or muffled speech.
        # Don't send garbage to the LLM (it'll hallucinate a response based on
        # noise-words) and don't trigger voicemail offers off of bogus replies.
        # After 2 low-conf turns in a row, fall through so we don't loop forever.
        if confidence < 0.40:
            low_conf_streak = state.get("low_conf_streak", 0) + 1
            state["low_conf_streak"] = low_conf_streak
            if low_conf_streak < 2:
                log.info(f"[call {sid[:8]}] low-confidence (conf={confidence:.2f}) — re-prompting")
                twiml = _gather("Sorry, the line cut out for a sec — could you say that again?",
                                action_url="/voice/gather",
                                hints=state.get("speech_hints", ""))
                return Response(twiml, mimetype="application/xml")
            # 2nd garbled turn: continue down to LLM but reset the streak
            log.info(f"[call {sid[:8]}] low-confidence twice in a row — letting LLM try")
        state["low_conf_streak"] = 0

        # If we previously offered voicemail and they replied — branch.
        if state.get("voicemail_offered") and not state.get("voicemail_started"):
            if _wants_voicemail(speech):
                state["voicemail_started"] = True
                log.info(f"[call {sid[:8]}] caller accepted voicemail")
                twiml = vm.record_voicemail_twiml(
                    callback_url="/voice/voicemail_recording_callback"
                )
                return Response(twiml, mimetype="application/xml")
            # They said no (or anything else) — keep talking
            state["voicemail_offered"] = False  # let it be re-offered later

        # Reuse the per-call business profile if we stashed it on /voice/incoming;
        # otherwise re-resolve from the To-number (covers Twilio retries that
        # might land in /voice/gather directly without a fresh incoming).
        business = state.get("business_profile") or _load_profile_for_inbound_call(to_number)
        scope = CONFIG.get("scope", {}) or {}

        # Goodbye must be detected synchronously (turns the call's tail into
        # an order finalize or a clean hangup); we still need a reply for the
        # hangup farewell, so render that inline.
        if _detect_goodbye(speech) or state["turns"] >= 15:
            import re as _re
            reply = _ai_reply(CONFIG, business, scope, state, speech)
            if reply and ("<<SCRAPE:" in reply or "<<NAV:" in reply):
                reply = _re.sub(r"\s*<<SCRAPE:\s*.+?\s*>>\s*", " ", reply)
                reply = _re.sub(r"\s*<<NAV:\s*.+?\s*>>\s*", " ", reply)
                reply = _re.sub(r"\s+", " ", reply).strip()
            log.info(f"[call {sid[:8]}] reply (raw, goodbye-path): {reply!r}")
            # If this conversation looks like an order, kick off the full
            # finalize pipeline (extract → cart → web_driver submit → SMS).
            # Bridges the caller with "Hold on a sec..." TwiML while the
            # background thread runs, then /voice/finalize reads the result.
            if _looks_like_order(list(state.get("history", []))):
                # Idempotency: if a finalize thread is already running or has
                # already completed, just return the bridge TwiML — don't
                # re-submit. Protects against Twilio webhook retries.
                if state.get("finalize_started"):
                    log.info(f"[call {sid[:8]}] finalize already started — "
                              "bridging to existing flow")
                    bridge = _say_then_pause_then_redirect(
                        "Almost done, one sec...",
                        pause_seconds=6,
                        redirect_url=f"/voice/finalize/{sid}",
                    )
                    return Response(bridge, mimetype="application/xml")
                state["finalize_started"] = True
                log.info(f"[call {sid[:8]}] caller said goodbye on an order — "
                          "kicking off finalize pipeline")
                threading.Thread(
                    target=_finalize_order_in_background,
                    args=(sid, list(state.get("history", [])),
                            state.get("business_profile") or {},
                            state.get("from", ""),
                            CONFIG),
                    daemon=True,
                ).start()
                bridge = _say_then_pause_then_redirect(
                    "Cool, hold on a sec while I send this to the kitchen.",
                    pause_seconds=10,
                    redirect_url=f"/voice/finalize/{sid}",
                )
                return Response(bridge, mimetype="application/xml")
            # Non-order conversation — just hang up cleanly
            _save_call_summary(DATA_DIR, state)
            with _CALLS_LOCK:
                _CALLS.pop(sid, None)
            return Response(_hangup(reply), mimetype="application/xml")

        # ── SMS-the-link fast-path ──────────────────────────────────────
        # If Orbi just offered to text the signup link AND the caller said
        # yes, actually fire the SMS. Frank caught the LLM saying "I'll text
        # the link" without us ever sending one. State-tracked so we only
        # send once per call.
        is_sales = bool(business and (business.get("is_sales_bot")
                or str(business.get("name","")).strip().lower().replace(" ","") == "myorbi"))
        if is_sales and not state.get("link_sms_sent"):
            history = list(state.get("history", []))
            last_asst = next((m["content"] for m in reversed(history)
                              if m.get("role") == "assistant"), "")
            offered_link = bool(last_asst) and any(p in last_asst.lower() for p in (
                "text or email you the link",
                "text you the link",
                "send you the link",
                "want me to text",
            ))
            if offered_link and _is_yes(speech):
                tunnel = (_TUNNEL_URL_REF[0] or "").rstrip("/") or "https://twickell.com/orbi"
                link = "https://twickell.com"
                body = (f"Hi! It's Orbi from myOrbi. Here's the link to "
                        f"pick your plan: {link} — after checkout you'll get a "
                        f"welcome email, then Frank personally emails your "
                        f"sign-in + setup walkthrough within 24 hours.")
                sms_status = sms_sender.send(CONFIG, from_phone, body)
                if sms_status.get("ok"):
                    state["link_sms_sent"] = True
                    log.info(f"[call {sid[:8]}] signup link SMS sent to {from_phone}")
                    reply = ("Sent! Check your phone — link's there. After you "
                             "check out, Frank emails you personally within 24 "
                             "hours to walk you through setup. Anything else?")
                else:
                    log.warning(f"[call {sid[:8]}] SMS failed: {sms_status.get('error')}")
                    reply = ("Hmm, the text didn't go through. You can head to "
                             "twickell.com yourself when you're "
                             "ready. Anything else?")
                state.setdefault("history", []).append(
                    {"role": "user", "content": speech})
                state["history"].append({"role": "assistant", "content": reply})
                state["turns"] = state.get("turns", 0) + 1
                twiml = _gather(reply, action_url="/voice/gather",
                                hints=state.get("speech_hints", ""))
                return Response(twiml, mimetype="application/xml")

        # NOTE: Canned-reply fast-paths intentionally removed 2026-06-21.
        # Frank's principle: every customer question must go to the LLM
        # so the same architecture works for every business — not just
        # myOrbi sales. The compact phone brief (~2.8KB) keeps the LLM
        # round-trip in the 1-3s range, fast enough without canning.

        # ── Dispatch background renderer ────────────────────────────────
        # LLM + Kokoro render runs in a thread. We BLOCK here for up to
        # ~7s waiting for it — that covers the common case (sub-1s LLM via
        # Scaleway + 3-6s Kokoro on CPU). If ready in time, we return the
        # answer directly with NO filler, NO redirect. The filler dance
        # only kicks in for the slow tail (>7s).
        with _PENDING_LOCK:
            _PENDING[sid] = {"ready": False, "started_at": time.time()}
        threading.Thread(
            target=_render_reply_in_background,
            args=(sid, CONFIG, business, scope, state, speech),
            daemon=True,
        ).start()

        deadline = time.time() + 7.0
        while time.time() < deadline:
            p = _PENDING.get(sid)
            if p and p.get("ready"):
                with _PENDING_LOCK:
                    _PENDING.pop(sid, None)
                if p.get("text") and not p.get("error"):
                    log.info(f"[call {sid[:8]}] fast-path (no filler)")
                    twiml = _gather(p["text"], action_url="/voice/gather",
                                    hints=state.get("speech_hints", ""))
                    return Response(twiml, mimetype="application/xml")
                break  # error case — fall through to filler path
            time.sleep(0.1)

        # Still rendering after 7s — play filler + redirect to /voice/wait
        ack_fname = _render_audio("Just one more second...")
        if ack_fname:
            ack_play = f'<Play>{_audio_url(ack_fname)}</Play>'
        else:
            ack_play = '<Say voice="Polly.Joanna">Just one moment.</Say>'
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            f'{ack_play}'
            f'<Redirect>/voice/wait?sid={sid}&amp;try=1</Redirect>'
            '</Response>'
        )
        return Response(twiml, mimetype="application/xml")

    @app.route("/voice/wait", methods=["GET", "POST"])
    def voice_wait():
        """Polled by Twilio while the background thread renders the reply.
        Each call here is its OWN webhook (its own 15s budget), so a slow
        Kokoro render no longer kills the call."""
        sid = request.args.get("sid") or request.form.get("sid", "")
        try:
            try_num = int(request.args.get("try") or request.form.get("try") or "1")
        except ValueError:
            try_num = 1

        pending = _PENDING.get(sid)
        state = _call_state(sid)

        if pending and pending.get("ready"):
            with _PENDING_LOCK:
                _PENDING.pop(sid, None)
            if pending.get("error") or not pending.get("text"):
                log.warning(f"[call {sid[:8]}] async render failed: {pending.get('error')}")
                twiml = _gather(
                    "Sorry, I had a hiccup. What was that again?",
                    action_url="/voice/gather",
                    hints=state.get("speech_hints", ""),
                )
                return Response(twiml, mimetype="application/xml")
            twiml = _gather(
                pending["text"],
                action_url="/voice/gather",
                hints=state.get("speech_hints", ""),
            )
            return Response(twiml, mimetype="application/xml")

        # Not ready. Cap retries so we never loop forever.
        if try_num >= 12:
            log.warning(f"[call {sid[:8]}] async render timed out after {try_num} tries")
            with _PENDING_LOCK:
                _PENDING.pop(sid, None)
            return Response(
                _hangup("Sorry, I'm running slow right now. Please try me again in a moment."),
                mimetype="application/xml",
            )
        # One filler already played in /voice/gather. Don't stack more — Frank
        # heard 4x "Just one more second" per turn in 2026-06-21 testing and
        # rightly called it out. Silent pause + redirect until ready.
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            '<Pause length="2"/>'
            f'<Redirect>/voice/wait?sid={sid}&amp;try={try_num + 1}</Redirect>'
            '</Response>'
        )
        return Response(twiml, mimetype="application/xml")

    @app.route("/voice/finalize/<sid>", methods=["POST"])
    def voice_finalize(sid):
        """Twilio redirects here after the bridging pause. The background
        thread should have written state['finalize_result'] by now. If not,
        we play another short bridge and redirect back to ourselves."""
        state = _CALLS.get(sid)
        if not state:
            log.warning(f"[finalize {sid[:8]}] no call state — giving up")
            return Response(
                _hangup("Sorry, something glitched on our end. Please call back."),
                mimetype="application/xml")
        result = state.get("finalize_result")
        retries = state.get("finalize_retries", 0)
        if not result:
            # Still working — give it one more shot
            if retries >= 2:
                log.warning(f"[finalize {sid[:8]}] background thread didn't finish in time")
                _save_call_summary(DATA_DIR, state)
                _CALLS.pop(sid, None)
                return Response(
                    _hangup("I'm still putting that in for you — someone'll call "
                             "you back to confirm in just a minute. Thanks!"),
                    mimetype="application/xml")
            state["finalize_retries"] = retries + 1
            bridge = _say_then_pause_then_redirect(
                "Just one more second...",
                pause_seconds=6,
                redirect_url=f"/voice/finalize/{sid}",
            )
            return Response(bridge, mimetype="application/xml")
        # We have a result — speak the confirmation and hang up
        submit_res = result.get("submit_result") or {}
        cart = result.get("cart") or []
        customer = result.get("customer") or {}
        if not cart:
            # Heuristic said order but extract/build came up empty —
            # don't claim we submitted anything that we didn't.
            log.info(f"[finalize {sid[:8]}] no cart built (stage={result.get('stage')}) "
                      "— closing as a thank-you")
            text = "Got it. Thanks for calling PurBlum — have a good one."
        else:
            text = _build_confirmation_speech(submit_res, customer, cart)
        _save_call_summary(DATA_DIR, state)
        _CALLS.pop(sid, None)
        return Response(_hangup(text), mimetype="application/xml")

    @app.route("/voice/voicemail_recording_callback", methods=["POST"])
    def voice_voicemail_recording_callback():
        """Twilio posts here when the <Record> verb finishes."""
        sid = request.form.get("CallSid", "unknown")
        from_phone = request.form.get("From", "")
        rec_url = request.form.get("RecordingUrl", "")
        duration = int(request.form.get("RecordingDuration", "0") or 0)
        log.info(f"[call {sid[:8]}] voicemail recording: "
                 f"url={rec_url} dur={duration}s from={from_phone}")

        # Optional Twilio signature check (skip in dev if no token)
        sig = request.headers.get("X-Twilio-Signature", "")
        if sig:
            full_url = request.url
            form = {k: request.form.get(k, "") for k in request.form.keys()}
            ok = vm.verify_twilio_signature(CONFIG, full_url, form, sig)
            if not ok:
                log.warning(f"[call {sid[:8]}] twilio signature mismatch "
                            "(continuing anyway — set strict mode to reject)")

        # Process in background so Twilio's callback returns fast.
        def _bg():
            try:
                vm.process_recording(
                    audio_url=rec_url,
                    caller_number=from_phone,
                    data_dir=DATA_DIR,
                    config=CONFIG,
                    duration_seconds=duration,
                )
            except Exception as e:
                log.exception(f"voicemail processing failed: {e}")
        threading.Thread(target=_bg, daemon=True).start()

        # Clean up the in-memory call state
        with _CALLS_LOCK:
            _CALLS.pop(sid, None)
        return Response("<Response/>", mimetype="application/xml")

    # ---- Owner dashboard JSON endpoints -----------------------------------
    orbi_dir = DATA_DIR.parent  # auth.require_owner expects ORBI_DIR

    @app.route("/api/owner/voicemails", methods=["GET"])
    def api_owner_voicemails():
        auth.require_owner(orbi_dir)
        return jsonify({"voicemails": vm.list_voicemails(DATA_DIR, limit=50)})

    @app.route("/api/owner/voicemails/<vm_id>", methods=["GET"])
    def api_owner_voicemail_get(vm_id):
        auth.require_owner(orbi_dir)
        rec = vm.get_voicemail(DATA_DIR, vm_id)
        if not rec:
            return jsonify({"error": "not_found"}), 404
        return jsonify(rec)

    @app.route("/api/owner/voicemails/<vm_id>/handled", methods=["POST"])
    def api_owner_voicemail_handled(vm_id):
        auth.require_owner(orbi_dir)
        ok = vm.mark_handled(DATA_DIR, vm_id)
        return jsonify({"status": "ok" if ok else "not_found"}), (200 if ok else 404)

    @app.route("/api/owner/voicemails/<vm_id>", methods=["DELETE"])
    def api_owner_voicemail_delete(vm_id):
        auth.require_owner(orbi_dir)
        ok = vm.delete(DATA_DIR, vm_id)
        return jsonify({"status": "ok" if ok else "not_found"}), (200 if ok else 404)

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

    log.info("Voice endpoints registered: /voice/incoming /voice/gather "
             "/voice/wait /voice/audio /voice/status /voice/voicemail_recording_callback "
             "/api/owner/voicemails (GET/DELETE) "
             "/api/owner/voicemails/<id>/handled (POST)")

    # Periodic cleanup of stale call state (in case status callback missed)
    def _cleanup_loop():
        while True:
            time.sleep(300)  # every 5 minutes
            try:
                _prune_calls()
            except Exception:
                pass
    threading.Thread(target=_cleanup_loop, daemon=True).start()
