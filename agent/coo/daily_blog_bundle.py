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
    topic_title: str
    quality_score: int
    quality_passed: bool
    quality_warnings: List[str]
    blog_file: str
    blog_preview: str
    image_file: str
    image_alt: str
    session: CEOApprovalSession

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "topic_title": self.topic_title,
            "quality_score": self.quality_score,
            "quality_passed": self.quality_passed,
            "quality_warnings": self.quality_warnings,
            "blog_file": self.blog_file,
            "blog_preview": self.blog_preview,
            "image_file": self.image_file,
            "image_alt": self.image_alt,
            "session": self.session.to_dict(),
        }


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


_DEFAULT_BUNDLE_STORE = DailyBlogApprovalBundleStore()


def get_default_bundle_store() -> DailyBlogApprovalBundleStore:
    return _DEFAULT_BUNDLE_STORE


def _preview(blog_content: str, max_chars: int = 400) -> str:
    body = blog_content or ""
    return body[:max_chars] + ("…" if len(body) > max_chars else "")


def _draft_section(draft: ContentDraft) -> CEOApprovalReportSection:
    return CEOApprovalReportSection(
        title=f"{draft.platform.label} ({draft.category.name}) — {draft.topic_title}",
        body_lines=[
            f"- status: `{'quality_passed' if draft.quality.passed else 'quality_failed'}`",
            f"- quality: score {draft.quality.score}, passed={draft.quality.passed}",
            f"- warnings: {len(draft.quality.warnings)}건",
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
        session = create_approval_session(
            report, orchestration_result, requester_id=requester_id, channel_id=channel_id, store=session_store,
        )
        items.append(
            DailyBlogApprovalItem(
                platform=draft.platform.id,
                topic_title=draft.topic_title,
                quality_score=draft.quality.score,
                quality_passed=draft.quality.passed,
                quality_warnings=list(draft.quality.warnings),
                blog_file=draft.blog_file,
                blog_preview=_preview(draft.blog_content),
                image_file=draft.image.file,
                image_alt=draft.image.alt,
                session=session,
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
