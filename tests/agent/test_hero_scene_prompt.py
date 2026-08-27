"""Regression tests for topic-specific hero scenes and photo styles."""

from __future__ import annotations

import pytest

from agent.content.images import ai_hero_image


def _fallback_prompt(*, seed: str, hero_mode: str = "photo") -> str:
    return ai_hero_image.build_hero_prompt(
        category_id="health",
        hero_mode=hero_mode,
        style_seed=seed,
        topic_title="",
        category_name="건강",
    )


def _photo_style(prompt: str) -> str:
    styles = getattr(ai_hero_image, "_PHOTO_STYLES", ())
    matches = [style for style in styles if style in prompt]
    assert len(matches) == 1
    return matches[0]


def test_photo_styles_have_four_variants():
    assert len(getattr(ai_hero_image, "_PHOTO_STYLES", ())) == 4


def test_same_seed_uses_same_photo_style():
    first = _photo_style(_fallback_prompt(seed="same-seed"))
    second = _photo_style(_fallback_prompt(seed="same-seed"))

    assert first == second


def test_multiple_seeds_use_multiple_photo_styles():
    styles = {
        _photo_style(_fallback_prompt(seed=f"seed-{index}"))
        for index in range(16)
    }

    assert len(styles) >= 2


def test_scene_prompt_requires_the_specific_title_subject():
    prompt = ai_hero_image._SCENE_SYSTEM_PROMPT

    assert "The scene MUST show the specific object, tool, food, place, or activity" in prompt
    assert "a bodyweight squat is not a stationary bike" in prompt
    assert "a quotation form is not sticky notes" in prompt
    assert "a Gantt chart is not an empty notebook" in prompt


def test_scene_prompt_does_not_force_every_scene_into_a_flat_lay():
    prompt = ai_hero_image._SCENE_SYSTEM_PROMPT

    assert "suitable for a clean editorial flat-lay or lifestyle photo" not in prompt
    assert "Do not default to a flat-lay every time." in prompt


def test_scene_prompt_treats_time_and_body_state_as_context():
    prompt = ai_hero_image._SCENE_SYSTEM_PROMPT

    assert "If the title also names a time of day, a season, or a body state" in prompt
    assert "The main subject stays the thing the title is about." in prompt


def test_card_mode_keeps_the_existing_card_style():
    prompt = _fallback_prompt(seed="card-seed", hero_mode="card")

    assert prompt.endswith(f", {ai_hero_image._CARD_STYLE}. {ai_hero_image._NEGATIVE}.")
    for style in getattr(ai_hero_image, "_PHOTO_STYLES", ()):
        assert style not in prompt


def test_empty_topic_title_does_not_call_llm(monkeypatch):
    calls = []

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("empty topic_title must not call _generate_llm_scene")

    monkeypatch.setattr(ai_hero_image, "_generate_llm_scene", fail_if_called)

    prompt = _fallback_prompt(seed="no-llm-seed")

    assert calls == []
    assert prompt
