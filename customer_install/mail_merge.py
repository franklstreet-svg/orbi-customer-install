"""
mail_merge — generate per-recipient documents from a single template.

The owner writes ONE template (a letter, a thank-you note, an estimate body)
with `{{placeholders}}` like `{{first_name}}` or `{{contact.email}}`. They
pick a list of contacts. Mail-merge renders one document per contact, packs
them all into a single zip, and the owner downloads the archive.

Two modes:
    deterministic (default) — pure string substitution. Fast. Predictable.
                              No surprises, no hallucinated content.
    llm_personalize=True    — after substitution, the LLM does light
                              sentence-level polish (does NOT invent
                              facts, names, dollar amounts, or dates).

This file is REUSE on top of:
    doc_convert.write_pdf / write_docx / write_txt / write_md
    modules.contacts.list_all
    modules.workspace.workspace_path

Public API:
    parse_template(text) -> list[str]
    render_template(text, context) -> str
    merge_one(text, contact, extras=None) -> str
    merge_all(config, user_dir, text, contact_ids, *, target_format, extras, llm_personalize) -> dict
    list_recent_merges(user_dir, limit=10) -> list[dict]
"""

from __future__ import annotations

import logging
import re
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger("orbi.mail_merge")

# Match {{ placeholder }} or {{ dotted.path }} — whitespace around the name is
# tolerated; the placeholder name itself may not contain whitespace or braces.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")

# Per-output filename sanitizer — strip anything that isn't word/space/dot/dash/parens.
_FILENAME_SAFE_RE = re.compile(r"[^\w\s.\-()]+")

# Lock around the zip-writing step (zipfile is not safe for concurrent writes).
_ZIP_LOCK = threading.Lock()

# How big a single rendered letter is allowed to be before we refuse to call
# the LLM polish step — keeps merges fast and predictable.
_MAX_POLISH_CHARS = 8_000


# ---------------------------------------------------------------------------
# Template parsing + rendering
# ---------------------------------------------------------------------------


def parse_template(template_text: str) -> list[str]:
    """Return placeholder names (deduped, in document order). Empty list if none."""
    if not template_text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(template_text):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _resolve_path(path: str, context: dict):
    """Walk a dotted path through nested dicts. Returns None if any segment misses."""
    cur = context
    for segment in path.split("."):
        if isinstance(cur, dict) and segment in cur:
            cur = cur[segment]
        else:
            return None
    return cur


def render_template(template_text: str, context: dict) -> str:
    """Substitute every `{{path}}` with the value from `context`.

    Supports dotted paths (`{{contact.email}}` -> `context["contact"]["email"]`).
    Missing or None values become empty strings (and we log a debug).
    Whitespace around the placeholder name is stripped.
    """
    if not template_text:
        return ""

    def _sub(match: re.Match) -> str:
        path = match.group(1).strip()
        value = _resolve_path(path, context)
        if value is None:
            log.debug("mail_merge: placeholder %r resolved to None/missing", path)
            return ""
        return str(value)

    return _PLACEHOLDER_RE.sub(_sub, template_text)


# ---------------------------------------------------------------------------
# Per-contact context + merge
# ---------------------------------------------------------------------------


def _split_name(full_name: str) -> tuple[str, str]:
    """Split a name into (first, last). 'Joe Smith' -> ('Joe', 'Smith').
    Single-word names: ('Joe', '')."""
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _today_human() -> str:
    """Today's date in a friendly human format, e.g. 'May 27, 2026'."""
    return datetime.now().strftime("%B %-d, %Y") if hasattr(datetime.now(), "strftime") else ""


def _build_context(contact: dict, extras: dict | None) -> dict:
    """Build the per-recipient context dict that the template renders against."""
    name = (contact.get("name") or "").strip()
    first, last = _split_name(name)
    extras = extras or {}

    ctx: dict = {
        "name":       name,
        "first_name": first,
        "last_name":  last,
        "email":      contact.get("email") or "",
        "phone":      contact.get("phone") or "",
        "company":    contact.get("company") or "",
        "contact":    contact,
        "today":      _today_human(),
        # Default sender/business from longer key names if caller used them
        "sender":     extras.get("sender_name", "") if extras else "",
        "business":   extras.get("business_name", "") if extras else "",
    }
    # Splat extras over the defaults — the caller's intent wins. This lets
    # the owner pass {"sender": "Frank"} OR {"sender_name": "Frank"} and
    # both work, AND lets extras override contact-derived defaults like
    # {{company}} when needed.
    for k, v in extras.items():
        # Reserved keys we always derive from the contact record
        if k in ("name", "first_name", "last_name", "contact"):
            continue
        ctx[k] = v
    return ctx


