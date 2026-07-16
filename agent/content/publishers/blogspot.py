"""Blogspot (Blogger API) draft publisher — ported from
multi-content-pipeline/scripts/blogspot-draft-test.js.

Real OAuth refresh-token exchange + Blogger API draft creation, gated by an
explicit ``live`` flag from the caller (post-approval only). With
``live=False`` (default) returns a dry-run payload preview with zero network
calls — never posts, and never anything but ``isDraft=true``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from agent.content.markdown_html import extract_labels, extract_title, markdown_to_html

_TIMEOUT = 30.0


class BlogspotPublisherError(RuntimeError):
    pass


def _exchange_refresh_token(*, client_id: str, client_secret: str, refresh_token: str) -> str:
    response = httpx.post(
        "https://oauth2.googleapis.com/token",
        headers={"content-type": "application/x-www-form-urlencoded"},
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=_TIMEOUT,
    )
    data = response.json() if response.content else {}
    if response.status_code >= 400 or not data.get("access_token"):
        raise BlogspotPublisherError(f"OAuth token refresh failed: HTTP {response.status_code}")
    return data["access_token"]


def build_blogger_post(markdown: str) -> Dict[str, Any]:
    return {
        "kind": "blogger#post",
        "title": extract_title(markdown, "Blogspot 초안"),
        "content": markdown_to_html(markdown),
        "labels": extract_labels(markdown),
    }


def create_blogspot_draft(*, markdown: str, blog_id: Optional[str], client_id: Optional[str],
                           client_secret: Optional[str], refresh_token: Optional[str], live: bool) -> Dict[str, Any]:
    post = build_blogger_post(markdown)

    if not live:
        return {"apiCalled": False, "dryRun": True, "postPreview": post}

    if not blog_id:
        raise BlogspotPublisherError("BLOGGER_BLOG_ID missing")
    if not client_id or not client_secret or not refresh_token:
        raise BlogspotPublisherError("Blogger OAuth client_id/client_secret/refresh_token missing")

    access_token = _exchange_refresh_token(client_id=client_id, client_secret=client_secret, refresh_token=refresh_token)
    url = f"https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts?isDraft=true"
    response = httpx.post(
        url,
        headers={"content-type": "application/json", "authorization": f"Bearer {access_token}"},
        json=post,
        timeout=_TIMEOUT,
    )
    data = response.json() if response.content else {}
    if response.status_code >= 400:
        raise BlogspotPublisherError(f"Blogger posts.insert draft failed: HTTP {response.status_code}")

    return {"apiCalled": True, "dryRun": False, "postId": data.get("id"), "url": data.get("url"), "selfLink": data.get("selfLink")}
