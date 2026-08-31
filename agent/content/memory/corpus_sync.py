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
from urllib.parse import unquote

from agent.content.memory.content_memory import (
    _normalize_text,
    add_content,
    load_memory,
    save_memory,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WP_CATEGORY_ALIASES = {
    "건강": "health",
    "it": "it-tech",
    "slack": "it-tech",
}
_WP_CATEGORY_IGNORED = {"uncategorized", ""}
_WP_PAGE_SIZE = 100
_WP_MAX_PAGES = 5


def _normalize_wp_slug(slug: str) -> str:
    """주소 방식 글자를 한글로 되돌리고 우리 이름으로 짝지어 준다."""
    raw = str(slug or "").strip()
    try:
        decoded = unquote(raw).strip().lower()
    except Exception:  # noqa: BLE001
        decoded = raw.lower()
    if decoded in _WP_CATEGORY_IGNORED:
        return ""
    return _WP_CATEGORY_ALIASES.get(decoded, decoded)

_HTML_ENTITY_RE = re.compile(r"&#?\w+;")
_JOSA = (
    "에서", "으로", "에게", "과의", "와의", "이나", "라도",
    "은", "는", "이", "가", "을", "를", "의", "에", "도", "와", "과", "로", "만",
)
_SKIP_KEYWORDS = frozenset(
    {
        "자고", "질문", "이런", "저런", "그런", "요즘", "정말", "진짜",
        "제발", "이제", "다시", "가장", "매우", "정도", "경우", "때문",
        "위해", "통해", "대해", "관련", "가지", "번째", "이상", "이하",
    }
)


def _strip_josa(token: str) -> str:
    """낱말 끝에 붙은 조사를 뗀다. 짧은 낱말은 건드리지 않는다."""
    for josa in sorted(_JOSA, key=len, reverse=True):
        if len(token) > len(josa) + 1 and token.endswith(josa):
            return token[: -len(josa)]
    return token


def _usable_token(token: str) -> bool:
    if len(token) < 2:
        return False
    if re.fullmatch(r"[0-9]+", token):
        return False
    if re.match(r"^[0-9]", token):
        return False
    if _strip_josa(token) in _SKIP_KEYWORDS:
        return False
    return True


def _lead_keyword(title: str) -> str:
    """제목이 '키워드, 설명' 꼴이면 맨 앞 낱말을 대표로 본다."""
    head = re.split(r"[,:|·]", str(title or ""), maxsplit=1)[0]
    head = _HTML_ENTITY_RE.sub(" ", head)
    tokens = [t for t in _normalize_text(head).split() if _usable_token(t)]
    if len(tokens) == 1:
        return _strip_josa(tokens[0])
    return ""


def _drafts_root() -> Path:
    override = os.environ.get("HERMES_CONTENT_DRAFTS_DIR")
    return Path(override) if override else _REPO_ROOT / "data" / "content_drafts"


def _guess_main_keyword(title: str, category_keywords: Optional[List[str]] = None) -> str:
    lead = _lead_keyword(title)
    if lead:
        return lead.lower()
    text = _normalize_text(_HTML_ENTITY_RE.sub(" ", str(title or "")))
    for kw in category_keywords or []:
        if _normalize_text(kw) and _normalize_text(kw) in text:
            return str(kw).strip()
    for token in text.split():
        if _usable_token(token):
            return _strip_josa(token).lower()
    return ""


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


def _fetch_category_slugs(site_url: str, term_ids: List[int]) -> Dict[int, str]:
    """카테고리 번호를 이름표로 바꾼다. 실패하면 빈 사전을 돌려준다."""
    ids = sorted({int(i) for i in term_ids if isinstance(i, int) or str(i).isdigit()})
    if not ids:
        return {}
    endpoint = (
        f"{site_url.rstrip('/')}/wp-json/wp/v2/categories"
        f"?include={','.join(str(i) for i in ids)}"
        f"&per_page=100&_fields=id,slug"
    )
    try:
        from agent.content.publishers.wordpress import _build_auth_header
        import httpx

        auth = _build_auth_header(
            username=os.environ.get("WORDPRESS_USERNAME"),
            app_password=os.environ.get("WORDPRESS_APP_PASSWORD"),
        )
        response = httpx.get(endpoint, headers={"Authorization": auth}, timeout=20.0)
        if response.status_code >= 400:
            return {}
        rows = response.json() if response.content else []
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[int, str] = {}
    for row in rows or []:
        try:
            out[int(row.get("id"))] = _normalize_wp_slug(row.get("slug"))
        except (TypeError, ValueError):
            continue
    return out


def _known_category_ids() -> set:
    """우리가 쓰는 카테고리 이름만 모은다. 못 얻으면 빈 집합."""
    try:
        from agent.content.config.categories import CATEGORIES

        return {category.id for category in CATEGORIES}
    except Exception:  # noqa: BLE001
        return set()


def _resolve_category(term_ids, slug_by_id: Dict[int, str], fallback: str) -> str:
    """글의 진짜 카테고리를 정한다. 못 알아내면 fallback 을 쓴다."""
    slugs = []
    for i in term_ids or []:
        try:
            slug = slug_by_id.get(int(i))
        except (TypeError, ValueError):
            continue
        if slug:
            slugs.append(slug)
    if fallback in slugs:
        return fallback
    known = _known_category_ids()
    for slug in slugs:
        if slug in known:
            return slug
    return fallback


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
    posts: List[Dict[str, Any]] = []
    for status in ("publish", "draft", "private"):
        for page in range(1, _WP_MAX_PAGES + 1):
            endpoint = (
                f"{site_url.rstrip('/')}/wp-json/wp/v2/posts"
                f"?per_page={_WP_PAGE_SIZE}&page={page}&status={status}"
                f"&_fields=id,title,link,date,slug,categories"
            )
            try:
                response = httpx.get(endpoint, headers={"Authorization": auth}, timeout=20.0)
                if response.status_code >= 400:
                    break
                body = response.json() if response.content else []
            except Exception:  # noqa: BLE001
                break
            if not isinstance(body, list) or not body:
                break
            posts.extend(body)
            if len(body) < _WP_PAGE_SIZE:
                break

    term_ids = [term_id for item in posts for term_id in (item.get("categories") or [])]
    slug_by_id = _fetch_category_slugs(site_url, term_ids)
    for item in posts:
        title = ((item.get("title") or {}).get("rendered") or "").strip()
        title = re.sub(r"<[^>]+>", "", title)
        if not title:
            continue
        raw_date = str(item.get("date") or "")[:10] or date_cls.today().isoformat()
        keyword = _guess_main_keyword(title, category_keywords)
        slug = str(item.get("slug") or item.get("id") or title)
        resolved_category = _resolve_category(item.get("categories"), slug_by_id, category_id)
        added = add_content(
            current,
            {
                "date": raw_date,
                "platform": "wordpress",
                "category": resolved_category,
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
