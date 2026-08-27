"""Regression tests for WordPress internal-link rendering."""

from __future__ import annotations

from agent.content.markdown_html import markdown_to_html


SITE_URL = "https://cocoboll.com"


def test_internal_host_omitted_keeps_existing_link_rendering() -> None:
    html = markdown_to_html(f"[어깨결림 스트레칭 순서]({SITE_URL}/shoulder)")

    assert '<a href="https://cocoboll.com/shoulder"' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert "display:block;padding:16px 20px" not in html


def test_internal_inline_link_does_not_open_new_window() -> None:
    html = markdown_to_html(
        f"본문 중간에 [이 글]({SITE_URL}/inline)을 섞어 쓴 문장입니다.",
        internal_host="cocoboll.com",
    )

    assert '<a href="https://cocoboll.com/inline"' in html
    assert 'target="_blank"' not in html
    assert "이 글 →" in html


def test_external_link_still_opens_new_window() -> None:
    html = markdown_to_html(
        "바깥 자료는 [질병관리청](https://kdca.go.kr/page)입니다.",
        internal_host="cocoboll.com",
    )

    assert '<a href="https://kdca.go.kr/page"' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_standalone_internal_link_paragraph_becomes_card() -> None:
    html = markdown_to_html(
        f"[어깨결림 스트레칭 순서]({SITE_URL}/shoulder)",
        internal_host="cocoboll.com",
    )

    assert html.count('style="display:block;padding:16px 20px') == 1
    assert "어깨결림 스트레칭 순서" in html
    assert '<span style="float:right;color:#6b7280;">→</span>' in html
    assert 'target="_blank"' not in html


def test_inline_internal_link_is_not_card() -> None:
    html = markdown_to_html(
        f"본문 중간에 [이 글]({SITE_URL}/inline)을 섞어 쓴 문장입니다.",
        internal_host="cocoboll.com",
    )

    assert "display:block;padding:16px 20px" not in html
    assert '<a href="https://cocoboll.com/inline"' in html
    assert "이 글 →" in html


def test_www_internal_host_matches_non_www_link() -> None:
    html = markdown_to_html(
        f"[수면 습관 점검하기]({SITE_URL}/sleep)",
        internal_host="www.cocoboll.com",
    )

    assert 'target="_blank"' not in html
    assert "display:block;padding:16px 20px" in html


def test_empty_internal_host_treats_every_link_as_external() -> None:
    html = markdown_to_html(
        f"[수면 습관 점검하기]({SITE_URL}/sleep)",
        internal_host="",
    )

    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert "display:block;padding:16px 20px" not in html


def test_malformed_url_does_not_raise() -> None:
    html = markdown_to_html(
        "[이상한 주소](https://[bad)",
        internal_host="cocoboll.com",
    )

    assert "이상한 주소" in html
    assert "target=\"_blank\"" in html

