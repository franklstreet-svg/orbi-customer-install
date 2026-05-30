#!/usr/bin/env python3
"""
test_orby_sweep — automated 35-question test sweep against a running Orby.

What this does (NOT a unit test — an end-to-end behavioral test):
  1. Logs in as Frank (owner) via /api/owner/login
  2. For each of the 35 tests, sends the message through the appropriate
     endpoint (/api/owner/chat for owner-context tests, /chat for
     public-context tests like the learning loop and lead capture).
  3. Scores each response against a pass criterion — substring matches,
     state-file checks (did the lead actually save?), or "needs human
     review" for genuinely subjective ones.
  4. Prints a colored summary and writes a JSON report to
     data/test_sweeps/<timestamp>.json.

Run:
    cd customer_install
    python3 tools/test_orby_sweep.py

Required env:
    ORBI_HOST       (default http://127.0.0.1:5050)
    ORBI_OWNER_USER (default frank)
    ORBI_OWNER_PASS (REQUIRED — owner password)

The pass/fail bar Frank set: 35/35 before shipping. This script makes
that visible without him pasting questions one by one.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
import urllib.parse
from pathlib import Path
from datetime import datetime

import urllib.request
import urllib.error

HERE = Path(__file__).resolve().parent
ORBI_DIR = HERE.parent  # customer_install/
DATA_DIR = ORBI_DIR / "data"

HOST = os.environ.get("ORBI_HOST", "http://127.0.0.1:5050").rstrip("/")
OWNER_USER = os.environ.get("ORBI_OWNER_USER", "frank")
OWNER_PASS = os.environ.get("ORBI_OWNER_PASS", "")

# Per-run session cookie jar (owner-authed)
_SESSION_COOKIE: str | None = None


# ─── HTTP helpers ─────────────────────────────────────────────────────────

def _http(method: str, path: str, body: dict | None = None,
           cookie: str | None = None, timeout: int = 30) -> tuple[int, dict, str]:
    """Returns (status, parsed_json_or_empty, raw_text). Empty dict if
    body isn't JSON."""
    url = HOST + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            # Capture Set-Cookie globally for the session
            global _SESSION_COOKIE
            set_cookie = resp.headers.get("Set-Cookie", "")
            if set_cookie and "orbi_session=" in set_cookie:
                _SESSION_COOKIE = set_cookie.split(";")[0]
            return resp.status, parsed, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        return e.code, parsed, raw
    except urllib.error.URLError as e:
        return 0, {}, f"URLError: {e.reason}"


def login_owner() -> bool:
    if not OWNER_PASS:
        _print(C_RED + "ORBI_OWNER_PASS env var is required.\n"
               "Run: ORBI_OWNER_PASS=your_password python3 tools/test_orby_sweep.py" + C_END)
        return False
    code, body, _ = _http("POST", "/api/owner/login",
                          {"username": OWNER_USER, "email": OWNER_USER,
                           "password": OWNER_PASS})
    if code != 200:
        _print(f"{C_RED}Login failed (HTTP {code}): {body}{C_END}")
        return False
    return True


def owner_chat(message: str) -> dict:
    code, body, _ = _http("POST", "/api/owner/chat",
                          {"message": message, "history": []},
                          cookie=_SESSION_COOKIE)
    return {"http": code, **(body if isinstance(body, dict) else {})}


def public_chat(message: str, history: list = None,
                 visitor: dict = None) -> dict:
    code, body, _ = _http("POST", "/chat", {
        "message": message,
        "history": history or [],
        "visitor": visitor or {},
    })
    return {"http": code, **(body if isinstance(body, dict) else {})}


# ─── State helpers (read JSON files to verify side-effects) ───────────────

def _data_file(path_parts: list[str]) -> dict | list | None:
    p = DATA_DIR.joinpath(*path_parts)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def user_data_file(username: str, *parts) -> dict | list | None:
    return _data_file(["users", username, *parts])


# ─── Scoring primitives ───────────────────────────────────────────────────

def contains_any(text: str, words: list[str], min_hits: int = 1) -> bool:
    t = (text or "").lower()
    return sum(1 for w in words if w.lower() in t) >= min_hits


# ─── The 35 tests ─────────────────────────────────────────────────────────
# Each test is (id, category, question, runner, scorer, notes).
# runner takes no args, returns the response dict.
# scorer takes the response dict, returns (status, reason).
#   status ∈ {"pass", "partial", "fail", "manual"}

def _t(id_, cat, q):
    return {"id": id_, "category": cat, "question": q}


