"""Section infographic generator.

Renders up to 5 in-body infographic cards from the blog markdown, one per
strong H2 section. Styles: grid (tables), checklist/timeline (ordered or
unordered item lists — the two are visually distinct but both a fair
representation of "a list of things"), gauge (a section built around one
standout stat), quote (pull-quote fallback for sections with no structured
data — used to keep the in-body image count at floor), before_after (나쁜
예/고친 예 contrast pairs). Style choice for shapes that have more than one
honest rendering (steps/numbered lists) is seeded via
``agent.content.images.style_rotation`` — the same seeding mechanism
``visual_accents.choose_hero_mode`` uses for hero selection — so hero and
section images share one picker instead of duplicating hash logic.

Cards never redraw the section's H2 title (the H2 already carries it in the
surrounding HTML) — only a short style label + marker glyph at the top, plus
the actual extracted data. Each ``InfographicSpec`` also carries
``strip_ranges``: the exact absolute line numbers of the source markdown
block it visualizes (table rows / step headings / numbered items / bullets /
나쁜예-고친예 pairs), so the caller can remove that block from the narrative
markdown before HTML conversion instead of leaving both the image AND the
original text in the published post. Quote-style cards are the one
exception — a pull quote intentionally repeats a sentence already in the
body, matching standard pull-quote convention, so nothing is stripped for
them.

Pure local PIL drawing — no LLM call, no network call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.content.images.hero_image import _korean_font  # shared font lookup
from agent.content.images.style_rotation import choose_style

_PALETTES: Dict[str, Tuple[str, str, str]] = {
    # (accent/main, deep header, light tint)
    "health": ("#2e7d32", "#1b4d21", "#f1f8f2"),
    "parenting": ("#ef6c00", "#9a4600", "#fdf6ef"),
    # Was blue (#1565c0) — a straight regression against the orange/purple
    # identity self-dev posts have actually shipped with (hero's rotation
    # includes "warm orange" and "lilac" for self-dev). Purple here, not
    # orange, so it doesn't collide with parenting's fixed orange above.
    "self-dev": ("#7b1fa2", "#4a148c", "#f5f0fa"),
    "it-tech": ("#3949ab", "#232c6e", "#f1f2fa"),
    "finance": ("#00695c", "#00443b", "#eff7f6"),
    "travel": ("#0277bd", "#014f7c", "#eff7fc"),
}
_DEFAULT_PALETTE = ("#37474f", "#232f36", "#f4f6f7")

_STYLE_LABELS: Dict[str, str] = {
    "grid": "비교",
    "checklist": "체크",
    "timeline": "단계",
    "gauge": "기준",
    "quote": "핵심",
    "before_after": "비교 전/후",
}

_SKIP_HEADING_RE = re.compile(r"출처|참고|마무리|결론|FAQ|자주 묻는|핵심\s*요약")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
_STEP_RE = re.compile(r"^###\s+(\d+단계\s*[:：]?\s*.+)$")
_NUMBERED_ITEM_RE = re.compile(r"^(\d+)\.\s+(.+)$")
_NUMBERED_DESC_RE = re.compile(r"^:\s*(.+)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")
_BAD_EXAMPLE_RE = re.compile(r"^나쁜\s*예\s*(?:→|->|:|：)\s*(.+)$")
_GOOD_EXAMPLE_RE = re.compile(r"^고친\s*예\s*(?:→|->|:|：)\s*(.+)$")
# Deliberately narrow: only units that are near-never ordinary sentence
# filler in Korean prose. An earlier version also matched 시간/분/회/세/일/년
# ("1일 섭취량" = "daily intake") and mis-fired on completely generic
# phrases — kg/kcal/mmHg/mg/%/ml/L are concrete measurements that don't
# double as common counter words, so they're a much more honest signal of
# "this section is actually built around one stat."
_GAUGE_STAT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|kcal|mmHg|mg|%|리터|L|ml)\b")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?다요])\s+")


@dataclass
class InfographicSpec:
    heading: str          # original markdown H2 text (used to locate the HTML anchor)
    display_title: str    # cleaned title — used for alt text only, never drawn on the card
    shape: str = ""        # "grid" | "before_after" | "ordered" | "bullets" | "quote" — the DATA shape;
                            # drives both which styles are honest for it and how it finalizes after allocation
    style: str = ""        # concrete render style, filled in by _allocate_styles() — empty until then
    eligible_styles: Tuple[str, ...] = ()  # every style that would honestly represent this section's data
    items: List[str] = field(default_factory=list)
    table: Optional[List[List[str]]] = None            # header + body rows, for "grid"
    gauge_stat: Optional[Tuple[str, str]] = None        # (number, unit), for "gauge"
    gauge_label: str = ""                               # the sentence the stat came from, for "gauge"
    before_pairs: List[Tuple[str, str]] = field(default_factory=list)  # (나쁜 예, 고친 예)
    quote_text: str = ""                                # for "quote"
    strip_ranges: List[Tuple[int, int]] = field(default_factory=list)  # absolute [start,end] line indices to drop from source markdown
    # "bullets"-shape only: the alternate checklist rendering (items/ranges),
    # used when allocation doesn't award this section "gauge".
    _checklist_items: List[str] = field(default_factory=list)
    _checklist_ranges: List[Tuple[int, int]] = field(default_factory=list)


def _clean_inline(text: str) -> str:
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _display_title(heading: str) -> str:
    return _clean_inline(re.sub(r"^\d+\.\s*", "", heading)).strip()


def _split_table_row(line: str) -> List[str]:
    return [_clean_inline(cell) for cell in line.strip().strip("|").split("|")]


def _extract_table_with_range(lines: List[str]) -> Optional[Tuple[List[List[str]], int, int]]:
    """Locate a markdown table within ``lines``; return (table, local_start, local_end)
    where local_start/local_end are the indices of the first/last row line actually
    consumed by the returned (possibly row-capped) table."""
    row_indices: List[int] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|"):
            row_indices.append(i)
        elif row_indices:
            break
    if len(row_indices) < 3:
        return None
    raw_rows = [lines[i].strip() for i in row_indices]
    header = _split_table_row(raw_rows[0])
    body_start = 1
    if _TABLE_SEP_RE.match(raw_rows[1]):
        body_start = 2
    body = [_split_table_row(r) for r in raw_rows[body_start:] if not _TABLE_SEP_RE.match(r)]
    body = [r for r in body if any(r)]
    if len(body) < 2:
        return None
    width = len(header)
    body = [(r + [""] * width)[:width] for r in body]
    kept_body_count = min(5, len(body))
    consumed = min(body_start + kept_body_count, len(row_indices))
    local_start, local_end = row_indices[0], row_indices[consumed - 1]
    return [header] + body[:5], local_start, local_end


def _extract_before_after(lines: List[str], *, base: int) -> Tuple[List[Tuple[str, str]], List[Tuple[int, int]]]:
    pairs: List[Tuple[str, str]] = []
    ranges: List[Tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        m_bad = _BAD_EXAMPLE_RE.match(lines[i].strip())
        if m_bad:
            for j in range(i + 1, min(i + 3, n)):
                m_good = _GOOD_EXAMPLE_RE.match(lines[j].strip())
                if m_good:
                    bad_text = _clean_inline(re.sub(r'^["“]|["”]$', "", m_bad.group(1).strip()))
                    good_text = _clean_inline(re.sub(r'^["“]|["”]$', "", m_good.group(1).strip()))
                    pairs.append((bad_text, good_text))
                    ranges.append((base + i, base + j))
                    i = j
                    break
        i += 1
    return pairs[:3], ranges


def _extract_steps(lines: List[str], *, base: int) -> Tuple[List[str], List[Tuple[int, int]]]:
    items: List[str] = []
    ranges: List[Tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = _STEP_RE.match(line)
        if m:
            items.append(_clean_inline(re.sub(r"^\d+단계\s*[:：]?\s*", "", m.group(1))))
            ranges.append((base + i, base + i))
    return items, ranges


def _extract_numbered(lines: List[str], *, base: int) -> Tuple[List[str], List[Tuple[int, int]]]:
    items: List[str] = []
    ranges: List[Tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        m = _NUMBERED_ITEM_RE.match(lines[i].strip())
        if m:
            end_i = i
            if i + 1 < n and _NUMBERED_DESC_RE.match(lines[i + 1].strip()):
                end_i = i + 1
            items.append(_clean_inline(m.group(2)))
            ranges.append((base + i, base + end_i))
            i = end_i
        i += 1
    return items, ranges


def _extract_bullets(lines: List[str], *, base: int) -> Tuple[List[str], List[Tuple[int, int]]]:
    items: List[str] = []
    ranges: List[Tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = _BULLET_RE.match(line)
        if m:
            items.append(_clean_inline(m.group(1)))
            ranges.append((base + i, base + i))
    return items, ranges


def _find_gauge_stat(items: List[str]) -> Optional[Tuple[str, str, str, List[str]]]:
    """A bullet list is gauge-eligible only when one item names a concrete
    stat (number + unit) — never fabricated, only ever a real match."""
    for idx, item in enumerate(items):
        m = _GAUGE_STAT_RE.search(item)
        if m:
            sub_items = [it for j, it in enumerate(items) if j != idx][:3]
            return m.group(1), m.group(2), item, sub_items
    return None


def _extract_quote(lines: List[str]) -> str:
    cleaned_lines = [re.sub(r"^[-*]\s+", "", l.strip()) for l in lines if l.strip() and not l.strip().startswith("#")]
    text = " ".join(cleaned_lines)
    if not text:
        return ""
    bold_match = re.search(r"\*\*(.{8,80}?)\*\*", text)
    if bold_match:
        return _clean_inline(bold_match.group(1))
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(_clean_inline(text)) if len(s.strip()) >= 12]
    return sentences[0][:80] if sentences else ""


def _build_spec_for_section(heading: str, section_lines: List[str], *, base: int) -> Optional[InfographicSpec]:
    display_title = _display_title(heading)

    table_info = _extract_table_with_range(section_lines)
    if table_info is not None:
        table, local_start, local_end = table_info
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="grid",
            eligible_styles=("grid",), table=table,
            strip_ranges=[(base + local_start, base + local_end)],
        )

    pairs, ba_ranges = _extract_before_after(section_lines, base=base)
    if pairs:
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="before_after",
            eligible_styles=("before_after",), before_pairs=pairs, strip_ranges=ba_ranges,
        )

    step_items, step_ranges = _extract_steps(section_lines, base=base)
    if len(step_items) >= 3:
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="ordered",
            eligible_styles=("timeline", "checklist"),
            items=step_items[:5], strip_ranges=step_ranges[:5],
        )

    numbered_items, num_ranges = _extract_numbered(section_lines, base=base)
    if len(numbered_items) >= 3:
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="ordered",
            eligible_styles=("timeline", "checklist"),
            items=numbered_items[:5], strip_ranges=num_ranges[:5],
        )

    bullet_items, bullet_ranges = _extract_bullets(section_lines, base=base)
    if len(bullet_items) >= 3:
        checklist_items, checklist_ranges = bullet_items[:5], bullet_ranges[:5]
        gauge = _find_gauge_stat(bullet_items)
        if gauge is not None:
            number, unit, label, sub_items = gauge
            gauge_ranges = [bullet_ranges[bullet_items.index(label)]] + [
                bullet_ranges[bullet_items.index(s)] for s in sub_items
            ]
            # Gauge is the preferred rendering when a concrete stat exists,
            # but a plain checklist of the same bullets is equally honest —
            # keeping both eligible gives the doc-level allocator (see
            # _allocate_styles) a real second option instead of forcing a
            # repeat when "gauge" is already used elsewhere in the post.
            return InfographicSpec(
                heading=heading, display_title=display_title, shape="bullets",
                eligible_styles=("gauge", "checklist"),
                items=sub_items, gauge_stat=(number, unit), gauge_label=label,
                strip_ranges=gauge_ranges,
                _checklist_items=checklist_items, _checklist_ranges=checklist_ranges,
            )
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="bullets",
            eligible_styles=("checklist",),
            items=checklist_items, strip_ranges=checklist_ranges,
        )

    quote_text = _extract_quote(section_lines)
    if quote_text:
        # Pull-quote convention: intentionally not stripped from the body.
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="quote",
            eligible_styles=("quote",), quote_text=quote_text, strip_ranges=[],
        )
    return None


def _allocate_styles(specs: List[InfographicSpec], *, style_seed: str) -> None:
    """Assign each spec's final concrete ``style``, avoiding a repeat within
    the same document until every honest option for that specific section is
    already used elsewhere in it.

    Shape-locked sections (a table can only honestly be "grid", a 나쁜예/고친예
    pair only "before_after", a plain paragraph only "quote") have exactly one
    eligible style — if that style is already used, the repeat is
    unavoidable without misrepresenting the section's actual data, so it's
    allowed rather than forced into a fabricated shape. Sections with real
    alternatives ("ordered" -> timeline/checklist, "bullets" with a stat ->
    gauge/checklist) get to dodge a collision by picking their still-unused
    option first.
    """
    used: set = set()
    for spec in specs:
        eligible = spec.eligible_styles or ("quote",)
        candidates = [s for s in eligible if s not in used]
        if not candidates:
            candidates = list(eligible)  # every honest option already used — unavoidable repeat
        chosen = candidates[0] if len(candidates) == 1 else choose_style(
            pool=candidates, seed_parts=(style_seed, spec.heading, "alloc"),
        )
        spec.style = chosen
        used.add(chosen)
        # Only swap in the alternate checklist rendering for "bullets" specs
        # that actually came from the dual-option (gauge-eligible) branch —
        # ``_checklist_items`` is empty for the plain checklist-only branch,
        # whose ``items``/``strip_ranges`` are already correct from
        # construction. Unconditionally overwriting here previously wiped a
        # checklist-only card down to just its label strip (caught by
        # rendering against a real draft: infographic_05.png came out
        # blank).
        if spec.shape == "bullets" and chosen == "checklist" and spec._checklist_items:
            spec.items = spec._checklist_items
            spec.strip_ranges = spec._checklist_ranges


def extract_infographic_specs(markdown: str, *, max_count: int = 5, style_seed: str = "") -> List[InfographicSpec]:
    """Pick H2 sections that tell a visual story, preferring variety.

    ``style_seed`` should be stable per-document (e.g. the topic title) so
    re-running the same draft reproduces the same style choices, while
    different posts land on different rotations.
    """
    lines_all = (markdown or "").split("\n")
    sections: List[Tuple[str, int, int]] = []
    current_heading = ""
    current_start: Optional[int] = None
    for i, line in enumerate(lines_all):
        match = re.match(r"^##\s+(.+)$", line)
        if match:
            if current_heading and current_start is not None:
                sections.append((current_heading, current_start, i))
            current_heading = match.group(1).strip()
            current_start = i
    if current_heading and current_start is not None:
        sections.append((current_heading, current_start, len(lines_all)))

    specs: List[InfographicSpec] = []
    for heading, start, end in sections:
        if _SKIP_HEADING_RE.search(heading):
            continue
        spec = _build_spec_for_section(heading, lines_all[start:end], base=start)
        if spec is not None:
            specs.append(spec)

    if not specs:
        return []

    # Prefer variety: one of each *structured* shape first (grid/before_after/
    # ordered/bullets — never quote here), then fill remaining slots from any
    # leftover structured sections in document order. "quote" is deliberately
    # excluded from both passes and only used to fill what's still missing
    # afterward — it's a last-resort floor-filler, and must never crowd out a
    # genuine second table/checklist/steps section just because it happened
    # to be scheduled in the same pass (a real ordering bug caught by testing
    # against a live draft: a numbered-list section was starved out because
    # the weaker "quote" fallback filled the last slot before leftover
    # structured sections got a chance). Selection is keyed on ``shape``, not
    # the final ``style`` — style isn't assigned until _allocate_styles runs,
    # below, on whichever sections actually get picked.
    structured_order = ("grid", "before_after", "ordered", "bullets")
    picked: List[InfographicSpec] = []
    used_ids: set = set()
    for shape in structured_order:
        for spec in specs:
            if spec.shape == shape and id(spec) not in used_ids:
                picked.append(spec)
                used_ids.add(id(spec))
                break
        if len(picked) >= max_count:
            break
    if len(picked) < max_count:
        leftovers = [s for s in specs if id(s) not in used_ids and s.shape != "quote"]
        while len(picked) < max_count and leftovers:
            picked.append(leftovers.pop(0))
            used_ids.add(id(picked[-1]))
    if len(picked) < max_count:
        quote_leftovers = [s for s in specs if id(s) not in used_ids and s.shape == "quote"]
        while len(picked) < max_count and quote_leftovers:
            picked.append(quote_leftovers.pop(0))
    picked = picked[:max_count]

    # Allocate concrete styles last, in original document order (not the
    # shape-grouped order above) — reading order is the most intuitive basis
    # for "who gets first claim on a style" when two sections could collide.
    doc_index = {id(s): i for i, s in enumerate(specs)}
    picked.sort(key=lambda s: doc_index[id(s)])
    _allocate_styles(picked, style_seed=style_seed)
    return picked


def _rgb(hex_color: str) -> Tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(color_a, color_b, ratio: float):
    return tuple(round(a + (b - a) * ratio) for a, b in zip(color_a, color_b))


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


def _draw_label_strip(draw, *, category_id: str, style: str) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """Small style label + marker glyph — replaces the old full title bar so
    the card never repeats the H2 heading text verbatim."""
    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    label = _STYLE_LABELS.get(style, "핵심")
    font = _korean_font(26)
    pad_x, pad_y = 20, 11
    tw = draw.textlength(label, font=font)
    box_h = 26 + pad_y * 2
    box_w = tw + pad_x * 2 + 30
    draw.rounded_rectangle((48, 34, 48 + box_w, 34 + box_h), radius=box_h / 2, fill=accent_rgb)
    mx, my = 48 + pad_x + 6, 34 + box_h / 2
    draw.polygon([(mx, my - 7), (mx, my + 7), (mx + 11, my)], fill="white")
    draw.text((mx + 22, 34 + pad_y), label, font=font, fill="white")
    return accent_rgb, deep_rgb


_HEADER_H = 100  # top margin reserved for the label strip (was 148 for the old title bar)


def _render_checklist_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    items = spec.items[:5]
    width = 1200
    row_h = 96
    height = _HEADER_H + 20 + len(items) * row_h + 40

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    accent_rgb, deep_rgb = _draw_label_strip(draw, category_id=category_id, style="checklist")
    pastel_line = _mix(accent_rgb, (255, 255, 255), 0.62)
    badge_fill = _mix(accent_rgb, (255, 255, 255), 0.70)

    item_font = _korean_font(30)
    y = _HEADER_H + 20
    for item in items:
        draw.rounded_rectangle((48, y, width - 48, y + row_h - 18), radius=20, fill="#ffffff", outline=pastel_line, width=3)
        cx, cy = 106, y + (row_h - 18) / 2
        draw.ellipse((cx - 25, cy - 25, cx + 25, cy + 25), fill=badge_fill)
        draw.line([(cx - 11, cy + 1), (cx - 3, cy + 10), (cx + 12, cy - 9)], fill=deep_rgb, width=5, joint="curve")
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


def _render_timeline_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    items = spec.items[:5]
    width = 1200
    height = 460

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    accent_rgb, deep_rgb = _draw_label_strip(draw, category_id=category_id, style="timeline")
    track_bg = _mix(accent_rgb, (255, 255, 255), 0.82)

    n = len(items)
    y = height // 2 + 30
    margin = 130
    span = width - margin * 2
    if n > 1:
        draw.line((margin, y, width - margin, y), fill=track_bg, width=10)
    label_font = _korean_font(24)
    num_font = _korean_font(34)
    for i, item in enumerate(items):
        x = margin + (span * i / (n - 1) if n > 1 else span / 2)
        r = 40
        fill = accent_rgb if i % 2 == 0 else deep_rgb
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)
        num = str(i + 1)
        box = draw.textbbox((0, 0), num, font=num_font)
        draw.text((x - (box[2] - box[0]) / 2 - box[0], y - (box[3] - box[1]) / 2 - box[1]), num, font=num_font, fill="white")
        lines = _wrap(draw, item, label_font, 210, 2)
        ty = y + r + 24
        for line in lines:
            tw = draw.textlength(line, font=label_font)
            draw.text((x - tw / 2, ty), line, font=label_font, fill="#37474f")
            ty += 32

    return _save_png(img, output_path)


def _render_gauge_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    width, height = 1200, 460
    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    accent_rgb, deep_rgb = _draw_label_strip(draw, category_id=category_id, style="gauge")
    track_bg = _mix(accent_rgb, (255, 255, 255), 0.82)

    number, unit = spec.gauge_stat or ("", "")
    cx, cy, r = 300, height // 2 + 30, 140
    draw.arc((cx - r, cy - r, cx + r, cy + r), start=140, end=400, fill=track_bg, width=28)
    draw.arc((cx - r, cy - r, cx + r, cy + r), start=140, end=140 + 260 * 0.62, fill=accent_rgb, width=28)
    num_font = _korean_font(76)
    unit_font = _korean_font(30)
    draw.text((cx, cy - 22), number, font=num_font, fill=deep_rgb, anchor="mm")
    draw.text((cx, cy + 44), unit, font=unit_font, fill=accent_rgb, anchor="mm")

    label_font = _korean_font(28)
    label_lines = _wrap(draw, spec.gauge_label, label_font, 560, 2)
    ty = 150
    for line in label_lines:
        draw.text((600, ty), line, font=label_font, fill="#37474f")
        ty += 38

    item_font = _korean_font(26)
    ty += 20
    for item in spec.items[:3]:
        draw.ellipse((600, ty + 4, 620, ty + 24), fill=accent_rgb)
        lines = _wrap(draw, item, item_font, 540, 1)
        draw.text((634, ty), lines[0] if lines else "", font=item_font, fill="#37474f")
        ty += 44

    return _save_png(img, output_path)


def _render_quote_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    width, height = 1200, 420

    img = Image.new("RGB", (width, height), deep_rgb)
    draw = ImageDraw.Draw(img, "RGBA")
    label = _STYLE_LABELS.get("quote", "핵심")
    label_font = _korean_font(24)
    draw.rounded_rectangle((48, 34, 48 + draw.textlength(label, font=label_font) + 56, 76), radius=21, fill=accent_rgb)
    draw.text((70, 44), label, font=label_font, fill="white")

    draw.text((70, 60), "“", font=_korean_font(150), fill=accent_rgb)
    quote_font = _korean_font(46)
    lines = _wrap(draw, spec.quote_text, quote_font, width - 200, 4)
    total_h = len(lines) * 62
    y = (height - total_h) // 2 + 30
    for line in lines:
        draw.text((100, y), line, font=quote_font, fill="white")
        y += 62

    return _save_png(img, output_path)


def _render_before_after_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    pairs = spec.before_pairs[:3]
    width = 1200
    row_h = 64
    height = 170 + len(pairs) * row_h + 50

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    accent_rgb, deep_rgb = _draw_label_strip(draw, category_id=category_id, style="before_after")

    mid = width // 2
    top = _HEADER_H + 20
    bottom = height - 30
    draw.rounded_rectangle((80, top, mid - 24, bottom), radius=20, fill=(255, 236, 236))
    draw.rounded_rectangle((mid + 24, top, width - 80, bottom), radius=20, fill=_mix(accent_rgb, (255, 255, 255), 0.88))

    head_font = _korean_font(26)
    draw.text((80 + (mid - 104) / 2, top + 22), "나쁜 예", font=head_font, fill=(198, 40, 40), anchor="ma")
    draw.text((mid + 24 + (mid - 104) / 2, top + 22), "고친 예", font=head_font, fill=deep_rgb, anchor="ma")

    item_font = _korean_font(24)
    ty = top + 76
    for bad, good in pairs:
        bad_lines = _wrap(draw, bad, item_font, mid - 104 - 24, 2)
        good_lines = _wrap(draw, good, item_font, mid - 104 - 24, 2)
        for i, line in enumerate(bad_lines):
            draw.text((104, ty + i * 30), line, font=item_font, fill=(120, 40, 40))
        for i, line in enumerate(good_lines):
            draw.text((mid + 48, ty + i * 30), line, font=item_font, fill="#1b3d2b")
        ty += max(len(bad_lines), len(good_lines)) * 30 + 24

    r = 34
    cy = (top + bottom) // 2
    draw.ellipse((mid - r, cy - r, mid + r, cy + r), fill=accent_rgb)
    draw.polygon([(mid - 8, cy - 12), (mid - 8, cy + 12), (mid + 14, cy)], fill="white")

    return _save_png(img, output_path)


def _render_grid_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    header_bg = _mix(accent_rgb, (255, 255, 255), 0.78)
    table = spec.table or [["항목", "내용"]]
    cols = max(len(table[0]), 1)
    rows = table

    width = 1200
    col_gap = 16
    left = 64
    usable = width - left * 2
    col_w = usable // cols
    row_h = 78
    height = _HEADER_H + 20 + len(rows) * row_h + 40

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_label_strip(draw, category_id=category_id, style="grid")

    card_top = _HEADER_H + 16
    card_bottom = height - 30
    draw.rounded_rectangle((48, card_top, width - 48, card_bottom), radius=22, fill="#ffffff",
                            outline=_mix(accent_rgb, (255, 255, 255), 0.55), width=3)

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
                color, font = deep_rgb, header_font
            else:
                if r_idx % 2 == 0:
                    draw.rounded_rectangle(cell_box, radius=10, fill=(248, 251, 248))
                color = "#c62828" if c_idx > 0 and re.search(r"주의|위험|피해야|상담|경계", cell) else "#37474f"
                if c_idx == 0:
                    color, font = deep_rgb, _korean_font(25)
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


_RENDERERS = {
    "grid": _render_grid_card,
    "checklist": _render_checklist_card,
    "timeline": _render_timeline_card,
    "gauge": _render_gauge_card,
    "quote": _render_quote_card,
    "before_after": _render_before_after_card,
}


def render_section_infographic(spec: InfographicSpec, output_path: str, *, category_id: str = "") -> str:
    """Draw one infographic card as a PNG and return the saved path."""
    renderer = _RENDERERS.get(spec.style, _render_checklist_card)
    return renderer(spec, output_path, category_id=category_id)


def build_section_infographics(*, markdown: str, category_id: str, output_dir: str,
                               max_count: int = 5, style_seed: str = "") -> List[Dict[str, object]]:
    """Render infographics for the strongest sections of the markdown.

    Returns [{"file", "heading", "alt", "style", "strip_ranges"}] where
    "heading" is the raw H2 text used to anchor the figure into the rendered
    HTML, "style" is the allocated render style (see module docstring), and
    "strip_ranges" are the absolute source-markdown line ranges the caller
    should remove before HTML conversion so the visualized data isn't left
    duplicated as plain text.
    """
    results: List[Dict[str, object]] = []
    for i, spec in enumerate(extract_infographic_specs(markdown, max_count=max_count, style_seed=style_seed), start=1):
        path = str(Path(output_dir) / f"infographic_{i:02d}.png")
        saved = render_section_infographic(spec, path, category_id=category_id)
        results.append(
            {
                "file": saved,
                "heading": spec.heading,
                "alt": f"{spec.display_title} 요약 인포그래픽",
                "style": spec.style,
                "strip_ranges": spec.strip_ranges,
            }
        )
    return results
