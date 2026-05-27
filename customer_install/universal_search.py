"""
universal_search — ONE query, every data source the logged-in user can see.

The owner types a single search phrase (or asks Orbi an open-ended question
like "tell me everything about Joe Smith") and this module fans out across
EVERY module + connected external service and returns a normalized,
ranked, capped set of hits.

Design choices:

  * Concurrent fan-out with ThreadPoolExecutor — every source runs in its
    own worker thread, so a slow connector (Gmail round-trip, Notion API)
    can NEVER block the local sources (contacts, calendar, notes...).

  * Per-source timeout of 3 seconds (hard). If a source doesn't return
    inside the window we cancel it, record the timeout in `errors`, and
    return whatever the fast sources produced. Better to surface partial
    results immediately than to make the owner wait.

  * Per-source exception catching — one broken source (corrupt JSON,
    expired token, network blip) NEVER takes down the whole search.

  * Lazy imports inside helpers — the file imports cheap, and a missing
    optional dep on one module doesn't block the rest.

  * Connector sources only run if `inst.is_connected()` — saves a round
    trip and prevents accidental "unauthorized" log spam.

  * Each hit is normalized to {title, snippet, source, source_label, link,
    ts, score?} so the frontend renders any source the same way.

  * `limit_per_source` caps each bucket so a massive workspace can't drown
    out two perfect contact matches.

  * `to_chat_context()` formats the result into a compact LLM context
    block — the chat handler can drop this straight into a system prompt
    when the owner asks open-ended questions about a person/topic.

Route surface (orchestrator wires this in):
  GET /api/owner/search?q=joe&limit=5
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

log = logging.getLogger("orbi.universal_search")

# Hard ceiling per source. A slow Gmail call cannot stall the whole UI.
SOURCE_TIMEOUT_SECONDS = 3.0

# Sources fall into three groups based on what they need to query.
# Order doesn't matter for correctness — only for tidy iteration.
_LOCAL_USER_SOURCES = ("contacts", "calendar", "tasks", "reminders")
_LOCAL_SHARED_SOURCES = ("notes", "messages", "workspace", "catalog",
                         "learned", "business")
_CONNECTOR_SOURCES = ("gmail", "outlook", "notion", "slack")

ALL_SOURCES = _LOCAL_USER_SOURCES + _LOCAL_SHARED_SOURCES + _CONNECTOR_SOURCES

# Human-readable labels — what shows up next to a hit in the UI.
SOURCE_LABELS = {
    "contacts":  "Contact",
    "calendar":  "Calendar",
    "tasks":     "Task",
    "reminders": "Reminder",
    "notes":     "Note",
    "messages":  "Message",
    "workspace": "Workspace file",
    "catalog":   "Catalog item",
    "learned":   "Learned answer",
    "business":  "Business info",
    "gmail":     "Gmail",
    "outlook":   "Outlook",
    "notion":    "Notion",
    "slack":     "Slack",
}


# ── Public API ──────────────────────────────────────────────────────────


def search(config: dict, data_dir: Path, user_dir: Path, query: str,
           limit_per_source: int = 5) -> dict:
    """Fan out to every source and return aggregated hits.

    Args:
        config:           loaded config.json
        data_dir:         shared per-install data dir
        user_dir:         per-user data dir (the logged-in owner/staff)
        query:            the search phrase
        limit_per_source: cap on hits returned per source

    Returns:
        {
          "query": "joe smith",
          "total_hits": N,
          "by_source": { "contacts": [...], "calendar": [...], ... },
          "errors":    { "gmail": "timeout", ... }
        }
    """
    q = (query or "").strip()
    out: dict[str, Any] = {
        "query":      q,
        "total_hits": 0,
        "by_source":  {},
        "errors":     {},
    }
    if not q:
        return out

    data_dir = Path(data_dir)
    user_dir = Path(user_dir)

    # Submit every source to the pool, then collect with per-future timeout.
    # max_workers = len(ALL_SOURCES) so nothing queues — every source gets
    # its own worker and the wall-clock cost is ~max(per-source latency).
    with ThreadPoolExecutor(
        max_workers=len(ALL_SOURCES),
        thread_name_prefix="universal-search",
    ) as pool:
        futures = {
            source_id: pool.submit(
                _safe_run, source_id, config, data_dir, user_dir, q,
                limit_per_source,
            )
            for source_id in ALL_SOURCES
        }

        deadline_start = time.monotonic()
        for source_id, fut in futures.items():
            # Each source gets its own slice of the timeout budget. We
            # compute remaining time from the original start so a single
            # slow source can't extend the total window past ~3s.
            remaining = SOURCE_TIMEOUT_SECONDS - (time.monotonic() - deadline_start)
            wait = max(0.05, min(SOURCE_TIMEOUT_SECONDS, remaining))
            try:
                hits, err = fut.result(timeout=wait)
            except FutureTimeoutError:
                fut.cancel()
                out["errors"][source_id] = "timeout"
                continue
            except Exception as e:
                # Belt-and-suspenders — _safe_run already catches, but if
                # something escaped we still don't poison the response.
                log.warning("universal_search: %s raised %s", source_id, e)
                out["errors"][source_id] = f"{type(e).__name__}: {e}"
                continue

            if err:
                out["errors"][source_id] = err

            if hits:
                out["by_source"][source_id] = hits
                out["total_hits"] += len(hits)

    return out


def search_one_source(source_id: str, config: dict, data_dir: Path,
                      user_dir: Path, query: str, limit: int = 5) -> list[dict]:
    """Run ONE source. Internal helper, exposed for unit tests + the
    orchestrator's debug surface."""
    fn = _DISPATCH.get(source_id)
    if not fn:
        return []
    try:
        return fn(config, Path(data_dir), Path(user_dir), query, limit) or []
    except Exception as e:
        log.warning("universal_search: %s failed: %s", source_id, e)
        return []


