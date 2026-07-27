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
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

_LOG = logging.getLogger(__name__)
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


def upload_wordpress_media(*, site_url: Optional[str], username: Optional[str], app_password: Optional[str],
                            file_path: str, alt_text: str = "", live: bool = False) -> Dict[str, Any]:
    """Upload one local image to the WordPress media library (PNG/JPEG only)."""
    from pathlib import Path

    path = Path(file_path)
    content_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower())
    if content_type is None:
        raise WordPressPublisherError(f"Unsupported media type for WordPress upload: {path.suffix}")

    if not live:
        return {"apiCalled": False, "dryRun": True, "file": str(path)}

    auth_header = _build_auth_header(username=username, app_password=app_password)
    endpoint = f"{_require_site_url(site_url)}/wp-json/wp/v2/media"
    response = httpx.post(
        endpoint,
        headers={
            "Authorization": auth_header,
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{path.name}"',
        },
        content=path.read_bytes(),
        timeout=_TIMEOUT,
    )
    body = response.json() if response.content else {}
    if response.status_code >= 400 or not body.get("id"):
        raise WordPressPublisherError(f"WordPress media upload failed: HTTP {response.status_code}")
    media_id = body["id"]
    if alt_text:
        httpx.post(
            f"{_require_site_url(site_url)}/wp-json/wp/v2/media/{media_id}",
            headers={"Authorization": auth_header, "Content-Type": "application/json"},
            json={"alt_text": alt_text},
            timeout=_TIMEOUT,
        )
    return {
        "apiCalled": True,
        "dryRun": False,
        "id": media_id,
        "sourceUrl": body.get("source_url"),
    }


def create_wordpress_draft(*, site_url: Optional[str], username: Optional[str], app_password: Optional[str],
                            payload: Dict[str, Any], live: bool,
                            expected_featured_media: Optional[int] = None) -> Dict[str, Any]:
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
    post_id = body.get("id")
    if not post_id:
        raise WordPressPublisherError("WordPress draft create response missing post ID")

    verify_endpoint = (
        f"{_require_site_url(site_url)}/wp-json/wp/v2/posts/{post_id}?context=edit"
    )
    verify_response = httpx.get(
        verify_endpoint,
        headers={"Authorization": auth_header},
        timeout=_TIMEOUT,
    )
    verify_body = verify_response.json() if verify_response.content else {}
    if verify_response.status_code >= 400:
        raise WordPressPublisherError(
            f"WordPress draft verification failed: HTTP {verify_response.status_code}"
        )
    if verify_body.get("status") != "draft":
        raise WordPressPublisherError(
            "WordPress verification did not return status=draft"
        )

    if expected_featured_media is not None:
        actual_featured = verify_body.get("featured_media")
        if actual_featured != expected_featured_media:
            _LOG.warning(
                "WordPress verification featured_media mismatch: "
                f"expected={expected_featured_media}, actual={actual_featured}"
            )
            featured_media_mismatch = True
        else:
            featured_media_mismatch = False
    else:
        featured_media_mismatch = False

    verification: Dict[str, Any] = {
        "id": verify_body.get("id"),
        "status": verify_body.get("status"),
        "slug": verify_body.get("slug"),
        "link": verify_body.get("link"),
        "checked": True,
        "featured_media": verify_body.get("featured_media"),
    }
    if featured_media_mismatch:
        verification["featured_media_mismatch"] = True
    if expected_featured_media is None:
        verification["featured_media_expected"] = False

    return {
        "apiCalled": True,
        "dryRun": False,
        "response": {
            "id": post_id,
            "status": body.get("status"),
            "slug": body.get("slug"),
            "link": body.get("link"),
            "editLink": (body.get("_links", {}).get("wp:action-edit") or [{}])[0].get("href"),
            "adminLink": (
                f"{_require_site_url(site_url)}/wp-admin/post.php?"
                f"post={post_id}&action=edit"
            ),
        },
        "verification": verification,
    }


def update_rank_math_seo(
    *,
    site_url: Optional[str],
    username: Optional[str],
    app_password: Optional[str],
    post_id: int,
    focus_keyword: str,
    seo_title: str,
    seo_description: str,
    live: bool = False,
) -> Dict[str, Any]:
    """Write Rank Math focus keyword / title / description for a draft.

    WordPress ``excerpt`` alone does not feed Rank Math. Without these meta
    fields the editor shows N/A or a very low SEO score even when the body
    content is fine.
    """
    if not live:
        return {
            "apiCalled": False,
            "dryRun": True,
            "focusKeyword": focus_keyword,
            "seoTitle": seo_title,
            "seoDescription": seo_description,
        }
    if not post_id:
        raise WordPressPublisherError("post_id is required for Rank Math meta update")
    if not focus_keyword:
        return {"apiCalled": False, "skipped": True, "reason": "empty focus keyword"}

    auth_header = _build_auth_header(username=username, app_password=app_password)
    endpoint = f"{_require_site_url(site_url)}/wp-json/rankmath/v1/updateMeta"
    meta = {
        "rank_math_focus_keyword": focus_keyword,
        "rank_math_title": seo_title or "",
        "rank_math_description": seo_description or "",
        "rank_math_robots": ["index"],
    }
    response = httpx.post(
        endpoint,
        headers={"Authorization": auth_header, "Content-Type": "application/json"},
        json={"objectType": "post", "objectID": int(post_id), "meta": meta},
        timeout=_TIMEOUT,
    )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        raise WordPressPublisherError(
            f"Rank Math meta update failed: HTTP {response.status_code} {body}"
        )
    return {
        "apiCalled": True,
        "dryRun": False,
        "focusKeyword": focus_keyword,
        "seoTitle": seo_title,
        "seoDescription": seo_description,
        "response": body,
    }


