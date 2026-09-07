"""Regression tests for the separate keyword-reuse window."""

from agent.content.memory import content_memory as cm


def _item(
    date: str,
    *,
    keyword: str,
    title: str,
    topic: str | None = None,
    slug: str | None = None,
) -> dict:
    return {
        "date": date,
        "platform": "wordpress",
        "category": "health",
        "topic": topic or title,
        "title": title,
        "mainKeyword": keyword,
        "slug": slug or title,
    }


def test_keyword_used_within_180_days_is_blocked_by_used_set():
    memory = {
        "items": [
            _item(
                "2026-01-01",
                keyword="운동",
                title="아침 운동 루틴",
            )
        ]
    }

    assert cm.used_main_keywords(
        memory,
        date="2026-06-30",
        platform="wordpress",
        category="health",
    ) == {"운동"}


def test_keyword_older_than_180_days_is_not_in_used_set():
    memory = {
        "items": [
            _item(
                "2025-12-31",
                keyword="운동",
                title="아침 운동 루틴",
            )
        ]
    }

    assert cm.used_main_keywords(
        memory,
        date="2026-06-30",
        platform="wordpress",
        category="health",
    ) == set()


def test_is_topic_blocked_keeps_blocking_old_similar_titles():
    memory = {
        "items": [
            _item(
                "2025-12-31",
                keyword="아침루틴",
                title="아침 운동 루틴",
                slug="old-topic",
            )
        ]
    }
    query = {
        "date": "2026-06-30",
        "platform": "wordpress",
        "category": "health",
        "topic": "아침 운동 식단",
        "title": "아침 운동 식단",
        "mainKeyword": "운동식단",
        "slug": "new-topic",
    }

    assert cm.is_topic_blocked(memory, query) is True


def test_used_main_keywords_returns_empty_set_without_records():
    assert cm.used_main_keywords(
        {"items": []},
        date="2026-06-30",
        platform="wordpress",
        category="health",
    ) == set()
