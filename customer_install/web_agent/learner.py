"""
learner — Orbi learns recipes by doing.

After a successful task on a site, the agent records the action sequence
she just took and saves it as a learned recipe keyed by the site + a goal
fingerprint. Next time the owner asks something similar on the same site,
the agent replays the learned recipe FIRST (fast, near-deterministic) and
only falls back to the brain-driven loop when a step doesn't match the
current page (button moved, selector changed, etc.). When the brain fixes
the broken step, the recipe gets updated.

End result: first run on a new site is slow (LLM driving). Second run is
fast. Tenth run is near-instant on the parts of the site that haven't
changed. The customer never writes a recipe — Orbi writes them by doing.

Storage:
    DATA_DIR / "learned_recipes" / <site_key>.json

Recipe format:
    {
        "site_key":   str,                # e.g. "quickbooks_intuit_com"
        "recipes":    list[LearnedRecipe],
        "_version":   int,
    }

LearnedRecipe:
    {
        "id":             str,             # stable random id
        "goal_text":      str,             # the goal that produced it
        "goal_tokens":    list[str],       # lowercased, stop-words removed
        "actions":        list[dict],      # the action sequence
        "created_at":     int,             # unix ts
        "last_used_at":   int,
        "success_count":  int,
        "failure_count":  int,
        "url_pattern":    str,             # the URL the recipe starts at
    }
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("orbi.web_agent.learner")

_DIR_NAME = "learned_recipes"
_FILE_VERSION = 1
_MIN_ACTIONS_TO_LEARN = 2  # don't bother saving 1-step "recipes"
_MAX_RECIPES_PER_SITE = 50  # cap, prune oldest beyond this
_MATCH_SCORE_THRESHOLD = 0.45  # 0..1 — how confident must the match be

# Common English stop words we strip before tokenizing goals — a goal is
# the customer's intent, not their phrasing. "search google for reno deli"
# and "please google reno deli" should tokenize to roughly the same set.
_STOP_WORDS = {
    "a", "an", "and", "or", "the", "for", "to", "of", "on", "in", "at",
    "with", "from", "by", "is", "are", "was", "were", "be", "been",
    "please", "can", "could", "would", "should", "do", "does", "did",
    "you", "your", "i", "me", "my", "we", "our", "they", "them",
    "this", "that", "those", "these", "it", "its", "as", "if",
    "into", "now", "today", "tomorrow", "later", "again",
}


# ---------------------------------------------------------------------------
# Tokenization + matching
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, drop stop words, dedupe (preserving
    order). Numbers are kept so 'invoice 1234' and 'invoice 5678' don't
    collide on the same recipe."""
    if not text:
        return []
    norm = re.sub(r"[^\w\s]", " ", text.lower())
    seen: set[str] = set()
    out: list[str] = []
    for tok in norm.split():
        if tok in _STOP_WORDS or len(tok) < 2:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _match_score(query_tokens: list[str], recipe_tokens: list[str]) -> float:
    """Jaccard-like overlap with a small boost for token-order similarity.
    Returns 0..1. Cheap, deterministic, no LLM. Good enough to pick the
    right recipe when several are close — LLM fallback handles the rare
    case where two recipes really do match equally well."""
    if not query_tokens or not recipe_tokens:
        return 0.0
    qset = set(query_tokens)
    rset = set(recipe_tokens)
    overlap = qset & rset
    if not overlap:
        return 0.0
    union = qset | rset
    base = len(overlap) / max(1, len(union))
    # Order bonus — if many shared tokens appear in the same order in
    # both, that's a stronger signal.
    q_ordered = [t for t in query_tokens if t in overlap]
    r_ordered = [t for t in recipe_tokens if t in overlap]
    order_bonus = 0.1 if q_ordered == r_ordered else 0.0
    return min(1.0, base + order_bonus)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _path(data_dir: Path, site_key: str) -> Path:
    return data_dir / _DIR_NAME / f"{site_key}.json"


