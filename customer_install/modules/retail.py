"""
retail module — services and products catalog for Orbi.

Covers any business that sells services, products, or both:
  - Auto shops (services: oil change, brakes; products: wiper blades)
  - Salons (services: haircut, color, blowout)
  - Retail stores (products only)
  - Mixed shops (e.g. car wash + detailing products)

Storage layout (inside the customer's data/ folder):
  data/retail/
    services.json   — list of service records
    products.json   — list of product records

Each service:  {id, name, category, price, duration_min, description, active}
Each product:  {id, name, category, price, sku, description, active}
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
_LOCK = threading.Lock()


# ── Storage helpers ──────────────────────────────────────────────────────────

def _retail_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "retail"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(path: Path) -> list[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text("utf-8")) or []
    except Exception:
        log.exception("retail: failed to load %s", path)
    return []


def _save(path: Path, records: list[dict]) -> None:
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), "utf-8")


# ── Services ─────────────────────────────────────────────────────────────────

def list_services(data_dir: Path, active_only: bool = False) -> list[dict]:
    path = _retail_dir(data_dir) / "services.json"
    with _LOCK:
        items = _load(path)
    if active_only:
        items = [s for s in items if s.get("active", True)]
    return items


def add_service(data_dir: Path, name: str, price: float,
                category: str = "", duration_min: int = 0,
                description: str = "") -> dict:
    path = _retail_dir(data_dir) / "services.json"
    record = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "category": (category or "").strip(),
        "price": round(float(price), 2),
        "duration_min": int(duration_min or 0),
        "description": (description or "").strip(),
        "active": True,
        "created_at": int(time.time()),
    }
    with _LOCK:
        items = _load(path)
        items.append(record)
        _save(path, items)
    return record


def update_service(data_dir: Path, service_id: str, **fields) -> dict | None:
    path = _retail_dir(data_dir) / "services.json"
    with _LOCK:
        items = _load(path)
        for item in items:
            if item["id"] == service_id:
                for k, v in fields.items():
                    if k in ("name", "category", "description"):
                        item[k] = str(v).strip()
                    elif k == "price":
                        item[k] = round(float(v), 2)
                    elif k == "duration_min":
                        item[k] = int(v or 0)
                    elif k == "active":
                        item[k] = bool(v)
                _save(path, items)
                return item
    return None


def delete_service(data_dir: Path, service_id: str) -> bool:
    path = _retail_dir(data_dir) / "services.json"
    with _LOCK:
        items = _load(path)
        before = len(items)
        items = [s for s in items if s["id"] != service_id]
        if len(items) < before:
            _save(path, items)
            return True
    return False


# ── Products ─────────────────────────────────────────────────────────────────

def list_products(data_dir: Path, active_only: bool = False) -> list[dict]:
    path = _retail_dir(data_dir) / "products.json"
    with _LOCK:
        items = _load(path)
    if active_only:
        items = [p for p in items if p.get("active", True)]
    return items


def add_product(data_dir: Path, name: str, price: float,
                category: str = "", sku: str = "",
                description: str = "") -> dict:
    path = _retail_dir(data_dir) / "products.json"
    record = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "category": (category or "").strip(),
        "price": round(float(price), 2),
        "sku": (sku or "").strip(),
        "description": (description or "").strip(),
        "active": True,
        "created_at": int(time.time()),
    }
    with _LOCK:
        items = _load(path)
        items.append(record)
        _save(path, items)
    return record


def update_product(data_dir: Path, product_id: str, **fields) -> dict | None:
    path = _retail_dir(data_dir) / "products.json"
    with _LOCK:
        items = _load(path)
        for item in items:
            if item["id"] == product_id:
                for k, v in fields.items():
                    if k in ("name", "category", "description", "sku"):
                        item[k] = str(v).strip()
                    elif k == "price":
                        item[k] = round(float(v), 2)
                    elif k == "active":
                        item[k] = bool(v)
                _save(path, items)
                return item
    return None


def delete_product(data_dir: Path, product_id: str) -> bool:
    path = _retail_dir(data_dir) / "products.json"
    with _LOCK:
        items = _load(path)
        before = len(items)
        items = [p for p in items if p["id"] != product_id]
        if len(items) < before:
            _save(path, items)
            return True
    return False


# ── Orby context block (injected into chat system prompt) ────────────────────

def context_block(data_dir: Path) -> str:
    """Return a compact text block summarising the catalog for the LLM."""
    services = list_services(data_dir, active_only=True)
    products = list_products(data_dir, active_only=True)
    if not services and not products:
        return ""
    lines = ["RETAIL CATALOG (answer from this — never guess prices):"]
    if services:
        lines.append("  Services:")
        cats: dict[str, list] = {}
        for s in services:
            cats.setdefault(s.get("category") or "General", []).append(s)
        for cat, items in cats.items():
            lines.append(f"    {cat}:")
            for s in items:
                dur = f", {s['duration_min']} min" if s.get("duration_min") else ""
                desc = f" — {s['description']}" if s.get("description") else ""
                price_str = f"${s['price']:.2f}" if s.get("price") else "pricing on request"
                lines.append(f"      • {s['name']}: {price_str}{dur}{desc}")
    if products:
        lines.append("  Products:")
        cats = {}
        for p in products:
            cats.setdefault(p.get("category") or "General", []).append(p)
        for cat, items in cats.items():
            lines.append(f"    {cat}:")
            for p in items:
                sku = f" (SKU: {p['sku']})" if p.get("sku") else ""
                desc = f" — {p['description']}" if p.get("description") else ""
                price_str = f"${p['price']:.2f}" if p.get("price") else "pricing on request"
                lines.append(f"      • {p['name']}{sku}: {price_str}{desc}")
    return "\n".join(lines)


# ── Import from website scrape ───────────────────────────────────────────────

def _parse_price(raw: str) -> float | None:
    """Extract the first dollar amount from a scraped price string.

    Handles: "$12.95", "350+", "$120-190+", "95+ /month", "From $4.95",
    "Not specified", "".  Returns None if no number is found.
    """
    import re
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if s in ("", "not specified", "varies", "call", "tbd", "tbh", "contact us"):
        return None
    # Pull every decimal number out of the string, take the first
    nums = re.findall(r"\d+(?:\.\d+)?", raw)
    if not nums:
        return None
    try:
        return round(float(nums[0]), 2)
    except ValueError:
        return None


def _duration_from_text(text: str) -> int:
    """Pull a duration in minutes from a description string if present.
    Handles: '30 min', '1 hr', '90 min', '1.5 hr'.
    Caps at 480 min (8 hours) so marketing phrases like '72-hour moisture'
    don't produce nonsense durations.
    """
    import re
    if not text:
        return 0
    # "hr" / "hour" — only count if the number ≤ 8
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:hr|hour)s?", text, re.I)
    if m:
        hours = float(m.group(1))
        if hours <= 8:
            return int(hours * 60)
    m = re.search(r"(\d+)\s*min", text, re.I)
    if m:
        mins = int(m.group(1))
        return min(mins, 480)
    return 0


def import_from_scrape_profile(data_dir: Path, profile: dict) -> dict:
    """Read a site_scraper profile dict and import any services / menu_items
    into the retail module.  Items already present (same lowered name) are
    skipped.  Returns {services_added, products_added, skipped}.
    """
    services_added = 0
    products_added = 0
    skipped = 0

    existing_svcs = {s["name"].strip().lower() for s in list_services(data_dir)}
    existing_prods = {p["name"].strip().lower() for p in list_products(data_dir)}
    # Track names imported this run so services don't also land as products
    imported_names: set[str] = set()

    # services → retail services
    for item in (profile.get("services") or []):
        name = (item.get("name") or "").strip()
        if not name or name.lower() in existing_svcs:
            skipped += 1
            continue
        price = _parse_price(item.get("price") or "")
        desc = (item.get("description") or "").strip()
        cat  = (item.get("category") or "").strip()
        # Skip bare category-label rows: no parseable price AND no description
        # (these are navigation headers the LLM mistakenly extracted as services)
        if price is None and not desc:
            skipped += 1
            continue
        price = price if price is not None else 0.0
        dur  = _duration_from_text(desc) or _duration_from_text(item.get("price") or "")
        add_service(data_dir, name=name, price=price,
                    category=cat, duration_min=dur, description=desc)
        existing_svcs.add(name.lower())
        imported_names.add(name.lower())
        services_added += 1

    # menu_items → retail products
    for item in (profile.get("menu_items") or []):
        name = (item.get("name") or "").strip()
        if not name or name.lower() in existing_prods or name.lower() in imported_names:
            skipped += 1
            continue
        price = _parse_price(item.get("price") or "")
        if price is None:
            price = 0.0
        desc = (item.get("description") or "").strip()
        cat  = (item.get("category") or "").strip()
        add_product(data_dir, name=name, price=price,
                    category=cat, description=desc)
        existing_prods.add(name.lower())
        products_added += 1

    log.info(
        "retail import: +%d services, +%d products, %d skipped",
        services_added, products_added, skipped,
    )
    return {
        "services_added": services_added,
        "products_added": products_added,
        "skipped": skipped,
    }


# ── Summary ──────────────────────────────────────────────────────────────────

def summary(data_dir: Path) -> dict:
    services = list_services(data_dir)
    products = list_products(data_dir)
    return {
        "service_count": len([s for s in services if s.get("active", True)]),
        "product_count": len([p for p in products if p.get("active", True)]),
        "total_items": len(services) + len(products),
    }
