"""
maps.py — Open-source mapping for Orby.

Stack:
  · Nominatim (nominatim.openstreetmap.org) — geocoding, no API key
  · OSRM public API (router.project-osrm.org) — driving routes + ETAs
  · Haversine — pure-Python straight-line fallback
  · Leaflet.js + OpenStreetMap tiles — interactive map display

Usage policy compliance:
  · Nominatim: max 1 req/sec, valid User-Agent, results cached locally
  · OSRM public: demo server, suitable for low-volume business use;
    swap endpoint to a self-hosted OSRM for high-volume production

Public API (called from vola.py handlers):
  geocode(address, cache_dir)         → {lat, lon, display_name} | None
  haversine(lat1, lon1, lat2, lon2)   → float (straight-line miles)
  driving_route(origin, dest)         → {distance_miles, duration_min, ok}
  in_service_area(addr, biz_profile, cache_dir) → {within, distance_miles, duration_min}
  distance_reply(origin, dest, cache_dir)       → str (conversational reply)
  map_share_url(lat, lon, zoom)       → str (openstreetmap.org link)
  contacts_with_coords(contacts, cache_dir)     → list[{name, lat, lon, address}]
  build_map_html(markers, route, center, zoom)  → str (self-contained Leaflet HTML)
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("orbi.maps")

_NOMINATIM_URL  = "https://nominatim.openstreetmap.org/search"
_OSRM_BASE      = "https://router.project-osrm.org/route/v1/driving"
_USER_AGENT     = "OrbyBusinessAssistant/1.0 (small-business AI; contact franklstreet@yahoo.com)"
_CACHE_FILE     = "geocode_cache.json"
_RATE_LIMIT_SEC = 1.1   # Nominatim: max 1 req/sec
_last_geocode_ts: list[float] = [0.0]   # mutable for closure


# ── Geocoding ─────────────────────────────────────────────────────────────────

def _load_cache(cache_dir: Path) -> dict:
    try:
        p = cache_dir / _CACHE_FILE
        if p.exists():
            return json.loads(p.read_text("utf-8"))
    except Exception:
        pass
    return {}


def _save_cache(cache_dir: Path, cache: dict) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / _CACHE_FILE).write_text(json.dumps(cache, indent=2), "utf-8")
    except Exception as e:
        log.debug(f"geocode cache save failed: {e}")


def geocode(address: str, cache_dir: Optional[Path] = None) -> Optional[dict]:
    """Convert a free-form address string to {lat, lon, display_name}.
    Caches results in cache_dir/geocode_cache.json.
    Returns None on failure."""
    if not address or not address.strip():
        return None
    key = address.strip().lower()

    if cache_dir:
        cache = _load_cache(cache_dir)
        if key in cache:
            return cache[key]

    # Rate-limit: Nominatim ToS requires max 1 req/sec
    now = time.monotonic()
    gap = now - _last_geocode_ts[0]
    if gap < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - gap)
    _last_geocode_ts[0] = time.monotonic()

    params = urllib.parse.urlencode({
        "q": address,
        "format": "json",
        "limit": 1,
        "addressdetails": 0,
    })
    url = f"{_NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning(f"geocode failed for {address!r}: {e}")
        return None

    if not data:
        return None

    result = {
        "lat": float(data[0]["lat"]),
        "lon": float(data[0]["lon"]),
        "display_name": data[0].get("display_name", address),
    }
    if cache_dir:
        cache[key] = result
        _save_cache(cache_dir, cache)
    return result


# ── Distance math ─────────────────────────────────────────────────────────────

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in miles between two lat/lon points."""
    R = 3_958.8  # Earth radius miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def driving_route(origin: dict, dest: dict) -> dict:
    """Call OSRM public API for a driving route between two geocoded points.
    origin/dest: {lat, lon} dicts.
    Returns: {ok, distance_miles, duration_min, geometry_coords}
    Falls back gracefully when OSRM is unavailable."""
    try:
        olat, olon = origin["lat"], origin["lon"]
        dlat, dlon = dest["lat"], dest["lon"]
        url = (f"{_OSRM_BASE}/{olon},{olat};{dlon},{dlat}"
               f"?overview=simplified&geometries=geojson&steps=false")
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("code") != "Ok" or not data.get("routes"):
            raise ValueError(f"OSRM code={data.get('code')}")

        route = data["routes"][0]
        meters   = route["distance"]          # metres
        seconds  = route["duration"]          # seconds
        coords   = (route.get("geometry", {}).get("coordinates") or [])
        return {
            "ok": True,
            "distance_miles": round(meters / 1609.344, 1),
            "duration_min":   round(seconds / 60.0),
            "geometry_coords": coords,        # [[lon, lat], ...] GeoJSON order
        }
    except Exception as e:
        log.debug(f"OSRM routing failed ({e}); using haversine fallback")
        dist = haversine(origin["lat"], origin["lon"], dest["lat"], dest["lon"])
        # Rough ETA: assume 25 mph average in mixed driving
        return {
            "ok": False,
            "distance_miles": round(dist, 1),
            "duration_min":   round(dist / 25 * 60),
            "geometry_coords": [],
            "fallback": True,
        }


