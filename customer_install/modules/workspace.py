"""
workspace module — files the owner drops into ~/Orbi/.

The folder is plain old filesystem. Owner drops PDFs, text, Word docs, photos,
etc. We index their content (text only — images get filename indexed) and
make matches available as context to LLM queries.

Lightweight RAG: keyword search over indexed chunks, no vector DB needed for
the volume one small business produces. Each file is re-indexed when its mtime
changes. Pruning runs lazily on each list call.

Supported:
  .txt .md .csv           → read as utf-8
  .pdf                    → text via pypdf if available, else filename only
  .docx                   → text via python-docx if available, else filename only
  .png .jpg .jpeg .gif    → filename indexed (no OCR in v1)

Anything else: filename indexed.

The workspace folder defaults to ~/Orbi/ but is overridable via
config["workspace"]["path"].
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("orbi.workspace")
_LOCK = threading.Lock()

# Optional deps — code degrades gracefully if missing
try:
    import pypdf
    HAS_PDF = True
except ImportError:
    HAS_PDF = False
try:
    import docx
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False
try:
    import openpyxl
    HAS_XLSX = True
except ImportError:
    HAS_XLSX = False


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def workspace_path(config: dict | None = None) -> Path:
    if config and (config.get("workspace") or {}).get("path"):
        return Path(config["workspace"]["path"]).expanduser()
    # Default: ~/Orbi/ — easy for the owner to find
    return Path.home() / "Orbi"

def index_path(data_dir: Path) -> Path:
    return data_dir / "workspace_index.json"

# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

MAX_CHUNK_CHARS = 800   # one entry per ~800 chars of text
MAX_FILE_CHARS  = 60_000  # cap per-file to keep memory reasonable

def _load_index(data_dir: Path) -> dict:
    p = index_path(data_dir)
    if not p.exists():
        return {"files": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"files": {}}

def _save_index(data_dir: Path, index: dict) -> None:
    p = index_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
    tmp.replace(p)

def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in (".txt", ".md", ".csv", ".log", ".json", ".html", ".htm"):
            t = path.read_text(encoding="utf-8", errors="ignore")[:MAX_FILE_CHARS]
            # Cheap HTML tag stripping so indexing isn't drowned in markup
            if suffix in (".html", ".htm"):
                import re as _re
                t = _re.sub(r"<script[\s\S]*?</script>", " ", t, flags=_re.IGNORECASE)
                t = _re.sub(r"<style[\s\S]*?</style>", " ", t, flags=_re.IGNORECASE)
                t = _re.sub(r"<[^>]+>", " ", t)
                t = _re.sub(r"\s+", " ", t).strip()
            return t
        if suffix == ".pdf" and HAS_PDF:
            text = []
            with path.open("rb") as f:
                reader = pypdf.PdfReader(f)
                for page in reader.pages:
                    try:
                        text.append(page.extract_text() or "")
                    except Exception:
                        continue
                    if sum(len(t) for t in text) > MAX_FILE_CHARS:
                        break
            return "\n".join(text)[:MAX_FILE_CHARS]
        if suffix == ".docx" and HAS_DOCX:
            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs)[:MAX_FILE_CHARS]
        if suffix == ".xlsx" and HAS_XLSX:
            wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
            chunks = []
            for sheet in wb.worksheets:
                chunks.append(f"## Sheet: {sheet.title}")
                for row in sheet.iter_rows(values_only=True):
                    row_text = " | ".join(str(c) if c is not None else "" for c in row)
                    if row_text.strip(" |"):
                        chunks.append(row_text)
                    if sum(len(c) for c in chunks) > MAX_FILE_CHARS:
                        break
                if sum(len(c) for c in chunks) > MAX_FILE_CHARS:
                    break
            return "\n".join(chunks)[:MAX_FILE_CHARS]
    except Exception as e:
        log.warning(f"could not extract text from {path.name}: {e}")
    return ""

def _chunk(text: str) -> list[str]:
    """Split text into topical chunks.
    For markdown files: split on ## headers, so each section is its own chunk.
    For other files: split on character count (with respect for line boundaries)."""
    if not text:
        return []

    # If the text looks like Markdown with section headers, split on them
    if "\n## " in text or text.startswith("## "):
        return _chunk_markdown(text)
    return _chunk_by_size(text)


def _chunk_markdown(text: str) -> list[str]:
    """Split a markdown doc at ## section boundaries. Each chunk includes its
    section header so the model knows the context."""
    sections = []
    current = []
    for line in text.splitlines():
        if line.startswith("## ") and current:
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        last = "\n".join(current).strip()
        if last:
            sections.append(last)

    # Further split any oversized section
    out = []
    for s in sections:
        if len(s) <= MAX_CHUNK_CHARS:
            out.append(s)
        else:
            # Split with the header preserved at the top of each sub-chunk
            header = s.splitlines()[0]
            sub = _chunk_by_size(s[len(header) + 1:])
            for i, piece in enumerate(sub):
                if i == 0:
                    out.append(header + "\n" + piece)
                else:
                    out.append(f"{header} (cont.)\n{piece}")
    return out


def _chunk_by_size(text: str) -> list[str]:
    chunks = []
    current = []
    cur_len = 0
    for line in text.splitlines():
        line = line.rstrip()
        if cur_len + len(line) > MAX_CHUNK_CHARS and current:
            chunks.append("\n".join(current).strip())
            current = []
            cur_len = 0
        current.append(line)
        cur_len += len(line) + 1
    if current:
        last = "\n".join(current).strip()
        if last:
            chunks.append(last)
    return chunks

def scan(config: dict, data_dir: Path) -> dict:
    """Walk the workspace folder, re-index any changed files. Returns summary."""
    ws = workspace_path(config)
    ws.mkdir(parents=True, exist_ok=True)

    with _LOCK:
        index = _load_index(data_dir)
        files_map = index.setdefault("files", {})

        seen = set()
        added = updated = 0
        for path in ws.rglob("*"):
            if not path.is_file():
                continue
            if path.name.startswith("."):
                continue
            key = str(path.relative_to(ws))
            seen.add(key)
            mtime = path.stat().st_mtime
            entry = files_map.get(key)
            if entry and entry.get("mtime") == mtime:
                continue
            text = _extract_text(path)
            chunks = _chunk(text) if text else []
            files_map[key] = {
                "mtime": mtime,
                "size": path.stat().st_size,
                "ext": path.suffix.lower(),
                "chunks": chunks,
                "filename": path.name,
            }
            if entry: updated += 1
            else:     added += 1

        # Prune deleted files
        removed = 0
        for key in list(files_map.keys()):
            if key not in seen:
                del files_map[key]
                removed += 1

        index["last_scan"] = time.time()
        _save_index(data_dir, index)

    return {
        "added": added, "updated": updated, "removed": removed,
        "total_files": len(files_map),
        "workspace": str(ws),
    }

def list_files(config: dict, data_dir: Path) -> list[dict]:
    with _LOCK:
        index = _load_index(data_dir)
    items = []
    for key, e in index.get("files", {}).items():
        chunks = e.get("chunks", []) or []
        size = e.get("size", 0)
        # Chunks may be stored as strings (current) or dicts (legacy)
        indexed_chars = sum(
            len(c) if isinstance(c, str) else len(c.get("text", "") if isinstance(c, dict) else "")
            for c in chunks
        )
        items.append({
            "path":          key,
            "filename":      e.get("filename", key),  # legacy
            "name":          e.get("filename", key),  # dashboard expects 'name'
            "size":          size,
            "size_kb":       size // 1024,
            "ext":           e.get("ext", ""),
            "chunks":        len(chunks),
            "indexed_chars": indexed_chars,
            "modified":      e.get("mtime"),
            "mtime":         e.get("mtime"),
        })
    items.sort(key=lambda i: i.get("modified", 0) or 0, reverse=True)
    return items

# Synonyms — when a query uses these words, also search for the corresponding
# domain words. Helps with "what's special right now" matching "promotion" file.
_SYNONYMS = {
    "special":    ["special", "promo", "promotion", "deal", "offer", "discount", "sale"],
    "promo":      ["promo", "promotion", "special", "deal", "offer"],
    "promotion":  ["promotion", "promo", "special", "deal", "offer"],
    "deal":       ["deal", "special", "promo", "promotion", "offer"],
    "offer":      ["offer", "deal", "special", "promo", "promotion"],
    "discount":   ["discount", "deal", "special", "promo", "sale"],
    "sale":       ["sale", "deal", "special", "promo", "discount"],
    "menu":       ["menu", "food", "items", "dishes"],
    "hours":      ["hours", "schedule", "open", "closed", "times"],
    "price":      ["price", "cost", "pricing", "fee", "rate"],
    "service":    ["service", "services", "offering", "offerings"],
    "current":    ["current", "today", "now", "recent", "latest"],
    "now":        ["now", "current", "today", "this week", "lately"],
}

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "do", "does", "did",
    "have", "has", "had", "what", "when", "where", "who", "how", "why",
    "right", "tell", "me", "i", "we", "you", "your", "my", "our", "us",
    "can", "could", "would", "should", "will", "shall", "may", "might",
    "to", "of", "in", "on", "at", "for", "with", "about", "from", "by",
}

def _expand_terms(query: str) -> list[str]:
    """Pull meaningful terms from query, expanded with synonyms."""
    q = (query or "").lower().strip()
    raw = [t.strip(".,?!\"'") for t in q.split() if len(t) > 2]
    raw = [t for t in raw if t and t not in _STOPWORDS]
    expanded = set(raw)
    for t in raw:
        for syn in _SYNONYMS.get(t, []):
            expanded.add(syn)
    return list(expanded)

def search(config: dict, data_dir: Path, query: str, limit: int = 5) -> list[dict]:
    """Keyword search across indexed chunks. Returns list of {path, chunk, score}.
    Smart matching: stopwords removed, synonyms expanded so 'what's special right now'
    matches a 'promotion' file."""
    terms = _expand_terms(query)
    if not terms:
        # Last-ditch: use the raw query as a single term
        q = (query or "").lower().strip()
        if not q:
            return []
        terms = [q]

    with _LOCK:
        index = _load_index(data_dir)

    results = []
    for key, e in index.get("files", {}).items():
        chunks = e.get("chunks", [])
        filename_lower = e.get("filename", "").lower()
        for i, chunk in enumerate(chunks):
            chunk_lower = chunk.lower()
            first_line = chunk.split("\n", 1)[0].lower()
            score = sum(chunk_lower.count(t) for t in terms)
            # Big bonus for matches in a section header (## Something).
            # Smaller bonus for the doc title (# Something) — those are less
            # topically specific.
            if first_line.startswith("## "):
                score += 8 * sum(1 for t in terms if t in first_line)
            elif first_line.startswith("# "):
                score += 2 * sum(1 for t in terms if t in first_line)
            else:
                score += 5 * sum(1 for t in terms if t in first_line)
            # Filename match bonus
            score += 3 * sum(1 for t in terms if t in filename_lower)
            if score > 0:
                results.append({
                    "path": key,
                    "chunk_index": i,
                    "chunk": chunk,
                    "filename": e.get("filename", key),
                    "score": score,
                })
        if not chunks and any(t in filename_lower for t in terms):
            results.append({
                "path": key,
                "chunk_index": -1,
                "chunk": f"(file: {e.get('filename', key)} — content not indexed)",
                "filename": e.get("filename", key),
                "score": 1,
            })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]

def context_block(config: dict, data_dir: Path, query: str,
                  max_chars: int = 800) -> str:
    """Build a prompt context block from the top matches for `query`.
    Kept short — small local LLMs choke on huge contexts."""
    matches = search(config, data_dir, query, limit=3)
    if not matches:
        return ""
    lines = ["RELEVANT FILES FROM OWNER'S WORKSPACE:"]
    total = len(lines[0])
    for m in matches:
        snippet = m["chunk"][:300]
        block = f'\n[{m["filename"]}]\n{snippet}\n'
        if total + len(block) > max_chars:
            break
        lines.append(block)
        total += len(block)
    return "".join(lines)


# ---------------------------------------------------------------------------
# Watcher (background polling — every 60s)
# ---------------------------------------------------------------------------

def start_watcher(config: dict, data_dir: Path) -> None:
    interval = (config.get("workspace") or {}).get("scan_interval_seconds", 60)

    def loop():
        while True:
            try:
                result = scan(config, data_dir)
                if result["added"] or result["updated"] or result["removed"]:
                    log.info(f"workspace scan: +{result['added']} "
                             f"~{result['updated']} -{result['removed']} "
                             f"({result['total_files']} total)")
            except Exception as e:
                log.warning(f"workspace scan failed: {e}")
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    log.info(f"workspace watcher started: {workspace_path(config)}")
