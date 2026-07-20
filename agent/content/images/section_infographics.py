"""Section infographic generator.

Renders 3–5 in-body infographic cards from the blog markdown.
Variants: checklist, steps, comparison (from markdown tables), tips.
Pure local PIL drawing — no LLM call, no network call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.content.images.hero_image import _korean_font  # shared font lookup

_PALETTES: Dict[str, Tuple[str, str, str]] = {
    # (accent/main, deep header, light tint)
    "health": ("#2e7d32", "#1b4d21", "#f1f8f2"),
    "parenting": ("#ef6c00", "#9a4600", "#fdf6ef"),
    "self-dev": ("#1565c0", "#0d3d78", "#f0f5fb"),
    "it-tech": ("#3949ab", "#232c6e", "#f1f2fa"),
    "finance": ("#00695c", "#00443b", "#eff7f6"),
    "travel": ("#0277bd", "#014f7c", "#eff7fc"),
}
_DEFAULT_PALETTE = ("#37474f", "#232f36", "#f4f6f7")

_SKIP_HEADING_RE = re.compile(r"출처|참고|마무리|결론|FAQ|자주 묻는")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


@dataclass
class InfographicSpec:
    heading: str          # original markdown H2 text (used to locate the HTML anchor)
    display_title: str    # cleaned title drawn on the card
    variant: str          # "steps" | "checklist" | "comparison" | "tips"
    items: List[str] = field(default_factory=list)
    table: Optional[List[List[str]]] = None  # header + body rows for comparison


def _clean_inline(text: str) -> str:
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _display_title(heading: str) -> str:
    return _clean_inline(re.sub(r"^\d+\.\s*", "", heading)).strip()


def _split_table_row(line: str) -> List[str]:
    return [_clean_inline(cell) for cell in line.strip().strip("|").split("|")]


def _extract_table(lines: List[str]) -> Optional[List[List[str]]]:
    rows: List[str] = []
    for line in lines:
        if line.lstrip().startswith("|"):
            rows.append(line.strip())
        elif rows:
            break
    if len(rows) < 3:
        return None
    header = _split_table_row(rows[0])
    body_start = 1
    if _TABLE_SEP_RE.match(rows[1]):
        body_start = 2
    body = [_split_table_row(r) for r in rows[body_start:] if not _TABLE_SEP_RE.match(r)]
    body = [r for r in body if any(r)]
    if len(body) < 2:
        return None
    # Normalize column count to header width.
    width = len(header)
    body = [(r + [""] * width)[:width] for r in body]
    return [header] + body[:5]


def extract_infographic_specs(markdown: str, *, max_count: int = 5) -> List[InfographicSpec]:
    """Pick H2 sections that tell a visual story, preferring variety."""
    sections: List[Tuple[str, List[str]]] = []
    current_heading = ""
    current_lines: List[str] = []
    for line in (markdown or "").split("\n"):
        match = re.match(r"^##\s+(.+)$", line)
        if match:
            if current_heading:
                sections.append((current_heading, current_lines))
            current_heading = match.group(1).strip()
            current_lines = []
        elif current_heading:
            current_lines.append(line)
    if current_heading:
        sections.append((current_heading, current_lines))

    specs: List[InfographicSpec] = []
    for heading, lines in sections:
        if _SKIP_HEADING_RE.search(heading):
            continue

        table = _extract_table(lines)
        step_titles = [
            _clean_inline(re.sub(r"^\d+단계\s*[::]\s*", "", m.group(1)))
            for line in lines
            if (m := re.match(r"^###\s+(\d+단계[::]?\s*.+)$", line))
        ]
        bullets: List[str] = []
        numbered: List[str] = []
        for line in lines:
            m = re.match(r"^[-*]\s+(.+)$", line)
            if m:
                bullets.append(_clean_inline(m.group(1)))
                continue
            m = re.match(r"^\d+\.\s+(.+)$", line)
            if m:
                numbered.append(_clean_inline(m.group(1)))

        if table is not None:
            specs.append(
                InfographicSpec(
                    heading=heading,
                    display_title=_display_title(heading),
                    variant="comparison",
                    items=[],
                    table=table,
                )
            )
        elif len(step_titles) >= 3:
            specs.append(
                InfographicSpec(
                    heading=heading,
                    display_title=_display_title(heading),
                    variant="steps",
                    items=step_titles[:5],
                )
            )
        elif len(numbered) >= 3:
            specs.append(
                InfographicSpec(
                    heading=heading,
                    display_title=_display_title(heading),
                    variant="tips",
                    items=numbered[:5],
                )
            )
        elif len(bullets) >= 3:
            specs.append(
                InfographicSpec(
                    heading=heading,
                    display_title=_display_title(heading),
                    variant="checklist",
                    items=bullets[:5],
                )
            )

    if not specs:
        return []

    # Prefer variety: one comparison, one steps/tips, then fill remaining.
    preferred_order = ("comparison", "steps", "tips", "checklist")
    picked: List[InfographicSpec] = []
    used_ids: set = set()
    for variant in preferred_order:
        for spec in specs:
            if spec.variant == variant and id(spec) not in used_ids:
                picked.append(spec)
                used_ids.add(id(spec))
                break
        if len(picked) >= max_count:
            break
    # Spread remaining slots across leftover sections.
    leftovers = [s for s in specs if id(s) not in used_ids]
    while len(picked) < max_count and leftovers:
        if len(leftovers) == 1:
            picked.append(leftovers.pop(0))
            break
        step = max(1, len(leftovers) // (max_count - len(picked) + 1))
        picked.append(leftovers.pop(0))
        leftovers = leftovers[step - 1:]
    return picked[:max_count]


def _rgb(hex_color: str) -> Tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _wrap(draw, text: str, font, max_width: int, max_lines: int) -> List[str]:
    lines: List[str] = []
    current = ""
    for word in (text or "").split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and lines and draw.textlength(lines[-1], font=font) > max_width:
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def _draw_header(draw, *, width: int, header_h: int, title: str,
                 accent_rgb, deep_rgb, pastel_rgb) -> None:
    draw.rounded_rectangle((48, 34, width - 48, header_h), radius=30, fill=pastel_rgb)
    title_font = _korean_font(38)
    title_lines = _wrap(draw, title, title_font, width - 300, 2)
    band_cy = (34 + header_h) / 2
    title_y = band_cy - len(title_lines) * 26
    marker_y = band_cy - 16
    draw.polygon(
        [(96, marker_y), (96, marker_y + 32), (122, marker_y + 16)],
        fill=accent_rgb,
    )
    for i, line in enumerate(title_lines):
        draw.text((144, title_y + i * 52), line, font=title_font, fill=deep_rgb)


def _render_list_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)

    def _mix(color_a, color_b, ratio: float):
        return tuple(round(a + (b - a) * ratio) for a, b in zip(color_a, color_b))

    pastel_rgb = _mix(accent_rgb, (255, 255, 255), 0.82)
    pastel_line = _mix(accent_rgb, (255, 255, 255), 0.62)
    badge_fill = _mix(accent_rgb, (255, 255, 255), 0.70)
    tip_fill = (255, 248, 196)  # soft yellow highlighter strip

    width = 1200
    header_h = 148
    row_h = 96
    items = spec.items[:5]
    height = header_h + 42 + len(items) * row_h + 40

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_header(
        draw, width=width, header_h=header_h, title=spec.display_title,
        accent_rgb=accent_rgb, deep_rgb=deep_rgb, pastel_rgb=pastel_rgb,
    )

    item_font = _korean_font(30)
    badge_font = _korean_font(28)
    y = header_h + 42
    for index, item in enumerate(items):
        fill = tip_fill if spec.variant == "tips" else "#ffffff"
        draw.rounded_rectangle(
            (48, y, width - 48, y + row_h - 18),
            radius=20,
            fill=fill,
            outline=pastel_line,
            width=3,
        )
        cx, cy = 106, y + (row_h - 18) / 2
        draw.ellipse((cx - 25, cy - 25, cx + 25, cy + 25), fill=badge_fill)
        if spec.variant in ("steps", "tips"):
            badge = str(index + 1)
            box = draw.textbbox((0, 0), badge, font=badge_font)
            draw.text(
                (cx - (box[2] - box[0]) / 2 - box[0], cy - (box[3] - box[1]) / 2 - box[1]),
                badge,
                font=badge_font,
                fill=deep_rgb,
            )
        else:
            draw.line(
                [(cx - 11, cy + 1), (cx - 3, cy + 10), (cx + 12, cy - 9)],
                fill=deep_rgb,
                width=5,
                joint="curve",
            )
        text_lines = _wrap(draw, item, item_font, width - 280, 2)
        if len(text_lines) == 1:
            draw.text((162, cy - 21), text_lines[0], font=item_font, fill="#37474f")
        else:
            small_font = _korean_font(24)
            small_lines = _wrap(draw, item, small_font, width - 280, 2)
            for i, line in enumerate(small_lines):
                draw.text((162, y + 12 + i * 32), line, font=small_font, fill="#37474f")
        y += row_h

    return _save_png(img, output_path)


def _render_comparison_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)

    def _mix(color_a, color_b, ratio: float):
        return tuple(round(a + (b - a) * ratio) for a, b in zip(color_a, color_b))

    pastel_rgb = _mix(accent_rgb, (255, 255, 255), 0.82)
    header_bg = _mix(accent_rgb, (255, 255, 255), 0.78)
    table = spec.table or [["항목", "내용"]]
    cols = max(len(table[0]), 1)
    rows = table

    width = 1200
    header_h = 148
    col_gap = 16
    left = 64
    usable = width - left * 2
    col_w = usable // cols
    row_h = 78
    height = header_h + 36 + len(rows) * row_h + 48

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_header(
        draw, width=width, header_h=header_h, title=spec.display_title,
        accent_rgb=accent_rgb, deep_rgb=deep_rgb, pastel_rgb=pastel_rgb,
    )

    # Outer rounded card.
    card_top = header_h + 28
    card_bottom = height - 36
    draw.rounded_rectangle(
        (48, card_top, width - 48, card_bottom),
        radius=22,
        fill="#ffffff",
        outline=_mix(accent_rgb, (255, 255, 255), 0.55),
        width=3,
    )

    header_font = _korean_font(26)
    cell_font = _korean_font(24)
    y = card_top + 10
    for r_idx, row in enumerate(rows):
        x = left
        for c_idx in range(cols):
            cell = row[c_idx] if c_idx < len(row) else ""
            cell_box = (x, y, x + col_w - col_gap, y + row_h - 12)
            if r_idx == 0:
                draw.rounded_rectangle(cell_box, radius=12, fill=header_bg)
                color = deep_rgb
                font = header_font
            else:
                if r_idx % 2 == 0:
                    draw.rounded_rectangle(cell_box, radius=10, fill=(248, 251, 248))
                color = "#c62828" if c_idx > 0 and re.search(r"주의|위험|피해야|상담|경계", cell) else "#37474f"
                if c_idx == 0:
                    color = deep_rgb
                    font = _korean_font(25)
                else:
                    font = cell_font
            lines = _wrap(draw, cell, font, col_w - col_gap - 24, 2)
            text_y = y + (row_h - 12) / 2 - len(lines) * 16
            for i, line in enumerate(lines):
                draw.text((x + 14, text_y + i * 32), line, font=font, fill=color)
            x += col_w
        y += row_h

    return _save_png(img, output_path)


def _save_png(img, output_path: str) -> str:
    preferred = Path(output_path)
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(preferred), "PNG")
        return str(preferred)
    except OSError:
        import tempfile

        handle = tempfile.NamedTemporaryFile(prefix="infographic_", suffix=".png", delete=False)
        handle.close()
        img.save(handle.name, "PNG")
        return handle.name


def render_section_infographic(spec: InfographicSpec, output_path: str, *,
                               category_id: str = "") -> str:
    """Draw one infographic card as a PNG and return the saved path."""
    if spec.variant == "comparison" and spec.table:
        return _render_comparison_card(spec, output_path, category_id=category_id)
    return _render_list_card(spec, output_path, category_id=category_id)


def build_section_infographics(*, markdown: str, category_id: str,
                               output_dir: str, max_count: int = 5) -> List[Dict[str, str]]:
    """Render infographics for the strongest sections of the markdown.

    Returns [{"file", "heading", "alt"}] where "heading" is the raw H2 text
    used to anchor the figure into the rendered HTML.
    """
    results: List[Dict[str, str]] = []
    for i, spec in enumerate(extract_infographic_specs(markdown, max_count=max_count), start=1):
        path = str(Path(output_dir) / f"infographic_{i:02d}.png")
        saved = render_section_infographic(spec, path, category_id=category_id)
        results.append(
            {
                "file": saved,
                "heading": spec.heading,
                "alt": f"{spec.display_title} 요약 인포그래픽",
            }
        )
    return results