def _load(data_dir: Path, site_key: str) -> dict:
    p = _path(data_dir, site_key)
    if not p.exists():
        return {"site_key": site_key, "recipes": [], "_version": _FILE_VERSION}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("learned recipes for %s corrupt, starting fresh: %s",
                    site_key, e)
        return {"site_key": site_key, "recipes": [], "_version": _FILE_VERSION}
    if data.get("_version") != _FILE_VERSION:
        log.info("learned recipes for %s version mismatch — ignoring", site_key)
        return {"site_key": site_key, "recipes": [], "_version": _FILE_VERSION}
    return data


def _save(data_dir: Path, site_key: str, data: dict) -> None:
    p = _path(data_dir, site_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_best_match(data_dir: Path, site_key: str,
                     goal: str) -> dict | None:
    """Return the highest-scoring learned recipe for this site+goal, or
    None if no recipe clears the match threshold. Callers replay the
    returned recipe optimistically; on any step failure they call
    record_failure() and fall through to the brain loop."""
    data = _load(data_dir, site_key)
    recipes = data.get("recipes") or []
    if not recipes:
        return None
    qtok = _tokenize(goal)
    best: dict | None = None
    best_score = 0.0
    for r in recipes:
        score = _match_score(qtok, r.get("goal_tokens") or [])
        # Slight bias toward recipes that have a higher success record —
        # tiebreaker, not a primary signal.
        success = max(0, int(r.get("success_count", 0)))
        score += min(0.05, success * 0.01)
        if score > best_score:
            best_score = score
            best = r
    if best is None or best_score < _MATCH_SCORE_THRESHOLD:
        return None
    log.info("learned recipe match for site=%s score=%.2f goal=%r",
              site_key, best_score, goal[:80])
    return best


def record_success(data_dir: Path, site_key: str, *,
                    goal: str,
                    actions: list[dict],
                    start_url: str | None,
                    recipe_id: str | None = None) -> str | None:
    """Persist a successful action sequence as a new learned recipe OR
    update an existing one's stats if `recipe_id` is passed.

    Returns the recipe id that was saved/updated, or None if the sequence
    was too short to bother saving.
    """
    actions = _clean_actions(actions)
    if recipe_id is None and len(actions) < _MIN_ACTIONS_TO_LEARN:
        return None

    data = _load(data_dir, site_key)
    recipes = data.get("recipes") or []

    if recipe_id:
        # Update existing recipe's success stats + last_used.
        for r in recipes:
            if r.get("id") == recipe_id:
                r["last_used_at"] = int(time.time())
                r["success_count"] = int(r.get("success_count", 0)) + 1
                _save(data_dir, site_key, data)
                return recipe_id
        # Recipe id passed but not found — fall through and store as new.

    # Skip dup-storing if an identical sequence already exists
    for r in recipes:
        if r.get("actions") == actions:
            r["last_used_at"] = int(time.time())
            r["success_count"] = int(r.get("success_count", 0)) + 1
            _save(data_dir, site_key, data)
            return r.get("id")

    new_id = f"r_{int(time.time())}_{secrets.token_hex(3)}"
    recipes.append({
        "id":            new_id,
        "goal_text":     goal[:300],
        "goal_tokens":   _tokenize(goal),
        "actions":       actions,
        "created_at":    int(time.time()),
        "last_used_at":  int(time.time()),
        "success_count": 1,
        "failure_count": 0,
        "url_pattern":   start_url or "",
    })

    # Prune oldest if we've grown past the cap. Keep the most-used + most-
    # recently-used recipes; drop the ones with the lowest score.
    if len(recipes) > _MAX_RECIPES_PER_SITE:
        recipes.sort(key=lambda r: (r.get("success_count", 0),
                                     r.get("last_used_at", 0)),
                      reverse=True)
        recipes = recipes[:_MAX_RECIPES_PER_SITE]

    data["recipes"] = recipes
    _save(data_dir, site_key, data)
    log.info("learned new recipe %s for site=%s (%d actions)",
              new_id, site_key, len(actions))
    return new_id


def record_failure(data_dir: Path, site_key: str,
                    recipe_id: str, *,
                    failed_at_step: int,
                    error: str) -> None:
    """Note that a learned recipe failed mid-replay. Bumps the failure
    counter so persistently-broken recipes get pruned. If a recipe's
    failure rate exceeds 50% over its lifetime AND it has been used 5+
    times, drop it — the site has probably changed enough that the
    learned shape is wrong, and the brain loop will learn a new one."""
    data = _load(data_dir, site_key)
    recipes = data.get("recipes") or []
    for r in list(recipes):
        if r.get("id") == recipe_id:
            r["failure_count"] = int(r.get("failure_count", 0)) + 1
            total = (int(r.get("success_count", 0))
                      + int(r.get("failure_count", 0)))
            if total >= 5 and (r["failure_count"] / total) > 0.5:
                recipes.remove(r)
                log.info("dropped stale recipe %s for site=%s "
                          "(failure rate > 50%% after %d attempts)",
                          recipe_id, site_key, total)
            log.debug("recipe %s for site=%s failed at step %d: %s",
                      recipe_id, site_key, failed_at_step, error[:200])
            break
    data["recipes"] = recipes
    _save(data_dir, site_key, data)


def update_action(data_dir: Path, site_key: str,
                   recipe_id: str, step_index: int,
                   replacement_action: dict) -> bool:
    """The brain repaired a broken step. Replace the original action so
    future replays use the fixed version. Returns True on success."""
    data = _load(data_dir, site_key)
    for r in data.get("recipes") or []:
        if r.get("id") != recipe_id:
            continue
        actions = r.get("actions") or []
        if 0 <= step_index < len(actions):
            actions[step_index] = _clean_actions([replacement_action])[0]
            r["actions"] = actions
            _save(data_dir, site_key, data)
            log.info("recipe %s for site=%s: step %d updated",
                      recipe_id, site_key, step_index)
            return True
    return False


def list_recipes(data_dir: Path, site_key: str) -> list[dict]:
    """Used by the dashboard's 'Learned recipes' tab. Returns a sanitized
    summary — no raw action payloads — so the owner can see what Orbi
    has learned for a given site and prune entries she doesn't trust."""
    data = _load(data_dir, site_key)
    out = []
    for r in data.get("recipes") or []:
        out.append({
            "id":            r.get("id"),
            "goal_text":     r.get("goal_text"),
            "action_count":  len(r.get("actions") or []),
            "created_at":    r.get("created_at"),
            "last_used_at":  r.get("last_used_at"),
            "success_count": r.get("success_count", 0),
            "failure_count": r.get("failure_count", 0),
        })
    return out


def forget(data_dir: Path, site_key: str, recipe_id: str) -> bool:
    """Delete a single learned recipe at the owner's request."""
    data = _load(data_dir, site_key)
    before = len(data.get("recipes") or [])
    data["recipes"] = [r for r in (data.get("recipes") or [])
                        if r.get("id") != recipe_id]
    after = len(data["recipes"])
    if before == after:
        return False
    _save(data_dir, site_key, data)
    log.info("recipe %s for site=%s deleted by owner", recipe_id, site_key)
    return True


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

# Action keys we PRESERVE when storing a learned recipe. Other keys
# (`_outcome`, `_elapsed_ms`, `reason` rationale, etc.) are noise we
# don't want polluting future replays.
_KEEP_KEYS = ("type", "url", "selector", "text", "value",
              "condition", "timeout_ms", "scroll", "confirm")


def _clean_actions(actions: list[dict]) -> list[dict]:
    """Strip the controller's per-step bookkeeping so we store the action
    template, not the run log. Recipes are cleaner that way."""
    out = []
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        cleaned = {k: a[k] for k in _KEEP_KEYS if k in a}
        if cleaned.get("type") in (None, "finish"):
            continue
        out.append(cleaned)
    return out
