"""Blogspot (Blogger API) draft publisher — ported from
multi-content-pipeline/scripts/blogspot-draft-test.js.

Real OAuth refresh-token exchange + Blogger API draft creation, gated by an
explicit ``live`` flag from the caller (post-approval only). With
``live=False`` (default) returns a dry-run payload preview with zero network
calls — never posts, and never anything but ``isDraft=true``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional

import httpx

from agent.content.markdown_html import extract_labels, markdown_to_html

_TIMEOUT = 30.0


class BlogspotPublisherError(RuntimeError):
    pass


def is_live_blogspot_draft_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when Blogger OAuth credentials are present for draft upload.

    Opt-out with LIVE_BLOGSPOT_DRAFT=0/false/off. Default is on when all
    BLOGGER_* credentials exist (approval should create a real draft).
    """
    source = env if env is not None else os.environ
    flag = str(source.get("LIVE_BLOGSPOT_DRAFT", "1")).strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    return bool(
        str(source.get("BLOGGER_BLOG_ID") or "").strip()
        and str(source.get("BLOGGER_CLIENT_ID") or "").strip()
        and str(source.get("BLOGGER_CLIENT_SECRET") or "").strip()
        and str(source.get("BLOGGER_REFRESH_TOKEN") or "").strip()
    )


def exchange_blogger_access_token(*, client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange the long-lived Blogger OAuth refresh token for a short-lived
    access token. Public (promoted from the former ``_exchange_refresh_token``)
    so ``publish_on_approval.py`` can call it once to fetch same-category
    internal-link candidates via ``fetch_blogspot_internal_candidates`` before
    ``create_blogspot_draft`` does its own independent exchange."""
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
        detail = data.get("error_description") or data.get("error") or response.text[:200]
        raise BlogspotPublisherError(
            f"OAuth token refresh failed: HTTP {response.status_code} ({detail})"
        )
    return data["access_token"]


def build_blogger_post(markdown: str, *, title: str, labels: Optional[List[str]] = None,
                        extra_content_html: str = "") -> Dict[str, Any]:
    """``labels`` should be extracted from the RAW pre-strip blog content by
    the caller and passed in explicitly — by the time ``markdown`` reaches
    here it's already had frontmatter/SEO-meta stripped (same reason
    ``title`` is passed in rather than re-extracted, see
    ``publish_on_approval.py``'s blogspot branch). Falls back to
    ``extract_labels(markdown)`` when the caller doesn't pass any, so this
    stays backward compatible for any other caller.

    ``extra_content_html`` (e.g. JSON-LD ``<script>`` tags — see
    ``agent/content/structured_data.py``) is appended AFTER
    ``markdown_to_html()`` runs, never mixed into the markdown itself: any
    line that doesn't match a structural markdown pattern gets HTML-escaped
    by the converter's fallback paragraph handler, which would mangle raw
    ``<script>`` tags if they were embedded in the markdown source instead.
    """
    content_html = markdown_to_html(markdown)
    if extra_content_html:
        content_html = f"{content_html}\n{extra_content_html}"
    return {
        "kind": "blogger#post",
        "title": title,
        "content": content_html,
        "labels": labels if labels is not None else extract_labels(markdown),
    }


def create_blogspot_draft(*, markdown: str, title: str, blog_id: Optional[str], client_id: Optional[str],
                           client_secret: Optional[str], refresh_token: Optional[str], live: bool,
                           labels: Optional[List[str]] = None, extra_content_html: str = "") -> Dict[str, Any]:
    post = build_blogger_post(markdown, title=title, labels=labels, extra_content_html=extra_content_html)

    if not live:
        return {"apiCalled": False, "dryRun": True, "postPreview": post}

    if not blog_id:
        raise BlogspotPublisherError("BLOGGER_BLOG_ID missing")
    if not client_id or not client_secret or not refresh_token:
        raise BlogspotPublisherError("Blogger OAuth client_id/client_secret/refresh_token missing")

    access_token = exchange_blogger_access_token(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
    )
    url = f"https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts?isDraft=true"
    response = httpx.post(
        url,
        headers={"content-type": "application/json", "authorization": f"Bearer {access_token}"},
        json=post,
        timeout=_TIMEOUT,
    )
    data = response.json() if response.content else {}
    if response.status_code >= 400:
        detail = (
            data.get("error", {}).get("message")
            if isinstance(data.get("error"), dict)
            else data.get("error")
        )
        raise BlogspotPublisherError(
            f"Blogger posts.insert draft failed: HTTP {response.status_code} "
            f"({detail or response.text[:200]})"
        )

    return {
        "apiCalled": True,
        "dryRun": False,
        "postId": data.get("id"),
        "url": data.get("url"),
        "selfLink": data.get("selfLink"),
        "status": "draft",
    }


def update_blogspot_draft(
    *,
    markdown: str,
    title: str,
    blog_id: Optional[str],
    post_id: str,
    client_id: Optional[str],
    client_secret: Optional[str],
    refresh_token: Optional[str],
    live: bool,
    labels: Optional[List[str]] = None,
    extra_content_html: str = "",
) -> Dict[str, Any]:
    """Overwrite an existing Blogger draft body (keeps isDraft).

    ``title`` was previously not accepted here at all, so the internal
    ``build_blogger_post(markdown)`` call was missing its required
    keyword-only ``title`` argument and would raise ``TypeError`` on any
    real call. Unused by any current call site (caught while fixing the
    labels bug alongside it), so this was never actually exercised.
    """
    post = build_blogger_post(markdown, title=title, labels=labels, extra_content_html=extra_content_html)
    if not live:
        return {"apiCalled": False, "dryRun": True, "postPreview": post, "postId": post_id}

    if not blog_id:
        raise BlogspotPublisherError("BLOGGER_BLOG_ID missing")
    if not client_id or not client_secret or not refresh_token:
        raise BlogspotPublisherError("Blogger OAuth client_id/client_secret/refresh_token missing")
    if not post_id:
        raise BlogspotPublisherError("post_id missing")

    access_token = exchange_blogger_access_token(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
    )
    url = f"https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts/{post_id}"
    response = httpx.put(
        url,
        headers={"content-type": "application/json", "authorization": f"Bearer {access_token}"},
        json=post,
        timeout=_TIMEOUT,
    )
    data = response.json() if response.content else {}
    if response.status_code >= 400:
        detail = (
            data.get("error", {}).get("message")
            if isinstance(data.get("error"), dict)
            else data.get("error")
        )
        raise BlogspotPublisherError(
            f"Blogger posts.update draft failed: HTTP {response.status_code} "
            f"({detail or response.text[:200]})"
        )

    return {
        "apiCalled": True,
        "dryRun": False,
        "postId": data.get("id") or post_id,
        "url": data.get("url"),
        "selfLink": data.get("selfLink"),
        "status": "draft",
        "updated": True,
    }
