"""
google_search — first working recipe; sanity check for the whole stack.

Goal: take a query string, return the top N organic results as a list of
{title, url, snippet} dicts. No auth, no consequential actions, no human
confirmation gate exercise. Verifies actions + observer + controller wiring
end-to-end against a real site every developer has access to.

Usage:
    run_task(
        goal="search Google for 'reno deli'",
        recipe_name="google_search",
        recipe_params={"query": "reno deli", "top_n": 5},
        ...
    )
"""

from __future__ import annotations

import logging

from . import register

log = logging.getLogger("orbi.web_agent.recipes.google_search")


def run(page, *, goal: str, params: dict, on_confirm, workspace_dir) -> dict:
    query = (params.get("query") or goal or "").strip()
    top_n = int(params.get("top_n", 5))
    if not query:
        return {"ok": False, "result": "no query provided", "downloaded": []}

    page.goto("https://www.google.com/", wait_until="domcontentloaded",
              timeout=15_000)

    # Search box — Google's accepted-cookies version uses textarea[name=q];
    # the unauthenticated version uses input[name=q]. Try both.
    for selector in ("textarea[name='q']", "input[name='q']"):
        try:
            page.fill(selector, query, timeout=3_000)
            page.press(selector, "Enter")
            break
        except Exception:
            continue
    else:
        return {"ok": False, "result": "no search box found", "downloaded": []}

    page.wait_for_load_state("domcontentloaded", timeout=15_000)

    # Scrape organic results — Google's result blocks are <div.g> with
    # an <h3> title and the first <a> being the destination.
    js = """
    (topN) => {
      const out = [];
      document.querySelectorAll('div.g, div[data-sokoban-container]')
              .forEach(block => {
        if (out.length >= topN) return;
        const titleEl = block.querySelector('h3');
        const linkEl  = block.querySelector('a[href]');
        const snipEl  = block.querySelector(
            'div[data-content-feature="1"], div.VwiC3b, span.aCOpRe, .lEBKkf');
        if (titleEl && linkEl) {
          out.push({
            title:   titleEl.innerText.trim(),
            url:     linkEl.href,
            snippet: (snipEl ? snipEl.innerText : '').trim().slice(0, 240),
          });
        }
      });
      return out;
    }
    """
    try:
        results = page.evaluate(js, top_n) or []
    except Exception as e:
        log.warning("google_search scrape failed: %s", e)
        return {"ok": False,
                "result": f"could not parse results: {e}",
                "downloaded": []}

    return {
        "ok": True,
        "result": {"query": query, "results": results,
                    "count": len(results)},
        "downloaded": [],
    }


# Self-register on import
register({
    "name": "google_search",
    "site": "google.com",
    "run":  run,
})
