"""Tests for agent.content.images.section_infographics._SKIP_HEADING_RE.

Regression coverage for: seo_enrich.append_internal_links() appends a
"## 함께 읽으면 좋은 글" section with only a connector sentence + 1-2 link
bullets. That's too short to qualify as a checklist (>=3 items), so it fell
through to the quote extractor, which turned the connector sentence into a
fake pull-quote card. Structural/meta sections like this must never be
imaged, even as a last-resort "quote" filler.
"""

from __future__ import annotations

from agent.content.images.section_infographics import extract_infographic_specs

_REAL_SECTION = "## 실제 내용 섹션\n- 항목 하나입니다\n- 항목 둘입니다\n- 항목 셋입니다\n"


def _headings(markdown: str) -> list:
    return [spec.heading for spec in extract_infographic_specs(markdown, max_count=5, style_seed="t")]


def test_internal_links_section_is_excluded():
    markdown = (
        f"# 제목\n\n{_REAL_SECTION}\n"
        "## 함께 읽으면 좋은 글\n"
        "같은 블로그에서 이어서 보면 도움이 되는 글입니다.\n"
        "- [글 A](https://example.com/a)\n"
        "- [글 B](https://example.com/b)\n"
    )
    assert _headings(markdown) == ["실제 내용 섹션"]


def test_external_reference_section_is_excluded():
    markdown = (
        f"# 제목\n\n{_REAL_SECTION}\n"
        "## 참고할 수 있는 공식 자료\n"
        "아래 기관 자료는 참고할 수 있습니다.\n"
        "- [국민건강보험공단](https://www.nhis.or.kr/)\n"
    )
    assert _headings(markdown) == ["실제 내용 섹션"]


def test_related_posts_variants_are_excluded():
    for heading in ("## 관련 글", "## 관련 포스트", "## 이어서 보면 좋은 글", "## 더 보기"):
        markdown = f"# 제목\n\n{_REAL_SECTION}\n{heading}\n- [글 A](https://example.com/a)\n"
        assert _headings(markdown) == ["실제 내용 섹션"], f"{heading} should have been excluded"


def test_real_content_sections_are_still_picked():
    markdown = f"# 제목\n\n{_REAL_SECTION}"
    assert _headings(markdown) == ["실제 내용 섹션"]