# ── Service area ──────────────────────────────────────────────────────────────

def in_service_area(address: str, biz_profile: dict,
                    cache_dir: Optional[Path] = None) -> dict:
    """Check whether an address falls within the business's service radius.

    Reads service radius from biz_profile['service_area_miles'] (default 25).
    Business center from biz_profile['address'] + biz_profile['contact']['website'].

    Returns {within, distance_miles, duration_min, error}.
    """
    radius = float((biz_profile or {}).get("service_area_miles") or 25.0)

    # Build a full business address string
    addr_block = (biz_profile or {}).get("address") or {}
    biz_addr = ", ".join(filter(None, [
        addr_block.get("street", ""),
        addr_block.get("city", ""),
        addr_block.get("state", ""),
        addr_block.get("zip", ""),
    ]))
    if not biz_addr.strip():
        return {"within": None, "error": "business_address_not_configured",
                "distance_miles": None, "duration_min": None}

    origin = geocode(biz_addr, cache_dir)
    if not origin:
        return {"within": None, "error": "could_not_geocode_business",
                "distance_miles": None, "duration_min": None}

    dest = geocode(address, cache_dir)
    if not dest:
        return {"within": None, "error": "could_not_geocode_destination",
                "distance_miles": None, "duration_min": None}

    route = driving_route(origin, dest)
    dist  = route["distance_miles"]
    return {
        "within": dist <= radius,
        "distance_miles": dist,
        "duration_min": route["duration_min"],
        "driving": route["ok"],
        "error": None,
    }


# ── Map URL helpers ───────────────────────────────────────────────────────────

def map_share_url(lat: float, lon: float, zoom: int = 15) -> str:
    """Return an openstreetmap.org link the owner or customer can click."""
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map={zoom}/{lat}/{lon}"


def directions_url(origin: dict, dest: dict) -> str:
    """Return an OpenStreetMap routing URL."""
    return (f"https://www.openstreetmap.org/directions?engine=osrm_car"
            f"&route={origin['lat']},{origin['lon']};{dest['lat']},{dest['lon']}")


# ── Conversational helpers ────────────────────────────────────────────────────

def distance_reply(origin_addr: str, dest_addr: str,
                   cache_dir: Optional[Path] = None) -> str:
    """Return a plain-English distance/ETA reply for the two addresses."""
    origin = geocode(origin_addr, cache_dir)
    if not origin:
        return f"I wasn't able to find \"{origin_addr}\" on the map. Double-check the address?"
    dest = geocode(dest_addr, cache_dir)
    if not dest:
        return f"I wasn't able to find \"{dest_addr}\" on the map. Double-check the address?"

    route = driving_route(origin, dest)
    dist  = route["distance_miles"]
    mins  = route["duration_min"]
    hours = mins // 60
    rem   = mins % 60
    dur   = (f"{hours}h {rem}m" if hours else f"{mins} min")
    method = "driving" if route["ok"] else "straight-line estimate"
    link   = directions_url(origin, dest)

    return (f"It's about {dist} miles from {origin_addr} to {dest_addr} "
            f"— roughly {dur} by {method}. "
            f"Directions: {link}")


# ── Contact / lead geo enrichment ─────────────────────────────────────────────

def contacts_with_coords(contacts: list[dict],
                         cache_dir: Optional[Path] = None) -> list[dict]:
    """Walk a contacts list and geocode any that have an address but no coords.
    Returns a filtered list of contacts that have valid lat/lon."""
    out = []
    for c in (contacts or []):
        addr = (c.get("address") or "").strip()
        if not addr:
            continue
        lat = c.get("_lat")
        lon = c.get("_lon")
        if not (lat and lon):
            geo = geocode(addr, cache_dir)
            if not geo:
                continue
            lat, lon = geo["lat"], geo["lon"]
        out.append({**c, "_lat": lat, "_lon": lon})
    return out


# ── Leaflet map builder ───────────────────────────────────────────────────────

