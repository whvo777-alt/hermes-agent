"""Unit tests for the Tistory inline-style Markdown converter."""

from __future__ import annotations

import re

import agent.content.markdown_html as markdown_html


def test_h2_has_blue_rule_and_automatic_numbers():
    html = markdown_html.markdown_to_tistory_html("## 첫 번째\n\n## 두 번째")

    assert 'border-left:6px solid #2f6fa8;' in html
    assert 'padding-left:14px;margin-top:50px;color:#16324f;' in html
    assert '1. ' in html
    assert '2. ' in html


def test_h3_has_blue_heading_color():
    html = markdown_html.markdown_to_tistory_html("### 소제목")

    assert '<h3 style="color:#1b4f80;margin-top:34px;"' in html


def test_table_and_cells_have_tistory_styles():
    markdown = "| 항목 | 값 |\n| --- | --- |\n| 첫 열 | 내용 |"
    html = markdown_html.markdown_to_tistory_html(markdown)

    assert '<table style="width:100%;border-collapse:collapse;margin:22px 0;"' in html
    assert '<th style="border:1px solid #dde3ea;padding:10px 12px;text-align:left;' in html
    assert 'background-color:#eef5fb;color:#1b4f80;' in html
    assert '<td style="border:1px solid #dde3ea;padding:10px 12px;font-weight:bold;">' in html
    assert '<td style="border:1px solid #dde3ea;padding:10px 12px;">' in html


def test_horizontal_rule_is_removed():
    html = markdown_html.markdown_to_tistory_html("앞 문장\n\n---\n\n뒤 문장")

    assert "<hr" not in html


def test_blockquote_becomes_gray_official_box():
    html = markdown_html.markdown_to_tistory_html("> 공식 안내")

    assert "<blockquote" not in html
    assert '<div style="background-color:#f5f6f8;border:1px solid #e1e4e8;' in html
    assert 'padding:16px 18px;margin:20px 0;border-radius:4px;text-align:center;' in html
    assert 'font-weight:bold;color:#333333;">공식 안내</div>' in html


def test_toc_section_becomes_blue_gray_box():
    markdown = "## 목차\n\n1. 하나\n2. 둘\n\n## 본문\n내용"
    html = markdown_html.markdown_to_tistory_html(markdown)

    assert '<div style="background-color:#f7f9fb;border:1px solid #e3e8ee;' in html
    assert '<p style="margin:0 0 10px 0;font-weight:bold;color:#16324f;"' in html
    assert '<ol style="margin:0;padding-left:22px;color:#40566b;"' in html
    assert "목차" in html and "하나" in html


def test_toc_h2_does_not_consume_automatic_section_number():
    markdown = "## 목차\n\n1. 첫 섹션\n2. 둘째 섹션\n\n## 첫 섹션\n내용\n\n## 둘째 섹션\n내용"
    html = markdown_html.markdown_to_tistory_html(markdown)
    h2_blocks = re.findall(r"<h2\b.*?</h2>", html, flags=re.S)

    assert len(h2_blocks) == 2
    assert '<p style="margin:0 0 10px 0;font-weight:bold;color:#16324f;"' in html
    assert ">1. 첫 섹션</h2>" in h2_blocks[0]
    assert ">2. 둘째 섹션</h2>" in h2_blocks[1]


def test_toc_box_contains_only_ordered_items():
    markdown = "## 목차\n\n1. 항목 하나\n2. 항목 둘\n3. 항목 셋\n\n## 첫 섹션\n본문"
    html = markdown_html.markdown_to_tistory_html(markdown)
    toc_box = re.search(
        r'<div style="background-color:#f7f9fb;.*?</div>',
        html,
        flags=re.S,
    )

    assert toc_box is not None
    assert all(item in toc_box.group(0) for item in ("항목 하나", "항목 둘", "항목 셋"))
    assert "본문" not in toc_box.group(0)


def test_checklist_after_h3_becomes_green_box():
    markdown = "### 체크리스트\n\n- 하나\n- 둘\n\n## 다음"
    html = markdown_html.markdown_to_tistory_html(markdown)
    checklist_box = re.search(
        r'<div style="background-color:#f6faf6;.*?</div>',
        html,
        flags=re.S,
    )

    assert '<div style="background-color:#f6faf6;border:1px solid #d7e8d7;' in html
    assert 'padding:20px 24px;margin:26px 0;border-radius:6px;">' in html
    assert checklist_box is not None
    assert "체크리스트" in checklist_box.group(0)
    assert "하나" in checklist_box.group(0)


