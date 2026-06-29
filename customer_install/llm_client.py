"""
Orbi LLM client — three-tier failover.

Tier 1: Frank's brain (HTTPS, 8B-13B model)
Tier 2: HuggingFace Inference (Llama-3.3-70B)
Tier 3: Local Llama-3.2-3B (offline fallback)

Each tier has a timeout and falls through to the next on failure.
Tracks which tier answered so the UI can show "backup mode" / "offline mode".
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
import socket

log = logging.getLogger("orbi.llm")

class LLMResponse:
    __slots__ = ("text", "tier", "latency_ms", "error")
    def __init__(self, text: str, tier: str, latency_ms: int, error: str | None = None):
        self.text = text
        self.tier = tier  # "brain" | "huggingface" | "local" | "none"
        self.latency_ms = latency_ms
        self.error = error
    def __bool__(self) -> bool:
        return bool(self.text) and self.error is None


# ---------------------------------------------------------------------------
# Tier 1: Frank's brain
# ---------------------------------------------------------------------------

def call_brain(config: dict, system: str, messages: list[dict]) -> LLMResponse:
    cfg = (config or {}).get("brain")
    if not cfg or not cfg.get("url") or not cfg.get("api_key"):
        # Brain not configured for this caller — skip without crashing.
        return LLMResponse("", "brain", 0, "brain_not_configured")
    url = cfg["url"].rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": "llama-3.1-8b-instruct",
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": 0.25,
        # 2048 covers ~1500 words — enough for marketing campaigns,
        # multi-section letters, blog drafts. 512 was cutting long
        # responses off mid-list.
        "max_tokens": _GEN_OVERRIDES.get("max_tokens") or 4096,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
        "X-Orbi-Client": "customer-install",
        # Brain sits behind Cloudflare which 403s default python-urllib UA
        # ("Python-urllib/3.10") via Bot Fight Mode / Browser Integrity
        # Check. Send a real-browser UA so the request gets through.
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    timeout = cfg.get("timeout_seconds", 60)
    # Retry once on transient failure — Render's free tier sleeps the brain
    # container after 15 min of inactivity, and the first request takes
    # 30-50s to wake it (often exceeding even a generous timeout). Without
    # a retry, a cold-start scrape extracts nothing because every per-page
    # brain call fails. After the first request lands, subsequent calls
    # are <2s warm. One retry covers the wake-up case without blowing up
    # response time on a healthy brain.
    resp = _http_chat(url, payload, headers, timeout, tier="brain")
    if resp:
        return resp
    # Don't retry on permanent config errors — only on transient network
    # failures consistent with Render cold-start.
    if resp.error in ("disabled", "brain_not_configured"):
        return resp
    import time as _time
    _time.sleep(5)
    return _http_chat(url, payload, headers, timeout, tier="brain")


# ---------------------------------------------------------------------------
# Phone tier — HF Inference Providers with a specific fast provider (Groq).
# Same HF wallet as Tier 2 but routed through Groq's LPU hardware for
# sub-second Llama 3.3 70B responses. Used by voice.py only.
# ---------------------------------------------------------------------------

def call_phone_llm(config: dict, system: str, messages: list[dict]) -> LLMResponse:
    """Phone tier: call HF Inference auto-router directly, bypassing the
    brain (Render free-tier container that cold-starts in 30-50s). Live
    probe 2026-06-21 showed Qwen 72B returning in 862ms via the auto-router
    while the brain hop was timing out at 15s. Same model as chat, just
    fewer hops in the chain."""
    cfg = (config or {}).get("phone_llm") or {}
    if not cfg.get("enabled"):
        return LLMResponse("", "phone_llm", 0, "disabled")
    hf_cfg = (config or {}).get("huggingface") or {}
    api_key = hf_cfg.get("api_key")
    if not api_key:
        return LLMResponse("", "phone_llm", 0, "no_hf_key")
    # Provider-pinned path (Scaleway + Llama 3.3 70B) when configured —
    # the auto-router was landing on Novita which is slow (4-5s) and 40%
    # more expensive per request than Scaleway. Pinning to Scaleway gives
    # 1-second responses, same HF wallet. Falls back to auto-router if
    # provider isn't set.
    provider = cfg.get("provider")
    model = cfg.get("model") or hf_cfg.get("model", "Qwen/Qwen2.5-72B-Instruct")
    timeout = cfg.get("timeout_seconds", 10)
    if provider:
        url = f"https://router.huggingface.co/{provider}/v1/chat/completions"
    else:
        url = "https://router.huggingface.co/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": 0.25,
        "max_tokens": _GEN_OVERRIDES.get("max_tokens") or 4096,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    return _http_chat(url, payload, headers, timeout, tier="phone_llm")


# ---------------------------------------------------------------------------
# Tier 2: HuggingFace
# ---------------------------------------------------------------------------

def call_huggingface(config: dict, system: str, messages: list[dict]) -> LLMResponse:
    """Try router first (best models), fall back to direct api-inference
    (works on free/older accounts)."""
    cfg = config.get("huggingface") or {}
    if not cfg.get("enabled") or not cfg.get("api_key"):
        return LLMResponse("", "huggingface", 0, "disabled")
    model = cfg.get("model", "meta-llama/Llama-3.1-8B-Instruct")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
        # Real browser UA — HF Router (behind Cloudflare) 403s on python-urllib UA
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    timeout = _GEN_OVERRIDES.get("timeout_seconds") or cfg.get("timeout_seconds", 15)

    # Attempt 1: featherless-ai (Qwen 2.5 72B dropped off all HF providers 2026-06-29;
    # featherless-ai serves Llama 3.3 70B, ~3-4s, cost unlisted but HF-billed).
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": 0.25,
        "max_tokens": _GEN_OVERRIDES.get("max_tokens") or 4096,
        "stream": False,
    }
    resp = _http_chat("https://router.huggingface.co/featherless-ai/v1/chat/completions",
                      payload, headers, timeout, tier="huggingface")
    if resp:
        return resp

    # Attempt 2: auto-router fallback (provider selection varies by availability)
    resp = _http_chat("https://router.huggingface.co/v1/chat/completions",
                      payload, headers, timeout, tier="huggingface")
    return resp


# ---------------------------------------------------------------------------
# Anthropic Claude — used exclusively for the legal module
# ---------------------------------------------------------------------------

def call_anthropic(config: dict, system: str, messages: list[dict]) -> LLMResponse:
    """Direct call to Anthropic Messages API. Used for legal document drafting,
    research memos, and contract analysis where quality > cost."""
    cfg = (config or {}).get("anthropic") or {}
    if not cfg.get("enabled") or not cfg.get("api_key"):
        return LLMResponse("", "anthropic", 0, "disabled")
    api_key = cfg["api_key"]
    model   = cfg.get("model", "claude-sonnet-4-6")
    timeout = _GEN_OVERRIDES.get("timeout_seconds") or cfg.get("timeout_seconds", 90)
    max_tok = _GEN_OVERRIDES.get("max_tokens") or cfg.get("max_tokens", 4096)

    payload = {
        "model":      model,
        "max_tokens": max_tok,
        "system":     system,
        "messages":   messages,
    }
    headers = {
        "Content-Type":    "application/json",
        "x-api-key":       api_key,
        "anthropic-version": "2023-06-01",
    }
    start = time.time()
    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data, headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        latency = int((time.time() - start) * 1000)
        obj  = json.loads(body)
        text = ""
        for block in obj.get("content") or []:
            if block.get("type") == "text":
                text += block.get("text", "")
        text = text.strip()
        if not text:
            return LLMResponse("", "anthropic", latency, "empty_content")
        log.info(f"tier=anthropic ok latency={latency}ms model={model}")
        return LLMResponse(text, "anthropic", latency)
    except (urllib.error.HTTPError, urllib.error.URLError,
            socket.timeout, ConnectionError, OSError) as e:
        latency = int((time.time() - start) * 1000)
        log.warning(f"tier=anthropic failed latency={latency}ms err={type(e).__name__}: {e}")
        return LLMResponse("", "anthropic", latency, f"{type(e).__name__}: {e}")
    except Exception as e:
        latency = int((time.time() - start) * 1000)
        log.exception("tier=anthropic unexpected error")
        return LLMResponse("", "anthropic", latency, f"unexpected: {e}")


def generate_legal(config: dict, system: str, messages: list[dict],
                   max_tokens: int | None = None,
                   timeout_seconds: int | None = None) -> LLMResponse:
    """LLM call for legal module work — goes straight to Anthropic Claude.
    Falls back to HuggingFace if Anthropic is not configured."""
    _GEN_OVERRIDES["max_tokens"] = max_tokens
    _GEN_OVERRIDES["timeout_seconds"] = timeout_seconds or 90
    resp = call_anthropic(config, system, messages)
    if resp:
        return resp
    log.warning("legal: Anthropic unavailable — falling back to HuggingFace")
    resp = call_huggingface(config, system, messages)
    if resp:
        return resp
    return LLMResponse("", "none", 0, "all_legal_tiers_failed")


# ---------------------------------------------------------------------------
# Tier 3: Local Llama-3.2-3B
# ---------------------------------------------------------------------------

def call_local(config: dict, system: str, messages: list[dict]) -> LLMResponse:
    cfg = config.get("local_llm") or {}
    if not cfg.get("enabled"):
        return LLMResponse("", "local", 0, "disabled")
    port = cfg.get("port", 11435)
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    payload = {
        "model": "llama-3.2-3b-instruct",
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": 0.25,
        # Local 3B is slow — keep its cap lower (~750 words) so
        # offline-mode fallback doesn't hang the browser for 2 min.
        "max_tokens": _GEN_OVERRIDES.get("max_tokens") or 1024,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    # Generous timeout — 3B on CPU with longer prompts can take 60s
    timeout = int(cfg.get("timeout_seconds", 90))
    return _http_chat(url, payload, headers, timeout, tier="local")


# ---------------------------------------------------------------------------
# Shared HTTP chat helper
# ---------------------------------------------------------------------------

def _http_chat(url: str, payload: dict, headers: dict,
               timeout: int, tier: str) -> LLMResponse:
    start = time.time()
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        latency = int((time.time() - start) * 1000)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError as e:
            return LLMResponse("", tier, latency, f"invalid_json: {e}")
        # OpenAI-format response
        choices = obj.get("choices") or []
        if not choices:
            return LLMResponse("", tier, latency, "no_choices")
        text = (choices[0].get("message") or {}).get("content", "").strip()
        if not text:
            return LLMResponse("", tier, latency, "empty_content")
        log.info(f"tier={tier} ok latency={latency}ms tokens~={len(text)//4}")
        return LLMResponse(text, tier, latency)
    except (urllib.error.HTTPError, urllib.error.URLError,
            socket.timeout, ConnectionError, OSError) as e:
        latency = int((time.time() - start) * 1000)
        log.warning(f"tier={tier} failed latency={latency}ms err={type(e).__name__}: {e}")
        return LLMResponse("", tier, latency, f"{type(e).__name__}: {e}")
    except Exception as e:
        latency = int((time.time() - start) * 1000)
        log.exception(f"tier={tier} unexpected error")
        return LLMResponse("", tier, latency, f"unexpected: {e}")


# ---------------------------------------------------------------------------
# Tier orchestrator
# ---------------------------------------------------------------------------

# Circuit breaker — once brain has failed N times in a row, skip it for the
# next COOLDOWN seconds. Brain DNS lookup costs ~50-100ms even when it fails;
# avoiding the dead tier saves real latency on every call.
_TIER_FAIL_STREAK: dict[str, int] = {"brain": 0, "huggingface": 0, "phone_llm": 0}
_TIER_SKIP_UNTIL: dict[str, float] = {"brain": 0.0, "huggingface": 0.0, "phone_llm": 0.0}
# Per-call overrides set by generate() before dispatching to a tier.
# Cleared after the call completes. Used to thread max_tokens through to
# the tier-specific call functions without adding it to every signature.
_GEN_OVERRIDES: dict = {}
_FAIL_THRESHOLD = 3            # trip after this many consecutive failures
_SKIP_COOLDOWN_SECONDS = 300   # then skip the tier for 5 min before retry


def generate(config: dict, system: str, messages: list[dict],
              max_tokens: int | None = None,
              channel: str = "chat",
              timeout_seconds: int | None = None) -> LLMResponse:
    """
    Try each tier in order, return the first successful response.
    Returns an empty LLMResponse with tier='none' if all tiers fail.

    Circuit breaker: a tier that has failed 3+ times in a row gets skipped
    for the next 5 minutes. Massive latency win when brain DNS is dead —
    we don't pay the 50-100ms DNS lookup on every call anymore.

    max_tokens (optional): cap the response length. Used by voice to
    force <=60-token replies so phone callers don't sit through 12-second
    LLM streams.

    channel: "chat" or "phone" — both can use the phone_llm tier
    (Scaleway-pinned, sub-second) FIRST, then fall back through
    brain → HF → local. The phone_llm config's `skip_brain` flag
    drops the brain hop entirely when set, eliminating the Render
    cold-start (and brain → HF passthrough latency).

    Frank's 2026-06-22 chat tests showed the brain tier intermittently
    failing or timing out under load — the chat would flip into
    "— offline mode —" mid-conversation. Routing chat through phone_llm
    too gives consistent sub-second responses for both channels.
    """
    now = time.time()
    # Stash on a thread-local so the tier callers can read it without
    # threading the param through every signature.
    _GEN_OVERRIDES["max_tokens"] = max_tokens
    _GEN_OVERRIDES["timeout_seconds"] = timeout_seconds
    tiers = []
    skip_brain = False
    phone_cfg = (config or {}).get("phone_llm") or {}
    # Use phone_llm as the FIRST tier for both phone and chat — it's the
    # fastest path (Scaleway-pinned Llama 3.3 70B, ~1s). Channels that
    # don't want it can pass an unknown channel name to bypass.
    if channel in ("phone", "chat") and phone_cfg.get("enabled"):
        tiers.append((call_phone_llm, "phone_llm"))
        skip_brain = bool(phone_cfg.get("skip_brain"))
    if not skip_brain:
        tiers.append((call_brain, "brain"))
    tiers.extend([
        (call_huggingface, "huggingface"),
        (call_local,       "local"),
    ])
    for tier_fn, tier_name in tiers:
        # Skip tiers we've tripped the circuit-breaker on
        if _TIER_SKIP_UNTIL.get(tier_name, 0) > now:
            continue
        try:
            resp = tier_fn(config, system, messages)
            if resp:
                # Reset streak on success
                _TIER_FAIL_STREAK[tier_name] = 0
                return resp
            # Failed but didn't crash — bump the streak
            _TIER_FAIL_STREAK[tier_name] = _TIER_FAIL_STREAK.get(tier_name, 0) + 1
            if _TIER_FAIL_STREAK[tier_name] >= _FAIL_THRESHOLD:
                _TIER_SKIP_UNTIL[tier_name] = now + _SKIP_COOLDOWN_SECONDS
                log.warning(f"tier={tier_name} tripped circuit breaker — "
                              f"skipping for {_SKIP_COOLDOWN_SECONDS}s "
                              f"(after {_FAIL_THRESHOLD} consecutive failures)")
        except Exception as e:
            log.exception(f"tier {tier_name} crashed")
            _TIER_FAIL_STREAK[tier_name] = _TIER_FAIL_STREAK.get(tier_name, 0) + 1
            if _TIER_FAIL_STREAK[tier_name] >= _FAIL_THRESHOLD:
                _TIER_SKIP_UNTIL[tier_name] = now + _SKIP_COOLDOWN_SECONDS
    return LLMResponse("", "none", 0, "all_tiers_failed")


# ---------------------------------------------------------------------------
# Multi-pass review (borrowed from Ultimate Bridge Brain module14)
# ---------------------------------------------------------------------------
# Two-pass generation: draft, then critique-and-revise. Use for high-stakes
# outputs (marketing copy, ads, customer-facing emails, finished campaigns).
# Costs ~2x the LLM time/tokens but the quality jump is dramatic.
#
# Don't use for casual chat — too slow, not worth the cost.

REVIEWER_SYSTEM = (
    "You are a senior editor reviewing an AI's draft. Your job is to "
    "produce a REVISED version that is sharper, more specific, more honest, "
    "and more punchy than the original.\n\n"
    "REVIEW CRITERIA:\n"
    "- SPECIFICITY: replace vague claims ('great service') with concrete ones\n"
    "  ('Friday 2pm pickup', '$2 mimosas', 'within 10 miles'). If a draft "
    "  has nothing concrete, leave the abstraction but tighten it.\n"
    "- HONESTY: cut anything not defensible. Strip invented features, "
    "  invented discounts, invented numbers, invented testimonials.\n"
    "- PUNCH: cut filler ('we are excited to', 'it goes without saying'). "
    "  Tighten sentences. Replace weak verbs with strong ones.\n"
    "- VOICE: PRESERVE the original tone exactly. If the draft is casual, "
    "  keep it casual. If formal, keep formal. Don't corporatize.\n"
    "- ERRORS: fix factual mistakes, contradictions, broken JSON.\n\n"
    "CRITICAL: PRESERVE the format and structure of the original exactly.\n"
    "  - JSON in → JSON out (same schema, same keys)\n"
    "  - prose in → prose out\n"
    "  - list in → list out\n"
    "  - bullets in → bullets out\n\n"
    "Output ONLY the revised version. No commentary. No preamble. No "
    "explanation of what you changed. Just the better version of the draft."
)


def generate_with_review(config: dict, system: str,
                          messages: list[dict],
                          enable_review: bool = True) -> LLMResponse:
    """Two-pass: initial draft + critique-and-revise pass.

    If enable_review=False or the review tier fails, returns the draft
    unchanged. Tier on a successful review is suffixed with '+review' so
    callers / logs can see the quality boost was applied.
    """
    draft = generate(config, system, messages)
    if not draft or not draft.text:
        return draft   # nothing to revise
    if not enable_review:
        return draft

    # Pass 2: have the LLM critique + rewrite its own draft
    original_brief = ""
    for m in messages:
        if m.get("role") == "user":
            original_brief = m.get("content", "")
    revise_user = (
        f"ORIGINAL REQUEST:\n{original_brief}\n\n"
        f"FIRST DRAFT (revise this):\n{draft.text}\n\n"
        "Return ONLY the revised version."
    )
    try:
        revised = generate(config, REVIEWER_SYSTEM,
                            [{"role": "user", "content": revise_user}])
    except Exception:
        log.exception("review pass crashed; using draft")
        return draft

    if revised and revised.text and len(revised.text) >= 10:
        revised.tier = f"{revised.tier}+review"
        log.info(f"review pass applied: draft={len(draft.text)} → "
                 f"revised={len(revised.text)} chars")
        return revised
    return draft


def current_connection_state(config: dict) -> str:
    """Quick liveness check used by /api/owner/status. Returns 'online', 'degraded', 'offline'."""
    # Try brain first with a short timeout
    try:
        url = config["brain"]["url"].rstrip("/") + "/health"
        with urllib.request.urlopen(url, timeout=3) as r:
            if r.status == 200:
                return "online"
    except Exception:
        pass
    # Brain unreachable — check internet by probing HF
    try:
        with urllib.request.urlopen("https://huggingface.co", timeout=3) as r:
            if r.status < 500:
                return "degraded"
    except Exception:
        pass
    return "offline"


# ---------------------------------------------------------------------------
# IMAGE GENERATION — HuggingFace Inference for diffusion models
# ---------------------------------------------------------------------------
# Used by the marketing_image sub-module. Uses the same HF account that
# powers the LLM tier, so no new credentials needed. FLUX.1-schnell is
# the recommended default — fast (~5-10s/image) and cheap. SDXL or
# FLUX.1-dev can be selected for higher quality at higher cost.

def generate_image(config: dict, prompt: str,
                    model: str = "black-forest-labs/FLUX.1-schnell",
                    size: tuple[int, int] = (1024, 1024),
                    timeout: int = 60) -> bytes:
    """Generate one image. Returns raw PNG bytes on success, raises on
    failure. The caller handles persistence + URL routing."""
    cfg = config.get("huggingface") or {}
    token = cfg.get("api_key")
    if not token:
        raise RuntimeError("HuggingFace API key not configured")
    if not prompt or len(prompt.strip()) < 4:
        raise ValueError("prompt is empty or too short")

    width, height = size
    # FLUX/SDXL accept width/height as ints divisible by 8.
    width  = max(256, min(1792, int(width)  // 8 * 8))
    height = max(256, min(1792, int(height) // 8 * 8))

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "image/png",
        "User-Agent":    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    body = {
        "inputs": prompt[:4000],
        "parameters": {
            "width":  width,
            "height": height,
            # FLUX.1-schnell ignores extra params; SDXL respects them.
            "num_inference_steps": 4 if "schnell" in model else 25,
            "guidance_scale": 0.0 if "schnell" in model else 7.5,
        },
    }

    # Primary: HF Inference router (Inference Providers — paid HF Pro)
    # Provider-specific routing happens server-side based on the model.
    primary_url = f"https://router.huggingface.co/hf-inference/models/{model}"
    # Fallback: direct (older free tier; some models still live here)
    fallback_url = f"https://api-inference.huggingface.co/models/{model}"

    last_err: Exception | None = None
    for url in (primary_url, fallback_url):
        try:
            req = urllib.request.Request(
                url, method="POST",
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    last_err = RuntimeError(
                        f"HF {url} returned HTTP {resp.status}")
                    continue
                content_type = resp.headers.get("Content-Type") or ""
                payload = resp.read()
                # Some HF endpoints wrap the image in JSON like
                # {"image": "<base64>"} when the model loader is busy.
                if "image" in content_type or content_type.startswith("image/"):
                    return payload
                if content_type.startswith("application/json"):
                    try:
                        data = json.loads(payload.decode("utf-8"))
                    except Exception:
                        last_err = RuntimeError("non-image, non-JSON response")
                        continue
                    # base64-encoded image fallback
                    b64 = (data.get("image") if isinstance(data, dict) else "") or ""
                    if b64:
                        import base64
                        return base64.b64decode(b64)
                    # "model is loading" / queued response — surface a useful error
                    err_msg = (data.get("error") if isinstance(data, dict) else "") or "unknown"
                    last_err = RuntimeError(f"HF response: {err_msg}")
                    continue
                # Unknown content type — best effort, return as-is
                return payload
        except urllib.error.HTTPError as e:
            last_err = RuntimeError(f"HTTP {e.code} from {url}: {e.reason}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            last_err = e
    raise RuntimeError(f"image generation failed: {last_err}")