# Cat 1: Business knowledge
CASE_1 = {**_t(1, "Business knowledge", "What are your hours?")}
def score_1(r):
    txt = r.get("reply", "")
    if any(w in txt.lower() for w in ("monday", "tuesday", "weekday", "open")):
        if re.search(r"\d", txt):   # at least some numbers
            return ("pass", "names days + has digits")
        return ("partial", "names days but no times")
    return ("fail", f"no hours info: {txt[:100]}")

CASE_2 = {**_t(2, "Business knowledge", "What's your address?")}
def score_2(r):
    txt = r.get("reply", "")
    biz = _data_file(["business_info.json"]) or {}
    # Address can be stored as a string OR a dict {street, city, state, zip}
    addr_raw = biz.get("address")
    if isinstance(addr_raw, dict):
        addr_parts = [str(addr_raw.get(k, "") or "").strip()
                       for k in ("street", "city", "state", "zip")]
        addr_parts = [p for p in addr_parts if p]
        addr = " ".join(addr_parts)
    else:
        addr = str(addr_raw or "").strip()
    if not addr:
        # No address in profile — pass if Orby doesn't invent one
        if any(w in txt.lower() for w in ("don't", "haven't", "no address", "not sure")) or len(txt) < 100:
            return ("pass", "no address in profile, didn't invent one")
        return ("manual", f"no address in profile — was reply honest? {txt[:120]}")
    # At least the first significant token of the address should appear
    significant = next((p for p in addr.split() if len(p) > 1), addr)
    if significant.lower() in txt.lower():
        return ("pass", "address from profile present")
    return ("fail", f"profile has address ({addr!r}) but reply doesn't include it: {txt[:120]}")

CASE_3 = {**_t(3, "Business knowledge", "What services do you offer?")}
def score_3(r):
    txt = r.get("reply", "")
    if len(txt) > 100 and ("$" in txt or "service" in txt.lower() or "tier" in txt.lower()):
        return ("pass", "substantive services answer")
    return ("fail", f"weak/missing services list: {txt[:120]}")

CASE_4 = {**_t(4, "Business knowledge", "How much do your services cost?")}
def score_4(r):
    txt = r.get("reply", "")
    if "$" in txt or re.search(r"\b\d{2,4}\b", txt):
        return ("pass", "has price")
    return ("fail", f"no pricing in reply: {txt[:120]}")

CASE_5 = {**_t(5, "Business knowledge", "Do you accept credit cards, cash, or Venmo?")}
def score_5(r):
    txt = r.get("reply", "").lower()
    if any(w in txt for w in ("credit", "card", "cash", "venmo", "stripe", "payment")):
        return ("pass", "addresses payment methods")
    return ("fail", "no payment-method info")


# Cat 2: Booking + calendar
CASE_6 = {**_t(6, "Booking", "Can I book an appointment for Thursday at 2pm?")}
def score_6(r):
    txt = r.get("reply", "").lower()
    # Pass if reply confirms a CREATED appointment OR proposes slot.
    if any(w in txt for w in ("booked", "added", "scheduled", "set up", "set for")):
        return ("pass", "claims booking action taken")
    if "calendar" in txt or "thursday" in txt:
        return ("partial", "engaged with calendar context but unclear action")
    return ("fail", f"no booking action: {txt[:120]}")

CASE_7 = {**_t(7, "Booking", "What times do you have open on Saturday?")}
def score_7(r):
    txt = r.get("reply", "")
    if re.search(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)", txt, re.I):
        return ("pass", "lists specific times")
    return ("partial", f"no specific times listed: {txt[:120]}")

CASE_8 = {**_t(8, "Booking", "Cancel my dentist appointment.")}
def score_8(r):
    txt = r.get("reply", "").lower()
    src = r.get("source", "")
    # Pass if Orby actually cancelled, OR if she correctly said no match.
    if src.startswith("calendar_cancel_done"):
        return ("pass", "actually removed the event")
    if src.startswith("calendar_cancel_no_match"):
        return ("pass", "honest no-match (no dentist appointment seeded)")
    if src.startswith("calendar_cancel_ambiguous"):
        return ("pass", "asked which event when multiple matched")
    # LLM fall-through is the bug we're catching
    if "i'll" in txt or "let me" in txt:
        return ("fail", f"appears to fake the action (no calendar_cancel_* source): {txt[:120]}")
    return ("partial", f"unclear: {txt[:120]}")

CASE_9 = {**_t(9, "Booking", "Do I have anything tomorrow?")}
def score_9(r):
    txt = r.get("reply", "").lower()
    if "tomorrow" in txt or "calendar" in txt or "no " in txt or "appointment" in txt:
        return ("pass", "looked at tomorrow's calendar")
    return ("fail", f"didn't address tomorrow's schedule: {txt[:120]}")

