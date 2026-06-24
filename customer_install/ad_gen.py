"""
ad_gen — Build a FINISHED ad creative: background image + headline overlay
+ body copy + CTA button, composited into a single uploadable PNG.

The user complaint that drove this module: "She'll draw pictures, she'll
give me copy, but she won't actually create the ad." image_gen produces
the background, prompts.py teaches the LLM to write campaign copy, but
neither hands the owner a finished asset they can paste into Facebook
Ads Manager / Canva / their CMS.

Pipeline:
    user_brief: "an ad for our deli's weekend brunch"
      → 1) LLM designs the ad (returns JSON: headline, body, cta, image_brief)
      → 2) image_gen.generate(image_brief, kind=platform_size)
      → 3) PIL composite — image at top, dark band at bottom holding the
           headline (large), body (medium), CTA button (accent color)
      → 4) return PNG bytes + the components dict (for logging / display)
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from pathlib import Path

import image_gen
import llm_client

log = logging.getLogger("orbi.ad_gen")


# ---------------------------------------------------------------------------
# Exemplar corpus — the owner pastes ads they like, we store them, and use
# them as few-shot examples on every future ad build.
# ---------------------------------------------------------------------------

_EXEMPLAR_FILE = "ad_exemplars.json"
_EXEMPLAR_MAX = 50   # keep newest N; older ones drop off so prompts stay sane


def save_exemplar(data_dir: Path, raw_text: str,
                  source: str = "owner_paste",
                  tags: list[str] | None = None) -> dict:
    """Append an exemplar ad to the corpus.  Returns the stored entry."""
    import time
    import uuid
    raw_text = (raw_text or "").strip()
    if not raw_text:
        raise ValueError("exemplar text required")
    p = Path(data_dir) / _EXEMPLAR_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "exemplars" not in data:
            data = {"exemplars": []}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {"exemplars": []}
    entry = {
        "id": uuid.uuid4().hex[:12],
        "saved_at": time.time(),
        "source": source,
        "tags": tags or [],
        "text": raw_text[:4000],   # cap individual size
    }
    data["exemplars"].append(entry)
    # Cap total count, newest first
    data["exemplars"] = sorted(
        data["exemplars"], key=lambda x: x.get("saved_at", 0),
        reverse=True)[:_EXEMPLAR_MAX]
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)
    log.info("saved ad exemplar (%d chars) → %s", len(raw_text), p)
    return entry


def load_exemplars(data_dir: Path, limit: int = 4) -> list[dict]:
    """Load the newest `limit` exemplars. Most recent are most relevant
    because tastes drift; an older paste might be outdated."""
    p = Path(data_dir) / _EXEMPLAR_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        exs = data.get("exemplars", []) if isinstance(data, dict) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    exs = sorted(exs, key=lambda x: x.get("saved_at", 0), reverse=True)
    return exs[:limit]


def _exemplar_prompt_block(exemplars: list[dict]) -> str:
    """Format exemplars as a few-shot examples block for the LLM."""
    if not exemplars:
        return ""
    parts = ["EXAMPLES OF ADS THE OWNER LIKES — match this voice, tone, "
             "rhythm, and level of detail. Do NOT copy the words; copy "
             "the STYLE (headline punch, sentence length, vocabulary, "
             "emotional register, CTA flavor):\n"]
    for i, ex in enumerate(exemplars, 1):
        parts.append(f"\n--- EXAMPLE {i} ---\n{ex['text']}\n")
    parts.append("\n--- END EXAMPLES ---\n")
    return "".join(parts)


# Platform → (canvas size, design tweaks). These mirror image_gen.SIZES but
# the ad layout is tuned per platform (a story has more vertical room for
# text than a square Facebook post).
AD_PLATFORM_SIZES = {
    "instagram_square":  (1080, 1080),
    "instagram_story":   (1080, 1920),
    "instagram_portrait":(1080, 1350),
    "facebook_post":     (1200, 1200),
    "facebook_cover":    (1640,  856),
    "twitter_post":      (1600,  900),
    "linkedin_post":     (1200, 1200),
    "youtube_thumbnail": (1280,  720),
    "tiktok_post":       (1080, 1920),
    "pinterest_pin":     (1000, 1500),
    "flyer_portrait":    (1224, 1584),
}

# Orbi violet — used for the CTA button background by default.
DEFAULT_ACCENT = (139, 92, 246)


AD_DESIGNER_SYSTEM = (
    "You are a senior performance-marketing copywriter at a top agency. "
    "Use the AIDA framework (Attention, Interest, Desire, Action) for the "
    "copy. Output STRICT JSON only — no preamble, no code fences, no "
    "explanations outside the JSON.\n\n"
    "CRITICAL — RESPECT THE BRIEF SUBJECT:\n"
    "The OWNER BRIEF tells you what THIS ad is about. The BUSINESS PROFILE "
    "tells you who the business IS. These can disagree — e.g., a business "
    "named 'myOrbi' might ask you for an ad about 'weekend "
    "brunch.' That's not a contradiction — they're promoting a SPECIFIC "
    "offering (the brunch), not their whole business. Always design the ad "
    "about THE SUBJECT IN THE BRIEF, not the business name. The business "
    "profile sets BRAND VOICE + AVAILABLE FACTS, not the topic.\n\n"
    "SCHEMA — exact keys:\n"
    "  headline       — 4-8 words. ATTENTION step. Hook them with a\n"
    "                   benefit, a question, or a sharp number. No vague\n"
    "                   filler like 'Welcome to' / 'Discover'.\n"
    "  body           — 18-30 words. Combines INTEREST (specific value prop)\n"
    "                   + DESIRE (one concrete proof/sensory detail). Plain\n"
    "                   conversational tone. NO em-dashes inside the body.\n"
    "  cta            — 1-3 words. ACTION step. Imperative verb +\n"
    "                   object: 'Order Now' / 'Book a Table' / 'Get a Quote'.\n"
    "                   Match the offer ('Try' if free, 'Buy' if paid).\n"
    "  headline_alts  — array of TWO alternate headlines, same rules as\n"
    "                   headline. Used for A/B test variants.\n"
    "  image_brief    — 1-2 sentences. The image MUST literally depict the\n"
    "                   SUBJECT of the headline / offer — not a generic\n"
    "                   'business person at desk' or 'man at breakfast'\n"
    "                   scene. Examples:\n"
    "                     * Headline about WEEKEND BRUNCH → 'overhead\n"
    "                       photograph of a brunch table: eggs benedict,\n"
    "                       coffee, mimosas, golden morning sunlight, food-\n"
    "                       magazine style, shallow depth of field'\n"
    "                     * Headline about AI RECEPTIONIST → 'professional\n"
    "                       woman wearing a wireless headset at a clean\n"
    "                       modern desk, smiling while taking a call, glowing\n"
    "                       laptop screen, soft office lighting, editorial\n"
    "                       commercial photography'\n"
    "                     * Headline about a SALE / PRODUCT → 'hero product\n"
    "                       shot of the specific item, studio softbox\n"
    "                       lighting, clean white background, e-commerce\n"
    "                       style, 50mm lens look'\n"
    "                   Required slots: SUBJECT + SETTING + LIGHTING + STYLE\n"
    "                   ('commercial photography' / 'editorial' / 'product\n"
    "                   hero shot' / 'lifestyle photography' / 'food magazine').\n"
    "                   Frame the subject so the UPPER 65% of the image\n"
    "                   contains the action and the LOWER 35% is cleaner\n"
    "                   (sky / table surface / blurred floor / wall) for\n"
    "                   the text overlay.\n"
    "                   NO words inside the image (AI image models garble\n"
    "                   text). NO 'campaign' / 'army' / 'troops' / loaded\n"
    "                   military words.\n\n"
    "RULES:\n"
    "- Pull facts ONLY from the business profile when provided. NEVER\n"
    "  invent a discount, sale, hours, address, phone, or feature.\n"
    "- If the business policy says no-trials / no-money-back, do NOT\n"
    "  promise either in the ad. Use 'cancel anytime, no penalties'\n"
    "  if applicable.\n"
    "- NEVER use the phrase 'free trial' / 'free AI' / 'try free' unless\n"
    "  the business profile EXPLICITLY lists a free offering. Defaulting\n"
    "  to 'free' is a leading source of false-advertising complaints.\n"
    "- NEVER use the phrase 'small business' / 'small businesses' /\n"
    "  'small-business' in headlines, body, or CTAs. The product works for\n"
    "  businesses of any size — use 'business', 'your business', 'business\n"
    "  owners', or specific audience descriptors instead.\n"
    "- Keep claims defensible. 'Loved by hundreds of regulars' is fine if\n"
    "  plausible. '#1 in town' / 'voted best' require proof in the profile.\n"
    "- The headline must NOT repeat the business name (it goes in the\n"
    "  Facebook ad's separate name field). Lead with the BENEFIT.\n"
    "- Vary the headline alts — don't just rephrase. One can be a question,\n"
    "  one can be a stat or curiosity hook.\n"
)


# Brief-quality detector — if the brief is too thin, we ask 2-3 clarifying
# questions before generating. Mirrors what ChatGPT does ("Sure! Before I
# build it, can you tell me the audience and the offer?").
def brief_needs_clarification(brief: str) -> list[str]:
    """Return a list of questions to ask the owner, or [] if brief is rich
    enough to design from. Questions are tailored to what's missing."""
    b = (brief or "").strip().lower()
    if not b:
        return [
            "What's the OFFER — what action do you want a viewer to take? "
                "(e.g. book a table, buy a product, sign up, visit the shop)",
            "Who's the AUDIENCE — locals, families, professionals, hobbyists, "
                "a specific age range?",
            "What TONE — premium / friendly / urgent / playful / professional?",
        ]
    questions: list[str] = []
    has_offer = any(w in b for w in (
        "sale", "% off", "discount", "free", "new", "launch", "open",
        "brunch", "lunch", "dinner", "menu", "happy hour", "special",
        "buy", "order", "book", "rsvp", "register", "sign up", "try",
        "visit", "stop by", "come in", "promotion"))
    has_audience = any(w in b for w in (
        "for ", "people who", "anyone", "moms", "dads", "parents", "kids",
        "families", "couples", "locals", "tourists", "professionals",
        "students", "business owners", "small business", "homeowners",
        "fans", "executives", "managers", "enterprises"))
    has_format_hint = any(w in b for w in (
        "facebook", "instagram", "story", "post", "tiktok", "linkedin",
        "youtube", "reels", "carousel", "flyer", "poster"))

    if len(b) < 30:
        questions.append(
            "Can you say more about the ad — what's the specific OFFER "
            "or message? (e.g. 'weekend brunch, $2 mimosas' or 'free "
            "delivery on orders over $30')")
    if not has_offer and "weekend brunch" not in b and "menu" not in b:
        questions.append(
            "What's the OFFER? What action should a viewer take after "
            "seeing the ad — book, buy, visit, call, sign up?")
    if not has_audience:
        questions.append(
            "Who's the AUDIENCE? Are you targeting locals, families, "
            "professionals, or some other specific group?")
    if not has_format_hint:
        questions.append(
            "What FORMAT — Facebook square post, Instagram story (tall), "
            "Facebook cover banner, or something else?")
    # Cap at 3 questions — more feels like an interrogation
    return questions[:3]


