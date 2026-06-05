"""
web_driver — Orby drives restaurant websites the same way a human would.

Instead of integrating with each POS via API (gated, expensive, per-vendor),
Orby acts as the restaurant's authorized agent: opens a real browser,
clicks through their existing online-ordering page, submits the order
through their existing checkout pipe. Order lands in the kitchen via
the same channel a normal customer would use.

Per-restaurant click-path drivers:
  · purblum.py       — Frank's demo deli site
  · (future) square_online.py, toast.py, etc.

Each driver exposes a `submit_order(cart, customer, headless=True)` that
returns {ok, order_id, total, screenshot_path, error}.

Two run modes:
  headless=True   production — runs invisibly inside the phone flow
  headless=False  demo       — visible cursor + click animations, the
                                wow factor when showing a restaurant owner

A single shared helper (base.py) wraps the Playwright lifecycle: launch
browser, take screenshots on error for debugging, clean up reliably.
"""

from .purblum import submit_order as submit_purblum_order  # noqa: F401
from .email_dispatch import submit_order as submit_order_by_email  # noqa: F401
from .print_dispatch import submit_order as submit_order_by_print  # noqa: F401


def dispatch_order(cart, customer, totals, profile):
    """Generic order-submission entry point. Reads the customer profile's
    `order_submission.method` field and routes to the right backend:

      · "print"      → print_dispatch (thermal receipt printer over LAN;
                       same way DoorDash/UberEats/GrubHub deliver to a
                       kitchen — works for any restaurant that has a
                       network thermal printer like Epson TM-T20 or
                       Star TSP100). RECOMMENDED default for new
                       restaurant customers.
      · "email"      → email_dispatch.submit_order (kitchen receives a
                       formatted order email; for restaurants without
                       a thermal printer).
      · "web_driver" → per-restaurant Playwright driver (PurBlum today;
                       same approach can be built for Toast / Square
                       Online / Clover templates later).
      · "print,email,web_driver" or any combination separated by commas →
                       try each method in order, first success wins. Use
                       this for redundancy ("print to the kitchen AND
                       email as a backup in case the printer is offline").
      · (unset)      → web_driver if profile slug matches a built driver,
                       otherwise: print if a printer_ip is set, otherwise
                       email if a kitchen_email is set, else error.

    Returns the same shape every backend returns:
      {ok: bool, order_id: str, method: str, total: float, ...}
    """
    cfg = (profile or {}).get("order_submission") or {}
    method = (cfg.get("method") or "").lower().strip()
    slug   = (profile or {}).get("_slug") or ""

    # Build a fallback chain. If `method` is a comma-separated list, try
    # each in order; otherwise just one method.
    if method:
        methods_to_try = [m.strip() for m in method.split(",") if m.strip()]
    else:
        # Inferred defaults based on what's configured.
        methods_to_try = []
        if slug == "purblum_com":
            methods_to_try.append("web_driver")
        # Any printing config (network IP, named system printer, or
        # "use system default") signals print is available.
        if (cfg.get("printer_ip") or cfg.get("printer_name")
                or cfg.get("use_system_default")):
            methods_to_try.append("print")
        if cfg.get("kitchen_email"):
            methods_to_try.append("email")
        if not methods_to_try:
            return {"ok": False,
                    "error": "no_submission_method_configured",
                    "hint": "Add order_submission to the customer profile. "
                            "Options: printer_name (a printer installed on "
                            "their computer), use_system_default (true), "
                            "printer_ip (a network thermal printer), or "
                            "kitchen_email (any email address)."}

    last_result = None
    for m in methods_to_try:
        if m == "web_driver":
            if slug == "purblum_com":
                last_result = submit_purblum_order(cart, customer)
            else:
                last_result = {"ok": False,
                               "error": f"no_web_driver_for_slug:{slug}",
                               "hint": "Either build a web_driver for "
                                       "this customer or switch to print/"
                                       "email."}
        elif m == "print":
            last_result = submit_order_by_print(cart, customer, totals, profile)
        elif m == "email":
            last_result = submit_order_by_email(cart, customer, totals, profile)
        else:
            last_result = {"ok": False, "error": f"unknown_method:{m}"}

        if last_result.get("ok"):
            return last_result
        # Otherwise try the next method in the chain.

    # All methods exhausted, return the last error.
    return last_result or {"ok": False, "error": "no_methods_attempted"}
