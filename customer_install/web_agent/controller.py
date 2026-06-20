"""
controller — the brain-driven loop that powers open-ended browser tasks.

Public entry point: run_task(goal, *, ...).

The loop:
    1. Open a Playwright Chrome with the saved session for the target site
       (if we have one). Otherwise a fresh browser context.
    2. observe() the page → build a compact accessibility-tree snapshot.
    3. Ask Orbi's brain: "Given this goal and this observation, what's
       the next action?" The brain returns one JSON action.
    4. needs_confirmation(action) → if yes, gate behind the customer's
       on_confirm callback. If they say no, stop with reason="declined".
    5. execute(action) → run it on the page.
    6. If the action was "finish" OR step count hits the cap, stop.
    7. Otherwise observe() again and repeat.

Recipes short-circuit step 2-6: when a recipe matches the target site,
we call the recipe's run() function directly. It can call back into
the brain loop for sub-tasks but typically just scripts the predictable
flow with deterministic Playwright calls.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from . import actions as _actions
from . import learner as _learner
from . import page_observer as _observer
from . import session as _session

log = logging.getLogger("orbi.web_agent.controller")

# Defaults — overridable per call. 25 steps covers virtually every
# real-world dispatch task (login → navigate → fill → submit → confirm).
DEFAULT_MAX_STEPS = 25
DEFAULT_PER_ACTION_TIMEOUT_MS = 15_000

_BRAIN_SYSTEM_PROMPT = """\
You are Orbi's browser agent. You drive a real Chrome browser to accomplish
goals the owner gives you. You see the current page as a list of interactive
elements (each with a `ref` id, role, name, value). You emit ONE next action
as a JSON object. No prose, no markdown — just the JSON.

