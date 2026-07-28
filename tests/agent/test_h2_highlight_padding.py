"""Regression coverage for a real published-post bug: H1/H2 highlighted
headings rendered with a duplicate ``padding`` CSS declaration in the same
inline style attribute (``padding:0 3px;padding:0 4px 3px;``), because
``highlighter_style()`` already emits its own ``padding:0 3px;`` and the H1/H2
renderers appended a second, hardcoded ``padding:0 4px 3px;`` on top instead
of overriding it. Fixed by giving ``highlighter_style()`` a ``padding``
parameter so callers can choose their padding without duplicating it.
"""

from __future__ import annotations

from agent.content.markdown_html import h2_inner_html, markdown_to_html
from agent.content.visual_accents import highlighter_style


def test_highlighter_style_default_padding_unchanged():
    """Callers that don't override padding (e.g. **bold** text via
    strong_style) must see the exact same output as before."""
    style = highlighter_style("테스트", seed="s")
    assert style.count("padding:") == 1
    assert "padding:0 3px;" in style


def test_highlighter_style_accepts_custom_padding():
    style = highlighter_style("테스트", seed="s", padding="0 4px 3px")
    assert style.count("padding:") == 1
    assert "padding:0 4px 3px;" in style
    assert "0 3px;" not in style


def test_h2_inner_html_has_no_duplicate_padding():
    html = h2_inner_html("직장인·취준생·사이드 프로젝트별 적용법", seed="test")
    assert html.count("padding:") == 1
    assert "padding:0 4px 3px;" in html


def test_h1_rendering_has_no_duplicate_padding():
    markdown = "# 테스트 제목입니다\n\n본문 내용.\n"
    html = markdown_to_html(markdown)
    h1_start = html.index("<h1")
    h1_end = html.index("</h1>") + len("</h1>")
    h1_html = html[h1_start:h1_end]
    assert h1_html.count("padding:") == 1
    assert "padding:0 4px 3px;" in h1_html
