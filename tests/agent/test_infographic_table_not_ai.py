"""Regression tests keeping table cards on the existing infographic path."""

from agent.content.images.infographic_prompt import (
    _clean_infographic_text,
    build_infographic_alt,
    build_infographic_prompt,
)
from agent.content.images.section_infographics import InfographicSpec, extract_infographic_specs


def _spec(**kwargs) -> InfographicSpec:
    heading = kwargs.pop("heading", "시험용 제목")
    display_title = kwargs.pop("display_title", heading)
    return InfographicSpec(
        heading=heading,
        display_title=display_title,
        **kwargs,
    )


def _table() -> list[list[str]]:
    return [
        ["구분", "기준", "다음 행동"],
        ["초보", "가볍게 시작", "첫 단계만 적기"],
        ["숙련", "강도를 조절", "다음 순서 정하기"],
    ]


def test_table_prompt_is_empty():
    # 표는 기존 인포그래픽 PNG를 사용하고 AI 후보에서는 제외한다.
    assert build_infographic_prompt(_spec(style="grid", table=_table())) == ""


def test_table_alt_is_empty():
    # 표 이미지를 AI에서 제외할 때 대응하는 ALT도 남기지 않는다.
    assert build_infographic_alt(_spec(style="grid", table=_table())) == ""


def test_checklist_prompt_remains_available():
    # 표 차단이 체크리스트 prompt까지 막지 않는지 확인한다.
    prompt = build_infographic_prompt(
        _spec(style="checklist", items=["첫 번째 기준", "두 번째 기준"])
    )

    assert prompt
    assert "첫 번째 기준" in prompt
    assert "두 번째 기준" in prompt


def test_timeline_prompt_remains_available():
    # 표 차단이 타임라인 prompt와 원래 순서를 바꾸지 않는지 확인한다.
    prompt = build_infographic_prompt(
        _spec(style="timeline", items=["준비", "시작", "마무리"])
    )

    assert prompt
    assert prompt.index("준비") < prompt.index("시작") < prompt.index("마무리")


def test_non_table_kinds_and_text_cleaning_remain_available():
    # 표가 아닌 갈래와 공통 텍스트 정리 규칙은 그대로 유지한다.
    specs = [
        _spec(style="before_after", before_pairs=[("이전", "이후")]),
        _spec(style="qa", qa_pairs=[("질문", "답변")]),
        _spec(
            style="risk_tier",
            risk_tiers=[
                ("safe", "안전", "안전한 상태"),
                ("mid", "주의", "주의할 상태"),
                ("risk", "위험", "위험한 상태"),
            ],
        ),
        _spec(style="ox_quiz", ox_pair=("통념", "사실")),
        _spec(style="gauge", gauge_stat=("7", "일"), gauge_label="기준"),
        _spec(style="quote", quote_text="한 문장"),
    ]

    assert all(build_infographic_prompt(spec) for spec in specs)

    cleaned = _clean_infographic_text(
        "**굵게** [링크](https://example.com) " + "긴 문장 " * 20
    )
    assert "**" not in cleaned
    assert "https://example.com" not in cleaned
    assert len(cleaned) <= 45
    assert not cleaned.endswith(" ")


def test_table_prompt_is_empty_for_every_variant():
    # variant가 바뀌어도 표는 계속 AI 후보에서 제외한다.
    spec = _spec(style="grid", table=_table())

    assert build_infographic_prompt(spec, variant=0) == ""
    assert build_infographic_prompt(spec, variant=9) == ""


def test_table_spec_is_still_extracted_without_rendering():
    # AI prompt를 비워도 기존 추출 경로가 표 spec을 계속 만든다.
    markdown = """## 표 꼭지
본문 설명입니다.

| 구분 | 기준 | 다음 행동 |
| --- | --- | --- |
| 초보 | 가볍게 시작 | 첫 단계만 적기 |
| 숙련 | 강도를 조절 | 다음 순서 정하기 |
"""

    specs = extract_infographic_specs(markdown, max_count=7, style_seed="table-test")

    table_specs = [spec for spec in specs if spec.heading == "표 꼭지" and spec.table]
    assert len(table_specs) == 1
    assert table_specs[0].table == _table()
