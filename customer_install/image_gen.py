"""
image_gen — Tier-2 feature: generate social-media-ready images for the owner.

Two paths:
    (a) HuggingFace Inference API on a free fast model (FLUX.1-schnell).
        Requires CONFIG.huggingface.api_key. POST {"inputs": prompt} to
        https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell
        and the body is raw PNG bytes.
    (b) PIL-based templated fallback. Pure stdlib + Pillow. No network,
        no API key, no model — just a clean gradient with the prompt text
        rendered on top. Always works, so the owner is never left empty-handed
        if HF is down, the API key is missing, or the network is offline.

generate() tries (a) first if the key is present, falls through to (b) on any
failure. The fallback is intentional design — owners ALWAYS get an image.

`save_to_workspace()` drops the PNG into the owner's workspace folder
(~/Orbi/ by default — see modules/workspace.py) so the Files tab picks it up
automatically on the next scan, no separate index touch needed.

CONVENTIONS
-----------
* logging.getLogger("orbi.image_gen")
* PIL is required (the icon generator already depends on it).
* HuggingFace stack is lazy-imported (we use stdlib urllib so there's no extra
  dep, but the import sits inside the function regardless).
* All output is PNG bytes.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

log = logging.getLogger("orbi.image_gen")

# ---------------------------------------------------------------------------
# Size presets
# ---------------------------------------------------------------------------

SIZES = {
    # Platform-specific (use these when the owner names the platform)
    "instagram_square":    (1080, 1080),
    "instagram_story":     (1080, 1920),
    "instagram_portrait":  (1080, 1350),
    "facebook_post":       (1200, 1200),
    "facebook_cover":      (1640,  856),
    "twitter_post":        (1600,  900),
    "linkedin_post":       (1200, 1200),
    "tiktok_post":         (1080, 1920),
    "youtube_thumbnail":   (1280,  720),
    "pinterest_pin":       (1000, 1500),
    # Print / general
    "flyer_portrait":      (1224, 1584),    # 8.5x11 @ 144dpi
    "poster_portrait":     (1080, 1620),
    "business_card":       (1050,  600),    # 3.5x2 @ 300dpi (resampled down)
    # Generic shape aliases
    "square":              (1080, 1080),
    "wide":                (1600,  900),
    "tall":                (1080, 1920),
    "banner":              (1500,  500),
    # Legacy aliases kept for backward compat
    "social_post":         (1080, 1080),
    "flyer":               (1224, 1584),
}
DEFAULT_KIND = "instagram_square"

# Default accent — Orbi violet. Owners can override per-call.
ACCENT_DEFAULT = (139, 92, 246)

# Candidate fonts in priority order. First one that exists wins.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",     # macOS
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",      # Windows
    "C:/Windows/Fonts/arial.ttf",
)

# HuggingFace endpoint for the free-tier fast text-to-image model.
# NOTE 2026-05-28: HF deprecated api-inference.huggingface.co and migrated
# image gen behind the paid Inference Providers tier on router.huggingface.co.
# The old URL now returns NXDOMAIN. We keep the HF code path for owners with
# a paid HF Pro account, but it's no longer Tier 1.
HF_MODEL = "black-forest-labs/FLUX.1-schnell"
HF_URL   = f"https://router.huggingface.co/hf-inference/models/{HF_MODEL}"
HF_TIMEOUT_SECONDS = 60

# Pollinations.ai — keyless, free, no signup. Generates from a plain URL,
# returns PNG bytes. Default tier 1 since it Just Works without any setup
# the customer has to do. Their FLUX model is the same family as HF's.
POLLINATIONS_BASE = "https://image.pollinations.ai/prompt/"
POLLINATIONS_TIMEOUT_SECONDS = 45


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate(config: dict, prompt: str, kind: str = DEFAULT_KIND) -> bytes:
    """
    Generate a PNG for `prompt` at the size matching `kind`. Tries
    HuggingFace first if a key is configured, falls back to the PIL template
    on any failure. Returns raw PNG bytes.

    Raises RuntimeError ONLY if even the PIL fallback fails (which means PIL
    itself is broken — should never happen on a real install).
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    if kind not in SIZES:
        log.info("unknown kind=%r, defaulting to %s", kind, DEFAULT_KIND)
        kind = DEFAULT_KIND

    # ── Path A: Pollinations.ai (keyless, free, default) ─────────────────
    img_cfg = (config or {}).get("image_gen") or {}
    if img_cfg.get("pollinations_enabled", True):
        try:
            png = _generate_pollinations(prompt, kind, img_cfg)
            if png:
                log.info("pollinations generated %d bytes for kind=%s", len(png), kind)
                return png
        except Exception as exc:    # noqa: BLE001 — fall through to next tier
            log.warning("pollinations generation failed (%s), trying next tier", exc)

    # ── Path B: HuggingFace (requires HF Pro account on the new Router) ──
    hf_cfg = (config or {}).get("huggingface") or {}
    if hf_cfg.get("api_key"):
        try:
            png = _generate_huggingface(hf_cfg, prompt, kind)
            if png:
                log.info("hf generated %d bytes for kind=%s", len(png), kind)
                return png
        except Exception as exc:    # noqa: BLE001 — fall through to template
            log.warning("hf generation failed (%s), falling back to template",
                        exc)

    # ── Path C: PIL template fallback ─────────────────────────────────────
    try:
        png = templated_post(prompt, kind)
        log.info("template generated %d bytes for kind=%s", len(png), kind)
        return png
    except Exception as exc:
        log.exception("template fallback crashed — image_gen is broken")
        raise RuntimeError(f"image_gen totally failed: {exc}") from exc


