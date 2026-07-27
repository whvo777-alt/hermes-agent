"""Tests for internal-link enrichment in agent.content.seo_enrich.

Regression coverage for the SEO-check follow-up that raised the internal
link minimum from 2 to 3 and added same-category filtering for WordPress and
Blogspot (previously Blogspot had no internal linking at all).
"""

from __future__ import annotations

from unittest.mock import patch

from agent.content.seo_enrich import (
    append_internal_links,
    enrich_wordpress_markdown_for_seo,
    fetch_blogspot_internal_candidates,
    fetch_wordpress_internal_candidates,
)

_CANDIDATES = [
    {"id": "1", "title": "요가 초보 확인 기준", "link": "https://x.com/a"},
    {"id": "2", "title": "스트레칭 루틴 만들기", "link": "https://x.com/b"},
    {"id": "3", "title": "수면 자세 체크 포인트", "link": "https://x.com/c"},
    {"id": "4", "title": "AI 노트북 리뷰", "link": "https://x.com/d"},
]


def test_default_max_links_is_now_three():
    out = append_internal_links(
        "## 마무리\n끝.", site_url="https://x.com", candidates=_CANDIDATES,
        preferred_terms=["요가", "스트레칭", "수면"],
    )
    assert out.count("- [") == 3


def test_degrades_gracefully_with_fewer_than_three_candidates():
    """A brand-new category with only 1 same-category post yet must still
    publish — never error, never block on an artificial minimum."""
    one_candidate = [_CANDIDATES[0]]
    out = append_internal_links(
        "## 마무리\n끝.", site_url="https://x.com", candidates=one_candidate,
        preferred_terms=["요가"],
    )
    assert "요가 초보 확인 기준" in out
    assert out.count("- [") == 1


def test_no_candidates_falls_back_to_home_link_not_a_crash():
    out = append_internal_links("## 마무리\n끝.", site_url="https://x.com", candidates=[])
    assert "블로그 홈" in out


def test_fetch_wordpress_internal_candidates_adds_categories_filter():
    captured = {}

    class _FakeResponse:
        status_code = 200
        content = b"[]"

        def json(self):
            return []

    def _fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResponse()

    with patch("agent.content.seo_enrich.httpx.get", side_effect=_fake_get):
        fetch_wordpress_internal_candidates(
            site_url="https://x.com", auth_header="Basic xyz", categories=42,
        )
    assert "categories=42" in captured["url"]


def test_fetch_wordpress_internal_candidates_omits_filter_when_no_category():
    class _FakeResponse:
        status_code = 200
        content = b"[]"

        def json(self):
            return []

    with patch("agent.content.seo_enrich.httpx.get", return_value=_FakeResponse()) as mocked:
        fetch_wordpress_internal_candidates(site_url="https://x.com", auth_header="Basic xyz")
    assert "categories=" not in mocked.call_args.args[0]


def test_fetch_blogspot_internal_candidates_filters_by_label_and_maps_url_field():
    class _FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return {
                "items": [
                    {"id": "111", "title": "건강 관련 글", "url": "https://coco.blogspot.com/p1"},
                ]
            }

    with patch("agent.content.seo_enrich.httpx.get", return_value=_FakeResponse()) as mocked:
        results = fetch_blogspot_internal_candidates(
            blog_id="123", access_token="tok", label="건강/헬스",
        )
    assert results == [{"id": "111", "title": "건강 관련 글", "link": "https://coco.blogspot.com/p1"}]
    # Category names can contain "/" (e.g. "건강/헬스") — must go through
    # params= so httpx URL-encodes it, never raw f-string interpolation.
    assert mocked.call_args.kwargs["params"]["labels"] == "건강/헬스"


def test_fetch_blogspot_internal_candidates_missing_inputs_returns_empty():
    assert fetch_blogspot_internal_candidates(blog_id="", access_token="", label="") == []


def test_enrich_wordpress_markdown_internal_added_matches_actual_target():
    """Regression: internal_added used to be hardcoded min(2, len(internal))
    regardless of the real max_links used, so it silently under/over-reported
    once the target changed. It must now track internal_link_target."""
    with patch(
        "agent.content.seo_enrich.fetch_wordpress_internal_candidates",
        return_value=_CANDIDATES,
    ):
        result = enrich_wordpress_markdown_for_seo(
            "## 마무리\n끝.",
            focus_keyword="요가",
            category_id="health",
            site_url="https://x.com",
            auth_header="Basic xyz",
            live=True,
            wp_category_term_id=7,
            internal_link_target=3,
        )
    assert result["internalCount"] == 3
