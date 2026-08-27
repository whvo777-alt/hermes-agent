"""Regression tests for the internal-link Markdown block."""

from __future__ import annotations

from agent.content.seo_enrich import append_internal_links


SITE_URL = "https://cocoboll.com"
CANDIDATES = [
    {"title": "어깨결림 스트레칭 순서", "link": f"{SITE_URL}/shoulder"},
    {"title": "수면 습관 점검하기", "link": f"{SITE_URL}/sleep"},
]


def test_internal_links_use_standalone_paragraphs_not_list_items() -> None:
    output = append_internal_links(
        "## 마무리\n끝.",
        site_url=SITE_URL,
        candidates=CANDIDATES,
        preferred_terms=["어깨결림", "수면"],
    )

    assert "- [" not in output
    assert (
        "[어깨결림 스트레칭 순서](https://cocoboll.com/shoulder)\n\n"
        "[수면 습관 점검하기](https://cocoboll.com/sleep)"
    ) in output


def test_internal_link_heading_and_intro_are_preserved() -> None:
    output = append_internal_links(
        "본문\n\n## 마무리\n끝.",
        site_url=SITE_URL,
        candidates=CANDIDATES,
    )

    assert "## 함께 읽으면 좋은 글" in output
    assert "같은 블로그에서 이어서 보면 도움이 되는 글입니다." in output


def test_existing_internal_link_prevents_another_block() -> None:
    markdown = "본문\n\n[기존 글](https://cocoboll.com/existing)"

    output = append_internal_links(
        markdown,
        site_url=SITE_URL,
        candidates=CANDIDATES,
    )

    assert output == markdown