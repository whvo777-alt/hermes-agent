"""Tests for topic-aware external reference links in agent.content.seo_enrich.

Regression coverage for: a health-category post about "건강보험료" was
getting a random health-category institution (식약처/질병관리청/...) that
has nothing to do with insurance premiums, because link selection only ever
looked at category_id. Links must now prefer a topic-keyword match
(_TOPIC_EXTERNAL_LINKS) before falling back to the category pool.
"""

from __future__ import annotations

from unittest.mock import patch

from agent.content.seo_enrich import (
    _pick_external_links_for_topic,
    _search_fallback_links,
    append_external_links,
)


def test_topic_match_wins_over_category_pool():
    links = _pick_external_links_for_topic(
        topic_title="건강보험료 아끼는 법", focus_keyword="", category_id="health",
        platform_id="wordpress", seed="s1",
    )
    urls = [url for _label, url in links]
    assert "https://www.nhis.or.kr/" in urls  # 국민건강보험공단
    assert len(links) == 2  # topped up from the category pool for the 2nd slot


def test_topic_match_excludes_unrelated_category_defaults():
    """The old bug: 식약처/질병관리청 riding along just because category=health."""
    links = _pick_external_links_for_topic(
        topic_title="국민연금 예상 수령액 계산법", focus_keyword="", category_id="health",
        platform_id="wordpress", seed="s2",
    )
    urls = [url for _label, url in links]
    assert "https://www.nps.or.kr/" in urls  # 국민연금공단
    assert "https://www.mfds.go.kr/" not in urls  # 식약처 — unrelated to this topic


def test_no_topic_title_preserves_old_category_only_behavior():
    """Callers that don't pass topic_title/focus_keyword must be unaffected."""
    links = _pick_external_links_for_topic(
        topic_title="", focus_keyword="", category_id="health",
        platform_id="wordpress", seed="s3",
    )
    assert len(links) == 2
    urls = {url for _label, url in links}
    from agent.content.seo_enrich import _EXTERNAL_LINKS

    pool_urls = {url for _label, url in _EXTERNAL_LINKS["health"]}
    assert urls <= pool_urls


def test_unmapped_topic_falls_back_to_search_then_category_pool():
    with patch("agent.content.seo_enrich._search_fallback_links", return_value=[]) as mocked:
        links = _pick_external_links_for_topic(
            topic_title="완전히 새로운 희귀 주제입니다", focus_keyword="", category_id="health",
            platform_id="wordpress", seed="s4",
        )
    mocked.assert_called_once()
    assert len(links) == 2  # degrades to the category pool, never returns nothing


def test_search_fallback_filters_to_official_domains_only():
    fake_result = {
        "success": True,
        "data": {
            "web": [
                {"title": "블로그 광고 글", "url": "https://random-blog.example.com/post"},
                {"title": "국민건강보험공단", "url": "https://www.nhis.or.kr/main.do"},
            ]
        },
    }

    class _FakeProvider:
        def supports_search(self):
            return True

        def is_available(self):
            return True

        def search(self, query, limit=5):
            return fake_result

    with patch("agent.web_search_registry.get_active_search_provider", return_value=_FakeProvider()):
        links = _search_fallback_links("아무 주제")
    assert links == [("국민건강보험공단", "https://www.nhis.or.kr/main.do")]


def test_search_fallback_never_raises_when_provider_missing():
    with patch("agent.web_search_registry.get_active_search_provider", return_value=None):
        assert _search_fallback_links("아무 주제") == []


def test_append_external_links_uses_topic_matched_institution():
    markdown = "# 건강보험료 아끼는 법\n\n본문입니다.\n"
    out = append_external_links(
        markdown, category_id="health", platform_id="wordpress", seed="s5",
        topic_title="건강보험료 아끼는 법",
    )
    assert "국민건강보험공단" in out
    assert "https://www.nhis.or.kr/" in out