def test_checklist_wrapper_does_not_capture_previous_h3():
    markdown = "### 일반 소제목\n\n일반 내용\n\n### 체크리스트\n\n- 하나\n- 둘"
    html = markdown_html.markdown_to_tistory_html(markdown)
    boxes = re.findall(
        r'<div style="background-color:#f6faf6;.*?</div>',
        html,
        flags=re.S,
    )

    assert len(boxes) == 1
    assert "일반 소제목" not in boxes[0]
    assert "체크리스트" in boxes[0]


def test_double_equals_sentence_gets_yellow_highlight():
    html = markdown_html.markdown_to_tistory_html("==강조 문장==")

    assert '<span style="background-color:#fff3a8;">강조 문장</span>' in html


def test_core_whole_paragraph_becomes_blue_callout():
    html = markdown_html.markdown_to_tistory_html("**핵심: 반드시 확인하세요.**")

    assert '<div style="background-color:#eef5fb;border-left:5px solid #2f6fa8;' in html
    assert '<span style="color:#1b4f80;font-weight:bold;">핵심 &middot; 반드시 확인하세요.</span>' in html


def test_caution_whole_paragraph_becomes_orange_callout():
    html = markdown_html.markdown_to_tistory_html("**주의: 무리하지 마세요.**")

    assert '<div style="background-color:#fff6e5;border-left:5px solid #e09b2d;' in html
    assert '<span style="color:#a86a10;font-weight:bold;">주의 &middot; 무리하지 마세요.</span>' in html


def test_disclaimer_whole_paragraph_becomes_dashed_gray_callout():
    html = markdown_html.markdown_to_tistory_html("**면책: 일반적인 정보입니다.**")

    assert '<div style="background-color:#f5f5f5;border:1px dashed #cccccc;' in html
    assert 'padding:14px 18px;margin:24px 0;border-radius:4px;color:#666666;font-size:0.95em;">' in html
    assert "면책:" not in html
    assert "일반적인 정보입니다." in html


def test_inline_strong_text_is_dark_blue():
    html = markdown_html.markdown_to_tistory_html("이것은 **굵은 글씨**입니다.")

    assert '<strong style="color:#1a5fa8;font-weight:800;font-size:1.04em;">굵은 글씨</strong>' in html


def test_inline_caution_strong_text_is_red():
    html = markdown_html.markdown_to_tistory_html("이것은 **위험 신호**입니다.")

    assert '<strong style="color:#c0392b;font-weight:800;font-size:1.06em;">위험 신호</strong>' in html


def test_tistory_inline_emphasis_uses_fixed_styles():
    html = markdown_html.markdown_to_tistory_html(
        "==강조 문장== **위험 신호** **일반 기준**"
    )

    assert html.count("#fff3a8") == 1
    assert html.count("#c0392b") == 1
    assert html.count("#1a5fa8") == 1
    assert "linear-gradient" not in html


def test_leading_h1_is_removed(monkeypatch):
    monkeypatch.setattr(markdown_html, "markdown_to_html", lambda _, **kwargs: "<h1>제목</h1><p>본문</p>")

    html = markdown_html.markdown_to_tistory_html("무시되는 입력")

    assert not html.lstrip().startswith("<h1")
    assert "본문" in html


def test_leading_base64_image_and_caption_are_removed(monkeypatch):
    base_html = (
        '<h1>제목</h1>'
        '<p><img src="data:image/png;base64,abc123" alt="대표 이미지"></p>'
        '<p>대표 이미지 캡션</p>'
        '<p>본문</p>'
    )
    monkeypatch.setattr(markdown_html, "markdown_to_html", lambda _, **kwargs: base_html)

    html = markdown_html.markdown_to_tistory_html("무시되는 입력")

    assert "data:image" not in html
    assert "대표 이미지 캡션" not in html
    assert "본문" in html


