"""WordPress REST draft publisher — ported from
multi-content-pipeline/publishers/wordpress-rest-client.js.

Hard rule preserved from the source: this client can only ever create a
DRAFT (``assertDraftStatus``) — it has no code path to publish. Real HTTP
calls only fire when ``live=True`` is passed explicitly by the caller
(post-approval); with ``live=False`` (default) it returns a dry-run preview
with zero network calls.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

_TIMEOUT = 30.0


class WordPressPublisherError(RuntimeError):
    pass


def _normalize_site_url(site_url: str) -> str:
    return str(site_url or "").rstrip("/")


def _assert_draft_status(status: str) -> None:
    if status != "draft":
        raise WordPressPublisherError("WordPress REST draft client only allows status=draft")


def _require_site_url(site_url: Optional[str]) -> str:
    if not site_url:
        raise WordPressPublisherError("WORDPRESS_SITE_URL is required for live draft mode")
    return _normalize_site_url(site_url)


def _build_auth_header(*, username: Optional[str], app_password: Optional[str]) -> str:
    if not username or not app_password:
        raise WordPressPublisherError("WORDPRESS_USERNAME and WORDPRESS_APP_PASSWORD are required for live draft mode")
    token = base64.b64encode(f"{username}:{app_password}".encode()).decode()
    return f"Basic {token}"


def is_live_wordpress_draft_enabled(env: Dict[str, str]) -> bool:
    return env.get("LIVE_WORDPRESS_DRAFT") == "true"


def check_slug_availability(*, site_url: str, slug: str, auth_header: str, live: bool) -> Dict[str, Any]:
    if not live:
        return {"available": True, "checked": False, "slug": slug}

    endpoint = f"{_require_site_url(site_url)}/wp-json/wp/v2/posts?slug={slug}"
    response = httpx.get(endpoint, headers={"Authorization": auth_header}, timeout=_TIMEOUT)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else []
    if response.status_code >= 400:
        raise WordPressPublisherError(f"WordPress slug check failed: HTTP {response.status_code}")
    return {"available": not isinstance(body, list) or len(body) == 0, "checked": True, "slug": slug}


def ensure_unique_slug(*, site_url: str, slug: str, auth_header: str, live: bool, max_attempts: int = 10) -> Dict[str, Any]:
    for index in range(max_attempts):
        candidate = slug if index == 0 else f"{slug}-{index + 1}"
        check = check_slug_availability(site_url=site_url, slug=candidate, auth_header=auth_header, live=live)
        if check["available"]:
            return {"slug": candidate, "checked": check["checked"], "changed": candidate != slug}
    raise WordPressPublisherError(f"Unable to find unique WordPress slug after {max_attempts} attempts")


def create_wordpress_draft(*, site_url: Optional[str], username: Optional[str], app_password: Optional[str],
                            payload: Dict[str, Any], live: bool) -> Dict[str, Any]:
    _assert_draft_status(payload.get("status"))

    if not live:
        return {
            "apiCalled": False,
            "dryRun": True,
            "request": {
                "endpoint": f"{_normalize_site_url(site_url or 'https://example.test')}/wp-json/wp/v2/posts",
                "method": "POST",
                "body": payload,
            },
        }

    auth_header = _build_auth_header(username=username, app_password=app_password)
    endpoint = f"{_require_site_url(site_url)}/wp-json/wp/v2/posts"
    response = httpx.post(
        endpoint,
        headers={"Authorization": auth_header, "Content-Type": "application/json"},
        json=payload,
        timeout=_TIMEOUT,
    )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        raise WordPressPublisherError(f"WordPress draft create failed: HTTP {response.status_code}")

    return {
        "apiCalled": True,
        "dryRun": False,
        "response": {
            "id": body.get("id"),
            "status": body.get("status"),
            "slug": body.get("slug"),
            "link": body.get("link"),
            "editLink": (body.get("_links", {}).get("wp:action-edit") or [{}])[0].get("href"),
        },
    }
