"""Regression tests for main-keyword selection from content titles."""

from __future__ import annotations

from agent.content.memory.corpus_sync import _guess_main_keyword


_KEYWORDS = ["다이어트", "운동", "스트레칭", "수면", "혈당", "걷기", "요가", "자세", "건강식품"]


def test_leading_single_word_before_comma_is_main_keyword():
    title = "어깨결림, 목과 어깨가 함께 뻐근할 때 스트레칭과 진료 판단"

    assert _guess_main_keyword(title, _KEYWORDS) == "어깨결림"


def test_multiple_words_before_comma_do_not_use_leading_phrase_as_keyword():
    title = "혈당 관리 식단, 밥 양부터 간식과 외식까지 고르는 법"

    assert _guess_main_keyword(title, _KEYWORDS) == "혈당"
    assert _guess_main_keyword(title, _KEYWORDS) != "혈당 관리 식단"


def test_html_entity_digits_are_not_selected_as_main_keyword():
    title = "&#8220;제발 스레드(Thread) 좀 써주세요&#8221;: 슬랙 대화 예절"

    assert _guess_main_keyword(title) != "8220"


def test_digit_starting_token_is_not_selected_as_main_keyword():
    title = "7가지 질문으로 정리하는 저칼로리 간식과 단백질 간식 선택법"

    result = _guess_main_keyword(title)
    assert result != "7가지"
    assert not result[:1].isdigit()


def test_josa_is_removed_from_main_keyword():
    assert _guess_main_keyword("노션에 날개 달기: 삭막한 페이지에 날씨, 시계 심는 위젯 사이트 추천") == "노션"


def test_two_character_word_keeps_its_final_character():
    assert _guess_main_keyword("요가 초보가 수업 전 확인할 7가지 기준") == "요가"


def test_category_keyword_fallback_is_preserved_for_legacy_titles():
    title = "다이어트 시작할 때 알아야 할 식단과 운동 기준"

    assert _guess_main_keyword(title, _KEYWORDS) == "다이어트"


def test_comma_lead_starting_with_digit_is_not_used_as_main_keyword():
    title = "30대, 직장인을 위한 아침 스트레칭 순서"

    assert _guess_main_keyword(title, _KEYWORDS) != "30대"


def test_common_word_is_not_used_as_main_keyword():
    title = "자고 일어나면 목결림이 생길 때, 살펴볼 생활 습관과 대처 순서"

    assert _guess_main_keyword(title, _KEYWORDS) != "자고"


def test_english_main_keyword_is_lowercased():
    title = "PT, 운동 초보는 가격과 계약 조건을 먼저 비교할까?"

    assert _guess_main_keyword(title, _KEYWORDS) == "pt"


def test_hangul_main_keyword_is_preserved():
    title = "요가 초보가 수업 전 확인할 7가지 기준"

    assert _guess_main_keyword(title, _KEYWORDS) == "요가"
