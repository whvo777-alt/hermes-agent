"""Publish-on-approval — the ONLY place a platform publisher may be called.

Hard rule: this function refuses to call any publisher unless the matching
``DailyBlogApprovalItem.session.status`` is APPROVED. There is no other
entry point in agent/content/ that calls a publisher.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from agent.coo.approval_session import CEOApprovalSessionStatus
from agent.coo.daily_blog_bundle import DailyBlogApprovalBundle, find_item
from agent.content.markdown_html import extract_title, markdown_to_html
from agent.content.publishers.blogspot import create_blogspot_draft
from agent.content.publishers.naver import create_naver_draft
from agent.content.publishers.tistory import create_tistory_draft
from agent.content.publishers.wordpress import create_wordpress_draft, is_live_wordpress_draft_enabled


class PublishBlockedError(RuntimeError):
    """Raised when a publish is attempted before CEO approval."""


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9가-힣]+", "-", title.lower()).strip("-")
    return slug or "post"


def publish_approved_item(bundle: DailyBlogApprovalBundle, platform_id: str, *, live: bool = False) -> Dict[str, Any]:
    """Call the matching platform publisher — ONLY if approved.

    ``live=False`` (default) always produces a dry-run/preview result with
    zero network calls, regardless of approval status, matching "승인 없는
    API 호출 금지" / "실제 발행 금지" defaults. ``live=True`` additionally
    requires the item to be APPROVED, or this raises ``PublishBlockedError``
    without calling anything.
    """
    item = find_item(bundle, platform_id)
    if item is None:
        raise ValueError(f"No item for platform: {platform_id}")

    if live and item.session.status is not CEOApprovalSessionStatus.APPROVED:
        raise PublishBlockedError(
            f"{platform_id} item is not approved (status={item.session.status.value}) — publisher not called."
        )

    blog_content = Path(item.blog_file).read_text(encoding="utf-8")

    if platform_id == "wordpress":
        title = extract_title(blog_content, item.topic_title)
        payload = {
            "status": "draft",
            "title": title,
            "slug": _slugify(title),
            "content": markdown_to_html(blog_content),
        }
        wp_live = live and is_live_wordpress_draft_enabled(os.environ)
        return create_wordpress_draft(
            site_url=os.environ.get("WORDPRESS_SITE_URL"),
            username=os.environ.get("WORDPRESS_USERNAME"),
            app_password=os.environ.get("WORDPRESS_APP_PASSWORD"),
            payload=payload,
            live=wp_live,
        )

    if platform_id == "blogspot":
        return create_blogspot_draft(
            markdown=blog_content,
            blog_id=os.environ.get("BLOGGER_BLOG_ID"),
            client_id=os.environ.get("BLOGGER_CLIENT_ID"),
            client_secret=os.environ.get("BLOGGER_CLIENT_SECRET"),
            refresh_token=os.environ.get("BLOGGER_REFRESH_TOKEN"),
            live=live,
        )

    if platform_id == "naver":
        return create_naver_draft(markdown=blog_content, live=live)

    if platform_id == "tistory":
        return create_tistory_draft(markdown=blog_content, live=live)

    raise ValueError(f"Unknown platform: {platform_id}")