def to_chat_context(results: dict, max_chars: int = 3000) -> str:
    """Compact the results dict into a string the LLM can ingest as part
    of a system prompt. Used when the owner asks open-ended questions
    ("tell me everything about Joe Smith") — the chat handler can include
    this block before the user message so the model has every source.

    Format prioritises readability for tiny local LLMs: short labels,
    one hit per line, capped per source. Truncates politely if we'd
    blow `max_chars`."""
    query = results.get("query") or ""
    by_source = results.get("by_source") or {}
    total = results.get("total_hits") or 0

    if not total:
        return f"SEARCH RESULTS for \"{query}\": no matches.\n"

    lines: list[str] = [
        f"SEARCH RESULTS for \"{query}\" ({total} match{'es' if total != 1 else ''}):",
    ]
    chars = len(lines[0]) + 1

    for source_id in ALL_SOURCES:
        hits = by_source.get(source_id) or []
        if not hits:
            continue
        label = SOURCE_LABELS.get(source_id, source_id.title())
        header = f"\n-- {label} ({len(hits)}) --"
        if chars + len(header) > max_chars:
            lines.append("\n... [truncated]")
            break
        lines.append(header)
        chars += len(header) + 1

        for hit in hits:
            title = (hit.get("title") or "").strip() or "(untitled)"
            snippet = (hit.get("snippet") or "").strip()
            # Compact one-liner per hit. Snippets already capped at 200 chars.
            line = f"- {title}"
            if snippet and snippet.lower() != title.lower():
                line += f" — {snippet}"
            if chars + len(line) > max_chars:
                lines.append("... [truncated]")
                break
            lines.append(line)
            chars += len(line) + 1
        else:
            continue
        break  # outer break propagation when inner truncated

    return "\n".join(lines)


# ── Per-source workers ──────────────────────────────────────────────────


def _safe_run(source_id: str, config: dict, data_dir: Path, user_dir: Path,
              query: str, limit: int) -> tuple[list[dict], str]:
    """Run a single source; never raise. Returns (hits, error_msg)."""
    fn = _DISPATCH.get(source_id)
    if not fn:
        return [], ""
    try:
        hits = fn(config, data_dir, user_dir, query, limit) or []
        return hits[:limit], ""
    except Exception as e:
        log.warning("universal_search: %s failed: %s", source_id, e)
        return [], f"{type(e).__name__}: {e}"


# ── Local per-user sources ──────────────────────────────────────────────


def _run_contacts(config, data_dir, user_dir, query, limit) -> list[dict]:
    from modules import contacts
    hits = contacts.search(user_dir, query) or []
    out = []
    for c in hits[:limit]:
        name = c.get("name") or "(no name)"
        company = c.get("company") or ""
        bits = []
        if c.get("phone"):   bits.append(c["phone"])
        if c.get("email"):   bits.append(c["email"])
        if c.get("notes"):   bits.append(c["notes"])
        snippet = " · ".join(b for b in bits if b)[:200]
        title = f"{name} ({company})" if company else name
        out.append({
            "title":        title,
            "snippet":      snippet,
            "source":       "contacts",
            "source_label": SOURCE_LABELS["contacts"],
            "link":         f"/owner#contacts?id={c.get('id','')}",
            "ts":           c.get("last_contact") or c.get("ts") or "",
        })
    return out


