"""Content memory — topic-duplicate detection, ported from
multi-content-pipeline/memory/content-memory.js.

Stores under Hermes-Agent's own data directory (not inside Repository 2) so
Hermes is self-contained.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_RECENT_DAYS = 30
# Same-platform main keyword / title collisions are never allowed again.
_BLOCK_FOREVER_DAYS = 3650

_BLOCKING_REASONS = frozenset(
    {
        "same_topic",
        "same_title",
        "same_slug",
        "same_main_keyword",
        "similar_title",
        "topic_contains",
    }
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_MEMORY_FILE = _REPO_ROOT / "data" / "content_memory.json"


def _memory_file() -> Path:
    override = os.environ.get("HERMES_CONTENT_MEMORY_FILE")
    return Path(override) if override else _DEFAULT_MEMORY_FILE


def _empty_memory() -> Dict[str, Any]:
    return {"version": 1, "updatedAt": None, "items": []}


def _normalize_text(value: Optional[str]) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[^a-z0-9가-힣\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_slug(value: Optional[str]) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"^-+|-+$", "", text)
    return re.sub(r"-{2,}", "-", text)


def _days_between(a: str, b: str) -> float:
    try:
        left = datetime.fromisoformat(f"{a}T00:00:00+00:00")
        right = datetime.fromisoformat(f"{b}T00:00:00+00:00")
    except ValueError:
        return float("inf")
    return abs((right - left).days)


_TITLE_STOPWORDS = frozenset(
    {
        "확인할", "알아야", "할때", "할", "때", "위한", "하는", "되는", "있는", "없는",
        "정리", "기준", "방법", "가이드", "체크", "초보", "시작", "전", "후", "가지",
        "단계", "안전", "체크리스트", "알아두면", "좋은", "꼭", "봐야", "할것",
        "관련", "정보", "블로그", "글",
    }
)


def _tokenize(value: Optional[str]) -> set:
    tokens = {t for t in _normalize_text(value).split(" ") if len(t) >= 2}
    return {t for t in tokens if t not in _TITLE_STOPWORDS and not re.fullmatch(r"\d+", t)}


def _token_overlap_score(a: Optional[str], b: Optional[str]) -> float:
    left, right = _tokenize(a), _tokenize(b)
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    return intersection / min(len(left), len(right))


def _hash_content(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _build_slug(slug: Optional[str], topic: Optional[str], title: Optional[str]) -> str:
    return _normalize_slug(slug or topic or title or "content")


def _build_main_keyword(main_keyword: Optional[str], sub_keywords: Optional[List[str]]) -> str:
    if main_keyword:
        return str(main_keyword).strip()
    if sub_keywords:
        return str(sub_keywords[0]).strip()
    return ""


def _to_memory_item(input_: Dict[str, Any]) -> Dict[str, Any]:
    sub_keywords = [k for k in (input_.get("subKeywords") or []) if k]
    main_keyword = _build_main_keyword(input_.get("mainKeyword"), sub_keywords)
    slug = _build_slug(input_.get("slug"), input_.get("topic"), input_.get("title"))
    hash_ = input_.get("hash") or _hash_content(
        "|".join(
            str(x)
            for x in [
                input_.get("date"), input_.get("platform"), input_.get("category"),
                input_.get("topic"), input_.get("title"), main_keyword, slug,
            ]
        )
    )
    return {
        "date": input_.get("date"),
        "platform": input_.get("platform"),
        "category": input_.get("category"),
        "topic": input_.get("topic"),
        "title": input_.get("title"),
        "mainKeyword": main_keyword,
        "subKeywords": sub_keywords,
        "slug": slug,
        "hash": hash_,
        "qualityScore": input_.get("qualityScore"),
        "filePath": input_.get("filePath"),
        "recordedAt": datetime.now(timezone.utc).isoformat(),
    }


def _dedupe_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    deduped = []
    for item in items:
        key = item.get("hash") or "|".join(str(item.get(k)) for k in ("date", "platform", "category", "topic", "slug"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _recent_items(memory: Dict[str, Any], date: str, days: int = _RECENT_DAYS) -> List[Dict[str, Any]]:
    # Include same-date drafts so regenerating today cannot repeat this morning's topic.
    return _items_in_window(memory, date=date, days=days, include_same_date=True)


def _similarity_reasons(item: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = []
    item_topic, query_topic = _normalize_text(item.get("topic")), _normalize_text(query.get("topic"))
    item_title = _normalize_text(item.get("title"))
    query_title = _normalize_text(query.get("title") or query.get("topic"))
    item_main_kw = _normalize_text(item.get("mainKeyword"))
    query_main_kw = _normalize_text(query.get("mainKeyword"))
    item_slug, query_slug = _normalize_slug(item.get("slug")), _normalize_slug(query.get("slug"))

    if item_topic and query_topic and item_topic == query_topic:
        reasons.append("same_topic")
    if item_title and query_title and item_title == query_title:
        reasons.append("same_title")
    if item_main_kw and query_main_kw and item_main_kw == query_main_kw:
        reasons.append("same_main_keyword")
    if item_slug and query_slug and item_slug == query_slug:
        reasons.append("same_slug")

    title_overlap = _token_overlap_score(item.get("title"), query.get("title") or query.get("topic"))
    if title_overlap >= 0.6:
        reasons.append("similar_title")

    topic_contains = item_topic and query_topic and (item_topic in query_topic or query_topic in item_topic)
    if topic_contains and item_topic != query_topic:
        reasons.append("topic_contains")

    return {"reasons": reasons, "titleOverlap": title_overlap}


def _risk_from_reasons(reasons: List[str]) -> str:
    # Absolute rule: any topical collision is a hard block (high).
    if any(r in _BLOCKING_REASONS for r in reasons):
        return "high"
    return "none"


def _risk_rank(risk: str) -> int:
    return {"high": 3, "medium": 2, "low": 1, "none": 0}.get(risk, 0)


def _items_in_window(
    memory: Dict[str, Any],
    *,
    date: str,
    days: int,
    include_same_date: bool = True,
) -> List[Dict[str, Any]]:
    result = []
    for item in memory.get("items") or []:
        item_date = item.get("date")
        if not item_date:
            continue
        if not include_same_date and item_date == date:
            continue
        if _days_between(item_date, date) <= days:
            result.append(item)
    return result


def load_memory() -> Dict[str, Any]:
    path = _memory_file()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        merged = {**_empty_memory(), **parsed}
        merged["items"] = parsed.get("items") if isinstance(parsed.get("items"), list) else []
        return merged
    except FileNotFoundError:
        return _empty_memory()
    except (json.JSONDecodeError, OSError):
        return _empty_memory()


def save_memory(memory: Dict[str, Any]) -> Path:
    path = _memory_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **_empty_memory(),
        **memory,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "items": _dedupe_items(memory.get("items") or []),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def add_content(memory: Dict[str, Any], input_: Dict[str, Any]) -> Dict[str, Any]:
    current = {**_empty_memory(), **(memory or {})}
    current["items"] = list((memory or {}).get("items") or [])
    item = _to_memory_item(input_)
    current["items"] = _dedupe_items([item] + current["items"])
    current["updatedAt"] = datetime.now(timezone.utc).isoformat()
    return {"memory": current, "item": item}


def find_recent_topics(
    memory: Dict[str, Any], *, date: str, days: int = _RECENT_DAYS,
    category: Optional[str] = None, platform: Optional[str] = None,
) -> List[Dict[str, Any]]:
    items = _recent_items(memory, date, days)
    if category:
        items = [i for i in items if i.get("category") == category]
    if platform:
        items = [i for i in items if i.get("platform") == platform]
    return sorted(items, key=lambda i: str(i.get("date")), reverse=True)


def find_similar_topics(memory: Dict[str, Any], query: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Forever window for absolute no-repeat; still filter by platform when provided.
    days = int(query.get("days") or _BLOCK_FOREVER_DAYS)
    candidates = find_recent_topics(
        memory,
        date=query.get("date"),
        days=days,
        category=query.get("category"),
        platform=query.get("platform"),
    )
    matches = []
    for item in candidates:
        sim = _similarity_reasons(item, query)
        risk = _risk_from_reasons(sim["reasons"])
        if risk == "none":
            continue
        days_ago = _days_between(item.get("date"), query.get("date")) if query.get("date") and item.get("date") else None
        matches.append({
            **item,
            "reasons": sim["reasons"],
            "risk": risk,
            "titleOverlap": round(sim["titleOverlap"], 2),
            "daysAgo": days_ago,
        })

    return sorted(matches, key=lambda m: (-_risk_rank(m["risk"]), m["daysAgo"] if m["daysAgo"] is not None else 9999))


def is_topic_blocked(memory: Dict[str, Any], query: Dict[str, Any]) -> bool:
    """Hard gate — True means this topic must not be written again."""
    return bool(find_similar_topics(memory, {**query, "days": _BLOCK_FOREVER_DAYS}))


def used_main_keywords(
    memory: Dict[str, Any],
    *,
    date: str,
    platform: str,
    category: Optional[str] = None,
) -> set:
    """Main keywords already used on this platform (never reuse)."""
    used = set()
    for item in find_recent_topics(
        memory, date=date, days=_BLOCK_FOREVER_DAYS, category=category, platform=platform
    ):
        kw = _normalize_text(item.get("mainKeyword") or "")
        if kw:
            used.add(kw)
    return used


def build_memory_check(
    memory: Dict[str, Any], *, date: str, topic_title: str, topic_id: str,
    topic_keywords: List[str], category_id: str, platform_id: str,
) -> Dict[str, Any]:
    main_keyword = topic_keywords[0] if topic_keywords else ""
    sub_keywords = topic_keywords[1:] if len(topic_keywords) > 1 else []
    query = {
        "date": date, "platform": platform_id, "category": category_id,
        "topic": topic_title, "title": topic_title, "mainKeyword": main_keyword,
        "subKeywords": sub_keywords, "slug": topic_id, "days": _BLOCK_FOREVER_DAYS,
    }
    similar_topics = find_similar_topics(memory, query)[:10]
    same_keyword = any("same_main_keyword" in item["reasons"] for item in similar_topics)
    duplicate_risk = similar_topics[0]["risk"] if similar_topics else "none"
    blocked = duplicate_risk == "high"

    return {
        "date": date, "platform": platform_id, "category": category_id,
        "topicId": topic_id, "topicTitle": topic_title, "mainKeyword": main_keyword,
        "windowDays": _BLOCK_FOREVER_DAYS, "sameKeyword": same_keyword,
        "duplicateRisk": duplicate_risk, "blocked": blocked,
        "similarCount": len(similar_topics),
        "similarTopics": similar_topics,
    }
