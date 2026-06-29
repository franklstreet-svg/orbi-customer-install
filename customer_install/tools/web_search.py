"""
Web search tool — DuckDuckGo HTML scrape (no API key required, free).

Used by Orbi when the owner asks about things outside their data:
  - "What's the weather in Reno right now?"
  - "What's today's news on X?"
  - "How do I do Y in Quickbooks?"
  - "What's a good price for Z?"

Falls back to Wikipedia API if DuckDuckGo blocks us. Both are free and
zero-config.

Public functions:
  needs_web_search(query)  → bool   heuristic: should we even try?
  search(query, limit=5)   → list   [{title, url, snippet}]
  context_block(query)     → str    prompt-ready search results
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import urllib.request

log = logging.getLogger("orbi.tools.web_search")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Heuristics for "this query benefits from fresh web info"
_FRESH_KEYWORDS = (
    "today", "tonight", "tomorrow", "this week", "current", "right now",
    "latest", "news", "weather", "forecast", "price", "stock", "score",
    "open today", "happening", "recent", "yesterday", "trending",
    "this year", "this month",
)
_KNOWLEDGE_KEYWORDS = (
    "how do i", "how to", "what is", "what's a", "what are", "who is",
    "who was", "when did", "where is", "why does", "compare",
)


def needs_web_search(query: str) -> bool:
    """Cheap check — should we even try the web for this?"""
    import re as _re
    q = (query or "").lower().strip()
    if not q:
        return False
    # Explicit "search the web / look this up" commands always get a real search
    if _re.match(r"(?:search\s+(?:the\s+)?(?:web|online|internet)\s+for\b|"
                 r"(?:look|find)\s+(?:it\s+)?(?:up|online)\b|"
                 r"google\b|search\s+for\b)", q):
        return True
    if any(k in q for k in _FRESH_KEYWORDS):
        return True
    # Knowledge-shaped questions get the web only if they don't seem to be
    # about the business itself
    if any(q.startswith(k) for k in _KNOWLEDGE_KEYWORDS):
        return True
    return False


# ---------------------------------------------------------------------------
# DuckDuckGo HTML scrape
# ---------------------------------------------------------------------------

def _ddg_search(query: str, limit: int = 5) -> list[dict]:
    url = "https://html.duckduckgo.com/html/"
    data = urllib.parse.urlencode({"q": query, "kl": "us-en"}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    return _parse_ddg_html(body, limit)


def _parse_ddg_html(html_text: str, limit: int) -> list[dict]:
    # DuckDuckGo HTML format — result rows look like:
    # <a class="result__a" href="...">TITLE</a>
    # <a class="result__snippet">SNIPPET</a>
    results = []
    # Use non-greedy patterns
    link_pat    = re.compile(r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.+?)</a>', re.DOTALL)
    snippet_pat = re.compile(r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.+?)</a>', re.DOTALL)

    titles = link_pat.findall(html_text)
    snippets = snippet_pat.findall(html_text)

    for i, (raw_url, raw_title) in enumerate(titles[:limit]):
        snippet = snippets[i] if i < len(snippets) else ""
        # DDG wraps URLs in redirect — try to extract the real one
        real_url = raw_url
        m = re.search(r'uddg=([^&]+)', raw_url)
        if m:
            real_url = urllib.parse.unquote(m.group(1))
        results.append({
            "title":   _clean_html(raw_title),
            "url":     real_url,
            "snippet": _clean_html(snippet),
        })
    return results


def _clean_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Wikipedia fallback (always works, no anti-scrape)
# ---------------------------------------------------------------------------

def _wikipedia_search(query: str, limit: int = 3) -> list[dict]:
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query",
        "list": "search",
        "srsearch": query,
        "format": "json",
        "srlimit": limit,
    })
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for r in (data.get("query", {}).get("search", []) or [])[:limit]:
        title = r.get("title", "")
        snippet = _clean_html(r.get("snippet", ""))
        out.append({
            "title": title,
            "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
            "snippet": snippet,
            "source": "wikipedia",
        })
    return out


# ---------------------------------------------------------------------------
# Weather (Open-Meteo, free, no key)
# ---------------------------------------------------------------------------

def _weather(query: str) -> list[dict]:
    """If query mentions weather + a place, hit Open-Meteo for actual forecast."""
    q = query.lower()
    if "weather" not in q and "forecast" not in q and "temperature" not in q:
        return []
    # Try to extract a place name. The place is whatever comes after
    # "weather in / for / at" up to a stop word (today, tomorrow, now, right now)
    # or end-of-string / punctuation.
    place_match = re.search(
        r'(?:weather|forecast|temperature)\s+(?:in|for|at)\s+'
        r'([A-Za-z][A-Za-z\s,\.]*?)'           # the place — letters, spaces, commas
        r'(?:\s+(?:right\s+now|today|tomorrow|tonight|this\s+\w+|now|currently))?'
        r'\s*[?.!]?\s*$',                       # optional trailing punctuation
        query, re.I
    )
    if not place_match:
        return []
    place = place_match.group(1).strip().rstrip("?.,!").strip()
    if not place or len(place) < 2:
        return []
    # Geocode
    geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({
        "name": place, "count": 1, "language": "en", "format": "json"
    })
    try:
        with urllib.request.urlopen(geo_url, timeout=5) as r:
            geo = json.loads(r.read())
        if not geo.get("results"):
            return []
        loc = geo["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        name = f"{loc.get('name')}, {loc.get('admin1', '')}".strip(", ")
    except Exception as e:
        log.warning(f"geocode failed: {e}")
        return []
    # Forecast
    fc_url = ("https://api.open-meteo.com/v1/forecast?"
              f"latitude={lat}&longitude={lon}&current=temperature_2m,weather_code,"
              f"wind_speed_10m,relative_humidity_2m&temperature_unit=fahrenheit"
              f"&wind_speed_unit=mph&timezone=auto")
    try:
        with urllib.request.urlopen(fc_url, timeout=5) as r:
            fc = json.loads(r.read())
        cur = fc.get("current", {})
        temp = cur.get("temperature_2m")
        humid = cur.get("relative_humidity_2m")
        wind = cur.get("wind_speed_10m")
        code = cur.get("weather_code")
        desc = _wmo_code(code)
        snippet = (f"Currently {temp}°F and {desc.lower()} in {name}, "
                   f"with {humid}% humidity and wind {wind} mph.")
        return [{
            "title": f"Current weather in {name}",
            "url": f"https://open-meteo.com/?latitude={lat}&longitude={lon}",
            "snippet": snippet,
            "source": "open-meteo",
        }]
    except Exception as e:
        log.warning(f"forecast failed: {e}")
        return []


_WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Rain showers", 81: "Heavy rain showers", 82: "Violent rain showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Severe thunderstorm with hail",
}

def _wmo_code(code) -> str:
    try:
        return _WMO.get(int(code), "weather")
    except Exception:
        return "weather"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search(query: str, limit: int = 5) -> list[dict]:
    """Web search with weather + DuckDuckGo + Wikipedia fallbacks."""
    out = []
    # 1. Weather gets a special quick path (always tried first if relevant)
    try:
        weather = _weather(query)
        if weather:
            out.extend(weather)
    except Exception as e:
        log.warning(f"weather lookup failed: {e}")
    # 2. DuckDuckGo
    if len(out) < limit:
        try:
            ddg = _ddg_search(query, limit - len(out))
            out.extend(ddg)
        except Exception as e:
            log.warning(f"ddg search failed: {e}")
    # 3. Wikipedia fallback if we still have nothing
    if not out:
        try:
            out.extend(_wikipedia_search(query, limit))
        except Exception as e:
            log.warning(f"wikipedia search failed: {e}")
    return out[:limit]


def context_block(query: str, max_chars: int = 900) -> str:
    """Build a prompt-ready block from search results. Empty string on failure."""
    try:
        results = search(query, limit=4)
    except Exception as e:
        log.warning(f"web search failed: {e}")
        return ""
    if not results:
        return ""
    lines = ["WEB SEARCH RESULTS (cite the source when you quote these):"]
    for r in results:
        line = f"\n[{r['title']}]({r['url']})\n{r['snippet']}"
        lines.append(line)
    out = "".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars - 3] + "..."
    return out
