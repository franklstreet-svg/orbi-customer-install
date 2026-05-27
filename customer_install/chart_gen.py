"""
chart_gen — Tier-2 feature: turn owner data (typed, JSON, or pulled from an
xlsx) into a clean branded chart PNG.

Pipeline mirrors image_gen / doc_convert:

    owner request / xlsx
      → (optional) parse_chart_request — LLM extracts kind + labels + values
      → generate_chart(...) — matplotlib renders a 1200x800 PNG
      → save_chart_to_workspace(...) — drops it into ~/Orbi/charts/ so it
        shows up in the Files tab on the next scan

Design notes
------------
* matplotlib is HEAVY at import time (~0.4–1.0s on a small box), so EVERY
  matplotlib import is lazy — wrapped inside the function that needs it.
  Boot time of orbi.py is unaffected.
* matplotlib.use("Agg") is called BEFORE pyplot is imported, every time,
  because Agg is the only backend guaranteed to work headless. Calling
  matplotlib.use() again after pyplot is imported is a no-op (or a warning),
  so doing it once per call is safe.
* LLM parsing is best-effort. If the brain / HF / local stack is all down,
  we fall back to a pure-regex parser that handles "Label NUMBER" lists.
  The owner ALWAYS gets a chart out of any reasonable phrasing.
* Brand colors are baked in as constants. "modern" style uses the violet/blue
  Orbi pair; "minimal" is greyscale-ish for printouts.
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("orbi.chart_gen")

# ---------------------------------------------------------------------------
# Brand
# ---------------------------------------------------------------------------

PRIMARY = "#4f8cff"   # Orbi blue
ACCENT  = "#8b5cf6"   # Orbi violet
BG      = "#0b0f1a"   # near-black slate
FG      = "#eaf0ff"   # off-white text

# Pleasing 6+ color cycle for pies / multi-series. Derived from PRIMARY/ACCENT
# by sliding hue around the wheel between them.
PIE_PALETTE = [
    "#4f8cff",   # blue   (PRIMARY)
    "#8b5cf6",   # violet (ACCENT)
    "#22d3ee",   # cyan
    "#f472b6",   # pink
    "#34d399",   # green
    "#fbbf24",   # amber
    "#fb7185",   # rose
    "#a78bfa",   # lighter violet
]

# Minimal style — grayscale-ish, for printable reports
MINIMAL_PALETTE = [
    "#1f2937", "#4b5563", "#6b7280", "#9ca3af",
    "#d1d5db", "#374151", "#111827", "#e5e7eb",
]

VALID_KINDS = ("bar", "line", "pie", "scatter", "horizontal_bar")
DEFAULT_KIND = "bar"

CHART_W_PX = 1200
CHART_H_PX = 800
CHART_DPI  = 100   # 1200/100 = 12in, 800/100 = 8in


# ---------------------------------------------------------------------------
# Public: generate_chart
# ---------------------------------------------------------------------------


def generate_chart(config: dict, *, title: str, kind: str, data: dict,
                   style: str = "modern") -> bytes:
    """Render a real PNG using matplotlib. Returns raw PNG bytes.

    kind:  "bar" | "line" | "pie" | "scatter" | "horizontal_bar"
    data:
        single-series:  {"labels": [...], "values": [...]}
        multi-series:   {"labels": [...], "series": [
                            {"name": "Sales", "values": [...]}, ...]}
        scatter:        {"x": [...], "y": [...]}
    style: "modern" (default) | "minimal"
    """
    kind = (kind or DEFAULT_KIND).lower().strip()
    if kind not in VALID_KINDS:
        log.info("unknown kind=%r, defaulting to %s", kind, DEFAULT_KIND)
        kind = DEFAULT_KIND
    style = (style or "modern").lower().strip()
    if style not in ("modern", "minimal"):
        style = "modern"

    # Lazy: matplotlib is expensive to import, only do it on demand.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # ── Theme palette ────────────────────────────────────────────────────
    if style == "modern":
        bg_color   = BG
        fg_color   = FG
        grid_color = "#1e293b"
        palette    = PIE_PALETTE
        edge       = "#1e293b"
    else:  # minimal
        bg_color   = "#ffffff"
        fg_color   = "#111827"
        grid_color = "#e5e7eb"
        palette    = MINIMAL_PALETTE
        edge       = "#9ca3af"

    fig_w_in = CHART_W_PX / CHART_DPI
    fig_h_in = CHART_H_PX / CHART_DPI

    fig, ax = plt.subplots(figsize=(fig_w_in, fig_h_in), dpi=CHART_DPI)
    fig.patch.set_facecolor(bg_color)
    ax.set_facecolor(bg_color)

    # Axis cosmetics — light enough to read on the BG, never overpowering
    for spine in ax.spines.values():
        spine.set_color(grid_color)
    ax.tick_params(colors=fg_color, labelsize=11)
    ax.title.set_color(fg_color)
    ax.xaxis.label.set_color(fg_color)
    ax.yaxis.label.set_color(fg_color)

    try:
        if kind == "bar":
            _render_bar(ax, data, palette, edge, horizontal=False)
        elif kind == "horizontal_bar":
            _render_bar(ax, data, palette, edge, horizontal=True)
        elif kind == "line":
            _render_line(ax, data, palette)
        elif kind == "pie":
            _render_pie(ax, data, palette, fg_color, edge)
        elif kind == "scatter":
            _render_scatter(ax, data, palette)
    except Exception as exc:
        plt.close(fig)
        log.exception("chart render crashed: %s", exc)
        raise

    # Title — always centered, always above everything else
    if title:
        ax.set_title(title, fontsize=18, color=fg_color, pad=18, weight="bold")

    # Grid for axis-based charts (not pie)
    if kind != "pie":
        ax.grid(True, color=grid_color, linewidth=0.6, alpha=0.6, zorder=0)
        ax.set_axisbelow(True)

    # Legend if multi-series — matplotlib adds it automatically if labels set
    if _is_multi_series(data) and kind in ("bar", "line", "horizontal_bar"):
        leg = ax.legend(loc="best", frameon=False, fontsize=11,
                        labelcolor=fg_color)
        if leg is not None:
            for txt in leg.get_texts():
                txt.set_color(fg_color)

    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=CHART_DPI,
                facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    png = buf.getvalue()
    log.info("chart rendered: kind=%s style=%s bytes=%d", kind, style, len(png))
    return png


# ---------------------------------------------------------------------------
# Renderers per kind (kept private, called from generate_chart)
# ---------------------------------------------------------------------------


def _is_multi_series(data: dict) -> bool:
    return isinstance(data, dict) and isinstance(data.get("series"), list) \
        and len(data.get("series") or []) > 0


def _render_bar(ax, data: dict, palette: list[str], edge: str,
                horizontal: bool) -> None:
    labels = list(data.get("labels") or [])
    if _is_multi_series(data):
        series = data["series"]
        n_groups = len(labels)
        n_series = len(series)
        # Group bar positions
        import numpy as np
        x = np.arange(n_groups)
        width = 0.8 / max(1, n_series)
        for i, s in enumerate(series):
            values = list(s.get("values") or [])
            name = s.get("name") or f"Series {i+1}"
            color = palette[i % len(palette)]
            offsets = x - 0.4 + width * (i + 0.5)
            if horizontal:
                ax.barh(offsets, values, height=width, color=color,
                        edgecolor=edge, linewidth=0.5, label=name)
            else:
                ax.bar(offsets, values, width=width, color=color,
                       edgecolor=edge, linewidth=0.5, label=name)
        if horizontal:
            ax.set_yticks(x)
            ax.set_yticklabels(labels)
        else:
            ax.set_xticks(x)
            ax.set_xticklabels(labels, rotation=0 if max(len(s) for s in labels) < 8 else 30,
                               ha="right" if max(len(s) for s in labels) >= 8 else "center")
    else:
        values = list(data.get("values") or [])
        if not values and labels:
            values = [0] * len(labels)
        # Single series — alternate brand colors for visual rhythm
        colors = [palette[i % len(palette)] for i in range(len(values))]
        if horizontal:
            ax.barh(labels, values, color=colors, edgecolor=edge, linewidth=0.5)
        else:
            ax.bar(labels, values, color=colors, edgecolor=edge, linewidth=0.5)
            if labels and max(len(str(l)) for l in labels) >= 8:
                for tick in ax.get_xticklabels():
                    tick.set_rotation(30)
                    tick.set_ha("right")
    ax.set_xlabel("Category" if not horizontal else "Value")
    ax.set_ylabel("Value"    if not horizontal else "Category")


def _render_line(ax, data: dict, palette: list[str]) -> None:
    labels = list(data.get("labels") or [])
    if _is_multi_series(data):
        for i, s in enumerate(data["series"]):
            values = list(s.get("values") or [])
            name   = s.get("name") or f"Series {i+1}"
            color  = palette[i % len(palette)]
            ax.plot(labels, values, marker="o", linewidth=2.4,
                    color=color, label=name, markersize=6)
    else:
        values = list(data.get("values") or [])
        ax.plot(labels, values, marker="o", linewidth=2.6,
                color=palette[0], markersize=7)
        # Subtle fill under the line for visual weight
        try:
            ax.fill_between(range(len(labels)), values, color=palette[0], alpha=0.15)
        except Exception:    # noqa: BLE001 — non-numeric labels are fine
            pass
    if labels and max(len(str(l)) for l in labels) >= 8:
        for tick in ax.get_xticklabels():
            tick.set_rotation(30)
            tick.set_ha("right")
    ax.set_xlabel("Category")
    ax.set_ylabel("Value")


def _render_pie(ax, data: dict, palette: list[str], fg_color: str, edge: str) -> None:
    labels = list(data.get("labels") or [])
    values = list(data.get("values") or [])
    if not values:
        # Last-ditch: try first series
        if _is_multi_series(data):
            values = list(data["series"][0].get("values") or [])
    # Filter out non-numeric / negative just in case
    clean_labels, clean_values = [], []
    for lab, v in zip(labels, values):
        try:
            n = float(v)
            if n < 0:
                continue
            clean_labels.append(str(lab))
            clean_values.append(n)
        except (TypeError, ValueError):
            continue
    if not clean_values:
        clean_labels = ["(no data)"]
        clean_values = [1]
    colors = [palette[i % len(palette)] for i in range(len(clean_values))]
    wedges, texts, autotexts = ax.pie(
        clean_values,
        labels=clean_labels,
        colors=colors,
        autopct="%1.1f%%",
        startangle=90,
        wedgeprops={"edgecolor": edge, "linewidth": 1.2},
        textprops={"color": fg_color, "fontsize": 11},
    )
    for at in autotexts:
        at.set_color("#ffffff")
        at.set_fontsize(10)
        at.set_weight("bold")
    ax.set_aspect("equal")


def _render_scatter(ax, data: dict, palette: list[str]) -> None:
    xs = list(data.get("x") or [])
    ys = list(data.get("y") or [])
    if not xs and "labels" in data:    # tolerate {labels,values} too
        xs = list(range(len(data.get("values") or [])))
        ys = list(data.get("values") or [])
    ax.scatter(xs, ys, c=palette[0], edgecolor=palette[1],
               s=80, alpha=0.85, linewidths=1.2)
    ax.set_xlabel("X")
    ax.set_ylabel("Y")


# ---------------------------------------------------------------------------
# Public: parse_chart_request
# ---------------------------------------------------------------------------


_PARSE_SYSTEM = """You convert an owner's plain-English chart request into a strict JSON object.

