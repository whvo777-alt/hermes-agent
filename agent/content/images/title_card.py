"""Title-card featured image generator — the fixed replacement for the old
hero image on the featured-image slot.

Six structurally distinct templates (see ``TEMPLATE_IDS``), each ported from
an approved mockup. The featured image always renders one of these six —
picked by a seeded rotation (``_seed_int``, the same seeding mechanism
``hero_image``/``visual_accents`` already use), never random and never the
old hero "photo" mode. In-body section cards (``section_infographics.py``)
are a separate system and untouched by this module.

Adding a 7th template later: write a ``_render_<name>`` function with the
signature ``(colors, blog_title, keyword, category_name, subtitle,
hashtags) -> PIL.Image``, add its id to ``TEMPLATE_IDS`` and an entry in
``_RENDERERS``. Nothing else needs to change — the rotation, palette lookup,
and text/hashtag extraction are all shared.

Pure local PIL drawing — no LLM call, no network call.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from agent.content.images.hero_image import HeroImage, extract_blog_title
from agent.content.visual_accents import _seed_int

W, H = 1280, 720

_FONT_DIR = Path("/usr/share/fonts/truetype/nanum")
_F_BOLD = _FONT_DIR / "NanumGothicBold.ttf"
_F_REGULAR = _FONT_DIR / "NanumGothic.ttf"
_F_ROUND_BOLD = _FONT_DIR / "NanumSquareRoundB.ttf"

_font_cache: Dict[Tuple[str, int], object] = {}


def _font(path: Path, size: int):
    from PIL import ImageFont

    key = (str(path), size)
    if key not in _font_cache:
        try:
            _font_cache[key] = ImageFont.truetype(str(path), size)
        except OSError:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def _hx(h) -> Tuple[int, int, int]:
    if isinstance(h, tuple):
        return h  # type: ignore[return-value]
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(a, b, t: float) -> Tuple[int, int, int]:
    ca, cb = _hx(a), _hx(b)
    return tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Category palette — matches the tone section_infographics.py already ships
# (health green/blue, self-dev purple, parenting orange, it-tech indigo,
# finance teal, travel blue). Two accent variants per category so the
# border/point color still varies post-to-post within one category's tone,
# picked deterministically below by ``_category_colors``.
# ---------------------------------------------------------------------------
CATEGORY_PALETTE: Dict[str, Dict[str, object]] = {
    "it-tech": dict(bg="#e8f0fe", ink="#1b2559", accents=("#1a56db", "#3949ab")),
    "self-dev": dict(bg="#efe6fb", ink="#3a1657", accents=("#7b1fa2", "#5e35b1")),
    "finance": dict(bg="#e3f2f0", ink="#00332c", accents=("#00695c", "#00897b")),
    "health": dict(bg="#e8f5e9", ink="#173d19", accents=("#2e7d32", "#1565c0")),
    "parenting": dict(bg="#fff8e7", ink="#4a2c00", accents=("#ef6c00", "#f9a825")),
    "travel": dict(bg="#e0f7fa", ink="#01324d", accents=("#0277bd", "#00838f")),
}
_DEFAULT_PALETTE = dict(bg="#eef0f4", ink="#232a36", accents=("#37474f", "#455a64"))
_MINT = "#00b894"  # fixed decorative accent for the illustration template's underline bar


def _category_colors(category_id: str, style_seed: str) -> Dict[str, str]:
    pal = CATEGORY_PALETTE.get(category_id, _DEFAULT_PALETTE)
    variants = pal["accents"]
    idx = _seed_int(style_seed, category_id, "accent") % len(variants)
    accent = variants[idx]
    return {
        "bg": pal["bg"],
        "ink": pal["ink"],
        "accent": accent,
        "soft": "#%02x%02x%02x" % _mix(accent, "#ffffff", 0.88),
    }


TEMPLATE_IDS: Tuple[str, ...] = (
    "frame_quote",
    "badge_quotes",
    "illustration",
    "topbar_hashtags",
    "minimal_icons",
    "photo_overlay",
)


def _choose_template(style_seed: str) -> str:
    return TEMPLATE_IDS[_seed_int(style_seed, "title_card_template") % len(TEMPLATE_IDS)]


# ---------------------------------------------------------------------------
# Text extraction — keyword highlight, wrapping, subtitle, hashtags.
# No LLM call: keyword/hashtags come from the category's core + Naver-
# expanded keyword cache (agent.content.config.categories), matched against
# the actual topic title.
# ---------------------------------------------------------------------------
_STOP_TOKENS = {"확인할", "알아야", "할", "때", "위한", "하는", "되는", "있는", "없는", "정리", "기준", "방법", "가지"}


def _select_keyword(title: str, category_keywords: Optional[Sequence[str]]) -> str:
    for kw in category_keywords or []:
        if kw and kw in title:
            return kw
    tokens = [t for t in re.split(r"[\s:/·|,\-–—]+", title) if len(t) >= 2 and t not in _STOP_TOKENS]
    if not tokens:
        return title[:6]
    return max(tokens, key=len)


_META_DESC_RE = re.compile(r"(?:메타\s*설명|Meta\s*description)\s*[:：]\s*(.+)", re.I)


def _extract_subtitle(blog_content: str, *, fallback: str, max_len: int = 42) -> str:
    match = _META_DESC_RE.search(blog_content or "")
    if match:
        text = re.sub(r"[*`]", "", match.group(1)).strip()
        if text:
            return text[:max_len]
    return fallback[:max_len]


def _pick_hashtags(title: str, category_id: str, category_keywords: Optional[Sequence[str]], *, count: int = 4) -> List[str]:
    try:
        from agent.content.config.categories import get_effective_keywords

        pool = list(get_effective_keywords(category_id)) or list(category_keywords or [])
    except Exception:  # noqa: BLE001 — keyword cache is optional, never blocking
        pool = list(category_keywords or [])
    pool = list(dict.fromkeys(k for k in pool if k))
    if not pool:
        return ["#가이드"]
    order = {k: i for i, k in enumerate(pool)}
    pool.sort(key=lambda k: (k not in title, order[k]))
    return [f"#{k}" for k in pool[:count]]


def _wrap_lines(draw, text: str, f, max_width: int, max_lines: int) -> List[str]:
    words = (text or "").split()
    lines: List[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip() if cur else w
        if draw.textlength(cand, font=f) <= max_width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines[:max_lines] or [text[:1]]


def _wrap_with_highlight(
    title: str, keyword: str, *, target_line: int, max_chars: int, max_lines: int,
) -> Tuple[List[str], int]:
    """Wrap ``title`` into <= max_lines char-budget lines so ``keyword`` (if
    present) lands on line ``target_line`` — that's the template's fixed
    "which line gets the accent color" slot. Falls back to a plain wrap with
    the same target index when the keyword isn't a literal substring."""

    def greedy(words: Sequence[str], budget_lines: int) -> List[str]:
        lines: List[str] = []
        cur = ""
        for w in words:
            cand = f"{cur} {w}".strip() if cur else w
            if len(cand) <= max_chars or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = w
            if len(lines) >= budget_lines:
                break
        if cur and len(lines) < budget_lines:
            lines.append(cur)
        return lines

    title = (title or "").strip()
    if keyword and keyword in title:
        idx = title.index(keyword)
        before, after = title[:idx].strip(), title[idx + len(keyword) :].strip()
    else:
        before, after, keyword = title, "", ""

    before_words = before.split()
    after_words = after.split()

    lines_before = greedy(before_words, target_line)
    while len(lines_before) < target_line and before_words:
        lines_before.append("")

    if keyword:
        highlight_line = keyword
        if after_words and len(f"{highlight_line} {after_words[0]}") <= max_chars:
            highlight_line = f"{highlight_line} {after_words[0]}"
            after_words = after_words[1:]
    elif after_words:
        highlight_line = after_words[0]
        after_words = after_words[1:]
    elif before_words:
        highlight_line = lines_before.pop() if lines_before else before_words[-1]
    else:
        highlight_line = title[:max_chars] or "가이드"

    remaining_budget = max(max_lines - len(lines_before) - 1, 0)
    lines_after = greedy(after_words, remaining_budget)

    lines = [l for l in (lines_before + [highlight_line] + lines_after) if l][:max_lines]
    if not lines:
        lines = [title[:max_chars] or "가이드"]
    accent_index = min(target_line, len(lines) - 1)
    return lines, accent_index