def test_platform_metadata_h2_cuts_the_remaining_html(monkeypatch):
    base_html = (
        "<h2>본문 섹션</h2><p>본문</p>"
        "<h2>내부링크 후보</h2><ul><li>제작 메모</li></ul>"
    )
    monkeypatch.setattr(markdown_html, "markdown_to_html", lambda _, **kwargs: base_html)

    html = markdown_html.markdown_to_tistory_html("무시되는 입력")

    assert "본문" in html
    assert "내부링크 후보" not in html
    assert "제작 메모" not in html


def test_converter_uses_base_result_and_never_emits_style_block(monkeypatch):
    monkeypatch.setattr(markdown_html, "markdown_to_html", lambda _, **kwargs: "<p>기본</p><hr>")

    html = markdown_html.markdown_to_tistory_html("무시되는 입력")

    assert "기본" in html
    assert "<hr" not in html
    assert "<style" not in html.lower()


def test_converter_calls_base_renderer_with_plain_true(monkeypatch):
    calls = []

    def fake_markdown_to_html(markdown, **kwargs):
        calls.append((markdown, kwargs))
        return "<p>기본</p>"

    monkeypatch.setattr(markdown_html, "markdown_to_html", fake_markdown_to_html)

    markdown_html.markdown_to_tistory_html("무시되는 입력")

    assert calls == [("무시되는 입력", {"plain": True})]


TISTORY_TARGET_MARKDOWN = """# 테스트 제목

## 목차
1. 첫 번째 대단원
2. 두 번째 대단원
3. 자주 묻는 질문

::이미지::
설명: 은행 창구 책상 위의 서류와 볼펜
프롬프트: A bank counter desk with documents and a pen, warm light, no text, no logo, 16:9
::이미지끝::

첫 문단입니다. ==여기가 형광펜== 부분입니다.

## 1. 첫 번째 대단원

### 소제목입니다

본문 문장입니다.

세전 이자 = 원금 × 연이율 × 예치 기간

**핵심: 최고금리가 아니라 조건을 채울 수 있는 금리를 봐야 합니다.**

- 목록 항목 하나
- 목록 항목 둘

## 2. 두 번째 대단원

| 구분 | 내용 |
| --- | --- |
| 첫 열 | 값 |

**주의: 중도해지 시 약정금리가 적용되지 않습니다.**

### 가입 전 체크리스트

- 확인 항목 하나
- 확인 항목 둘

## 3. 자주 묻는 질문

Q. 첫 번째 질문인가요?
A. 네, 그렇습니다.

Q. 두 번째 질문인가요?
A. 네, 그렇습니다.

**면책: 이 글은 정보 제공을 목적으로 하며 특정 상품의 가입을 권유하지 않습니다.**"""



def test_tistory_target_shape_has_boxes_and_editor_attributes():
    html = markdown_html.markdown_to_tistory_html(TISTORY_TARGET_MARKDOWN)

    assert 'data-ke-size="size16"' in html
    assert 'data-ke-size="size26"' in html
    assert 'data-ke-size="size23"' in html
    assert 'data-ke-list-type="decimal"' in html
    assert 'data-ke-list-type="disc"' in html
    assert 'data-ke-align="alignLeft"' in html
    assert "핵심 &middot; 최고금리가" in html
    assert "주의 &middot; 중도해지" in html
    assert "[이미지 1] 여기에 이미지를 넣고" in html
    assert "border-top:2px solid #2f6fa8;" in html
    assert "border-top:1px solid #e3e8ee;" in html
    assert '<p style="margin:0 0 10px 0;font-weight:bold;color:#16324f;"' in html
    assert '<p style="margin:0 0 12px 0;font-weight:bold;color:#2b6b3f;"' in html
    assert '<ul style="margin:0;padding-left:22px;color:#33553f;"' in html
    strong_html = markdown_html.markdown_to_tistory_html("일반 **굵은 글씨**")
    assert "color:#1a5fa8;font-weight:800;font-size:1.04em;" in strong_html

    assert '<span style="color:#16324f;">1. </span>' not in html
    assert ">목차</h2>" not in html
    assert "체크리스트</h3>" not in html
    assert "::이미지::" not in html
    assert "::이미지끝::" not in html
    assert "면책 &middot;" not in html
    assert '<span style="font-weight:bold;">면책' not in html

    toc_box = re.search(
        r'<div style="background-color:#f7f9fb;.*?</div>',
        html,
        flags=re.S,
    )
    assert toc_box is not None
    assert "첫 문단입니다" not in toc_box.group(0)