def overlay_caption(png_bytes: bytes, caption: str,
                    position: str = "bottom",
                    accent: tuple[int, int, int] = ACCENT_DEFAULT) -> bytes:
    """
    Lay clean readable caption text on top of an existing image.

    FLUX (and most image models) generate garbled text when asked to
    render words inside the image. So instead we have FLUX produce a
    clean background, then PIL stamps the caption on top using a real
    system font. Result: marketing-grade composites with sharp, correct
    text.

    `position` ∈ {"bottom", "top", "center"} — where the caption band
    sits on the image.
    """
    from PIL import Image, ImageDraw, ImageFont

    if not caption:
        return png_bytes
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    W, H = img.size
    draw = ImageDraw.Draw(img, "RGBA")

    # Sizing scales with width so it looks right on square OR portrait OR wide
    font_size = max(28, int(W * 0.055))
    font = _load_font(font_size, bold=True)
    max_text_width = int(W * 0.86)
    lines = _wrap(draw, caption, font, max_text_width)
    line_h = _line_height(draw, "Ag", font)
    block_h = line_h * len(lines)
    pad_v = max(20, int(font_size * 0.6))
    pad_h = max(24, int(W * 0.05))
    band_h = block_h + pad_v * 2

    # Y position of the caption block
    if position == "top":
        band_y0 = 0
    elif position == "center":
        band_y0 = (H - band_h) // 2
    else:  # bottom
        band_y0 = H - band_h

    # Semi-transparent dark band for legibility over busy backgrounds
    draw.rectangle((0, band_y0, W, band_y0 + band_h), fill=(0, 0, 0, 160))
    # Accent stripe at the band edge for brand cue
    stripe_h = max(4, int(font_size * 0.12))
    if position == "top":
        draw.rectangle((0, band_y0 + band_h - stripe_h, W, band_y0 + band_h), fill=accent)
    else:
        draw.rectangle((0, band_y0, W, band_y0 + stripe_h), fill=accent)

    # Draw each line, centered, with a tight shadow for crispness
    y = band_y0 + pad_v
    for ln in lines:
        w = _text_width(draw, ln, font)
        x = (W - w) // 2
        draw.text((x + 2, y + 2), ln, fill=(0, 0, 0, 200), font=font)
        draw.text((x, y), ln, fill=(255, 255, 255), font=font)
        y += line_h

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def save_to_workspace(image_bytes: bytes, prompt: str,
                      workspace_dir: Path) -> Path:
    """
    Write the PNG to the owner's workspace folder under an /images/
    subdirectory, with a stable, sortable filename that's safe for the
    workspace scanner to pick up.

    Filename: social_<YYYY-MM-DD>_<HHMMSS>_<short_hash>.png
    Returns the absolute path written.
    """
    workspace_dir = Path(workspace_dir).expanduser()
    images_dir = workspace_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    short = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
    fname = (f"social_{now.strftime('%Y-%m-%d')}"
             f"_{now.strftime('%H%M%S')}_{short}.png")
    path = images_dir / fname

    # Atomic-ish write: write to .tmp then rename
    tmp = path.with_suffix(".png.tmp")
    tmp.write_bytes(image_bytes)
    tmp.replace(path)

    log.info("saved image (%d bytes) -> %s", len(image_bytes), path)
    return path


