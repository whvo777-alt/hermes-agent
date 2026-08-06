"""Situational accent colors + hero mode for blog visuals.

Colors and hero layout are chosen from the *meaning* of the heading/phrase
(and a light seed for variety) — never locked to a single yellow highlighter
or a single card template.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, Tuple

# Rainbow-ish highlighter marks (background under text) + matching ink.
_MARK_RAINBOW: Tuple[str, ...] = (
    "#ffe566",  # yellow
    "#90caf9",  # blue
    "#a5d6a7",  # green
    "#ffab91",  # orange
    "#ce93d8",  # purple
    "#80deea",  # cyan
    "#f48fb1",  # pink
    "#fff59d",  # soft gold
)

_INK_RAINBOW: Tuple[str, ...] = (
    "#111111",
    "#0d47a1",
    "#1b5e20",
    "#e65100",
    "#4a148c",
    "#006064",
    "#880e4f",
    "#5d4037",
)

# Soft tip-bar backgrounds that pair with the marks above.
_TIP_RAINBOW: Tuple[str, ...] = (
    "#f7f1e3",  # cream
    "#e3f2fd",  # blue mist
    "#e8f5e9",  # green mist
    "#fff3e0",  # orange mist
    "#f3e5f5",  # lilac mist
    "#e0f7fa",  # cyan mist
    "#fce4ec",  # rose mist
    "#fffde7",  # gold mist
)


def _seed_int(*parts: str) -> int:
    raw = "|".join(str(p or "") for p in parts)
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8], 16)


def classify_emphasis(text: str) -> str:
    """Map a phrase/heading to a semantic bucket."""
    value = str(text or "")
    if re.search(
        r"주의|위험|피해야|금지|부작용|이상\s*반응|전문가와?\s*상담|질환|약물|과다|경계|통증|멈추|"
        r"참고\s*버티|중단|의사|물리치료|한계|하지\s*말|피하",
        value,
    ):
        return "caution"
    if re.search(r"기대|도움|완화|가능|장점|좋은|권장|추천|효과적|편안", value):
        return "positive"
    if re.search(
        r"단계|방법|기록|비교|고친|실천|강도\s*조절|선택\s*기준|수정\s*문장|실행\s*단계|상황별\s*적용",
        value,
    ):
        return "method"
    if re.search(r"목표|체크|필수|핵심|중요|오늘|확인\s*질문|판단\s*기준", value):
        return "focus"
    if re.search(r"습관|루틴|집중|계획|생산|자기계발", value):
        return "growth"
    if re.search(r"요가|운동|스트레칭|걷기|수면|건강|영양|다이어트", value):
        return "body"
    return "neutral"


_SEMANTIC: Dict[str, Dict[str, str]] = {
    # caution → red / warm orange (never soft yellow)
    "caution": {"mark": "#ffcdd2", "ink": "#b71c1c", "tip": "#ffebee", "hero": "warm"},
    # positive expect → green
    "positive": {"mark": "#c8e6c9", "ink": "#1b5e20", "tip": "#e8f5e9", "hero": "fresh"},
    # how-to steps → blue
    "method": {"mark": "#bbdefb", "ink": "#0d47a1", "tip": "#e3f2fd", "hero": "cool"},
    # key criteria → gold / amber (can be yellow, but not the only option)
    "focus": {"mark": "#ffe082", "ink": "#e65100", "tip": "#fff8e1", "hero": "warm"},
    # self-dev growth → purple
    "growth": {"mark": "#e1bee7", "ink": "#6a1b9a", "tip": "#f3e5f5", "hero": "violet"},
    # body/health → teal / mint
    "body": {"mark": "#b2dfdb", "ink": "#00695c", "tip": "#e0f2f1", "hero": "fresh"},
    "neutral": {"mark": "", "ink": "#111111", "tip": "#f7f1e3", "hero": "neutral"},
}


def accent_for(text: str, *, seed: str = "") -> Dict[str, str]:
    """Return mark/ink/tip colors for a heading or bold phrase."""
    kind = classify_emphasis(text)
    base = dict(_SEMANTIC[kind])
    if kind == "neutral" or not base.get("mark"):
        idx = _seed_int(seed, text, kind) % len(_MARK_RAINBOW)
        base["mark"] = _MARK_RAINBOW[idx]
        base["ink"] = _INK_RAINBOW[idx]
        base["tip"] = _TIP_RAINBOW[idx]
    elif kind == "focus":
        # Focus can still rotate yellow / orange / gold so it is not one-note.
        idx = _seed_int(seed, text) % 3
        marks = ("#ffe566", "#ffcc80", "#fff176")
        inks = ("#111111", "#e65100", "#f9a825")
        tips = ("#f7f1e3", "#fff3e0", "#fffde7")
        base["mark"], base["ink"], base["tip"] = marks[idx], inks[idx], tips[idx]
    return base


def highlighter_style(text: str, *, seed: str = "", padding: str = "0 3px") -> str:
    accent = accent_for(text, seed=seed)
    return (
        "background-color:#f1f2ca;"
        f"color:{accent['ink']};font-weight:800;padding:{padding};"
    )


def strong_style(text: str, *, seed: str = "") -> str:
    """Inline **bold** style — caution red, all other emphasis dark blue."""
    kind = classify_emphasis(text)
    if kind == "caution":
        return "color:#ef5369;"
    return "color:#006dd7;"


def tip_bar_style(text: str, *, seed: str = "") -> str:
    accent = accent_for(text, seed=seed)
    return (
        f"margin:0.85em 0 0.35em;line-height:1.75;padding:0.7em 0.95em;"
        f"background:{accent['tip']};border-radius:6px;"
    )


def choose_hero_mode(*, topic_title: str = "", category_id: str = "", style_seed: str = "") -> str:
    """Pick "photo" (full-bleed gradient mood art, not an actual photograph) or
    "card" hero from topic situation + seed (roughly 50/50).

    Photo mode prefers experiential / body / travel-ish topics;
    card mode prefers checklists / criteria / comparison topics.
    A strong bias (|photo_bias - card_bias| >= 3) is trusted outright —
    no flip. A weak bias (diff == 1) still gets a seeded flip so
    consecutive posts in the same category don't clone. True ties
    (diff == 0) keep the original 50/50 seed coin-flip.
    """
    title = str(topic_title or "")
    photo_hits = len(re.findall(r"요가|운동|스트레칭|걷기|여행|맛집|수면|루틴|아침|사진|현장|분위기", title))
    card_hits = len(re.findall(r"기준|체크|비교|선택|리스트|확인|단계|가이드|방법|체크리스트", title))
    photo_bias = photo_hits * 2
    card_bias = card_hits * 2
    if category_id in {"travel", "health", "parenting"}:
        photo_bias += 1
    if category_id in {"finance", "it-tech", "self-dev"}:
        card_bias += 1
    roll = _seed_int(style_seed, topic_title, category_id) % 10
    bias_diff = abs(photo_bias - card_bias)
    if photo_bias > card_bias:
        if bias_diff >= 3:
            return "photo"
        return "photo" if roll >= 2 else "card"
    if card_bias > photo_bias:
        if bias_diff >= 3:
            return "card"
        return "card" if roll >= 2 else "photo"
    return "photo" if roll % 2 == 0 else "card"
