"""Regression coverage for the "quote 폴백이 반복돼 보인다" complaint's
second fix: quote-shaped sections now have two eligible styles ("quote" the
pull-quote card, "quote_keyword" a keyword-highlight card) sharing the same
underlying quote_text, so _allocate_styles' existing repeat-avoidance makes
multiple quote-shaped sections in one document alternate layouts instead of
all rendering as identical cards. Combined with quote_cap (see
test_quote_fallback_cap.py), which bounds how many quote-shaped sections
get picked at all.
"""

from __future__ import annotations

from agent.content.images.section_infographics import (
    _split_keyword_phrase,
    extract_infographic_specs,
)

_TWO_QUOTE_SECTIONS_MD = """## 첫번째 섹션
아침에 일어나서 물 한 잔을 마시는 습관은 몸의 신진대사를 깨우는 데 도움이 됩니다. 개인차가 있으니 본인의 컨디션에 맞게 조절하세요.

## 두번째 섹션
저녁 식사 시간을 너무 늦추지 않는 것이 소화에 좋습니다. 상황에 따라 유연하게 조정하는 것이 중요합니다.
"""


def test_quote_shape_has_two_eligible_styles():
    specs = extract_infographic_specs(_TWO_QUOTE_SECTIONS_MD, max_count=5, style_seed="s1")
    assert len(specs) == 2
    for spec in specs:
        assert spec.shape == "quote"


def test_two_quote_sections_get_different_styles_not_both_quote():
    """The literal repetition complaint: two quote-shaped sections in the
    same document must not both render with the identical style."""
    specs = extract_infographic_specs(_TWO_QUOTE_SECTIONS_MD, max_count=5, style_seed="s1")
    styles = {s.style for s in specs}
    assert styles == {"quote", "quote_keyword"}


def test_alternation_is_deterministic_for_a_given_seed():
    a = extract_infographic_specs(_TWO_QUOTE_SECTIONS_MD, max_count=5, style_seed="stable-seed")
    b = extract_infographic_specs(_TWO_QUOTE_SECTIONS_MD, max_count=5, style_seed="stable-seed")
    assert [s.style for s in a] == [s.style for s in b]


def test_single_quote_section_still_works_with_either_style():
    single = "## 한 섹션\n그냥 서술형 문단입니다. 구조 없음. 충분히 긴 문장입니다."
    specs = extract_infographic_specs(single, max_count=5, style_seed="s2")
    assert len(specs) == 1
    assert specs[0].style in ("quote", "quote_keyword")


def test_split_keyword_phrase_short_text_returned_whole():
    phrase, rest = _split_keyword_phrase("짧은 문장")
    assert phrase == "짧은 문장"
    assert rest == ""


def test_split_keyword_phrase_long_text_splits_at_word_boundary():
    text = "아침에 일어나서 물 한 잔을 마시는 습관은 몸에 좋습니다"
    phrase, rest = _split_keyword_phrase(text, max_phrase_chars=16)
    assert len(phrase) <= 16 or " " not in phrase  # single overlong word is the only exception
    assert not phrase.endswith(" ")
    # phrase must be a real word-boundary prefix of the original text, and
    # rest must be exactly what's left after it -- no word gets mangled.
    assert text.startswith(phrase)
    assert text[len(phrase):].strip() == rest


def test_split_keyword_phrase_empty_text():
    assert _split_keyword_phrase("") == ("", "")
