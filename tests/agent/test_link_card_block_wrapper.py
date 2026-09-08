"""Regression tests for paragraph-wrapped link cards and boxes."""

from __future__ import annotations

import re

from agent.content.markdown_html import _render_internal_link_card, markdown_to_html


SITE_URL = "https://cocoboll.com"


def test_internal_link_card_is_wrapped_in_a_paragraph() -> None:
    html = markdown_to_html(
        f"[내부 글]({SITE_URL}/inside)",
        internal_host="cocoboll.com",
    )

    assert html.startswith('<p style="margin:14px 0;"><a href=')
    assert 'margin:14px 0' not in html.split("<a", 1)[1].split("</a>", 1)[0]
    assert html.endswith("</a></p>")


def test_reference_box_is_wrapped_in_a_paragraph() -> None:
    html = markdown_to_html(
        "[참고 자료](https://example.com/reference)",
        internal_host=SITE_URL,
    )

    assert html.startswith('<p style="margin:18px 0;"><a href=')
    assert "📚 참고 자료" in html
    assert html.endswith("</a></p>")


def test_official_box_is_wrapped_in_a_paragraph() -> None:
    html = markdown_to_html(
        "[공식 사이트](https://bokjiro.go.kr/)",
        internal_host=SITE_URL,
    )

    assert html.startswith('<p style="margin:18px 0;"><a href=')
    assert "🔗 공식 사이트 ↗" in html
    assert html.endswith("</a></p>")


def test_news_box_is_wrapped_in_a_paragraph() -> None:
    html = markdown_to_html(
        "[뉴스 기사](https://news.naver.com/article/1)",
        internal_host=SITE_URL,
    )

    assert html.startswith('<p style="margin:18px 0;"><a href=')
    assert "📰 뉴스 기사 ↗" in html
    assert html.endswith("</a></p>")


def test_two_cards_have_two_paragraphs_instead_of_running_together() -> None:
    html = markdown_to_html(
        f"[첫 번째]({SITE_URL}/first)\n\n[두 번째]({SITE_URL}/second)",
        internal_host="cocoboll.com",
    )

    assert html.count('<p style="margin:14px 0;">') == 2
    assert html.count("</p>") == 2


def test_paragraph_survives_removing_all_inline_styles() -> None:
    html = markdown_to_html(
        f"[내부 글]({SITE_URL}/inside)",
        internal_host="cocoboll.com",
    )
    blogspot_sanitized = re.sub(r'style="[^"]*"', "", html)

    assert re.match(r"<p\b", blogspot_sanitized)
    assert blogspot_sanitized.endswith("</a></p>")


def test_missing_internal_host_does_not_create_a_card() -> None:
    line = f"[내부 글]({SITE_URL}/inside)"

    assert _render_internal_link_card(line, internal_host="") is None
    html = markdown_to_html(line)
    assert "display:block;padding:16px 20px" not in html
    assert f'<a href="{SITE_URL}/inside"' in html
