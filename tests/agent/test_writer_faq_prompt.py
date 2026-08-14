"""Regression coverage for optional FAQ prompt guidance."""

from unittest.mock import patch

from agent.content.writing.writer import write_blog_post


_FORBIDDEN_FAQ_REQUIREMENTS = (
    "FAQ는 필수",
    "FAQ 섹션 포함",
    "FAQ 포함",
    "Q 3~4개",
    "3~4쌍",
)


def _combined_prompt(*, platform_id: str, category_id: str) -> str:
    captured = {}

    def _fake_call_llm(*, system: str, user: str) -> str:
        captured["prompt"] = f"{system}\n{user}"
        return "# 제목\n\n본문\n\n---META---\nURL slug: t\n"

    with patch("agent.content.writing.writer.call_llm", side_effect=_fake_call_llm):
        write_blog_post(
            platform_id=platform_id,
            platform_label=platform_id,
            category_id=category_id,
            category_name="테스트 카테고리",
            target_audience="테스트 독자",
            tone="테스트 톤",
            topic_title="테스트 주제",
            topic_keywords=["테스트"],
            category_keywords=["카테고리"],
            caution_hints=[],
            current_date="2026-08-14",
            research_content="리서치 내용",
            planning_content="플래닝 내용",
        )

    return captured["prompt"]


def test_wordpress_prompt_does_not_require_faq_but_keeps_markers():
    prompt = _combined_prompt(platform_id="wordpress", category_id="health")

    assert "질문:" in prompt
    assert "답변:" in prompt
    assert not any(marker in prompt for marker in _FORBIDDEN_FAQ_REQUIREMENTS)


def test_blogspot_prompt_does_not_require_faq_but_keeps_markers():
    prompt = _combined_prompt(platform_id="blogspot", category_id="self-dev")

    assert "질문:" in prompt
    assert "답변:" in prompt
    assert not any(marker in prompt for marker in _FORBIDDEN_FAQ_REQUIREMENTS)
