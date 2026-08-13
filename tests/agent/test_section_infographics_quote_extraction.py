"""Regression tests for quote sentence extraction boundaries."""

from agent.content.images.section_infographics import _extract_quote


def test_extract_quote_does_not_split_korean_connective_ending():
    text = (
        "스쿼트는 무릎만 굽혔다 펴는 동작이 아닙니다. "
        "엉덩이를 뒤로 보내고, 무릎과 발목이 함께 움직이며, "
        "상체가 지나치게 무너지지 않도록 균형을 잡아야 합니다."
    )

    assert _extract_quote([text]) == "스쿼트는 무릎만 굽혔다 펴는 동작이 아닙니다."


def test_extract_quote_does_not_split_korean_comparative_particle():
    text = (
        "집에서 스쿼트를 할 때는 운동복이나 기구보다 주변 환경이 먼저입니다. "
        "미끄러운 양말을 신고 바닥에서 반복하거나, 뒤로 물러날 공간이 없는 곳에서 "
        "의자를 이용하면 균형을 잃을 수 있습니다."
    )

    assert _extract_quote([text]) == "집에서 스쿼트를 할 때는 운동복이나 기구보다 주변 환경이 먼저입니다."
