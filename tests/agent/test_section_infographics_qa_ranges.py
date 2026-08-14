"""Regression tests for Q&A infographic source-range coverage."""

from __future__ import annotations

from agent.content.images.section_infographics import _extract_qa


def test_qa_strip_ranges_match_the_two_pairs_rendered_on_the_card():
    lines = str("""질문: 무릎이 발끝보다 앞으로 나오면 스쿼트를 잘못한 것인가요?  
답변: 무릎의 위치만으로 잘못된 자세라고 단정하기 어렵습니다. 발목의 움직임, 다리 길이, 발 간격에 따라 앞으로 이동할 수 있습니다. 무릎이 발끝 방향을 따라가는지, 발바닥이 유지되는지, 통증이 없는지를 함께 확인하세요.

질문: 허벅지가 바닥과 평행할 때까지 내려가야 하나요?  
답변: 꼭 그렇지는 않습니다. 초보자는 발바닥과 허리 정렬, 무릎 방향을 유지할 수 있는 깊이에서 시작하는 것이 좋습니다. 깊어질수록 자세가 무너지거나 통증이 나타난다면 범위를 줄이고, 움직임이 안정된 뒤 천천히 조정합니다.

질문: 집에서 스쿼트를 할 때 맨발이 좋은가요?  
답변: 특정 신발이나 맨발이 모든 사람에게 같다고 말하기는 어렵습니다. 미끄럽지 않고 바닥을 안정적으로 느낄 수 있는 환경이 우선입니다. 양말만 신고 미끄러운 바닥에서 하거나, 굽이 불안정한 신발을 신고 반복하는 것은 피하세요.

질문: 스쿼트 후 무릎 주변이 뻐근하면 계속해도 되나요?  
답변: 뻐근함의 원인과 정도를 글만으로 구분할 수 없습니다. 반복할수록 심해지는 통증, 특정 지점의 날카로운 통증, 붓기나 불안정감이 동반되는 경우에는 운동을 중단하고 전문가에게 확인받아야 합니다. 단순히 참고 반복하는 것은 안전한 조정 방법이 아닙니다.""").splitlines()

    pairs, ranges = _extract_qa(lines, base=0)

    assert len(pairs) == 2
    assert len(ranges) == 2
    assert ranges == [(0, 1), (3, 4)]
