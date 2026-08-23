"""Regression tests for AI hero selection and title-card fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.content import orchestrator
from agent.content.images import ai_hero_image


@pytest.fixture
def hero_kwargs(tmp_path):
    return {
        "out_dir": tmp_path,
        "platform_id": "wordpress",
        "platform_label": "WordPress",
        "category_id": "health",
        "category_name": "건강/헬스",
        "category_keywords": ["운동"],
        "topic_title": "운동",
        "blog_content": "# 운동\n\n본문",
        "style_seed": "seed",
    }


def test_non_ai_mode_uses_title_card(monkeypatch, hero_kwargs):
    expected = object()
    calls = []

    monkeypatch.setattr(ai_hero_image, "_is_ai_hero_enabled", lambda: False)
    monkeypatch.setattr(
        orchestrator,
        "create_title_card",
        lambda **kwargs: calls.append(kwargs) or expected,
    )

    result = orchestrator._make_hero_image(**hero_kwargs)

    assert result is expected
    assert calls == [hero_kwargs]


def test_ai_mode_uses_real_ai_hero(monkeypatch, hero_kwargs):
    ai_result = SimpleNamespace(file=str(hero_kwargs["out_dir"] / "hero_ai.png"))
    title_calls = []

    monkeypatch.setattr(ai_hero_image, "_is_ai_hero_enabled", lambda: True)
    monkeypatch.setattr(ai_hero_image, "create_hero_image_ai", lambda **kwargs: ai_result)
    monkeypatch.setattr(
        orchestrator,
        "create_title_card",
        lambda **kwargs: title_calls.append(kwargs),
    )

    result = orchestrator._make_hero_image(**hero_kwargs)

    assert result is ai_result
    assert title_calls == []


def test_ai_mode_falls_back_when_ai_returns_non_ai_file(monkeypatch, hero_kwargs):
    fallback = object()
    ai_result = SimpleNamespace(file=str(hero_kwargs["out_dir"] / "hero.svg"))
    title_calls = []

    monkeypatch.setattr(ai_hero_image, "_is_ai_hero_enabled", lambda: True)
    monkeypatch.setattr(ai_hero_image, "create_hero_image_ai", lambda **kwargs: ai_result)
    monkeypatch.setattr(
        orchestrator,
        "create_title_card",
        lambda **kwargs: title_calls.append(kwargs) or fallback,
    )

    result = orchestrator._make_hero_image(**hero_kwargs)

    assert result is fallback
    assert title_calls == [hero_kwargs]


def test_ai_mode_falls_back_when_ai_raises(monkeypatch, hero_kwargs):
    fallback = object()
    title_calls = []

    monkeypatch.setattr(ai_hero_image, "_is_ai_hero_enabled", lambda: True)

    def raise_generation_error(**kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(ai_hero_image, "create_hero_image_ai", raise_generation_error)
    monkeypatch.setattr(
        orchestrator,
        "create_title_card",
        lambda **kwargs: title_calls.append(kwargs) or fallback,
    )

    result = orchestrator._make_hero_image(**hero_kwargs)

    assert result is fallback
    assert title_calls == [hero_kwargs]
