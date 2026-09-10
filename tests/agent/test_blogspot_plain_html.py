"""Regression tests for Blogspot plain HTML output."""

from agent.content.markdown_html import markdown_to_html
from agent.content.publishers.blogspot import build_blogger_post


BLOGSPOT_URL = "https://cocoboll.com"


def _blogspot_html(monkeypatch, markdown: str) -> str:
    monkeypatch.setenv("BLOGSPOT_SITE_URL", BLOGSPOT_URL)
    return build_blogger_post(markdown, title="테스트 글")["content"]


def test_blogspot_plain_html_does_not_add_bullet_characters(monkeypatch):
    html = _blogspot_html(monkeypatch, "- 책 제목과 저자\n- 읽기 시작한 날과 멈춘 날")

    assert "●" not in html


def test_blogspot_plain_html_keeps_list_structure_and_content(monkeypatch):
    html = _blogspot_html(monkeypatch, "- 책 제목과 저자\n- 읽기 시작한 날과 멈춘 날")

    assert "<ul" in html
    assert html.count("<li>") == 2
    assert "책 제목과 저자" in html
    assert "읽기 시작한 날과 멈춘 날" in html


def test_blogspot_plain_html_does_not_use_none_list_style(monkeypatch):
    html = _blogspot_html(monkeypatch, "- 첫 번째\n- 두 번째")

    assert "list-style:none" not in html


def test_blogspot_plain_html_keeps_internal_link_and_arrow(monkeypatch):
    target_url = f"{BLOGSPOT_URL}/inside"
    html = _blogspot_html(monkeypatch, f"[내부 글]({target_url})")

    assert f'<a href="{target_url}"' in html
    assert "내부 글" in html
    assert "→" in html


def test_blogspot_plain_html_keeps_external_link_target_and_rel(monkeypatch):
    html = _blogspot_html(
        monkeypatch,
        "본문의 [참고 자료](https://example.com/reference)입니다.",
    )

    assert '<a href="https://example.com/reference"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html


def test_blogspot_plain_html_internal_inline_link_has_no_target(monkeypatch):
    target_url = f"{BLOGSPOT_URL}/inside"
    html = _blogspot_html(
        monkeypatch,
        f"본문의 [내부 글]({target_url})입니다.",
    )

    assert f'<a href="{target_url}"' in html
    assert "내부 글 →" in html
    assert 'target="_blank"' not in html


def test_blogspot_plain_html_relative_link_has_no_target(monkeypatch):
    html = _blogspot_html(monkeypatch, "본문의 [소개](/about)입니다.")

    assert '<a href="/about">소개</a>' in html
    assert 'target="_blank"' not in html
    assert 'rel="noopener noreferrer"' not in html


def test_blogspot_plain_html_keeps_table_and_cell_content(monkeypatch):
    html = _blogspot_html(
        monkeypatch,
        "| 항목 | 내용 |\n| --- | --- |\n| 책 | 독서 |",
    )

    assert "<table" in html
    assert "<td" in html
    assert "항목" in html
    assert "독서" in html


def test_blogspot_plain_html_keeps_images(monkeypatch):
    html = _blogspot_html(monkeypatch, "![책 표지](https://example.com/cover.png)")

    assert "<img" in html
    assert 'src="https://example.com/cover.png"' in html
    assert 'alt="책 표지"' in html


def test_blogspot_plain_html_converts_italic_text_to_em(monkeypatch):
    html = _blogspot_html(monkeypatch, "이 문장은 *기울임*입니다.")

    assert "<em>기울임</em>" in html


def test_default_styled_html_for_wordpress_keeps_bullets_and_styles():
    html = markdown_to_html(
        f"- 워드프레스 목록\n\n[내부 글]({BLOGSPOT_URL}/inside)",
        internal_host="cocoboll.com",
    )

    assert 'list-style:none;' in html
    assert "●" in html
    assert "background:#f8f9fa" in html
    assert "border:1px solid #e5e7eb" in html
