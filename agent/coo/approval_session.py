"""CEO Approval Session — in-memory approval workflow (Phase 5C).

Creates and tracks CEO approval sessions from ``CEOApprovalReport``.
Approval transitions are record-only: no Repository 2 execution, no publish,
no Execution Ticket generation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from agent.coo.approval_report import CEOApprovalReport, CEOApprovalReportStatus
from agent.coo.models import COOOrchestrationResult

_SESSION_TTL = timedelta(hours=24)


class CEOApprovalSessionStatus(str, Enum):
    """Lifecycle status for a CEO approval session."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso8601(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _default_expires_at(created_at: str) -> str:
    return (_parse_iso8601(created_at) + _SESSION_TTL).isoformat()


@dataclass
class CEOApprovalSession:
    """In-memory CEO approval session — no execution side effects."""

    session_id: str
    status: CEOApprovalSessionStatus
    task_kind: str
    run_date: str
    report_status: str
    runtime_status: str
    auto_apply: bool = False
    review_required: bool = True
    approval_required: bool = False
    requester_id: str = "CEO"
    channel_id: str = ""
    expires_at: Optional[str] = None
    blocked_workers: List[str] = field(default_factory=list)
    waiting_workers: List[str] = field(default_factory=list)
    selected_workers: List[str] = field(default_factory=list)
    provider_result_count: int = 0
    reviewer: str = ""
    rejection_reason: str = ""
    approved_at: str = ""
    rejected_at: str = ""
    execution_ticket_id: str = ""
    execution_dispatched: bool = False
    publish_dispatched: bool = False
    created_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status.value,
            "task_kind": self.task_kind,
            "run_date": self.run_date,
            "report_status": self.report_status,
            "runtime_status": self.runtime_status,
            "auto_apply": self.auto_apply,
            "review_required": self.review_required,
            "approval_required": self.approval_required,
            "requester_id": self.requester_id,
            "channel_id": self.channel_id,
            "expires_at": self.expires_at,
            "blocked_workers": list(self.blocked_workers),
            "waiting_workers": list(self.waiting_workers),
            "selected_workers": list(self.selected_workers),
            "provider_result_count": self.provider_result_count,
            "reviewer": self.reviewer,
            "rejection_reason": self.rejection_reason,
            "approved_at": self.approved_at,
            "rejected_at": self.rejected_at,
            "execution_ticket_id": self.execution_ticket_id,
            "execution_dispatched": self.execution_dispatched,
            "publish_dispatched": self.publish_dispatched,
            "created_at": self.created_at,
        }


class CEOApprovalSessionStore:
    """Process-local in-memory session store — no file persistence."""

    def __init__(self) -> None:
        self._sessions: Dict[str, CEOApprovalSession] = {}

    def save(self, session: CEOApprovalSession) -> None:
        self._sessions[session.session_id] = session

    def get(self, session_id: str) -> Optional[CEOApprovalSession]:
        return self._sessions.get(session_id)

    def list_sessions(self) -> List[CEOApprovalSession]:
        return list(self._sessions.values())

    def clear(self) -> None:
        self._sessions.clear()

    def expire_pending_sessions(self, now: Optional[datetime] = None) -> int:
        """Mark overdue pending sessions as EXPIRED. Returns count expired."""
        current = now or datetime.now(timezone.utc)
        expired_count = 0
        for session in self._sessions.values():
            if session.status is not CEOApprovalSessionStatus.PENDING:
                continue
            if not session.expires_at:
                continue
            if current >= _parse_iso8601(session.expires_at):
                session.status = CEOApprovalSessionStatus.EXPIRED
                expired_count += 1
        return expired_count


_DEFAULT_STORE = CEOApprovalSessionStore()


def get_default_session_store() -> CEOApprovalSessionStore:
    """Return the module-level in-memory session store."""
    return _DEFAULT_STORE


def should_create_approval_session(report: CEOApprovalReport) -> bool:
    """Return whether an approval session should be created for this report."""
    if report.status is CEOApprovalReportStatus.NOT_STARTED:
        return False
    return report.review_required or report.approval_required


def create_approval_session(
    report: CEOApprovalReport,
    orchestration_result: COOOrchestrationResult,
    *,
    requester_id: str = "CEO",
    channel_id: str = "",
    store: Optional[CEOApprovalSessionStore] = None,
) -> CEOApprovalSession:
    """Create a pending approval session from a CEO approval report."""
    session_store = store or _DEFAULT_STORE
    created_at = _utc_now_iso()
    session = CEOApprovalSession(
        session_id=str(uuid.uuid4()),
        status=CEOApprovalSessionStatus.PENDING,
        task_kind=report.task_kind,
        run_date=report.run_date,
        report_status=report.status.value,
        runtime_status=report.runtime_status,
        auto_apply=False,
        review_required=True,
        approval_required=report.approval_required,
        requester_id=requester_id,
        channel_id=channel_id,
        expires_at=_default_expires_at(created_at),
        blocked_workers=list(report.blocked_workers),
        waiting_workers=list(report.waiting_workers),
        selected_workers=list(report.selected_workers),
        provider_result_count=report.provider_result_count,
        created_at=created_at,
    )
    session_store.save(session)
    return session


def _assert_requester_authorized(
    session: CEOApprovalSession,
    *,
    reviewer: str,
    requester_id: Optional[str],
) -> None:
    actor = requester_id if requester_id is not None else reviewer
    if actor != session.requester_id:
        raise ValueError(
            f"Requester {actor!r} is not authorized for session {session.session_id} "
            f"(owner: {session.requester_id!r})"
        )


def approve_session(
    session_id: str,
    *,
    reviewer: str = "CEO",
    requester_id: Optional[str] = None,
    store: Optional[CEOApprovalSessionStore] = None,
) -> CEOApprovalSession:
    """Mark a session approved — record only; no execution or publish."""
    session_store = store or _DEFAULT_STORE
    session = session_store.get(session_id)
    if session is None:
        raise KeyError(f"Approval session not found: {session_id}")
    if session.status is not CEOApprovalSessionStatus.PENDING:
        raise ValueError(
            f"Cannot approve session {session_id} in status {session.status.value}"
        )
    _assert_requester_authorized(session, reviewer=reviewer, requester_id=requester_id)

    session.status = CEOApprovalSessionStatus.APPROVED
    session.reviewer = reviewer
    session.approved_at = _utc_now_iso()
    # Phase 5C: approval is record-only — no ticket, no R2 dispatch, no publish.
    session.execution_ticket_id = ""
    session.execution_dispatched = False
    session.publish_dispatched = False
    session_store.save(session)
    return session


def reject_session(
    session_id: str,
    *,
    reviewer: str = "CEO",
    requester_id: Optional[str] = None,
    reason: Optional[str] = None,
    store: Optional[CEOApprovalSessionStore] = None,
) -> CEOApprovalSession:
    """Mark a session rejected — record only."""
    session_store = store or _DEFAULT_STORE
    session = session_store.get(session_id)
    if session is None:
        raise KeyError(f"Approval session not found: {session_id}")
    if session.status is not CEOApprovalSessionStatus.PENDING:
        raise ValueError(
            f"Cannot reject session {session_id} in status {session.status.value}"
        )
    _assert_requester_authorized(session, reviewer=reviewer, requester_id=requester_id)

    session.status = CEOApprovalSessionStatus.REJECTED
    session.reviewer = reviewer
    session.rejection_reason = reason or ""
    session.rejected_at = _utc_now_iso()
    session.execution_ticket_id = ""
    session.execution_dispatched = False
    session.publish_dispatched = False
    session_store.save(session)
    return session
