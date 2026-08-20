"""Regression test for the document-level infographic count default."""

from inspect import signature

from agent.content.images.section_infographics import extract_infographic_specs


def _eight_eligible_sections() -> str:
    sections = []
    for index in range(1, 9):
        sections.append(
            f"## 대단원 {index}\n"
            f"- 조건을 충족하는 항목 {index}-1\n"
            f"- 조건을 충족하는 항목 {index}-2\n"
            f"- 조건을 충족하는 항목 {index}-3\n"
        )
    return "# 제목\n\n" + "\n".join(sections)


def test_default_max_count_limits_specs_to_default():
    specs = extract_infographic_specs(_eight_eligible_sections(), style_seed="max-count")
    default_max_count = signature(extract_infographic_specs).parameters["max_count"].default

    assert len(specs) == default_max_count
