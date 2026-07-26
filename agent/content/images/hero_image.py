"""Hero image generator — ported from multi-content-pipeline/agents/images/hero-image.js.

Pure local SVG templating. No LLM call, no network call.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PALETTES: Dict[str, Tuple[str, str, str, str]] = {
    "health": ("#e8f5e9", "#2e7d32", "#81c784", "#fff8e1"),
    "parenting": ("#fff3e0", "#ef6c00", "#ffcc80", "#fce4ec"),
    "self-dev": ("#e3f2fd", "#1565c0", "#90caf9", "#fffde7"),
    "it-tech": ("#e8eaf6", "#3949ab", "#9fa8da", "#e0f7fa"),
    "finance": ("#e8f5e9", "#00695c", "#80cbc4", "#fffde7"),
    "travel": ("#e0f7fa", "#0277bd", "#80deea", "#fff8e1"),
}

# Extra palettes rotated per post so two health posts never share the same look.
_STYLE_PALETTES: Tuple[Tuple[str, str, str, str], ...] = (
    ("#e8f5e9", "#2e7d32", "#81c784", "#fff8e1"),  # mint green
    ("#e3f2fd", "#1565c0", "#64b5f6", "#fffde7"),  # sky blue
    ("#fce4ec", "#c2185b", "#f48fb1", "#fff8e1"),  # soft rose
    ("#fff3e0", "#ef6c00", "#ffb74d", "#e8f5e9"),  # warm orange
    ("#f3e5f5", "#7b1fa2", "#ce93d8", "#e0f7fa"),  # lilac
    ("#e0f2f1", "#00695c", "#4db6ac", "#fff8e1"),  # teal
    ("#efebe9", "#5d4037", "#a1887f", "#fffde7"),  # cocoa
    ("#e8eaf6", "#303f9f", "#7986cb", "#fce4ec"),  # indigo
)

_DEFAULT_PALETTE = ("#f5f5f5", "#37474f", "#b0bec5", "#ffffff")

_ICONS: Dict[str, Tuple[str, str, str, str]] = {
    "health": ("+", "물", "단백질", "운동"),
    "parenting": ("+", "이유식", "안전", "루틴"),
    "self-dev": ("+", "계획", "집중", "기록"),
    "it-tech": ("+", "비교", "성능", "선택"),
    "finance": ("+", "분산", "기준", "위험"),
    "travel": ("+", "동선", "맛집", "준비"),
}
_DEFAULT_ICONS = ("+", "요약", "체크", "가이드")


def _escape_xml(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _short_title(title: str, *, max_len: int = 42) -> str:
    value = str(title or "")
    value = re.sub(r"^제목 \(H1\):\s*", "", value)
    value = re.sub(r"^제목:\s*", "", value)
    value = re.sub(r"^#\s*", "", value)
    value = value.strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def _wrap_title_lines(title: str, *, max_chars: int = 18, max_lines: int = 3) -> List[str]:
    """Split a Korean/English title into display lines for the hero card."""
    text = _short_title(title, max_len=54)
    if not text:
        return ["블로그 가이드"]
    # Prefer splitting on spaces / middle dots / colons.
    parts = re.split(r"\s+", text)
    lines: List[str] = []
    current = ""
    for part in parts:
        candidate = f"{current} {part}".strip() if current else part
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = part
        if len(lines) >= max_lines - 1:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if not lines:
        lines = [text[:max_chars]]
    # If still one overlong line, hard-wrap.
    fixed: List[str] = []
    for line in lines[:max_lines]:
        while len(line) > max_chars and len(fixed) < max_lines - 1:
            fixed.append(line[:max_chars])
            line = line[max_chars:]
        if len(line) > max_chars:
            # Last available slot still overflows — mark the cut instead of
            # silently dropping the remainder.
            line = line[: max_chars - 1].rstrip() + "…"
        fixed.append(line)
    return fixed[:max_lines]


def _topic_chips(topic_title: str, fallback: Tuple[str, str, str]) -> Tuple[str, str, str]:
    """Pick 3 short chips from the topic instead of generic category labels."""
    tokens = [
        t for t in re.split(r"[\s:/·|,\-–—]+", str(topic_title or ""))
        if t and len(t) >= 2 and not re.fullmatch(r"\d+가지|\d+단계|\d+", t)
    ]
    # Drop filler words.
    stop = {"확인할", "알아야", "할", "때", "위한", "하는", "되는", "있는", "없는", "정리", "기준", "방법"}
    tokens = [t for t in tokens if t not in stop][:6]
    chips = []
    for token in tokens:
        chips.append(token[:6])
        if len(chips) == 3:
            break
    while len(chips) < 3:
        chips.append(fallback[len(chips)])
    return chips[0], chips[1], chips[2]


def extract_blog_title(content: str, fallback: str = "블로그 가이드") -> str:
    lines = [line.strip() for line in (content or "").split("\n") if line.strip()]
    found = next(
        (line for line in lines if re.match(r"^제목 \(H1\):|^제목:|^#\s+", line)),
        None,
    )
    return _short_title(found or fallback, max_len=48)


@dataclass
class CategoryVisual:
    bg: str
    main: str
    accent: str
    soft: str
    keyword: str
    title: str
    icons: Tuple[str, str, str, str]
    style_index: int = 0
    badge_kind: str = "checklist"
    layout: str = "card-right"  # card-right | band-left | split-diagonal
    hero_mode: str = "card"  # card | photo (photo = full-bleed gradient mood art, not a real photo)


def _style_index(seed: str) -> int:
    digest = hashlib.sha256(str(seed or "hero").encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _category_visual(
    category_id: str,
    category_name: str,
    category_keywords: Optional[list],
    *,
    style_seed: str = "",
    topic_title: str = "",
) -> CategoryVisual:
    from agent.content.visual_accents import choose_hero_mode

    idx = _style_index(style_seed or category_id)
    # Prefer a post-unique palette so same-category heroes do not look cloned.
    bg, main, accent, soft = _STYLE_PALETTES[idx % len(_STYLE_PALETTES)]
    # Keep a light category bias on even styles so brand still reads.
    if idx % 2 == 0 and category_id in _PALETTES:
        base = _PALETTES[category_id]
        bg, soft = base[0], base[3]
        main, accent = _STYLE_PALETTES[(idx // 2) % len(_STYLE_PALETTES)][1:3]
    keyword = (category_keywords or [None])[0] or category_name
    icons = _ICONS.get(category_id, _DEFAULT_ICONS)
    badge_cycle = ("capsule", "checklist", "target", "book")
    layout_cycle = ("card-right", "band-left", "split-diagonal")
    hero_mode = choose_hero_mode(
        topic_title=topic_title or keyword,
        category_id=category_id,
        style_seed=style_seed or f"{category_id}:{topic_title}",
    )
    return CategoryVisual(
        bg=bg,
        main=main,
        accent=accent,
        soft=soft,
        keyword=keyword,
        title=category_name,
        icons=icons,
        style_index=idx,
        badge_kind=badge_cycle[idx % len(badge_cycle)],
        layout=layout_cycle[(idx // 3) % len(layout_cycle)],
        hero_mode=hero_mode,
    )


@dataclass
class HeroImage:
    role: str
    status: str
    file: str
    alt: str
    caption: str
    prompt: str
    inserted_in: str
    created_at: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "role": self.role,
            "status": self.status,
            "file": self.file,
            "alt": self.alt,
            "caption": self.caption,
            "prompt": self.prompt,
            "insertedIn": self.inserted_in,
            "createdAt": self.created_at,
        }


def _badge_svg(visual: CategoryVisual) -> str:
    """Right-side badge glyph — varies by style so heroes do not clone."""
    if visual.badge_kind == "capsule":
        return (
            f'<rect x="78" y="108" width="104" height="44" rx="22" fill="{visual.accent}"/>'
            f'<rect x="78" y="108" width="52" height="44" rx="22" fill="#ffffff" opacity="0.92"/>'
            f'<circle cx="92" cy="90" r="8" fill="#ffffff" opacity="0.85"/>'
            f'<circle cx="168" cy="168" r="7" fill="#ffffff" opacity="0.7"/>'
        )
    if visual.badge_kind == "target":
        return (
            f'<circle cx="130" cy="130" r="70" fill="none" stroke="{visual.main}" stroke-width="10"/>'
            f'<circle cx="130" cy="130" r="42" fill="none" stroke="{visual.accent}" stroke-width="10"/>'
            f'<circle cx="130" cy="130" r="16" fill="{visual.main}"/>'
        )
    if visual.badge_kind == "book":
        return (
            f'<rect x="70" y="70" width="120" height="150" rx="12" fill="#ffffff" '
            f'stroke="{visual.main}" stroke-width="5"/>'
            f'<rect x="70" y="70" width="18" height="150" fill="{visual.main}"/>'
            f'<line x1="108" y1="110" x2="170" y2="110" stroke="{visual.accent}" stroke-width="6" stroke-linecap="round"/>'
            f'<line x1="108" y1="140" x2="170" y2="140" stroke="{visual.accent}" stroke-width="6" stroke-linecap="round"/>'
            f'<line x1="108" y1="170" x2="155" y2="170" stroke="{visual.accent}" stroke-width="6" stroke-linecap="round"/>'
        )
    # checklist default
    return (
        f'<rect x="70" y="78" width="120" height="140" rx="18" fill="#ffffff" '
        f'stroke="{visual.main}" stroke-width="5"/>'
        f'<rect x="70" y="78" width="120" height="34" rx="18" fill="{visual.main}"/>'
        f'<rect x="70" y="96" width="120" height="16" fill="{visual.main}"/>'
        + "".join(
            f'<circle cx="92" cy="{130 + i * 28}" r="7" fill="none" '
            f'stroke="{visual.accent}" stroke-width="3"/>'
            f'<line x1="108" y1="{130 + i * 28}" x2="168" y2="{130 + i * 28}" '
            f'stroke="{visual.accent}" stroke-width="4" stroke-linecap="round"/>'
            for i in range(3)
        )
    )


def create_hero_image(
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
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / "hero_adsense.svg"
    visual = _category_visual(
        category_id,
        category_name,
        category_keywords,
        style_seed=style_seed or f"{platform_id}:{topic_title}",
        topic_title=topic_title,
    )
    blog_title = extract_blog_title(blog_content, topic_title or f"{visual.keyword} 가이드")
    title_lines = _wrap_title_lines(blog_title, max_chars=16 if platform_id == "blogspot" else 18)
    chips = _topic_chips(topic_title or blog_title, visual.icons[1:])

    if visual.hero_mode == "photo":
        svg = _photo_hero_svg(
            visual=visual,
            blog_title=blog_title,
            title_lines=title_lines,
            chips=chips,
            category_name=category_name,
            topic_title=topic_title,
        )
    else:
        svg = _card_hero_svg(
            visual=visual,
            blog_title=blog_title,
            title_lines=title_lines,
            chips=chips,
            category_name=category_name,
            topic_title=topic_title,
        )

    file_path.write_text(svg, encoding="utf-8")

    alt = f"{blog_title} — {category_name} 핵심 기준을 한눈에 보여주는 대표 이미지"
    caption = f"{category_name} 대표 이미지입니다."

    return HeroImage(
        role="hero",
        status="local_ready_upload_required",
        file=str(file_path),
        alt=alt,
        caption=caption,
        prompt=(
            f"{'Photo-like' if visual.hero_mode == 'photo' else 'Card'} magazine hero "
            f"for {category_name}: {blog_title} style={visual.style_index % 8}"
        ),
        inserted_in="after_title",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _photo_hero_svg(
    *,
    visual: CategoryVisual,
    blog_title: str,
    title_lines: List[str],
    chips: Tuple[str, str, str],
    category_name: str,
    topic_title: str,
) -> str:
    """Cinematic full-bleed hero — an SVG gradient + accent shapes evoking a
    photo-like mood (no actual photograph/raster image involved), no platform stamp."""
    title_svg = "\n  ".join(
        f'<text x="96" y="{250 + i * 64}" font-size="54" fill="#ffffff" '
        f'font-family="\'Noto Sans KR\', \'NanumGothic\', Arial, sans-serif" font-weight="800">'
        f"{_escape_xml(line)}</text>"
        for i, line in enumerate(title_lines)
    )
    chip_y = 250 + len(title_lines) * 64 + 36
    chip_blocks = []
    x = 96
    for label in chips:
        w = max(100, 26 * len(label) + 40)
        chip_blocks.append(
            f'<rect x="{x}" y="{chip_y}" width="{w}" height="42" rx="21" '
            f'fill="#ffffff" fill-opacity="0.18" stroke="#ffffff" stroke-opacity="0.55"/>'
            f'<text x="{x + w / 2}" y="{chip_y + 28}" font-size="20" text-anchor="middle" '
            f'fill="#ffffff" font-family="\'Noto Sans KR\', \'NanumGothic\', Arial, sans-serif" '
            f'font-weight="700">{_escape_xml(label)}</text>'
        )
        x += w + 14
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720" role="img" aria-label="{_escape_xml(blog_title)} 대표 이미지">
  <defs>
    <linearGradient id="photoBg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{visual.main}"/>
      <stop offset="45%" stop-color="{visual.accent}"/>
      <stop offset="100%" stop-color="{visual.bg}"/>
    </linearGradient>
    <linearGradient id="vignette" x1="0" y1="0.5" x2="1" y2="0.5">
      <stop offset="0%" stop-color="#000000" stop-opacity="0.55"/>
      <stop offset="55%" stop-color="#000000" stop-opacity="0.18"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0.05"/>
    </linearGradient>
  </defs>
  <rect width="1280" height="720" fill="url(#photoBg)"/>
  <circle cx="980" cy="160" r="260" fill="#ffffff" opacity="0.12"/>
  <circle cx="1080" cy="560" r="180" fill="{visual.soft}" opacity="0.35"/>
  <circle cx="200" cy="620" r="220" fill="#000000" opacity="0.18"/>
  <rect width="1280" height="720" fill="url(#vignette)"/>
  <text x="96" y="150" font-size="24" fill="#ffffff" fill-opacity="0.9"
        font-family="'Noto Sans KR', 'NanumGothic', Arial, sans-serif" font-weight="700">{_escape_xml(category_name)}</text>
  {title_svg}
  {"".join(chip_blocks)}
  <text x="96" y="640" font-size="22" fill="#ffffff" fill-opacity="0.85"
        font-family="'Noto Sans KR', 'NanumGothic', Arial, sans-serif">{_escape_xml(_short_title(topic_title, max_len=40))}</text>
</svg>"""


