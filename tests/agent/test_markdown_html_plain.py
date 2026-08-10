"""Golden and plain-mode coverage for the shared Markdown HTML renderer."""

from __future__ import annotations

import re

from agent.content.markdown_html import markdown_to_html


GOLDEN_MARKDOWN = """# 골든 제목 **볼드** ==형광펜== *이탤릭* [링크](url)

## 대단원

### 소제목

1. 번호 하나
2. 번호 둘

- bullet 하나
  - 중첩 bullet

일반 문단입니다.

인라인 본문: **볼드** ==형광펜== *이탤릭* [링크](url)

| 표 머리 | 값 |
| --- | --- |
| 하나 | 둘 |

> 인용 문장
"""

EXPECTED_DEFAULT_HTML = """<h1 style="margin:0 0 1.1em;line-height:1.35;font-weight:800;color:#0d47a1;"><span style="background-color:#f1f2ca;color:#0d47a1;font-weight:800;padding:0 4px 3px;">골든 제목 **볼드** ==형광펜== *이탤릭* [링크](url)</span></h1>
<h2 style="border-left:6px solid #2f6fa8;padding-left:14px;margin-top:50px;color:#16324f;">대단원</h2>
<h3 style="color:#1b4f80;margin-top:34px;">소제목</h3>
<p style="margin:0.85em 0 0.35em;line-height:1.75;padding:0.7em 0.95em;background:#f3e5f5;border-radius:6px;"><span style="color:#4a148c;font-weight:800;margin-right:8px;font-size:1.05em;">①</span><span style="font-weight:800;color:#111;">번호 하나</span></p>
<p style="margin:0.85em 0 0.35em;line-height:1.75;padding:0.7em 0.95em;background:#fffde7;border-radius:6px;"><span style="color:#5d4037;font-weight:800;margin-right:8px;font-size:1.05em;">②</span><span style="font-weight:800;color:#111;">번호 둘</span></p>
<ul style="margin:0.8em 0 1.6em;padding-left:1.4em;line-height:1.85;list-style:none;">
<li style="margin:0 0 0.55em;"><span style="color:#880e4f;margin-right:8px;">●</span>bullet 하나</li>
</ul>
<p style="margin:0 0 1.25em;line-height:1.9;color:#212121;">  - 중첩 bullet</p>
<p style="margin:0 0 1.25em;line-height:1.9;color:#212121;">일반 문단입니다.</p>
<p style="margin:0 0 1.25em;line-height:1.9;color:#212121;">인라인 본문: <strong style="color:#006dd7;">볼드</strong> <span style="background-color:#f1f2ca;">형광펜</span> *이탤릭* <a href="url" style="color:#1565c0;font-weight:600;text-decoration:underline;text-underline-offset:3px;" target="_blank" rel="noopener noreferrer">링크</a></p>
<div style="overflow-x:auto;margin:1.6em 0 2.1em;border:1px solid #e5e7eb;border-radius:14px;padding:6px 18px;box-shadow:0 1px 4px rgba(0,0,0,0.04);">
<table style="border-collapse:collapse;width:100%;line-height:1.7;">
<thead>
<tr>
<th style="padding:14px 12px;text-align:left;color:#1b5e20;font-weight:700;background:#edf6ed;border-bottom:2px solid #dcebdc;white-space:nowrap;">표 머리</th>
<th style="padding:14px 12px;text-align:left;color:#1b5e20;font-weight:700;background:#edf6ed;border-bottom:2px solid #dcebdc;white-space:nowrap;">값</th>
</tr>
</thead>
<tbody>
<tr>
<td style="padding:13px 10px;vertical-align:top;border-bottom:1px solid #f1f3f2;font-weight:700;color:#37474f;">하나</td>
<td style="padding:13px 10px;vertical-align:top;border-bottom:1px solid #f1f3f2;">둘</td>
</tr>
</tbody>
</table>
</div>
<blockquote style="margin:1.4em 0;padding:0.8em 1.1em;border-left:4px solid #5d4037;background:#fffde7;">인용 문장</blockquote>"""


