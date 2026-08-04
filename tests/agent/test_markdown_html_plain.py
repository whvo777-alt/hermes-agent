"""Golden and plain-mode coverage for the shared Markdown HTML renderer."""

from __future__ import annotations

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

EXPECTED_DEFAULT_HTML = """<h1 style="margin:0 0 1.1em;line-height:1.35;font-weight:800;color:#0d47a1;"><span style="background:linear-gradient(transparent 55%, #90caf9 55%);color:#0d47a1;font-weight:800;padding:0 4px 3px;">골든 제목 **볼드** ==형광펜== *이탤릭* [링크](url)</span></h1>
<h2 style="margin:2.2em 0 0.9em;line-height:1.4;font-weight:800;letter-spacing:-0.01em;color:#111;"><span style="color:#006064;margin-right:6px;">▶</span><span style="background:linear-gradient(transparent 55%, #80deea 55%);color:#006064;font-weight:800;padding:0 4px 3px;">대단원</span></h2>
<h3 style="margin:1.7em 0 0.65em;line-height:1.45;font-weight:800;color:#1b5e20;"><span style="background:linear-gradient(transparent 55%, #a5d6a7 55%);color:#1b5e20;font-weight:800;padding:0 3px;padding:0 5px 3px;">소제목</span></h3>
<p style="margin:0.85em 0 0.35em;line-height:1.75;padding:0.7em 0.95em;background:#f3e5f5;border-radius:6px;"><span style="color:#4a148c;font-weight:800;margin-right:8px;font-size:1.05em;">①</span><span style="font-weight:800;color:#111;">번호 하나</span></p>
<p style="margin:0.85em 0 0.35em;line-height:1.75;padding:0.7em 0.95em;background:#fffde7;border-radius:6px;"><span style="color:#5d4037;font-weight:800;margin-right:8px;font-size:1.05em;">②</span><span style="font-weight:800;color:#111;">번호 둘</span></p>
<ul style="margin:0.8em 0 1.6em;padding-left:1.4em;line-height:1.85;list-style:none;">
<li style="margin:0 0 0.55em;"><span style="color:#880e4f;margin-right:8px;">●</span>bullet 하나</li>
</ul>
<p style="margin:0 0 1.25em;line-height:1.9;color:#212121;">  - 중첩 bullet</p>
<p style="margin:0 0 1.25em;line-height:1.9;color:#212121;">일반 문단입니다.</p>
<p style="margin:0 0 1.25em;line-height:1.9;color:#212121;">인라인 본문: <strong style="background:linear-gradient(transparent 55%, #a5d6a7 55%);color:#1b5e20;font-weight:800;padding:0 3px;font-size:1.04em;">볼드</strong> ==형광펜== *이탤릭* <a href="url" style="color:#1565c0;font-weight:600;text-decoration:underline;text-underline-offset:3px;" target="_blank" rel="noopener noreferrer">링크</a></p>
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
    assert '<span style="background-color:#fff3a8;">형광펜</span>' in plain
    assert "<em>이탤릭</em>" in plain
    assert '<a href="url">링크</a>' in plain
    assert "<blockquote>인용 문장</blockquote>" in plain
    assert "**볼드**" not in plain
    assert "==형광펜==" not in plain
    assert "*이탤릭*" not in plain
    assert "[링크](url)" not in plain
