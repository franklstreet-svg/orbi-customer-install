"""
web_agent — Orbi's browser-control module.

The horizontal infrastructure piece that lets Orbi operate any web app the
customer already uses: dispatch systems, supplier portals, restaurant
ordering platforms, permit sites, legal filing portals. Build once,
work everywhere the customer's data lives behind a login.

Architecture
------------
Five layers, mirrored from the agent loop pattern:

    eyes    page_observer.py  — read the current page (accessibility tree,
                                fallback to DOM scrape, fallback to visual)
    brain   controller.py     — given goal + observation, pick the next
                                action via Orbi's brain (HF Qwen)
    hands   actions.py        — execute structured commands on a real Chrome
                                via Playwright (click, type, select, etc.)
    state   session.py        — per-site cookie/auth persistence
    safety  guards.py         — confirmation gating for consequential
                                actions, error recovery, safe-stop

Hybrid execution
----------------
Known sites get RECIPES — deterministic Playwright scripts that execute
the predictable 80% with near-100% reliability. The decision-loop LLM
only fires on the unpredictable 20% (page changed, unfamiliar layout,
multi-step reasoning required). This matches how production browser
agents stay reliable.

Public API
----------
    run_task(goal, *, site=None, recipe=None, user_dir, on_confirm=None)
        High-level entry point. If a recipe is provided OR auto-detected
        for `site`, runs the recipe. Otherwise runs the open-ended
        agent loop with the brain.

    list_recipes() -> list[str]
        Returns names of all registered recipes (google_search, etc.).

Storage
-------
    Downloads land in ~/Orbi/ (the customer-friendly workspace folder).
    Cookies + session state live in DATA_DIR/web_agent_sessions/<site>.json,
    encrypted with the install's session secret.

Local-only fit
--------------
    Per the customer-data-local rule: cookies are LOCAL (live in DATA_DIR
    on the customer's machine, never centralized). Decision-loop LLM
    calls go through Orbi's brain (HF Qwen) — that's processing, not
    data, so it's fine.
"""

from __future__ import annotations

__all__ = [
    "run_task",
    "list_recipes",
    "AgentResult",
]

# Defer heavy imports (Playwright, brain client) until first use so just
# importing this package doesn't pay the Chromium-launch cost.
def run_task(*args, **kwargs):  # type: ignore
    from .controller import run_task as _run
    return _run(*args, **kwargs)


def list_recipes():
    from .recipes import REGISTRY
    return sorted(REGISTRY.keys())


# Re-exported for type hints in callers.
class AgentResult(dict):
    """{
        "ok": bool,
        "goal": str,
        "actions_taken": list[dict],
        "final_observation": str,
        "downloaded_files": list[str],
        "elapsed_seconds": float,
        "stopped_reason": str,  # "goal_met" | "max_steps" | "blocked" | ...
        "screenshots": list[str],  # paths under DATA_DIR/web_agent_screenshots/
        "error": str | None,
    }"""
    pass
