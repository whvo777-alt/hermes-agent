"""Execution Ticket — in-memory dispatch-prep layer (Phase 7B).

Creates audit-ready execution tickets from approved CEO approval sessions.
Ticket creation is record-only: no Repository 2 execution, no publish, no
subprocess, no terminal, no dispatch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from agent.coo.approval_session import CEOApprovalSession, CEOApprovalSessionStatus
from agent.coo.skills_catalog import get_skill

_DEFAULT_STORE: Optional["ExecutionTicketStore"] = None

_ENTRYPOINTS_UNAVAILABLE_NOTE = (
    "entrypoints unavailable: selected_workers are worker IDs; "
    "no catalog skill_ids were captured on the approval session"
)


class ExecutionTicketStatus(str, Enum):
    """Lifecycle status for an execution ticket."""

    CREATED = "created"
    DISPATCH_PENDING = "dispatch_pending"  # reached when a dispatch plan is created
    DISPATCHED = "dispatched"  # future reserved — no reachable path until execution Phase
    CANCELLED = "cancelled"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entrypoints_from_catalog(skill_ids: List[str]) -> List[str]:
    """Resolve entrypoints from skills_catalog only — never from external input."""
    entrypoints: List[str] = []
    for skill_id in skill_ids:
        definition = get_skill(skill_id)
        if definition is None:
            entrypoints.append("")
            continue
        entrypoints.append(definition.entrypoint_hint)
    return entrypoints


def _ticket_skill_metadata(
    session: CEOApprovalSession,
) -> tuple[List[str], List[str], List[str], List[str]]:
    """Return (selected_workers, selected_skills, entrypoints) with honest namespaces."""
    selected_workers = list(session.selected_workers)
    selected_skills = list(session.selected_skill_ids)
    notes: List[str] = []

    if selected_skills:
        entrypoints = _entrypoints_from_catalog(selected_skills)
    else:
        entrypoints = []
        if selected_workers:
            notes.append(_ENTRYPOINTS_UNAVAILABLE_NOTE)

    return selected_workers, selected_skills, entrypoints, notes


@dataclass
class ExecutionTicket:
    """In-memory execution ticket — no dispatch side effects."""

    ticket_id: str
    approval_session_id: str
    status: ExecutionTicketStatus
    task_kind: str
    run_date: str
    selected_workers: List[str] = field(default_factory=list)
    selected_skills: List[str] = field(default_factory=list)
    entrypoints: List[str] = field(default_factory=list)
    strategy_id: str = ""
    requester_id: str = "CEO"
    channel_id: str = ""
    reviewer: str = ""
    auto_apply: bool = False
    review_required: bool = True
    execution_dispatched: bool = False
    publish_dispatched: bool = False
    repository2_touched: bool = False
    dispatch_generation: int = 0
    created_at: str = field(default_factory=_utc_now_iso)
    source_report_status: str = ""
    source_runtime_status: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "approval_session_id": self.approval_session_id,
            "status": self.status.value,
            "task_kind": self.task_kind,
            "run_date": self.run_date,
            "strategy_id": self.strategy_id,
            "selected_workers": list(self.selected_workers),
            "selected_skills": list(self.selected_skills),
            "entrypoints": list(self.entrypoints),
            "requester_id": self.requester_id,
            "channel_id": self.channel_id,
            "reviewer": self.reviewer,
            "auto_apply": self.auto_apply,
            "review_required": self.review_required,
            "execution_dispatched": self.execution_dispatched,
            "publish_dispatched": self.publish_dispatched,
            "repository2_touched": self.repository2_touched,
            "dispatch_generation": self.dispatch_generation,
            "created_at": self.created_at,
            "source_report_status": self.source_report_status,
            "source_runtime_status": self.source_runtime_status,
            "notes": list(self.notes),
        }


class ExecutionTicketStore:
    """Process-local in-memory ticket store — no file persistence."""

    def __init__(self) -> None:
        self._tickets: Dict[str, ExecutionTicket] = {}
        self._by_session: Dict[str, str] = {}

    def save(self, ticket: ExecutionTicket) -> None:
        existing_ticket_id = self._by_session.get(ticket.approval_session_id)
        if (
            existing_ticket_id is not None
            and existing_ticket_id != ticket.ticket_id
        ):
            raise ValueError(
                f"Execution ticket already exists for approval session "
                f"{ticket.approval_session_id}: {existing_ticket_id}"
            )
        self._tickets[ticket.ticket_id] = ticket
        self._by_session[ticket.approval_session_id] = ticket.ticket_id

    def get(self, ticket_id: str) -> Optional[ExecutionTicket]:
        return self._tickets.get(ticket_id)

    def get_by_session(self, session_id: str) -> Optional[ExecutionTicket]:
        ticket_id = self._by_session.get(session_id)
        if ticket_id is None:
            return None
        return self._tickets.get(ticket_id)

    def list_tickets(self) -> List[ExecutionTicket]:
        return list(self._tickets.values())

    def clear(self) -> None:
        self._tickets.clear()
        self._by_session.clear()


def get_default_ticket_store() -> ExecutionTicketStore:
    """Return the module-level in-memory ticket store."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = ExecutionTicketStore()
    return _DEFAULT_STORE