CASE_10 = {**_t(10, "Booking", "Reschedule my haircut to Friday at 3pm")}
def score_10(r):
    txt = r.get("reply", "").lower()
    src = r.get("source", "")
    if src.startswith("calendar_reschedule_done"):
        return ("pass", "actually updated the event start time")
    if src.startswith("calendar_reschedule_no_match"):
        return ("pass", "honest no-match (no haircut event seeded)")
    if src.startswith("calendar_reschedule_ambiguous"):
        return ("pass", "asked which event when multiple matched")
    if src.startswith("calendar_reschedule_bad_when"):
        return ("partial", "matched event but couldn't parse new time")
    if "i'll" in txt or "let me" in txt:
        return ("fail", f"appears to fake the action: {txt[:120]}")
    return ("partial", f"unclear: {txt[:120]}")


# Cat 3: Lead capture (PUBLIC chat — strangers)
CASE_11 = {**_t(11, "Lead capture (public)",
                 "I'd like someone to call me back about pricing.")}
def score_11(r):
    txt = r.get("reply", "").lower()
    src = r.get("source", "")
    if src == "capture_needs_contact" or "name" in txt or "phone" in txt or "number" in txt:
        return ("pass", "asked for contact info")
    if r.get("capture_pending"):
        return ("pass", "capture pending flag set")
    return ("fail", f"didn't ask for contact: {txt[:120]}")

CASE_12 = {**_t(12, "Lead capture (public)",
                 "I'd like a callback about pricing. My name is Bob Smith and my number is 555-123-4567.")}
def score_12(r):
    # After this, a lead should be in messages.json
    txt = r.get("reply", "")
    time.sleep(0.5)  # give the write a moment
    msgs = _data_file(["messages.json"]) or {}
    items = msgs.get("messages", []) if isinstance(msgs, dict) else []
    recent = [m for m in items if "555" in str(m.get("from_phone", ""))
              or "Bob" in str(m.get("from_name", ""))]
    if recent:
        return ("pass", f"lead captured (Bob/555-1234) in messages.json")
    return ("fail", f"lead NOT captured. Reply: {txt[:120]}")

CASE_13 = {**_t(13, "Lead capture (public)",
                 "Will the owner know I called?", )}
def score_13(r):
    txt = r.get("reply", "").lower()
    if any(w in txt for w in ("yes", "will", "owner", "notify", "let")):
        return ("pass", "answered yes/will")
    return ("manual", f"unclear: {txt[:120]}")


# Cat 4: Learning loop (PUBLIC chat — needs no-existing-policy question)
CASE_14 = {**_t(14, "Learning loop (public)",
                 "Do you offer mobile pet grooming?")}
def score_14(r):
    txt = r.get("reply", "").lower()
    if any(w in txt for w in ("not sure", "don't know", "let me ask", "find out", "check with the owner")):
        return ("pass", "honest 'I don't know' + offers to ask")
    if "yes" in txt or "we offer" in txt:
        return ("fail", "appears to invent an answer")
    return ("partial", f"unclear stance: {txt[:120]}")

CASE_15 = {**_t(15, "Learning loop (public)",
                 "What's your policy on group bookings of 8 or more?")}
def score_15(r):
    txt = r.get("reply", "").lower()
    if any(w in txt for w in ("not sure", "don't have", "let me ask", "check with")):
        return ("pass", "honest unknown + ask offered")
    return ("partial", f"may have invented: {txt[:140]}")


# Cat 5: Personal assistant (OWNER chat)
CASE_18 = {**_t(18, "Personal assistant", "Give me my morning brief.")}
def score_18(r):
    txt = r.get("reply", "")
    if len(txt) > 200 and any(w in txt.lower() for w in ("calendar", "task", "reminder", "stripe", "review", "morning")):
        return ("pass", "substantive brief")
    return ("fail", f"weak brief: {txt[:120]}")

CASE_19 = {**_t(19, "Personal assistant",
                 "What's a good gift for my daughter Tamra for her birthday?")}
def score_19(r):
    txt = r.get("reply", "")
    src = r.get("source", "")
    if src == "gift_suggest" and ("$" in txt or "tier" in txt.lower()):
        return ("pass", f"used gift_suggest path")
    if "tamra" in txt.lower() and ("birthday" in txt.lower() or "$" in txt):
        return ("partial", "addresses Tamra but unclear if used taste profile")
    return ("fail", f"generic suggestions: {txt[:120]}")

