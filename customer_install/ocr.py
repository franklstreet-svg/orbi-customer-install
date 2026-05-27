"""
ocr — extract text and structured data from images uploaded to the workspace.

Handles three classes of image:
  - receipt        → parsed into vendor/total/tax/line_items, saved to receipts.json
  - business_card  → parsed into name/phone/email/company, auto-added to contacts
  - document/other → just returns the OCR'd text, owner decides

Pipeline:
    image file
      → extract_text()      Tesseract OCR via pytesseract
      → classify_image()    receipt | business_card | document | unknown
      → parse_receipt()  or parse_business_card()   (LLM-driven, regex-fallback)
      → process_image()  the orchestrator: persists + returns a dict

INSTALL (owner runs ONCE per machine):
    Linux:    sudo apt install tesseract-ocr
    macOS:    brew install tesseract
    Windows:  download installer from https://github.com/UB-Mannheim/tesseract/wiki
    Then:     pip install pytesseract Pillow

If pytesseract OR the tesseract binary is missing, extract_text() returns ""
and logs a clear warning — the rest of Orbi keeps working, image uploads just
get indexed by filename only (same behavior as before this module existed).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("orbi.ocr")

_RECEIPTS_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Tesseract availability — checked lazily, cached after first probe
# ---------------------------------------------------------------------------

_TESSERACT_OK: bool | None = None  # None = not probed yet


def _tesseract_available() -> bool:
    """Probe pytesseract + tesseract binary once, cache the answer."""
    global _TESSERACT_OK
    if _TESSERACT_OK is not None:
        return _TESSERACT_OK
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as e:
        log.warning(
            "OCR disabled: %s. Run `pip install pytesseract Pillow` "
            "to enable image text extraction.", e
        )
        _TESSERACT_OK = False
        return False
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
    except Exception as e:
        log.warning(
            "OCR disabled: tesseract binary not found on PATH (%s). "
            "Install: linux=`sudo apt install tesseract-ocr`, "
            "mac=`brew install tesseract`, "
            "windows=https://github.com/UB-Mannheim/tesseract/wiki", e
        )
        _TESSERACT_OK = False
        return False
    _TESSERACT_OK = True
    return True


# ---------------------------------------------------------------------------
# extract_text — the only function that talks to tesseract directly
# ---------------------------------------------------------------------------


def extract_text(image_path: Path) -> str:
    """Run OCR on an image. Returns "" if OCR unavailable or fails."""
    image_path = Path(image_path)
    if not image_path.exists():
        log.warning("OCR: file not found: %s", image_path)
        return ""
    if not _tesseract_available():
        return ""
    try:
        import pytesseract
        from PIL import Image
        with Image.open(image_path) as img:
            # Convert to a sane mode — tesseract handles L and RGB cleanly
            if img.mode not in ("L", "RGB"):
                img = img.convert("RGB")
            text = pytesseract.image_to_string(img)
        return (text or "").strip()
    except Exception:
        log.exception("OCR failed on %s", image_path)
        return ""


# ---------------------------------------------------------------------------
# classify_image — heuristic, runs on OCR'd text
# ---------------------------------------------------------------------------

_RE_MONEY        = re.compile(r"\$?\d+\.\d{2}\b")
_RE_PHONE        = re.compile(r"(?:\+?1[\s\-.])?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}")
_RE_EMAIL        = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_RE_WEBSITE      = re.compile(r"\b(?:https?://)?(?:www\.)?[A-Za-z0-9\-]+\.(?:com|net|org|io|co|us|biz)\b", re.IGNORECASE)
_RE_PERSON_NAME  = re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b")
_RECEIPT_KEYWORDS = ("total", "subtotal", "tax", "amount due", "balance")


def classify_image(image_path: Path) -> str:
    """Guess receipt / business_card / document / unknown from OCR content."""
    text = extract_text(image_path)
    if not text:
        return "unknown"
    return _classify_text(text)


def _classify_text(text: str) -> str:
    low = text.lower()
    has_money = bool(_RE_MONEY.search(text))
    has_receipt_kw = any(kw in low for kw in _RECEIPT_KEYWORDS)
    if has_receipt_kw and has_money:
        return "receipt"

    # Business card: short, has phone+email+name pattern
    char_count = len(text)
    line_count = len([l for l in text.splitlines() if l.strip()])
    has_phone = bool(_RE_PHONE.search(text))
    has_email = bool(_RE_EMAIL.search(text))
    has_name  = bool(_RE_PERSON_NAME.search(text))
    if char_count < 500 and line_count < 15 and has_phone and has_email and has_name:
        return "business_card"

    # Anything with multiple paragraphs / decent length is a "document"
    if char_count > 200 and line_count >= 3:
        return "document"

    return "unknown"


# ---------------------------------------------------------------------------
# LLM helpers — defensive JSON extraction
# ---------------------------------------------------------------------------


def _llm_json(config: dict, system: str, user_text: str) -> dict:
    """Ask the LLM for JSON, parse defensively. Returns {} on failure."""
    try:
        from llm_client import generate
    except Exception:
        log.exception("OCR: llm_client unavailable")
        return {}
    try:
        resp = generate(config, system, [{"role": "user", "content": user_text}])
    except Exception:
        log.exception("OCR: LLM call crashed")
        return {}
    raw = (getattr(resp, "text", "") or "").strip()
    if not raw:
        return {}
    return _coerce_json(raw)


def _coerce_json(raw: str) -> dict:
    """Pull a JSON object out of an LLM response that may contain prose/fences."""
    if not raw:
        return {}
    # Strip code fences
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    # Try direct parse first
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    # Find first balanced { ... } and try that
    brace_start = raw.find("{")
    if brace_start == -1:
        return {}
    depth = 0
    for i in range(brace_start, len(raw)):
        ch = raw[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                snippet = raw[brace_start : i + 1]
                try:
                    data = json.loads(snippet)
                    return data if isinstance(data, dict) else {}
                except json.JSONDecodeError:
                    return {}
    return {}


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).replace("$", "").replace(",", "").strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# parse_receipt
# ---------------------------------------------------------------------------

_RECEIPT_SYSTEM = (
    "You are a receipt parser. The user will paste OCR text from a paper receipt. "
    "Return ONLY a single JSON object with these keys: "
    "vendor (string), total (number), subtotal (number), tax (number), "
    "date (YYYY-MM-DD string), payment_method (string), "
    "line_items (array of {name, price}). "
    "Use null for any field you cannot determine. Do not include any prose or "
    "explanation outside the JSON object."
)


def parse_receipt(text: str, config: dict | None = None) -> dict:
    """Extract structured fields from receipt OCR text. Always returns a dict."""
    result = {
        "vendor":         "",
        "total":          None,
        "subtotal":       None,
        "tax":            None,
        "date":           "",
        "payment_method": "",
        "line_items":     [],
        "raw_text":       text or "",
    }
    if not text:
        return result

    if config:
        data = _llm_json(config, _RECEIPT_SYSTEM, text[:6000])
        if data:
            result["vendor"]         = str(data.get("vendor") or "").strip()
            result["total"]          = _to_float(data.get("total"))
            result["subtotal"]       = _to_float(data.get("subtotal"))
            result["tax"]            = _to_float(data.get("tax"))
            result["date"]           = str(data.get("date") or "").strip()
            result["payment_method"] = str(data.get("payment_method") or "").strip()
            items = data.get("line_items") or []
            if isinstance(items, list):
                cleaned = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    name = str(it.get("name") or "").strip()
                    price = _to_float(it.get("price"))
                    if name:
                        cleaned.append({"name": name, "price": price})
                result["line_items"] = cleaned

    # Regex fallbacks — fill anything LLM missed
    _regex_fill_receipt(text, result)
    return result


def _regex_fill_receipt(text: str, result: dict) -> None:
    """Fill in totals / subtotals / tax / date from regex if not already set."""
    low = text.lower()

    def _find_amount_after(label: str) -> float | None:
        # Match "label .... $12.34" within one line
        for line in text.splitlines():
            if label in line.lower():
                m = _RE_MONEY.findall(line)
                if m:
                    return _to_float(m[-1])
        return None

    if result["total"] is None:
        result["total"] = _find_amount_after("total")
    if result["subtotal"] is None:
        result["subtotal"] = _find_amount_after("subtotal")
    if result["tax"] is None:
        result["tax"] = _find_amount_after("tax")

    if not result["date"]:
        # ISO first, then US-style
        m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        if m:
            result["date"] = m.group(1)
        else:
            m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b", text)
            if m:
                mo, dy, yr = m.groups()
                if len(yr) == 2:
                    yr = "20" + yr
                try:
                    result["date"] = f"{int(yr):04d}-{int(mo):02d}-{int(dy):02d}"
                except ValueError:
                    pass

    if not result["vendor"]:
        # First non-empty line is often the merchant name
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not _RE_MONEY.search(stripped) and len(stripped) < 60:
                result["vendor"] = stripped
                break


# ---------------------------------------------------------------------------
# parse_business_card
# ---------------------------------------------------------------------------

_CARD_SYSTEM = (
    "You are a business-card parser. The user will paste OCR text from a "
    "business card. Return ONLY a single JSON object with these keys: "
    "name (string), company (string), title (string), phone (string in E.164 "
    "format if possible), email (string), website (string), address (string), "
    "notes (string). Use empty string for fields you cannot determine. "
    "Do not include any prose outside the JSON object."
)


def parse_business_card(text: str, config: dict | None = None) -> dict:
    """Extract contact fields from a business card OCR text."""
    result = {
        "name":     "",
        "company":  "",
        "title":    "",
        "phone":    "",
        "email":    "",
        "website":  "",
        "address":  "",
        "notes":    "",
        "raw_text": text or "",
    }
    if not text:
        return result

    if config:
        data = _llm_json(config, _CARD_SYSTEM, text[:3000])
        if data:
            for k in ("name", "company", "title", "phone", "email",
                      "website", "address", "notes"):
                v = data.get(k)
                if v:
                    result[k] = str(v).strip()

    # Regex fallbacks
    if not result["email"]:
        m = _RE_EMAIL.search(text)
        if m:
            result["email"] = m.group(0)
    if not result["phone"]:
        m = _RE_PHONE.search(text)
        if m:
            result["phone"] = _normalize_phone(m.group(0))
    if not result["website"]:
        m = _RE_WEBSITE.search(text)
        if m:
            result["website"] = m.group(0)
    if not result["name"]:
        m = _RE_PERSON_NAME.search(text)
        if m:
            result["name"] = m.group(0)

    return result


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return raw.strip()


# ---------------------------------------------------------------------------
# Receipts storage — receipts.json in user_dir
# ---------------------------------------------------------------------------


def _receipts_path(user_dir: Path) -> Path:
    return Path(user_dir) / "receipts.json"


def _load_receipts(user_dir: Path) -> list[dict]:
    p = _receipts_path(user_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        log.exception("receipts.json unreadable, starting fresh")
        return []


def _save_receipts(user_dir: Path, receipts: list[dict]) -> None:
    p = _receipts_path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(receipts, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _add_receipt(user_dir: Path, parsed: dict, image_path: Path) -> dict:
    record = {
        "id":          uuid.uuid4().hex[:12],
        "ts":          time.time(),
        "image_file":  Path(image_path).name,
        "vendor":      parsed.get("vendor") or "",
        "total":       parsed.get("total"),
        "subtotal":    parsed.get("subtotal"),
        "tax":         parsed.get("tax"),
        "date":        parsed.get("date") or "",
        "payment_method": parsed.get("payment_method") or "",
        "line_items":  parsed.get("line_items") or [],
        "raw_text":    parsed.get("raw_text") or "",
    }
    with _RECEIPTS_LOCK:
        receipts = _load_receipts(user_dir)
        receipts.append(record)
        _save_receipts(user_dir, receipts)
    return record


def list_receipts(user_dir: Path, limit: int = 100) -> list[dict]:
    """Newest first, capped at `limit`."""
    with _RECEIPTS_LOCK:
        receipts = _load_receipts(user_dir)
    receipts.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return receipts[: max(0, int(limit))]


def get_receipt(user_dir: Path, receipt_id: str) -> dict | None:
    with _RECEIPTS_LOCK:
        for r in _load_receipts(user_dir):
            if r.get("id") == receipt_id:
                return r
    return None


def delete_receipt(user_dir: Path, receipt_id: str) -> bool:
    with _RECEIPTS_LOCK:
        receipts = _load_receipts(user_dir)
        before = len(receipts)
        receipts = [r for r in receipts if r.get("id") != receipt_id]
        if len(receipts) == before:
            return False
        _save_receipts(user_dir, receipts)
        return True


# ---------------------------------------------------------------------------
# process_image — the orchestrator endpoint called by the web route
# ---------------------------------------------------------------------------


def process_image(config: dict, image_path: Path, user_dir: Path) -> dict:
    """OCR → classify → parse → persist. Returns a result dict for the API."""
    image_path = Path(image_path)
    user_dir = Path(user_dir)

    text = extract_text(image_path)
    if not text:
        return {
            "kind":     "unknown",
            "ocr_text": "",
            "action":   "ocr_unavailable",
            "note":     "Tesseract OCR is not installed or the image had no readable text.",
        }

    kind = _classify_text(text)

    if kind == "business_card":
        parsed = parse_business_card(text, config)
        try:
            from modules import contacts
            contact = contacts.add(
                user_dir,
                name=parsed.get("name") or "(unknown)",
                phone=parsed.get("phone") or "",
                email=parsed.get("email") or "",
                notes=_card_to_notes(parsed),
                tags=["business_card"],
                source="business_card",
                company=parsed.get("company") or "",
            )
            return {
                "kind":       "business_card",
                "parsed":     parsed,
                "action":     "contact_added",
                "contact_id": contact.get("id"),
            }
        except Exception:
            log.exception("failed to add contact from business card")
            return {
                "kind":   "business_card",
                "parsed": parsed,
                "action": "contact_failed",
            }

    if kind == "receipt":
        parsed = parse_receipt(text, config)
        try:
            record = _add_receipt(user_dir, parsed, image_path)
            return {
                "kind":       "receipt",
                "parsed":     parsed,
                "action":     "receipt_saved",
                "receipt_id": record["id"],
            }
        except Exception:
            log.exception("failed to save receipt")
            return {
                "kind":   "receipt",
                "parsed": parsed,
                "action": "receipt_failed",
            }

    # document / unknown — just return OCR text, owner decides
    return {
        "kind":     kind,
        "ocr_text": text,
        "action":   "ocr_only",
    }


def _card_to_notes(parsed: dict) -> str:
    """Fold non-standard fields into the contact notes."""
    bits = []
    if parsed.get("title"):
        bits.append(parsed["title"])
    if parsed.get("website"):
        bits.append(parsed["website"])
    if parsed.get("address"):
        bits.append(parsed["address"])
    if parsed.get("notes"):
        bits.append(parsed["notes"])
    return " | ".join(b for b in bits if b)


# ---------------------------------------------------------------------------
# Route comment block — wire these up in orbi.py / owner_dashboard
# ---------------------------------------------------------------------------
#
# POST   /api/owner/ocr/process
#        body: {"filename": "<name of file already uploaded via
#                            /api/owner/workspace/upload>"}
#        Resolves filename inside the owner's workspace dir, then calls
#            ocr.process_image(config, workspace_dir / filename, user_dir)
#        Returns the process_image() result as JSON.
#
# GET    /api/owner/receipts
#        Returns ocr.list_receipts(user_dir).
#        Optional query: ?limit=N (default 100).
#
# GET    /api/owner/receipts/<id>
#        Returns ocr.get_receipt(user_dir, id) or 404.
#
# DELETE /api/owner/receipts/<id>
#        Returns {"ok": True} on success, 404 if not found.
#
# Owner-install reminders for the dashboard "Setup" panel:
#   - Linux:   sudo apt install tesseract-ocr
#   - macOS:   brew install tesseract
#   - Windows: https://github.com/UB-Mannheim/tesseract/wiki
#   - Then:    pip install pytesseract Pillow