def _quote_glyph(draw, cx, cy, size, color, opening: bool = True):
    ch = "\u201c" if opening else "\u201d"
    draw.text((cx, cy), ch, font=_font(_F_BOLD, size), fill=color, anchor="mm")


def _pill(draw, x, y, text, f, *, text_color, fill=None, outline=None, outline_width=2, pad_x=22, height=44) -> int:
    tw = draw.textlength(text, font=f)
    w = int(tw + pad_x * 2)
    draw.rounded_rectangle((x, y, x + w, y + height), radius=height / 2, fill=fill, outline=outline, width=outline_width)
    draw.text((x + w / 2, y + height / 2), text, font=f, fill=text_color, anchor="mm")
    return w


def _speech_bubble(draw, x, y, text, f, *, text_color, fill) -> int:
    tw = draw.textlength(text, font=f)
    w, h = int(tw + 44), 54
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h / 2, fill=fill)
    draw.text((x + w / 2, y + h / 2), text, font=f, fill=text_color, anchor="mm")
    tx = x + 26
    draw.polygon([(tx - 10, y + h - 2), (tx + 10, y + h - 2), (tx, y + h + 14)], fill=fill)
    return w


def _gradient_bg(size, c1, c2, vertical=True):
    from PIL import Image

    w, h = size
    img = Image.new("RGB", size, c1)
    px = img.load()
    steps = h if vertical else w
    for i in range(steps):
        t = i / max(steps - 1, 1)
        col = _mix(c1, c2, t)
        if vertical:
            for xx in range(w):
                px[xx, i] = col
        else:
            for yy in range(h):
                px[i, yy] = col
    return img