def templated_post(text: str, kind: str = DEFAULT_KIND,
                   accent_color: tuple[int, int, int] = ACCENT_DEFAULT) -> bytes:
    """
    Pure-PIL fallback. Gradient background, prompt text rendered nicely.
    No AI, no network — always works.

    Returns PNG bytes.
    """
    from PIL import Image, ImageDraw, ImageFont

    width, height = SIZES.get(kind, SIZES[DEFAULT_KIND])
    img = Image.new("RGB", (width, height), (24, 24, 32))

    # ── Vertical gradient: dark slate -> accent ──────────────────────────
    _draw_gradient(img, top=(24, 24, 32), bottom=accent_color)

    draw = ImageDraw.Draw(img)

    # ── Soft corner glow / vignette for depth ────────────────────────────
    _draw_corner_glow(img, accent_color)

    # ── Text rendering ───────────────────────────────────────────────────
    # Headline = first line / sentence of the prompt; body = the rest.
    headline, body = _split_text(text, max_headline=64)

    # Sizing — scale font size with the canvas width so all 3 kinds
    # look proportional, not "tiny on flyer / huge on banner".
    headline_size = max(36, int(width * 0.060))
    body_size     = max(20, int(width * 0.028))
    accent_size   = max(16, int(width * 0.018))

    headline_font = _load_font(headline_size, bold=True)
    body_font     = _load_font(body_size, bold=False)
    accent_font   = _load_font(accent_size, bold=True)

    # Wrap headline + body into multiple lines that fit the canvas
    max_text_width = int(width * 0.85)
    headline_lines = _wrap(draw, headline, headline_font, max_text_width)
    body_lines     = _wrap(draw, body,     body_font,     max_text_width) if body else []

    # Compute total block height to vertically center
    line_gap_h = int(headline_size * 0.15)
    line_gap_b = int(body_size     * 0.25)
    block_h = 0
    for ln in headline_lines:
        block_h += _line_height(draw, ln, headline_font) + line_gap_h
    if body_lines:
        block_h += int(headline_size * 0.6)  # gap between headline and body
    for ln in body_lines:
        block_h += _line_height(draw, ln, body_font) + line_gap_b

    y = max(int(height * 0.10), (height - block_h) // 2)

    # Draw headline (white, with shadow for legibility)
    for ln in headline_lines:
        w = _text_width(draw, ln, headline_font)
        x = (width - w) // 2
        # Shadow
        draw.text((x + 2, y + 2), ln, fill=(0, 0, 0, 180), font=headline_font)
        # Foreground
        draw.text((x, y), ln, fill=(255, 255, 255), font=headline_font)
        y += _line_height(draw, ln, headline_font) + line_gap_h

    if body_lines:
        y += int(headline_size * 0.4)

    for ln in body_lines:
        w = _text_width(draw, ln, body_font)
        x = (width - w) // 2
        draw.text((x + 1, y + 1), ln, fill=(0, 0, 0, 180), font=body_font)
        draw.text((x, y), ln, fill=(235, 235, 240), font=body_font)
        y += _line_height(draw, ln, body_font) + line_gap_b

    # Accent footer band — small "ORBI" tag bottom-right so the owner can tell
    # their own posts at a glance. Keep it tasteful.
    tag = "Made with Orbi"
    tw = _text_width(draw, tag, accent_font)
    tx = width - tw - int(width * 0.04)
    ty = height - accent_size - int(height * 0.04)
    draw.text((tx, ty), tag, fill=(255, 255, 255, 200), font=accent_font)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pollinations.ai path (default — keyless, free)
# ---------------------------------------------------------------------------


def _generate_pollinations(prompt: str, kind: str, img_cfg: dict) -> bytes:
    """
    Generate a PNG via Pollinations.ai. No API key required. The whole
    request is a single GET against an encoded URL — they generate the image
    on their backend (FLUX family) and return raw PNG bytes.

    Free tier rate limit (anonymous) is generous enough for a single
    customer install — minutes-per-minute, not seconds. If you hit it
    you'll get a 429 and we'll fall through to the HF or PIL tier.
    """
    import urllib.parse

    width, height = SIZES[kind]
    model = (img_cfg.get("pollinations_model") or "flux").strip()
    timeout = int(img_cfg.get("pollinations_timeout_seconds", POLLINATIONS_TIMEOUT_SECONDS))

    # Pollinations puts the prompt in the URL path, not query string. Stable
    # seed gives reproducible images for the same prompt; we use a hash of
    # the prompt so re-running gives the same picture (useful for testing).
    seed = int(hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8], 16) % 100000
    encoded = urllib.parse.quote(prompt, safe="")
    url = (f"{POLLINATIONS_BASE}{encoded}"
           f"?width={width}&height={height}"
           f"&model={urllib.parse.quote(model)}"
           f"&seed={seed}"
           f"&nologo=true&enhance=true&safe=true")

    req = urllib.request.Request(url, method="GET", headers={
        "Accept":     "image/png, image/jpeg, image/*",
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    })

    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="ignore")
        except Exception:    # noqa: BLE001
            err_body = ""
        raise RuntimeError(f"pollinations HTTP {exc.code}: {err_body[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"pollinations network error: {exc}") from exc

    elapsed_ms = int((time.time() - start) * 1000)
    log.info("pollinations GET took %dms (%d bytes, ctype=%s)",
             elapsed_ms, len(payload), ctype)

    # They may return jpeg — accept that, convert to PNG so downstream is uniform
    if _looks_like_png(payload):
        return payload
    if _looks_like_jpeg(payload):
        return _jpeg_to_png(payload)
    snippet = payload[:300].decode("utf-8", errors="ignore")
    raise RuntimeError(f"pollinations returned non-image payload: {snippet!r}")


def _looks_like_jpeg(buf: bytes) -> bool:
    """JPEG magic = b'\\xff\\xd8\\xff' (3 bytes)."""
    return len(buf) >= 3 and buf[:3] == b"\xff\xd8\xff"


def _jpeg_to_png(jpeg_bytes: bytes) -> bytes:
    """Re-encode JPEG → PNG so save_to_workspace() always writes .png."""
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------------------
# HuggingFace path
# ---------------------------------------------------------------------------


def _generate_huggingface(hf_cfg: dict, prompt: str, kind: str) -> bytes:
    """
    POST the prompt to the HF FLUX.1-schnell endpoint, return PNG bytes.

    HF returns raw image bytes (Content-Type: image/png) on success, or a
    JSON error body on failure. We treat anything that isn't bytes >= ~1KB
    starting with the PNG magic as a failure and let the caller fall through
    to the template.
    """
    api_key = hf_cfg.get("api_key", "")
    if not api_key:
        return b""

    # Allow the owner to override the model via config — defaults to FLUX.1-schnell.
    model = (hf_cfg.get("image_model") or HF_MODEL).strip()
    url = f"https://api-inference.huggingface.co/models/{model}"

    width, height = SIZES[kind]

    body = {
        "inputs": prompt,
        "parameters": {
            "width":  width,
            "height": height,
            # FLUX.1-schnell is distilled for 1-4 step inference. 4 is the
            # sweet spot for quality at this resolution.
            "num_inference_steps": 4,
        },
        # HF returns a 503 with {"estimated_time": ...} while a model spins up
        # on a cold worker. Asking it to wait keeps us from getting a 503 on
        # first call of the day.
        "options": {"wait_for_model": True},
    }
    data = json.dumps(body).encode("utf-8")
    req  = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "image/png",
        # HF Router sits behind Cloudflare and 403s on python-urllib UA.
        "User-Agent":    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    })
    timeout = int(hf_cfg.get("image_timeout_seconds", HF_TIMEOUT_SECONDS))

    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="ignore")
        except Exception:    # noqa: BLE001
            err_body = ""
        raise RuntimeError(f"hf HTTP {exc.code}: {err_body[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"hf network error: {exc}") from exc

    elapsed_ms = int((time.time() - start) * 1000)
    log.info("hf POST %s took %dms (%d bytes, ctype=%s)",
             model, elapsed_ms, len(payload), ctype)

    # Validate it's actually PNG bytes — HF sometimes returns JSON-shaped
    # errors with a 200, especially when the model is still loading.
    if not _looks_like_png(payload):
        # Try to parse as a JSON error for the log
        snippet = payload[:300].decode("utf-8", errors="ignore")
        raise RuntimeError(f"hf returned non-PNG payload: {snippet!r}")

    return payload


def _looks_like_png(buf: bytes) -> bool:
    """PNG magic = b'\\x89PNG\\r\\n\\x1a\\n' (8 bytes)."""
    return len(buf) >= 8 and buf[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# PIL helpers (template path)
# ---------------------------------------------------------------------------


def _draw_gradient(img, top: tuple[int, int, int],
                   bottom: tuple[int, int, int]) -> None:
    """Top->bottom linear gradient. Mutates img in place."""
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))


