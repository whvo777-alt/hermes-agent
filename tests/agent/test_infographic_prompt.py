"""Tests for body-data infographic prompt construction."""

from agent.content.images.infographic_prompt import build_infographic_prompt
from agent.content.images.section_infographics import InfographicSpec


def _spec(**kwargs) -> InfographicSpec:
    heading = kwargs.pop("heading", "시험용 제목")
    display_title = kwargs.pop("display_title", heading)
    return InfographicSpec(
        heading=heading,
        display_title=display_title,
        **kwargs,
    )


def test_checklist_prompt_keeps_five_items():
    items = [
        "서 있을 때 한쪽 발에만 체중이 실리는 느낌이 있는가",
        "양발의 방향이 지나치게 안쪽 또는 바깥쪽으로 벌어지는가",
        "의자에서 일어날 때 무릎이 안쪽으로 모이는가",
        "허리를 세우려고 가슴을 과하게 내밀거나 갈비뼈를 들고 있지는 않은가",
        "내려갈 때 발바닥이 바닥에서 뜨거나 몸이 한쪽으로 기우는가",
    ]

    prompt = build_infographic_prompt(
        _spec(style="checklist", items=items),
        category_id="health",
        category_name="건강",
    )

    assert "체크박스 5개" in prompt
    for item in items:
        assert f'"{item}"' in prompt


def test_timeline_prompt_preserves_item_order():
    items = ["준비한다", "호흡을 정리한다", "동작을 시작한다"]

    prompt = build_infographic_prompt(_spec(style="timeline", items=items))

    assert prompt.index("준비한다") < prompt.index("호흡을 정리한다") < prompt.index("동작을 시작한다")


def test_qa_prompt_contains_questions_and_answers():
    pairs = [
        ("언제 쉬어야 하나요?", "통증이 생기면 동작을 멈춥니다."),
        ("얼마나 반복하나요?", "몸 상태에 맞춰 횟수를 정합니다."),
    ]

    prompt = build_infographic_prompt(_spec(style="qa", qa_pairs=pairs))

    for question, answer in pairs:
        assert question in prompt
        assert answer in prompt


def test_table_prompt_contains_header_and_body_cells():
    table = [
        ["구분", "기준"],
        ["초보", "가볍게 시작"],
        ["숙련", "강도를 조절"],
    ]

    prompt = build_infographic_prompt(_spec(style="grid", table=table))

    for cell in ("구분", "기준", "초보", "가볍게 시작", "숙련", "강도를 조절"):
        assert f'"{cell}"' in prompt


def test_one_item_returns_empty_prompt():
    prompt = build_infographic_prompt(_spec(style="checklist", items=["하나만 있음"]))

    assert prompt == ""


def test_eight_items_are_capped_at_six():
    items = [
        "첫 번째 문장",
        "두 번째 문장",
        "세 번째 문장",
        "네 번째 문장",
        "다섯 번째 문장",
        "여섯 번째 문장",
        "일곱 번째 문장",
        "여덟 번째 문장",
    ]

    prompt = build_infographic_prompt(_spec(style="checklist", items=items))

    for item in items[:6]:
        assert item in prompt
    assert "일곱 번째 문장" not in prompt
    assert "여덟 번째 문장" not in prompt


def test_markdown_symbols_are_removed_before_insertion():
    items = [
        "**굵게** `코드` [링크](https://example.com)",
        "- 앞머리 문장",
    ]

    prompt = build_infographic_prompt(_spec(style="checklist", items=items))

    assert "굵게" in prompt
    assert "코드" in prompt
    assert "링크" in prompt
    assert "**" not in prompt
    assert "`" not in prompt
    assert "https://example.com" not in prompt
    assert "- 앞머리 문장" not in prompt


def test_unknown_style_uses_summary_structure_instead_of_empty():
    prompt = build_infographic_prompt(
        _spec(style="알 수 없는 유형", items=["핵심 하나", "핵심 둘"])
    )

    assert prompt
    assert "둥근 카드" in prompt
    assert "핵심 하나" in prompt
    assert "핵심 둘" in prompt


