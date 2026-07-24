"""Shared seed-based style picker for in-body/hero card visuals.

Single source of truth for the "8-style pool" (6 structural card styles +
2 photo styles) so hero selection (``visual_accents.choose_hero_mode``) and
section-infographic selection (``section_infographics.py``) draw from the
same seeding mechanism instead of each rolling their own hash logic.

Section infographics only ever pick from :data:`SECTION_CARD_STYLES` — a
checklist/table/steps section cannot be honestly represented as a mood photo,
so the 2 photo styles are hero-only candidates (see :data:`PHOTO_STYLES`).
"""
from __future__ import annotations

from typing import Sequence

from agent.content.visual_accents import _seed_int

# Structural card styles — usable for both the hero and in-body section images.
SECTION_CARD_STYLES = ("grid", "checklist", "timeline", "gauge", "quote", "before_after")

# Mood/atmosphere styles — hero-only. A person/object photo cannot carry a
# table or checklist's actual data, so these never enter the section pool.
PHOTO_STYLES = ("photo_person", "photo_object")

# Full 8-style pool, documented in one place for anything that needs to
# reason about "all styles" (e.g. future hero-side wiring).
ALL_STYLES = SECTION_CARD_STYLES + PHOTO_STYLES


def choose_style(*, pool: Sequence[str], seed_parts: Sequence[str], exclude: Sequence[str] = ()) -> str:
    """Deterministically pick one style from ``pool`` based on ``seed_parts``.

    Same seed inputs -> same output (reproducible across re-runs of the same
    draft), different headings/topics -> different hash -> varied picks
    across a document. ``exclude`` narrows the pool (e.g. drop a style that
    doesn't fit the extracted data shape) without needing a second pool
    constant.
    """
    options = [s for s in pool if s not in exclude]
    if not options:
        options = list(pool)
    idx = _seed_int(*seed_parts) % len(options)
    return options[idx]