def merge_one(template_text: str, contact: dict,
              extras: dict | None = None) -> str:
    """Render the template for a single contact. Pure substitution — no LLM."""
    ctx = _build_context(contact, extras)
    return render_template(template_text, ctx)


# ---------------------------------------------------------------------------
# Optional LLM personalization (sentence-level polish only)
# ---------------------------------------------------------------------------


_POLISH_SYSTEM = """You are a careful editor doing LIGHT polish on a letter.

ABSOLUTE RULES:
1. Do NOT change any facts.
2. Do NOT change any names (people, businesses, products).
3. Do NOT change any dollar amounts, numbers, percentages, or quantities.
4. Do NOT change any dates, times, or addresses.
5. Do NOT add new sentences or new information.
6. Do NOT remove sentences.
7. Only fix awkward phrasing, smooth transitions, fix obvious grammar.
8. Keep the same structure, same tone, same length.

If the letter is already fine, return it nearly verbatim.
Output ONLY the polished letter — no preamble, no commentary, no backticks."""


def _llm_polish(config: dict, rendered: str) -> str:
    """Send the rendered letter through the LLM for light personalization.
    Returns the polished text, or the input unchanged on any failure.
    Safety: refuses to polish anything over `_MAX_POLISH_CHARS`."""
    if not rendered or not rendered.strip():
        return rendered
    if len(rendered) > _MAX_POLISH_CHARS:
        log.info("mail_merge: skipping LLM polish — letter is %d chars (limit %d)",
                 len(rendered), _MAX_POLISH_CHARS)
        return rendered
    try:
        import llm_client
        resp = llm_client.generate(config, _POLISH_SYSTEM,
                                   [{"role": "user", "content": rendered}])
        out = (resp.text or "").strip()
        if not out:
            return rendered
        # Strip common LLM preambles defensively.
        out = re.sub(r"^(here(?:'s| is) (?:the )?(?:polished|edited)[^\n:]*:?\s*\n)",
                     "", out, flags=re.IGNORECASE)
        return out
    except Exception as e:
        log.warning("mail_merge: LLM polish failed, using raw render: %s", e)
        return rendered


