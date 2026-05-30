#!/usr/bin/env python3
"""
test_contractor_module — automated sweep of the Contractor module's
~45 chat intents + the public client-facing routes.

Pattern matches tools/test_orby_sweep.py: log in as Frank, send each
chat probe, check the response source/content. Adds public-route HTTP
GETs/POSTs for the portal + sign + review flows.

Run:
    cd customer_install
    ORBI_OWNER_PASS=<frank pw> python3 tools/test_contractor_module.py

For a clean run, reseed first:
    python3 tools/seed_demo_contractor.py --reset

Exit code 0 = every test passed, 1 = any failure or manual review needed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).resolve().parent
ORBI_DIR = HERE.parent
DATA_DIR = ORBI_DIR / "data"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ORBI_DIR))

# Reuse the auth helpers from the existing sweep
from test_orby_sweep import (
    _http, login_owner, owner_chat, public_chat,
    C_GREEN, C_YELLOW, C_RED, C_BLUE, C_DIM, C_BOLD, C_END,
    STATUS_TAG, _print, HOST,
)


# ─── Test registry ────────────────────────────────────────────────────────

CASES = []  # list of (id, category, question, scorer, kind)


def test_case(id_, cat, q, scorer, kind="owner"):
    CASES.append({"id": id_, "category": cat, "question": q,
                  "scorer": scorer, "kind": kind})


# ─── Scorers — small lambdas keyed to the response source ────────────────

def expects_source(name: str, also_contains: str | None = None,
                    fail_msg: str = ""):
    def _scorer(r):
        src = r.get("source", "")
        reply = (r.get("reply") or "").lower()
        if name in src:
            if also_contains and also_contains.lower() not in reply:
                return ("partial", f"correct source but reply missing {also_contains!r}")
            return ("pass", f"source={src}")
        return ("fail", fail_msg or f"expected source~='{name}', got '{src}'. reply: {reply[:120]}")
    return _scorer


def expects_contains(needle: str, fail_msg: str = ""):
    def _scorer(r):
        reply = (r.get("reply") or "").lower()
        if needle.lower() in reply:
            return ("pass", f"contains {needle!r}")
        return ("fail", fail_msg or f"missing {needle!r}: {reply[:120]}")
    return _scorer


# ─── Cat 1: Projects ──────────────────────────────────────────────────────

test_case("p1", "Projects",
          "what jobs are open",
          expects_source("gc_jobs_listed"))

test_case("p2", "Projects",
          "status of the Oak project",
          expects_source("gc_project_status"))

test_case("p3", "Projects",
          "full report on Oak",
          expects_source("gc_project_report"))

test_case("p4", "Projects",
          "new project at 999 Test Drive — $7,000 deck repair",
          expects_source("gc_project_added"))


# ─── Cat 2: Change Orders (on-site flow) ──────────────────────────────────

def _scorer_co_drafted_with_sign_url(r):
    src = r.get("source", "")
    if "gc_co_drafted_for_sign" not in src:
        return ("fail", f"expected on-site sign flow, got source={src}")
    reply = r.get("reply", "")
    if "/co/sign/" not in reply:
        return ("fail", "reply missing /co/sign/ URL")
    return ("pass", "draft created + sign URL in reply")

test_case("co1", "Change Orders",
          "CO on Oak — $450 add 20A outlet over the dishwasher",
          _scorer_co_drafted_with_sign_url)

test_case("co2", "Change Orders",
          "pending COs",
          lambda r: ("pass", r.get("source", ""))
                     if "gc_" in r.get("source", "") else
                     ("fail", "no contractor-source response"))

test_case("co3", "Change Orders",
          "leak check",
          lambda r: ("pass", r.get("source", ""))
                     if r.get("source", "").startswith("gc_leak") else
                     ("fail", f"unexpected: {r.get('source','')}"))


# ─── Cat 3: Invoicing + Receivables ───────────────────────────────────────

test_case("i1", "Invoicing",
          "who owes me money",
          expects_source("gc_unpaid"))

test_case("i2", "Invoicing",
          "aging report",
          lambda r: ("pass", r.get("source",""))
                     if r.get("source","").startswith("gc_aging") else
                     ("fail", f"unexpected: {r.get('source','')}"))

test_case("i3", "Invoicing",
          "send invoice for Oak — $4,500 progress draw",
          expects_source("gc_inv_created"))

test_case("i4", "Invoicing",
          "PDF INV-2026-0001",
          lambda r: ("pass", "PDF generated")
                     if r.get("source","").startswith("gc_pdf_generated") else
                     ("fail", f"unexpected: {r.get('source','')}"))

test_case("i5", "Invoicing",
          "received $1000 on INV-2026-0001",
          lambda r: ("pass", r.get("source",""))
                     if r.get("source","").startswith("gc_payment") else
                     ("fail", f"unexpected: {r.get('source','')}"))


# ─── Cat 4: Receivables nudges ────────────────────────────────────────────

test_case("n1", "Receivables nudges",
          "check for overdue payments",
          lambda r: ("pass", r.get("source",""))
                     if r.get("source","").startswith("gc_sweep") else
                     ("fail", f"unexpected: {r.get('source','')}"))

test_case("n2", "Receivables nudges",
          "show queued reminders",
          lambda r: ("pass", r.get("source",""))
                     if r.get("source","").startswith("gc_queue") else
                     ("fail", f"unexpected: {r.get('source','')}"))


# ─── Cat 5: Bids ──────────────────────────────────────────────────────────

test_case("b1", "Bids",
          "sent bid for TestPerson NewName at 7777 Sweep Court — $18,500 deck",
          expects_source("gc_bid_added"))

test_case("b2", "Bids",
          "open bids",
          expects_source("gc_bids_listed"))

test_case("b3", "Bids",
          "win rate",
          expects_source("gc_bid_report"))

test_case("b4", "Bids",
          "proposal for TestPerson",
          lambda r: ("pass", "proposal PDF")
                     if r.get("source","").startswith("gc_proposal_generated") else
                     ("fail", f"unexpected: {r.get('source','')}"))


# ─── Cat 6: Subcontractors ────────────────────────────────────────────────

test_case("s1", "Subs",
          "subs",
          expects_source("gc_subs_listed"))

test_case("s2", "Subs",
          "subs for plumbing",
          expects_source("gc_subs_listed", also_contains="plumbing"))

test_case("s3", "Subs",
          "whose insurance is expiring",
          lambda r: ("pass", r.get("source",""))
                     if r.get("source","").startswith("gc_insurance") else
                     ("fail", f"unexpected: {r.get('source','')}"))

test_case("s4", "Subs",
          "what's Bob working on",
          lambda r: ("pass", r.get("source",""))
                     if r.get("source","").startswith("gc_sub_schedule") else
                     ("fail", f"unexpected: {r.get('source','')}"))


# ─── Cat 7: Daily logs ────────────────────────────────────────────────────

test_case("l1", "Daily logs",
          "logs for Oak",
          expects_source("gc_logs_project"))

test_case("l2", "Daily logs",
          "this week's logs",
          expects_source("gc_logs_week"))


# ─── Cat 8: Reviews ───────────────────────────────────────────────────────

test_case("r1", "Reviews",
          "review link for Birch",
          expects_source("gc_review_link_issued"))

test_case("r2", "Reviews",
          "my rating",
          lambda r: ("pass", r.get("source",""))
                     if r.get("source","").startswith("gc_rating") else
                     ("fail", f"unexpected: {r.get('source','')}"))


# ─── Cat 9: Closeout ──────────────────────────────────────────────────────

test_case("cl1", "Closeout",
          "closeout for Birch",
          lambda r: ("pass", "closeout PDF generated")
                     if r.get("source","").startswith("gc_closeout_generated") else
                     ("fail", f"unexpected: {r.get('source','')}"))


# ─── Cat 10: Portal (chat side — minting) ─────────────────────────────────

def _scorer_share_portal(r):
    if r.get("source") != "gc_portal_shared":
        return ("fail", f"unexpected source: {r.get('source','?')}")
    reply = r.get("reply", "")
    if "/p/" not in reply:
        return ("fail", "no /p/ URL in reply")
    return ("pass", "portal URL minted")

test_case("port1", "Portal",
          "share Oak with the client",
          _scorer_share_portal)


# ─── Cat 11: Reporting ───────────────────────────────────────────────────

test_case("rep1", "Reporting",
          "morning brief",
          expects_source("morning_brief"))

test_case("rep2", "Reporting",
          "tomorrow's brief",
          expects_source("tomorrow_brief"))

test_case("rep3", "Reporting",
          "weekly recap",
          expects_source("gc_weekly_recap"))

test_case("rep4", "Reporting",
          "show me the money",
          expects_source("gc_report"))

test_case("rep5", "Reporting",
          "help",
          expects_contains("CHANGE ORDERS",
                            fail_msg="help text didn't include change orders section"))


# ─── Public route tests (HTTP, not chat) ──────────────────────────────────

def _public_route_test(case_id, cat, description, runner_fn):
    """Wrap a function that talks to public HTTP routes into a test case."""
    def _scorer(_r):
        try:
            ok, reason = runner_fn()
        except Exception as e:
            return ("fail", f"exception: {e}")
        return ("pass" if ok else "fail", reason)
    test_case(case_id, cat, description, _scorer, kind="public_route")


def _test_portal_renders():
    # Get a portal URL via chat
    r = owner_chat("share Maple with the client")
    m = re.search(r"https?://[^\s]+/p/[A-Za-z0-9_-]+", r.get("reply", ""))
    if not m:
        return False, "couldn't mint portal URL via chat"
    body = urllib.request.urlopen(m.group(0), timeout=5).read().decode("utf-8")
    if "Money" not in body or "Recent activity" not in body and "Need something" not in body:
        return False, "portal page didn't render expected sections"
    return True, "portal page renders + has money + activity/CTA sections"


def _test_portal_change_request():
    r = owner_chat("share Cedar with the client")
    m = re.search(r"https?://[^\s]+/p/[A-Za-z0-9_-]+", r.get("reply", ""))
    if not m:
        return False, "no portal URL"
    portal_url = m.group(0)
    # Visit the change-request form
    form_url = portal_url + "/request-change"
    body = urllib.request.urlopen(form_url, timeout=5).read().decode("utf-8")
    if "Request a change" not in body:
        return False, "request form did not render"
    # Submit it
    data = urllib.parse.urlencode({
        "description": "Add a step lamp by the back stairs (test).",
        "estimated_cost": "$200",
    }).encode()
    req = urllib.request.Request(form_url, data=data, method="POST")
    body = urllib.request.urlopen(req, timeout=5).read().decode("utf-8")
    if "Request sent" not in body:
        return False, "post failed"
    return True, "request form GET + POST both worked"


def _test_review_flow():
    r = owner_chat("review link for Birch")
    m = re.search(r"https?://[^\s]+/r/[A-Za-z0-9_-]+", r.get("reply", ""))
    if not m:
        return False, "no review URL from chat"
    url = m.group(0)
    body = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
    if "star" not in body.lower():
        return False, "review form didn't render"
    # Submit a 5-star
    data = urllib.parse.urlencode({
        "rating": "5",
        "comment": "Auto-test rating.",
        "recommend": "yes",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    body = urllib.request.urlopen(req, timeout=5).read().decode("utf-8")
    if "Got it" not in body and "Thanks" not in body:
        return False, "review POST failed"
    return True, "review form GET + POST"


def _test_co_sign_flow():
    # Create a fresh CO with the on-site flow + sign it
    r = owner_chat("CO on Cedar — $300 add a deck light fixture")
    sign_url_match = re.search(r"https?://[^\s]+/co/sign/[A-Za-z0-9_-]+",
                                 r.get("reply", ""))
    if not sign_url_match:
        return False, f"no sign URL in reply: {r.get('reply','')[:120]}"
    url = sign_url_match.group(0)
    body = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
    if "Type your full name" not in body:
        return False, "sign form didn't render"
    data = urllib.parse.urlencode({"typed_name": "Auto Tester"}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    body = urllib.request.urlopen(req, timeout=5).read().decode("utf-8")
    if "Signed" not in body:
        return False, "sign POST didn't return success page"
    return True, "in-person CO sign flow worked end-to-end"


_public_route_test("pub1", "Public routes",
                    "Portal GET renders project state",
                    _test_portal_renders)

_public_route_test("pub2", "Public routes",
                    "Portal change-request form GET + POST",
                    _test_portal_change_request)

_public_route_test("pub3", "Public routes",
                    "Review form GET + POST",
                    _test_review_flow)

_public_route_test("pub4", "Public routes",
                    "On-site CO sign form GET + POST",
                    _test_co_sign_flow)


# ─── Main runner ──────────────────────────────────────────────────────────

def main():
    _print(f"{C_BOLD}Contractor Module Test Sweep{C_END}")
    _print(f"  host: {HOST}")
    _print(f"  data: {DATA_DIR}")
    _print("")

    if not login_owner():
        return 1
    _print(f"  {C_GREEN}✓ logged in{C_END}")
    _print("")

    results = []
    cur_cat = None
    for case in CASES:
        if case["category"] != cur_cat:
            cur_cat = case["category"]
            _print(f"{C_BOLD}{cur_cat}{C_END}")
        q = case["question"]
        _print(f"  [{case['id']:>5}] {q[:65]}")
        try:
            if case["kind"] == "owner":
                resp = owner_chat(q)
            elif case["kind"] == "public_route":
                resp = {}    # the scorer does its own HTTP
            else:
                resp = {}
            status, reason = case["scorer"](resp)
        except Exception as e:
            status, reason = "fail", f"exception: {e}"
        _print(f"          {STATUS_TAG[status]} {reason}")
        results.append({"id": case["id"], "category": case["category"],
                        "question": q, "status": status, "reason": reason})

    counts = {"pass": 0, "partial": 0, "fail": 0, "manual": 0}
    for r in results:
        counts[r["status"]] += 1
    total = sum(counts.values())
    _print("")
    _print(f"{C_BOLD}SUMMARY ({total} tests){C_END}")
    _print(f"  {C_GREEN}✓ pass   : {counts['pass']:>2}{C_END}")
    _print(f"  {C_YELLOW}~ partial: {counts['partial']:>2}{C_END}")
    _print(f"  {C_RED}✗ fail   : {counts['fail']:>2}{C_END}")
    _print(f"  {C_BLUE}? manual : {counts['manual']:>2}{C_END}")
    _print("")

    fails = [r for r in results if r["status"] == "fail"]
    if fails:
        _print(f"{C_BOLD}{C_RED}FAILURES:{C_END}")
        for f in fails:
            _print(f"  [{f['id']}] {f['question'][:60]}")
            _print(f"        → {f['reason']}")
        _print("")

    # JSON report alongside the base sweep's
    out_dir = DATA_DIR / "test_sweeps"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"contractor_sweep_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"host": HOST, "ran_at": datetime.now().isoformat(),
                                 "counts": counts, "results": results},
                                indent=2, default=str), encoding="utf-8")
    _print(f"Report → {out}")

    return 0 if (counts["fail"] == 0 and counts["manual"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
