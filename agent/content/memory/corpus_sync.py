"""Merge local drafts + live WordPress titles into content memory.

Absolute no-repeat rule: any already-written topic/keyword on a platform
must be visible to topic picking before generation starts.
"""

from __future__ import annotations

import os
import re
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.content.memory.content_memory import (
    _normalize_text,
    add_content,
    load_memory,
    save_memory,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _drafts_root() -> Path:
    override = os.environ.get("HERMES_CONTENT_DRAFTS_DIR")
    return Path(override) if override else _REPO_ROOT / "data" / "content_drafts"


def _guess_main_keyword(title: str, category_keywords: Optional[List[str]] = None) -> str:
    text = _normalize_text(title)
    for kw in category_keywords or []:
        if _normalize_text(kw) and _normalize_text(kw) in text:
            return str(kw).strip()
    tokens = [t for t in text.split() if len(t) >= 2]
    return tokens[0] if tokens else ""


def _extract_h1(markdown: str) -> str:
    match = re.search(r"^#\s+(.+)$", markdown or "", flags=re.M)
    if match:
        return re.sub(r"[*_`]", "", match.group(1)).strip()
    fm = re.search(r"(?m)^topic_title:\s*(.+)$", markdown or "")
    return (fm.group(1).strip() if fm else "").strip()


def ingest_local_drafts(
    memory: Dict[str, Any],
    *,
    platform_id: Optional[str] = None,
    category_keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Scan content_drafts/*/…/blog.md and record titles into memory."""
    from agent.content.config.launch_policy import get_launch_category

    root = _drafts_root()
    if not root.is_dir():
        return memory
    current = memory
    for date_dir in sorted(root.iterdir()):
        if not date_dir.is_dir():
            continue
        run_date = date_dir.name
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", run_date):
            continue
        for platform_dir in date_dir.iterdir():
            if not platform_dir.is_dir():
                continue
            pid = platform_dir.name
            if platform_id and pid != platform_id:
                continue
            blog = platform_dir / "blog.md"
            if not blog.is_file():
                continue
            try:
                text = blog.read_text(encoding="utf-8")
            except OSError:
                continue
            title = _extract_h1(text) or blog.stem
            if not title:
                continue
            fm_topic = re.search(r"(?m)^topic_title:\s*(.+)$", text)
            topic = fm_topic.group(1).strip() if fm_topic else title
            try:
                cat = get_launch_category(pid)
                cat_id = cat.id
                kws = list(cat.keywords) if category_keywords is None else category_keywords
            except Exception:  # noqa: BLE001
                cat_id = ""
                kws = category_keywords or []
            keyword = _guess_main_keyword(topic + " " + title, kws)
            added = add_content(
                current,
                {
                    "date": run_date,
                    "platform": pid,
                    "category": cat_id,
                    "topic": topic,
                    "title": title,
                    "mainKeyword": keyword,
                    "subKeywords": [],
                    "slug": f"draft-{run_date}-{pid}",
                    "filePath": str(blog),
                },
            )
            current = added["memory"]
    return current


def ingest_wordpress_published(
    memory: Dict[str, Any],
    *,
    category_id: str = "health",
    category_keywords: Optional[List[str]] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Pull published + draft WP titles so live duplicates are blocked."""
    site_url = os.environ.get("WORDPRESS_SITE_URL") or ""
    username = os.environ.get("WORDPRESS_USERNAME")
    password = os.environ.get("WORDPRESS_APP_PASSWORD")
    if not (site_url and username and password):
        return memory
    try:
        from agent.content.publishers.wordpress import _build_auth_header
        import httpx
    except Exception:  # noqa: BLE001
        return memory

    try:
        auth = _build_auth_header(username=username, app_password=password)
    except Exception:  # noqa: BLE001
        return memory

    current = memory
    for status in ("publish", "draft"):
        endpoint = (
            f"{site_url.rstrip('/')}/wp-json/wp/v2/posts"
            f"?per_page={limit}&status={status}&_fields=id,title,link,date,slug"
        )
        try:
            response = httpx.get(endpoint, headers={"Authorization": auth}, timeout=20.0)
            if response.status_code >= 400:
                continue
            body = response.json() if response.content else []
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(body, list):
            continue
        for item in body:
            title = ((item.get("title") or {}).get("rendered") or "").strip()
            title = re.sub(r"<[^>]+>", "", title)
            if not title:
                continue
            raw_date = str(item.get("date") or "")[:10] or date_cls.today().isoformat()
            keyword = _guess_main_keyword(title, category_keywords)
            slug = str(item.get("slug") or item.get("id") or title)
            added = add_content(
                current,
                {
                    "date": raw_date,
                    "platform": "wordpress",
                    "category": category_id,
                    "topic": title,
                    "title": title,
                    "mainKeyword": keyword,
                    "subKeywords": [],
                    "slug": f"wp-{slug}",
                    "filePath": str(item.get("link") or ""),
                },
            )
            current = added["memory"]
    return current


def sync_written_corpus(
    *,
    platform_id: str,
    category_id: str,
    category_keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Load memory, merge drafts (+ WP for wordpress), persist, return memory."""
    memory = load_memory()
    memory = ingest_local_drafts(
        memory, platform_id=platform_id, category_keywords=category_keywords
    )
    if platform_id == "wordpress":
        memory = ingest_wordpress_published(
            memory, category_id=category_id, category_keywords=category_keywords
        )
    save_memory(memory)
    return memory
