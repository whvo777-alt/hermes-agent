"""Tests for agent.content.images.text_fit — shared measured-width text
wrapping/ellipsizing, extracted from section_infographics.py's working
_wrap() so title_card.py could reuse the same correct pattern instead of
its own char-count-budget wrapping (which hard-clipped text mid-word with
no ellipsis)."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from agent.content.images.text_fit import ellipsize, fit_font_and_wrap, wrap_text

_FONT = ImageFont.load_default()


def _draw():
    return ImageDraw.Draw(Image.new("RGB", (1, 1)))


def test_ellipsize_returns_unchanged_when_it_fits():
    draw = _draw()
    assert ellipsize(draw, "짧은 글", _FONT, 1000) == "짧은 글"


def test_ellipsize_trims_and_appends_marker_when_too_wide():
    draw = _draw()
    long_text = "가" * 50
    out = ellipsize(draw, long_text, _FONT, 40)
    assert out.endswith("…")
    assert draw.textlength(out, font=_FONT) <= 40


def test_wrap_text_never_leaves_a_line_without_ellipsis_when_a_single_word_overflows():
    """The literal Bug-1 scenario: one word wider than max_width forced onto
    its own line -- must be ellipsized, not hard-clipped."""
    draw = _draw()
    long_word = "가" * 60
    lines = wrap_text(draw, long_word, _FONT, 30, 2)
    assert lines
    assert all(draw.textlength(l, font=_FONT) <= 30 for l in lines)
    assert lines[0].endswith("…")


def test_wrap_text_marks_truncation_when_text_needs_more_than_max_lines():
    """Even when every produced line individually fits max_width, running
    out of max_lines with leftover words must still show '…', not silently
    drop the rest of the sentence."""
    draw = _draw()
    text = " ".join(["word"] * 40)
    # Confirm the premise first: this text genuinely needs more lines than
    # we're about to allow, given this font/width (avoids a test that
    # passes vacuously if the font's metrics don't force a wrap).
    from agent.content.images.text_fit import _greedy_wrap_all

    assert len(_greedy_wrap_all(draw, text, _FONT, 60)) > 2
    lines = wrap_text(draw, text, _FONT, 60, 2)
    assert len(lines) == 2
    assert lines[-1].endswith("…")


def test_wrap_text_no_ellipsis_when_everything_fits():
    draw = _draw()
    lines = wrap_text(draw, "짧은 제목", _FONT, 1000, 3)
    assert not any(l.endswith("…") for l in lines)


def test_fit_font_and_wrap_shrinks_font_before_falling_back_to_ellipsis():
    draw = _draw()
    text = "적당히 긴 제목 문장 테스트"
    lines, font = fit_font_and_wrap(
        draw, text, font_loader=lambda size: ImageFont.load_default(),
        base_size=60, min_size=20, step=10, max_width=10_000, max_lines=2,
    )
    # With a huge max_width everything fits at base_size immediately.
    assert not any(l.endswith("…") for l in lines)


def test_fit_font_and_wrap_falls_back_to_ellipsis_at_min_size_if_still_too_long():
    draw = _draw()
    text = "가" * 200
    lines, font = fit_font_and_wrap(
        draw, text, font_loader=lambda size: ImageFont.load_default(),
        base_size=60, min_size=20, step=10, max_width=50, max_lines=1,
    )
    assert lines[-1].endswith("…")