def _run_calendar(config, data_dir, user_dir, query, limit) -> list[dict]:
    from modules import calendar as cal
    q = query.lower()
    events = cal.list_all(user_dir) or []
    hits = []
    for e in events:
        haystack = " ".join([
            e.get("title", "") or "",
            e.get("notes", "") or "",
            e.get("location", "") or "",
            " ".join(e.get("with", []) or []),
        ]).lower()
        if q in haystack:
            hits.append(e)
    hits.sort(key=lambda x: x.get("start", ""), reverse=True)
    out = []
    for e in hits[:limit]:
        title = e.get("title") or "(untitled event)"
        when = (e.get("start") or "")[:16].replace("T", " ")
        loc = e.get("location") or ""
        snippet_bits = [when]
        if loc:                snippet_bits.append(f"@ {loc}")
        if e.get("notes"):     snippet_bits.append(e["notes"])
        snippet = " · ".join(b for b in snippet_bits if b)[:200]
        out.append({
            "title":        title,
            "snippet":      snippet,
            "source":       "calendar",
            "source_label": SOURCE_LABELS["calendar"],
            "link":         f"/owner#calendar?id={e.get('id','')}",
            "ts":           e.get("start") or "",
        })
    return out


def _run_tasks(config, data_dir, user_dir, query, limit) -> list[dict]:
    from modules import tasks
    hits = tasks.search(user_dir, query) or []
    out = []
    for t in hits[:limit]:
        text = t.get("text") or "(empty task)"
        status = t.get("status") or "open"
        tags = ", ".join(t.get("tags") or [])
        bits = [f"[{status}]"]
        if tags: bits.append(tags)
        out.append({
            "title":        text[:80],
            "snippet":      " ".join(bits)[:200],
            "source":       "tasks",
            "source_label": SOURCE_LABELS["tasks"],
            "link":         f"/owner#tasks?id={t.get('id','')}",
            "ts":           t.get("done_at") or t.get("ts") or "",
        })
    return out


def _run_reminders(config, data_dir, user_dir, query, limit) -> list[dict]:
    from modules import reminders
    q = query.lower()
    items = reminders.list_all(user_dir, include_done=True) or []
    hits = [r for r in items
            if q in (r.get("text", "") or "").lower()]
    out = []
    for r in hits[:limit]:
        text = r.get("text") or "(empty)"
        due = (r.get("due") or "")[:16].replace("T", " ")
        status = r.get("status") or "pending"
        snippet = f"due {due} · {status}"
        out.append({
            "title":        text[:80],
            "snippet":      snippet[:200],
            "source":       "reminders",
            "source_label": SOURCE_LABELS["reminders"],
            "link":         f"/owner#reminders?id={r.get('id','')}",
            "ts":           r.get("due") or "",
        })
    return out


# ── Local shared sources ────────────────────────────────────────────────


def _run_notes(config, data_dir, user_dir, query, limit) -> list[dict]:
    from modules import notes
    hits = notes.search(data_dir, query) or []
    out = []
    for n in hits[:limit]:
        content = (n.get("content") or "").strip()
        tags = ", ".join(n.get("tags") or [])
        title = (content.split("\n", 1)[0])[:80] or "(empty note)"
        snippet = content[:200]
        if tags:
            snippet = f"[{tags}] {snippet}"[:200]
        # notes.ts is a unix float — keep as-is for sorting downstream
        out.append({
            "title":        title,
            "snippet":      snippet,
            "source":       "notes",
            "source_label": SOURCE_LABELS["notes"],
            "link":         f"/owner#notes?id={n.get('id','')}",
            "ts":           n.get("ts") or "",
        })
    return out


def _run_messages(config, data_dir, user_dir, query, limit) -> list[dict]:
    from modules import messages
    q = query.lower()
    items = messages.list_all(data_dir, limit=500) or []
    hits = []
    for m in items:
        haystack = " ".join([
            m.get("from_name", "") or "",
            m.get("from_phone", "") or "",
            m.get("from_email", "") or "",
            m.get("body", "") or "",
            m.get("type", "") or "",
        ]).lower()
        if q in haystack:
            hits.append(m)
    out = []
    for m in hits[:limit]:
        who = m.get("from_name") or m.get("from_phone") or m.get("from_email") or "(unknown)"
        msg_type = m.get("type") or "message"
        title = f"{msg_type.title()} from {who}"
        out.append({
            "title":        title,
            "snippet":      (m.get("body") or "")[:200],
            "source":       "messages",
            "source_label": SOURCE_LABELS["messages"],
            "link":         f"/owner#messages?id={m.get('id','')}",
            "ts":           m.get("timestamp") or "",
        })
    return out


