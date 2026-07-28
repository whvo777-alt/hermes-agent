"""Tests for agent.content.publishers.wordpress._parse_json_response.

Regression coverage for a real published-post failure: a WordPress REST
call occasionally comes back with a non-empty, non-JSON body (a
security-plugin challenge page, a reverse-proxy timeout page, etc.) instead
of the expected JSON payload. Every call site used to do
``response.json() if response.content else {}``, which only guards EMPTY
bodies -- a non-empty non-JSON body still hit json.JSONDecodeError directly,
surfacing as the opaque "Expecting value: line 1 column 1 (char 0)" with no
indication of which call or why. _parse_json_response() now catches that and
re-raises a WordPressPublisherError carrying the HTTP status, content-type,
and a body snippet.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from agent.content.publishers.wordpress import (
    WordPressPublisherError,
    _parse_json_response,
    create_wordpress_draft,
    ensure_wordpress_tags,
    resolve_or_create_wordpress_category,
)


def _response(status_code: int, content: bytes, content_type: str = "application/json") -> httpx.Response:
    return httpx.Response(
        status_code=status_code, content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://example.test/"),
    )


def test_empty_body_returns_default():
    resp = _response(200, b"")
    assert _parse_json_response(resp, context="test") == {}
    assert _parse_json_response(resp, context="test", empty_default=[]) == []


def test_valid_json_parses_normally():
    resp = _response(200, b'{"id": 42, "status": "draft"}')
    assert _parse_json_response(resp, context="test") == {"id": 42, "status": "draft"}


def test_non_json_html_body_raises_clear_error_not_raw_json_error():
    """The literal reported bug: a non-empty HTML challenge/error page must
    raise WordPressPublisherError with diagnostic detail, not a bare
    json.JSONDecodeError with no context."""
    html = b"<html><body>403 Forbidden - security check</body></html>"
    resp = _response(403, html, content_type="text/html; charset=UTF-8")
    with pytest.raises(WordPressPublisherError) as exc_info:
        _parse_json_response(resp, context="WordPress draft create")
    message = str(exc_info.value)
    assert "WordPress draft create" in message
    assert "403" in message
    assert "text/html" in message
    assert "Forbidden" in message


def test_non_json_error_chains_the_original_json_decode_error():
    resp = _response(200, b"not json at all")
    with pytest.raises(WordPressPublisherError) as exc_info:
        _parse_json_response(resp, context="test")
    assert isinstance(exc_info.value.__cause__, ValueError)


def _auth_kwargs():
    return dict(site_url="https://example.test", username="u", app_password="p")


def test_create_wordpress_draft_raises_clear_error_on_html_response():
    html_response = _response(200, b"<html>upstream timeout</html>", content_type="text/html")
    with patch("agent.content.publishers.wordpress.httpx.post", return_value=html_response):
        with pytest.raises(WordPressPublisherError) as exc_info:
            create_wordpress_draft(
                **_auth_kwargs(),
                payload={"status": "draft", "title": "t", "content": "c"},
                live=True,
            )
    assert "WordPress draft create" in str(exc_info.value)


def test_ensure_wordpress_tags_raises_clear_error_on_html_search_response():
    html_response = _response(200, b"<html>challenge</html>", content_type="text/html")
    with patch("agent.content.publishers.wordpress.httpx.get", return_value=html_response):
        with pytest.raises(WordPressPublisherError) as exc_info:
            ensure_wordpress_tags(**_auth_kwargs(), tag_names=["a-tag"], live=True)
    assert "WordPress tag search" in str(exc_info.value)


def test_resolve_or_create_wordpress_category_raises_clear_error_on_html_response():
    html_response = _response(200, b"<html>challenge</html>", content_type="text/html")
    with patch("agent.content.publishers.wordpress.httpx.get", return_value=html_response):
        with pytest.raises(WordPressPublisherError) as exc_info:
            resolve_or_create_wordpress_category(
                **_auth_kwargs(), slug="health", name="건강", live=True,
            )
    assert "WordPress category search" in str(exc_info.value)
