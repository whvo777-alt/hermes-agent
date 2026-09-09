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


def test_marker_alone_on_line_blocks_publication():
    assert _find_publish_content_blockers("검토 필요") == [("검토 필요", 1)]


def test_marker_at_line_start_with_only_closing_quote_blocks():
    assert _find_publish_content_blockers('검토 필요"') == [("검토 필요", 1)]


def test_marker_at_line_end_with_only_opening_quote_blocks():
    assert _find_publish_content_blockers('"검토 필요') == [("검토 필요", 1)]


def test_marker_with_straight_quotes_on_both_sides_is_allowed():
    assert _find_publish_content_blockers('"검토 필요"') == []


def test_marker_with_curly_quotes_on_both_sides_is_allowed():
    assert _find_publish_content_blockers("“검토 필요”") == []


def test_marker_in_middle_of_sentence_blocks():
    assert _find_publish_content_blockers("본문 검토 필요 입니다") == [("검토 필요", 1)]


def test_marker_in_list_item_blocks():
    assert _find_publish_content_blockers("- 검토 필요") == [("검토 필요", 1)]