def design_ad(config: dict, brief: str, business: dict | None = None,
              platform: str = "instagram_square",
              data_dir: Path | None = None) -> dict:
    """LLM-design the ad. Returns {'headline', 'body', 'cta', 'image_brief'}.
    Raises RuntimeError if the LLM is unreachable or returns invalid JSON.

    If `data_dir` is provided, pulls the most recent ad exemplars from
    the corpus and uses them as few-shot examples so the output matches
    the owner's preferred style.
    """
    biz_ctx = ""
    if business:
        # Compact summary the LLM can chew on
        keys = ("name", "tagline", "services", "hours", "address",
                "policies", "faq")
        parts = []
        for k in keys:
            v = business.get(k)
            if v:
                if isinstance(v, (list, dict)):
                    v = json.dumps(v)[:500]
                parts.append(f"{k}: {v}")
        biz_ctx = ("BUSINESS PROFILE (use these facts, don't invent others):\n"
                   + "\n".join(parts) + "\n\n")

    # Few-shot exemplars — ads the owner pasted as "do more like this"
    exemplar_block = ""
    if data_dir:
        try:
            exemplars = load_exemplars(data_dir, limit=4)
            exemplar_block = _exemplar_prompt_block(exemplars)
            if exemplars:
                log.info("ad_design using %d exemplars", len(exemplars))
        except Exception as exc:    # noqa: BLE001
            log.warning("exemplar load failed: %s", exc)

    system = AD_DESIGNER_SYSTEM + ("\n\n" + exemplar_block if exemplar_block else "")
    user_msg = (f"{biz_ctx}PLATFORM: {platform}\n\n"
                f"OWNER BRIEF: {brief.strip()}")
    # Use the two-pass review path — ads are high-stakes output and the
    # quality jump is worth the ~2x LLM time. Reviewer preserves JSON
    # format so the schema is intact.
    #
    # Frank 2026-06-23: the reviewer pass occasionally truncates the JSON
    # mid-headline_alts (Frank's test transcript showed exactly this).
    # If the first attempt's JSON is unparseable, retry with single-pass
    # (no review) — sacrifices the quality boost but at least we ship
    # a working ad. Two failed parses = real error.
    def _try_parse(text):
        if not text:
            return None
        raw = text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    resp = llm_client.generate_with_review(
        config, system, [{"role": "user", "content": user_msg}],
        enable_review=True)
    if not resp or not resp.text:
        raise RuntimeError("ad_designer LLM unavailable")
    data = _try_parse(resp.text)
    if data is None:
        log.warning("ad_designer review-pass produced unparseable JSON, "
                    "retrying single-pass: raw=%r", (resp.text or "")[:200])
        resp2 = llm_client.generate(
            config, system, [{"role": "user", "content": user_msg}])
        if not resp2 or not resp2.text:
            raise RuntimeError("ad_designer LLM unavailable on retry")
        data = _try_parse(resp2.text)
        if data is None:
            raise RuntimeError(
                f"ad_designer LLM did not return parseable JSON "
                f"(both attempts failed): {(resp2.text or '')[:300]!r}")
    # Validate
    for key in ("headline", "body", "cta", "image_brief"):
        v = data.get(key)
        if not v or not isinstance(v, str):
            raise RuntimeError(f"ad_designer missing/invalid '{key}'")
    return data


