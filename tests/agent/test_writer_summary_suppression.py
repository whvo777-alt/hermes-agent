"""Regression coverage for the removal of the canned summary append."""

from agent.content.writing.writer import _enhance_blog_quality


def test_quality_enhancement_does_not_append_canned_summary():
    content = "# 테스트 제목\n\n본문 내용입니다. 별도의 추가 문단은 없습니다."

    result = _enhance_blog_quality(
        platform_id="wordpress",
        category_name="테스트",
        topic_title="테스트 주제",
        content=content,
    )

    assert "한눈에 보는 핵심 요약" not in result
    assert "오늘 주제는" not in result
