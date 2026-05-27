"""
style_learner — write replies in the OWNER's voice, not generic LLM-ese.

THE IDEA
--------
A generic LLM will draft a perfectly grammatical, perfectly bland reply.
Owners don't talk like that. Joe at the deli ends every email "thanks
again — Joe". Mary at the salon writes one-line replies. Tom at the
auto shop always says "no problem at all, happy to help."

This module learns each owner's voice by indexing their PAST sent
emails / replies and then, when drafting a new reply, retrieving the
most relevant 5-10 examples and injecting them into the LLM prompt as
"Write in this voice — here is exactly how the owner writes."

PIPELINE
--------
1.  index_owner_sent(config, user_dir)
    Owner clicks "Refresh style corpus" in Settings. We walk the Sent
    folder of every connected mailbox (Gmail "in:sent" search, Outlook
    /me/mailFolders/sentitems) and pull the most recent N messages.
    For each, strip quoted-reply chains and store
    {id, source, date, subject, snippet, body} in style_corpus.json.

    Expensive (dozens of API calls). On-demand only. Never in a loop.

2.  pick_relevant_examples(user_dir, draft_context, k=6)
    Given the context of what's being drafted (the incoming customer
    message + situation), TF-IDF rank the corpus and return the top K.
    Pure Python — collections.Counter for term frequency, math.log
    for IDF. No sklearn dependency. Body snippets capped at 500 chars.

3.  build_style_prompt_addendum(examples)
    Wrap the examples in a clear system-prompt block — labelled with
    date and any context — instructing the model to MATCH that voice.

4.  draft_in_owner_voice(config, user_dir, draft_context, what_to_say)
    End-to-end. Picks examples, builds the prompt, calls llm_client.

COLD START
----------
If the corpus is empty (owner just installed, hasn't refreshed yet, or
has no Sent mail), pick_relevant_examples returns []. The prompt
addendum is then empty and draft_in_owner_voice falls through to a
neutral baseline ("write a professional but warm reply"). Callers
should also surface this in the dashboard ("No style examples yet —
click Refresh to learn your voice").

ROUTE SURFACE (see bottom of file).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("orbi.style_learner")

_LOCK = threading.Lock()

_CORPUS_FILENAME = "style_corpus.json"

# Maximum body length stored per example. Long signatures + previous-thread
# chatter inflate the prompt; 4 KB per body is plenty for style.
_BODY_STORE_CAP = 4000

# Cap on body length INJECTED into the prompt per example (instructions said
# 500 chars). We store more so future tuning can extend without re-indexing.
_BODY_PROMPT_CAP = 500

# A reply must have at least this many alphanumeric chars to be useful as a
# style example. Filters out "ok", "thanks!", auto-replies, etc.
_MIN_BODY_CHARS = 30


# ---------------------------------------------------------------------------
# Path helpers + atomic IO
# ---------------------------------------------------------------------------

def _corpus_path(user_dir: Path) -> Path:
    return Path(user_dir) / _CORPUS_FILENAME


def _read_corpus(user_dir: Path) -> dict:
    """Return {"indexed_at": iso, "examples": [...]} — always a dict, never None."""
    p = _corpus_path(user_dir)
    if not p.exists():
        return {"indexed_at": "", "examples": [], "by_source": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"indexed_at": "", "examples": [], "by_source": {}}
        data.setdefault("examples", [])
        data.setdefault("indexed_at", "")
        data.setdefault("by_source", {})
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("style_learner: corpus read failed: %s", exc)
        return {"indexed_at": "", "examples": [], "by_source": {}}


def _write_corpus(user_dir: Path, data: dict) -> None:
    p = _corpus_path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except (OSError, NotImplementedError):
            pass


def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"))


# ---------------------------------------------------------------------------
# Quoted-reply stripper
# ---------------------------------------------------------------------------

# "On Mon, May 26, 2026 at 4:33 PM, Joe Smith <joe@x.com> wrote:" — Gmail style.
# "On 5/26/26, Joe Smith wrote:" — also Gmail.
# "From: Joe Smith\nSent: ..." — Outlook style.
# We cut everything from the first such marker onward.
_QUOTE_MARKERS = [
    re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*-{3,}\s*Original Message\s*-{3,}\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*From:\s.+\nSent:\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*From:\s.+\nDate:\s",  re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*_{5,}\s*$", re.MULTILINE),                     # Outlook hr
    re.compile(r"^\s*Sent from my (iPhone|Android|Samsung).*$", re.IGNORECASE | re.MULTILINE),
]


def _strip_quoted(body: str) -> str:
    """Cut quoted-reply chains and mobile signatures. Returns just the
    owner-authored portion of a reply."""
    if not body:
        return ""
    text = body.replace("\r\n", "\n").replace("\r", "\n")

    # Earliest match across markers wins.
    earliest = len(text)
    for rx in _QUOTE_MARKERS:
        m = rx.search(text)
        if m and m.start() < earliest:
            earliest = m.start()
    text = text[:earliest]

    # Strip lines starting with ">" (classic quoted-reply prefix).
    kept: list[str] = []
    for line in text.split("\n"):
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    text = "\n".join(kept)

    # Collapse runs of blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Public: index_owner_sent
# ---------------------------------------------------------------------------

def index_owner_sent(config: dict, user_dir: Path, limit: int = 200) -> dict:
    """Walk Gmail + Outlook Sent folders, build the style corpus.

    Run on-demand only (it's expensive — dozens of API calls).

    Returns
    -------
    {
      "indexed":   total_examples_stored,
      "by_source": {"gmail": N, "outlook": M},
      "errors":    [ "...", ... ],
      "indexed_at": "2026-05-27T..."
    }
    """
    user_dir = Path(user_dir)
    examples: list[dict] = []
    by_source: dict[str, int] = {"gmail": 0, "outlook": 0}
    errors: list[str] = []

    # --- Gmail ----------------------------------------------------------------
    try:
        from connectors.gmail import GmailConnector
        gconn = GmailConnector(config, user_dir)
        if gconn.is_connected():
            gmail_items = _pull_gmail_sent(gconn, limit, errors)
            for item in gmail_items:
                examples.append(item)
                by_source["gmail"] += 1
        else:
            log.info("style_learner: gmail not connected — skipping")
    except Exception as exc:    # noqa: BLE001 — never break the indexer
        log.warning("style_learner: gmail indexing crashed: %s", exc)
        errors.append(f"gmail: {type(exc).__name__}: {exc}")

    # --- Outlook --------------------------------------------------------------
    try:
        from connectors.outlook import OutlookConnector
        oconn = OutlookConnector(config, user_dir)
        if oconn.is_connected():
            outlook_items = _pull_outlook_sent(oconn, limit, errors)
            for item in outlook_items:
                examples.append(item)
                by_source["outlook"] += 1
        else:
            log.info("style_learner: outlook not connected — skipping")
    except Exception as exc:    # noqa: BLE001
        log.warning("style_learner: outlook indexing crashed: %s", exc)
        errors.append(f"outlook: {type(exc).__name__}: {exc}")

    # Sort newest first by date (best-effort — string ISO sorts correctly,
    # RFC 2822 dates from Gmail won't but that's fine, we keep them all).
    examples.sort(key=lambda e: e.get("date", ""), reverse=True)

    record = {
        "indexed_at": _now_iso(),
        "examples":   examples,
        "by_source":  by_source,
        "errors":     errors,
    }
    _write_corpus(user_dir, record)

    log.info("style_learner: indexed %d examples (gmail=%d outlook=%d errors=%d)",
             len(examples), by_source["gmail"], by_source["outlook"], len(errors))
    return {
        "indexed":    len(examples),
        "by_source":  by_source,
        "errors":     errors,
        "indexed_at": record["indexed_at"],
    }


def _pull_gmail_sent(conn, limit: int, errors: list[str]) -> list[dict]:
    """Use GmailConnector.search('in:sent', limit) → full message → strip."""
    out: list[dict] = []
    try:
        summaries = conn.search("in:sent", limit) or []
    except Exception as exc:    # noqa: BLE001
        errors.append(f"gmail search in:sent failed: {exc}")
        return out

    for s in summaries:
        mid = s.get("id", "")
        if not mid:
            continue
        try:
            full = conn.get_message(mid) or {}
        except Exception as exc:    # noqa: BLE001
            errors.append(f"gmail get_message {mid}: {exc}")
            continue
        body = _strip_quoted(full.get("body", "") or "")
        if len(body) < _MIN_BODY_CHARS:
            continue
        out.append({
            "id":      f"gmail:{mid}",
            "source":  "gmail",
            "date":    full.get("date", "") or "",
            "subject": full.get("subject", "") or "",
            "snippet": (full.get("snippet", "") or "")[:200],
            "body":    body[:_BODY_STORE_CAP],
        })
    return out


def _pull_outlook_sent(conn, limit: int, errors: list[str]) -> list[dict]:
    """Hit Graph /me/mailFolders/sentitems/messages directly via conn._graph.

    The OutlookConnector exposes _graph (lower-case) — we use it because
    the public API doesn't yet have a "list sent" method. Tolerated:
    we're inside the same install, and _graph already handles auth refresh.
    """
    out: list[dict] = []
    params = {
        "$top":     max(1, min(int(limit or 200), 100)),
        "$select":  "id,subject,from,bodyPreview,sentDateTime",
        "$orderby": "sentDateTime desc",
    }
    try:
        status, body = conn._graph(
            "GET", "/me/mailFolders/sentitems/messages", params=params,
        )
    except Exception as exc:    # noqa: BLE001
        errors.append(f"outlook list sentitems failed: {exc}")
        return out

    if status != 200 or not isinstance(body, dict):
        errors.append(f"outlook list sentitems status={status}")
        return out

    for m in body.get("value", []) or []:
        mid = m.get("id", "")
        if not mid:
            continue
        # Full message for the body content.
        try:
            full = conn.get_message(mid) or {}
        except Exception as exc:    # noqa: BLE001
            errors.append(f"outlook get_message {mid}: {exc}")
            continue
        body_text = _strip_quoted(full.get("body", "") or "")
        if len(body_text) < _MIN_BODY_CHARS:
            continue
        out.append({
            "id":      f"outlook:{mid}",
            "source":  "outlook",
            "date":    full.get("received_iso", "") or m.get("sentDateTime", "") or "",
            "subject": full.get("subject", "") or m.get("subject", "") or "",
            "snippet": (m.get("bodyPreview", "") or "")[:200],
            "body":    body_text[:_BODY_STORE_CAP],
        })
    return out


# ---------------------------------------------------------------------------
# TF-IDF — pure Python, no sklearn
# ---------------------------------------------------------------------------
#
# Why TF-IDF (and not embeddings)?
#   - Embedding models require either a paid API or another local model
#     loaded in RAM. Orbi is local-first and already runs a 3B LLM —
#     we can't afford another model just for retrieval.
#   - TF-IDF in pure Python over 200 short emails is microseconds.
#   - For style retrieval the words that matter (recurring vocab, sign-offs,
#     hedges, idioms) are exactly the ones TF-IDF surfaces. Embeddings
#     would over-prioritise semantic match over lexical voice match.
#
# Implementation:
#   - Tokenize: lower-case, [a-z0-9']+ runs, drop tokens shorter than 2.
#   - Term frequency: collections.Counter per document.
#   - Inverse document frequency: log((N+1) / (df+1)) + 1  (smoothed).
#   - Document vector: tf * idf per term.
#   - Score: cosine similarity between query vec and doc vec.
#   - Stopwords stripped from the QUERY only (we want stopword tokens in
#     the documents so they get an IDF boost downward — but a query
#     dominated by "the of and" would be all noise).

_TOKEN_RE = re.compile(r"[a-z0-9']+")

_STOPWORDS = frozenset("""
a an and are as at be by for from has have he her him his i in is it its
me my of on or our she that the their them they this to us was we were
will with you your yours
""".split())


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2]


def _tokenize_query(text: str) -> list[str]:
    return [t for t in _tokenize(text) if t not in _STOPWORDS]


def _compute_idf(docs: list[list[str]]) -> dict[str, float]:
    """Smoothed IDF: log((N+1) / (df+1)) + 1."""
    n = len(docs)
    df: Counter[str] = Counter()
    for tokens in docs:
        for t in set(tokens):
            df[t] += 1
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def _vectorize(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    """tf * idf vector for one document or query."""
    tf = Counter(tokens)
    if not tf:
        return {}
    return {t: c * idf.get(t, 1.0) for t, c in tf.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    # Iterate over the smaller dict for the dot product.
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    if dot == 0.0:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Public: pick_relevant_examples
# ---------------------------------------------------------------------------

def pick_relevant_examples(user_dir: Path, draft_context: str,
                            k: int = 6) -> list[dict]:
    """Return the K examples from the corpus most relevant to draft_context.

    If the query has no informative tokens (cold start or junk input),
    we fall back to the K most recent examples — better to give the
    model SOME voice signal than none.
    """
    corpus = _read_corpus(Path(user_dir))
    examples = corpus.get("examples", []) or []
    if not examples:
        return []

    k = max(1, int(k or 6))

    # Tokenize every body once. Subject is included with extra weight by
    # being duplicated into the token stream — subjects often capture the
    # situation ("re: catering question") which is great for retrieval.
    doc_tokens: list[list[str]] = []
    for ex in examples:
        toks = _tokenize(ex.get("body", "") or "")
        subj = _tokenize(ex.get("subject", "") or "")
        doc_tokens.append(toks + subj + subj)   # subject weighted x2

    idf = _compute_idf(doc_tokens)
    doc_vecs = [_vectorize(t, idf) for t in doc_tokens]

    q_tokens = _tokenize_query(draft_context)
    if not q_tokens:
        # Cold-start / junk-query fallback: most recent k.
        ranked = examples[:k]
    else:
        q_vec = _vectorize(q_tokens, idf)
        scored = [
            (i, _cosine(q_vec, doc_vecs[i]))
            for i in range(len(examples))
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        # If every score is 0 (no token overlap at all), still return the
        # most recent k rather than an empty list.
        if scored and scored[0][1] == 0.0:
            ranked = examples[:k]
        else:
            ranked = [examples[i] for i, _ in scored[:k]]

    # Trim bodies for prompt insertion. Keep the original dict but cap body.
    out: list[dict] = []
    for ex in ranked:
        body = (ex.get("body", "") or "")[:_BODY_PROMPT_CAP]
        out.append({
            "id":      ex.get("id", ""),
            "source":  ex.get("source", ""),
            "date":    ex.get("date", ""),
            "subject": ex.get("subject", ""),
            "snippet": ex.get("snippet", ""),
            "body":    body,
        })
    return out


# ---------------------------------------------------------------------------
# Public: build_style_prompt_addendum
# ---------------------------------------------------------------------------

def build_style_prompt_addendum(examples: list[dict]) -> str:
    """Format examples into a prompt block telling the model to match the
    owner's voice exactly. Returns "" if the example list is empty so
    callers can concatenate safely without leaving a stray heading."""
    if not examples:
        return ""
    n = len(examples)
    lines: list[str] = [
        f"VOICE EXAMPLES — Here are {n} real example{'s' if n != 1 else ''} "
        "of how the OWNER personally writes replies. Match this voice exactly:",
        "  - same vocabulary",
        "  - same warmth / tone",
        "  - same opening + sign-off style",
        "  - same sentence length",
        "  - same use of abbreviations, contractions, emojis, etc.",
        "Do NOT use stiff or formal phrasing the owner doesn't use.",
        "",
    ]
    for i, ex in enumerate(examples, 1):
        date = (ex.get("date") or "")[:10] or "unknown date"
        subj = (ex.get("subject") or "").strip() or "(no subject)"
        body = (ex.get("body") or "").strip()
        lines.append(f"--- Example {i} ({date}) — subject: {subj} ---")
        lines.append(body)
        lines.append("")
    lines.append("--- end of voice examples ---")
    lines.append("")
    lines.append("Now write the reply below in EXACTLY that voice.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public: draft_in_owner_voice
# ---------------------------------------------------------------------------

def draft_in_owner_voice(config: dict, user_dir: Path,
                         draft_context: str, what_to_say: str) -> str:
    """End-to-end. Pick examples, build prompt, call llm_client.generate().

    draft_context: the situation — usually the incoming customer message
                   (or a summary of it). Used both for retrieval and as
                   the "context" the model sees.
    what_to_say:   what the reply should accomplish — "thank them and confirm
                   the appointment is Tuesday at 3pm", "politely decline,
                   we don't do weddings".
    Returns: the draft reply text. Empty string if all LLM tiers fail.
    """
    user_dir = Path(user_dir)
    examples = pick_relevant_examples(user_dir, draft_context, k=6)
    style_block = build_style_prompt_addendum(examples)

    if style_block:
        system = (
            "You are drafting a reply on behalf of the business owner. "
            "Below are examples of how the owner personally writes — "
            "you MUST match that voice exactly. Do not invent facts; if "
            "the requested message asks you to say something specific, "
            "say it in the owner's words.\n\n"
            + style_block
        )
    else:
        # Cold-start fallback: no voice examples available.
        system = (
            "You are drafting a reply on behalf of the business owner. "
            "Write in a warm, friendly, professional tone — short sentences, "
            "no corporate stiffness, no emojis unless clearly appropriate. "
            "Do not invent facts. "
            "(NOTE: no voice examples have been indexed yet — owner can click "
            "'Refresh style corpus' in Settings to teach Orbi their voice.)"
        )

    user_msg = (
        f"INCOMING CONTEXT:\n{(draft_context or '').strip()}\n\n"
        f"WHAT THE REPLY SHOULD SAY:\n{(what_to_say or '').strip()}\n\n"
        "Write the reply now. Output ONLY the reply body — no subject line, "
        "no headers, no commentary."
    )

    # Lazy import — keeps llm_client out of this module's import-time cost
    # and avoids a circular import if llm_client ever wants to use us.
    try:
        import llm_client
        resp = llm_client.generate(
            config,
            system,
            [{"role": "user", "content": user_msg}],
        )
    except Exception as exc:    # noqa: BLE001
        log.warning("style_learner: llm_client.generate crashed: %s", exc)
        return ""

    text = (resp.text or "").strip() if resp else ""
    if not text:
        log.info("style_learner: LLM returned empty (tier=%s err=%s)",
                 getattr(resp, "tier", "none"),
                 getattr(resp, "error", "unknown"))
    return text


# ---------------------------------------------------------------------------
# Public: count_corpus
# ---------------------------------------------------------------------------

def count_corpus(user_dir: Path) -> int:
    """Number of examples currently in the corpus. For dashboard display."""
    corpus = _read_corpus(Path(user_dir))
    return len(corpus.get("examples", []) or [])


def corpus_status(user_dir: Path) -> dict:
    """Convenience for the /status route — count + last_indexed + by_source."""
    corpus = _read_corpus(Path(user_dir))
    return {
        "count":        len(corpus.get("examples", []) or []),
        "last_indexed": corpus.get("indexed_at", "") or "",
        "by_source":    corpus.get("by_source", {}) or {},
    }


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are OWNER-AUTHED (cookie). user_dir comes from the logged-in
# owner's per-user data folder. config is the loaded config.json.
#
#   POST /api/owner/style/refresh
#       body:    {} (optional: {"limit": 200})
#       calls:   style_learner.index_owner_sent(CONFIG, user_dir,
#                                               limit=body.get("limit", 200))
#       returns: {"indexed": N, "by_source": {"gmail": N, "outlook": M},
#                 "errors": [...], "indexed_at": "..."}
#       NOTE — expensive (dozens of API calls). Run on-demand only,
#       never in a background loop.
#
#   GET  /api/owner/style/status
#       calls:   style_learner.corpus_status(user_dir)
#       returns: {"count": N, "last_indexed": "...", "by_source": {...}}
#
#   POST /api/owner/style/draft
#       body:    {"draft_context": "...", "what_to_say": "..."}
#       calls:   style_learner.draft_in_owner_voice(
#                    CONFIG, user_dir,
#                    body["draft_context"], body["what_to_say"])
#       returns: {"draft": "...reply text..."}
#       errors:  {"draft": ""} on total LLM failure — UI shows "Orbi can't
#                reach any LLM right now, try again in a moment".
#
# ---------------------------------------------------------------------------