def build_ad(config: dict, brief: str, business: dict | None = None,
             platform: str = "instagram_square",
             accent: tuple[int, int, int] = DEFAULT_ACCENT,
             data_dir: Path | None = None) -> tuple[bytes, dict]:
    """End-to-end: design the ad, generate the background image, composite
    everything. Returns (png_bytes, components_dict)."""
    platform = platform if platform in AD_PLATFORM_SIZES else "instagram_square"
    components = design_ad(config, brief, business=business, platform=platform,
                            data_dir=data_dir)

    log.info("ad design: headline=%r cta=%r", components["headline"][:40],
             components["cta"])

    # Generate the background image at the platform's native size
    bg_png = image_gen.generate(
        config, components["image_brief"],
        kind=platform,
        enhance=False,  # the brief already came from an LLM, no need to re-enhance
    )

    # Composite
    final = _composite_ad(
        bg_png,
        headline=components["headline"],
        body=components["body"],
        cta=components["cta"],
        platform=platform,
        accent=accent,
    )
    return final, components


def _composite_ad(bg_png: bytes, *, headline: str, body: str, cta: str,
                   platform: str, accent: tuple[int, int, int]) -> bytes:
    """Lay headline, body, and CTA button on top of the background image.

    Layout choice: a SOFT GRADIENT fade from transparent (top) to dark
    (bottom) over the lower ~40% of the image, with text on top. Reads
    much more modern than a hard dark rectangle — the image is still
    visible through the gradient, but the text stays crisp.
    """
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(bg_png)).convert("RGBA")
    W, H = img.size

    # Sizing scales with width so it looks proportional on any aspect ratio.
    # Slightly smaller than before so the band can shrink.
    headline_size = max(36, int(W * 0.050))
    body_size     = max(20, int(W * 0.026))
    cta_size      = max(22, int(W * 0.030))

    headline_font = image_gen._load_font(headline_size, bold=True)
    body_font     = image_gen._load_font(body_size, bold=False)
    cta_font      = image_gen._load_font(cta_size, bold=True)

    # Use a measurement-only draw for wrapping before we know band size
    measure = ImageDraw.Draw(img)
    max_text_w = int(W * 0.84)
    headline_lines = image_gen._wrap(measure, headline, headline_font, max_text_w)
    body_lines     = image_gen._wrap(measure, body, body_font, max_text_w)

    headline_h = image_gen._line_height(measure, "Ag", headline_font)
    body_h     = image_gen._line_height(measure, "Ag", body_font)

    line_gap = int(headline_size * 0.16)
    section_gap = int(headline_size * 0.45)
    cta_pad_x = max(24, int(W * 0.035))
    cta_pad_y = max(12, int(cta_size * 0.45))

    block_h = (
        headline_h * len(headline_lines)
        + line_gap * max(0, len(headline_lines) - 1)
        + section_gap
        + body_h * len(body_lines)
        + line_gap * max(0, len(body_lines) - 1)
        + section_gap
        + cta_size + cta_pad_y * 2
    )
    # Cap text-region height at 38% of canvas — never let it eat the
    # image. Anything over that and the gradient just extends further
    # above without enlarging.
    band_pad = max(28, int(H * 0.035))
    text_region_h = block_h + band_pad * 2
    max_text_region = int(H * 0.38)
    text_region_h = min(text_region_h, max_text_region)
    # The gradient fades in over a region 50% taller than the text region
    # so the transition is smooth and the image keeps breathing room.
    gradient_h = min(int(text_region_h * 1.5), int(H * 0.55))
    gradient_y0 = H - gradient_h
    text_y0 = H - text_region_h

    # ── Soft black gradient overlay ──────────────────────────────────────
    # Build a 1-pixel-wide alpha column then stretch horizontally.
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay_pixels = overlay.load()
    for y in range(gradient_y0, H):
        t = (y - gradient_y0) / max(1, gradient_h)        # 0 → 1
        t = t * t                                          # ease-in (slower start)
        alpha = int(t * 195)                               # max ~76% opacity at bottom
        for x in range(W):
            overlay_pixels[x, y] = (0, 0, 0, alpha)
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img, "RGBA")

    # Accent dot above the headline as a subtle brand cue (instead of a
    # full-width stripe which felt like a hard divider).
    dot_r = max(5, int(headline_size * 0.12))
    dot_y = text_y0 + band_pad - dot_r * 3
    if dot_y > 0:
        draw.ellipse(
            (W // 2 - dot_r, dot_y, W // 2 + dot_r, dot_y + dot_r * 2),
            fill=accent)

    # ── Text ─────────────────────────────────────────────────────────────
    y = text_y0 + band_pad
    for ln in headline_lines:
        w = image_gen._text_width(draw, ln, headline_font)
        x = (W - w) // 2
        # Soft drop-shadow for crisp legibility on busy photos
        draw.text((x + 2, y + 3), ln, fill=(0, 0, 0, 200), font=headline_font)
        draw.text((x, y), ln, fill=(255, 255, 255), font=headline_font)
        y += headline_h + line_gap
    y += section_gap - line_gap

    for ln in body_lines:
        w = image_gen._text_width(draw, ln, body_font)
        x = (W - w) // 2
        draw.text((x + 1, y + 2), ln, fill=(0, 0, 0, 180), font=body_font)
        draw.text((x, y), ln, fill=(235, 235, 235), font=body_font)
        y += body_h + line_gap
    y += section_gap - line_gap

    # CTA button — accent fill, white text, rounded corners
    cta_text_w = image_gen._text_width(draw, cta, cta_font)
    btn_w = cta_text_w + cta_pad_x * 2
    btn_h = cta_size + cta_pad_y * 2
    btn_x0 = (W - btn_w) // 2
    btn_y0 = y
    btn_x1 = btn_x0 + btn_w
    btn_y1 = btn_y0 + btn_h

    try:
        draw.rounded_rectangle(
            (btn_x0, btn_y0, btn_x1, btn_y1),
            radius=int(btn_h * 0.45), fill=accent)
    except AttributeError:
        draw.rectangle((btn_x0, btn_y0, btn_x1, btn_y1), fill=accent)

    cta_text_x = btn_x0 + (btn_w - cta_text_w) // 2
    cta_text_y = btn_y0 + cta_pad_y
    draw.text((cta_text_x, cta_text_y), cta, fill=(255, 255, 255), font=cta_font)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def save_ad_to_workspace(png_bytes: bytes, brief: str,
                          workspace_dir: Path) -> Path:
    """Write the finished ad PNG to the owner's workspace under /ads/."""
    workspace_dir = Path(workspace_dir).expanduser()
    ads_dir = workspace_dir / "ads"
    ads_dir.mkdir(parents=True, exist_ok=True)

    import hashlib
    from datetime import datetime
    now = datetime.now()
    short = hashlib.sha1(brief.encode("utf-8")).hexdigest()[:8]
    fname = (f"ad_{now.strftime('%Y-%m-%d')}_{now.strftime('%H%M%S')}_{short}.png")
    path = ads_dir / fname
    tmp = path.with_suffix(".png.tmp")
    tmp.write_bytes(png_bytes)
    tmp.replace(path)
    log.info("saved ad (%d bytes) -> %s", len(png_bytes), path)
    return path
