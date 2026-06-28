"""
modules/legal — paralegal module for attorneys.

Covers the core paralegal workflow:
  - Matter/case management (open, track, close)
  - Deadline tracking (court dates, filing deadlines, statute of limitations)
  - Conflict checks (new client vs existing clients + opposing parties)
  - Time tracking (billable hours per matter)
  - Document drafting (LLM-generated from templates)
  - Client intake

Design rule: Idunn works FOR the attorney. She never gives legal advice TO
clients directly. Everything she produces is for the attorney to review.

Data lives under data/legal/ at the business level (shared across users
on the same attorney account — one firm, one brain).

Matter shape:
  {
    "id":               "12-char hex",
    "matter_number":    "2026-001",
    "title":            "Smith v. Jones",
    "type":             "civil" | "criminal" | "family" | "estate" |
                        "business" | "immigration" | "personal_injury" |
                        "real_estate" | "bankruptcy" | "other",
    "status":           "intake" | "active" | "discovery" | "negotiation" |
                        "trial" | "appeal" | "settled" | "closed",
    "client_name":      "John Smith",
    "client_id":        "contacts module id or null",
    "client_phone":     "+17751234567",
    "client_email":     "john@example.com",
    "opposing_party":   "Jane Jones",
    "opposing_counsel": "Bob Attorney, Esq.",
    "opposing_firm":    "Jones & Associates",
    "court":            "Washoe County District Court",
    "judge":            "Hon. Patricia Williams",
    "case_number":      "CV-2026-01234",
    "jurisdiction":     "NV",
    "practice_area":    "personal_injury",
    "date_opened":      "2026-01-15",
    "date_closed":      null,
    "retainer":         5000.00,
    "rate":             350.00,
    "notes":            "",
    "tags":             [],
    "created_at":       1780000000,
    "updated_at":       1780000000
  }

Deadline shape:
  {
    "id":           "12-char hex",
    "matter_id":    "matter hex id",
    "matter_title": "Smith v. Jones",
    "title":        "Answer to Complaint Due",
    "type":         "filing" | "court_date" | "hearing" | "deposition" |
                    "discovery_cutoff" | "statute_of_limitations" |
                    "trial" | "appeal" | "other",
    "due_date":     "2026-07-15",
    "due_time":     "09:00",
    "location":     "Dept. 3, Washoe County Courthouse",
    "notes":        "",
    "status":       "pending" | "completed" | "continued" | "missed",
    "reminder_days":[30, 14, 7, 3, 1],
    "created_at":   1780000000
  }

Time entry shape:
  {
    "id":          "12-char hex",
    "matter_id":   "matter hex id",
    "matter_title":"Smith v. Jones",
    "date":        "2026-06-27",
    "hours":       2.5,
    "rate":        350.00,
    "amount":      875.00,
    "description": "Client consultation — discovery responses",
    "billed":      false,
    "invoice_id":  null,
    "created_at":  1780000000
  }
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

_LOCK = threading.Lock()


# ── Path helpers ──────────────────────────────────────────────────────────────

def _legal_dir(data_dir: Path) -> Path:
    d = data_dir / "legal"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _matters_path(data_dir: Path) -> Path:
    return _legal_dir(data_dir) / "matters.json"

def _deadlines_path(data_dir: Path) -> Path:
    return _legal_dir(data_dir) / "deadlines.json"

def _time_path(data_dir: Path) -> Path:
    return _legal_dir(data_dir) / "time_entries.json"


# ── Generic load/save ─────────────────────────────────────────────────────────

def _load(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []

def _save(path: Path, data: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

def _new_id() -> str:
    return uuid.uuid4().hex[:12]

def _now() -> int:
    return int(time.time())


# ── MATTERS ───────────────────────────────────────────────────────────────────

def _next_matter_number(matters: list) -> str:
    year = datetime.now().year
    existing = [
        m.get("matter_number", "") for m in matters
        if str(year) in m.get("matter_number", "")
    ]
    nums = []
    for mn in existing:
        try:
            nums.append(int(mn.split("-")[-1]))
        except ValueError:
            pass
    n = max(nums) + 1 if nums else 1
    return f"{year}-{n:03d}"


def open_matter(data_dir: Path, *, title: str, client_name: str,
                matter_type: str = "other", practice_area: str = "",
                client_phone: str = "", client_email: str = "",
                opposing_party: str = "", opposing_counsel: str = "",
                opposing_firm: str = "", court: str = "", judge: str = "",
                case_number: str = "", jurisdiction: str = "",
                retainer: float = 0.0, rate: float = 0.0,
                notes: str = "", tags: list | None = None) -> dict:
    with _LOCK:
        matters = _load(_matters_path(data_dir))
        matter = {
            "id":               _new_id(),
            "matter_number":    _next_matter_number(matters),
            "title":            title.strip(),
            "type":             matter_type,
            "status":           "intake",
            "client_name":      client_name.strip(),
            "client_id":        None,
            "client_phone":     client_phone.strip(),
            "client_email":     client_email.strip(),
            "opposing_party":   opposing_party.strip(),
            "opposing_counsel": opposing_counsel.strip(),
            "opposing_firm":    opposing_firm.strip(),
            "court":            court.strip(),
            "judge":            judge.strip(),
            "case_number":      case_number.strip(),
            "jurisdiction":     jurisdiction.strip(),
            "practice_area":    practice_area.strip(),
            "date_opened":      date.today().isoformat(),
            "date_closed":      None,
            "retainer":         retainer,
            "rate":             rate,
            "notes":            notes.strip(),
            "tags":             tags or [],
            "created_at":       _now(),
            "updated_at":       _now(),
        }
        matters.append(matter)
        _save(_matters_path(data_dir), matters)
        return matter


def get_matter(data_dir: Path, matter_id: str) -> dict | None:
    matters = _load(_matters_path(data_dir))
    for m in matters:
        if m.get("id") == matter_id:
            return m
    return None


def list_matters(data_dir: Path, status: str | None = None,
                 search: str | None = None) -> list:
    matters = _load(_matters_path(data_dir))
    if status:
        matters = [m for m in matters if m.get("status") == status]
    if search:
        q = search.lower()
        matters = [
            m for m in matters
            if q in m.get("title", "").lower()
            or q in m.get("client_name", "").lower()
            or q in m.get("matter_number", "").lower()
            or q in m.get("case_number", "").lower()
            or q in m.get("opposing_party", "").lower()
        ]
    return sorted(matters, key=lambda m: m.get("updated_at", 0), reverse=True)


def update_matter(data_dir: Path, matter_id: str, **fields) -> dict | None:
    with _LOCK:
        matters = _load(_matters_path(data_dir))
        for m in matters:
            if m.get("id") == matter_id:
                allowed = {
                    "title", "type", "status", "client_name", "client_phone",
                    "client_email", "opposing_party", "opposing_counsel",
                    "opposing_firm", "court", "judge", "case_number",
                    "jurisdiction", "practice_area", "retainer", "rate",
                    "notes", "tags", "date_closed",
                }
                for k, v in fields.items():
                    if k in allowed:
                        m[k] = v
                m["updated_at"] = _now()
                if fields.get("status") in ("closed", "settled") and not m.get("date_closed"):
                    m["date_closed"] = date.today().isoformat()
                _save(_matters_path(data_dir), matters)
                return m
    return None


def close_matter(data_dir: Path, matter_id: str,
                 status: str = "closed") -> dict | None:
    return update_matter(data_dir, matter_id,
                         status=status,
                         date_closed=date.today().isoformat())


# ── CONFLICT CHECK ────────────────────────────────────────────────────────────

def conflict_check(data_dir: Path, name: str) -> list:
    """
    Check a new client or party name against all existing matters.
    Returns list of conflicting matters (client names + opposing parties).
    Attorney must review before opening a new matter.
    """
    q = name.lower().strip()
    if not q:
        return []
    matters = _load(_matters_path(data_dir))
    hits = []
    for m in matters:
        client_match = q in m.get("client_name", "").lower()
        opposing_match = q in m.get("opposing_party", "").lower()
        counsel_match = q in m.get("opposing_counsel", "").lower()
        if client_match or opposing_match or counsel_match:
            hits.append({
                "matter_number": m.get("matter_number"),
                "title":         m.get("title"),
                "status":        m.get("status"),
                "client_name":   m.get("client_name"),
                "opposing_party":m.get("opposing_party"),
                "conflict_type": (
                    "existing client" if client_match
                    else "opposing party" if opposing_match
                    else "opposing counsel"
                ),
            })
    return hits


# ── DEADLINES ─────────────────────────────────────────────────────────────────

def add_deadline(data_dir: Path, *, matter_id: str, matter_title: str,
                 title: str, due_date: str, deadline_type: str = "other",
                 due_time: str = "", location: str = "",
                 notes: str = "",
                 reminder_days: list | None = None) -> dict:
    with _LOCK:
        deadlines = _load(_deadlines_path(data_dir))
        entry = {
            "id":           _new_id(),
            "matter_id":    matter_id,
            "matter_title": matter_title,
            "title":        title.strip(),
            "type":         deadline_type,
            "due_date":     due_date,
            "due_time":     due_time.strip(),
            "location":     location.strip(),
            "notes":        notes.strip(),
            "status":       "pending",
            "reminder_days": reminder_days if reminder_days is not None
                             else [30, 14, 7, 3, 1],
            "created_at":   _now(),
        }
        deadlines.append(entry)
        _save(_deadlines_path(data_dir), deadlines)
        return entry


def list_deadlines(data_dir: Path, matter_id: str | None = None,
                   upcoming_days: int | None = None,
                   status: str | None = None) -> list:
    deadlines = _load(_deadlines_path(data_dir))
    if matter_id:
        deadlines = [d for d in deadlines if d.get("matter_id") == matter_id]
    if status:
        deadlines = [d for d in deadlines if d.get("status") == status]
    if upcoming_days is not None:
        today = date.today()
        cutoff = today + timedelta(days=upcoming_days)
        filtered = []
        for d in deadlines:
            try:
                dd = date.fromisoformat(d["due_date"])
                if today <= dd <= cutoff:
                    filtered.append(d)
            except (KeyError, ValueError):
                pass
        deadlines = filtered
    return sorted(deadlines, key=lambda d: d.get("due_date", ""))


def complete_deadline(data_dir: Path, deadline_id: str) -> dict | None:
    with _LOCK:
        deadlines = _load(_deadlines_path(data_dir))
        for d in deadlines:
            if d.get("id") == deadline_id:
                d["status"] = "completed"
                _save(_deadlines_path(data_dir), deadlines)
                return d
    return None


def update_deadline(data_dir: Path, deadline_id: str, **fields) -> dict | None:
    with _LOCK:
        deadlines = _load(_deadlines_path(data_dir))
        allowed = {"title", "due_date", "due_time", "location",
                   "notes", "status", "type", "reminder_days"}
        for d in deadlines:
            if d.get("id") == deadline_id:
                for k, v in fields.items():
                    if k in allowed:
                        d[k] = v
                _save(_deadlines_path(data_dir), deadlines)
                return d
    return None


def upcoming_deadlines_summary(data_dir: Path, days: int = 30) -> str:
    """Human-readable summary for the morning brief / owner chat."""
    deadlines = list_deadlines(data_dir, upcoming_days=days, status="pending")
    if not deadlines:
        return f"No deadlines in the next {days} days."
    today = date.today()
    lines = []
    for d in deadlines:
        try:
            dd = date.fromisoformat(d["due_date"])
            delta = (dd - today).days
            when = (
                "TODAY" if delta == 0
                else "TOMORROW" if delta == 1
                else f"in {delta} days ({d['due_date']})"
            )
            lines.append(
                f"• {d['title']} — {d.get('matter_title','?')} — {when}"
            )
        except (ValueError, KeyError):
            lines.append(f"• {d.get('title','?')} — {d.get('matter_title','?')}")
    return "\n".join(lines)


# ── TIME TRACKING ─────────────────────────────────────────────────────────────

def log_time(data_dir: Path, *, matter_id: str, matter_title: str,
             hours: float, description: str,
             entry_date: str | None = None,
             rate: float = 0.0) -> dict:
    with _LOCK:
        entries = _load(_time_path(data_dir))
        d = entry_date or date.today().isoformat()
        amount = round(hours * rate, 2)
        entry = {
            "id":           _new_id(),
            "matter_id":    matter_id,
            "matter_title": matter_title,
            "date":         d,
            "hours":        hours,
            "rate":         rate,
            "amount":       amount,
            "description":  description.strip(),
            "billed":       False,
            "invoice_id":   None,
            "created_at":   _now(),
        }
        entries.append(entry)
        _save(_time_path(data_dir), entries)
        return entry


def list_time(data_dir: Path, matter_id: str | None = None,
              billed: bool | None = None) -> list:
    entries = _load(_time_path(data_dir))
    if matter_id:
        entries = [e for e in entries if e.get("matter_id") == matter_id]
    if billed is not None:
        entries = [e for e in entries if e.get("billed") == billed]
    return sorted(entries, key=lambda e: e.get("date", ""), reverse=True)


def matter_time_summary(data_dir: Path, matter_id: str) -> dict:
    """Total hours + unbilled amount for a matter."""
    entries = list_time(data_dir, matter_id=matter_id)
    total_hours = sum(e.get("hours", 0) for e in entries)
    total_amount = sum(e.get("amount", 0) for e in entries)
    unbilled_hours = sum(e.get("hours", 0) for e in entries if not e.get("billed"))
    unbilled_amount = sum(e.get("amount", 0) for e in entries if not e.get("billed"))
    return {
        "total_hours":     round(total_hours, 2),
        "total_amount":    round(total_amount, 2),
        "unbilled_hours":  round(unbilled_hours, 2),
        "unbilled_amount": round(unbilled_amount, 2),
        "entry_count":     len(entries),
    }


def mark_time_billed(data_dir: Path, matter_id: str) -> int:
    """Mark all unbilled time entries for a matter as billed. Returns count."""
    with _LOCK:
        entries = _load(_time_path(data_dir))
        count = 0
        for e in entries:
            if e.get("matter_id") == matter_id and not e.get("billed"):
                e["billed"] = True
                count += 1
        if count:
            _save(_time_path(data_dir), entries)
        return count


# ── STATUTE OF LIMITATIONS REFERENCE ─────────────────────────────────────────
# Reference guide — attorney must always verify for specific facts/jurisdiction.

SOL_REFERENCE = {
    "personal_injury": {
        "default_years": 2,
        "notes": "Most states: 2 years from date of injury. NV: 2 years (NRS 11.190). CA: 2 years. TX: 2 years. NY: 3 years. Discovery rule may apply.",
    },
    "medical_malpractice": {
        "default_years": 2,
        "notes": "Most states: 2-3 years from act or discovery. NV: 3 years from act or 1 year from discovery (NRS 41A.097). Verify — highly jurisdiction-specific.",
    },
    "breach_of_contract_written": {
        "default_years": 6,
        "notes": "Written contracts: NV 6 years (NRS 11.190(1)(b)). CA 4 years. NY 6 years. TX 4 years.",
    },
    "breach_of_contract_oral": {
        "default_years": 4,
        "notes": "Oral contracts: NV 6 years. CA 2 years. TX 4 years. Varies significantly.",
    },
    "property_damage": {
        "default_years": 3,
        "notes": "NV: 3 years (NRS 11.190). CA: 3 years. TX: 2 years.",
    },
    "wrongful_death": {
        "default_years": 2,
        "notes": "Most states: 2 years from death. NV: 2 years (NRS 41.085). Often tied to underlying tort SOL.",
    },
    "fraud": {
        "default_years": 3,
        "notes": "Usually 3-6 years from discovery. NV: 3 years from discovery (NRS 11.190(3)(d)).",
    },
    "defamation": {
        "default_years": 2,
        "notes": "Most states: 1-2 years. NV: 2 years. CA: 1 year. TX: 1 year.",
    },
    "employment_discrimination": {
        "default_years": 0,
        "notes": "EEOC charge must be filed within 180 or 300 days depending on state. Lawsuit after right-to-sue letter: 90 days federal. State claims vary.",
    },
    "federal_civil_rights_1983": {
        "default_years": 2,
        "notes": "Federal § 1983 claims borrow state personal injury SOL. NV: 2 years. CA: 2 years.",
    },
}


def sol_lookup(practice_area: str) -> dict | None:
    return SOL_REFERENCE.get(practice_area.lower().replace(" ", "_"))


def sol_deadline_estimate(injury_date: str, practice_area: str,
                          jurisdiction: str = "") -> dict:
    """
    Estimate SOL deadline. ALWAYS verify with current statute — this is
    a starting point for the attorney to confirm, not legal advice.
    """
    ref = sol_lookup(practice_area)
    if not ref or ref["default_years"] == 0:
        return {
            "estimated_deadline": None,
            "notes": ref["notes"] if ref else "Unknown practice area. Verify SOL manually.",
            "warning": "Attorney must verify applicable SOL for this matter.",
        }
    try:
        base = date.fromisoformat(injury_date)
        estimated = date(
            base.year + ref["default_years"],
            base.month,
            base.day,
        )
        days_remaining = (estimated - date.today()).days
        return {
            "estimated_deadline": estimated.isoformat(),
            "days_remaining":     days_remaining,
            "years":              ref["default_years"],
            "notes":              ref["notes"],
            "warning":            "VERIFY: SOL varies by jurisdiction, tolling, and specific facts. Attorney must confirm.",
        }
    except (ValueError, OverflowError) as e:
        return {
            "estimated_deadline": None,
            "notes":              ref["notes"],
            "warning":            f"Could not calculate date: {e}",
        }


# ── DOCUMENT TEMPLATE LIBRARY ─────────────────────────────────────────────────

DOCUMENT_TEMPLATES = {
    "demand_letter": {
        "name":        "Demand Letter",
        "description": "Pre-litigation demand for settlement",
        "fields":      ["client_name", "opposing_party", "incident_date",
                        "demand_amount", "deadline_days", "attorney_name",
                        "firm_name", "injuries_summary", "damages_summary"],
        "prompt":      (
            "Draft a professional demand letter. Client: {client_name}. "
            "Opposing party: {opposing_party}. Incident date: {incident_date}. "
            "Demand amount: ${demand_amount}. Response deadline: {deadline_days} days. "
            "Injuries/damages summary: {injuries_summary}. {damages_summary}. "
            "Attorney: {attorney_name}, {firm_name}. "
            "Tone: firm, professional, non-emotional. Include: facts, liability summary, "
            "damages breakdown, demand amount, response deadline, consequences of non-response. "
            "Format: formal business letter. Do NOT include legal advice to the opposing party."
        ),
    },
    "retainer_agreement": {
        "name":        "Retainer Agreement",
        "description": "Attorney-client fee agreement",
        "fields":      ["client_name", "attorney_name", "firm_name", "matter_description",
                        "retainer_amount", "hourly_rate", "jurisdiction"],
        "prompt":      (
            "Draft an attorney-client retainer agreement. "
            "Client: {client_name}. Attorney: {attorney_name}, {firm_name}. "
            "Matter: {matter_description}. Retainer: ${retainer_amount}. "
            "Hourly rate: ${hourly_rate}/hr. Jurisdiction: {jurisdiction}. "
            "Include: scope of representation, fee structure, billing practices, "
            "client responsibilities, withdrawal conditions, file retention policy, "
            "dispute resolution. Note that this is a template for attorney review "
            "and must be adapted to jurisdiction-specific rules of professional conduct."
        ),
    },
    "client_intake": {
        "name":        "Client Intake Summary",
        "description": "Structured new client intake for attorney review",
        "fields":      ["client_name", "client_phone", "client_email",
                        "matter_type", "incident_description",
                        "incident_date", "opposing_party", "injuries",
                        "prior_attorneys", "referral_source"],
        "prompt":      (
            "Prepare a structured client intake summary for attorney review. "
            "Client: {client_name}, {client_phone}, {client_email}. "
            "Matter type: {matter_type}. "
            "Incident: {incident_description}. Date: {incident_date}. "
            "Opposing party: {opposing_party}. "
            "Injuries/damages: {injuries}. "
            "Prior attorneys: {prior_attorneys}. Referral: {referral_source}. "
            "Format: organized sections for attorney review. Flag any immediate "
            "deadline concerns or conflict issues. Note what additional information "
            "is needed before opening a matter."
        ),
    },
    "motion_to_dismiss": {
        "name":        "Motion to Dismiss (Shell)",
        "description": "Framework motion to dismiss — attorney completes argument",
        "fields":      ["case_title", "case_number", "court", "judge",
                        "attorney_name", "firm_name", "client_name",
                        "grounds", "jurisdiction"],
        "prompt":      (
            "Draft a shell motion to dismiss for attorney completion. "
            "Case: {case_title}, No. {case_number}. Court: {court}. Judge: {judge}. "
            "Moving party/client: {client_name}. Attorney: {attorney_name}, {firm_name}. "
            "Grounds: {grounds}. Jurisdiction: {jurisdiction}. "
            "Include: caption, introduction, standard of review placeholder, "
            "argument section headers (attorney fills in substance), conclusion, "
            "certificate of service. Mark all legal argument sections as "
            '[ATTORNEY TO COMPLETE] — this is a structural scaffold only.'
        ),
    },
    "discovery_request": {
        "name":        "Discovery Request Set",
        "description": "Interrogatories + document requests template",
        "fields":      ["case_title", "case_number", "court", "client_name",
                        "opposing_party", "matter_type", "attorney_name"],
        "prompt":      (
            "Draft a discovery request set (interrogatories + document requests). "
            "Case: {case_title}, No. {case_number}. Court: {court}. "
            "Propounding party: {client_name}. Responding party: {opposing_party}. "
            "Matter type: {matter_type}. Attorney: {attorney_name}. "
            "Include 15-20 interrogatories and 15-20 document requests appropriate "
            "for this matter type. Include definitions section, instructions, "
            "and time frame for response. Mark any interrogatories that may need "
            "jurisdiction-specific modification."
        ),
    },
    "settlement_letter": {
        "name":        "Settlement Confirmation Letter",
        "description": "Letter confirming settlement terms to opposing counsel",
        "fields":      ["case_title", "client_name", "opposing_party",
                        "opposing_counsel", "settlement_amount",
                        "payment_deadline", "conditions", "attorney_name", "firm_name"],
        "prompt":      (
            "Draft a settlement confirmation letter to opposing counsel. "
            "Case: {case_title}. Our client: {client_name}. "
            "Opposing party: {opposing_party}. Opposing counsel: {opposing_counsel}. "
            "Settlement amount: ${settlement_amount}. "
            "Payment deadline: {payment_deadline}. "
            "Conditions: {conditions}. "
            "Attorney: {attorney_name}, {firm_name}. "
            "Tone: professional, confirmatory. Reference any agreed conditions, "
            "releases, and next steps (dismissal, etc.)."
        ),
    },
}


def list_templates() -> list:
    return [
        {"key": k, "name": v["name"], "description": v["description"],
         "fields": v["fields"]}
        for k, v in DOCUMENT_TEMPLATES.items()
    ]


def get_template(key: str) -> dict | None:
    return DOCUMENT_TEMPLATES.get(key)


def build_draft_prompt(template_key: str, field_values: dict) -> str | None:
    tmpl = DOCUMENT_TEMPLATES.get(template_key)
    if not tmpl:
        return None
    try:
        return tmpl["prompt"].format(**{
            f: field_values.get(f, f"[{f.upper()}]")
            for f in tmpl["fields"]
        })
    except KeyError:
        return tmpl["prompt"]


# ── DASHBOARD SUMMARY ─────────────────────────────────────────────────────────

# ── DOCUMENT DRAFTS (approval workflow) ──────────────────────────────────────

def _drafts_path(data_dir: Path) -> Path:
    return _legal_dir(data_dir) / "drafts.json"


def save_draft(data_dir: Path, *, matter_id: str, matter_title: str,
               template_key: str, template_name: str,
               content: str, fields: dict) -> dict:
    """Save a freshly generated draft for attorney review."""
    with _LOCK:
        drafts = _load(_drafts_path(data_dir))
        draft = {
            "id":            _new_id(),
            "matter_id":     matter_id,
            "matter_title":  matter_title,
            "template_key":  template_key,
            "template_name": template_name,
            "status":        "draft",   # draft | approved | revised
            "content":       content,
            "fields":        fields,
            "notes":         "",
            "created_at":    _now(),
            "updated_at":    _now(),
        }
        drafts.append(draft)
        _save(_drafts_path(data_dir), drafts)
        return draft


def list_drafts(data_dir: Path, matter_id: str | None = None,
                status: str | None = None) -> list:
    drafts = _load(_drafts_path(data_dir))
    if matter_id:
        drafts = [d for d in drafts if d.get("matter_id") == matter_id]
    if status:
        drafts = [d for d in drafts if d.get("status") == status]
    return sorted(drafts, key=lambda d: d.get("updated_at", 0), reverse=True)


def approve_draft(data_dir: Path, draft_id: str) -> dict | None:
    with _LOCK:
        drafts = _load(_drafts_path(data_dir))
        for d in drafts:
            if d.get("id") == draft_id:
                d["status"] = "approved"
                d["updated_at"] = _now()
                _save(_drafts_path(data_dir), drafts)
                return d
    return None


def revise_draft(data_dir: Path, draft_id: str, new_content: str,
                 notes: str = "") -> dict | None:
    with _LOCK:
        drafts = _load(_drafts_path(data_dir))
        for d in drafts:
            if d.get("id") == draft_id:
                d["content"] = new_content
                d["status"] = "revised"
                d["notes"] = notes
                d["updated_at"] = _now()
                _save(_drafts_path(data_dir), drafts)
                return d
    return None


def get_draft(data_dir: Path, draft_id: str) -> dict | None:
    drafts = _load(_drafts_path(data_dir))
    for d in drafts:
        if d.get("id") == draft_id:
            return d
    return None


# ── LEGAL RESEARCH PROMPT BUILDER ─────────────────────────────────────────────

def build_research_prompt(question: str, jurisdiction: str = "",
                           practice_area: str = "",
                           matter_context: str = "",
                           db_results_block: str = "") -> str:
    """
    Build a research prompt for the LLM. Real database results are injected
    above the instructions so the LLM cites actual cases, not invented ones.
    """
    jx = f" in {jurisdiction}" if jurisdiction else ""
    area = f" ({practice_area})" if practice_area else ""
    context = f"\n\nMatter context: {matter_context}" if matter_context else ""
    db_section = f"\n\n{db_results_block}\n" if db_results_block else ""

    return (
        f"{db_section}"
        f"You are a paralegal preparing a legal research memo for an attorney's review.\n\n"
        f"Research question{area}{jx}: {question}{context}\n\n"
        f"Structure your response as follows:\n\n"
        f"1. ISSUE\nState the precise legal question.\n\n"
        f"2. BRIEF ANSWER\nOne paragraph summary of the answer.\n\n"
        f"3. APPLICABLE LAW\n"
        f"   - Governing statutes (cite by name and code section)\n"
        f"   - Leading cases (cite case name, court, year, and holding)\n"
        f"   - Use ONLY cases from the REAL LEGAL RESEARCH RESULTS above — "
        f"if no cases were found, say so and note the attorney must search manually.\n"
        f"   - Regulatory guidance if relevant\n\n"
        f"4. ANALYSIS\n"
        f"Apply the law to the facts. Address any circuit split, majority/minority "
        f"rule, or jurisdiction-specific variation.\n\n"
        f"5. CONCLUSION\nPractical recommendation for the attorney.\n\n"
        f"6. CAVEATS\n"
        f"Flag: (a) any area where the law may have changed after your training "
        f"cutoff, (b) jurisdiction-specific issues the attorney must verify locally, "
        f"(c) any unsettled areas of law, (d) any citations not found in the "
        f"database results that the attorney must independently verify.\n\n"
        f"End with: 'Attorney should verify all citations and confirm current authority "
        f"before relying on this memo in practice.'\n\n"
        f"Be thorough but practical. The attorney needs usable analysis, not general "
        f"information."
    )


def build_contract_extraction_prompt(contract_text: str, client_side: str = "") -> str:
    """Phase 1: Extract key legal issues and exit grounds from a contract."""
    party_note = f"\nThe client is: {client_side}." if client_side else ""
    return (
        f"You are a contract attorney reviewing a contract for exit strategy analysis.{party_note}\n\n"
        f"CONTRACT TEXT:\n{contract_text[:10000]}\n\n"
        f"Extract and list concisely — stick to what is actually in the contract:\n\n"
        f"1. PARTIES: Who are the parties?\n"
        f"2. KEY OBLIGATIONS: What must each party do? (bullet points, keep brief)\n"
        f"3. TERM AND TERMINATION: Exact quote of the termination/exit clause(s).\n"
        f"4. BREACH PROVISIONS: What constitutes breach? Cure periods?\n"
        f"5. EXIT GROUNDS: ALL potential grounds to exit or void this contract — "
        f"e.g. mutual mistake, fraud, material breach by other party, impossibility, "
        f"unconscionability, illegality, lack of consideration, force majeure, duress.\n"
        f"6. AMBIGUOUS CLAUSES: Flag any language that could be argued in the client's favor.\n"
        f"7. SEARCH TERMS: List 3 specific legal research queries to find relevant case law "
        f"(format each on its own line starting with '- ', be specific, include jurisdiction if known, "
        f"e.g. '- contract rescission mutual mistake Nevada', "
        f"'- commercial lease early termination force majeure').\n\n"
        f"Be factual and precise. Only extract what is in the contract text."
    )


def build_contract_analysis_prompt(contract_text: str, extraction: str,
                                    db_results_block: str = "",
                                    jurisdiction: str = "",
                                    client_side: str = "") -> str:
    """Phase 2: Full exit strategy memo using extraction + real database results."""
    jx = f" in {jurisdiction}" if jurisdiction else ""
    party_note = f"\nClient: {client_side}" if client_side else ""
    db_section = f"\n\n{db_results_block}\n" if db_results_block else ""

    return (
        f"{db_section}"
        f"You are an experienced contract attorney preparing an exit strategy analysis{jx}.{party_note}\n\n"
        f"ISSUES IDENTIFIED FROM CONTRACT:\n{extraction}\n\n"
        f"CONTRACT EXCERPT:\n{contract_text[:3000]}\n\n"
        f"Prepare a CONTRACT EXIT STRATEGY MEMO:\n\n"
        f"1. SITUATION SUMMARY\nParties, what the contract does, what the client needs.\n\n"
        f"2. EXIT OPTIONS (ranked best to least viable)\n"
        f"For each viable exit route:\n"
        f"   a) Legal theory (breach, rescission, frustration of purpose, etc.)\n"
        f"   b) Required facts/evidence\n"
        f"   c) Strength: Strong / Moderate / Weak — and why\n"
        f"   d) Supporting case law from the REAL LEGAL RESEARCH RESULTS above "
        f"(cite ONLY cases found there — if none found, say so)\n"
        f"   e) Risks and downsides\n\n"
        f"3. BEST STRATEGY\nOne clear recommended path with specific next steps.\n\n"
        f"4. LEVERAGE POINTS\n"
        f"Provisions, ambiguities, or counterclaims the client can use as negotiating "
        f"leverage even without a clean exit.\n\n"
        f"5. IMMEDIATE ACTION ITEMS\nWhat the attorney must do in the next 7-30 days.\n\n"
        f"6. RISKS AND CAVEATS\n"
        f"What could go wrong, jurisdiction-specific issues, anything requiring independent "
        f"verification. Use ONLY cases from the database results. Never invent citations.\n\n"
        f"End with: 'Prepared for attorney review — verify all citations before relying "
        f"on this memo in practice.'"
    )


def build_intake_prompt(intake_data: dict) -> str:
    """Build a structured intake summary prompt."""
    return (
        f"You are a paralegal preparing a new client intake summary for attorney review.\n\n"
        f"Client information provided:\n{json.dumps(intake_data, indent=2)}\n\n"
        f"Prepare a structured intake memo with these sections:\n\n"
        f"1. CLIENT INFORMATION\n"
        f"2. MATTER SUMMARY (what happened, when, who's involved)\n"
        f"3. POTENTIAL CLAIMS (list each possible claim with elements)\n"
        f"4. STATUTE OF LIMITATIONS FLAGS (list any SOL concerns with warning to verify)\n"
        f"5. CONFLICT CHECK NEEDED (list all names attorney must check)\n"
        f"6. IMMEDIATE ACTION ITEMS (what must happen in the next 30 days)\n"
        f"7. INFORMATION STILL NEEDED (what to gather from client)\n"
        f"8. RECOMMENDED NEXT STEPS\n\n"
        f"Flag any red flags clearly. Mark every SOL date estimate with "
        f"'[VERIFY SOL — jurisdiction-specific]'.\n"
        f"End with: 'Prepared for attorney review — do not share with client.'"
    )


# ── DASHBOARD SUMMARY ─────────────────────────────────────────────────────────

def dashboard_summary(data_dir: Path) -> dict:
    """Quick numbers for owner dashboard / morning brief."""
    matters = _load(_matters_path(data_dir))
    active = [m for m in matters if m.get("status") not in ("closed", "settled")]
    deadlines_30 = list_deadlines(data_dir, upcoming_days=30, status="pending")
    deadlines_7 = [d for d in deadlines_30
                   if _days_until(d.get("due_date", "")) <= 7]
    entries = _load(_time_path(data_dir))
    unbilled = [e for e in entries if not e.get("billed")]
    unbilled_amount = sum(e.get("amount", 0) for e in unbilled)
    return {
        "active_matters":      len(active),
        "total_matters":       len(matters),
        "deadlines_next_7":    len(deadlines_7),
        "deadlines_next_30":   len(deadlines_30),
        "unbilled_hours":      round(sum(e.get("hours", 0) for e in unbilled), 2),
        "unbilled_amount":     round(unbilled_amount, 2),
    }


def _days_until(due_date_str: str) -> int:
    try:
        return (date.fromisoformat(due_date_str) - date.today()).days
    except ValueError:
        return 9999
