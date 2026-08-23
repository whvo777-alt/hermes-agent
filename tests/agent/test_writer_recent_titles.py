"""Regression coverage for recent-title guidance in the writing prompt."""

from __future__ import annotations

from unittest.mock import patch

from agent.content.writing.writer import write_blog_post


def _capture_user_prompt(**extra):
    captured = {}

    def _fake_call_llm(*, system: str, user: str) -> str:
        captured["user"] = user
        return "# 제목\n\n본문\n\n---META---\nURL slug: t\n"

    kwargs = {
        "platform_id": "wordpress",
        "platform_label": "테스트",
        "category_id": "health",
        "category_name": "건강",
        "target_audience": "테스트 독자",
        "tone": "테스트 톤",
        "topic_title": "테스트 주제",
        "topic_keywords": ["테스트"],
        "category_keywords": ["건강"],
        "caution_hints": [],
        "current_date": "2026-08-23",
        "research_content": "리서치 내용",
        "planning_content": "기획 내용",
    }
    kwargs.update(extra)

    with patch("agent.content.writing.writer.call_llm", side_effect=_fake_call_llm):
        write_blog_post(**kwargs)

    return captured["user"]


def test_recent_titles_are_added_to_the_prompt():
    prompt = _capture_user_prompt(recent_titles=["어깨결림 완화 운동", "목결림이 심할 때 확인할 점"])

    assert "[최근에 쓴 글]" in prompt
    assert "- 어깨결림 완화 운동" in prompt
    assert "- 목결림이 심할 때 확인할 점" in prompt
    assert "위 글들과 겹치지 않게 쓴다." in prompt


def test_empty_recent_titles_omit_the_recent_titles_block():
    prompt = _capture_user_prompt(recent_titles=[])

    assert "[최근에 쓴 글]" not in prompt
    assert "위 글들과 겹치지 않게 쓴다." not in prompt


def test_recent_titles_are_limited_to_ten_items():
    titles = [f"최근 제목 {index}" for index in range(11)]
    prompt = _capture_user_prompt(recent_titles=titles)

    for title in titles[:10]:
        assert f"- {title}" in prompt
    assert "- 최근 제목 10" not in prompt


def test_recent_titles_are_optional():
    prompt = _capture_user_prompt()

    assert "카테고리: 건강" in prompt
