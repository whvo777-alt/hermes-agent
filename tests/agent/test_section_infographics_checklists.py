"""Regression tests for checklist infographic extraction and HTML fallback."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image, ImageChops
import pytest

from agent.content.images.section_infographics import (
    _HEADER_H,
    _render_checklist_card,
    _render_checklist_card_skin_a,
    _render_checklist_card_skin_b,
    extract_infographic_specs,
)
from agent.content.markdown_html import markdown_to_html
from agent.content.publish_on_approval import _strip_infographic_source_blocks


def _checkbox_markdown(items: list[str]) -> str:
    return "# 제목\n\n## 체크 목록\n" + "\n".join(items) + "\n"


def _seven_checkboxes() -> list[str]:
    return [f"- [ ] 체크 항목 {index}" for index in range(1, 8)]


def test_seven_check_items_are_all_in_spec_and_strip_ranges():
    markdown = _checkbox_markdown(_seven_checkboxes())

    spec = extract_infographic_specs(markdown, max_count=5, style_seed="checklist")[0]
    stripped = _strip_infographic_source_blocks(
        markdown, [{"strip_ranges": spec.strip_ranges}]
    )

    assert spec.items == [f"체크 항목 {index}" for index in range(1, 8)]
    assert len(spec.strip_ranges) == 7
    assert stripped.count("- [ ]") == 0


def test_ten_check_items_are_all_in_spec_and_strip_ranges():
    markdown = _checkbox_markdown(
        [f"- [x] 열 개 항목 {index}" for index in range(1, 11)]
    )

    spec = extract_infographic_specs(markdown, max_count=5, style_seed="checklist")[0]
    stripped = _strip_infographic_source_blocks(
        markdown, [{"strip_ranges": spec.strip_ranges}]
    )

    assert spec.items == [f"열 개 항목 {index}" for index in range(1, 11)]
    assert len(spec.strip_ranges) == 10
    assert stripped.count("- [x]") == 0


def test_check_items_after_an_intervening_paragraph_join_the_same_card():
    markdown = _checkbox_markdown(
        _seven_checkboxes()[:3]
        + ["일반 문단입니다."]
        + _seven_checkboxes()[3:]
    )

    spec = extract_infographic_specs(markdown, max_count=5, style_seed="checklist")[0]

    assert spec.items == [f"체크 항목 {index}" for index in range(1, 8)]
    assert len(spec.strip_ranges) == 7


def test_markdown_html_removes_task_checkbox_tokens_from_bullets():
    markdown = _checkbox_markdown(
        [
            "- [ ] 미완료 항목",
            "- [x] 완료 항목",
            "- [X] 대문자 완료 항목",
        ]
    )

    html = markdown_to_html(markdown)

    assert "[ ]" not in html
    assert "[x]" not in html
    assert "[X]" not in html
    assert "미완료 항목" in html
    assert "완료 항목" in html
    assert "대문자 완료 항목" in html


def test_numeric_checkbox_items_stay_checklist_not_gauge():
    markdown = _checkbox_markdown(
        [
            "- [ ] 5mg 복용량 확인",
            "- [x] 10% 할인 여부 확인",
            "- [ ] 3회 반복 여부 확인",
            "- [ ] 전문가 상담 확인",
        ]
    )

    spec = extract_infographic_specs(markdown, max_count=5, style_seed="checklist")[0]

    assert spec.style == "checklist"
    assert spec.items == [
        "5mg 복용량 확인",
        "10% 할인 여부 확인",
        "3회 반복 여부 확인",
        "전문가 상담 확인",
    ]


@pytest.mark.parametrize(
    "renderer",
    [
        _render_checklist_card,
        _render_checklist_card_skin_a,
        _render_checklist_card_skin_b,
    ],
    ids=["default", "skin-a", "skin-b"],
)
def test_ten_item_checklist_cards_grow_and_draw_the_last_row(renderer):
    with tempfile.TemporaryDirectory(dir="/opt/data/Hermes-Agent") as temp_dir:
        image_sizes = {}
        for count in (5, 10):
            markdown = _checkbox_markdown(
                [f"- [ ] 카드 높이 항목 {index}" for index in range(1, count + 1)]
            )
            spec = extract_infographic_specs(markdown, max_count=5, style_seed="checklist")[0]
            output_path = Path(temp_dir) / f"checklist-{count}.png"
            renderer(spec, str(output_path), category_id="health")

            with Image.open(output_path) as image:
                image_sizes[count] = image.size
                if count == 10:
                    background = Image.new("RGB", image.size, image.getpixel((0, 0)))
                    assert ImageChops.difference(image, background).getbbox() is not None
                    last_row = image.crop((80, max(0, image.height - 110), 1140, image.height - 10))
                    last_background = Image.new("RGB", last_row.size, last_row.getpixel((0, 0)))
                    assert ImageChops.difference(last_row, last_background).getbbox() is not None

        assert image_sizes[10][1] > image_sizes[5][1]
        if renderer is _render_checklist_card:
            assert image_sizes[10][1] == _HEADER_H + 20 + 10 * 96 + 40
