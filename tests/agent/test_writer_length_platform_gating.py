"""Regression coverage for the SEO length-target bump (4,000~6,500자/H2 4~5개
-> 5,500~7,500자/H2 6~7개 for WordPress/Blogspot).

_expression_goals() and write_blog_post()'s final length/H2 line used to
interpolate this text with NO platform gate at all -- a naver post in
category "health", or any naver/tistory post via the catch-all branch, was
already silently receiving the wordpress/blogspot wording. These tests pin
that naver/tistory's effective prompt text is byte-identical to the
pre-bump wording, so a future edit can't reintroduce that leak.
"""

from __future__ import annotations

from unittest.mock import patch

from agent.content.writing.writer import _expression_goals, write_blog_post

_OLD_LENGTH_LINE = "- 본문 분량 4,000~6,500자 (3,000자 미만·속이 빈 짧은 글 금지, 8,000자 초과 금지)."
_OLD_H2_LINE = "- H2 4~5개. 흐름: 독자 문제 → 판단 기준 → 실전 적용(상황별) → 함정/주의 → 오늘 체크·마무리."
_NEW_LENGTH_LINE = "- 본문 분량 5,500~7,500자 (4,500자 미만·속이 빈 짧은 글 금지, 9,000자 초과 금지)."


def test_naver_expression_goals_unchanged_even_for_health_category():
    """naver + category "health" used to hit the SAME branch as wordpress
    (the "health" OR "wordpress" condition) -- confirm naver still gets the
    OLD text, not the new wordpress/blogspot target."""
    text = _expression_goals(platform_id="naver", category_id="health")
    assert _OLD_LENGTH_LINE in text
    assert _OLD_H2_LINE in text
    assert _NEW_LENGTH_LINE not in text


def test_tistory_expression_goals_unchanged_for_self_dev_category():
    """tistory + category "self-dev" used to hit the SAME branch as blogspot
    (the "self-dev" OR "blogspot" condition) -- confirm tistory keeps the
    OLD FAQ-optional text, not blogspot's new FAQ-required text."""
    text = _expression_goals(platform_id="tistory", category_id="self-dev")
    assert _OLD_LENGTH_LINE in text
    assert "FAQ는 생략하거나 Q 2개만" in text
    assert "FAQ는 필수 섹션으로 넣는다" not in text


def test_wordpress_and_blogspot_get_new_length_target():
    for platform_id, category_id in (("wordpress", "health"), ("blogspot", "self-dev")):
        text = _expression_goals(platform_id=platform_id, category_id=category_id)
        assert _NEW_LENGTH_LINE in text
        assert "H2 6~7개" in text


def test_blogspot_faq_is_now_required_not_optional():
    text = _expression_goals(platform_id="blogspot", category_id="self-dev")
    assert "FAQ는 필수 섹션으로 넣는다" in text
    assert "FAQ는 생략하거나 Q 2개만" not in text


def _write_and_capture_user_prompt(*, platform_id: str, category_id: str) -> str:
    captured = {}

    def _fake_call_llm(*, system: str, user: str) -> str:
        captured["user"] = user
        return "# 제목\n\n본문\n\n---META---\nURL slug: t\n"

    with patch("agent.content.writing.writer.call_llm", side_effect=_fake_call_llm):
        write_blog_post(
            platform_id=platform_id, platform_label="테스트", category_id=category_id,
            category_name="테스트카테고리", target_audience="테스트 독자", tone="테스트 톤",
            topic_title="테스트 주제", topic_keywords=["키워드"], category_keywords=["카테고리키워드"],
            caution_hints=[], current_date="2026-07-27",
            research_content="리서치 내용", planning_content="플래닝 내용",
        )
    return captured["user"]


def test_write_blog_post_final_line_naver_unchanged():
    user_prompt = _write_and_capture_user_prompt(platform_id="naver", category_id="it-tech")
    assert "H1 1개, H2 4~5개, 본문 4,000~6,500자. 개인차 안내를 포함한다." in user_prompt
    assert "H2 6~7개(실전 사례·FAQ 섹션 포함)" not in user_prompt


def test_write_blog_post_final_line_wordpress_updated():
    user_prompt = _write_and_capture_user_prompt(platform_id="wordpress", category_id="health")
    assert "H1 1개, H2 6~7개(실전 사례·FAQ 섹션 포함), 본문 5,500~7,500자. 개인차 안내를 포함한다." in user_prompt
