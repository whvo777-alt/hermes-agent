"""Regression coverage for extracting verified source links from research output."""

from agent.content.research.research import extract_source_links


def test_extracts_source_titles_and_urls():
    content = "- [첫 자료](https://example.com/first)\n- [둘째 자료](https://example.com/second)"

    assert extract_source_links(content) == [
        {"title": "첫 자료", "url": "https://example.com/first"},
        {"title": "둘째 자료", "url": "https://example.com/second"},
    ]


def test_duplicate_urls_are_returned_once():
    content = "[첫 제목](https://example.com/same)\n[다른 제목](https://example.com/same)"

    assert extract_source_links(content) == [
        {"title": "첫 제목", "url": "https://example.com/same"}
    ]


def test_extraction_is_limited_to_six_links():
    content = "\n".join(
        f"[자료 {index}](https://example.com/{index})" for index in range(7)
    )

    result = extract_source_links(content)

    assert len(result) == 6
    assert result[-1] == {"title": "자료 5", "url": "https://example.com/5"}


def test_empty_research_content_returns_empty_list():
    assert extract_source_links("") == []
    assert extract_source_links(None) == []


def test_content_without_urls_returns_empty_list():
    assert extract_source_links("참고 출처는 아직 없습니다.") == []
    assert extract_source_links("[제목](not-a-url)") == []


def test_unexpected_content_does_not_raise():
    assert extract_source_links(object()) == []