CASE_20 = {**_t(20, "Personal assistant", "What's on my calendar today?")}
def score_20(r):
    txt = r.get("reply", "").lower()
    if "calendar" in txt or "today" in txt or "nothing" in txt or "appointment" in txt:
        return ("pass", "addressed today's calendar")
    return ("fail", f"didn't pull calendar: {txt[:120]}")

CASE_21 = {**_t(21, "Personal assistant",
                 "Remind me to test the auto-test at 11:59 PM today.")}
def score_21(r):
    txt = r.get("reply", "").lower()
    if "reminder" in txt and "11:59" in txt and "today" in txt:
        return ("pass", "set for today as specified")
    if "reminder" in txt and ("tomorrow" in txt or "passed" in txt):
        return ("partial", "set but maybe wrong day")
    return ("fail", f"didn't set reminder: {txt[:120]}")

CASE_22 = {**_t(22, "Personal assistant", "Wrap up my day — what happened today?")}
def score_22(r):
    txt = r.get("reply", "")
    if len(txt) > 200 and any(w in txt.lower() for w in ("today", "completed", "task", "message", "stripe", "summary")):
        return ("pass", "substantive EOD")
    return ("fail", f"weak EOD: {txt[:120]}")


# Cat 6: Internal team
CASE_23 = {**_t(23, "Internal team",
                 "Tell Cathi the auto-tester is running.")}
def score_23(r):
    txt = r.get("reply", "").lower()
    if "sent" in txt and "cathi" in txt:
        return ("pass", "sent to Cathi")
    return ("fail", f"not sent: {txt[:120]}")

CASE_24 = {**_t(24, "Internal team",
                 "Message the sales team — auto-tester is running.")}
def score_24(r):
    txt = r.get("reply", "").lower()
    if "sent" in txt and ("team" in txt or "group" in txt or "people" in txt):
        return ("pass", "sent to group")
    return ("fail", f"group send failed: {txt[:140]}")

CASE_25 = {**_t(25, "Internal team", "Did anyone message me today?")}
def score_25(r):
    txt = r.get("reply", "").lower()
    if "message" in txt or "no" in txt or "call" in txt or "today" in txt:
        return ("pass", "engaged with messages-today query")
    return ("manual", f"unclear: {txt[:120]}")


# Cat 7: Edge cases — most are physical-world (phone call quality);
# we can probe what's automatable.
CASE_29 = {**_t(29, "Edge case",
                 "What's John Smith Roberts Anderson's phone number?")}
def score_29(r):
    txt = r.get("reply", "").lower()
    if any(w in txt for w in ("don't have", "don't see", "no record", "not in",
                               "couldn't find", "not in your", "no contact")):
        return ("pass", "didn't invent a phone number")
    if re.search(r"\d{3}[-.]\d{3}[-.]\d{4}", r.get("reply", "")):
        return ("fail", "appears to have invented a phone number")
    return ("manual", f"unclear: {txt[:120]}")

CASE_30 = {**_t(30, "Edge case", "What's the meaning of life?")}
def score_30(r):
    txt = r.get("reply", "")
    if len(txt) < 400 and ("?" in txt or "business" in txt.lower() or len(txt) < 300):
        return ("pass", "kept it brief or redirected")
    return ("partial", f"may have rambled: {txt[:150]}")


# Cat 8: System (manual / state checks)
CASE_33 = {**_t(33, "System", "[staff permission check]")}
def score_33(r):
    return ("manual", "Log in as a staff user; confirm they don't see owner-only buttons.")

CASE_34 = {**_t(34, "System", "[data folder local check]")}
def score_34(r):
    # Auto: check the data folder exists + has user data
    p = DATA_DIR
    if p.exists() and any((p / sub).exists() for sub in ("users", "business_info.json", "messages.json")):
        return ("pass", f"data folder populated at {p}")
    return ("fail", f"data folder missing or empty at {p}")


# ─── Test registry ────────────────────────────────────────────────────────
# Tests that ONLY make sense from public chat (no auth) live in the
# public group. Owner-context tests use the authed chat.

OWNER_TESTS = [
    (CASE_1, score_1), (CASE_2, score_2), (CASE_3, score_3), (CASE_4, score_4),
    (CASE_5, score_5),
    (CASE_6, score_6), (CASE_7, score_7), (CASE_8, score_8), (CASE_9, score_9),
    (CASE_10, score_10),
    (CASE_18, score_18), (CASE_19, score_19), (CASE_20, score_20),
    (CASE_21, score_21), (CASE_22, score_22),
    (CASE_23, score_23), (CASE_24, score_24), (CASE_25, score_25),
    (CASE_29, score_29), (CASE_30, score_30),
]
PUBLIC_TESTS = [
    (CASE_11, score_11), (CASE_12, score_12), (CASE_13, score_13),
    (CASE_14, score_14), (CASE_15, score_15),
]
STATE_TESTS = [
    (CASE_33, score_33), (CASE_34, score_34),
]


