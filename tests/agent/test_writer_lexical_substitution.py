"""Regression coverage for unsafe lexical substitution in writer post-processing."""

from agent.content.writing.writer import _enhance_blog_quality


def test_quality_enhancement_preserves_adverbial_mujogeon():
    content = "# 테스트 제목\n\n무조건 옳다는 뜻은 아닙니다."

    result = _enhance_blog_quality(
        platform_id="wordpress",
        category_name="테스트",
        topic_title="테스트 주제",
        content=content,
    )

    assert "무조건 옳다는 뜻은 아닙니다." in result
    assert "일률적인 옳다" not in result
