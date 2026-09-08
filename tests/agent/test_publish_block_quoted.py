"""Quoted publication-blocker markers are treated as content, not notes."""

from agent.content.publish_on_approval import _find_publish_content_blockers


def test_unquoted_marker_blocks_publication():
    assert _find_publish_content_blockers("검토 필요: 이 수치 재확인") == [("검토 필요", 1)]


def test_curly_double_quoted_marker_is_allowed():
    assert _find_publish_content_blockers("“검토 필요”는 표현의 나쁜 예다.") == []


def test_straight_double_quoted_marker_is_allowed():
    assert _find_publish_content_blockers('"검토 필요"는 표현의 나쁜 예다.') == []


def test_single_quoted_marker_is_allowed():
    assert _find_publish_content_blockers("'검토 필요'는 표현의 나쁜 예다.") == []


def test_mixed_quoted_and_unquoted_marker_blocks():
    content = '"검토 필요"는 인용이고 검토 필요: 수치를 다시 확인한다.'

    assert _find_publish_content_blockers(content) == [("검토 필요", 1)]


def test_unclosed_opening_quote_does_not_bypass_blocker():
    assert _find_publish_content_blockers("“검토 필요는 아직 확인하지 않았다.") == [
        ("검토 필요", 1)
    ]


def test_other_marker_uses_the_same_quote_rule():
    assert _find_publish_content_blockers("“현재 제공된 자료”를 인용한다.") == []
    assert _find_publish_content_blockers("현재 제공된 자료: 원문을 첨부한다.") == [
        ("현재 제공된 자료", 1)
    ]