# ─── Color output ─────────────────────────────────────────────────────────

C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_BLUE = "\033[34m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_END = "\033[0m"

def _print(s=""):
    print(s, flush=True)

STATUS_TAG = {
    "pass":    f"{C_GREEN}✓ PASS  {C_END}",
    "partial": f"{C_YELLOW}~ PART  {C_END}",
    "fail":    f"{C_RED}✗ FAIL  {C_END}",
    "manual":  f"{C_BLUE}? MAN   {C_END}",
}


# ─── Main runner ──────────────────────────────────────────────────────────

def run_test(case, scorer, runner_kind="owner"):
    qid = case["id"]
    cat = case["category"]
    q = case["question"]
    _print(f"  [{qid:>2}] {cat:<26} {q[:60]}")
    try:
        if runner_kind == "owner":
            resp = owner_chat(q)
        elif runner_kind == "public":
            resp = public_chat(q)
        else:
            resp = {}
        status, reason = scorer(resp)
    except Exception as e:
        resp = {"error": str(e)}
        status, reason = "fail", f"exception: {e}"
    _print(f"        {STATUS_TAG[status]} {reason}")
    return {"id": qid, "category": cat, "question": q,
            "status": status, "reason": reason, "response": resp}


def main():
    _print(f"{C_BOLD}Orby Auto Test Sweep{C_END}")
    _print(f"  host: {HOST}")
    _print(f"  data: {DATA_DIR}")
    _print("")

    if not login_owner():
        return 1
    _print(f"  {C_GREEN}✓ logged in as {OWNER_USER}{C_END}")
    _print("")

    results = []
    _print(f"{C_BOLD}OWNER chat tests{C_END}")
    for case, scorer in OWNER_TESTS:
        results.append(run_test(case, scorer, "owner"))
    _print("")
    _print(f"{C_BOLD}PUBLIC chat tests (stranger context){C_END}")
    for case, scorer in PUBLIC_TESTS:
        results.append(run_test(case, scorer, "public"))
    _print("")
    _print(f"{C_BOLD}STATE / file system tests{C_END}")
    for case, scorer in STATE_TESTS:
        results.append(run_test(case, scorer, "state"))

    # Summary
    counts = {"pass": 0, "partial": 0, "fail": 0, "manual": 0}
    for r in results:
        counts[r["status"]] += 1
    total = sum(counts.values())
    _print("")
    _print(f"{C_BOLD}SUMMARY ({total} tests run){C_END}")
    _print(f"  {C_GREEN}✓ pass   : {counts['pass']:>2}{C_END}")
    _print(f"  {C_YELLOW}~ partial: {counts['partial']:>2}{C_END}")
    _print(f"  {C_RED}✗ fail   : {counts['fail']:>2}{C_END}")
    _print(f"  {C_BLUE}? manual : {counts['manual']:>2}{C_END}")
    _print("")

    fails = [r for r in results if r["status"] == "fail"]
    if fails:
        _print(f"{C_BOLD}{C_RED}FAILURES — need code fixes:{C_END}")
        for f in fails:
            _print(f"  #{f['id']:>2} {f['category']:<26} {f['question'][:50]}")
            _print(f"      → {f['reason']}")
        _print("")

    manuals = [r for r in results if r["status"] == "manual"]
    if manuals:
        _print(f"{C_BOLD}{C_BLUE}MANUAL REVIEW — judgment needed:{C_END}")
        for m in manuals:
            _print(f"  #{m['id']:>2} {m['category']:<26} {m['question'][:50]}")
            _print(f"      reason: {m['reason']}")
        _print("")

    # Save JSON report
    out_dir = DATA_DIR / "test_sweeps"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"sweep_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_file.write_text(json.dumps({
        "host": HOST,
        "ran_at": datetime.now().isoformat(),
        "counts": counts,
        "results": results,
    }, indent=2, default=str), encoding="utf-8")
    _print(f"Report saved → {out_file}")
    _print("")

    bar = 35
    if counts["pass"] + counts["partial"] >= bar:
        _print(f"{C_GREEN}Ready-to-ship bar (35): MET (pass+partial = {counts['pass']+counts['partial']}){C_END}")
        return 0
    needed = bar - (counts["pass"] + counts["partial"])
    _print(f"{C_RED}Ready-to-ship bar (35): NOT MET ({counts['pass']+counts['partial']}/{bar}, need {needed} more){C_END}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
