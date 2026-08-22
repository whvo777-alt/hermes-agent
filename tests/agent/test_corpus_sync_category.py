"""Regression tests for WordPress category preservation during corpus sync."""

from __future__ import annotations

import httpx

from agent.content.memory import corpus_sync
from agent.content.publishers import wordpress


class _FakeResponse:
    def __init__(self, body, *, status_code: int = 200):
        self.content = b"response"
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


_MISSING = object()


def _post(slug: str, *, categories=_MISSING):
    post = {
        "id": len(slug),
        "title": {"rendered": slug.replace("-", " ")},
        "link": f"https://example.test/{slug}",
        "date": "2026-08-22T00:00:00",
        "slug": slug,
    }
    if categories is not _MISSING:
        post["categories"] = categories
    return post


def _run_ingest(monkeypatch, posts, *, category_rows=None, category_error=None):
    calls = []

    monkeypatch.setenv("WORDPRESS_SITE_URL", "https://example.test")
    monkeypatch.setenv("WORDPRESS_USERNAME", "user")
    monkeypatch.setenv("WORDPRESS_APP_PASSWORD", "password")
    monkeypatch.setattr(
        wordpress,
        "_build_auth_header",
        lambda **_kwargs: "Basic test-auth",
    )

    def fake_get(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        if "/wp/v2/posts" in endpoint:
            if "status=publish" in endpoint:
                return _FakeResponse(posts)
            return _FakeResponse([])
        if "/wp/v2/categories" in endpoint:
            if category_error:
                raise category_error
            return _FakeResponse(category_rows or [])
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(httpx, "get", fake_get)
    memory = corpus_sync.ingest_wordpress_published(
        {"items": []},
        category_id="health",
    )
    return memory, calls


def _items(memory):
    return memory["items"]


def test_resolved_wordpress_category_slug_is_saved(monkeypatch):
    memory, _calls = _run_ingest(
        monkeypatch,
        [_post("notion", categories=[7])],
        category_rows=[{"id": 7, "slug": "it-tech"}],
    )

    assert _items(memory)[0]["category"] == "it-tech"


def test_fallback_category_wins_when_current_category_is_also_attached(monkeypatch):
    memory, _calls = _run_ingest(
        monkeypatch,
        [_post("health-and-it", categories=[7, 12])],
        category_rows=[
            {"id": 7, "slug": "it-tech"},
            {"id": 12, "slug": "health"},
        ],
    )

    assert _items(memory)[0]["category"] == "health"


def test_category_lookup_failure_keeps_fallback_category(monkeypatch):
    memory, _calls = _run_ingest(
        monkeypatch,
        [_post("legacy-post", categories=[7])],
        category_error=RuntimeError("category lookup failed"),
    )

    assert _items(memory)[0]["category"] == "health"


def test_missing_post_categories_keeps_fallback_category(monkeypatch):
    memory, calls = _run_ingest(monkeypatch, [_post("uncategorized-post")])

    assert _items(memory)[0]["category"] == "health"
    assert not any("/wp/v2/categories" in endpoint for endpoint, _kwargs in calls)


def test_category_lookup_is_called_once_for_all_posts(monkeypatch):
    memory, calls = _run_ingest(
        monkeypatch,
        [
            _post("notion", categories=[7]),
            _post("slack", categories=[7, 12]),
        ],
        category_rows=[
            {"id": 7, "slug": "it-tech"},
            {"id": 12, "slug": "health"},
        ],
    )

    category_calls = [endpoint for endpoint, _kwargs in calls if "/wp/v2/categories" in endpoint]
    assert len(_items(memory)) == 2
    assert len(category_calls) == 1
    assert "include=7,12" in category_calls[0]


def test_url_encoded_korean_category_slug_maps_to_health(monkeypatch):
    memory, _calls = _run_ingest(
        monkeypatch,
        [_post("health-post", categories=[7])],
        category_rows=[{"id": 7, "slug": "%ea%b1%b4%ea%b0%95"}],
    )

    assert _items(memory)[0]["category"] == "health"


def test_it_category_slug_maps_to_it_tech(monkeypatch):
    memory, _calls = _run_ingest(
        monkeypatch,
        [_post("intel", categories=[7])],
        category_rows=[{"id": 7, "slug": "it"}],
    )

    assert _items(memory)[0]["category"] == "it-tech"


def test_slack_category_slug_maps_to_it_tech(monkeypatch):
    memory, _calls = _run_ingest(
        monkeypatch,
        [_post("slack", categories=[7])],
        category_rows=[{"id": 7, "slug": "slack"}],
    )

    assert _items(memory)[0]["category"] == "it-tech"


def test_uncategorized_category_slug_uses_fallback(monkeypatch):
    memory, _calls = _run_ingest(
        monkeypatch,
        [_post("walking", categories=[7])],
        category_rows=[{"id": 7, "slug": "uncategorized"}],
    )

    assert _items(memory)[0]["category"] == "health"


def test_it_tech_category_slug_is_unchanged(monkeypatch):
    memory, _calls = _run_ingest(
        monkeypatch,
        [_post("it-tech-post", categories=[7])],
        category_rows=[{"id": 7, "slug": "it-tech"}],
    )

    assert _items(memory)[0]["category"] == "it-tech"


def test_unlisted_category_slug_is_unchanged(monkeypatch):
    memory, _calls = _run_ingest(
        monkeypatch,
        [_post("travel", categories=[7])],
        category_rows=[{"id": 7, "slug": "travel"}],
    )

    assert _items(memory)[0]["category"] == "travel"
