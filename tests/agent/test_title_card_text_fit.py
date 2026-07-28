"""Regression coverage for the published-post bug: the title card's
subtitle was hard-clipped mid-word with no ellipsis (e.g. "...운동일 피"),
because _extract_subtitle sliced with a plain text[:max_len] instead of
cutting at a word boundary. Also covers the title/subtitle wrap+font-fit
pipeline end-to-end via the real templates.
"""

from __future__ import annotations

from agent.content.images.title_card import _RENDERERS, _category_colors, _extract_subtitle, _word_safe_truncate


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
