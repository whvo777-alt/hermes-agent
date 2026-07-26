"""Content orchestrator — Research → Planning → Writing → Quality for one
platform, using the fixed launch-mode category policy.

No new queue/manifest/pipeline system: this just sequences the ported
stages and returns one in-memory result. Persistence is a single markdown
file + one hero image per platform per day (not a JSON queue).

Topic selection is deterministic (date-seeded pick from the category's
keyword list) — no LLM call, per the token-saving principle (LLM is used
only for Research/Planning/Writing/rewrite). Already-written topics and
main keywords are hard-blocked (never repeated on the same platform).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.content.config.categories import Category, get_effective_keywords
from agent.content.config.launch_policy import get_launch_category
from agent.content.config.platforms import Platform, find_platform
from agent.content.images.ai_hero_image import create_hero_image_ai
from agent.content.images.hero_image import HeroImage, insert_hero_image
from agent.content.learning.feedback import get_recent_feedback
from agent.content.memory.content_memory import (
    _normalize_text,
    add_content,
    build_memory_check,
    is_topic_blocked,
    load_memory,
    save_memory,
    used_main_keywords,
)
from agent.content.memory.corpus_sync import sync_written_corpus
from agent.content.planning.planning import run_planning
from agent.content.quality.quality_gate import QualityGateResult, run_quality_gate
from agent.content.research.research import run_research
from agent.content.writing.writer import write_blog_post

_REPO_ROOT = Path(__file__).resolve().parents[2]


class DuplicateTopicError(RuntimeError):
    """Raised when every category keyword is already used on this platform."""


def _drafts_dir(date: str) -> Path:
    override = os.environ.get("HERMES_CONTENT_DRAFTS_DIR")
    base = Path(override) if override else _REPO_ROOT / "data" / "content_drafts"
    return base / date


def _pick_daily_topic(
    category: Category,
    platform_id: str,
    run_date: str,
    memory: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Deterministic topic pick that never repeats a used main keyword."""
    keywords = get_effective_keywords(category.id) or [category.name]
    seed = int(hashlib.sha256(f"{run_date}:{platform_id}:{category.id}".encode()).hexdigest(), 16)
    mem = memory if memory is not None else load_memory()
    used = used_main_keywords(
        mem, date=run_date, platform=platform_id, category=category.id
    )

    # Rotate from the date seed, then walk the full keyword list until free.
    for offset in range(len(keywords)):
        keyword = keywords[(seed + offset) % len(keywords)]
        topic_title = f"{keyword} 확인할 때 알아야 할 기준"
        topic_id = f"{run_date}-{platform_id}-{category.id}-{(seed + offset) % 10000}"
        query = {
            "date": run_date,
            "platform": platform_id,
            "category": category.id,
            "topic": topic_title,
            "title": topic_title,
            "mainKeyword": keyword,
            "slug": topic_id,
        }
        if _normalize_text(keyword) in used:
            continue
        if is_topic_blocked(mem, query):
            continue
        return {
            "topic_id": topic_id,
            "topic_title": topic_title,
            "topic_keywords": [keyword],
        }

    raise DuplicateTopicError(
        f"{platform_id}/{category.id}: 사용 가능한 새 주제가 없습니다. "
        f"이미 쓴 키워드={sorted(used) or list(keywords)}"
    )


@dataclass
class ContentDraft:
    platform: Platform
    category: Category
    run_date: str
    topic_id: str
    topic_title: str
    topic_keywords: List[str]
    research_content: str
    planning_content: str
    blog_content: str
    image: HeroImage
    quality: QualityGateResult
    blog_file: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform.id,
            "platformLabel": self.platform.label,
            "category": self.category.id,
            "categoryName": self.category.name,
            "runDate": self.run_date,
            "topicId": self.topic_id,
            "topicTitle": self.topic_title,
            "blogContent": self.blog_content,
            "blogFile": self.blog_file,
            "image": self.image.to_dict(),
            "quality": self.quality.to_dict(),
        }


