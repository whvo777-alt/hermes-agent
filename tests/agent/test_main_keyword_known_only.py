"""Regression tests for known-only main-keyword selection."""

from __future__ import annotations

from agent.content.config import categories
from agent.content.memory import corpus_sync
from agent.content.memory.content_memory import add_content


def test_basic_keyword_in_title_is_selected(monkeypatch):
    monkeypatch.setattr(categories, "get_effective_keywords", lambda _category_id: [])

    known = corpus_sync._known_keywords("health", ["다이어트"])

    assert corpus_sync._guess_main_keyword("다이어트 식단을 고르는 기준", known) == "다이어트"


def test_cached_keyword_is_selected(monkeypatch):
    monkeypatch.setattr(
        categories,
        "get_effective_keywords",
        lambda _category_id: ["인바디"],
    )

    known = corpus_sync._known_keywords("health", ["다이어트"])

    assert known == ["다이어트", "인바디"]
    assert corpus_sync._guess_main_keyword("인바디 결과를 읽는 방법", known) == "인바디"


def test_unknown_title_returns_empty_keyword():
    assert corpus_sync._guess_main_keyword("정리하, 생활 습관을 바꾸는 방법", ["다이어트"]) == ""


def test_known_keyword_is_not_replaced_by_random_leading_word():
    known = ["인바디"]

    assert corpus_sync._guess_main_keyword("정리하, 인바디 결과를 읽는 방법", known) == "인바디"
    assert corpus_sync._guess_main_keyword("일어나면, 수면 습관을 점검하는 방법", known) == ""


def test_leading_keyword_is_used_only_when_known():
    assert corpus_sync._guess_main_keyword("인바디, 체성분을 확인하는 방법", ["인바디"]) == "인바디"
    assert corpus_sync._guess_main_keyword("일어나면, 목이 뻐근할 때 확인할 것", ["수면"]) == ""


def test_keyword_cache_failure_falls_back_to_extra_keywords(monkeypatch):
    def fail_get_effective_keywords(_category_id):
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(categories, "get_effective_keywords", fail_get_effective_keywords)

    assert corpus_sync._known_keywords("health", ["다이어트"]) == ["다이어트"]
    assert corpus_sync._known_keywords("", ["다이어트"]) == ["다이어트"]


def test_empty_keyword_can_be_recorded_without_breaking():
    keyword = corpus_sync._guess_main_keyword("정리하, 설명이 없는 제목", ["다이어트"])

    added = add_content(
        {"version": 1, "updatedAt": None, "items": []},
        {
            "date": "2026-09-07",
            "platform": "blogspot",
            "category": "health",
            "topic": "설명 없는 제목",
            "title": "설명 없는 제목",
            "mainKeyword": keyword,
            "subKeywords": [],
            "slug": "empty-main-keyword",
        },
    )

    assert added["item"]["mainKeyword"] == ""
