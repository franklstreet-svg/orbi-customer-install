"""
dispatch_example — skeleton recipe for a generic dispatch system.

This is a TEMPLATE Frank should clone + customize for the actual
dispatch site he operates. The structure shows the four common steps
a dispatch automation needs:

    1. Land on the dispatch dashboard (auth handled by saved session).
    2. Find the next pending order/job.
    3. Assign it to a specific driver/crew.
    4. Confirm submission — gated behind on_confirm.

To turn this into a real recipe:
    • Replace the placeholder URLs and selectors with the real site's.
    • Update the parse_pending_jobs JS to match the real DOM structure.
    • Test with on_confirm always-False to confirm Orbi STOPS before
      doing anything irreversible.
"""

from __future__ import annotations

import logging
from . import register

log = logging.getLogger("orbi.web_agent.recipes.dispatch_example")

# Replace with the customer's actual dispatch URL.
DISPATCH_BASE_URL = "https://dispatch.example.com/"


def run(page, *, goal: str, params: dict, on_confirm, workspace_dir) -> dict:
    """Run a dispatch assignment.

    params:
        driver_id:  required — which driver to assign
        order_id:   optional — specific order; otherwise picks the next
                    pending one
    """
    driver_id = params.get("driver_id")
    if not driver_id:
        return {"ok": False, "result": "driver_id required", "downloaded": []}

    # 1. Navigate. Session cookies, if previously saved, get us straight
    #    into the dashboard. Otherwise the brain-driven path takes over
    #    (caller can pre-handle login by running the open-ended agent
    #    with goal="log into dispatch.example.com").
    page.goto(DISPATCH_BASE_URL, wait_until="domcontentloaded",
              timeout=20_000)

    # If the login form is what we landed on, bail with BLOCKED so the
    # caller can run the login flow separately. We don't auto-fill
    # credentials in a recipe — that's a CONSEQUENTIAL action gated
    # behind a more cautious flow.
    if page.locator("input[type='password']").count() > 0:
        return {"ok": False,
                "result": "BLOCKED: not signed in — run sign-in flow first",
                "downloaded": []}

    # 2. Find the next pending job. Real recipes replace this selector
    #    with what the dispatch site actually uses.
    js_pending = """
    () => {
      const rows = document.querySelectorAll(
        '[data-status="pending"], tr.pending, .order-card.pending');
      const out = [];
      rows.forEach((el, i) => {
        if (i >= 10) return;
        out.push({
          id:      el.getAttribute('data-order-id') || `row-${i}`,
          summary: (el.innerText || '').trim().slice(0, 200),
        });
      });
      return out;
    }
    """
    try:
        pending = page.evaluate(js_pending) or []
    except Exception as e:
        return {"ok": False,
                "result": f"could not list pending jobs: {e}",
                "downloaded": []}
    if not pending:
        return {"ok": True,
                "result": {"message": "no pending jobs"},
                "downloaded": []}

    target = pending[0]
    target_id = params.get("order_id") or target["id"]

    # 3. Build the confirmation summary the owner will approve.
    proposed = {
        "type":    "submit",
        "summary": f"Assign driver {driver_id} to order {target_id}",
        "reason":  f"order looks like: {target['summary'][:120]}",
        "confirm": True,
    }
    if not on_confirm(proposed):
        return {"ok": False,
                "result": "BLOCKED: owner declined assignment",
                "downloaded": []}

    # 4. Execute the assignment. Replace these selectors with the real
    #    ones from the dispatch site. The pattern (click row → choose
    #    driver from dropdown → click Assign) is common but every site
    #    differs.
    try:
        page.click(f'[data-order-id="{target_id}"]', timeout=8_000)
        page.select_option('select[name="driver"]', value=str(driver_id),
                            timeout=8_000)
        page.click('button:has-text("Assign")', timeout=8_000)
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception as e:
        return {"ok": False,
                "result": f"assignment failed at execution step: {e}",
                "downloaded": []}

    return {
        "ok": True,
        "result": {"order_id": target_id, "driver_id": driver_id,
                    "url": page.url},
        "downloaded": [],
    }


register({
    "name": "dispatch_example",
    "site": "dispatch.example.com",
    "run":  run,
})