def ensure_wordpress_tags(
    *,
    site_url: Optional[str],
    username: Optional[str],
    app_password: Optional[str],
    tag_names: list,
    live: bool = False,
    minimum: int = 5,
) -> Dict[str, Any]:
    """Resolve or create WordPress tags and return their IDs (at least ``minimum``)."""
    names: list[str] = []
    seen = set()
    for raw in tag_names or []:
        name = str(raw or "").strip().lstrip("#")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name[:60])

    if not live:
        return {
            "apiCalled": False,
            "dryRun": True,
            "names": names[: max(minimum, len(names))],
            "ids": [],
        }
    if len(names) < minimum:
        # Caller should pad; still proceed with what we have if live.
        pass
    if not names:
        return {"apiCalled": False, "skipped": True, "reason": "no tag names", "ids": []}

    auth_header = _build_auth_header(username=username, app_password=app_password)
    base = f"{_require_site_url(site_url)}/wp-json/wp/v2/tags"
    ids: list[int] = []
    resolved: list[Dict[str, Any]] = []

    for name in names:
        # Prefer an exact existing tag.
        search = httpx.get(
            base,
            headers={"Authorization": auth_header},
            params={"search": name, "per_page": 20},
            timeout=_TIMEOUT,
        )
        found_id = None
        if search.status_code < 400:
            for item in search.json() if search.content else []:
                if str(item.get("name") or "").strip().lower() == name.lower():
                    found_id = item.get("id")
                    break
                if found_id is None and name.lower() in str(item.get("name") or "").lower():
                    found_id = item.get("id")
        if found_id:
            ids.append(int(found_id))
            resolved.append({"id": int(found_id), "name": name, "created": False})
            continue

        create = httpx.post(
            base,
            headers={"Authorization": auth_header, "Content-Type": "application/json"},
            json={"name": name},
            timeout=_TIMEOUT,
        )
        body = create.json() if create.content else {}
        if create.status_code >= 400:
            # Tag may already exist under a different slug — try slug lookup.
            existing = body.get("data", {}).get("term_id") if isinstance(body, dict) else None
            if existing:
                ids.append(int(existing))
                resolved.append({"id": int(existing), "name": name, "created": False})
            continue
        tag_id = body.get("id")
        if tag_id:
            ids.append(int(tag_id))
            resolved.append({"id": int(tag_id), "name": name, "created": True})

    # Deduplicate while preserving order.
    uniq: list[int] = []
    for tag_id in ids:
        if tag_id not in uniq:
            uniq.append(tag_id)

    return {
        "apiCalled": True,
        "dryRun": False,
        "ids": uniq,
        "names": [item["name"] for item in resolved],
        "resolved": resolved,
        "count": len(uniq),
    }


def resolve_or_create_wordpress_category(*, site_url: Optional[str], username: Optional[str],
                                          app_password: Optional[str], slug: str, name: str,
                                          live: bool = False) -> Dict[str, Any]:
    """Resolve our internal ``category_id`` (already an ASCII slug, e.g.
    "health") to a WordPress category term ID via an exact ``slug=`` lookup,
    creating the WP category if it doesn't exist yet. Used both to tag new
    posts with a real WP category and to scope internal-link candidates to
    "same category" via ``fetch_wordpress_internal_candidates(categories=...)``.
    """
    if not live:
        return {"apiCalled": False, "dryRun": True, "id": None, "slug": slug}
    if not slug:
        return {"apiCalled": False, "skipped": True, "reason": "no slug", "id": None}

    auth_header = _build_auth_header(username=username, app_password=app_password)
    base = f"{_require_site_url(site_url)}/wp-json/wp/v2/categories"

    search = httpx.get(
        base,
        headers={"Authorization": auth_header},
        params={"slug": slug},
        timeout=_TIMEOUT,
    )
    if search.status_code < 400:
        for item in search.json() if search.content else []:
            if str(item.get("slug") or "").strip().lower() == slug.lower():
                return {"apiCalled": True, "id": int(item["id"]), "slug": slug, "created": False}

    create = httpx.post(
        base,
        headers={"Authorization": auth_header, "Content-Type": "application/json"},
        json={"name": name or slug, "slug": slug},
        timeout=_TIMEOUT,
    )
    body = create.json() if create.content else {}
    if create.status_code >= 400:
        # Category may already exist under this slug but the create call
        # raced with another process — try a term_id hint if present.
        existing = body.get("data", {}).get("term_id") if isinstance(body, dict) else None
        if existing:
            return {"apiCalled": True, "id": int(existing), "slug": slug, "created": False}
        raise WordPressPublisherError(
            f"WordPress category resolve/create failed: HTTP {create.status_code} ({body})"
        )
    category_id = body.get("id")
    if not category_id:
        raise WordPressPublisherError("WordPress category create returned no id")
    return {"apiCalled": True, "id": int(category_id), "slug": slug, "created": True}
