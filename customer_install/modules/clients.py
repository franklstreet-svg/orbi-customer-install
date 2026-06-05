"""
modules/clients — per-client folder organization on the local disk.

Every customer who shows up as a project gets a dedicated folder under
~/Orbi/clients/<sanitized name>/ with a consistent subfolder layout.
Generated docs (CO PDFs, invoices, signed contracts, etc.) save into
these folders so the contractor has one place to look for everything
about Mr. Johnson.

Folder layout:
  ~/Orbi/clients/<Customer Name>/
    change_orders/      — filled CO PDFs (unsigned + signed)
    invoices/           — generated invoice PDFs
    contracts/          — signed contracts, MSAs
    photos/             — jobsite photos
    signed_docs/        — any other e-signed paperwork
    proposals/          — proposal PDFs
    closeouts/          — final closeout packages
    client_notes.md     — Orby's running notes about this client

Sanitization rule: strip filesystem-unsafe chars but preserve readability
so the owner can find Mr. Johnson's folder by browsing in Finder/Explorer.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

_LOCK = threading.Lock()

# Subfolders created in every client folder. Keep this stable across releases —
# customers will rely on the layout being predictable.
SUBFOLDERS = [
    "change_orders",
    "invoices",
    "contracts",
    "photos",
    "signed_docs",
    "proposals",
    "closeouts",
]


def _workspace_root(config: dict | None = None) -> Path:
    """Lazy import to avoid circular dependency with modules/workspace.py."""
    from modules import workspace as _ws
    return _ws.workspace_path(config)


def sanitize_name(name: str) -> str:
    """Turn 'Sarah Johnson / Johnson Construction!' into a clean folder
    name. Strips filesystem-unsafe chars, collapses whitespace, caps length.
    Preserves casing for human readability."""
    if not name:
        return "_unnamed_client"
    s = (name or "").strip()
    # Remove path separators and other dangerous chars
    s = re.sub(r"[\\/:*?\"<>|]+", " ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    # Cap length (long names look bad in file managers)
    if len(s) > 80:
        s = s[:80].rstrip()
    if not s:
        return "_unnamed_client"
    return s


def client_root(name: str, config: dict | None = None) -> Path:
    """Return the path to a client's root folder. Does NOT create it —
    use ensure_folder() for that."""
    return _workspace_root(config) / "clients" / sanitize_name(name)


def ensure_folder(name: str, config: dict | None = None) -> Path:
    """Create the client folder + all subfolders if missing. Returns the
    root path. Idempotent — safe to call every time a project touches a
    customer."""
    root = client_root(name, config)
    with _LOCK:
        root.mkdir(parents=True, exist_ok=True)
        for sub in SUBFOLDERS:
            (root / sub).mkdir(exist_ok=True)
        # Seed a client_notes.md if missing so the owner has a place to
        # jot context. Orby will append to this as she learns things.
        notes_path = root / "client_notes.md"
        if not notes_path.exists():
            notes_path.write_text(
                f"# Notes on {name}\n\n"
                f"(Orby appends what she learns about this customer here. "
                f"You can edit freely.)\n",
                encoding="utf-8",
            )
    return root


def subfolder(name: str, kind: str, config: dict | None = None) -> Path:
    """Get a specific subfolder for a client, creating the full structure
    on demand. kind = one of SUBFOLDERS."""
    if kind not in SUBFOLDERS:
        raise ValueError(f"unknown subfolder kind {kind!r}; expected one of {SUBFOLDERS}")
    root = ensure_folder(name, config)
    return root / kind


def list_clients(config: dict | None = None) -> list[dict]:
    """Walk the clients/ folder. Returns one record per existing client
    folder with disk-usage hint and last-modified."""
    base = _workspace_root(config) / "clients"
    if not base.exists():
        return []
    out = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = 0
        # Count files (recursive, capped) so the UI can show "12 files"
        file_count = 0
        for _ in entry.rglob("*"):
            file_count += 1
            if file_count >= 500:
                break
        out.append({
            "name": entry.name,
            "path": str(entry),
            "file_count": file_count,
            "modified_ts": mtime,
        })
    return out


def append_note(name: str, note: str, config: dict | None = None) -> bool:
    """Append a dated bullet to the client's notes file. Returns True on
    success. Used by chat handlers when Orby learns something worth
    remembering (e.g. "customer prefers texts over calls")."""
    if not note or len(note.strip()) < 3:
        return False
    root = ensure_folder(name, config)
    notes = root / "client_notes.md"
    from datetime import date as _date
    today = _date.today().isoformat()
    line = f"\n- **{today}**: {note.strip()}\n"
    with _LOCK:
        try:
            with notes.open("a", encoding="utf-8") as f:
                f.write(line)
            return True
        except OSError:
            return False


def save_doc(client_name: str, kind: str, src_path: Path,
              filename: str | None = None,
              config: dict | None = None) -> Path:
    """Copy a generated doc into the client's subfolder. Returns the
    destination path. kind must be in SUBFOLDERS. If filename is None,
    uses the source file's basename."""
    src = Path(src_path)
    if not src.exists():
        raise FileNotFoundError(f"source doc missing: {src}")
    dest_dir = subfolder(client_name, kind, config)
    dest = dest_dir / (filename or src.name)
    # If a file with this name exists, append a timestamp to avoid clobber
    if dest.exists():
        from datetime import datetime
        stem, ext = dest.stem, dest.suffix
        dest = dest_dir / f"{stem}__{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
    with _LOCK:
        dest.write_bytes(src.read_bytes())
    return dest
