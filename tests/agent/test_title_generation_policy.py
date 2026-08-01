"""Regression tests for separating internal topic keys from reader-facing titles."""

from __future__ import annotations

from agent.content.config.categories import Category
from agent.content import orchestrator
from agent.content.memory.content_memory import is_topic_blocked


def test_pick_daily_topic_uses_keyword_as_internal_topic_title(monkeypatch):
    category = Category(
        id="health",
        name="건강/헬스",
        target_audience="건강 관심 독자",
        tone="실전형",
        keywords=["운동"],
    )
    monkeypatch.setattr(orchestrator, "get_effective_keywords", lambda _category_id: ["운동"])
    monkeypatch.setattr(orchestrator, "used_main_keywords", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(orchestrator, "is_topic_blocked", lambda *_args, **_kwargs: False)

    topic = orchestrator._pick_daily_topic(
        category,
        platform_id="wordpress",
        run_date="2026-08-01",
        memory={},
    )

    assert topic["topic_title"] == "운동"
    assert "확인할 때 알아야 할 기준" not in topic["topic_title"]
    assert topic["topic_keywords"] == ["운동"]


def test_new_keyword_query_blocks_legacy_template_memory_item():
    memory = {
        "items": [
            {
                "date": "2026-07-20",
                "platform": "wordpress",
                "category": "health",
                "topic": "운동 확인할 때 알아야 할 기준",
                "title": "운동 확인할 때 알아야 할 기준",
                "mainKeyword": "운동",
                "slug": "legacy-topic",
            }
        ]
    }

    assert is_topic_blocked(
        memory,
        {
            "date": "2026-08-01",
            "platform": "wordpress",
            "category": "health",
            "topic": "운동",
            "title": "운동",
            "mainKeyword": "운동",
            "slug": "new-topic",
        },
    ) is True
