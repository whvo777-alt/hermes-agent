"""Content orchestrator — Research → Planning → Writing → Quality for one
platform, using the fixed launch-mode category policy.

No new queue/manifest/pipeline system: this just sequences the ported
stages and returns one in-memory result. Persistence is a single markdown
file + one hero image per platform per day (not a JSON queue).

Topic selection is deterministic (date-seeded pick from the category's
keyword list) — no LLM call, per the token-saving principle (LLM is used
only for Research/Planning/Writing/rewrite).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.content.config.categories import Category
from agent.content.config.launch_policy import get_launch_category
from agent.content.config.platforms import Platform, find_platform
from agent.content.images.hero_image import HeroImage, create_hero_image, insert_hero_image
from agent.content.learning.feedback import get_recent_feedback
from agent.content.memory.content_memory import add_content, build_memory_check, load_memory, save_memory
from agent.content.planning.planning import run_planning
from agent.content.quality.quality_gate import QualityGateResult, run_quality_gate
from agent.content.research.research import run_research
from agent.content.writing.writer import write_blog_post

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _drafts_dir(date: str) -> Path:
    override = os.environ.get("HERMES_CONTENT_DRAFTS_DIR")
    base = Path(override) if override else _REPO_ROOT / "data" / "content_drafts"
    return base / date


def _pick_daily_topic(category: Category, platform_id: str, run_date: str) -> Dict[str, Any]:
    """Deterministic (no-LLM) topic pick — date-seeded rotation over category keywords."""
    seed = int(hashlib.sha256(f"{run_date}:{platform_id}:{category.id}".encode()).hexdigest(), 16)
    keyword = category.keywords[seed % len(category.keywords)] if category.keywords else category.name
    topic_title = f"{keyword} 확인할 때 알아야 할 기준"
    topic_id = f"{run_date}-{platform_id}-{category.id}-{seed % 10000}"
    return {"topic_id": topic_id, "topic_title": topic_title, "topic_keywords": [keyword]}


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

    topic = _pick_daily_topic(category, platform_id, resolved_date)
    if prior_feedback is None:
        prior_feedback = get_recent_feedback(platform_id=platform_id)

    memory = load_memory()
    memory_check = build_memory_check(
        memory, date=resolved_date, topic_title=topic["topic_title"], topic_id=topic["topic_id"],
        topic_keywords=topic["topic_keywords"], category_id=category.id, platform_id=platform_id,
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
    image = create_hero_image(
        out_dir=platform_dir / "images", platform_id=platform.id, platform_label=platform.label,
        category_id=category.id, category_name=category.name, category_keywords=category.keywords,
        topic_title=topic["topic_title"], blog_content=blog_content,
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


def generate_daily_bundle(*, run_date: Optional[str] = None) -> List[ContentDraft]:
    """Generate one draft per launch-policy platform (wordpress/blogspot/tistory/naver)."""
    from agent.content.config.launch_policy import LAUNCH_CATEGORY_MAP

    return [generate_platform_draft(platform_id=platform_id, run_date=run_date) for platform_id in LAUNCH_CATEGORY_MAP]
