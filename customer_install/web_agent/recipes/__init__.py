"""
recipes — known-site Playwright scripts that handle the predictable 80%
deterministically.

For sites the customer uses every day (a dispatch system, a supplier
portal), the workflow is the same every time: log in, click this menu,
type this value, hit submit. Scripting it as a Playwright function makes
it fast (no per-step LLM latency) and reliable (no LLM hallucination).

Only the unpredictable 20% — page changed, unexpected popup, new layout —
hands control back to the brain-driven loop.

Recipe contract
---------------
A recipe is a callable:

    def run(page, *, goal, params, on_confirm) -> dict:
        ...
        return {"ok": bool, "result": ..., "downloaded": [...], "needs_followup": bool}

`params` is whatever the caller passed in (driver_id, customer_phone, etc).
`on_confirm(action_summary) -> bool` is an injected gate the recipe MUST
call before any consequential action (assign driver, submit payment, etc.).
Returning False from on_confirm means the customer declined — the recipe
should bail cleanly.

Registration
------------
Each recipe module exposes RECIPE = {"name": ..., "site": ..., "run": ...}
and the package __init__ collects them into REGISTRY at import time.
"""

from __future__ import annotations

from typing import Any, Callable

# Recipe registry: site_key → recipe dict
# Populated lazily — each file under recipes/ that defines RECIPE gets
# imported by import_all().
REGISTRY: dict[str, dict[str, Any]] = {}


def register(recipe: dict) -> None:
    """Add a recipe to the registry. Last-write-wins so a customer
    override file can replace the bundled default."""
    name = recipe.get("name")
    site = recipe.get("site")
    run  = recipe.get("run")
    if not name or not site or not callable(run):
        raise ValueError(f"recipe must have name, site, and callable run: {recipe!r}")
    REGISTRY[name] = recipe


def get(name: str) -> dict | None:
    return REGISTRY.get(name)


def import_all() -> None:
    """Import every recipe module under this package so they self-register.
    Called lazily on first run_task() so import overhead is paid once."""
    import importlib
    import pkgutil
    for _, modname, _ in pkgutil.iter_modules(__path__):
        if modname.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{modname}")
        except Exception as e:
            import logging
            logging.getLogger("orbi.web_agent.recipes").warning(
                "recipe %s failed to import: %s", modname, e)
