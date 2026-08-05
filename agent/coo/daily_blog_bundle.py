"""Daily blog approval bundle — groups today's N Hermes-generated blog
drafts (agent/content/orchestrator.py) into one CEO approval report backed
by N *existing, unmodified* CEO approval sessions (Phase 5C
``approval_session`` API).

Design constraints:
- Sources items from Hermes' own content orchestrator — NOT from
  Repository 2's discord_approval_queue.json (that round-trip is exactly
  what the Big-Bang migration removes).
- Reuses ``CEOApprovalReport`` / ``CEOApprovalReportSection`` as-is.
- Reuses ``create_approval_session`` / ``approve_session`` / ``reject_session``
  as-is — ``approval_session.py``'s status model is never modified.
- Does not create a new "bundle session" or new session status; a bundle is
  just a thin, in-memory grouping of N real sessions plus the drafts they
  refer to.
- "Approve all" / "discard all" are thin sequential-call helpers over the
  existing per-item session functions — not a bundle-level transition.
- Per-item "revise" does not touch ``approval_session`` at all and does not
  write back to Repository 2 (no more R2 queue to write to): it records the
  revision note via ``agent.content.learning.feedback`` so the *next*
  content-generation pass can reference it (per the Learning requirement —
  not an immediate regeneration).
"""

from __future__ import annotations

import uuid
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.coo.approval_report import CEOApprovalReport, CEOApprovalReportSection
from agent.coo.approval_session import (
    CEOApprovalSession,
    CEOApprovalSessionStatus,
    CEOApprovalSessionStore,
    approve_session,
    create_approval_session,
    reject_session,
)
from agent.coo.models import COOOrchestrationResult
from agent.content.orchestrator import ContentDraft


@dataclass
class DailyBlogApprovalItem:
    """One platform's blog draft, paired with its own CEO approval session."""

    platform: str
    platform_label: str
    category_id: str
    category_name: str
    topic_title: str
    quality_score: int
    quality_passed: bool
    quality_warnings: List[str]
    human_review_items: List[str]
    blog_file: str
    blog_summary: str
    blog_preview: str
    preview_chunks: List[str]
    image_file: str
    image_alt: str
    session: CEOApprovalSession
    title: str = ""
    tags: str = ""
    slug: str = ""
    description: str = ""
    revision_requested: bool = False
    revision_note: str = ""
    publish_result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "platform": self.platform,
            "platform_label": self.platform_label,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "topic_title": self.topic_title,
            "quality_score": self.quality_score,
            "quality_passed": self.quality_passed,
            "quality_warnings": self.quality_warnings,
            "human_review_items": list(self.human_review_items),
            "blog_file": self.blog_file,
            "blog_summary": self.blog_summary,
            "blog_preview": self.blog_preview,
            "preview_chunks": list(self.preview_chunks),
            "image_file": self.image_file,
            "image_alt": self.image_alt,
            "session": self.session.to_dict(),
            "revision_requested": self.revision_requested,
            "revision_note": self.revision_note,
            "publish_result": self.publish_result,
        }
        if self.platform == "tistory":
            payload.update(
                title=self.title,
                tags=self.tags,
                slug=self.slug,
                description=self.description,
            )
        return payload


@dataclass
class DailyBlogApprovalBundle:
    """In-memory grouping of N real approval sessions — not a new session type."""

    bundle_id: str
    run_date: str
    requester_id: str
    channel_id: str
    report: CEOApprovalReport
    items: List[DailyBlogApprovalItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "run_date": self.run_date,
            "requester_id": self.requester_id,
            "channel_id": self.channel_id,
            "report_markdown": self.report.to_markdown(),
            "items": [item.to_dict() for item in self.items],
        }


class DailyBlogApprovalBundleStore:
    """Process-local in-memory bundle store — mirrors ``CEOApprovalSessionStore``."""

    def __init__(self) -> None:
        self._bundles: Dict[str, DailyBlogApprovalBundle] = {}

    def save(self, bundle: DailyBlogApprovalBundle) -> None:
        self._bundles[bundle.bundle_id] = bundle

    def get(self, bundle_id: str) -> Optional[DailyBlogApprovalBundle]:
        return self._bundles.get(bundle_id)

    def find_by_session(
        self, session_id: str
    ) -> Optional[tuple[DailyBlogApprovalBundle, DailyBlogApprovalItem]]:
        """Find the existing bundle/item pair behind a Discord custom_id."""
        for bundle in self._bundles.values():
            for item in bundle.items:
                if item.session.session_id == session_id:
                    return bundle, item
        return None