def _soft_blob(img, cx, cy, r, color, opacity):
    from PIL import Image, ImageDraw, ImageFilter

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_hx(color) + (opacity,))
    overlay = overlay.filter(ImageFilter.GaussianBlur(40))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


# ===========================================================================
# Templates
# ===========================================================================
def _render_frame_quote(*, colors, blog_title, keyword, category_name, subtitle, hashtags):
    from PIL import Image, ImageDraw

    lines, accent_idx = _wrap_with_highlight(blog_title, keyword, target_line=1, max_chars=11, max_lines=3)
    img = Image.new("RGB", (W, H), colors["bg"])
    draw = ImageDraw.Draw(img)
    card = (160, 90, W - 160, H - 90)
    draw.rounded_rectangle(card, radius=32, fill="#ffffff", outline=_hx(colors["accent"]), width=5)
    _quote_glyph(draw, card[0] + 90, card[1] + 66, 130, "#1a1a1a", opening=True)

    tf = _font(_F_BOLD, 54)
    ty = card[1] + 128
    for i, line in enumerate(lines):
        color = colors["accent"] if i == accent_idx else "#1a1a1a"
        draw.text((card[0] + 70, ty), line, font=tf, fill=color)
        ty += 70

    ty += 10
    draw.line((card[0] + 70, ty, card[2] - 70, ty), fill="#d8dce0", width=2)
    ty += 26
    draw.text((card[0] + 70, ty), subtitle, font=_font(_F_REGULAR, 26), fill="#78828c")
    return img