def generate_platform_draft(
    *, platform_id: str, run_date: Optional[str] = None, prior_feedback: Optional[List[str]] = None,
) -> ContentDraft:
    """Run Research→Planning→Writing→Quality for one platform's fixed launch category."""
    platform = find_platform(platform_id)
    if platform is None:
        raise ValueError(f"Unknown platform: {platform_id}")
    category = get_launch_category(platform_id)
    resolved_date = run_date or date_cls.today().isoformat()

    if prior_feedback is None:
        prior_feedback = get_recent_feedback(platform_id=platform_id)

    # Absolute rule: merge local drafts + live WP posts before picking.
    memory = sync_written_corpus(
        platform_id=platform_id,
        category_id=category.id,
        category_keywords=category.keywords,
    )
    topic = _pick_daily_topic(category, platform_id, resolved_date, memory=memory)

    memory_check = build_memory_check(
        memory, date=resolved_date, topic_title=topic["topic_title"], topic_id=topic["topic_id"],
        topic_keywords=topic["topic_keywords"], category_id=category.id, platform_id=platform_id,
    )
    if memory_check.get("blocked"):
        raise DuplicateTopicError(
            f"주제 중복 차단: {topic['topic_title']} "
            f"(similar={memory_check.get('similarCount')})"
        )

    research_content = run_research(
        platform_id=platform.id, platform_label=platform.label, category_id=category.id,
        category_name=category.name, target_audience=category.target_audience, tone=category.tone,
        topic_title=topic["topic_title"], topic_keywords=topic["topic_keywords"],
        category_keywords=category.keywords, caution_hints=category.caution_hints,
    )

    planning_content = run_planning(
        platform_id=platform.id, platform_label=platform.label, category_id=category.id,
        category_name=category.name, target_audience=category.target_audience, tone=category.tone,
        topic_title=topic["topic_title"], topic_keywords=topic["topic_keywords"],
        caution_hints=category.caution_hints, research_content=research_content, memory_check=memory_check,
    )

    blog_content = write_blog_post(
        platform_id=platform.id, platform_label=platform.label, category_id=category.id,
        category_name=category.name, target_audience=category.target_audience, tone=category.tone,
        topic_title=topic["topic_title"], topic_keywords=topic["topic_keywords"],
        category_keywords=category.keywords, caution_hints=category.caution_hints,
        current_date=resolved_date, research_content=research_content, planning_content=planning_content,
        prior_feedback=prior_feedback,
    )

    platform_dir = _drafts_dir(resolved_date) / platform_id
    image = create_hero_image_ai(
        out_dir=platform_dir / "images", platform_id=platform.id, platform_label=platform.label,
        category_id=category.id, category_name=category.name, category_keywords=category.keywords,
        topic_title=topic["topic_title"], blog_content=blog_content,
        style_seed=topic["topic_id"],
    )
    blog_content = insert_hero_image(platform_id=platform.id, blog_content=blog_content, image=image)

    quality = run_quality_gate(
        topic_title=topic["topic_title"], category_id=category.id, platform_id=platform.id,
        content_type="blog", content=blog_content, image=image.to_dict(),
    )

    platform_dir.mkdir(parents=True, exist_ok=True)
    blog_file = platform_dir / "blog.md"
    blog_file.write_text(blog_content, encoding="utf-8")

    added = add_content(
        memory,
        {
            "date": resolved_date, "platform": platform.id, "category": category.id,
            "topic": topic["topic_title"], "title": topic["topic_title"],
            "mainKeyword": topic["topic_keywords"][0] if topic["topic_keywords"] else "",
            "subKeywords": topic["topic_keywords"][1:], "slug": topic["topic_id"],
            "qualityScore": quality.score, "filePath": str(blog_file),
        },
    )
    save_memory(added["memory"])

    return ContentDraft(
        platform=platform, category=category, run_date=resolved_date,
        topic_id=topic["topic_id"], topic_title=topic["topic_title"], topic_keywords=topic["topic_keywords"],
        research_content=research_content, planning_content=planning_content, blog_content=blog_content,
        image=image, quality=quality, blog_file=str(blog_file),
    )


def generate_daily_bundle(
    *,
    run_date: Optional[str] = None,
    platforms: Optional[List[str]] = None,
) -> List[ContentDraft]:
    """Generate drafts for launch-policy platforms.

    When ``platforms`` is omitted, all launch platforms are generated.
    Pass a subset (e.g. ``["wordpress"]``) for sequential verification.
    """
    from agent.content.config.launch_policy import LAUNCH_CATEGORY_MAP

    selected = list(platforms) if platforms else list(LAUNCH_CATEGORY_MAP)
    unknown = [platform_id for platform_id in selected if platform_id not in LAUNCH_CATEGORY_MAP]
    if unknown:
        raise ValueError(f"Unknown platform(s) for daily bundle: {', '.join(unknown)}")
    return [
        generate_platform_draft(platform_id=platform_id, run_date=run_date)
        for platform_id in selected
    ]