_DEFAULT_BUNDLE_STORE = DailyBlogApprovalBundleStore()


def get_default_bundle_store() -> DailyBlogApprovalBundleStore:
    return _DEFAULT_BUNDLE_STORE


def _strip_frontmatter(content: str) -> str:
    return re.sub(r"^---[\s\S]*?---\s*", "", content or "").strip()


def _frontmatter_value(content: str, key: str) -> str:
    match = re.match(r"^---\s*\n(?P<header>[\s\S]*?)\n---\s*(?:\n|$)", content or "")
    if not match:
        return ""
    key_prefix = f"{key}:"
    for line in match.group("header").splitlines():
        if line.startswith(key_prefix):
            return line[len(key_prefix) :].strip()
    return ""


def _safe_excerpt(text: str, max_chars: int) -> str:
    """Shorten at a sentence/word boundary without breaking markdown lines."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= max_chars:
        return value
    prefix = value[: max_chars + 1]
    sentence_ends = [prefix.rfind(mark) for mark in (". ", "다. ", "요. ", "? ", "! ")]
    cut = max(sentence_ends)
    if cut >= max_chars // 2:
        return prefix[: cut + 1].strip()
    word_cut = prefix.rfind(" ", 0, max_chars)
    return (prefix[:word_cut] if word_cut >= max_chars // 2 else prefix[:max_chars]).rstrip() + "…"


def _paragraphs(lines: List[str]) -> List[str]:
    paragraphs: List[str] = []
    buffer: List[str] = []

    def flush() -> None:
        value = " ".join(buffer).strip()
        if value:
            paragraphs.append(value)
        buffer.clear()

    for raw in lines:
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith(("![", "> ")):
            continue
        if re.match(r"^[-*]\s+", line):
            flush()
            paragraphs.append(line)
            continue
        buffer.append(line)
    flush()
    return paragraphs


def build_structured_preview(blog_content: str) -> tuple[str, str, List[str]]:
    """Return summary/full preview/chunks sampled across the whole article.

    The preview includes the title, introduction, representative H2 sections,
    and a conclusion section when present. It never slices the raw file at an
    arbitrary character offset.
    """
    frontmatter_title = _frontmatter_value(blog_content, "title")
    body = _strip_frontmatter(blog_content)
    lines = body.splitlines()
    title = next(
        (re.sub(r"^#\s+", "", line).strip() for line in lines if re.match(r"^#\s+", line)),
        "제목 없음",
    )
    title = frontmatter_title or title

    h2_indexes = [index for index, line in enumerate(lines) if re.match(r"^##\s+", line)]
    intro_end = h2_indexes[0] if h2_indexes else len(lines)
    intro_paragraphs = _paragraphs(
        [line for line in lines[:intro_end] if not re.match(r"^#\s+", line)]
    )
    intro = next((p for p in intro_paragraphs if len(p) >= 40), "")
    summary = _safe_excerpt(intro or title, 420)

    sections: List[tuple[str, str]] = []
    for offset, start in enumerate(h2_indexes):
        end = h2_indexes[offset + 1] if offset + 1 < len(h2_indexes) else len(lines)
        heading = re.sub(r"^##\s+", "", lines[start]).strip()
        section_paragraphs = _paragraphs(lines[start + 1 : end])
        detail = next((p for p in section_paragraphs if len(p) >= 30), "")
        if detail:
            sections.append((heading, _safe_excerpt(detail, 300)))

    conclusion_pattern = re.compile(r"마무리|결론|정리|끝으로|체크리스트", re.I)
    conclusion = next(
        ((heading, detail) for heading, detail in reversed(sections) if conclusion_pattern.search(heading)),
        sections[-1] if sections else None,
    )
    selected = sections[:3]
    if conclusion and conclusion not in selected:
        selected.append(conclusion)

    parts = [f"# {title}", f"**도입부 핵심**\n{summary}"]
    parts.extend(f"## {heading}\n{detail}" for heading, detail in selected)
    preview = "\n\n".join(parts)

    chunks: List[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}\n\n{part}".strip() if current else part
        if len(candidate) <= 950:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = part
    if current:
        chunks.append(current)
    return summary, preview, chunks


def _draft_section(draft: ContentDraft) -> CEOApprovalReportSection:
    warnings = list(draft.quality.warnings)
    warning_lines = [f"- warning: {warning}" for warning in warnings] or ["- warning: 없음"]
    return CEOApprovalReportSection(
        title=f"{draft.platform.label} ({draft.category.name}) — {draft.topic_title}",
        body_lines=[
            f"- status: `{'quality_passed' if draft.quality.passed else 'quality_failed'}`",
            f"- quality: score {draft.quality.score}, passed={draft.quality.passed}",
            f"- warnings: {len(warnings)}건",
            *warning_lines,
            f"- file: `{draft.blog_file}`",
            f"- image: `{draft.image.file}`",
        ],
    )


def build_daily_blog_report(drafts: List[ContentDraft], run_date: str) -> CEOApprovalReport:
    """Build one ``CEOApprovalReport`` with one section per platform draft."""
    from agent.coo.approval_report import CEOApprovalReportStatus

    sections = [_draft_section(draft) for draft in drafts]
    return CEOApprovalReport(
        status=CEOApprovalReportStatus.READY,
        task_kind="create_and_report",
        run_date=run_date,
        runtime_status="not_started",
        worker_summary=f"오늘({run_date}) 블로그 초안 {len(drafts)}건 — 승인/수정/폐기 대기",
        approval_required=True,
        review_required=True,
        auto_apply=False,
        next_actions=["항목별로 승인/수정/폐기를 선택하거나 전체 승인/전체 폐기를 사용하세요."],
        sections=sections,
    )


def create_daily_blog_approval_bundle(
    drafts: List[ContentDraft],
    orchestration_result: COOOrchestrationResult,
    *,
    run_date: str,
    requester_id: str,
    channel_id: str,
    session_store: Optional[CEOApprovalSessionStore] = None,
    bundle_store: Optional[DailyBlogApprovalBundleStore] = None,
) -> Optional[DailyBlogApprovalBundle]:
    """Create one CEO approval session per draft and group them.

    Returns ``None`` when there are no drafts for ``run_date``. Never calls
    a publisher — approval sessions are record-only.
    """
    if not drafts:
        return None

    report = build_daily_blog_report(drafts, run_date)
    items: List[DailyBlogApprovalItem] = []
    for draft in drafts:
        blog_summary, blog_preview, preview_chunks = build_structured_preview(
            draft.blog_content
        )
        session = create_approval_session(
            report, orchestration_result, requester_id=requester_id, channel_id=channel_id, store=session_store,
        )
        items.append(
            DailyBlogApprovalItem(
                platform=draft.platform.id,
                platform_label=draft.platform.label,
                category_id=draft.category.id,
                category_name=draft.category.name,
                topic_title=draft.topic_title,
                quality_score=draft.quality.score,
                quality_passed=draft.quality.passed,
                quality_warnings=list(draft.quality.warnings),
                human_review_items=list(
                    (draft.quality.metadata or {}).get("humanReviewNeeded") or []
                ),
                blog_file=draft.blog_file,
                blog_summary=blog_summary,
                blog_preview=blog_preview,
                preview_chunks=preview_chunks,
                image_file=draft.image.file,
                image_alt=draft.image.alt,
                session=session,
                title=draft.title,
                tags=draft.tags,
                slug=draft.slug,
                description=draft.description,
            )
        )

    bundle = DailyBlogApprovalBundle(
        bundle_id=str(uuid.uuid4()), run_date=run_date, requester_id=requester_id,
        channel_id=channel_id, report=report, items=items,
    )
    store = bundle_store or _DEFAULT_BUNDLE_STORE
    store.save(bundle)
    return bundle


def approve_all_items(
    bundle: DailyBlogApprovalBundle, *, reviewer: str, requester_id: Optional[str] = None,
    store: Optional[CEOApprovalSessionStore] = None,
) -> List[CEOApprovalSession]:
    """Sequentially approve every item's existing session — no bundle-level state."""
    updated: List[CEOApprovalSession] = []
    for item in bundle.items:
        if item.session.status is not CEOApprovalSessionStatus.PENDING:
            continue
        updated.append(approve_session(item.session.session_id, reviewer=reviewer, requester_id=requester_id, store=store))
    return updated


def reject_all_items(
    bundle: DailyBlogApprovalBundle, *, reviewer: str, requester_id: Optional[str] = None,
    reason: Optional[str] = None, store: Optional[CEOApprovalSessionStore] = None,
) -> List[CEOApprovalSession]:
    """Sequentially reject (discard) every item's existing session."""
    updated: List[CEOApprovalSession] = []
    for item in bundle.items:
        if item.session.status is not CEOApprovalSessionStatus.PENDING:
            continue
        updated.append(reject_session(item.session.session_id, reviewer=reviewer, requester_id=requester_id, reason=reason, store=store))
    return updated


def find_item(bundle: DailyBlogApprovalBundle, platform: str) -> Optional[DailyBlogApprovalItem]:
    for item in bundle.items:
        if item.platform == platform:
            return item
    return None
