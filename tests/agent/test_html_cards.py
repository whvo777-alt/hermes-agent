"""Tests for the checklist-only HTML + Chromium card renderer."""

from __future__ import annotations

from types import SimpleNamespace

from PIL import Image
import pytest

from agent.content.images import html_cards


def _spec(*items: str, style: str = "checklist"):
    return SimpleNamespace(
        style=style,
        items=list(items),
        display_title="시험용 체크리스트",
        heading="시험용 체크리스트",
    )


def test_non_checklist_style_returns_none_without_chromium(tmp_path):
    output_path = tmp_path / "unsupported.png"

    rendered = html_cards.render_html_card(
        _spec("항목 1", "항목 2", "항목 3", style="timeline"),
        str(output_path),
    )

    assert rendered is None
    assert not output_path.exists()


def test_two_items_returns_none_without_chromium(tmp_path):
    output_path = tmp_path / "too-short.png"

    rendered = html_cards.render_html_card(
        _spec("항목 1", "항목 2"),
        str(output_path),
    )

    assert rendered is None
    assert not output_path.exists()


@pytest.mark.skipif(
    html_cards.find_chromium() is None,
    reason="chromium not installed",
)
def test_six_items_render_1080_by_1350_png(tmp_path):
    output_path = tmp_path / "six-items.png"

    rendered = html_cards.render_html_card(
        _spec(*(f"시험 항목 {index}" for index in range(1, 7))),
        str(output_path),
    )

    assert rendered == str(output_path)
    with Image.open(output_path) as image:
        assert image.size == (1080, 1350)


@pytest.mark.skipif(
    html_cards.find_chromium() is None,
    reason="chromium not installed",
)
def test_nine_items_render_taller_than_1080_by_1350_png(tmp_path):
    output_path = tmp_path / "nine-items.png"

    rendered = html_cards.render_html_card(
        _spec(*(f"시험 항목 {index}" for index in range(1, 10))),
        str(output_path),
    )

    assert rendered == str(output_path)
    with Image.open(output_path) as image:
        assert image.width == 1080
        assert image.height > 1350
