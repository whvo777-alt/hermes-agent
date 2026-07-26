"""Small shared vector-icon vocabulary for the skinned section-card renderers.

No icon asset library exists in this repo (hero_image.py/title_card.py only
ever hand-draw shapes with PIL primitives, or use short text chips) — these
are the same kind of hand-drawn PIL vector glyphs, just factored out so every
skinned card renderer (checklist/steps/Q&A/etc., across section_infographics.py)
can share one small vocabulary instead of each re-inventing icon shapes.

Every draw function takes a bounding box and a single foreground color, and
draws centered within that box using only PIL primitives (ellipse/line/
polygon/rounded_rectangle) — no external image files, no fonts.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, Tuple

RGB = Tuple[int, int, int]

ICON_IDS = (
    "check", "bulb", "book", "clock", "footprint",
    "moon", "calendar", "shield", "pencil", "chart",
)


def _box_metrics(box: Tuple[float, float, float, float]) -> Tuple[float, float, float]:
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    size = min(x1 - x0, y1 - y0)
    return cx, cy, size


def _draw_check(draw, box, color: RGB, width: int) -> None:
    cx, cy, s = _box_metrics(box)
    r = s / 2
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=width)
    draw.line(
        [(cx - r * 0.45, cy + r * 0.05), (cx - r * 0.1, cy + r * 0.4), (cx + r * 0.5, cy - r * 0.35)],
        fill=color, width=width, joint="curve",
    )


def _draw_bulb(draw, box, color: RGB, width: int) -> None:
    cx, cy, s = _box_metrics(box)
    r = s * 0.3
    top = cy - s * 0.14
    draw.ellipse((cx - r, top - r, cx + r, top + r), outline=color, width=width)
    base_y = top + r * 0.92
    draw.line([(cx - r * 0.55, base_y), (cx - r * 0.32, base_y + s * 0.16), (cx + r * 0.32, base_y + s * 0.16), (cx + r * 0.55, base_y)],
              fill=color, width=width, joint="curve")
    for i in range(3):
        ly = base_y + s * 0.2 + i * (s * 0.075)
        draw.line((cx - r * 0.3, ly, cx + r * 0.3, ly), fill=color, width=max(1, width - 1))


def _draw_book(draw, box, color: RGB, width: int) -> None:
    cx, cy, s = _box_metrics(box)
    hw, hh = s * 0.42, s * 0.32
    draw.line((cx, cy - hh, cx, cy + hh), fill=color, width=width)
    draw.line([(cx, cy - hh * 0.7), (cx - hw, cy - hh), (cx - hw, cy + hh), (cx, cy + hh * 0.7)], fill=color, width=width, joint="curve")
    draw.line([(cx, cy - hh * 0.7), (cx + hw, cy - hh), (cx + hw, cy + hh), (cx, cy + hh * 0.7)], fill=color, width=width, joint="curve")


def _draw_clock(draw, box, color: RGB, width: int) -> None:
    cx, cy, s = _box_metrics(box)
    r = s / 2
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=width)
    draw.line((cx, cy, cx, cy - r * 0.55), fill=color, width=width)
    draw.line((cx, cy, cx + r * 0.4, cy + r * 0.1), fill=color, width=width)


def _draw_footprint(draw, box, color: RGB, width: int) -> None:
    """Minimal two-print glyph (front/ball print larger, heel print smaller,
    offset diagonally) -- the common simplified "footprint" pictogram shape;
    a literal toes+sole composition didn't read cleanly at icon size."""
    cx, cy, s = _box_metrics(box)
    r1, r2 = s * 0.22, s * 0.16
    draw.ellipse((cx - r1 + s * 0.1, cy - r1 - s * 0.14, cx + r1 + s * 0.1, cy + r1 - s * 0.14), outline=color, width=width)
    draw.ellipse((cx - r2 - s * 0.14, cy - r2 + s * 0.16, cx + r2 - s * 0.14, cy + r2 + s * 0.16), outline=color, width=width)


def _draw_moon(draw, box, color: RGB, width: int) -> None:
    """Filled crescent -- built as a closed polygon from two offset arcs
    rather than a circle-minus-circle cutout, since the drawer only fills
    with ``color`` and has no way to know (or punch through to) whatever
    background sits behind it.
    """
    import math

    cx, cy, s = _box_metrics(box)
    r_outer = s * 0.42
    r_inner = s * 0.36
    inner_offset = s * 0.22  # shifts the "biting" circle right -> crescent opens left

    points = []
    for deg in range(100, 261, 10):
        a = math.radians(deg)
        points.append((cx + r_outer * math.cos(a), cy + r_outer * math.sin(a)))
    for deg in range(261, 99, -10):
        a = math.radians(deg)
        points.append((cx + inner_offset + r_inner * math.cos(a), cy + r_inner * math.sin(a)))
    draw.polygon(points, fill=color)


