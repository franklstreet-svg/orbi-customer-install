# Orbi — Roles & Permissions

**Status:** Blueprint v1. NOT YET ENFORCED in code.
**Last updated:** 2026-06-19
**Owner:** Frank Street
**Implementation tracker:** `permits.py` (to be built) is the chokepoint.

This document is the contract. Every data-fetch endpoint in Orbi
must consult `permits.check(actor_role, data_type, action)` before
returning anything. Roles are assigned per seat at install/setup time.

---

## The seat model

- **A SEAT = one logged-in person on one machine, with one role.**
- A multi-seat business buys N seats. Each seat installs the same Orbi
  binary, points at the company's hub (designated PC or cloud box),
  and authenticates with a seat-specific credential. The role lives on
  the hub, not on the seat — a tampered seat client can't elevate
  itself.
- The hub is the source of truth. Seats hold only personal scratch
  data (their own draft messages, their own search history). Shared
  business data is always fetched from the hub on demand.

## Roles

Six baseline roles. A given business may use a subset.

| Role | Description |
|---|---|
| **owner** | The customer who paid. Sees everything; grants/revokes other roles. |
| **manager** | Trusted lieutenants. Most read, most write, can't see other managers' performance-review-in-progress drafts. |
| **sales** | Customer-facing revenue role. Sees their own pipeline + customer records; cannot see financials or HR. |
| **accountant** | Books, taxes, payroll structure (not individual employee notes). |
| **hr** | People records, time off, performance, hiring pipeline. Cannot see customer financial detail. |
| **receptionist** | Phone-receptionist seat for the front desk. Sees what's needed to greet/route, nothing more. |

Custom roles ("warehouse", "field_tech") can be created by the owner
from the dashboard by cloning + editing a baseline role.

## The matrix

`R` = read, `W` = read + write, `—` = none, `R*`/`W*` = scoped subset (see Footnotes).

