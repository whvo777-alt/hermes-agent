"""Regression coverage for the published-post bug: the title card's
subtitle was hard-clipped mid-word with no ellipsis (e.g. "...운동일 피"),
because _extract_subtitle sliced with a plain text[:max_len] instead of
cutting at a word boundary. Also covers the title/subtitle wrap+font-fit
pipeline end-to-end via the real templates.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from agent.content.images.title_card import (
    _F_BOLD,
    _RENDERERS,
    _category_colors,
    _extract_subtitle,
    _fit_title_with_highlight,
    _font,
    _wrap_with_highlight,
    _word_safe_truncate,
)


def _fit_illustration_title(title: str, keyword: str):
    draw = ImageDraw.Draw(Image.new("RGB", (1280, 720)))
    return _fit_title_with_highlight(
        draw,
        title,
        keyword,
        target_line=1,
        max_lines=2,
        max_width=830,
        font_path=_F_BOLD,
        base_size=58,
        min_size=38,
    )


def test_word_safe_truncate_never_cuts_mid_word():
    text = "공복 유지 시간과 회복 운동일 피해야 하는 상황을 정리했습니다"
    out = _word_safe_truncate(text, 20)
    assert out.endswith("…")
    # Every word in the truncated output (minus the ellipsis) must be a
    # complete word from the original text -- not a fragment like "운동일".
    words = out[:-1].split()
    original_words = text.split()
    assert all(w in original_words for w in words)


def test_word_safe_truncate_returns_unchanged_when_short_enough():
    assert _word_safe_truncate("짧은 문장", 80) == "짧은 문장"


def test_extract_subtitle_uses_meta_description_word_safely():
    blog_content = "Meta description: 아침 스트레칭할 때 헷갈리는 확인 기준과 상황별 적용법을 정리했습니다\n"
    subtitle = _extract_subtitle(blog_content, fallback="fallback text")
    assert not subtitle.endswith(" ")
    # No fragment shorter than any real word boundary in the source text.
    if subtitle.endswith("…"):
        words = subtitle[:-1].split()
        source_words = "아침 스트레칭할 때 헷갈리는 확인 기준과 상황별 적용법을 정리했습니다".split()
        assert all(w in source_words for w in words)


def test_extract_subtitle_falls_back_word_safely_too():
    subtitle = _extract_subtitle("", fallback="이것은 폴백으로 사용되는 매우 길고 긴 문장 예시입니다 계속 이어집니다")
    if subtitle.endswith("…"):
        assert not subtitle[:-1].split()[-1] in ("이", "것", "은")  # not a mid-word fragment


def test_wrap_with_highlight_reports_failed_placement_without_accent():
    draw = ImageDraw.Draw(Image.new("RGB", (1280, 720)))
    lines, accent_index, fits_cleanly = _wrap_with_highlight(
        draw,
        "HIIT 운동 강도와 주의사항: 초보자가 확인할 5가지 기준",
        "운동",
        target_line=1,
        font=_font(_F_BOLD, 58),
        max_width=830,
        max_lines=2,
    )

    assert lines == ["HIIT", "운동 강도와"]
    assert accent_index is None
    assert fits_cleanly is False


@pytest.mark.parametrize(
    ("title", "keyword", "expected_lines"),
    [
        (
            "HIIT 운동 강도와 주의사항: 초보자가 확인할 5가지 기준",
            "운동",
            ["HIIT 운동 강도와 주의사항:", "초보자가 확인할 5가지 기준"],
        ),
        (
            "절식 기준 5가지, 어디부터 너무 적게 먹는 걸까?",
            "어디부터",
            ["절식 기준 5가지, 어디부터 너무", "적게 먹는 걸까?"],
        ),
    ],
)
def test_failed_highlight_falls_back_without_losing_title(title, keyword, expected_lines):
    lines, accent_index, _font_used, highlight_enabled = _fit_illustration_title(title, keyword)

    assert lines == expected_lines
    assert not any(line.endswith("…") for line in lines)
    assert "".join(lines).replace(" ", "") == title.replace(" ", "")
    assert accent_index is None
    assert highlight_enabled is False


def test_successful_highlight_remains_enabled():
    lines, accent_index, _font_used, highlight_enabled = _fit_illustration_title(
        "걷기 확인 기준 5가지, 초보자가 시간보다 먼저 볼 것",
        "걷기",
    )

    assert lines == ["걷기 확인", "기준 5가지, 초보자가 시간보다 먼저 볼 것"]
    assert accent_index == 1
    assert highlight_enabled is True


def test_title_font_never_goes_below_min_size_after_clamp():
    _lines, _accent_index, font, _highlight_enabled = _fit_illustration_title(
        " ".join(["매우긴제목"] * 80),
        "매우긴제목",
    )

    assert font.size >= 38


@pytest.mark.parametrize(
    ("title", "keyword"),
    [
        ("HIIT 운동 강도와 주의사항: 초보자가 확인할 5가지 기준", "운동"),
        ("절식 기준 5가지, 어디부터 너무 적게 먹는 걸까?", "어디부터"),
    ],
)
def test_fallback_titles_render_through_all_highlight_templates(title, keyword):
    colors = _category_colors("health", "fallback-template-test")
    for name in ("frame_quote", "badge_quotes", "illustration", "topbar_hashtags", "photo_overlay"):
        image = _RENDERERS[name](
            colors=colors,
            blog_title=title,
            keyword=keyword,
            category_name="건강/헬스",
            subtitle="검증용 부제목입니다.",
            hashtags=["#운동", "#건강"],
        )
        assert image.size == (1280, 720), name


def test_all_templates_render_without_error_on_long_title_and_subtitle():
    """End-to-end: the exact category of input that broke in production
    (long title with a highlighted keyword, long unwrapped subtitle) must
    render cleanly through every template with no exception."""
    colors = _category_colors("health", "test-seed")
    long_title = "절식 기준 5가지 어디부터 너무 무리하는 건지 확인하는 방법"
    long_subtitle = "공복 유지 시간과 회복 운동일 피해야 하는 상황을 정리했습니다"
    for name, fn in _RENDERERS.items():
        img = fn(
            colors=colors, blog_title=long_title, keyword="절식 기준",
            category_name="건강/헬스", subtitle=long_subtitle, hashtags=["#절식", "#다이어트"],
        )
        assert img.size == (1280, 720), name