def _card_hero_svg(
    *,
    visual: CategoryVisual,
    blog_title: str,
    title_lines: List[str],
    chips: Tuple[str, str, str],
    category_name: str,
    topic_title: str,
) -> str:
    title_svg = "\n  ".join(
        f'<text x="120" y="{220 + i * 62}" font-size="52" fill="{visual.main}" '
        f'font-family="\'Noto Sans KR\', \'NanumGothic\', Arial, sans-serif" font-weight="800">'
        f"{_escape_xml(line)}</text>"
        for i, line in enumerate(title_lines)
    )
    chip_y = 220 + len(title_lines) * 62 + 40
    chip_blocks = []
    x = 120
    for label in chips:
        w = max(110, 28 * len(label) + 48)
        chip_blocks.append(
            f'<rect x="{x}" y="{chip_y}" width="{w}" height="48" rx="24" '
            f'fill="#ffffff" stroke="{visual.accent}" stroke-width="2.5"/>'
            f'<text x="{x + w / 2}" y="{chip_y + 32}" font-size="22" text-anchor="middle" '
            f'fill="{visual.main}" font-family="\'Noto Sans KR\', \'NanumGothic\', Arial, sans-serif" '
            f'font-weight="700">{_escape_xml(label)}</text>'
        )
        x += w + 16
    chip_svg = "\n".join(chip_blocks)
    subtitle_y = chip_y + 78
    badge_inner = _badge_svg(visual)

    if visual.layout == "band-left":
        frame = (
            f'<rect x="0" y="0" width="220" height="720" fill="{visual.main}"/>'
            f'<rect x="220" y="64" width="988" height="592" rx="36" fill="#ffffff" opacity="0.94"/>'
        )
        badge_transform = 'transform="translate(920 210)"'
    elif visual.layout == "split-diagonal":
        frame = (
            f'<polygon points="0,0 780,0 520,720 0,720" fill="{visual.bg}"/>'
            f'<polygon points="780,0 1280,0 1280,720 520,720" fill="{visual.soft}"/>'
            f'<rect x="72" y="64" width="1136" height="592" rx="40" fill="#ffffff" opacity="0.88"/>'
        )
        badge_transform = 'transform="translate(900 200)"'
    else:
        frame = (
            f'<rect x="72" y="64" width="1136" height="592" rx="40" fill="url(#card)" filter="url(#shadow)"/>'
        )
        badge_transform = 'transform="translate(920 210)"'

    kicker = _escape_xml(category_name or visual.title)
    kicker_w = max(140, 22 * len(category_name or visual.title) + 56)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720" role="img" aria-label="{_escape_xml(blog_title)} 대표 이미지">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{visual.bg}"/>
      <stop offset="55%" stop-color="#ffffff"/>
      <stop offset="100%" stop-color="{visual.soft}"/>
    </linearGradient>
    <linearGradient id="card" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ffffff" stop-opacity="0.98"/>
      <stop offset="100%" stop-color="{visual.bg}" stop-opacity="0.55"/>
    </linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="14" stdDeviation="20" flood-color="#1a237e" flood-opacity="0.14"/>
    </filter>
  </defs>
  <rect width="1280" height="720" fill="url(#bg)"/>
  <circle cx="1120" cy="90" r="190" fill="{visual.accent}" opacity="0.22"/>
  <circle cx="90" cy="640" r="220" fill="{visual.main}" opacity="0.10"/>
  <circle cx="980" cy="560" r="70" fill="{visual.accent}" opacity="0.18"/>
  {frame}
  <rect x="120" y="108" width="{kicker_w}" height="44" rx="22" fill="{visual.main}"/>
  <text x="{120 + kicker_w / 2}" y="138" font-size="22" text-anchor="middle" fill="#ffffff"
        font-family="'Noto Sans KR', 'NanumGothic', Arial, sans-serif" font-weight="700">{kicker}</text>
  {title_svg}
  {chip_svg}
  <text x="120" y="{subtitle_y}" font-size="24" fill="#455a64"
        font-family="'Noto Sans KR', 'NanumGothic', Arial, sans-serif">{_escape_xml(_short_title(topic_title, max_len=36))}</text>
  <g {badge_transform}>
    <circle cx="130" cy="130" r="128" fill="{visual.main}" opacity="0.12"/>
    <circle cx="130" cy="130" r="112" fill="#ffffff"/>
    {badge_inner}
  </g>