def test_prompt_always_contains_common_design_and_korean_text_rules():
    prompt = build_infographic_prompt(
        _spec(style="checklist", items=["첫 항목", "둘째 항목"]),
        category_id="health",
        category_name="건강",
    )

    assert "한국 건강·웰니스 블로그용" in prompt
    assert "밝은 아이보리 배경" in prompt
    assert "모바일에서 읽기 쉬운 큰 글씨" in prompt
    assert "모든 글자는 한국어로 쓴다" in prompt
    assert "따옴표 안의 문장은 글자 하나 바꾸지 말고 그대로 쓴다" in prompt


def test_prompt_does_not_repeat_category_or_title():
    title = "맨몸스쿼트 전 자세 점검 5가지"

    prompt = build_infographic_prompt(
        _spec(display_title=title, style="checklist", items=["첫 항목", "둘째 항목"]),
        category_id="health",
        category_name="",
    )

    assert "분야 이름은" not in prompt
    assert f'제목은 "{title}".' in prompt
    assert prompt.count(f'"{title}"') == 1
    assert "이라는 제목 아래에" not in prompt
    assert "제목 아래에 큰 체크박스" in prompt


def test_risk_tier_prompt_keeps_source_order_and_flips_only_colors():
    prompt = build_infographic_prompt(
        _spec(
            style="risk_tier",
            risk_tiers=[
                ("safe", "안전", "안전한 상태"),
                ("mid", "주의", "주의할 상태"),
                ("risk", "위험", "위험한 상태"),
            ],
        ),
    )

    assert "위는 민트, 가운데는 낮은 채도의 주황, 아래는 낮은 채도의 코랄." in prompt
    assert "아래는 빨강" not in prompt
    assert prompt.index('이름표: "안전"') < prompt.index('이름표: "주의"')
    assert prompt.index('이름표: "주의"') < prompt.index('이름표: "위험"')


def test_health_prompt_contains_self_diagnosis_safety_rule():
    prompt = build_infographic_prompt(
        _spec(style="checklist", items=["첫 항목", "둘째 항목"]),
        category_id="health",
    )

    assert "병을 단정하거나 스스로 진단하게 만드는 글자를 넣지 않는다." in prompt


def test_self_dev_prompt_blocks_trophy_and_rocket_imagery():
    prompt = build_infographic_prompt(
        _spec(style="checklist", items=["첫 항목", "둘째 항목"]),
        category_id="self-dev",
    )

    assert "돈다발, 트로피, 로켓, 슈퍼히어로 그림을 넣지 않는다." in prompt


def test_unknown_category_prompt_is_still_created():
    prompt = build_infographic_prompt(
        _spec(style="checklist", items=["첫 항목", "둘째 항목"]),
        category_id="finance",
    )

    assert prompt


def test_qa_prompt_uses_vertical_cards_not_speech_bubbles():
    prompt = build_infographic_prompt(
        _spec(style="qa", qa_pairs=[("질문", "답변")]),
    )

    assert "문답 하나를 카드 하나로 만든다." in prompt
    assert "카드들을 같은 크기로 세로로 나란히 배치한다." in prompt
    assert "말풍선" not in prompt


def test_four_to_five_ratio_is_in_every_prompt_type():
    specs = [
        _spec(style="checklist", items=["첫 항목", "둘째 항목"]),
        _spec(style="qa", qa_pairs=[("질문", "답변")]),
        _spec(
            style="risk_tier",
            risk_tiers=[
                ("safe", "안전", "안전한 상태"),
                ("mid", "주의", "주의할 상태"),
                ("risk", "위험", "위험한 상태"),
            ],
        ),
        _spec(style="gauge", gauge_stat=("7", "일"), gauge_label="기준"),
        _spec(style="quote", quote_text="한 문장"),
    ]

    for spec in specs:
        prompt = build_infographic_prompt(spec)
        assert "세로로 긴 4:5 비율. 1080 x 1350 픽셀." in prompt
