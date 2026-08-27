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
    assert out.count("](https://x.com/") == 3


def test_degrades_gracefully_with_fewer_than_three_candidates():
    """A brand-new category with only 1 same-category post yet must still
    publish — never error, never block on an artificial minimum."""
    one_candidate = [_CANDIDATES[0]]
    out = append_internal_links(
        "## 마무리\n끝.", site_url="https://x.com", candidates=one_candidate,
        preferred_terms=["요가"],
    )
    assert "요가 초보 확인 기준" in out
    assert out.count("](https://x.com/") == 1


def test_no_candidates_falls_back_to_home_link_not_a_crash():
    out = append_internal_links("## 마무리\n끝.", site_url="https://x.com", candidates=[])
    assert "블로그 홈" in out


def test_fetch_wordpress_internal_candidates_adds_categories_filter():
    captured = []

    class _FakeResponse:
        status_code = 200
        content = b"[]"

        def json(self):
            return []

    def _fake_get(url, headers=None, timeout=None):
        captured.append(url)
        return _FakeResponse()

    with patch("agent.content.seo_enrich.httpx.get", side_effect=_fake_get):
        fetch_wordpress_internal_candidates(
            site_url="https://x.com", auth_header="Basic xyz", categories=42,
        )
    # An empty categorized result also triggers the site-wide top-up fetch
    # (see the next test), so at least one call -- not necessarily the last
    # -- must carry the categories filter.
    assert any("categories=42" in u for u in captured)


def test_fetch_wordpress_internal_candidates_tops_up_from_site_wide_when_category_thin():
    """Regression for a real published-post bug: a thin/new category
    returned 0 same-category posts, so the caller fell all the way to the
    "블로그 홈"-only placeholder and the SEO checker flagged "내부 링크가 3개
    미만입니다". A same-category pool under ``min_results`` must now top up
    with an unscoped site-wide fetch instead of returning early."""
    same_category = []  # thin category: nothing published there yet
    site_wide = [
        {"id": 1, "title": {"rendered": "요가 초보 확인 기준"}, "link": "https://x.com/a"},
        {"id": 2, "title": {"rendered": "스트레칭 루틴 만들기"}, "link": "https://x.com/b"},
        {"id": 3, "title": {"rendered": "수면 자세 체크 포인트"}, "link": "https://x.com/c"},
    ]

    def _fake_get(url, headers=None, timeout=None):
        body = same_category if "categories=42" in url else site_wide

        class _FakeResponse:
            status_code = 200
            content = b"x"

            def json(self):
                return body

        return _FakeResponse()

    with patch("agent.content.seo_enrich.httpx.get", side_effect=_fake_get):
        results = fetch_wordpress_internal_candidates(
            site_url="https://x.com", auth_header="Basic xyz", categories=42,
        )
    assert len(results) == 3
    assert {r["link"] for r in results} == {"https://x.com/a", "https://x.com/b", "https://x.com/c"}


def test_fetch_wordpress_internal_candidates_no_top_up_when_category_has_enough():
    """The site-wide fetch must not fire at all once the categorized pool
    already meets ``min_results`` -- otherwise every call doubles its
    request volume for no reason."""
    call_count = {"n": 0}
    same_category = [
        {"id": i, "title": {"rendered": f"글 {i}"}, "link": f"https://x.com/{i}"}
        for i in range(1, 4)
    ]

    def _fake_get(url, headers=None, timeout=None):
        call_count["n"] += 1

        class _FakeResponse:
            status_code = 200
            content = b"x"

            def json(self):
                return same_category

        return _FakeResponse()

    with patch("agent.content.seo_enrich.httpx.get", side_effect=_fake_get):
        results = fetch_wordpress_internal_candidates(
            site_url="https://x.com", auth_header="Basic xyz", categories=42,
        )
    assert len(results) == 3
    assert call_count["n"] == 1


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


def test_enrich_wordpress_markdown_flags_fallback_only_internal_links():
    """Regression: a fallback-only publish ("블로그 홈" placeholder, 0 real
    candidates) must be surfaced via internalLinksFallbackOnly so it can be
    logged/reviewed, not silently indistinguishable from a healthy publish
    that legitimately added 0 links for some other reason."""
    with patch(
        "agent.content.seo_enrich.fetch_wordpress_internal_candidates",
        return_value=[],
    ):
        result = enrich_wordpress_markdown_for_seo(
            "## 마무리\n끝.",
            focus_keyword="요가",
            category_id="health",
            site_url="https://x.com",
            auth_header="Basic xyz",
            live=True,
            wp_category_term_id=7,
        )
    assert result["internalLinksFallbackOnly"] is True
    assert "블로그 홈" in result["markdown"]


def test_enrich_wordpress_markdown_no_fallback_flag_with_real_candidates():
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
        )
    assert result["internalLinksFallbackOnly"] is False


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


def test_full_topic_title_is_split_into_terms_for_matching():
    candidates = [
        {"title": "다크초콜릿 고르는 법", "link": "https://x.com/unrelated"},
        {"title": "연차계산기 사용법", "link": "https://x.com/related"},
    ]

    out = append_internal_links(
        "## 마무리\n끝.",
        site_url="https://x.com",
        candidates=candidates,
        max_links=1,
        preferred_terms=["연차계산기 | 입사일로 연차휴가를 계산하는 방법과 확인할 점"],
    )

    assert "[연차계산기 사용법](https://x.com/related)" in out
    assert "다크초콜릿 고르는 법" not in out


def test_two_character_overlap_can_rank_a_candidate():
    candidates = [
        {"title": "다크초콜릿 고르는 법", "link": "https://x.com/unrelated"},
        {"title": "컴퓨터 어깨 통증 완화", "link": "https://x.com/related"},
    ]

    out = append_internal_links(
        "## 마무리\n끝.",
        site_url="https://x.com",
        candidates=candidates,
        max_links=1,
        preferred_terms=["어깨결림"],
    )

    assert "[컴퓨터 어깨 통증 완화](https://x.com/related)" in out
    assert "다크초콜릿 고르는 법" not in out


def test_unrelated_candidates_are_excluded_when_any_related_candidate_exists():
    candidates = [
        {"title": "다크초콜릿 고르는 법", "link": "https://x.com/chocolate"},
        {"title": "셀룰라이트 관리 방법", "link": "https://x.com/cellulite"},
        {"title": "컴퓨터 어깨 통증 완화", "link": "https://x.com/shoulder"},
    ]

    out = append_internal_links(
        "## 마무리\n끝.",
        site_url="https://x.com",
        candidates=candidates,
        preferred_terms=["어깨결림"],
    )

    assert out.count("](https://x.com/") == 1
    assert "[컴퓨터 어깨 통증 완화](https://x.com/shoulder)" in out
    assert "다크초콜릿 고르는 법" not in out
    assert "셀룰라이트 관리 방법" not in out