OUTPUT EXACTLY ONE JSON OBJECT, NO PREAMBLE, NO BACKTICKS, NO COMMENTARY.

Shape:
{
  "title": "<short title for the chart>",
  "kind":  "bar" | "line" | "pie" | "scatter" | "horizontal_bar",
  "data":  { "labels": ["..."], "values": [<numbers>] }
}

Rules:
* If the owner doesn't say what kind, default to "bar".
* Numbers must be plain numbers — no $, no commas, no units.
* Labels must be short — single words or short phrases.
* If you cannot extract any data, still emit valid JSON with empty arrays.
* Never include trailing commas. Never wrap the JSON in markdown fences."""


def parse_chart_request(config: dict, natural_language: str) -> dict:
    """Turn "make me a bar chart of monthly revenue: Jan 1000, Feb 1500, Mar 2200"
    into {title, kind, data: {labels, values}}.

    Strategy:
      1. Ask the LLM (defensive JSON parse).
      2. If LLM blank or unreachable, fall through to a regex parser that
         handles "Label NUMBER" comma-separated lists.
    """
    text = (natural_language or "").strip()
    if not text:
        raise ValueError("empty chart request")

    # ── Step 1: try the LLM ─────────────────────────────────────────────
    parsed = _llm_parse(config, text)
    if parsed:
        return parsed

    # ── Step 2: regex-only fallback ─────────────────────────────────────
    log.info("LLM parse failed/empty, using regex fallback")
    return _regex_parse(text)


def _llm_parse(config: dict, text: str) -> dict | None:
    try:
        import llm_client
    except Exception as exc:    # noqa: BLE001
        log.warning("llm_client import failed: %s", exc)
        return None
    try:
        resp = llm_client.generate(
            config, _PARSE_SYSTEM,
            [{"role": "user", "content": text}],
        )
        raw = (resp.text or "").strip()
    except Exception as exc:    # noqa: BLE001
        log.warning("llm parse call failed: %s", exc)
        return None

    if not raw:
        return None

    # Defensive: try json.loads, then regex-extract the first { ... } block
    parsed = _try_json(raw)
    if parsed is None:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if m:
            parsed = _try_json(m.group(0))
    if parsed is None:
        return None

    return _normalize_parsed(parsed)


def _try_json(s: str) -> dict | None:
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        return None
    return None


def _normalize_parsed(p: dict) -> dict:
    """Make sure the result conforms to the public {title, kind, data} shape."""
    title = str(p.get("title") or "Chart").strip()[:120]
    kind  = str(p.get("kind")  or DEFAULT_KIND).lower().strip()
    if kind not in VALID_KINDS:
        kind = DEFAULT_KIND
    data = p.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    # Coerce values to numbers where possible
    if "values" in data:
        data["values"] = [_coerce_num(v) for v in (data.get("values") or [])]
    if "labels" in data:
        data["labels"] = [str(l) for l in (data.get("labels") or [])]
    return {"title": title, "kind": kind, "data": data}


def _coerce_num(v):
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(",", "").replace("$", "")
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return 0


# ── Regex fallback ─────────────────────────────────────────────────────

# "Jan 1000", "Mar: 2,200", "Q1=$15,000", "Apple - 42", "May  3.5k"
_PAIR_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 &/_'\.\-]{0,30}?)"     # label
    r"\s*[:=\-–—]?\s*"                            # optional separator
    r"\$?\s*"                                     # optional $
    r"(-?\d[\d,]*(?:\.\d+)?)"                     # number
    r"\s*([kKmM])?\b"                             # optional k/m suffix
)

_KIND_HINTS = (
    ("horizontal_bar", ("horizontal bar", "horizontal-bar", "h-bar", "hbar")),
    ("scatter",        ("scatter",)),
    ("pie",            ("pie",)),
    ("line",           ("line chart", "line graph", "trend",)),
    ("bar",            ("bar chart", "bar graph", "bars")),
)


def _regex_parse(text: str) -> dict:
    """Pure-regex fallback. Always returns SOMETHING."""
    # Kind detection
    lowered = text.lower()
    kind = DEFAULT_KIND
    for k, hints in _KIND_HINTS:
        if any(h in lowered for h in hints):
            kind = k
            break

    # Title: text up to the first ":" or "—" or end of first sentence
    title = "Chart"
    for sep in (":", "—", "-", "."):
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if 0 < len(head) <= 80:
                title = head
                break
    if title == "Chart":
        title = text.strip()[:80] or "Chart"

    # If there's a clear "intro: data" separator, drop the intro before
    # scanning for pairs — keeps "pie chart of expenses - Rent 3000, ..."
    # from gluing "expenses" onto the first label.
    scan_text = text
    for sep in (":", "—"):
        if sep in scan_text:
            scan_text = scan_text.split(sep, 1)[1]
            break
    else:
        # Single " - " also acts as a "here comes data" cue, but only if
        # there are multiple pairs (so we don't break "Rent-3000").
        m_sep = re.search(r"\s[-–]\s", scan_text)
        if m_sep and len(re.findall(r"\d", scan_text[m_sep.end():])) >= 2:
            scan_text = scan_text[m_sep.end():]

    # Pair extraction — scan the data portion. Trim leading noise words from
    # labels.
    labels: list[str] = []
    values: list[float | int] = []
    for m in _PAIR_RE.finditer(scan_text):
        label = m.group(1).strip(" \t,;-")
        num_s = m.group(2).replace(",", "")
        suffix = (m.group(3) or "").lower()
        label = _clean_label(label)
        if not label or _looks_like_noise(label):
            continue
        try:
            num = float(num_s)
        except ValueError:
            continue
        if suffix == "k":
            num *= 1_000
        elif suffix == "m":
            num *= 1_000_000
        # Cast to int if it's a whole number
        if num == int(num):
            num = int(num)
        labels.append(label)
        values.append(num)

    return {
        "title": title,
        "kind":  kind,
        "data":  {"labels": labels, "values": values},
    }


_NOISE_WORDS = {
    "make", "give", "show", "draw", "create", "build", "chart", "graph",
    "of", "the", "a", "an", "for", "me", "please", "with", "and",
    "bar", "line", "pie", "scatter", "horizontal",
    "monthly", "weekly", "daily", "yearly", "quarterly",
    "revenue", "sales", "data", "values", "title",
}


def _looks_like_noise(label: str) -> bool:
    """Filter out matches where the 'label' is actually leftover prose."""
    lab = label.strip().lower()
    if not lab:
        return True
    if lab in _NOISE_WORDS:
        return True
    # If every token is a noise word, drop it
    toks = re.split(r"\s+", lab)
    if all(t in _NOISE_WORDS for t in toks if t):
        return True
    return False


def _clean_label(label: str) -> str:
    """Strip leading noise tokens like 'pie chart of expenses - Rent' → 'Rent'.

    Walks left-to-right, dropping noise words until we hit a non-noise word,
    then keeps the rest.
    """
    label = label.strip(" \t,;-:")
    if not label:
        return label
    toks = re.split(r"\s+", label)
    # Find the first non-noise token, keep from there onward.
    for i, t in enumerate(toks):
        if t.lower() not in _NOISE_WORDS:
            return " ".join(toks[i:]).strip(" \t,;-:")
    return label


# ---------------------------------------------------------------------------
# Public: chart_from_xlsx
# ---------------------------------------------------------------------------


def chart_from_xlsx(config: dict, xlsx_path: Path, *, sheet: str | None = None,
                    x_col: int = 0, y_col: int = 1, kind: str = "bar",
                    title: str = "") -> bytes:
    """Read an Excel file and render a chart from two columns.

    If `sheet` is None, the active sheet is used. `x_col` / `y_col` are
    0-indexed column numbers. The first row is treated as headers (which
    become the axis labels) and is excluded from the data.
    """
    xlsx_path = Path(xlsx_path).expanduser()
    if not xlsx_path.exists():
        raise FileNotFoundError(str(xlsx_path))

    import openpyxl
    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True, read_only=True)
    try:
        ws = wb[sheet] if sheet else wb.active

        # Pull all rows once — read-only mode iterates a generator
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise ValueError(f"sheet {ws.title!r} is empty")

        header_row = rows[0]
        data_rows = rows[1:] if len(rows) > 1 else []

        # Resolve header names for axis labels (best-effort)
        def _header(idx: int, fallback: str) -> str:
            try:
                v = header_row[idx]
                if v not in (None, ""):
                    return str(v)
            except IndexError:
                pass
            return fallback

        x_header = _header(x_col, f"Column {x_col + 1}")
        y_header = _header(y_col, f"Column {y_col + 1}")

        labels: list[str] = []
        values: list[float | int] = []
        for r in data_rows:
            try:
                lab = r[x_col]
                val = r[y_col]
            except IndexError:
                continue
            if lab is None and val is None:
                continue
            # Coerce value to number; skip rows where value isn't numeric
            try:
                num = float(val)
                if num == int(num):
                    num = int(num)
            except (TypeError, ValueError):
                continue
            labels.append("" if lab is None else str(lab))
            values.append(num)
    finally:
        wb.close()

    if not values:
        raise ValueError(
            f"no numeric data in column {y_col} of sheet "
            f"{(sheet or 'active')!r}"
        )

    if not title:
        title = f"{y_header} by {x_header}"

    return generate_chart(
        config,
        title=title,
        kind=kind,
        data={"labels": labels, "values": values},
    )


# ---------------------------------------------------------------------------
# Public: save_chart_to_workspace
# ---------------------------------------------------------------------------


def save_chart_to_workspace(png_bytes: bytes, title: str,
                            workspace_dir: Path) -> Path:
    """Write the PNG to ~/Orbi/charts/. Filename pattern:
        chart_<slug>_<YYYY-MM-DD_HHMMSS>.png
    Returns the absolute path written.
    """
    workspace_dir = Path(workspace_dir).expanduser()
    charts_dir = workspace_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    slug = _slugify(title) or "chart"
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    fname = f"chart_{slug}_{stamp}.png"
    path = charts_dir / fname

    tmp = path.with_suffix(".png.tmp")
    tmp.write_bytes(png_bytes)
    tmp.replace(path)

    log.info("saved chart (%d bytes) -> %s", len(png_bytes), path)
    return path


def _slugify(s: str) -> str:
    """Filename-safe slug, lowercase, hyphen-separated, max 48 chars."""
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:48]


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# All routes are owner-authed (cookie). `config` comes from the logged-in owner.
# workspace_dir = modules.workspace.workspace_path(config) — defaults to ~/Orbi/.
#
#   POST /api/owner/chart/from_data
#       body:   { "title": "...", "kind": "bar"|"line"|"pie"|"scatter"|"horizontal_bar",
#                 "data":  { ... see generate_chart() docstring ... },
#                 "style": "modern"|"minimal"   (optional, default "modern") }
#       returns: { "filename": "...", "download_url": "/api/owner/workspace/files/..." }
#
#       implementation:
#           from modules.workspace import workspace_path
#           import chart_gen
#           png  = chart_gen.generate_chart(config,
#                       title=body["title"], kind=body["kind"],
#                       data=body["data"], style=body.get("style", "modern"))
#           path = chart_gen.save_chart_to_workspace(png, body["title"],
#                       workspace_path(config))
#           return { "filename":     path.name,
#                    "download_url": "/api/owner/workspace/files/" + path.name,
#                    "bytes":        len(png) }
#
#   POST /api/owner/chart/from_request
#       body:   { "request": "make a bar chart of monthly revenue: Jan 1000, Feb 1500, Mar 2200" }
#       returns: same shape as /from_data
#
#       implementation:
#           parsed = chart_gen.parse_chart_request(config, body["request"])
#           png    = chart_gen.generate_chart(config, **parsed)
#           path   = chart_gen.save_chart_to_workspace(png, parsed["title"],
#                                                     workspace_path(config))
#           return { "filename":     path.name,
#                    "download_url": "/api/owner/workspace/files/" + path.name,
#                    "kind":         parsed["kind"],
#                    "title":        parsed["title"],
#                    "bytes":        len(png) }
#
#   POST /api/owner/chart/from_xlsx
#       body:   { "filename": "sales_q1.xlsx",   (file already in workspace)
#                 "sheet":    "Q1"               (optional),
#                 "x_col":    0                  (optional, default 0),
#                 "y_col":    1                  (optional, default 1),
#                 "kind":     "bar"              (optional, default "bar"),
#                 "title":    "..."              (optional) }
#       returns: same shape as /from_data
#
#       implementation:
#           ws   = workspace_path(config)
#           src  = ws / body["filename"]
#           png  = chart_gen.chart_from_xlsx(config, src,
#                       sheet=body.get("sheet"),
#                       x_col=int(body.get("x_col", 0)),
#                       y_col=int(body.get("y_col", 1)),
#                       kind=body.get("kind", "bar"),
#                       title=body.get("title", ""))
#           path = chart_gen.save_chart_to_workspace(png,
#                       body.get("title") or body["filename"], ws)
#           return { "filename":     path.name,
#                    "download_url": "/api/owner/workspace/files/" + path.name,
#                    "bytes":        len(png) }
#
# ---------------------------------------------------------------------------