def _assert_session_approved(session: CEOApprovalSession) -> None:
    if session.status is not CEOApprovalSessionStatus.APPROVED:
        raise ValueError(
            f"Cannot create execution ticket for session {session.session_id} "
            f"in status {session.status.value}"
        )


def _assert_session_safety_flags(session: CEOApprovalSession) -> None:
    if session.auto_apply:
        raise ValueError(
            f"Cannot create execution ticket for session {session.session_id}: "
            "auto_apply must remain false."
        )
    if not session.review_required:
        raise ValueError(
            f"Cannot create execution ticket for session {session.session_id}: "
            "review_required must remain true."
        )


def create_ticket_from_approval_session(
    session: CEOApprovalSession,
    *,
    store: Optional[ExecutionTicketStore] = None,
) -> ExecutionTicket:
    """Create an execution ticket from an approved session — no dispatch.

  Idempotent: if a ticket already exists for ``session.session_id``, returns
  the existing ticket without creating a duplicate.
    """
    ticket_store = store or get_default_ticket_store()

    existing = ticket_store.get_by_session(session.session_id)
    if existing is not None:
        return existing

    _assert_session_approved(session)
    _assert_session_safety_flags(session)

    selected_workers, selected_skills, entrypoints, notes = _ticket_skill_metadata(session)

    ticket = ExecutionTicket(
        ticket_id=str(uuid.uuid4()),
        approval_session_id=session.session_id,
        status=ExecutionTicketStatus.CREATED,
        task_kind=session.task_kind,
        run_date=session.run_date,
        selected_workers=selected_workers,
        selected_skills=selected_skills,
        entrypoints=entrypoints,
        requester_id=session.requester_id,
        channel_id=session.channel_id,
        reviewer=session.reviewer,
        auto_apply=False,
        review_required=True,
        execution_dispatched=False,
        publish_dispatched=False,
        repository2_touched=False,
        created_at=_utc_now_iso(),
        source_report_status=session.report_status,
        source_runtime_status=session.runtime_status,
        notes=notes,
    )
    ticket_store.save(ticket)
    return ticket


def mark_dispatch_pending(ticket: ExecutionTicket) -> None:
    """Transition a ticket from CREATED to DISPATCH_PENDING — no execution flags."""
    if ticket.status is not ExecutionTicketStatus.CREATED:
        raise ValueError(
            f"Cannot mark ticket {ticket.ticket_id} dispatch pending "
            f"from status {ticket.status.value}"
        )
    ticket.status = ExecutionTicketStatus.DISPATCH_PENDING


def bump_dispatch_generation(ticket: ExecutionTicket, reason: str = "") -> int:
    """Increment the ticket dispatch generation — lineage invalidation marker."""
    ticket.dispatch_generation += 1
    if reason:
        ticket.notes.append(f"dispatch_generation bumped: {reason}")
    return ticket.dispatch_generation
