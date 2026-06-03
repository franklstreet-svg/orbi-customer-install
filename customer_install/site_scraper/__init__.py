"""
site_scraper — full-domain crawler with LLM per-page extraction.

This is the rebuilt scraper Frank specced: every page on the customer's
domain gets fetched and read, no keyword prioritization, no "this page
looks unimportant" heuristics. The LLM extracts whatever structured data
it can find from each page's text — menu items, hours, addresses, policies,
gift-card rules, allergens, parking, anything useful for an AI receptionist.

Architecture:
  http_client.py       — fetch a single URL (timeouts, UA, error capture)
  link_extractor.py    — extract every <a href> on a page, same-domain only,
                         NO prioritization (every link gets queued)
  page_parser.py       — strip HTML to readable text + image alt-text
  llm_extract.py       — call the brain with the page text → structured dict
  crawler.py           — BFS over the link graph with safety caps
  merge.py             — fold every page's extraction into one profile
  storage.py           — write data/customer_profiles/<domain>.json +
                         data/customer_profiles/<domain>_pages/<slug>.txt

Public API:
  crawl_site(start_url, data_dir, brain_call=None, max_pages=500, max_depth=5)
"""

from .crawler import crawl_site  # noqa: F401
from .storage import customer_profile_path, customer_pages_dir  # noqa: F401


def make_brain_call(config: dict):
    """Build the brain_call adapter the crawler hands to llm_extract.
    Wraps Orby's existing llm_client so the scraper goes through the
    same 3-tier fallback (brain → HF → local) every other LLM call uses.

    EXTRACTOR USES A SEPARATE MODEL. The customer-facing chat model
    (Qwen 72B) is slow and bottlenecks during scrapes — one extraction
    call per page, each running 8-12 sec, with the 20-sec HF timeout
    tripping often. The extractor reads `huggingface_extractor.model`
    from config (default Llama 3.1 8B) for a much faster, JSON-friendly
    model better suited to per-page structured extraction.

    Returns a callable: brain_call(system_prompt, messages) → str
    or None if no LLM is configured at all (then llm_extract falls back
    to regex-only)."""
    try:
        import llm_client
    except ImportError:
        return None

    # Build a per-call config where the HF model is swapped for the fast
    # extractor model — doesn't mutate the caller's config.
    extractor_override = config.get("huggingface_extractor") or {}
    extractor_config = dict(config)
    base_hf = dict(config.get("huggingface", {}))
    if extractor_override.get("model"):
        base_hf["model"] = extractor_override["model"]
    if extractor_override.get("timeout_seconds"):
        base_hf["timeout_seconds"] = extractor_override["timeout_seconds"]
    extractor_config["huggingface"] = base_hf

    def _call(system: str, messages: list[dict]) -> str:
        resp = llm_client.generate(extractor_config, system, messages)
        return (resp.text or "").strip()
    return _call