def _draw_calendar(draw, box, color: RGB, width: int) -> None:
    cx, cy, s = _box_metrics(box)
    hw, hh = s * 0.42, s * 0.36
    draw.rounded_rectangle((cx - hw, cy - hh, cx + hw, cy + hh), radius=4, outline=color, width=width)
    draw.line((cx - hw, cy - hh * 0.25, cx + hw, cy - hh * 0.25), fill=color, width=width)
    draw.line((cx - hw * 0.5, cy - hh - 4, cx - hw * 0.5, cy - hh + 6), fill=color, width=width)
    draw.line((cx + hw * 0.5, cy - hh - 4, cx + hw * 0.5, cy - hh + 6), fill=color, width=width)


def _draw_shield(draw, box, color: RGB, width: int) -> None:
    cx, cy, s = _box_metrics(box)
    hw, top, bottom = s * 0.38, cy - s * 0.42, cy + s * 0.44
    shoulder_y = top + s * 0.14
    draw.line(
        [(cx - hw, shoulder_y), (cx - hw, top), (cx + hw, top), (cx + hw, shoulder_y),
         (cx + hw * 0.85, cy + s * 0.1), (cx, bottom), (cx - hw * 0.85, cy + s * 0.1),
         (cx - hw, shoulder_y)],
        fill=color, width=width, joint="curve",
    )


def _draw_pencil(draw, box, color: RGB, width: int) -> None:
    import math

    cx, cy, s = _box_metrics(box)
    half = s * 0.38
    body_w = s * 0.1
    angle = math.radians(-45)
    dx, dy = math.cos(angle), math.sin(angle)
    px, py = -dy, dx  # perpendicular unit vector, for body thickness
    x0, y0 = cx - half * dx, cy - half * dy       # eraser end
    x1, y1 = cx + half * 0.55 * dx, cy + half * 0.55 * dy  # where the tip cone starts
    tip_x, tip_y = cx + half * dx, cy + half * dy  # pencil point
    hw = body_w / 2
    draw.polygon(
        [(x0 - hw * px, y0 - hw * py), (x1 - hw * px, y1 - hw * py),
         (tip_x, tip_y), (x1 + hw * px, y1 + hw * py), (x0 + hw * px, y0 + hw * py)],
        outline=color, width=width,
    )
    draw.line((x0 - hw * 1.6 * px, y0 - hw * 1.6 * py, x0 + hw * 1.6 * px, y0 + hw * 1.6 * py), fill=color, width=width)


def _draw_chart(draw, box, color: RGB, width: int) -> None:
    cx, cy, s = _box_metrics(box)
    base_y = cy + s * 0.4
    bar_w = s * 0.18
    heights = (0.35, 0.65, 0.5, 0.85)
    start_x = cx - s * 0.45
    for i, h in enumerate(heights):
        x = start_x + i * (bar_w + s * 0.08)
        draw.rectangle((x, base_y - s * h, x + bar_w, base_y), outline=color, width=width)


_DRAWERS: Dict[str, Callable] = {
    "check": _draw_check,
    "bulb": _draw_bulb,
    "book": _draw_book,
    "clock": _draw_clock,
    "footprint": _draw_footprint,
    "moon": _draw_moon,
    "calendar": _draw_calendar,
    "shield": _draw_shield,
    "pencil": _draw_pencil,
    "chart": _draw_chart,
}


def draw_icon(draw, icon_id: str, box: Tuple[float, float, float, float], *, color: RGB, width: int = 3) -> None:
    """Draw ``icon_id`` centered in ``box`` (x0, y0, x1, y1) using ``color``.

    Unknown ``icon_id`` falls back to "check" rather than raising — a
    section always gets *some* icon instead of a rendering crash over a
    cosmetic detail.
    """
    fn = _DRAWERS.get(icon_id, _draw_check)
    fn(draw, box, color, width)


# Keyword -> icon id, checked in order (first match wins). Deliberately small
# and coarse -- this is decoration, not a classifier; a wrong-but-plausible
# icon is harmless, so ambiguous text just falls through to the default.
_KEYWORD_ICON_MAP: Tuple[Tuple[str, str], ...] = (
    (r"수면|잠|밤|취침", "moon"),
    (r"걷기|산책|운동|스트레칭|자세", "footprint"),
    (r"기록|메모|일지|적", "pencil"),
    (r"책|독서|공부|학습", "book"),
    (r"시간|분|초|타이머|일정", "clock"),
    (r"달력|주|요일|월간|매일", "calendar"),
    (r"주의|위험|안전|보호|경고", "shield"),
    (r"확인|점검|완료|체크", "check"),
    (r"통계|비율|수치|퍼센트|%", "chart"),
)


def pick_icon_for_text(text: str) -> str:
    """Best-effort keyword match against ``text`` (heading or item label).

    Falls back to "bulb" (a neutral "tip/idea" glyph) when nothing matches.
    """
    value = text or ""
    for pattern, icon_id in _KEYWORD_ICON_MAP:
        if re.search(pattern, value):
            return icon_id
    return "bulb"