def _render_badge_quotes(*, colors, blog_title, keyword, category_name, subtitle, hashtags):
    from PIL import Image, ImageDraw

    lines, accent_idx = _wrap_with_highlight(blog_title, keyword, target_line=2, max_chars=11, max_lines=3)
    img = _gradient_bg((W, H), colors["accent"], _mix(colors["accent"], colors["bg"], 0.7), vertical=True)
    draw = ImageDraw.Draw(img)

    card = (170, 110, W - 170, H - 110)
    draw.rounded_rectangle(card, radius=40, fill="#ffffff", outline=_hx(colors["accent"]), width=6)

    badge_r = 46
    top_c = (W // 2, card[1])
    draw.ellipse((top_c[0] - badge_r, top_c[1] - badge_r, top_c[0] + badge_r, top_c[1] + badge_r), fill=_hx(colors["soft"]), outline=_hx(colors["accent"]), width=4)
    _quote_glyph(draw, top_c[0], top_c[1] - 4, 60, colors["accent"], opening=True)

    tf = _font(_F_BOLD, 50)
    ty = card[1] + 96
    for i, line in enumerate(lines):
        color = colors["accent"] if i == accent_idx else "#1a1a1a"
        w = draw.textlength(line, font=tf)
        draw.text((W / 2 - w / 2, ty), line, font=tf, fill=color)
        ty += 66

    ty += 16
    sub_w = draw.textlength(subtitle, font=_font(_F_REGULAR, 25))
    draw.text((W / 2 - sub_w / 2, ty), subtitle, font=_font(_F_REGULAR, 25), fill="#78828c")

    bot_c = (W // 2, card[3])
    draw.ellipse((bot_c[0] - badge_r, bot_c[1] - badge_r, bot_c[0] + badge_r, bot_c[1] + badge_r), fill=_hx(colors["soft"]), outline=_hx(colors["accent"]), width=4)
    _quote_glyph(draw, bot_c[0], bot_c[1] + 4, 60, colors["accent"], opening=False)
    return img


def _render_illustration(*, colors, blog_title, keyword, category_name, subtitle, hashtags):
    from PIL import Image, ImageDraw

    lines, underline_idx = _wrap_with_highlight(blog_title, keyword, target_line=1, max_chars=13, max_lines=2)
    img = _gradient_bg((W, H), colors["bg"], _mix(colors["bg"], "#ffffff", 0.5), vertical=False)
    draw = ImageDraw.Draw(img)

    lx, ly = W - 320, H - 230
    draw.rounded_rectangle((lx, ly, lx + 170, ly + 100), radius=10, fill="#ffffff", outline=_hx(colors["accent"]), width=3)
    draw.rounded_rectangle((lx + 14, ly + 14, lx + 156, ly + 78), radius=4, fill=_hx(colors["soft"]))
    draw.rounded_rectangle((lx - 8, ly + 100, lx + 178, ly + 112), radius=6, fill=_hx(colors["accent"]))
    cx, cy = lx + 215, ly + 90
    draw.rounded_rectangle((cx - 20, cy - 26, cx + 20, cy + 18), radius=6, fill="#ffffff", outline=_hx(colors["accent"]), width=3)
    draw.arc((cx + 14, cy - 18, cx + 34, cy + 2), start=280, end=100, fill=_hx(colors["accent"]), width=3)
    for wx in (-6, 6):
        draw.line((cx + wx, cy - 34, cx + wx, cy - 28), fill=_hx(colors["accent"]), width=2)
    bx, by = lx - 46, ly + 58
    draw.rounded_rectangle((bx, by, bx + 34, by + 46), radius=4, fill=_hx(colors["accent"]))
    draw.line((bx + 17, by, bx + 17, by + 46), fill="#ffffff", width=2)

    tf = _font(_F_BOLD, 58)
    ty = 190
    line_boxes = []
    for line in lines:
        w = draw.textlength(line, font=tf)
        draw.text((110, ty), line, font=tf, fill=colors["ink"])
        line_boxes.append((110, ty, 110 + w, ty + 58))
        ty += 76
    ux0, _uy0, ux1, uy1 = line_boxes[underline_idx]
    draw.rounded_rectangle((ux0, uy1 + 6, ux1, uy1 + 18), radius=6, fill=_MINT)

    badge_text = f"{category_name} 꿀팁" if category_name else "블로그 꿀팁"
    badge_y, badge_x, bh = ty + 20, 110, 48
    label_w = draw.textlength(badge_text, font=_font(_F_ROUND_BOLD, 24))
    pill_w = int(label_w + 66)
    draw.rounded_rectangle((badge_x, badge_y, badge_x + pill_w, badge_y + bh), radius=bh / 2, fill="#ffffff", outline=_hx(colors["accent"]), width=2)
    draw.ellipse((badge_x + 16, badge_y + bh / 2 - 9, badge_x + 34, badge_y + bh / 2 + 9), outline=_hx(colors["accent"]), width=3)
    draw.line((badge_x + 31, badge_y + bh / 2 + 6, badge_x + 38, badge_y + bh / 2 + 13), fill=_hx(colors["accent"]), width=3)
    draw.text((badge_x + 46, badge_y + bh / 2), badge_text, font=_font(_F_ROUND_BOLD, 24), fill=colors["ink"], anchor="lm")

    bx2, by2 = 110, ty + 96
    for i, tag in enumerate(hashtags[:3]):
        bw = _speech_bubble(draw, bx2, by2, tag, _font(_F_ROUND_BOLD, 22), text_color="#ffffff", fill=_hx(colors["accent"]) if i % 2 == 0 else _hx(_mix(colors["accent"], colors["ink"], 0.3)))
        bx2 += bw + 24
    return img


def _render_topbar_hashtags(*, colors, blog_title, keyword, category_name, subtitle, hashtags):
    from PIL import Image, ImageDraw

    lines, accent_idx = _wrap_with_highlight(blog_title, keyword, target_line=0, max_chars=11, max_lines=3)
    img = Image.new("RGB", (W, H), colors["bg"])
    draw = ImageDraw.Draw(img)

    card = (150, 80, W - 150, H - 80)
    draw.rounded_rectangle(card, radius=28, fill="#ffffff", outline=(20, 20, 20), width=4)

    tf = _font(_F_BOLD, 52)
    ty = card[1] + 72
    for i, line in enumerate(lines):
        color = colors["accent"] if i == accent_idx else "#1a1a1a"
        draw.text((card[0] + 56, ty), line, font=tf, fill=color)
        ty += 68

    ty += 8
    for line in _wrap_lines(draw, subtitle, _font(_F_REGULAR, 24), card[2] - card[0] - 112, 2):
        draw.text((card[0] + 56, ty), line, font=_font(_F_REGULAR, 24), fill="#7a838c")
        ty += 34

    ty += 20
    x = card[0] + 56
    for i, tag in enumerate(hashtags[:5]):
        if i == 0:
            wtag = _pill(draw, x, ty, tag, _font(_F_BOLD, 22), text_color="#ffffff", fill=_hx(colors["accent"]), height=42)
        else:
            wtag = _pill(draw, x, ty, tag, _font(_F_REGULAR, 22), text_color=colors["ink"], fill=_hx(colors["soft"]), outline=_hx(colors["accent"]), outline_width=2, height=42)
        x += wtag + 14
    return img


def _render_minimal_icons(*, colors, blog_title, keyword, category_name, subtitle, hashtags):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), "#fdf8e9")
    draw = ImageDraw.Draw(img)

    icon_y = 70
    sx = W - 210
    draw.ellipse((sx, icon_y, sx + 36, icon_y + 36), outline=_hx(colors["ink"]), width=4)
    draw.line((sx + 32, icon_y + 32, sx + 48, icon_y + 48), fill=_hx(colors["ink"]), width=4)
    bx = W - 130
    draw.polygon([(bx, icon_y), (bx + 32, icon_y), (bx + 32, icon_y + 44), (bx + 16, icon_y + 30), (bx, icon_y + 44)], outline=_hx(colors["ink"]), width=3)

    tf = _font(_F_BOLD, 58)
    lines = _wrap_lines(draw, blog_title, tf, 720, 2)
    ty = 210
    for line in lines:
        draw.text((120, ty), line, font=tf, fill=colors["ink"])
        ty += 76

    ty += 24
    x = 120
    for tag in hashtags[:3]:
        wtag = _pill(draw, x, ty, tag, _font(_F_REGULAR, 23), text_color=colors["ink"], fill=None, outline=_hx(colors["accent"]), outline_width=2, height=44)
        x += wtag + 14
    ty += 44 + 34

    draw.line((120, ty, W - 120, ty), fill="#e6ddc0", width=2)
    ty += 24
    draw.text((120, ty), subtitle, font=_font(_F_REGULAR, 24), fill="#8a8264")
    return img


