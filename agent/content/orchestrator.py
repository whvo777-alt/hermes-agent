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
import logging
import os
import re
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.content.config.categories import Category, get_effective_keywords
from agent.content.config.launch_policy import get_launch_category
from agent.content.config.platforms import Platform, find_platform
from agent.content.images.hero_image import HeroImage, insert_hero_image
from agent.content.images.title_card import create_title_card
from agent.content.markdown_html import extract_title, markdown_to_tistory_html
from agent.content.learning.feedback import get_recent_feedback
from agent.content.memory.content_memory import (
    _normalize_text,
    add_content,
    build_memory_check,
    find_recent_topics,
    is_topic_blocked,
    load_memory,
    save_memory,
    used_main_keywords,
)
from agent.content.memory.corpus_sync import sync_written_corpus
from agent.content.planning.planning import run_planning
from agent.content.quality.quality_gate import QualityGateResult, run_quality_gate
from agent.content.research.research import run_research
from agent.content.seo_enrich import is_seo_title_length_valid, truncate_seo_title
from agent.content.writing.writer import rewrite_title_for_seo_length, write_blog_post

_REPO_ROOT = Path(__file__).resolve().parents[2]


class DuplicateTopicError(RuntimeError):
    """Raised when every category keyword is already used on this platform."""


def _drafts_dir(date: str) -> Path:
    override = os.environ.get("HERMES_CONTENT_DRAFTS_DIR")
    base = Path(override) if override else _REPO_ROOT / "data" / "content_drafts"
    return base / date


_META_DELIM_RE = re.compile(r"\n-{3,}\s*META\s*-{3,}\s*\n?", re.I)
_FRONTMATTER_RE = re.compile(r"\A(---[^\n]*\n.*?\n---[^\n]*(?:\n|$))", re.S)


def _split_meta_block(markdown: str) -> tuple[str, str, bool]:
    value = str(markdown or "")
    parts = _META_DELIM_RE.split(value, maxsplit=1)
    if len(parts) == 2:
        return parts[0].rstrip(), parts[1].strip(), True
    return value, "", False


def _strip_leading_h1(markdown: str) -> str:
    value = str(markdown or "")
    frontmatter_match = _FRONTMATTER_RE.match(value)
    frontmatter = frontmatter_match.group(1) if frontmatter_match else ""
    body = value[len(frontmatter):]
    body = re.sub(r"^#\s+[^\n]*(?:\n|$)", "", body, count=1, flags=re.M)
    return f"{frontmatter}{body}"


def _save_draft_files(
    platform_dir: Path,
    markdown: str,
    *,
    separate_meta: bool = False,
    platform_id: Optional[str] = None,
) -> tuple[Path, Optional[Path], str]:
    body, notes, has_meta = _split_meta_block(markdown) if separate_meta else (markdown, "", False)
    if separate_meta:
        body = _strip_leading_h1(body)
    platform_dir.mkdir(parents=True, exist_ok=True)

    blog_file = platform_dir / "blog.md"
    blog_file.write_text(body, encoding="utf-8")
    if platform_id == "tistory":
        (platform_dir / "blog.tistory.html").write_text(
            markdown_to_tistory_html(body),
            encoding="utf-8",
        )

    notes_file: Optional[Path] = None
    if has_meta:
        notes_file = platform_dir / "notes.md"
        notes_file.write_text(f"{notes}\n" if notes else "", encoding="utf-8")
    return blog_file, notes_file, body


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
        topic_title = keyword
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


def _replace_h1_title(markdown: str, title: str) -> str:
    """Replace only the first Markdown H1, preserving frontmatter and body."""
    replacement = f"# {title.strip()}"
    return re.sub(r"^#\s+.+$", replacement, str(markdown or ""), count=1, flags=re.M)