# ---------------------------------------------------------------------------
# Filename + output-dir helpers
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str) -> str:
    """Replace anything not in [\\w\\s.\\-()] with '_', collapse runs, trim."""
    if not name:
        return "contact"
    cleaned = _FILENAME_SAFE_RE.sub("_", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned[:80] or "contact"


def _write_output(text: str, out_path: Path, target: str) -> Path:
    """Dispatch to the right doc_convert writer. Lazy import — reportlab/docx
    are heavy."""
    target = target.lower().strip()
    if target == "pdf":
        from doc_convert import write_pdf
        return write_pdf(text, out_path, title=out_path.stem)
    if target == "docx":
        from doc_convert import write_docx
        return write_docx(text, out_path, title=out_path.stem)
    if target == "md":
        from doc_convert import write_md
        return write_md(text, out_path)
    if target == "txt":
        from doc_convert import write_txt
        return write_txt(text, out_path)
    raise ValueError(f"unsupported mail_merge target {target!r} — "
                     "use pdf, docx, txt, or md")


# ---------------------------------------------------------------------------
# The big one — full mail merge
# ---------------------------------------------------------------------------


def merge_all(config: dict, user_dir: Path, template_text: str,
              contact_ids: list[str], *, target_format: str = "pdf",
              extras: dict | None = None,
              llm_personalize: bool = False) -> dict:
    """Run a full mail merge.

    For each contact_id: look it up, substitute, optionally polish via LLM,
    render to the chosen format, save into a per-run folder, then zip them all.

    Returns a dict with output_dir, zip_path, merged list, and errors list.
    """
    target_format = (target_format or "pdf").lower().strip()
    if target_format not in ("pdf", "docx", "txt", "md"):
        raise ValueError(f"target_format must be pdf/docx/txt/md, got {target_format!r}")

    from modules.workspace import workspace_path
    from modules.contacts import list_all

    ws = workspace_path(config)
    ws.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = ws / f"mail_merge_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build the contact lookup once.
    all_contacts = list_all(Path(user_dir))
    by_id = {c.get("id"): c for c in all_contacts if c.get("id")}

    merged: list[dict] = []
    errors: list[dict] = []

    for cid in (contact_ids or []):
        contact = by_id.get(cid)
        if not contact:
            errors.append({"contact_id": cid, "error": "contact not found"})
            log.warning("mail_merge: contact_id %r not found in user_dir", cid)
            continue
        try:
            rendered = merge_one(template_text, contact, extras=extras)
            if llm_personalize:
                rendered = _llm_polish(config, rendered)

            base = _sanitize_filename(contact.get("name") or cid)
            ext = "." + target_format
            out_path = out_dir / f"{base}{ext}"
            # Avoid clobbering if two contacts share a name.
            n = 2
            while out_path.exists():
                out_path = out_dir / f"{base} ({n}){ext}"
                n += 1

            _write_output(rendered, out_path, target_format)

            merged.append({
                "contact_id": cid,
                "name":       contact.get("name", ""),
                "filename":   out_path.name,
                "size":       out_path.stat().st_size,
            })
        except Exception as e:
            log.exception("mail_merge: failed for contact_id %r", cid)
            errors.append({"contact_id": cid, "error": str(e)})

    # Zip all the per-recipient files into one archive in the workspace root.
    zip_path = ws / f"mail_merge_{timestamp}.zip"
    with _ZIP_LOCK:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(out_dir.iterdir()):
                if f.is_file():
                    zf.write(f, arcname=f.name)

    log.info("mail_merge: rendered %d files into %s (errors=%d)",
             len(merged), out_dir, len(errors))

    return {
        "output_dir": str(out_dir),
        "zip_path":   str(zip_path),
        "merged":     merged,
        "errors":     errors,
    }


# ---------------------------------------------------------------------------
# Browse recent runs
# ---------------------------------------------------------------------------


def list_recent_merges(user_dir: Path, limit: int = 10) -> list[dict]:
    """Return the most-recent mail_merge_*.zip files in the workspace.

    `user_dir` is accepted for API symmetry with the other functions, but the
    zips live in the workspace (per-owner, not per-user-dir). We import
    workspace_path with no config — that falls back to ~/Orbi which is the
    same place merge_all wrote them when no override was set.
    """
    from modules.workspace import workspace_path
    ws = workspace_path(None)
    if not ws.exists():
        return []
    out: list[dict] = []
    for p in ws.glob("mail_merge_*.zip"):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "name":    p.name,
            "path":    str(p),
            "size":    st.st_size,
            "mtime":   st.st_mtime,
        })
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out[: max(0, int(limit or 10))]


# ---------------------------------------------------------------------------
# Routes the orbi.py owner-API layer should expose (NOT wired here — orbi.py
# is not modified by this file). Reference for whoever wires the endpoints:
#
#   POST /api/owner/mail_merge/preview
#       body: { "template": str, "contact_id": str, "extras": {...} (opt) }
#       -> render ONE document for preview (text only, no file written)
#       impl: contact = next(c for c in list_all(user_dir) if c["id"]==contact_id)
#             return {"rendered": merge_one(template, contact, extras)}
#
#   POST /api/owner/mail_merge/run
#       body: { "template": str, "contact_ids": [str,...],
#               "target_format": "pdf"|"docx"|"txt"|"md" (default "pdf"),
#               "extras": {...} (opt),
#               "llm_personalize": bool (default false) }
#       -> full run; return {
#              "zip_url":  "/api/files/<token>",   # via file_fetch.mint_download_token
#              "merged":   [...],
#              "errors":   [...],
#          }
#       impl: result = mail_merge.merge_all(config, user_dir, template,
#                                           contact_ids, target_format=...,
#                                           extras=..., llm_personalize=...)
#             token = file_fetch.mint_download_token(DATA_DIR, result["zip_path"])
#             result["zip_url"] = f"/api/files/{token}"
#             return result
#
#   GET  /api/owner/mail_merge/templates
#       -> list templates the owner has saved in the workspace
#       impl: workspace_path(config).glob("*.merge.txt") — return name + size + mtime
#
#   GET  /api/owner/mail_merge/recent
#       -> list_recent_merges(user_dir, limit=10)
# ---------------------------------------------------------------------------
