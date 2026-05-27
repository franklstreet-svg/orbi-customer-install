"""
translation.py — Language detection + LLM-driven translation for Orbi.

Used by the receptionist + dashboard so that if a customer messages
in Spanish (or any of ~12 common languages), the owner can read it
in English AND we can suggest a reply back in the customer's language.

Detection is a lightweight heuristic (character ranges + tiny common-
word lists) — no extra pip deps. If the heuristic can't decide, we
ask the LLM (one cheap call) to identify the language.

Translation is pure LLM. We cap inputs at 5000 characters so a bad
paste can't blow up a tier-1 token budget.

ROUTES (registered by orbi.py — leave a comment block here):

  POST /api/owner/translate
       Body: {text, target_lang?, source_lang?}
       Returns {translation, source_lang, target_lang}.

  POST /api/owner/detect_language
       Body: {text}
       Returns {lang}.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("orbi.translation")

MAX_TRANSLATE_CHARS = 5000

# ── Language metadata ───────────────────────────────────────────────────

LANG_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
}

# Distinctive common words (lowercased, whitespace-padded for matching).
# Kept short on purpose — we only need to disambiguate between Latin-
# script European languages.
_WORD_HINTS = {
    "en": (" the ", " and ", " you ", " for ", " with ", " this ", " that ", " hello "),
    "es": (" el ", " la ", " los ", " las ", " que ", " por ", " para ",
           " hola ", " gracias ", " usted ", " pero ", " también "),
    "fr": (" le ", " la ", " les ", " des ", " une ", " avec ", " pour ",
           " bonjour ", " merci ", " est ", " mais ", " vous "),
    "de": (" der ", " die ", " das ", " und ", " ist ", " nicht ", " mit ",
           " hallo ", " danke ", " nicht ", " sind ", " auch "),
    "it": (" il ", " la ", " gli ", " che ", " con ", " per ", " sono ",
           " ciao ", " grazie ", " buongiorno ", " questo "),
    "pt": (" o ", " a ", " os ", " as ", " que ", " com ", " para ", " não ",
           " obrigado ", " obrigada ", " olá ", " você "),
}

# Unicode-range probes for non-Latin scripts.
_SCRIPT_RANGES = (
    ("zh", re.compile(r"[一-鿿]")),       # CJK Unified
    ("ja", re.compile(r"[぀-ゟ゠-ヿ]")),  # Hiragana / Katakana
    ("ko", re.compile(r"[가-힯]")),       # Hangul Syllables
    ("ru", re.compile(r"[Ѐ-ӿ]")),       # Cyrillic
    ("ar", re.compile(r"[؀-ۿ]")),       # Arabic
    ("hi", re.compile(r"[ऀ-ॿ]")),       # Devanagari
)


# ── detect_language ─────────────────────────────────────────────────────


def detect_language(text: str) -> str:
    """Best-effort ISO-639-1 code. Returns "" if undecided.

    Strategy:
      1. Non-Latin script ranges win immediately (CJK / Cyrillic / etc).
         For CJK we check Japanese kana before Chinese, otherwise mixed
         Japanese text gets mis-labeled as Chinese.
      2. For Latin script we count common-word matches per language
         and pick the winner — but only if it beats the runner-up by
         a comfortable margin AND clears a minimum hit count.
      3. If nothing wins by margin, return "" so the caller can ask
         the LLM.
    """
    if not text or not text.strip():
        return ""
    sample = text[:1000]

    # 1. Script-range checks (non-Latin scripts).
    # Japanese must be checked before Chinese (kana would otherwise be
    # missed if pure Chinese is present alongside).
    if _SCRIPT_RANGES[1][1].search(sample):  # ja kana
        return "ja"
    if _SCRIPT_RANGES[2][1].search(sample):  # ko hangul
        return "ko"
    if _SCRIPT_RANGES[0][1].search(sample):  # zh han (no kana)
        return "zh"
    for code, rx in _SCRIPT_RANGES[3:]:
        if rx.search(sample):
            return code

    # 2. Latin-script word-frequency scoring.
    padded = " " + sample.lower() + " "
    scores: dict[str, int] = {}
    for code, words in _WORD_HINTS.items():
        scores[code] = sum(1 for w in words if w in padded)

    if not scores or max(scores.values()) == 0:
        return ""

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_lang, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0

    # Require minimum 2 hits AND beat runner-up by at least 2.
    if top_score >= 2 and (top_score - runner_up) >= 2:
        return top_lang

    # Heuristic didn't decide — ask the LLM.
    return _llm_detect_language(sample)


def _llm_detect_language(sample: str) -> str:
    """LLM fallback for language detection. Returns "" on any error."""
    try:
        import llm_client  # lazy
    except ImportError:
        return ""

    system = (
        "You are a language identifier. Reply with ONLY the ISO-639-1 "
        "two-letter language code (lowercase) for the text the user sends. "
        "If you cannot tell, reply with the single character '?' and nothing else. "
        "Do not include punctuation, explanation, or quotes."
    )
    try:
        resp = llm_client.generate(None_safe_config(), system, [
            {"role": "user", "content": sample[:500]}
        ])
        raw = (resp.text or "").strip().lower() if resp else ""
    except Exception as e:
        log.warning(f"detect_language LLM call failed: {e}")
        return ""

    # Extract the first two letters that look like a code.
    m = re.search(r"[a-z]{2}", raw)
    if not m:
        return ""
    code = m.group(0)
    return code if code in LANG_NAMES else ""


def None_safe_config():
    """detect_language is sometimes called without a config (we only
    need the LLM client's tier list, not any business settings). Try
    loading the default config; on failure return an empty dict so
    llm_client tries each tier with whatever defaults it has."""
    try:
        import json
        from pathlib import Path
        here = Path(__file__).resolve().parent
        for name in ("config.json", "config.json.template"):
            p = here / name
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# ── translate ───────────────────────────────────────────────────────────


def translate(config: dict, text: str, target_lang: str = "en",
              source_lang: str | None = None) -> str:
    """Translate `text` to `target_lang` (ISO-639-1). Returns the
    translation as plain text — no preamble, no quotes. Input is
    capped at MAX_TRANSLATE_CHARS to prevent runaway costs.

    Returns "" on total failure (the caller can decide whether to
    show the original or an error).
    """
    if not text or not text.strip():
        return ""
    if len(text) > MAX_TRANSLATE_CHARS:
        text = text[:MAX_TRANSLATE_CHARS]

    target_name = LANG_NAMES.get((target_lang or "en").lower(), target_lang or "English")
    source_hint = ""
    if source_lang:
        source_name = LANG_NAMES.get(source_lang.lower(), source_lang)
        source_hint = f" The source language is {source_name}."

    system = (
        f"Translate to {target_name} preserving meaning and tone."
        f"{source_hint} Output ONLY the translation — no preamble, "
        "no quotation marks, no explanation."
    )

    try:
        import llm_client  # lazy
        resp = llm_client.generate(config or {}, system, [
            {"role": "user", "content": text}
        ])
        out = (resp.text or "").strip() if resp else ""
    except Exception as e:
        log.warning(f"translate LLM call failed: {e}")
        return ""

    # Strip surrounding quotes some models add even when told not to.
    if out.startswith(('"', "'")) and out.endswith(('"', "'")) and len(out) > 2:
        out = out[1:-1].strip()
    return out


# ── auto_handle_customer_message ────────────────────────────────────────


def auto_handle_customer_message(config: dict, text: str,
                                  owner_lang: str = "en") -> dict:
    """Front-line helper for the receptionist. Given the customer's
    raw message and the owner's preferred language, decide whether
    translation is needed.

    Returns:
      {original_text, detected_lang, translation, suggested_reply_lang}

    `translation` is None when no translation is needed (customer is
    already speaking owner's language).
    `suggested_reply_lang` is the language Orbi should REPLY in —
    always matches the customer's detected language when known, so
    the customer feels heard in their own tongue.
    """
    owner_lang = (owner_lang or "en").lower()
    detected = detect_language(text) or ""

    if detected and detected != owner_lang:
        translation = translate(config, text,
                                target_lang=owner_lang,
                                source_lang=detected)
        return {
            "original_text":         text,
            "detected_lang":         detected,
            "translation":           translation or None,
            "suggested_reply_lang":  detected,
        }

    # Either same language as owner, or undetectable — no translation needed.
    return {
        "original_text":         text,
        "detected_lang":         detected or owner_lang,
        "translation":           None,
        "suggested_reply_lang":  detected or owner_lang,
    }