def _prepare_title_for_image_and_quality(
    blog_content: str,
    *,
    topic_title: str,
) -> Dict[str, Any]:
    """Make the final H1/SEO title decision before rendering the title card."""
    # These helpers live in publish_on_approval.py because the same title
    # construction must be used at Rank Math publish time. Import locally to
    # avoid the orchestrator -> bundle -> orchestrator import cycle.
    from agent.content.publish_on_approval import (
        _build_rank_math_title,
        _pick_focus_keyword,
        extract_seo_meta,
    )

    content = str(blog_content or "")
    title = extract_title(content, topic_title)
    seo_meta = extract_seo_meta(content)
    focus_keyword = _pick_focus_keyword(
        seo_meta,
        topic_title=topic_title,
        title=title,
    )
    seo_title = _build_rank_math_title(title, focus_keyword)
    rewrite_attempted = not is_seo_title_length_valid(seo_title)
    rewrite_failed = False

    if rewrite_attempted:
        prefixed_seo_title = truncate_seo_title(f"{focus_keyword} | {title}")
        try:
            rewritten_title = rewrite_title_for_seo_length(
                title=title,
                focus_keyword=focus_keyword,
                current_seo_title=seo_title,
                prefixed_seo_title=prefixed_seo_title,
            )
        except Exception as exc:  # noqa: BLE001 — retain draft and report the final range
            logging.getLogger(__name__).warning(
                "SEO title rewrite failed; retaining original H1: %s",
                exc,
            )
            rewritten_title = ""
        if rewritten_title:
            content = _replace_h1_title(content, rewritten_title)
            title = extract_title(content, topic_title)
            seo_title = _build_rank_math_title(title, focus_keyword)
        else:
            rewrite_failed = True

    return {
        "blog_content": content,
        "h1_title": title,
        "h1_length": len(title),
        "focus_keyword": focus_keyword,
        "seo_title": seo_title,
        "rewrite_attempted": rewrite_attempted,
        "rewrite_failed": rewrite_failed,
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
    prepared_title = _prepare_title_for_image_and_quality(
        blog_content,
        topic_title=topic["topic_title"],
    )
    blog_content = prepared_title["blog_content"]

    platform_dir = _drafts_dir(resolved_date) / platform_id
    # Featured image is always a title card (seeded rotation across
    # title_card.TEMPLATE_IDS) -- never the old hero "photo" mode. In-body
    # section cards (section_infographics.py) are unaffected.
    if platform_id == "tistory":
        image = HeroImage(
            role="hero",
            status="disabled",
            file="",
            alt="",
            caption="",
            prompt="",
            inserted_in="",
            created_at="",
        )
    else:
        image = create_title_card(
            out_dir=platform_dir / "images", platform_id=platform.id, platform_label=platform.label,
            category_id=category.id, category_name=category.name, category_keywords=category.keywords,
            topic_title=prepared_title["h1_title"], blog_content=blog_content,
            style_seed=topic["topic_id"],
        )
        blog_content = insert_hero_image(platform_id=platform.id, blog_content=blog_content, image=image)

    recent_title_items = find_recent_topics(
        memory,
        date=resolved_date,
        category=category.id,
        platform=platform.id,
    )
    recent_titles = [
        str(item.get("title") or "").strip()
        for item in recent_title_items
        if str(item.get("title") or "").strip()
    ]

    quality = run_quality_gate(
        topic_title=topic["topic_title"], category_id=category.id, platform_id=platform.id,
        content_type="blog", content=blog_content, image=image.to_dict(),
        recent_titles=recent_titles,
        seo_title=prepared_title["seo_title"],
        seo_title_rewrite_attempted=prepared_title["rewrite_attempted"],
        seo_title_rewrite_failed=prepared_title["rewrite_failed"],
    )

    blog_file, _notes_file, blog_content = _save_draft_files(
        platform_dir,
        blog_content,
        separate_meta=platform.id == "tistory",
        platform_id=platform.id,
    )

    added = add_content(
        memory,
        {
            "date": resolved_date, "platform": platform.id, "category": category.id,
            "topic": topic["topic_title"], "title": prepared_title["h1_title"],
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