def _draw_corner_glow(img, accent: tuple[int, int, int]) -> None:
    """Subtle radial highlight in the top-left to add depth. Cheap-ish —
    only draws ~6 concentric ellipses, not a full radial gradient."""
    from PIL import ImageDraw
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    cx, cy = int(w * 0.18), int(h * 0.18)
    r_max = int(min(w, h) * 0.55)
    steps = 8
    for i in range(steps, 0, -1):
        r = int(r_max * (i / steps))
        alpha = int(40 * (1 - (i / steps)))
        if alpha <= 0:
            continue
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(accent[0], accent[1], accent[2], alpha),
        )


def _load_font(size: int, bold: bool = True):
    """Try each candidate font path; fall back to PIL's bitmap default if
    none are available (which is fugly but means the call never fails)."""
    from PIL import ImageFont
    paths = list(FONT_CANDIDATES)
    if not bold:
        # Prefer the non-bold variant when available
        paths = [p.replace("-Bold", "").replace("Bold", "") for p in paths] + paths
    for p in paths:
        try:
            return ImageFont.truetype(p, size=size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _text_width(draw, text: str, font) -> int:
    try:
        # Pillow >= 9.2 — textbbox is the canonical API
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except Exception:    # noqa: BLE001 — very old Pillow
        try:
            return draw.textlength(text, font=font)
        except Exception:    # noqa: BLE001
            return len(text) * 8


def _line_height(draw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text or "M", font=font)
        return bbox[3] - bbox[1]
    except Exception:    # noqa: BLE001
        try:
            return getattr(font, "size", 16)
        except Exception:
            return 16


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    """Wrap text to fit within `max_width` pixels. Words longer than the
    line get hard-broken so we don't overflow."""
    if not text:
        return []
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip() if current else word
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
                current = word
            else:
                # Single word is wider than the line — chunk it.
                chunk = ""
                for ch in word:
                    if _text_width(draw, chunk + ch, font) <= max_width:
                        chunk += ch
                    else:
                        if chunk:
                            lines.append(chunk)
                        chunk = ch
                current = chunk
    if current:
        lines.append(current)
    return lines


def _split_text(text: str, max_headline: int = 64) -> tuple[str, str]:
    """Split a prompt into (headline, body). First sentence (or first line)
    becomes the headline; everything else is body. Headline is capped at
    `max_headline` chars to keep it punchy."""
    s = (text or "").strip()
    if not s:
        return "", ""
    # Prefer line breaks if the prompt has them
    if "\n" in s:
        first, _, rest = s.partition("\n")
        return first.strip()[:max_headline], rest.strip()
    # Otherwise, first sentence
    for sep in (". ", "! ", "? "):
        if sep in s:
            i = s.index(sep)
            return s[:i + 1].strip()[:max_headline], s[i + 2:].strip()
    # No sentence break — split at max_headline if needed
    if len(s) <= max_headline:
        return s, ""
    cut = s.rfind(" ", 0, max_headline)
    if cut == -1:
        cut = max_headline
    return s[:cut].strip(), s[cut:].strip()


# ---------------------------------------------------------------------------
# ROUTE SURFACE — for the orchestrator to wire into orbi.py
# ---------------------------------------------------------------------------
#
# Owner-authed (cookie). config + user_dir come from the logged-in owner.
# workspace_dir = modules.workspace.workspace_path(config) — defaults to ~/Orbi/.
#
#   POST /api/owner/image_gen
#       body:   { "prompt": "Summer sale — 20% off all sandwiches",
#                 "kind":   "social_post" | "flyer" | "banner"  (optional,
#                           defaults to "social_post") }
#
#       implementation:
#           from modules.workspace import workspace_path
#           import image_gen
#           png  = image_gen.generate(config, prompt, kind)
#           path = image_gen.save_to_workspace(png, prompt, workspace_path(config))
#           return { "filename":       path.name,
#                    "workspace_path": str(path),
#                    "download_url":   "/api/owner/workspace/files/" + path.name,
#                    "kind":           kind,
#                    "bytes":          len(png) }
#
#       errors:
#           400 if prompt is missing/empty
#           500 only if even the PIL template path crashes (which would mean
#               PIL itself is broken — never expected in production)
#
# ---------------------------------------------------------------------------
