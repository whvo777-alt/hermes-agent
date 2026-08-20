"""Tests for the checklist-only HTML + Chromium card renderer."""

from __future__ import annotations

from types import SimpleNamespace

from PIL import Image
import pytest

from agent.content.images import html_cards


def _spec(*items: str, style: str = "checklist", **fields):
    return SimpleNamespace(
        style=style,
        items=list(items),
        display_title="시험용 체크리스트",
        heading="시험용 체크리스트",
        **fields,
    )


def test_non_checklist_style_returns_none_without_chromium(tmp_path):
    output_path = tmp_path / "unsupported.png"

    rendered = html_cards.render_html_card(
        _spec("항목 1", "항목 2", "항목 3", style="ox_quiz"),
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
def test_six_items_render_png(tmp_path):
    output_path = tmp_path / "six-items.png"

    rendered = html_cards.render_html_card(
        _spec(*(f"시험 항목 {index}" for index in range(1, 7))),
        str(output_path),
    )

    assert rendered == str(output_path)
    with Image.open(output_path) as image:
        assert image.width == 1080
        assert 700 <= image.height <= 2400


@pytest.mark.skipif(
    html_cards.find_chromium() is None,
    reason="chromium not installed",
)
def test_more_items_make_taller_png(tmp_path):
    six_path = tmp_path / "six-items.png"
    nine_path = tmp_path / "nine-items.png"

    six_rendered = html_cards.render_html_card(
        _spec(*(f"시험 항목 {index}" for index in range(1, 7))),
        str(six_path),
    )
    nine_rendered = html_cards.render_html_card(
        _spec(*(f"시험 항목 {index}" for index in range(1, 10))),
        str(nine_path),
    )

    assert six_rendered == str(six_path)
    assert nine_rendered == str(nine_path)
    with Image.open(six_path) as six_image, Image.open(nine_path) as nine_image:
        assert six_image.width == 1080
        assert nine_image.width == 1080
        assert nine_image.height > six_image.height


@pytest.mark.parametrize(
    ("style", "fields"),
    [
        (
            "grid",
            {
                "table": [
                    ["구분", "기준"],
                    ["가", "하나"],
                    ["나", "둘"],
                    ["다", "셋"],
                ]
            },
        ),
        (
            "qa",
            {"qa_pairs": [("질문이 하나 있습니다", "답변은 이렇습니다.")]},
        ),
        (
            "quote",
            {
                "quote_text": (
                    "잠을 억지로 재촉하기보다 각성감을 높이는 행동을 줄이는 쪽이 먼저입니다."
                )
            },
        ),
        (
            "risk_tier",
            {
                "risk_tiers": [
                    ("safe", "낮음", "부담이 적은 범위입니다."),
                    ("danger", "높음", "영향을 줄 수 있는 범위입니다."),
                ]
            },
        ),
    ],
    ids=["grid", "qa", "quote", "risk-tier"],
)
@pytest.mark.skipif(
    html_cards.find_chromium() is None,
    reason="chromium not installed",
)
def test_additional_styles_render_1080_wide_png(tmp_path, style, fields):
    output_path = tmp_path / f"{style}.png"

    rendered = html_cards.render_html_card(
        _spec(style=style, **fields),
        str(output_path),
    )

    assert rendered == str(output_path)
    with Image.open(output_path) as image:
        assert image.width == 1080