def _run_workspace(config, data_dir, user_dir, query, limit) -> list[dict]:
    from modules import workspace
    hits = workspace.search(config, data_dir, query, limit=limit) or []
    out = []
    for h in hits[:limit]:
        fn = h.get("filename") or h.get("path") or "(file)"
        chunk = (h.get("chunk") or "").strip()
        out.append({
            "title":        fn,
            "snippet":      chunk[:200],
            "source":       "workspace",
            "source_label": SOURCE_LABELS["workspace"],
            "link":         f"/owner#workspace?path={h.get('path','')}",
            "ts":           "",
            "score":        h.get("score"),
        })
    return out


def _run_catalog(config, data_dir, user_dir, query, limit) -> list[dict]:
    from modules import catalog
    hits = catalog.search(data_dir, query, limit=limit) or []
    out = []
    for h in hits[:limit]:
        name = h.get("name") or "(unnamed item)"
        sku = h.get("sku") or ""
        title = f"{name} · #{sku}" if sku else name
        bits = []
        if h.get("price") is not None:
            try:
                bits.append(f"${float(h['price']):.2f}")
            except (TypeError, ValueError):
                pass
        if h.get("stock") is not None:
            bits.append(f"{h['stock']} in stock")
        if h.get("brand"):    bits.append(h["brand"])
        if h.get("category"): bits.append(h["category"])
        if h.get("description"):
            bits.append(h["description"])
        out.append({
            "title":        title[:80],
            "snippet":      " · ".join(b for b in bits if b)[:200],
            "source":       "catalog",
            "source_label": SOURCE_LABELS["catalog"],
            "link":         f"/owner#catalog?sku={sku}",
            "ts":           "",
            "score":        h.get("score"),
        })
    return out


def _run_learned(config, data_dir, user_dir, query, limit) -> list[dict]:
    from modules import learning_loop
    q = query.lower()
    out = []
    # 1. Verified learned answers
    learned = learning_loop.list_learned(data_dir) or []
    for l in learned:
        hay = " ".join([
            l.get("question", "") or "",
            l.get("answer", "") or "",
        ]).lower()
        if q in hay:
            out.append({
                "title":        (l.get("question") or "(no question)")[:80],
                "snippet":      (l.get("answer") or "")[:200],
                "source":       "learned",
                "source_label": SOURCE_LABELS["learned"],
                "link":         f"/owner#learning?token={l.get('token','')}",
                "ts":           l.get("answered_at") or "",
            })
            if len(out) >= limit:
                return out
    # 2. Pending — still useful context for the owner
    pending = learning_loop.list_pending(data_dir) or []
    for p in pending:
        hay = (p.get("question") or "").lower()
        if q in hay:
            out.append({
                "title":        f"(pending) {p.get('question','')}"[:80],
                "snippet":      f"awaiting answer · asked_count={p.get('asked_count',1)}"[:200],
                "source":       "learned",
                "source_label": SOURCE_LABELS["learned"],
                "link":         f"/owner#learning?token={p.get('token','')}",
                "ts":           p.get("asked_at") or "",
            })
            if len(out) >= limit:
                break
    return out[:limit]


def _run_business(config, data_dir, user_dir, query, limit) -> list[dict]:
    """Substring search across every leaf string in the business_info dict.
    Flattens nested dicts/lists so address.city, faq[i].q, etc. all match."""
    from modules import business_info
    info = business_info.load(data_dir) or {}
    q = query.lower()
    hits: list[dict] = []
    for path, value in _walk_values(info, ""):
        s = "" if value is None else str(value)
        if not s:
            continue
        if q in s.lower():
            hits.append({
                "title":        path or "business",
                "snippet":      s[:200],
                "source":       "business",
                "source_label": SOURCE_LABELS["business"],
                "link":         "/owner#business",
                "ts":           "",
            })
            if len(hits) >= limit:
                break
    return hits


