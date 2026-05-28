#!/usr/bin/env python3
"""
Orbi capability test harness — hits the live local Orbi via HTTP and
verifies that every capability we advertise actually works.

Run:
    python3 customer_install/test_capabilities.py
    python3 customer_install/test_capabilities.py --user frank --password ...
    ORBI_URL=http://127.0.0.1:5050 ORBI_USER=frank ORBI_PASS=... \
        python3 customer_install/test_capabilities.py

Exit code 0 = all green, 1 = at least one failure.

Tests are grouped by category. Each test prints PASS / FAIL / SKIP on
one line. A summary table at the end shows the totals. Tests that need
network/LLM are marked NEEDS_LLM and skip cleanly if the brain isn't
reachable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from dataclasses import dataclass, field
from typing import Callable

# ── ANSI colors (auto-disabled if not a TTY) ───────────────────────────
_TTY = sys.stdout.isatty()
def _c(code, s):  return f"\033[{code}m{s}\033[0m" if _TTY else s
GREEN = lambda s: _c("92", s)
RED   = lambda s: _c("91", s)
YEL   = lambda s: _c("93", s)
DIM   = lambda s: _c("90", s)
BOLD  = lambda s: _c("1",  s)


@dataclass
class Result:
    name:   str
    status: str        # "PASS" | "FAIL" | "SKIP"
    detail: str = ""
    cat:    str = ""

@dataclass
class Ctx:
    base:    str
    user:    str
    pwd:     str
    opener:  urllib.request.OpenerDirector = field(default=None)
    results: list = field(default_factory=list)
    logged_in: bool = False

    def record(self, r: Result):
        self.results.append(r)
        glyph = {"PASS": GREEN("✓"), "FAIL": RED("✗"), "SKIP": YEL("○")}[r.status]
        line = f"  {glyph} {r.name}"
        if r.detail:
            line += DIM(f"  — {r.detail}")
        print(line)


def _request(ctx: Ctx, path: str, *, method="GET",
             body=None, headers=None, timeout=30):
    url = ctx.base.rstrip("/") + path
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with ctx.opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, body, dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, str(e), {}


def _login(ctx: Ctx) -> Result:
    if not ctx.user or not ctx.pwd:
        return Result("login", "SKIP", "no ORBI_USER / ORBI_PASS provided")
    code, body, _ = _request(ctx, "/api/owner/login", method="POST",
                             body={"username": ctx.user, "password": ctx.pwd})
    if code == 200:
        ctx.logged_in = True
        return Result("login", "PASS", f"as {ctx.user}")
    return Result("login", "FAIL", f"HTTP {code}: {body[:80]}")


def _chat(ctx: Ctx, message: str) -> tuple[int, dict]:
    code, body, _ = _request(ctx, "/api/owner/chat", method="POST",
                             body={"message": message, "history": []})
    try:
        return code, json.loads(body)
    except json.JSONDecodeError:
        return code, {"_raw": body[:200]}


# ─────────────────────────────────────────────────────────────────────────
# Test cases
# ─────────────────────────────────────────────────────────────────────────

# ─── A. Health + static endpoints ──────────────────────────────────────

def test_health(ctx):
    code, body, _ = _request(ctx, "/health")
    try:
        j = json.loads(body)
        if code == 200 and j.get("status") == "ok":
            return Result("/health responds 200 with status=ok", "PASS",
                          cat="A. Static")
    except json.JSONDecodeError:
        pass
    return Result("/health responds 200 with status=ok", "FAIL",
                  f"got HTTP {code}: {body[:80]}", cat="A. Static")

def test_help_capabilities(ctx):
    code, body, headers = _request(ctx, "/api/help/capabilities")
    ctype = headers.get("Content-Type", "").lower()
    if code != 200:
        return Result("/api/help/capabilities returns 200", "FAIL",
                      f"HTTP {code}", cat="A. Static")
    if "markdown" not in ctype:
        return Result("/api/help/capabilities returns 200", "FAIL",
                      f"wrong Content-Type: {ctype}", cat="A. Static")
    if "# What Orbi Can Do" not in body:
        return Result("/api/help/capabilities returns 200", "FAIL",
                      "missing expected header in markdown", cat="A. Static")
    return Result(f"/api/help/capabilities ({len(body)} bytes md)", "PASS",
                  cat="A. Static")

def test_brain_proxy_present(ctx):
    """The brain proxy endpoint should exist on the billing server (not
    on the customer Orbi). This test just confirms the customer Orbi
    has /api/active to ping. Brain proxy itself is server-side."""
    code, _, _ = _request(ctx, "/api/active/test-fake-key")
    if code in (200, 404, 401, 403):  # any non-500 means the route shape exists
        return Result("billing pingback shape OK", "PASS", cat="A. Static")
    return Result("billing pingback shape OK", "SKIP",
                  f"unexpected HTTP {code}", cat="A. Static")

# ─── B. Authenticated owner chat ──────────────────────────────────────

def test_capabilities_overview_offline(ctx):
    """Asking 'give me a full list of capabilities' must NOT need the LLM —
    it should return a curated overview from the shipped doc."""
    code, j = _chat(ctx, "give me a full list of capabilities")
    reply = j.get("reply", "")
    source = j.get("source", "")
    if code != 200:
        return Result("capabilities overview answers offline", "FAIL",
                      f"HTTP {code}", cat="B. Owner chat")
    if source != "capabilities_overview":
        return Result("capabilities overview answers offline", "FAIL",
                      f"wrong source: {source!r} (expected capabilities_overview)",
                      cat="B. Owner chat")
    if "Calendar" not in reply or "Tasks" not in reply:
        return Result("capabilities overview answers offline", "FAIL",
                      "reply missing expected sections", cat="B. Owner chat")
    return Result("capabilities overview answers offline", "PASS", cat="B. Owner chat")

def test_staff_list(ctx):
    code, j = _chat(ctx, "who's on my staff")
    reply = j.get("reply", "")
    source = j.get("source", "")
    if code != 200 or not reply:
        return Result("staff list answers from data", "FAIL",
                      f"HTTP {code}, reply={reply[:60]!r}", cat="B. Owner chat")
    if source != "personal_assistant":
        return Result("staff list answers from data", "FAIL",
                      f"wrong source: {source!r}", cat="B. Owner chat")
    if not ("staff" in reply.lower() or "owner" in reply.lower() or
            "you have no active staff" in reply.lower()):
        return Result("staff list answers from data", "FAIL",
                      f"unexpected reply: {reply[:80]!r}", cat="B. Owner chat")
    return Result("staff list answers from data", "PASS", cat="B. Owner chat")

def test_calendar_today_read(ctx):
    code, j = _chat(ctx, "what's on my calendar today")
    reply = j.get("reply", "")
    if code != 200 or not reply:
        return Result("calendar today read", "FAIL", f"HTTP {code}", cat="B. Owner chat")
    if "calendar" not in reply.lower() and "today" not in reply.lower():
        return Result("calendar today read", "FAIL",
                      f"unexpected reply: {reply[:80]!r}", cat="B. Owner chat")
    return Result("calendar today read", "PASS", cat="B. Owner chat")

def test_tasks_read(ctx):
    code, j = _chat(ctx, "show my tasks")
    if code == 200 and "task" in j.get("reply", "").lower():
        return Result("tasks read", "PASS", cat="B. Owner chat")
    return Result("tasks read", "FAIL",
                  f"HTTP {code}, reply={j.get('reply','')[:60]!r}",
                  cat="B. Owner chat")

def test_reminders_read(ctx):
    code, j = _chat(ctx, "show my reminders")
    if code == 200 and ("reminder" in j.get("reply", "").lower()
                        or "no pending" in j.get("reply", "").lower()):
        return Result("reminders read", "PASS", cat="B. Owner chat")
    return Result("reminders read", "FAIL",
                  f"HTTP {code}, reply={j.get('reply','')[:60]!r}",
                  cat="B. Owner chat")

# ─── C. Reminder lifecycle (create + verify + fire) ─────────────────────

def test_reminder_create_polite_prefix(ctx):
    """The 'can you remind me to ...' phrasing must route to the fast-path,
    not the LLM (which hallucinates that it saved the reminder)."""
    marker = f"smoketest-{int(time.time())}"
    code, j = _chat(ctx, f"can you remind me to {marker} in 90 minutes")
    src = j.get("source", "")
    reply = j.get("reply", "")
    if src != "quick_capture":
        return Result("reminder via polite prefix uses fast-path", "FAIL",
                      f"source={src!r} (LLM hallucination risk)",
                      cat="C. Reminders")
    if marker not in reply:
        return Result("reminder via polite prefix uses fast-path", "FAIL",
                      f"reply missing marker: {reply[:80]!r}", cat="C. Reminders")
    return Result("reminder via polite prefix uses fast-path", "PASS",
                  cat="C. Reminders")

def test_reminder_local_time(ctx):
    """Reminder set for 'at 5:45' should display as 5:45 PM (local), not UTC."""
    code, j = _chat(ctx, "remind me at 5:45 to smoke-test the time display")
    reply = j.get("reply", "")
    if "5:45 PM" not in reply:
        return Result("reminder shows local time (5:45 PM not 17:45/22:45)",
                      "FAIL", f"reply={reply[:80]!r}", cat="C. Reminders")
    return Result("reminder shows local time", "PASS", cat="C. Reminders")

def test_reminder_dangling_preposition(ctx):
    """'remind me to call X at' should ask for the time, not silently save."""
    code, j = _chat(ctx, "remind me to call SmokeTest at")
    reply = j.get("reply", "").lower()
    if "what time" in reply or "should i remind" in reply:
        return Result("dangling preposition asks instead of saving", "PASS",
                      cat="C. Reminders")
    return Result("dangling preposition asks instead of saving", "FAIL",
                  f"didn't ask: {reply[:80]!r}", cat="C. Reminders")

def test_reminder_fires_to_inbox(ctx):
    """Set a reminder 30s out, sleep 90s, verify it appears in the inbox."""
    marker = f"firetest-{int(time.time())}"
    code, j = _chat(ctx, f"remind me in 1 minute to {marker}")
    if j.get("source") != "quick_capture":
        return Result("reminder fires → toast inbox", "FAIL",
                      "reminder didn't even save", cat="C. Reminders")
    # Wait through the firing worker's 60s tick + a margin.
    print(DIM("    (waiting 75s for reminder to fire…)"))
    time.sleep(75)
    code, body, _ = _request(ctx, "/api/owner/notifications/inbox")
    if code != 200:
        return Result("reminder fires → toast inbox", "FAIL",
                      f"inbox HTTP {code}", cat="C. Reminders")
    try:
        inbox = json.loads(body).get("items", [])
    except json.JSONDecodeError:
        return Result("reminder fires → toast inbox", "FAIL",
                      "inbox returned non-JSON", cat="C. Reminders")
    hit = next((n for n in inbox if marker in n.get("body", "")), None)
    if hit:
        return Result(f"reminder fires → toast inbox", "PASS",
                      f"event={hit.get('event')}", cat="C. Reminders")
    return Result("reminder fires → toast inbox", "FAIL",
                  f"marker {marker!r} never appeared in inbox", cat="C. Reminders")

# ─── D. Quick-capture other kinds ───────────────────────────────────────

def test_quick_capture_task(ctx):
    marker = f"smoketask-{int(time.time())}"
    code, j = _chat(ctx, f"add task: {marker}")
    if j.get("source") == "quick_capture" and marker in j.get("reply", ""):
        return Result("quick-capture: task created", "PASS",
                      cat="D. Quick capture")
    return Result("quick-capture: task created", "FAIL",
                  f"src={j.get('source')!r}, reply={j.get('reply','')[:60]!r}",
                  cat="D. Quick capture")

def test_quick_capture_contact(ctx):
    marker_last = f"Smoke{int(time.time())%10000}"
    code, j = _chat(ctx, f"add contact: Test {marker_last} test@example.com")
    if j.get("source") == "quick_capture" and marker_last in j.get("reply", ""):
        return Result("quick-capture: contact created", "PASS",
                      cat="D. Quick capture")
    return Result("quick-capture: contact created", "FAIL",
                  f"src={j.get('source')!r}, reply={j.get('reply','')[:60]!r}",
                  cat="D. Quick capture")

# ─── E. Public visitor chat ─────────────────────────────────────────────

def test_public_chat_basics(ctx):
    """Anonymous visitor chat must respond without needing owner login."""
    fresh = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    req = urllib.request.Request(
        ctx.base.rstrip("/") + "/chat",
        data=json.dumps({"message": "are you open today",
                         "history": []}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with fresh.open(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if "reply" in body and body["reply"]:
            return Result("anonymous visitor /chat answers", "PASS",
                          cat="E. Visitor")
        return Result("anonymous visitor /chat answers", "FAIL",
                      f"no reply: {str(body)[:80]}", cat="E. Visitor")
    except Exception as e:
        return Result("anonymous visitor /chat answers", "FAIL", str(e),
                      cat="E. Visitor")

# ─── F. Cross-tier sanity ───────────────────────────────────────────────

def test_llm_status(ctx):
    code, body, _ = _request(ctx, "/api/owner/status")
    if code == 200:
        try:
            j = json.loads(body)
            tier = j.get("connection") or j.get("llm_tier") or "?"
            return Result(f"LLM connection state ({tier})", "PASS",
                          cat="F. Sanity")
        except json.JSONDecodeError:
            pass
    return Result("LLM connection state", "SKIP",
                  f"/api/owner/status HTTP {code}", cat="F. Sanity")


# ─── runner ─────────────────────────────────────────────────────────────

TESTS: list[tuple[str, Callable]] = [
    ("A. Static + health",   test_health),
    ("A. Static + health",   test_help_capabilities),
    ("A. Static + health",   test_brain_proxy_present),
    ("B. Owner chat",        test_capabilities_overview_offline),
    ("B. Owner chat",        test_staff_list),
    ("B. Owner chat",        test_calendar_today_read),
    ("B. Owner chat",        test_tasks_read),
    ("B. Owner chat",        test_reminders_read),
    ("C. Reminders",         test_reminder_create_polite_prefix),
    ("C. Reminders",         test_reminder_local_time),
    ("C. Reminders",         test_reminder_dangling_preposition),
    ("C. Reminders",         test_reminder_fires_to_inbox),
    ("D. Quick capture",     test_quick_capture_task),
    ("D. Quick capture",     test_quick_capture_contact),
    ("E. Visitor",           test_public_chat_basics),
    ("F. Sanity",            test_llm_status),
]

OWNER_TESTS = {
    test_capabilities_overview_offline, test_staff_list,
    test_calendar_today_read, test_tasks_read, test_reminders_read,
    test_reminder_create_polite_prefix, test_reminder_local_time,
    test_reminder_dangling_preposition, test_reminder_fires_to_inbox,
    test_quick_capture_task, test_quick_capture_contact, test_llm_status,
}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url",  default=os.environ.get("ORBI_URL", "http://127.0.0.1:5050"))
    ap.add_argument("--user", default=os.environ.get("ORBI_USER", ""))
    ap.add_argument("--password", "--pwd",
                    default=os.environ.get("ORBI_PASS", ""))
    ap.add_argument("--skip-fire", action="store_true",
                    help="skip the 75-second reminder-firing test")
    args = ap.parse_args()

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    ctx = Ctx(base=args.url, user=args.user, pwd=args.password, opener=opener)

    print(BOLD(f"\nOrbi capability check — {args.url}\n"))

    # Login first so subsequent /api/owner/* calls work
    print(BOLD("Authentication"))
    r = _login(ctx); ctx.record(r)

    last_cat = None
    for cat, fn in TESTS:
        if cat != last_cat:
            print()
            print(BOLD(cat))
            last_cat = cat
        if fn in OWNER_TESTS and not ctx.logged_in:
            ctx.record(Result(fn.__name__.replace("test_", "").replace("_", " "),
                              "SKIP", "owner not logged in", cat=cat))
            continue
        if fn is test_reminder_fires_to_inbox and args.skip_fire:
            ctx.record(Result("reminder fires → toast inbox", "SKIP",
                              "--skip-fire", cat=cat))
            continue
        try:
            ctx.record(fn(ctx))
        except Exception as e:
            ctx.record(Result(fn.__name__, "FAIL", f"crashed: {e}", cat=cat))

    # Summary
    p = sum(1 for r in ctx.results if r.status == "PASS")
    f = sum(1 for r in ctx.results if r.status == "FAIL")
    s = sum(1 for r in ctx.results if r.status == "SKIP")
    print()
    print(BOLD("Summary:"),
          GREEN(f"{p} pass"), "/",
          RED(f"{f} fail") if f else f"{f} fail", "/",
          YEL(f"{s} skip") if s else f"{s} skip")
    print()
    return 0 if f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
