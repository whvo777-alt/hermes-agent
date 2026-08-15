"""Regression tests for preserving lists that exceed card capacity."""

from agent.content.images.section_infographics import extract_infographic_specs


def _section(lines: list[str], heading: str = "오늘 점심 전에 확인할 목록") -> str:
    return "# 제목\n\n## " + heading + "\n" + "\n".join(lines) + "\n"


def _numbered_items(count: int) -> list[str]:
    return [f"{index}. 번호 항목 {index}" for index in range(1, count + 1)]


def _step_items(count: int) -> list[str]:
    return [f"### {index}단계: 단계 항목 {index}" for index in range(1, count + 1)]


def _checklist_items(count: int) -> list[str]:
    return [f"- [ ] 체크 항목 {index}" for index in range(1, count + 1)]


def _table_rows(count: int) -> list[str]:
    return [
        "| 기준 | 설명 |",
        "| --- | --- |",
        *[f"| {index} | 표 항목 {index} |" for index in range(1, count + 1)],
    ]


def test_numbered_list_over_card_limit_does_not_create_spec():
    specs = extract_infographic_specs(_section(_numbered_items(6)), style_seed="numbered-overflow")

    assert specs == []


def test_numbered_list_at_card_limit_still_creates_spec():
    specs = extract_infographic_specs(_section(_numbered_items(5)), style_seed="numbered-limit")

    assert len(specs) == 1
    assert specs[0].items == [f"번호 항목 {index}" for index in range(1, 6)]
    assert len(specs[0].strip_ranges) == 5


def test_step_list_over_card_limit_does_not_create_spec():
    specs = extract_infographic_specs(_section(_step_items(6)), style_seed="steps-overflow")

    assert specs == []


def test_step_list_at_card_limit_still_creates_spec():
    specs = extract_infographic_specs(_section(_step_items(5)), style_seed="steps-limit")

    assert len(specs) == 1
    assert specs[0].items == [f"단계 항목 {index}" for index in range(1, 6)]
    assert len(specs[0].strip_ranges) == 5


def test_checklist_over_card_limit_does_not_create_spec():
    specs = extract_infographic_specs(_section(_checklist_items(11)), style_seed="checklist-overflow")

    assert specs == []


def test_checklist_at_card_limit_still_creates_spec():
    specs = extract_infographic_specs(_section(_checklist_items(10)), style_seed="checklist-limit")

    assert len(specs) == 1
    assert specs[0].items == [f"체크 항목 {index}" for index in range(1, 11)]
    assert len(specs[0].strip_ranges) == 10


def test_table_over_card_limit_does_not_create_spec():
    specs = extract_infographic_specs(_section(_table_rows(6)), style_seed="table-overflow")

    assert specs == []


def test_table_at_card_limit_still_creates_spec():
    specs = extract_infographic_specs(_section(_table_rows(5)), style_seed="table-limit")

    assert len(specs) == 1
    assert specs[0].table == [
        ["기준", "설명"],
        *[[str(index), f"표 항목 {index}"] for index in range(1, 6)],
    ]
    assert len(specs[0].strip_ranges) == 1