| Data type | owner | manager | sales | accountant | hr | receptionist |
|---|---|---|---|---|---|---|
| Customer records (`contacts`, `customer_thread`) | W | W | W (own assigned) | R (no notes) | — | R (basic name+phone) |
| Invoices / billing (`invoices`, `invoice_pdf`, brain billing.db) | W | R | R (own customers) | W | — | — |
| HR / payroll / people records (`users`, future `hr.py`) | W | R structure only | — | R structure only | W | — |
| Calendar (`calendar`, `gcal`) — own | W | W | W | W | W | W |
| Calendar — others' | W | R (free/busy only) | R own team free/busy | R | R | R business-hours only |
| Internal messages (`internal_messages`) | W | W | W (team channels) | W (finance channel) | W (hr channel) | W (front desk channel) |
| Vault docs (`~/Orbi/` workspace, `workspace.py`) | W | W | R sales-tagged | R finance-tagged | R hr-tagged | — |
| Phone receptionist (`voice`, `voicemail`, `caller_history`) | W | R | R own callbacks | — | — | W |
| Marketing assets (`marketing`, `ad_gen`, `image_gen`) | W | W | R | — | — | — |
| Memory — owner's personal (`memory` 4-tier) | W | — | — | — | — | — |
| Memory — business shared (learning loop answers) | W | W | R | R | R | R |
| Web Tasks (`web_agent`) | W | W | W (within role's data scope) | W (within role's data scope) | W (within role's data scope) | R (read-only) |
| Audit log (`audit`) | R | — | — | — | — | — |
| Permissions admin (this matrix) | W | — | — | — | — | — |

### Footnotes

- **"own assigned":** scoped to records where the row's `assigned_to`
  field matches the requesting user's username.
- **"sales-tagged" / "finance-tagged" / "hr-tagged":** docs in the
  vault carry tags set by the uploader. Tags are NOT secrets — they're
  routing labels. A doc with no tag defaults to owner-only.
- **"structure only" (payroll/HR):** the role can see the SHAPE of the
  data (positions, departments, total headcount) but not individual
  employee compensation, personal addresses, or PII.
- **"basic name+phone" (receptionist customer records):** enough to
  greet a returning caller by name and route them. No order history,
  no payment info, no preferences.
- **Web Tasks within role's scope:** a sales seat can run web_agent to
  pull data from their pipeline tools, but the recipe + result filter
  through the same matrix on the way back out.

## Default role for a new seat

`receptionist`. Least-privilege. Owner explicitly upgrades each new
person from the default during onboarding. This means an accidentally-
provisioned seat (mis-typed email, etc.) can't see anything sensitive.

## Override mechanics

The owner can grant a one-off, time-bounded exception via the
dashboard ("let Maria see the Q3 financials this week" → adds an
override entry valid for 7 days, auto-revokes). Overrides are logged
to the audit trail and surface as a banner on the recipient's seat
("you have temporary access until 2026-09-30, granted by Frank").

## Hard rules (no exceptions, even owner can't disable)

1. **Server-side enforcement only.** The seat client never decides
   what it shows. The hub refuses to send data the seat's role isn't
   allowed to see. If a hostile seat asks for HR data while logged in
   as sales, the hub returns 403, not "filtered results."
2. **Filter at retrieval, not at prompt.** When a non-owner role's
   Orbi reaches the LLM, the system prompt is built from the data
   their role is allowed to see. No "be careful not to mention X" —
   X never enters the prompt in the first place.
3. **Audit every check.** Every permit check writes
   `data/permits_audit.jsonl` on the hub. Every denial is logged. The
   owner can scrub the log monthly for "who tried to see what."
4. **No role can see the audit log except owner.** Otherwise an
   attacker who lands on the audit log can see what others searched
   for and use it to time their probes.
5. **Hub is the only thing that holds the matrix.** Seats download
   their own role at session-start (signed by hub's HMAC). They never
   make permission decisions locally.

## Test plan — negative tests that must pass before v1 ships

For each role pair (R_attacker, R_target), build a fixture user, log
in, and attempt to read EVERY data type R_target has but R_attacker
shouldn't. Every attempt must return 403, and the attempt must appear
in the audit log.

| # | Test | Expected |
|---|---|---|
| 1 | Sales tries `/api/owner/invoices/list` for OTHER salesperson's customer | 403 + audit entry |
| 2 | Sales tries `/api/owner/users/list?role=hr` | 403 |
| 3 | Sales tries `/api/owner/business_info/financial_summary` | 403 |
| 4 | Accountant tries `/api/owner/contacts/<id>/notes` | 403 |
| 5 | Accountant tries `/api/owner/voicemails/list` | 403 |
| 6 | HR tries `/api/owner/customer_thread/<id>` | 403 |
| 7 | HR tries `/api/owner/invoices/list` | 403 |
| 8 | Receptionist tries `/api/owner/contacts/<id>/order_history` | 403 |
| 9 | Receptionist tries `/api/owner/marketing/campaigns` | 403 |
| 10 | Sales prompts the chat: "what's Maria's salary?" — confirm Maria's salary is NOT in the system prompt that goes to the brain | salary string absent from prompt payload (log dump) |
| 11 | Accountant prompts the chat: "tell me about Joe Smith" (a customer) — confirm customer NOTES are NOT in the prompt | notes absent from prompt |
| 12 | Sales attempts override-token replay (tries to use a granted-to-someone-else override token) | 403 |
| 13 | Any role tries `/api/owner/permits/audit/list` (except owner) | 403 |
| 14 | Any role tries to call `permits.set_role(self, "owner")` via dashboard | 403 + critical-event audit entry |

Tests 10 and 11 are the most important — they're what separates
"looks permissioned" from "actually permissioned."

## Test plan — how to run them without 10 computers

1. **Single-laptop, multi-session:** Create 10 fixture users in
   `users.py`. Open 10 Chrome incognito windows. Each window logs in
   as a different role. Run the negative tests by hand first to
   confirm the wiring; then automate as a pytest suite under
   `tests/permits/`.
2. **Docker, 10 containers:** Same matrix run against containers
   hitting the hub container over Docker network. Catches the
   "works on localhost but breaks across machines" bugs.
3. **One DO box + 2-3 real machines:** Promote the matrix from
   localhost-only to real-network with a TLS cert. At least one
   negative test must pass over a real network before declaring v1
   shippable.

## Out of scope for v1 (note + park)

- Field-level encryption per role (encrypt rows so even DB-level
  access can't read them without the role's key). Heavy. Park for v2.
- Cross-org permissions (sharing a customer record between two
  separate Orbi businesses). Park.
- Time-bounded scheduled access ("Sales sees commission report only
  on the 1st of each month"). Park.

## What ships first

The matrix above + `permits.py` (deny-by-default chokepoint) +
the 14 negative tests. Nothing else changes about Orbi until those
ship green. After that we can split work cleanly: hub install mode,
seat install mode, dashboard role-manager, override granting, real-
network test. None of those is hard once `permits.py` is the law.
