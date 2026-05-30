#!/usr/bin/env python3
"""
seed_demo_contractor — preload a realistic GC's data for a demo / sales call.

Drops in 5 projects in various stages, 7 COs across the spectrum (signed,
pending, out for signature), 8 invoices (paid, sent, overdue), 4 subs
with one whose insurance is expiring soon, daily logs that trigger the
leak alarm, and one un-covered scope mention so "leak check" lights up.

Idempotent reset: pass --reset to wipe existing contractor data first.
Without --reset, just adds. Either way the demo runs the same.

Usage:
    cd customer_install
    python3 tools/seed_demo_contractor.py --reset

After seeding, open a chat session as the owner and try:
  · what jobs are open
  · pending COs
  · who owes me money
  · aging report
  · leak check
  · this week's logs
  · whose insurance is expiring
  · show me the money

Each one should return rich, demo-ready data.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORBI_DIR = HERE.parent
sys.path.insert(0, str(ORBI_DIR))

from modules import projects as mod_projects
from modules import change_orders as mod_co
from modules import invoices as mod_invoices
from modules import daily_logs as mod_logs
from modules import subcontractors as mod_subs

DATA_DIR = Path(os.environ.get("ORBI_DIR", str(ORBI_DIR))) / "data"


def _reset():
    """Wipe contractor-module data files but leave everything else alone."""
    for f in ("projects.json", "change_orders.json", "invoices.json",
              "daily_logs.json", "subcontractors.json", "co_sign_tokens.json"):
        p = DATA_DIR / f
        if p.exists():
            p.unlink()
            print(f"  removed {f}")


def _ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _future(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_ago(days: int) -> int:
    return int(time.time()) - days * 86400


def _date_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="wipe contractor data files before seeding")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.reset:
        print("Resetting contractor data...")
        _reset()

    print("\nSeeding projects...")
    p_oak = mod_projects.add(DATA_DIR,
        address="555 Oak Avenue, Reno NV 89501",
        label="Kitchen Remodel",
        customer_name="Sarah Johnson",
        customer_phone="+17755551234",
        customer_email="sarah.j@example.com",
        contract_amount=48500.00,
        contracted_at=_ago(45),
        started_at=_ago(35),
        est_complete=_future(10),
        foreman="Mike Torres",
        stage="finish carpentry",
        status="active",
        notes="Customer chose quartz upgrade mid-build. Wants slow rollout on tile.")
    print(f"  + Oak project (kitchen remodel, ${p_oak['contract_amount']:,.0f})")

    p_maple = mod_projects.add(DATA_DIR,
        address="123 Maple Street, Reno NV 89502",
        label="Master Bath",
        customer_name="David & Lisa Park",
        customer_phone="+17755555678",
        customer_email="dpark@example.com",
        contract_amount=22000.00,
        contracted_at=_ago(20),
        started_at=_ago(12),
        est_complete=_future(20),
        foreman="Joe Rivera",
        stage="rough plumbing",
        status="active")
    print(f"  + Maple project (master bath, ${p_maple['contract_amount']:,.0f})")

    p_cedar = mod_projects.add(DATA_DIR,
        address="89 Cedar Lane, Sparks NV 89431",
        label="Deck Build",
        customer_name="Tom Anderson",
        customer_phone="+17755559876",
        customer_email="tom.a@example.com",
        contract_amount=14200.00,
        contracted_at=_ago(8),
        started_at=_ago(5),
        est_complete=_future(7),
        foreman="Mike Torres",
        stage="framing",
        status="active")
    print(f"  + Cedar project (deck, ${p_cedar['contract_amount']:,.0f})")

    p_birch = mod_projects.add(DATA_DIR,
        address="247 Birch Court, Reno NV 89509",
        label="Whole-Home Repaint",
        customer_name="Maria Garcia",
        customer_phone="+17755553344",
        customer_email="m.garcia@example.com",
        contract_amount=8800.00,
        contracted_at=_ago(70),
        started_at=_ago(60),
        est_complete=_ago(5),
        foreman="Joe Rivera",
        stage="closeout",
        status="completed")
    # actual_complete isn't an add() kwarg; set via update.
    mod_projects.update(DATA_DIR, p_birch["id"], actual_complete=_ago(3))
    print(f"  + Birch project (repaint, completed)")

    p_pine = mod_projects.add(DATA_DIR,
        address="1010 Pine Ridge, Reno NV 89511",
        label="Garage Conversion",
        customer_name="Kevin Wong",
        customer_phone="+17755557722",
        customer_email="kw@example.com",
        contract_amount=32500.00,
        contracted_at=_ago(3),
        foreman="",
        stage="awaiting permit",
        status="estimate",
        notes="Estimate sent, waiting on signed contract")
    print(f"  + Pine project (garage conversion, in estimate)")

    print("\nSeeding subs...")
    s_bob = mod_subs.add_sub(DATA_DIR,
        name="Bob's Plumbing", contact_name="Bob Davis",
        phone="+17755551111", email="bob@bobsplumbing.com",
        trade="plumbing", license="NV-PL-123456",
        insurance_expires=(date.today() + timedelta(days=120)).isoformat(),
        rate="$95/hr", rating=4,
        notes="Solid on rough-in. Slow on returns.")
    print(f"  + Bob's Plumbing")

    s_acme = mod_subs.add_sub(DATA_DIR,
        name="Acme Electric", contact_name="Tony Castellano",
        phone="+17755552222", email="tony@acme-elec.com",
        trade="electrical", license="NV-EL-789012",
        insurance_expires=(date.today() + timedelta(days=18)).isoformat(),
        rate="$110/hr", rating=5,
        notes="Best electrician we know. Insurance up for renewal soon.")
    print(f"  + Acme Electric (insurance expiring in 18 days — flagged)")

    s_drywall = mod_subs.add_sub(DATA_DIR,
        name="Reno Drywall Pros", contact_name="Sarah Kim",
        phone="+17755553333", email="sarah@renodrywall.com",
        trade="drywall", license="NV-DW-345678",
        insurance_expires=(date.today() + timedelta(days=200)).isoformat(),
        rate="$1.85/sf", rating=4)
    print(f"  + Reno Drywall Pros")

    s_tile = mod_subs.add_sub(DATA_DIR,
        name="Mountain Tile & Stone", contact_name="Carlos Mendez",
        phone="+17755554444", email="carlos@mtntile.com",
        trade="tile", license="NV-TS-901234",
        insurance_expires=(date.today() + timedelta(days=85)).isoformat(),
        rate="$8.50/sf", rating=3,
        notes="Quality is good, scheduling is hit-or-miss.")
    print(f"  + Mountain Tile & Stone")

    print("\nSeeding sub assignments...")
    mod_subs.assign(DATA_DIR, sub_id=s_bob["id"], project_id=p_oak["id"],
                     scope="Kitchen rough + finish plumbing", scheduled="Started Oct 12")
    mod_subs.assign(DATA_DIR, sub_id=s_acme["id"], project_id=p_oak["id"],
                     scope="Kitchen branch circuits, dedicated 20A for island",
                     scheduled="Started Oct 18")
    mod_subs.assign(DATA_DIR, sub_id=s_bob["id"], project_id=p_maple["id"],
                     scope="Master bath rough-in", scheduled="Tuesday")
    mod_subs.assign(DATA_DIR, sub_id=s_drywall["id"], project_id=p_oak["id"],
                     scope="Ceiling + walls level-5 finish")
    print(f"  + 4 assignments")

    print("\nSeeding change orders...")
    co1 = mod_co.add(DATA_DIR, project_id=p_oak["id"],
                      description="Upgrade counters from laminate to quartz",
                      amount=3200.00, status="signed")
    mod_co.update(DATA_DIR, co1["id"],
                   gc_approved_at=_ts_ago(28), gc_approved_by="frank",
                   sent_at=_ts_ago(27), sent_via="email",
                   client_signer="Sarah Johnson", client_signed_at=_ts_ago(26))
    print(f"  + CO #1 Oak: quartz upgrade (signed, $3,200)")

    co2 = mod_co.add(DATA_DIR, project_id=p_oak["id"],
                      description="Add under-cabinet LED lighting throughout",
                      amount=850.00, status="signed")
    mod_co.update(DATA_DIR, co2["id"],
                   gc_approved_at=_ts_ago(15), gc_approved_by="frank",
                   sent_at=_ts_ago(14), client_signer="Sarah Johnson",
                   client_signed_at=_ts_ago(13))
    print(f"  + CO #2 Oak: LED lighting (signed, $850)")

    co3 = mod_co.add(DATA_DIR, project_id=p_oak["id"],
                      description="Replace dishwasher box per code update",
                      amount=425.00, status="awaiting_approval")
    print(f"  + CO #3 Oak: dishwasher box (awaiting your approval, $425)")

    co4 = mod_co.add(DATA_DIR, project_id=p_maple["id"],
                      description="Upgrade shower valve to thermostatic",
                      amount=680.00, status="sent_for_signature")
    mod_co.update(DATA_DIR, co4["id"],
                   gc_approved_at=_ts_ago(2), gc_approved_by="frank",
                   sent_at=_ts_ago(1), sent_via="email")
    print(f"  + CO #4 Maple: thermostatic valve (waiting on client signature, $680)")

    co5 = mod_co.add(DATA_DIR, project_id=p_cedar["id"],
                      description="Add stair railing per code inspection",
                      amount=1100.00, status="awaiting_approval")
    print(f"  + CO #5 Cedar: stair railing (awaiting your approval, $1,100)")

    co6 = mod_co.add(DATA_DIR, project_id=p_birch["id"],
                      description="Customer-requested accent wall (Birch)",
                      amount=425.00, status="awaiting_approval")
    mod_co.mark_rejected(DATA_DIR, co6["id"],
                          reason="Client decided against on final walkthrough")
    print(f"  + CO #6 Birch: accent wall (rejected)")

    print("\nSeeding invoices...")
    # Oak — paid first draw, sent second draw
    inv1 = mod_invoices.add(DATA_DIR, project_id=p_oak["id"],
                              line_items=[{"label": "Progress draw #1 — demolition + rough", "amount": 14000}],
                              retainage_pct=10.0,
                              memo="Draw 1 — demolition + rough framing complete",
                              due_at=_ts_ago(20),
                              is_draw=True, status="approved")
    mod_invoices.mark_sent(DATA_DIR, inv1["id"])
    mod_invoices.record_payment(DATA_DIR, inv1["id"], 12600)  # 14000 - 1400 retainage
    print(f"  + {inv1['invoice_number']} Oak draw #1 (PAID, $14,000)")

    inv2 = mod_invoices.add(DATA_DIR, project_id=p_oak["id"],
                              line_items=[{"label": "Progress draw #2 — cabinets + counters", "amount": 18000}],
                              retainage_pct=10.0,
                              memo="Draw 2 — cabinet install + quartz tops",
                              due_at=_ts_ago(8),  # overdue
                              is_draw=True, status="approved")
    mod_invoices.mark_sent(DATA_DIR, inv2["id"])
    print(f"  + {inv2['invoice_number']} Oak draw #2 (SENT, OVERDUE by 8 days)")

    # Maple — sent, not yet due
    inv3 = mod_invoices.add(DATA_DIR, project_id=p_maple["id"],
                              line_items=[{"label": "Progress draw #1 — demo + framing", "amount": 6000}],
                              retainage_pct=10.0,
                              memo="Draw 1 — demo + framing",
                              due_at=_ts_ago(-15),  # due in 15 days
                              is_draw=True, status="approved")
    mod_invoices.mark_sent(DATA_DIR, inv3["id"])
    print(f"  + {inv3['invoice_number']} Maple draw #1 (SENT, due in 15 days)")

    # Cedar — small materials advance, partially paid
    inv4 = mod_invoices.add(DATA_DIR, project_id=p_cedar["id"],
                              line_items=[{"label": "Materials deposit (lumber + hardware)", "amount": 3500}],
                              memo="Materials deposit",
                              due_at=_ts_ago(-3),
                              status="approved")
    mod_invoices.mark_sent(DATA_DIR, inv4["id"])
    mod_invoices.record_payment(DATA_DIR, inv4["id"], 1750)  # half
    print(f"  + {inv4['invoice_number']} Cedar materials deposit (PARTIAL — half paid)")

    # Birch — final invoice, paid
    inv5 = mod_invoices.add(DATA_DIR, project_id=p_birch["id"],
                              line_items=[{"label": "Final — labor + materials", "amount": 8800}],
                              memo="Final invoice — project closeout",
                              due_at=_ts_ago(15),
                              status="approved")
    mod_invoices.mark_sent(DATA_DIR, inv5["id"])
    mod_invoices.record_payment(DATA_DIR, inv5["id"], 8800)
    print(f"  + {inv5['invoice_number']} Birch final (PAID)")

    # Another overdue one for aging-report drama
    inv6 = mod_invoices.add(DATA_DIR, project_id=p_oak["id"],
                              line_items=[{"label": "Quartz upgrade CO", "amount": 3200}],
                              memo="CO #1 — Quartz upgrade",
                              due_at=_ts_ago(45),  # really old
                              status="approved")
    mod_invoices.mark_sent(DATA_DIR, inv6["id"])
    print(f"  + {inv6['invoice_number']} Oak quartz CO (OVERDUE 45+ days)")

    print("\nSeeding daily logs...")
    log1 = mod_logs.add(DATA_DIR, project_id=p_oak["id"],
                         date_iso=_date_ago(1),
                         crew=["Mike Torres", "Diego Reyes"],
                         work_done="Hung upper cabinets on north and east walls. Started lower run. Quartz template guy came for measurements.",
                         hours=8.0, weather="Sunny, 78°F",
                         deliveries=["Cabinets - 14 units from KraftMaid"],
                         logged_by="frank")
    print(f"  + Oak log yesterday")

    log2 = mod_logs.add(DATA_DIR, project_id=p_oak["id"],
                         date_iso=_date_ago(2),
                         crew=["Mike Torres", "Diego Reyes"],
                         work_done="Demoed remaining laminate sections. Client wanted additional baseboard replacement throughout the kitchen — we'll need a CO for that.",
                         hours=7.5,
                         logged_by="frank")
    print(f"  + Oak log 2 days ago (flagged scope addition for leak alarm)")

    log3 = mod_logs.add(DATA_DIR, project_id=p_maple["id"],
                         date_iso=_date_ago(1),
                         crew=["Joe Rivera", "Pedro Sandoval"],
                         work_done="Rough plumbing inspection passed. Started drywall prep. Client requested heated floor add — talk to them about scope.",
                         hours=8.5,
                         logged_by="frank")
    print(f"  + Maple log yesterday (flagged scope addition for leak alarm)")

    log4 = mod_logs.add(DATA_DIR, project_id=p_cedar["id"],
                         date_iso=_date_ago(0),
                         crew=["Mike Torres"],
                         work_done="Set deck posts on footings. Frost depth verified. Joists tomorrow.",
                         hours=5.0,
                         logged_by="frank")
    print(f"  + Cedar log today")

    print("\n" + "=" * 60)
    print("Demo seed complete. Try these in chat:")
    print("  · what jobs are open")
    print("  · pending COs")
    print("  · who owes me money")
    print("  · aging report")
    print("  · leak check        ← should flag baseboard + heated floor")
    print("  · this week's logs")
    print("  · whose insurance is expiring")
    print("  · show me the money")
    print("=" * 60)


if __name__ == "__main__":
    main()