def test_markdown_to_html_default_matches_golden() -> None:
    assert markdown_to_html(GOLDEN_MARKDOWN) == EXPECTED_DEFAULT_HTML


def test_markdown_to_html_plain_removes_decorations_and_keeps_basic_markup() -> None:
    styled = markdown_to_html(GOLDEN_MARKDOWN)
    plain = markdown_to_html(GOLDEN_MARKDOWN, plain=True)

    assert styled != plain
    assert "▶" not in plain
    assert "linear-gradient(" not in plain
    assert not any(icon in plain for icon in ("①", "②", "③"))
    assert 'style="margin:0 0 1.25em;line-height:1.9;color:#212121;"' not in plain
    assert 'style="overflow-x:auto;' not in plain
    assert "box-shadow:" not in plain
    assert '<ol style="list-style-type:decimal;">' in plain
    assert '<ul style="list-style-type:disc;">' in plain
    assert '<table style=' in plain
    assert "<div" not in plain

    assert "<strong>볼드</strong>" in plain
    assert '<span style="background-color:#f1f2ca;font-weight:bold;">형광펜</span>' in plain
    assert "<em>이탤릭</em>" in plain
    assert '<a href="url">링크</a>' in plain
    assert "<blockquote>인용 문장</blockquote>" in plain
    assert "**볼드**" not in plain
    assert "==형광펜==" not in plain
    assert "*이탤릭*" not in plain
    assert "[링크](url)" not in plain


def test_styled_headings_use_tistory_heading_styles_without_gradients() -> None:
    markdown = "# 문서 제목\n\n## 큰 제목\n\n### 작은 제목"

    html = markdown_to_html(markdown)

    assert "linear-gradient" not in html
    assert '<h2 style="border-left:6px solid #2f6fa8;padding-left:14px;margin-top:50px;color:#16324f;">큰 제목</h2>' in html
    assert '<h3 style="color:#1b4f80;margin-top:34px;">작은 제목</h3>' in html


def test_styled_double_equals_uses_one_solid_highlighter_color() -> None:
    markdown = "주의 ==주의 문장== 장점 ==장점 문장== 방법 ==방법 문장== 목표 ==목표 문장=="

    html = markdown_to_html(markdown)

    assert html.count('background-color:#f1f2ca;') == 4
    assert "linear-gradient" not in html


def test_styled_strong_text_uses_only_red_or_dark_blue_without_highlighting() -> None:
    markdown = "**위험 신호**와 **도움이 되는 방법**과 **일반 기준**"

    html = markdown_to_html(markdown)

    strong_styles = re.findall(r'<strong style="([^"]+)">', html)
    colors = set()
    for style in strong_styles:
        match = re.search(r"color:(#[0-9a-f]+);", style)
        assert match is not None
        colors.add(match.group(1))
    assert colors == {"#ef5369", "#006dd7"}
    assert "#f1f2ca" not in html
    assert "linear-gradient" not in html


def test_styled_non_tistory_strong_markup_remains_colored_strong() -> None:
    html = markdown_to_html("**일반 기준**")

    assert '<strong style="color:#006dd7;">일반 기준</strong>' in html


def test_styled_emphasis_is_independent_of_document_seed() -> None:
    body = "## 같은 제목\n\n### 같은 소제목\n\n==중요 문장== **위험 신호** **일반 기준**"

    first = markdown_to_html(f"# 첫 문서\n\n{body}")
    second = markdown_to_html(f"# 두 번째 문서\n\n{body}")

    first_without_h1 = re.sub(r"<h1\b.*?</h1>\n?", "", first, flags=re.S)
    second_without_h1 = re.sub(r"<h1\b.*?</h1>\n?", "", second, flags=re.S)
    assert first_without_h1 == second_without_h1
