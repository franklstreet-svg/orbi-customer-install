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
    cfg = config["brain"]
    url = cfg["url"].rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": "llama-3.1-8b-instruct",
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": 0.6,
        # 2048 covers ~1500 words — enough for marketing campaigns,
        # multi-section letters, blog drafts. 512 was cutting long
        # responses off mid-list.
        "max_tokens": 2048,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
        "X-Orbi-Client": "customer-install",
    }
    return _http_chat(url, payload, headers, cfg.get("timeout_seconds", 15), tier="brain")


# ---------------------------------------------------------------------------
# Tier 2: HuggingFace
# ---------------------------------------------------------------------------

def call_huggingface(config: dict, system: str, messages: list[dict]) -> LLMResponse:
    """Try router first (best models), fall back to direct api-inference
    (works on free/older accounts)."""
    cfg = config["huggingface"]
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
    timeout = cfg.get("timeout_seconds", 15)

    # Attempt 1: router (Inference Providers — requires paid HF Pro)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": 0.6,
        "max_tokens": 2048,
        "stream": False,
    }
    resp = _http_chat("https://router.huggingface.co/v1/chat/completions",
                      payload, headers, timeout, tier="huggingface")
    if resp:
        return resp

    # Attempt 2: direct model endpoint (older free tier — model is in URL path)
    url = f"https://api-inference.huggingface.co/models/{model}/v1/chat/completions"
    resp = _http_chat(url, payload, headers, timeout, tier="huggingface")
    return resp


# ---------------------------------------------------------------------------
# Tier 3: Local Llama-3.2-3B
# ---------------------------------------------------------------------------

def call_local(config: dict, system: str, messages: list[dict]) -> LLMResponse:
    cfg = config["local_llm"]
    if not cfg.get("enabled"):
        return LLMResponse("", "local", 0, "disabled")
    port = cfg.get("port", 11435)
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    payload = {
        "model": "llama-3.2-3b-instruct",
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": 0.5,
        # Local 3B is slow — keep its cap lower (~750 words) so
        # offline-mode fallback doesn't hang the browser for 2 min.
        "max_tokens": 1024,
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

def generate(config: dict, system: str, messages: list[dict]) -> LLMResponse:
    """
    Try each tier in order, return the first successful response.
    Returns an empty LLMResponse with tier='none' if all tiers fail.
    """
    for tier_fn, tier_name in (
        (call_brain,       "brain"),
        (call_huggingface, "huggingface"),
        (call_local,       "local"),
    ):
        try:
            resp = tier_fn(config, system, messages)
            if resp:
                return resp
        except Exception as e:
            log.exception(f"tier {tier_name} crashed")
    return LLMResponse("", "none", 0, "all_tiers_failed")


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
