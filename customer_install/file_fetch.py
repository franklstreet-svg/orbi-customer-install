"""
file_fetch — remote file retrieval for the owner's away-from-desk workflow.

THE FEATURE
    The owner is out somewhere with their phone. They open the Orbi PWA
    (which points at their home computer through the Cloudflare tunnel),
    type or say "grab the Maxwell estimate from my Documents folder and
    send it to me", and within seconds get a one-tap download link to the
    file from their own machine.

    This module is the safe back-end for that. It:
      1. Maintains a per-install ALLOW-LIST of folders Orbi may read
         (default ~/Documents and ~/Desktop).
      2. Searches those folders for filenames matching the owner's query.
      3. Mints SINGLE-USE, time-limited download tokens.
      4. Redeems those tokens at the public /download/<token> route.
      5. Zips folders on demand so the owner can grab a whole project.

SECURITY MODEL
    - Everything is gated by a real-path containment check
      (_path_is_safe): the requested file must, after resolving symlinks
      and any "..", live INSIDE one of the configured allowed roots.
      This is the path-traversal defense.
    - The /api/owner/files/* routes are session-protected (owner only) —
      the orchestrator wires that up; this module trusts its callers
      because it's never invoked from a public route except
      redeem_token().
    - /download/<token> IS public (so the phone can grab the file
     without re-auth), but it only works for tokens that the owner
     just minted, exactly once, within the TTL window.
    - Blocked extensions (.key, .pem, .env) are NEVER searchable and
      NEVER tokenisable. There's no escape hatch for those even if the
      owner asks for them by name — the same scope config enforces it.

STORAGE LAYOUT
    <data_dir>/file_fetch_scope.json     allow-list + limits
    <data_dir>/file_fetch_tokens.json    active and recently-used tokens
    <data_dir>/.tmp_downloads/           ephemeral zips for folder fetch
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import secrets
import shutil
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("orbi.file_fetch")

SCOPE_FILE  = "file_fetch_scope.json"
TOKENS_FILE = "file_fetch_tokens.json"
TMP_DIR     = ".tmp_downloads"

DEFAULT_ALLOWED   = ["~/Documents", "~/Desktop"]
DEFAULT_MAX_MB    = 500
DEFAULT_BLOCKED   = [".key", ".pem", ".env"]
DEFAULT_TTL_MIN   = 10
SEARCH_FILE_CAP   = 5000   # safety: never walk more than this many entries

_TOKEN_LOCK = threading.Lock()
_SCOPE_LOCK = threading.Lock()


# ── Scope (allow-list) config ───────────────────────────────────────────


def load_scope(data_dir: Path) -> dict:
    """Return the scope config dict. Always has the three keys.

    Shape:
        {
          "allowed_paths":      ["~/Documents", "~/Desktop"],
          "max_file_mb":        500,
          "blocked_extensions": [".key", ".pem", ".env"],
        }
    """
    path = Path(data_dir) / SCOPE_FILE
    if not path.exists():
        return {
            "allowed_paths":      list(DEFAULT_ALLOWED),
            "max_file_mb":        DEFAULT_MAX_MB,
            "blocked_extensions": list(DEFAULT_BLOCKED),
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"scope read failed, using defaults: {e}")
        raw = {}
    return {
        "allowed_paths":      raw.get("allowed_paths") or list(DEFAULT_ALLOWED),
        "max_file_mb":        int(raw.get("max_file_mb") or DEFAULT_MAX_MB),
        "blocked_extensions": [e.lower() for e in (raw.get("blocked_extensions") or DEFAULT_BLOCKED)],
    }


def save_scope(data_dir: Path, scope: dict) -> None:
    """Atomic write of the scope config. Validates types; raises ValueError
    on bad input."""
    if not isinstance(scope, dict):
        raise ValueError("scope must be a dict")
    allowed = scope.get("allowed_paths")
    if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
        raise ValueError("allowed_paths must be a list of strings")
    max_mb = scope.get("max_file_mb", DEFAULT_MAX_MB)
    try:
        max_mb = int(max_mb)
    except (TypeError, ValueError):
        raise ValueError("max_file_mb must be an integer")
    blocked = scope.get("blocked_extensions", DEFAULT_BLOCKED)
    if not isinstance(blocked, list) or not all(isinstance(x, str) for x in blocked):
        raise ValueError("blocked_extensions must be a list of strings")

    clean = {
        "allowed_paths":      [str(x).strip() for x in allowed if str(x).strip()],
        "max_file_mb":        max_mb,
        "blocked_extensions": [x.lower().strip() for x in blocked if x.strip()],
    }
    path = Path(data_dir) / SCOPE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SCOPE_LOCK:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    log.info(f"scope updated: {len(clean['allowed_paths'])} paths, max {clean['max_file_mb']}MB")


def get_allowed_roots(data_dir: Path) -> list[Path]:
    """Resolve the configured allowed_paths to absolute Paths (expanding ~).
    Skips any path that doesn't exist or doesn't resolve."""
    scope = load_scope(data_dir)
    roots = []
    for raw in scope["allowed_paths"]:
        try:
            p = Path(raw).expanduser().resolve(strict=False)
            if p.exists():
                roots.append(p)
            else:
                log.debug(f"allowed root missing on disk, skipping: {raw}")
        except (OSError, RuntimeError) as e:
            log.warning(f"could not resolve allowed root {raw!r}: {e}")
    return roots


# ── Path-traversal guard ────────────────────────────────────────────────


def _path_is_safe(path: Path, allowed_roots: list[Path]) -> bool:
    """The core security check. Resolves `path` to its real, absolute
    location (following symlinks, collapsing '..') and returns True only
    if it sits INSIDE one of the allowed_roots.

    This defeats:
      - "../../etc/passwd" style traversal
      - symlinks that point outside the allowed tree
      - absolute paths to anywhere on disk

    Empty allowed_roots → always False (fail closed).
    """
    if not allowed_roots:
        return False
    try:
        real = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    for root in allowed_roots:
        try:
            root_real = Path(root).resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        try:
            if real == root_real or real.is_relative_to(root_real):
                return True
        except AttributeError:
            # is_relative_to is 3.9+; fall back to string-prefix check
            try:
                real.relative_to(root_real)
                return True
            except ValueError:
                continue
    return False


# ── Search ──────────────────────────────────────────────────────────────


def search(data_dir: Path, query: str, limit: int = 10) -> list[dict]:
    """Walk the allowed roots, case-insensitively substring-match `query`
    against filenames, return up to `limit` hits.

    Each hit:
        {
          "path":          "/home/frank/Documents/Maxwell Estimate.pdf",
          "name":          "Maxwell Estimate.pdf",
          "size_bytes":    123456,
          "modified_iso":  "2026-05-26T14:30:00Z",
          "parent_folder": "/home/frank/Documents",
          "is_dir":        false,
          "oversized":     false   # only present when true
        }

    Rules:
      - Skip dotfiles (any path component starting with ".")
      - Skip blocked extensions entirely (never shown to caller)
      - Files > max_file_mb get oversized=true but ARE returned so the
        owner sees them and can adjust the cap
      - Caps the walk at SEARCH_FILE_CAP entries scanned (5000) for safety
    """
    q = (query or "").strip().lower()
    if not q:
        return []

    scope        = load_scope(data_dir)
    roots        = get_allowed_roots(data_dir)
    max_bytes    = scope["max_file_mb"] * 1024 * 1024
    blocked_exts = set(scope["blocked_extensions"])

    if not roots:
        log.info("search called but no allowed roots are reachable")
        return []

    hits: list[dict] = []
    scanned = 0

    for root in roots:
        if len(hits) >= limit or scanned >= SEARCH_FILE_CAP:
            break
        try:
            for entry in root.rglob("*"):
                scanned += 1
                if scanned >= SEARCH_FILE_CAP:
                    log.info(f"search hit scan cap of {SEARCH_FILE_CAP}")
                    break
                if len(hits) >= limit:
                    break

                # Skip dotfiles / dotdirs anywhere in the path tail
                try:
                    rel = entry.relative_to(root)
                except ValueError:
                    continue
                if any(part.startswith(".") for part in rel.parts):
                    continue

                name_lower = entry.name.lower()

                # Blocked extension → invisible
                ext = entry.suffix.lower()
                if ext in blocked_exts:
                    continue

                if q not in name_lower:
                    continue

                # Stat may fail for broken symlinks etc — skip rather than crash
                try:
                    st = entry.stat()
                except OSError:
                    continue

                # Final containment check — defends against symlinks the
                # walker might have followed into forbidden territory.
                if not _path_is_safe(entry, roots):
                    continue

                is_dir = entry.is_dir()
                size   = 0 if is_dir else st.st_size
                hit = {
                    "path":          str(entry.resolve(strict=False)),
                    "name":          entry.name,
                    "size_bytes":    size,
                    "modified_iso":  datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "parent_folder": str(entry.parent.resolve(strict=False)),
                    "is_dir":        is_dir,
                }
                if not is_dir and size > max_bytes:
                    hit["oversized"] = True
                hits.append(hit)
        except (OSError, PermissionError) as e:
            log.warning(f"walk error under {root}: {e}")
            continue

    log.info(f"search q={q!r} → {len(hits)} hits ({scanned} entries scanned)")
    return hits


# ── Token mint / redeem ─────────────────────────────────────────────────


def _load_tokens(data_dir: Path) -> dict:
    path = Path(data_dir) / TOKENS_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"tokens read failed, resetting: {e}")
        return {}


def _save_tokens(data_dir: Path, tokens: dict) -> None:
    path = Path(data_dir) / TOKENS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _prune_tokens(tokens: dict) -> dict:
    """Drop tokens older than 24h regardless of state — keeps the file tidy."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    kept = {}
    for tok, rec in tokens.items():
        try:
            exp = datetime.strptime(rec.get("expires_at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if exp >= cutoff:
            kept[tok] = rec
    return kept


def mint_download_token(data_dir: Path, path: str, ttl_minutes: int = DEFAULT_TTL_MIN,
                        extra_allowed_roots: list[Path] | None = None) -> str:
    """Create a single-use, time-limited token bound to `path`.

    Validates that `path` is inside an allowed root (or under the tmp
    downloads dir, which is implicitly allowed because we just wrote it
    there ourselves). Callers like the doc-convert pipeline pass their own
    `extra_allowed_roots` (e.g. the workspace folder) so files Orbi just
    created on the owner's behalf can be downloaded without forcing the
    owner to widen their global file-fetch scope. Raises ValueError otherwise.

    Returns the hex token string. Caller hands that to the user as
    /download/<token>.
    """
    target = Path(path).expanduser().resolve(strict=False)
    if not target.exists():
        raise ValueError(f"path does not exist: {path}")

    tmp_root = (Path(data_dir) / TMP_DIR).resolve(strict=False)
    extra = [Path(p).expanduser().resolve(strict=False) for p in (extra_allowed_roots or [])]
    allowed = get_allowed_roots(data_dir) + [tmp_root] + extra
    if not _path_is_safe(target, allowed):
        log.warning(f"mint refused — outside scope: {target}")
        raise ValueError("path is outside the configured allowed scope")

    # Blocked-extension check applies to mint too (defense in depth — even
    # if something slipped past search, it can't be tokenised).
    scope = load_scope(data_dir)
    if target.suffix.lower() in set(scope["blocked_extensions"]):
        raise ValueError("file type is blocked from remote fetch")

    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=max(1, int(ttl_minutes)))
    record = {
        "token":      token,
        "path":       str(target),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "used":       False,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Roots that were trusted at MINT time. Redeem re-checks scope using
        # these so a workspace-minted token still works even though workspace
        # isn't in the visitor-facing allowed_paths config.
        "extra_allowed_roots": [str(p) for p in extra],
    }

    with _TOKEN_LOCK:
        tokens = _prune_tokens(_load_tokens(data_dir))
        tokens[token] = record
        _save_tokens(data_dir, tokens)

    log.info(f"minted token for {target.name} (ttl={ttl_minutes}m)")
    return token


def redeem_token(data_dir: Path, token: str) -> dict | None:
    """Validate + consume a token. Returns the resolved file info on
    success, None on any failure (expired, used, unknown, file gone,
    scope mismatch).

    Returned dict:
        {"path": str, "filename": str, "mime": str}
    """
    if not token:
        return None

    with _TOKEN_LOCK:
        tokens = _load_tokens(data_dir)
        rec = tokens.get(token)
        if not rec:
            log.info("redeem: unknown token")
            return None
        if rec.get("used"):
            log.info("redeem: token already used")
            return None
        try:
            exp = datetime.strptime(rec["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            return None
        if datetime.now(timezone.utc) > exp:
            log.info("redeem: token expired")
            return None

        target = Path(rec["path"])
        if not target.exists():
            log.warning(f"redeem: file vanished: {target}")
            return None

        # Re-check scope at redeem time — config may have changed. Honor any
        # extra roots that were trusted at MINT time (e.g. the workspace
        # folder for files Orbi created on the owner's behalf).
        tmp_root = (Path(data_dir) / TMP_DIR).resolve(strict=False)
        extra = [Path(p).expanduser().resolve(strict=False)
                 for p in (rec.get("extra_allowed_roots") or [])]
        allowed = get_allowed_roots(data_dir) + [tmp_root] + extra
        if not _path_is_safe(target, allowed):
            log.warning(f"redeem: file no longer in scope: {target}")
            return None

        # Mark used BEFORE returning — single-use semantics
        rec["used"]       = True
        rec["used_at"]    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tokens[token]     = rec
        _save_tokens(data_dir, tokens)

    mime, _ = mimetypes.guess_type(target.name)
    return {
        "path":     str(target),
        "filename": target.name,
        "mime":     mime or "application/octet-stream",
    }


# ── Folder zip-on-demand ────────────────────────────────────────────────


def prepare_folder_download(data_dir: Path, folder_path: str) -> str:
    """Zip `folder_path` to a temp file under <data_dir>/.tmp_downloads/
    and return the temp file's absolute path.

    Caller then mints a token for the returned path. The temp file lives
    for ~60 minutes until purge_old_temp_downloads sweeps it.

    Validates that folder_path is in scope. Skips files matching blocked
    extensions. Raises ValueError on bad input.
    """
    src = Path(folder_path).expanduser().resolve(strict=False)
    if not src.exists() or not src.is_dir():
        raise ValueError(f"not a folder: {folder_path}")

    roots = get_allowed_roots(data_dir)
    if not _path_is_safe(src, roots):
        raise ValueError("folder is outside the configured allowed scope")

    scope        = load_scope(data_dir)
    blocked_exts = set(scope["blocked_extensions"])

    tmp_dir = Path(data_dir) / TMP_DIR
    tmp_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", src.name) or "folder"
    out_path = tmp_dir / f"{safe_name}_{stamp}_{secrets.token_hex(4)}.zip"

    file_count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in src.rglob("*"):
            # Skip dotfiles/dotdirs and blocked extensions
            try:
                rel = entry.relative_to(src)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if entry.is_file() and entry.suffix.lower() in blocked_exts:
                continue
            if not entry.is_file():
                continue
            try:
                # Defense in depth — symlinks pointing out of scope are skipped
                if not _path_is_safe(entry, roots):
                    continue
                zf.write(entry, arcname=str(rel))
                file_count += 1
            except (OSError, PermissionError) as e:
                log.warning(f"zip skip {entry}: {e}")
                continue

    log.info(f"zipped {file_count} files from {src.name} → {out_path.name}")
    return str(out_path)


def purge_old_temp_downloads(data_dir: Path, older_than_minutes: int = 60) -> int:
    """Delete temp-zip files older than the given age. Returns count
    purged. Safe to call on a timer."""
    tmp_dir = Path(data_dir) / TMP_DIR
    if not tmp_dir.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, int(older_than_minutes)))
    purged = 0
    for f in tmp_dir.iterdir():
        if not f.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                f.unlink()
                purged += 1
            except OSError as e:
                log.warning(f"purge failed {f}: {e}")
    if purged:
        log.info(f"purged {purged} temp downloads")
    return purged


# ── Intent extraction (NLU helper) ──────────────────────────────────────


# Patterns are intentionally liberal — false-positive lookups are cheap
# (search returns 0 hits), but missing a legitimate request feels broken.
# Each pattern captures the file/folder reference into group "q".
# The order matters: the more specific patterns come first.
_INTENT_PATTERNS = [
    # "send me the Maxwell estimate" / "send me Maxwell estimate from my Documents"
    re.compile(r"\bsend\s+(?:me\s+|over\s+|to\s+me\s+)?(?:the\s+|a\s+|that\s+|my\s+)?(?P<q>[^.?!]+?)(?:\s+(?:from|in|on)\s+(?:my\s+)?(?:computer|documents|desktop|downloads|folder|drive))?\s*[.?!]?\s*$", re.I),
    # "grab the Maxwell estimate for me"
    re.compile(r"\bgrab\s+(?:the\s+|that\s+|my\s+|a\s+)?(?P<q>[^.?!]+?)(?:\s+for\s+me)?(?:\s+(?:from|in|on)\s+(?:my\s+)?(?:computer|documents|desktop|downloads|folder|drive))?\s*[.?!]?\s*$", re.I),
    # "fetch the Maxwell estimate"
    re.compile(r"\bfetch\s+(?:the\s+|that\s+|my\s+|a\s+)?(?P<q>[^.?!]+?)(?:\s+(?:from|in|on)\s+(?:my\s+)?(?:computer|documents|desktop|downloads|folder|drive))?\s*[.?!]?\s*$", re.I),
    # "get me the Maxwell estimate file"
    re.compile(r"\bget\s+me\s+(?:the\s+|that\s+|my\s+|a\s+)?(?P<q>[^.?!]+?)(?:\s+(?:from|in|on)\s+(?:my\s+)?(?:computer|documents|desktop|downloads|folder|drive))?\s*[.?!]?\s*$", re.I),
    # "i need the Maxwell estimate from my computer"
    re.compile(r"\bi\s+need\s+(?:the\s+|that\s+|my\s+|a\s+)?(?P<q>[^.?!]+?)\s+(?:from|in|on)\s+(?:my\s+)?(?:computer|documents|desktop|downloads|folder|drive)\s*[.?!]?\s*$", re.I),
    # "pull the Maxwell estimate off my desktop"
    re.compile(r"\bpull\s+(?:up\s+)?(?:the\s+|that\s+|my\s+|a\s+)?(?P<q>[^.?!]+?)(?:\s+(?:from|off|in|on)\s+(?:my\s+)?(?:computer|documents|desktop|downloads|folder|drive))?\s*[.?!]?\s*$", re.I),
    # "find me the X file" / "find me X"
    re.compile(r"\bfind\s+me\s+(?:the\s+|that\s+|my\s+|a\s+)?(?P<q>[^.?!]+?)(?:\s+(?:from|in|on)\s+(?:my\s+)?(?:computer|documents|desktop|downloads|folder|drive))?\s*[.?!]?\s*$", re.I),
    # "email me the X" / "text me the X"
    re.compile(r"\b(?:email|text|mail)\s+(?:me\s+)?(?:the\s+|that\s+|my\s+|a\s+)?(?P<q>[^.?!]+?)(?:\s+(?:from|in|on)\s+(?:my\s+)?(?:computer|documents|desktop|downloads|folder|drive))?\s*[.?!]?\s*$", re.I),
]

# Words to strip from the captured query — they aren't part of the filename
_NOISE_WORDS = {
    "file", "files", "document", "documents", "doc", "docs",
    "pdf", "spreadsheet", "image", "photo", "picture", "video",
    "please", "thanks", "thank", "you",
}

_FOLDER_HINTS = {"folder", "directory", "project", "all the files in"}


def extract_file_request(message: str) -> dict | None:
    """Lightweight intent matcher. Returns
        {"query": "maxwell estimate", "kind": "file" | "folder"}
    or None if the message doesn't look like a remote-fetch request.

    Designed to be liberal — we'd rather match too often (the search
    will simply return 0 hits and the assistant tells the owner so) than
    miss something the owner expects to work.
    """
    if not message:
        return None
    text = message.strip()
    if not text:
        return None

    # Hard exclusions — phrases that LOOK file-fetchy but aren't.
    # "send me a website", "send me a link", etc. should fall through to
    # the URL-fetcher path, not the file-search path.
    lower = text.lower()
    if any(kw in lower for kw in (
        "a website", "the website", "this website",
        "a link", "this link", "a url", "this url",
        "send you a", "send you the", "send you something",
        "give you a", "give you the",
    )):
        return None
    # COMPOSITION requests — owner wants Orby to WRITE something, not
    # search for an existing file. Two patterns to catch:
    #   1) Message starts with a writing verb: draft/write/compose/reply/respond
    #   2) Message contains a writing-verb + writing-object pair anywhere:
    #      "can you CREATE a follow-up EMAIL for Joe Maxwell"
    #      "put together a LETTER for the landlord"
    #      "help me MAKE a REPLY to Sarah"
    if re.match(
        r"^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
        r"(?:draft|write|compose|reply|respond|put\s+together|"
        r"help\s+me\s+(?:draft|write|compose|reply|respond))\s+",
        text, re.IGNORECASE):
        return None
    # Verb + writing-object pair — "create/make/build/put-together/draft/
    # write/compose A/AN/THE email/letter/message/reply/note/memo/response/post"
    if re.search(
        r"\b(?:create|make|build|draft|write|compose|put\s+together|do|"
        r"help\s+me\s+(?:with|make|create|build|write|draft))\s+"
        r"(?:me\s+|us\s+|him\s+|her\s+|them\s+)?"
        r"(?:a\s+|an\s+|the\s+|some\s+|another\s+)?"
        r"(?:short\s+|quick\s+|brief\s+|long\s+|detailed\s+|"
        r"polite\s+|warm\s+|friendly\s+|professional\s+|follow[- ]?up\s+)?"
        r"(?:email|letter|message|reply|response|note|memo|"
        r"text|sms|dm|comment|post|caption|tweet|reply|"
        r"thank[- ]?you|apology|invoice\s+message|response)\b",
        text, re.IGNORECASE):
        return None
    # If the message contains a URL, that's definitively a url-fetch case
    if re.search(r"https?://", text, re.IGNORECASE):
        return None

    for pat in _INTENT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        q_raw = (m.group("q") or "").strip()
        if not q_raw:
            continue

        # Folder vs file?
        kind = "file"
        lower = q_raw.lower()
        if any(h in lower for h in _FOLDER_HINTS) or " folder" in lower:
            kind = "folder"

        # Strip trailing noise words ("send me the maxwell estimate file" → "maxwell estimate")
        words = [w for w in re.split(r"\s+", q_raw) if w]
        while words and words[-1].lower().strip(".,!?") in _NOISE_WORDS:
            words.pop()
        while words and words[0].lower().strip(".,!?") in _NOISE_WORDS:
            words.pop(0)

        query = " ".join(words).strip(" .,!?")
        if not query:
            continue
        # Filter the obviously-not-a-file noise — "send me a text", "send me an email"
        if query.lower() in {"a text", "an email", "me", "him", "her", "them"}:
            continue
        return {"query": query, "kind": kind}

    return None


# ── Routes the orchestrator will wire in orbi.py ────────────────────────
#
# Module surface to bind:
#
#   GET  /api/owner/files/search?q=<query>
#        → file_fetch.search(DATA_DIR, q) → JSON list of hits
#
#   POST /api/owner/files/request   {path: str, kind: "file"|"folder"}
#        → if kind == "folder":
#              tmp = file_fetch.prepare_folder_download(DATA_DIR, path)
#              token = file_fetch.mint_download_token(DATA_DIR, tmp)
#          else:
#              token = file_fetch.mint_download_token(DATA_DIR, path)
#          → return {"url": f"/download/{token}", "expires_in_min": 10}
#
#   GET  /download/<token>
#        → info = file_fetch.redeem_token(DATA_DIR, token)
#          if not info: abort(404)
#          return send_file(info["path"], as_attachment=True,
#                           download_name=info["filename"],
#                           mimetype=info["mime"])
#
#   GET  /api/owner/files/scope
#        → file_fetch.load_scope(DATA_DIR)
#
#   PUT  /api/owner/files/scope     {allowed_paths, max_file_mb, blocked_extensions}
#        → file_fetch.save_scope(DATA_DIR, payload)
#
# Also call file_fetch.purge_old_temp_downloads(DATA_DIR) on the same
# timer that already runs purge_expired_archives() in users.py.
#
# All /api/owner/* routes need the existing owner-session-cookie guard.
# /download/<token> stays PUBLIC — the single-use token IS the auth.