def _walk_values(obj: Any, prefix: str):
    """Yield (dotted_path, leaf_value) for every primitive leaf in a
    nested dict/list. Skips empty containers."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{prefix}.{k}" if prefix else k
            yield from _walk_values(v, sub)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            sub = f"{prefix}[{i}]"
            yield from _walk_values(v, sub)
    else:
        yield prefix, obj


# ── Connector sources (only run if connected) ──────────────────────────


def _connector_instance(connector_id: str, config: dict, user_dir: Path):
    """Return a connected connector instance, or None if not connected
    or not installed. Tolerates the connector module being absent."""
    try:
        from connectors.base import get_instance, import_all
        # Make sure registry is populated (safe to call repeatedly).
        import_all()
        inst = get_instance(connector_id, config, user_dir)
    except Exception as e:
        log.warning("universal_search: cannot instantiate %s: %s",
                    connector_id, e)
        return None
    if inst is None:
        return None
    try:
        if not inst.is_connected():
            return None
    except Exception:
        return None
    return inst


def _run_gmail(config, data_dir, user_dir, query, limit) -> list[dict]:
    inst = _connector_instance("gmail", config, user_dir)
    if inst is None:
        return []
    msgs = inst.search(query, limit=limit) or []
    out = []
    for m in msgs[:limit]:
        subj = m.get("subject") or "(no subject)"
        frm = m.get("from") or ""
        snippet_bits = []
        if frm:                  snippet_bits.append(f"from {frm}")
        if m.get("snippet"):     snippet_bits.append(m["snippet"])
        out.append({
            "title":        subj[:80],
            "snippet":      " — ".join(snippet_bits)[:200],
            "source":       "gmail",
            "source_label": SOURCE_LABELS["gmail"],
            "link":         f"https://mail.google.com/mail/u/0/#inbox/{m.get('id','')}",
            "ts":           m.get("date") or "",
        })
    return out


def _run_outlook(config, data_dir, user_dir, query, limit) -> list[dict]:
    inst = _connector_instance("outlook", config, user_dir)
    if inst is None:
        return []
    msgs = inst.search(query, limit=limit) or []
    out = []
    for m in msgs[:limit]:
        subj = m.get("subject") or "(no subject)"
        frm = m.get("from") or m.get("from_name") or ""
        snippet_bits = []
        if frm:                  snippet_bits.append(f"from {frm}")
        if m.get("snippet"):     snippet_bits.append(m["snippet"])
        out.append({
            "title":        subj[:80],
            "snippet":      " — ".join(snippet_bits)[:200],
            "source":       "outlook",
            "source_label": SOURCE_LABELS["outlook"],
            "link":         f"https://outlook.office.com/mail/inbox/id/{m.get('id','')}",
            "ts":           m.get("received_iso") or "",
        })
    return out


def _run_notion(config, data_dir, user_dir, query, limit) -> list[dict]:
    inst = _connector_instance("notion", config, user_dir)
    if inst is None:
        return []
    payload = inst.search(query, limit=limit) or {}
    pages = payload.get("results") or []
    out = []
    for p in pages[:limit]:
        title = p.get("title") or "(untitled page)"
        out.append({
            "title":        title[:80],
            "snippet":      (p.get("url") or "")[:200],
            "source":       "notion",
            "source_label": SOURCE_LABELS["notion"],
            "link":         p.get("url") or "",
            "ts":           p.get("last_edited_time") or p.get("created_time") or "",
        })
    return out


def _run_slack(config, data_dir, user_dir, query, limit) -> list[dict]:
    inst = _connector_instance("slack", config, user_dir)
    if inst is None:
        return []
    payload = inst.search_messages(query, limit=limit) or {}
    msgs = payload.get("messages") or []
    out = []
    for m in msgs[:limit]:
        who = m.get("username") or m.get("user") or "(unknown)"
        channel = m.get("channel") or ""
        text = m.get("text") or ""
        title = f"#{channel} · {who}" if channel else who
        out.append({
            "title":        title[:80],
            "snippet":      text[:200],
            "source":       "slack",
            "source_label": SOURCE_LABELS["slack"],
            "link":         m.get("permalink") or "",
            "ts":           m.get("ts") or "",
        })
    return out


# ── Dispatch table ─────────────────────────────────────────────────────


_DISPATCH = {
    "contacts":  _run_contacts,
    "calendar":  _run_calendar,
    "tasks":     _run_tasks,
    "reminders": _run_reminders,
    "notes":     _run_notes,
    "messages":  _run_messages,
    "workspace": _run_workspace,
    "catalog":   _run_catalog,
    "learned":   _run_learned,
    "business":  _run_business,
    "gmail":     _run_gmail,
    "outlook":   _run_outlook,
    "notion":    _run_notion,
    "slack":     _run_slack,
}


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# GET /api/owner/search?q=joe&limit=5
#     cookie-authed (owner or staff)
#     calls:    universal_search.search(config, data_dir, user_dir, q, limit)
#     returns:  the full result dict (see search() docstring)
#     errors:   400 if q is empty / missing
#
# The chat handler can also call universal_search.to_chat_context(results)
# directly when the owner asks open-ended questions ("tell me everything
# about Joe Smith") to inject every-source context into the system prompt.