</svg>"""


_KOREAN_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
)


def _korean_font(size: int):
    from PIL import ImageFont

    for path in _KOREAN_FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _draw_capsule_badge(img, draw, cx: int, cy: int, radius: int,
                        main_rgb, accent_rgb) -> None:
    """Flat illustration: white circle badge with a tilted two-tone capsule
    and a few small pills — friendly pharmacy-blog look."""
    from PIL import Image, ImageDraw

    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill="#ffffff")

    cap_w, cap_h = int(radius * 1.35), int(radius * 0.52)
    capsule = Image.new("RGBA", (cap_w + 40, cap_h + 40), (0, 0, 0, 0))
    cd = ImageDraw.Draw(capsule)
    cd.rounded_rectangle((20, 20, 20 + cap_w, 20 + cap_h), radius=cap_h // 2, fill=accent_rgb)
    cd.rounded_rectangle((20, 20, 20 + cap_w // 2, 20 + cap_h), radius=cap_h // 2, fill=main_rgb)
    cd.rectangle((20 + cap_w // 2 - cap_h // 2, 20, 20 + cap_w // 2, 20 + cap_h), fill=main_rgb)
    # Gloss line for a soft 3D feel.
    cd.rounded_rectangle(
        (34, 28, 20 + cap_w - 14, 28 + cap_h // 5),
        radius=cap_h // 10,
        fill=(255, 255, 255, 90),
    )
    capsule = capsule.rotate(28, expand=True, resample=Image.BICUBIC)
    img.paste(capsule, (cx - capsule.width // 2, cy - capsule.height // 2), capsule)

    for dx, dy, r in ((-radius + 34, radius - 52, 13), (radius - 46, -radius + 56, 11),
                      (radius - 62, radius - 40, 9)):
        draw.ellipse((cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r), fill=accent_rgb)
        draw.ellipse((cx + dx - r + 3, cy + dy - r + 3, cx + dx - 1, cy + dy - 1),
                     fill=(255, 255, 255, 140))


def _draw_checklist_badge(draw, cx: int, cy: int, radius: int, main_rgb, accent_rgb) -> None:
    """Flat illustration: white circle badge with a checklist card."""
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill="#ffffff")
    card_w, card_h = int(radius * 1.1), int(radius * 1.3)
    x0, y0 = cx - card_w // 2, cy - card_h // 2
    draw.rounded_rectangle((x0, y0, x0 + card_w, y0 + card_h), radius=18,
                           fill="#ffffff", outline=main_rgb, width=5)
    draw.rounded_rectangle((x0, y0, x0 + card_w, y0 + int(card_h * 0.22)), radius=18, fill=main_rgb)
    draw.rectangle((x0, y0 + int(card_h * 0.12), x0 + card_w, y0 + int(card_h * 0.22)), fill=main_rgb)
    line_y = y0 + int(card_h * 0.36)
    for i in range(3):
        ly = line_y + i * int(card_h * 0.2)
        r = 9
        draw.ellipse((x0 + 20 - r, ly - r, x0 + 20 + r, ly + r), outline=accent_rgb, width=4)
        draw.line([(x0 + 14, ly), (x0 + 19, ly + 6), (x0 + 27, ly - 5)], fill=accent_rgb, width=3)
        draw.line([(x0 + 42, ly), (x0 + card_w - 22, ly)], fill="#c8d6c9", width=6)


def render_hero_png(
    image_file: str,
    *,
    category_id: str = "",
    title: str = "",
    subtitle: str = "",
    platform_label: str = "",
    style_seed: str = "",
) -> str:
    """Render a friendly pastel hero banner as PNG.

    Light rounded card + flat illustration badge. WordPress rejects SVG
    uploads, so this PNG is what gets uploaded. Style is seeded per topic
    so consecutive health posts do not share the same look.
    """
    from PIL import Image, ImageDraw

    visual = _category_visual(
        category_id,
        (subtitle.split("·")[0].strip() if subtitle else "") or category_id or "블로그",
        None,
        style_seed=style_seed or f"{category_id}:{title}",
        topic_title=title,
    )
    bg, main, accent = visual.bg, visual.main, visual.accent

    def _rgb(hex_color: str):
        hex_color = hex_color.lstrip("#")
        return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))

    def _mix(color_a, color_b, ratio: float):
        return tuple(round(a + (b - a) * ratio) for a, b in zip(color_a, color_b))

    main_rgb, accent_rgb, bg_rgb = _rgb(main), _rgb(accent), _rgb(bg)
    soft_rgb = _rgb(visual.soft)
    deep_rgb = _mix(main_rgb, (0, 0, 0), 0.25)

    width, height = 1280, 720
    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")

    if visual.hero_mode == "photo":
        # Full-bleed cinematic gradient (mood art, not an actual photograph), white title.
        for y in range(height):
            t = y / max(height - 1, 1)
            row = tuple(round(a + (b - a) * t) for a, b in zip(main_rgb, accent_rgb))
            draw.line([(0, y), (width, y)], fill=row)
        draw.ellipse((820, -40, 1380, 420), fill=_mix(accent_rgb, (255, 255, 255), 0.35) + (90,))
        draw.ellipse((940, 420, 1400, 820), fill=soft_rgb + (100,))
        draw.rectangle((0, 0, width, height), fill=(0, 0, 0, 90))
        kicker = (subtitle.split("·")[0].strip() if subtitle else "") or ""
        if re.search(r"워드프레스|블로그스팟|wordpress|blogspot|blogger", kicker, re.I):
            kicker = ""
        margin_x = 96
        if kicker:
            draw.text((margin_x, 140), kicker, font=_korean_font(28), fill=(255, 255, 255, 230))
        title_text = (title or "블로그 가이드")[:80]
        title_font = _korean_font(58)
        title_lines: list[str] = []
        current = ""
        for word in title_text.split():
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=title_font) <= 700 or not current:
                current = candidate
            else:
                title_lines.append(current)
                current = word
            if len(title_lines) == 3:
                break
        if current and len(title_lines) < 3:
            title_lines.append(current)
        title_y = 220
        for i, line in enumerate(title_lines[:3]):
            draw.text((margin_x, title_y + i * 78), line, font=title_font, fill=(255, 255, 255))
        if subtitle and "·" in subtitle:
            draw.text(
                (margin_x, title_y + len(title_lines[:3]) * 78 + 24),
                subtitle.split("·", 1)[-1].strip()[:42],
                font=_korean_font(26),
                fill=(255, 255, 255, 210),
            )
    else:
        card = (48, 48, width - 48, height - 48)
        if visual.layout == "band-left":
            draw.rectangle((0, 0, 200, height), fill=main_rgb)
            draw.rounded_rectangle(card, radius=56, fill=bg_rgb)
        elif visual.layout == "split-diagonal":
            draw.polygon([(0, 0), (860, 0), (520, height), (0, height)], fill=bg_rgb)
            draw.polygon([(860, 0), (width, 0), (width, height), (520, height)], fill=soft_rgb)
            draw.rounded_rectangle(card, radius=48, fill=(255, 255, 255, 230))
        else:
            draw.rounded_rectangle(card, radius=56, fill=bg_rgb)

        draw.ellipse((card[2] - 340, card[1] - 120, card[2] + 120, card[1] + 320),
                     fill=_mix(bg_rgb, accent_rgb, 0.35) + (110,))
        draw.ellipse((card[0] - 110, card[3] - 200, card[0] + 200, card[3] + 110),
                     fill=_mix(bg_rgb, accent_rgb, 0.25) + (110,))
        for i in range(5):
            dx = card[0] + 70 + i * 26
            draw.ellipse((dx, card[1] + 56, dx + 8, card[1] + 64), fill=accent_rgb + (150,))

        margin_x = 150
        kicker = (subtitle.split("·")[0].strip() if subtitle else "") or ""
        if re.search(r"워드프레스|블로그스팟|wordpress|blogspot|blogger", kicker, re.I):
            kicker = ""
        if kicker:
            draw.text((margin_x, 160), kicker, font=_korean_font(30), fill=_mix(main_rgb, bg_rgb, 0.15))

        title_text = (title or "블로그 가이드")[:80]
        title_font = _korean_font(62)
        max_title_width = 620
        title_lines = []
        current = ""
        words = title_text.split()
        truncated = False
        for idx, word in enumerate(words):
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=title_font) <= max_title_width or not current:
                current = candidate
            else:
                title_lines.append(current)
                current = word
            if len(title_lines) == 3:
                truncated = idx < len(words) - 1 or bool(current)
                break
        if current and len(title_lines) < 3:
            title_lines.append(current)
        if truncated and title_lines:
            last = title_lines[-1]
            while last and draw.textlength(last + "…", font=title_font) > max_title_width:
                last = last[:-1]
            title_lines[-1] = last + "…"
        title_y = 228
        for i, line in enumerate(title_lines[:3]):
            draw.text((margin_x, title_y + i * 84), line, font=title_font, fill=deep_rgb)

        subtitle_y = title_y + len(title_lines[:3]) * 84 + 26
        if subtitle:
            draw.text((margin_x, subtitle_y), subtitle[:46], font=_korean_font(28),
                      fill=_mix(deep_rgb, bg_rgb, 0.35))

        badge_cx, badge_cy, badge_r = 990, 360, 185
        draw.ellipse(
            (badge_cx - badge_r - 12, badge_cy - badge_r - 12,
             badge_cx + badge_r + 12, badge_cy + badge_r + 12),
            fill=_mix(bg_rgb, accent_rgb, 0.4) + (160,),
        )
        if visual.badge_kind == "capsule":
            _draw_capsule_badge(img, draw, badge_cx, badge_cy, badge_r, main_rgb, accent_rgb)
            draw = ImageDraw.Draw(img, "RGBA")
        else:
            _draw_checklist_badge(draw, badge_cx, badge_cy, badge_r, main_rgb, accent_rgb)

    preferred = Path(image_file).with_suffix(".png")
    import tempfile

    handle = tempfile.NamedTemporaryFile(prefix="hero_", suffix=".png", delete=False)
    handle.close()
    try:
        img.save(handle.name, "PNG")
        try:
            img.save(str(preferred), "PNG")
        except OSError:
            pass
        return handle.name
    except OSError:
        img.save(str(preferred), "PNG")
        return str(preferred)


def insert_hero_image(*, platform_id: str, blog_content: str, image: HeroImage) -> str:
    if not image.file:
        return blog_content
    # Dedupe by the actual featured-image filename (title_card.png,
    # hero_adsense.svg, hero_ai.*, ...) rather than one hardcoded name, so
    # this works for whichever generator produced ``image``.
    image_name = re.escape(Path(image.file).name)
    if re.search(rf"!\[[^\]]+\]\([^)]*{image_name}\)", blog_content):
        return blog_content

    # Blogspot: hero image only — no "발행용 대표 이미지입니다" caption in the body.
    if platform_id == "blogspot":
        md = f"![{image.alt}]({image.file})"
    else:
        md = f"![{image.alt}]({image.file})\n\n> {image.caption}"

    if platform_id in ("blogspot", "tistory"):
        return re.sub(r"(^#\s+[^\n]+\n)", lambda m: f"{m.group(1)}\n{md}\n", blog_content, count=1, flags=re.M)

    note = f"대표 이미지 파일: {image.file}\n이미지 ALT: {image.alt}\n이미지 설명: {image.caption}"
    lines = blog_content.split("\n")
    idx = next((i for i, line in enumerate(lines) if re.match(r"^태그:", line)), -1)
    if idx >= 0:
        lines.insert(idx + 1, note)
        return "\n".join(lines)
    return f"{blog_content}\n\n{note}"