Available action types and their fields (omit fields that don't apply):

  navigate    {type:"navigate", url:"https://...", reason:"..."}
  click       {type:"click", ref:"e12", reason:"..."}
  type        {type:"type", ref:"e3", text:"...", reason:"..."}
  select      {type:"select", ref:"e7", value:"...", reason:"..."}
  wait_for    {type:"wait_for", condition:"visible"|"hidden"|"network_idle"|"url_contains:<frag>",
               ref:"e9", reason:"..."}
  read        {type:"read", ref:"e4", reason:"why we need this text"}
  screenshot  {type:"screenshot", reason:"why a picture would help"}
  scroll      {type:"scroll", scroll:"page_down"|"page_up"|"to_selector", ref:"e9", reason:"..."}
  submit      {type:"submit", ref:"e2", reason:"...", confirm:true}
  finish      {type:"finish", reason:"goal accomplished — summary of what we did"}

Rules:
- ALWAYS include `reason` — one short sentence explaining why this action.
- `ref` values come from the observation's `elements` list. NEVER invent refs.
- For consequential actions (submit, anything that costs money or dispatches
  real work), set `confirm:true` so the owner approves before we act.
- Prefer `read` and `wait_for` over assuming. If you're unsure what's on the
  page, read it.
- When the goal is met, emit `finish` with a clear summary in `reason`.
- When you hit something you can't safely do (paywall, captcha, MFA, page
  doesn't match expectations), emit `finish` with reason starting with
  "BLOCKED:" so the loop reports back instead of guessing.
"""


def _format_observation_for_brain(obs: dict) -> str:
    """Render the observation as a token-efficient block the brain reads.
    Compact and stable so the LLM doesn't waste tokens on layout noise."""
    lines = [
        f"URL: {obs.get('url')}",
        f"Title: {obs.get('title', '')}",
        "",
        "Elements:",
    ]
    for el in (obs.get("elements") or []):
        bits = [el["ref"], el["role"]]
        if el.get("name"):
            bits.append(f'"{el["name"]}"')
        if el.get("value"):
            bits.append(f"value={el['value']!r}")
        if el.get("disabled"):
            bits.append("(disabled)")
        lines.append("  " + " ".join(bits))
    if obs.get("truncated"):
        lines.append(f"  … (truncated — more elements off-screen)")
    txt = obs.get("page_text") or ""
    if txt:
        lines.append("")
        lines.append("Page text (excerpt):")
        lines.append(txt[:1500])
    return "\n".join(lines)


def _ask_brain(brain_call: Callable, goal: str, obs: dict,
               history: list[dict]) -> dict:
    """One brain call → one parsed action dict."""
    history_summary = ""
    if history:
        recent = history[-5:]
        history_summary = "Actions taken so far (most recent last):\n"
        for h in recent:
            history_summary += f"  - {h.get('type')}: {h.get('reason', '')[:120]}\n"
        history_summary += "\n"
    user_msg = (
        f"Goal: {goal}\n\n"
        f"{history_summary}"
        f"Current page observation:\n{_format_observation_for_brain(obs)}\n\n"
        f"Emit the next action as JSON."
    )
    try:
        raw = brain_call(_BRAIN_SYSTEM_PROMPT,
                          [{"role": "user", "content": user_msg}])
    except Exception as e:
        log.warning("brain call raised: %s", e)
        return {"type": "finish",
                "reason": f"BLOCKED: brain unavailable ({type(e).__name__})"}
    return _parse_action(raw)


def _parse_action(raw: str) -> dict:
    """Tolerate the brain emitting prose around the JSON. Strip markdown
    fences, pull the first { ... } block, json.loads it."""
    raw = (raw or "").strip()
    if "```" in raw:
        # Strip the most common markdown fence patterns.
        for fence in ("```json", "```JSON", "```"):
            if fence in raw:
                parts = raw.split(fence)
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("\n"):
                    raw = raw[1:]
                break
        if "```" in raw:
            raw = raw.split("```", 1)[0]
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {"type": "finish",
                "reason": "BLOCKED: brain emitted no JSON action"}
    try:
        action = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        return {"type": "finish",
                "reason": f"BLOCKED: brain emitted invalid JSON ({e})"}
    if not isinstance(action, dict) or "type" not in action:
        return {"type": "finish",
                "reason": "BLOCKED: brain emitted non-action JSON"}
    return action


def _resolve_action_selector(action: dict, obs: dict) -> dict:
    """The brain emits `ref:"e7"`. The executor needs `selector:"role=button[name='Sign in']"`.
    Translate via the current observation. Leaves non-ref actions alone."""
    ref = action.get("ref")
    if not ref or action.get("selector"):
        return action
    sel = _observer.selector_for_ref(obs, ref)
    if sel:
        action["selector"] = sel
    return action


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def run_task(goal: str,
              *,
              data_dir: Path,
              workspace_dir: Path,
              brain_call: Callable,
              start_url: str | None = None,
              recipe_name: str | None = None,
              recipe_params: dict | None = None,
              on_confirm: Callable[[dict], bool] | None = None,
              max_steps: int = DEFAULT_MAX_STEPS,
              headless: bool = True) -> dict:
    """Run a browser task end-to-end and return an AgentResult dict.

    Args:
        goal:           Plain-English description of what to do.
        data_dir:       Per-install data folder (cookies live here).
        workspace_dir:  ~/Orbi/ — downloads + screenshots land here.
        brain_call:     Same callable shape as site_scraper.make_brain_call.
        start_url:      Where to begin. Recipe can override.
        recipe_name:    If set, run that recipe instead of open-ended loop.
        recipe_params:  Recipe-specific dict (driver_id, etc.).
        on_confirm:     Callable(action_summary_dict) → bool. Returning False
                        cancels the action. If None, all consequential actions
                        are blocked (safe default for headless runs).
        max_steps:      Cap on agent-loop iterations.
        headless:       Run Chromium headless. Set False for debug.
    """
    started_at = time.time()
    history: list[dict] = []
    downloaded: list[str] = []
    screenshots_dir = data_dir / "web_agent_screenshots"

    if on_confirm is None:
        # Safe default: always decline consequential actions. Caller is
        # expected to pass a real confirm handler in production.
        on_confirm = lambda _action: False  # noqa: E731

    if start_url:
        try:
            site_key = _session.site_key_for(start_url)
        except ValueError:
            site_key = None
    else:
        site_key = None

    storage_state = _session.load(data_dir, site_key) if site_key else None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _failed_result(goal, started_at,
                              "BLOCKED: playwright not installed on this host")

    result: dict[str, Any] = {
        "ok": False, "goal": goal, "actions_taken": [],
        "final_observation": "", "downloaded_files": downloaded,
        "elapsed_seconds": 0.0, "stopped_reason": "", "screenshots": [],
        "error": None,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context_kwargs: dict[str, Any] = {
            "accept_downloads": True,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        }
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        # Capture downloads into the workspace folder.
        def _on_download(download):
            target = workspace_dir / download.suggested_filename
            try:
                download.save_as(str(target))
                downloaded.append(str(target))
            except Exception as e:
                log.warning("download save failed: %s", e)
        page.on("download", _on_download)

        try:
            if start_url:
                page.goto(start_url, timeout=DEFAULT_PER_ACTION_TIMEOUT_MS,
                          wait_until="domcontentloaded")

            # Hand-written recipe path — fast, deterministic.
            if recipe_name:
                from . import recipes as _recipes
                _recipes.import_all()
                recipe = _recipes.get(recipe_name)
                if not recipe:
                    raise RuntimeError(f"unknown recipe: {recipe_name}")
                r = recipe["run"](page, goal=goal,
                                   params=recipe_params or {},
                                   on_confirm=on_confirm,
                                   workspace_dir=workspace_dir)
                result.update({"ok": bool(r.get("ok")),
                               "stopped_reason": "recipe_done",
                               "final_observation": str(r.get("result", ""))[:2000]})
                result["actions_taken"].append({"type": "recipe",
                                                 "name": recipe_name,
                                                 "ok": r.get("ok"),
                                                 "result": r.get("result")})
            else:
                # Try a LEARNED recipe first — Orbi may have done this
                # before on this site. If it works, the task finishes in
                # milliseconds per step instead of seconds. If a step
                # fails (page changed, selector moved), we drop back into
                # the brain-driven loop from that step forward and let it
                # repair what broke.
                learned_recipe = None
                if site_key:
                    learned_recipe = _learner.find_best_match(
                        data_dir, site_key, goal)
                replay_history: list[dict] = []
                if learned_recipe:
                    replay_history = _replay_learned_recipe(
                        page=page,
                        recipe=learned_recipe,
                        data_dir=data_dir,
                        site_key=site_key,
                        screenshots_dir=screenshots_dir,
                        downloaded=downloaded,
                        on_confirm=on_confirm,
                        result=result,
                    )
                    history.extend(replay_history)
                    # If the replay completed all steps cleanly, we're
                    # done. record_success updates the recipe's stats.
                    if (replay_history
                            and not any(h.get("_outcome") == "error"
                                         for h in replay_history)):
                        _learner.record_success(
                            data_dir, site_key, goal=goal,
                            actions=replay_history, start_url=start_url,
                            recipe_id=learned_recipe.get("id"))
                        result["ok"] = True
                        result["stopped_reason"] = "learned_recipe_completed"
                        # Skip the brain-driven loop below.
                        max_steps = 0

                # Open-ended brain-driven loop
                for step in range(max_steps):
                    obs = _observer.observe(page)
                    action = _ask_brain(brain_call, goal, obs, history)
                    action = _resolve_action_selector(action, obs)

                    if action.get("type") == "finish":
                        result["ok"] = not (action.get("reason", "")
                                             .startswith("BLOCKED:"))
                        result["stopped_reason"] = action.get("reason") or "finish"
                        result["actions_taken"].append(action)
                        # Save what worked — Orbi just learned a new
                        # recipe for this site if the goal succeeded.
                        # Next time the owner asks something similar
                        # on this site, the replay path picks it up.
                        if result["ok"] and site_key:
                            try:
                                _learner.record_success(
                                    data_dir, site_key,
                                    goal=goal,
                                    actions=history,
                                    start_url=start_url)
                            except Exception as _e:
                                log.warning("could not save learned "
                                            "recipe: %s", _e)
                        break

                    if _actions.needs_confirmation(action):
                        if not on_confirm(action):
                            result["stopped_reason"] = "declined_by_owner"
                            result["actions_taken"].append(
                                {**action, "_outcome": "declined"})
                            break

                    try:
                        exec_result = _actions.execute(
                            page, action, download_dir=screenshots_dir)
                    except _actions.ActionError as e:
                        log.warning("action failed: %s", e)
                        history.append({**action, "_outcome": "error",
                                         "_error": str(e)})
                        result["actions_taken"].append(
                            {**action, "_outcome": "error", "_error": str(e)})
                        # Let the brain see the error on the next loop and recover.
                        continue

                    history.append(action)
                    result["actions_taken"].append(
                        {**action, "_outcome": "ok",
                         "_elapsed_ms": exec_result.get("elapsed_ms")})
                    if exec_result.get("downloaded"):
                        downloaded.append(exec_result["downloaded"])
                else:
                    result["stopped_reason"] = "max_steps_reached"

            # Capture final state for the caller log
            try:
                final = _observer.observe(page)
                result["final_observation"] = (
                    final.get("page_text") or final.get("url") or "")[:2000]
            except Exception:
                pass

            # Persist updated session for next run
            if site_key:
                try:
                    _session.save(data_dir, site_key, context.storage_state())
                except Exception as e:
                    log.warning("session save failed: %s", e)
        finally:
            context.close()
            browser.close()

    result["elapsed_seconds"] = round(time.time() - started_at, 1)
    result["downloaded_files"] = downloaded
    return result


def _replay_learned_recipe(*, page, recipe: dict, data_dir: Path,
                            site_key: str, screenshots_dir: Path,
                            downloaded: list, on_confirm,
                            result: dict) -> list[dict]:
    """Replay a learned recipe step-by-step. Stops at the first step that
    raises an ActionError, logs the failure to the learner, and returns
    the action history up to that point. The caller then drops back into
    the brain-driven loop to repair the broken step + the rest of the
    flow."""
    replayed: list[dict] = []
    saved_actions = recipe.get("actions") or []
    recipe_id = recipe.get("id") or ""
    for step_idx, action in enumerate(saved_actions):
        if _actions.needs_confirmation(action):
            if not on_confirm(action):
                replayed.append({**action, "_outcome": "declined"})
                result["stopped_reason"] = "declined_by_owner"
                return replayed
        try:
            exec_result = _actions.execute(
                page, action, download_dir=screenshots_dir)
        except _actions.ActionError as e:
            log.info("learned recipe %s failed at step %d (%s) — "
                      "brain will repair from here",
                      recipe_id, step_idx, e)
            _learner.record_failure(
                data_dir, site_key, recipe_id,
                failed_at_step=step_idx, error=str(e))
            replayed.append({**action, "_outcome": "error",
                              "_error": str(e), "_step": step_idx})
            return replayed
        replayed.append({**action, "_outcome": "ok",
                          "_elapsed_ms": exec_result.get("elapsed_ms"),
                          "_step": step_idx, "_replay": True})
        result["actions_taken"].append(replayed[-1])
        if exec_result.get("downloaded"):
            downloaded.append(exec_result["downloaded"])
    return replayed


def _failed_result(goal: str, started_at: float, reason: str) -> dict:
    return {
        "ok": False, "goal": goal, "actions_taken": [],
        "final_observation": "", "downloaded_files": [],
        "elapsed_seconds": round(time.time() - started_at, 1),
        "stopped_reason": reason, "screenshots": [],
        "error": reason,
    }