def _render_photo_overlay(*, colors, blog_title, keyword, category_name, subtitle, hashtags):
    from PIL import Image, ImageDraw

    lines, accent_idx = _wrap_with_highlight(blog_title, keyword, target_line=1, max_chars=13, max_lines=3)
    title_runs = [(line, i == accent_idx) for i, line in enumerate(lines)]

    # Fake "photo" -- gradient + soft blobs standing in for a real photograph
    # (matches hero_image.py's existing mood-art convention for "photo" mode).
    img = _gradient_bg((W, H), _mix(colors["accent"], "#2b2b2b", 0.35), _mix(colors["ink"], "#111111", 0.55))
    img = _soft_blob(img, W - 260, 160, 260, "#ffffff", 40)
    img = _soft_blob(img, 220, H - 120, 220, colors["accent"], 60)

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for y in range(H):
        t = y / H
        alpha = int(60 + 140 * t)
        od.line((0, y, W, y), fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    tf = _font(_F_BOLD, 56)
    ty = 190
    for text, is_accent in title_runs:
        color = _mix(colors["accent"], "#ffffff", 0.25) if is_accent else "#ffffff"
        draw.text((100, ty), text, font=tf, fill=color)
        ty += 74

    ty += 10
    draw.text((100, ty), subtitle, font=_font(_F_REGULAR, 25), fill="#d8dce0")
    ty += 56

    x = 100
    for tag in hashtags[:2]:
        wtag = _pill(draw, x, ty, tag, _font(_F_REGULAR, 22), text_color="#ffffff", fill=None, outline=(255, 255, 255), outline_width=2, height=42)
        x += wtag + 14
    return img


_RENDERERS = {
    "frame_quote": _render_frame_quote,
    "badge_quotes": _render_badge_quotes,
    "illustration": _render_illustration,
    "topbar_hashtags": _render_topbar_hashtags,
    "minimal_icons": _render_minimal_icons,
    "photo_overlay": _render_photo_overlay,
}


def create_title_card(
    *,
    out_dir: Path,
    platform_id: str,
    platform_label: str,
    category_id: str,
    category_name: str,
    category_keywords: Optional[list],
    topic_title: str,
    blog_content: str,
    style_seed: str = "",
) -> HeroImage:
    """Render the featured image as a title card and return its ``HeroImage``
    record — same shape as ``hero_image.create_hero_image`` /
    ``ai_hero_image.create_hero_image_ai`` so this is a drop-in replacement
    at the orchestrator call site."""
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = style_seed or f"{platform_id}:{topic_title}"

    template_id = _choose_template(seed)
    colors = _category_colors(category_id, seed)
    blog_title = extract_blog_title(blog_content, topic_title or f"{category_name} 가이드")
    keyword = _select_keyword(blog_title, category_keywords)
    hashtags = _pick_hashtags(blog_title, category_id, category_keywords)
    subtitle = _extract_subtitle(blog_content, fallback=f"{category_name} 핵심만 정리했습니다")

    renderer = _RENDERERS[template_id]
    img = renderer(
        colors=colors, blog_title=blog_title, keyword=keyword,
        category_name=category_name, subtitle=subtitle, hashtags=hashtags,
    )

    file_path = out_dir / "title_card.png"
    img.convert("RGB").save(str(file_path), "PNG")

    alt = f"{blog_title} — {category_name} 핵심을 정리한 타이틀 카드"
    caption = f"{category_name} 대표 이미지입니다."
    return HeroImage(
        role="hero",
        status="local_ready_upload_required",
        file=str(file_path),
        alt=alt,
        caption=caption,
        prompt=f"title_card template={template_id} category={category_id}",
        inserted_in="after_title",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
