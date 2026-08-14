"""Regression test for the document-level infographic count default."""

from agent.content.images.section_infographics import extract_infographic_specs


def _six_eligible_sections() -> str:
    sections = []
    for index in range(1, 7):
        sections.append(
            f"## 대단원 {index}\n"
            f"- 조건을 충족하는 항목 {index}-1\n"
            f"- 조건을 충족하는 항목 {index}-2\n"
            f"- 조건을 충족하는 항목 {index}-3\n"
        )
    return "# 제목\n\n" + "\n".join(sections)


def test_default_max_count_limits_specs_to_three():
    specs = extract_infographic_specs(_six_eligible_sections(), style_seed="max-count")

    assert len(specs) <= 3