def build_map_html(
    markers: list[dict],          # [{lat, lon, label, color, popup}]
    route_coords: list = None,    # [[lon, lat], ...] GeoJSON order from OSRM
    center: tuple = None,         # (lat, lon) — auto-fit if None
    zoom: int = 13,
    height: str = "400px",
    title: str = "",
) -> str:
    """Return a self-contained HTML string with a Leaflet.js map.

    Drop it into a <div> or serve it in an iframe — no server-side deps.
    Markers: [{lat, lon, label, color='#1a5276', popup='...'}]
    route_coords: list of [lon, lat] pairs (GeoJSON order) from OSRM.
    """
    if not markers:
        return "<p>No locations to display.</p>"

    # Auto-center on first marker if not provided
    if not center:
        lats = [m["lat"] for m in markers if m.get("lat")]
        lons = [m["lon"] for m in markers if m.get("lon")]
        center = (
            sum(lats) / len(lats),
            sum(lons) / len(lons),
        ) if lats else (39.5296, -119.8138)  # Reno default

    # Build JS marker list
    marker_js_lines = []
    for m in markers:
        lat   = m.get("lat", 0)
        lon   = m.get("lon", 0)
        label = str(m.get("label", "")).replace("'", "\\'")
        popup = str(m.get("popup", label)).replace("'", "\\'")
        color = m.get("color", "#1a5276")
        marker_js_lines.append(
            f"  addMarker({lat}, {lon}, '{label}', '{popup}', '{color}');"
        )
    markers_js = "\n".join(marker_js_lines)

    # Build route polyline JS
    route_js = ""
    if route_coords:
        # GeoJSON is [lon, lat] — Leaflet wants [lat, lon]
        pairs = [[c[1], c[0]] for c in route_coords if len(c) >= 2]
        route_js = (
            f"  L.polyline({json.dumps(pairs)}, "
            f"{{color:'#e74c3c', weight:4, opacity:0.8}}).addTo(map);"
        )

    title_html = f"<div style='font:700 13px sans-serif;margin-bottom:6px'>{title}</div>" if title else ""

    return f"""
{title_html}
<div id="orby-map-{id(markers)}" style="width:100%;height:{height};border-radius:8px;overflow:hidden"></div>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){{
  var mapId = 'orby-map-{id(markers)}';
  if (document.getElementById(mapId)._leaflet_id) return;
  var map = L.map(mapId).setView([{center[0]}, {center[1]}], {zoom});
  L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }}).addTo(map);

  function addMarker(lat, lon, label, popup, color) {{
    var icon = L.divIcon({{
      html: '<div style="background:'+color+';width:14px;height:14px;border-radius:50%;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4)"></div>',
      className: '', iconSize: [14,14], iconAnchor: [7,7], popupAnchor: [0,-10]
    }});
    L.marker([lat, lon], {{icon:icon}}).addTo(map).bindPopup(popup);
  }}

{markers_js}
{route_js}

  if ({len(markers)} > 1) {{
    var bounds = L.latLngBounds({json.dumps([[m['lat'],m['lon']] for m in markers if m.get('lat')])});
    map.fitBounds(bounds, {{padding:[30,30]}});
  }}
}})();
</script>
""".strip()


# ── Owner dashboard: all-contacts map ────────────────────────────────────────

def contacts_map_html(contacts: list[dict], biz_profile: dict,
                      cache_dir: Optional[Path] = None) -> str:
    """Build a Leaflet map HTML showing all contacts that have addresses.
    Includes a pin for the business itself."""
    markers = []

    # Business pin (blue)
    addr_block = (biz_profile or {}).get("address") or {}
    biz_addr = ", ".join(filter(None, [
        addr_block.get("street"), addr_block.get("city"),
        addr_block.get("state"), addr_block.get("zip"),
    ]))
    biz_name = (biz_profile or {}).get("name", "Your Business")
    if biz_addr:
        geo = geocode(biz_addr, cache_dir)
        if geo:
            markers.append({
                "lat": geo["lat"], "lon": geo["lon"],
                "label": biz_name,
                "popup": f"<b>{biz_name}</b><br>{biz_addr}",
                "color": "#1a5276",
            })

    # Contact pins (green for customers, orange for leads)
    enriched = contacts_with_coords(contacts, cache_dir)
    for c in enriched:
        role  = (c.get("role") or "contact").lower()
        color = "#27ae60" if "customer" in role else "#e67e22" if "lead" in role else "#8e44ad"
        name  = c.get("name") or "Contact"
        addr  = c.get("address") or ""
        phone = c.get("phone") or ""
        popup = f"<b>{name}</b>" + (f"<br>{addr}" if addr else "") + (f"<br>{phone}" if phone else "")
        markers.append({
            "lat": c["_lat"], "lon": c["_lon"],
            "label": name, "popup": popup, "color": color,
        })

    if not markers:
        return ("<p style='color:#888;padding:20px'>No contacts with addresses found. "
                "Add addresses to your contacts and they'll appear here.</p>")

    legend = """
<div style='font-size:12px;color:#555;margin-top:8px;display:flex;gap:16px;flex-wrap:wrap'>
  <span><span style='display:inline-block;width:10px;height:10px;background:#1a5276;border-radius:50%;margin-right:4px'></span>Your business</span>
  <span><span style='display:inline-block;width:10px;height:10px;background:#27ae60;border-radius:50%;margin-right:4px'></span>Customers</span>
  <span><span style='display:inline-block;width:10px;height:10px;background:#e67e22;border-radius:50%;margin-right:4px'></span>Leads</span>
  <span><span style='display:inline-block;width:10px;height:10px;background:#8e44ad;border-radius:50%;margin-right:4px'></span>Contacts</span>
</div>"""

    map_html = build_map_html(markers, height="480px",
                               title=f"{biz_name} — Contact Map")
    return map_html + legend
