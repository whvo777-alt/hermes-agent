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
    InfographicSpec,
    _render_quote_card_skin_c,
    _render_quote_keyword_card_skin_c,
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


def test_split_keyword_phrase_prefers_comma_over_mid_construction_word_cut():
    """Regression for a real published-post bug: the strict word-boundary
    fill cut this sentence at exactly 16 chars, landing right between the
    negated noun and "아니라" ("OO가 아니라, XX" -- "not OO, but XX") and
    splitting a single contrastive clause across the highlight/body divide.
    A comma sits just 5 chars past the limit and is the actual clause
    boundary, so it must be preferred over the raw character budget."""
    text = "다음은 성과를 보장하는 후기가 아니라, 기록을 어떻게 해석할 수 있는지 보여주는 두 가지 상황이다."
    phrase, rest = _split_keyword_phrase(text, max_phrase_chars=16)
    assert phrase == "다음은 성과를 보장하는 후기가 아니라,"
    assert rest == "기록을 어떻게 해석할 수 있는지 보여주는 두 가지 상황이다."
    assert not phrase.endswith("가")  # the mid-construction cut this regresses


def test_split_keyword_phrase_ignores_far_comma_and_falls_back_to_word_boundary():
    """A comma well beyond the slack window must not be used -- otherwise
    the "big headline callout" could grow arbitrarily long for any sentence
    that happens to contain a comma somewhere later on."""
    text = "아침에 일어나서 물 한 잔을 마시는 습관은 몸에 좋다고 알려져 있지만, 사람마다 다르다"
    assert text.find(",") > 16 + 10  # comma must actually sit outside the window
    phrase, rest = _split_keyword_phrase(text, max_phrase_chars=16, comma_slack=10)
    assert "," not in phrase
    assert len(phrase) <= 16 or " " not in phrase


def test_split_keyword_phrase_empty_text():
    assert _split_keyword_phrase("") == ("", "")


def test_skin_c_quote_and_quote_keyword_remain_visually_distinct_for_short_text(tmp_path):
    """Regression for a real published-post bug: with the SHORT quote_text
    _extract_quote() commonly produces (leaving _split_keyword_phrase's
    ``rest`` empty), the two skin-C cards used to collapse to "gradient bg +
    pill + one bold line + icon" for both, differing only in the pill's
    label text -- easy to miss at a glance. The keyword card must always
    carry its own highlight-block background behind the text and an
    outlined (not solid-filled) pill, so the two remain structurally
    distinct regardless of text length.
    """
    from PIL import Image

    short_text = "같은 시간표도 사람마다"  # short enough that rest == ""
    assert _split_keyword_phrase(short_text)[1] == ""  # confirms this reproduces the bug condition

    spec = InfographicSpec(
        heading="테스트 섹션", display_title="테스트 섹션",
        shape="quote", quote_text=short_text,
    )
    quote_path = str(tmp_path / "quote.png")
    keyword_path = str(tmp_path / "quote_keyword.png")
    _render_quote_card_skin_c(spec, quote_path, category_id="self-dev")
    _render_quote_keyword_card_skin_c(spec, keyword_path, category_id="self-dev")

    quote_img = Image.open(quote_path).convert("RGB")
    keyword_img = Image.open(keyword_path).convert("RGB")

    def _has_near_white_pixel(img, box) -> bool:
        region = img.crop(box)
        return any(all(c >= 230 for c in px) for px in region.getdata())

    # The keyword card's text must sit on its own near-white highlight block
    # (the fix); the plain quote card's text sits directly on the gradient
    # background and must NOT have any such block at the same region.
    text_region = (72, 100, 700, 180)
    assert _has_near_white_pixel(keyword_img, text_region), (
        "quote_keyword card is missing its highlight-block background for short text"
    )
    assert not _has_near_white_pixel(quote_img, text_region), (
        "quote card unexpectedly has a near-white block -- test region assumption may be stale"
    )
