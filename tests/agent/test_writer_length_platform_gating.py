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


def test_blogspot_faq_is_not_required_and_follows_platform_rule():
    text = _expression_goals(platform_id="blogspot", category_id="self-dev")
    assert "자주 묻는 질문 절은 플랫폼 규칙을 따른다" in text
    assert "FAQ는 필수 섹션으로 넣는다" not in text
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
    assert "H1 1개, H2 6~7개(실전 사례 포함), 본문 5,500~7,500자. 개인차 안내를 포함한다." in user_prompt


def test_write_blog_post_title_prompt_uses_internal_topic_label_and_positive_criteria():
    user_prompt = _write_and_capture_user_prompt(platform_id="wordpress", category_id="health")

    assert "오늘의 주제:" not in user_prompt
    assert "주제 키워드(내부 식별용, 제목으로 사용하지 말 것): 테스트 주제" in user_prompt
    assert "독자가 실제로 검색창에 칠 법한 말" in user_prompt
    assert "본문이 실제로 답하는 질문 하나" in user_prompt
    assert "질문형·서술형·비교형·상황 제시형" in user_prompt
    assert "운동 후 심장이 너무 빨리 뛰면 강도를 낮춰야 할까?" in user_prompt
    assert "초보자 HIIT는 운동 시간보다 회복 간격이 먼저입니다" in user_prompt
    assert "집에서 하는 HIIT, 무릎 통증이 있을 때 달라지는 선택 기준" in user_prompt
    assert "운동 확인할 때 알아야 할 5가지 기준" in user_prompt
    assert "이것만 알면 무조건 살 빠지는 7가지 비밀" in user_prompt
    assert "건강에 관한 모든 것을 완벽하게 정리한 최고의 필독 가이드" in user_prompt
    assert "구체적인 대상이나 상황을 반드시 넣는다" in user_prompt
    assert "\"이 제품\", \"이것\", \"그 방법\"처럼 지시어로 대신하지 않는다" in user_prompt
    assert "주제 키워드를 문장에 자연스럽게 녹여 쓴다" in user_prompt
    assert "문장을 그대로 베끼라는 것이지 키워드를 빼라는 뜻이 아니다" in user_prompt
    assert "이 제품, 건강식품일까? 성분표와 섭취 기준부터 읽는 법" in user_prompt
    assert "무엇에 관한 글인지 제목만으로 알 수 없음" in user_prompt
