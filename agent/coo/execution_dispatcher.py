"""Execution Dispatcher — plan-only dispatch preparation (Phase 7E).

Creates dispatch plans from execution tickets. Plan creation is record-only:
no Repository 2 execution, no publish, no subprocess, no terminal, no adapter
dispatch. Entrypoints in plans are audit metadata only — not execution commands.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from agent.coo.execution_ticket import ExecutionTicket, ExecutionTicketStatus, mark_dispatch_pending
from agent.coo.skills_catalog import (
    RISK_CATEGORY_PUBLISH,
    get_skill,
)


class DispatchPlanStatus(str, Enum):
    """Lifecycle status for a dispatch plan — no EXECUTED state."""

    PLANNED = "planned"
    BLOCKED = "blocked"  # future reserved — not emitted in Phase 7E plan-only flow


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ExecutionDispatchRequest:
    """Immutable dispatch request audit record."""

    request_id: str
    ticket_id: str
    approval_session_id: str
    requested_by: str
    requested_at: str
    reason: str = ""
    auto_apply: bool = False
    review_required: bool = True


@dataclass
class ExecutionDispatchPlan:
    """Dispatch plan produced from a ticket — no execution side effects."""

    plan_id: str
    request_id: str
    ticket_id: str
    approval_session_id: str
    status: DispatchPlanStatus
    run_date: str
    requested_by: str = ""
    requested_at: str = ""
    reason: str = ""
    dispatchable_skills: List[str] = field(default_factory=list)
    preview_only_skills: List[str] = field(default_factory=list)
    excluded_skills: List[str] = field(default_factory=list)
    exclusion_reasons: Dict[str, str] = field(default_factory=dict)
    entrypoints_metadata: List[str] = field(default_factory=list)
    blocked_reason: str = ""
    auto_apply: bool = False
    review_required: bool = True
    executed: bool = False
    repository2_touched: bool = False
    created_at: str = field(default_factory=_utc_now_iso)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "request_id": self.request_id,
            "ticket_id": self.ticket_id,
            "approval_session_id": self.approval_session_id,
            "status": self.status.value,
            "run_date": self.run_date,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
            "dispatchable_skills": list(self.dispatchable_skills),
            "preview_only_skills": list(self.preview_only_skills),
            "excluded_skills": list(self.excluded_skills),
            "exclusion_reasons": dict(self.exclusion_reasons),
            "entrypoints_metadata": list(self.entrypoints_metadata),
            "blocked_reason": self.blocked_reason,
            "auto_apply": self.auto_apply,
            "review_required": self.review_required,
            "executed": self.executed,
            "repository2_touched": self.repository2_touched,
            "created_at": self.created_at,
            "notes": list(self.notes),
        }


_DEFAULT_PLAN_STORE: Optional["ExecutionDispatchPlanStore"] = None


class ExecutionDispatchPlanStore:
    """Process-local in-memory dispatch plan store — no file persistence."""

    def __init__(self) -> None:
        self._plans: Dict[str, ExecutionDispatchPlan] = {}
        self._by_ticket: Dict[str, str] = {}

    def save(self, plan: ExecutionDispatchPlan) -> None:
        existing_plan_id = self._by_ticket.get(plan.ticket_id)
        if existing_plan_id is not None and existing_plan_id != plan.plan_id:
            raise ValueError(
                f"Dispatch plan already exists for ticket {plan.ticket_id}: "
                f"{existing_plan_id}"
            )
        self._plans[plan.plan_id] = plan
        self._by_ticket[plan.ticket_id] = plan.plan_id

    def get(self, plan_id: str) -> Optional[ExecutionDispatchPlan]:
        return self._plans.get(plan_id)

    def get_by_ticket(self, ticket_id: str) -> Optional[ExecutionDispatchPlan]:
        plan_id = self._by_ticket.get(ticket_id)
        if plan_id is None:
            return None
        return self._plans.get(plan_id)

    def list_plans(self) -> List[ExecutionDispatchPlan]:
        return list(self._plans.values())

    def clear(self) -> None:
        self._plans.clear()
        self._by_ticket.clear()


def get_default_dispatch_plan_store() -> ExecutionDispatchPlanStore:
    """Return the module-level in-memory dispatch plan store."""
    global _DEFAULT_PLAN_STORE
    if _DEFAULT_PLAN_STORE is None:
        _DEFAULT_PLAN_STORE = ExecutionDispatchPlanStore()
    return _DEFAULT_PLAN_STORE


def _assert_ticket_created(ticket: ExecutionTicket) -> None:
    if ticket.status is not ExecutionTicketStatus.CREATED:
        raise ValueError(
            f"Cannot create dispatch plan for ticket {ticket.ticket_id} "
            f"in status {ticket.status.value}"
        )


def _assert_ticket_safety_flags(ticket: ExecutionTicket) -> None:
    if ticket.execution_dispatched:
        raise ValueError(
            f"Cannot create dispatch plan for ticket {ticket.ticket_id}: "
            "execution_dispatched must remain false."
        )
    if ticket.publish_dispatched:
        raise ValueError(
            f"Cannot create dispatch plan for ticket {ticket.ticket_id}: "
            "publish_dispatched must remain false."
        )
    if ticket.repository2_touched:
        raise ValueError(
            f"Cannot create dispatch plan for ticket {ticket.ticket_id}: "
            "repository2_touched must remain false."
        )
    if ticket.auto_apply:
        raise ValueError(
            f"Cannot create dispatch plan for ticket {ticket.ticket_id}: "
            "auto_apply must remain false."
        )
    if not ticket.review_required:
        raise ValueError(
            f"Cannot create dispatch plan for ticket {ticket.ticket_id}: "
            "review_required must remain true."
        )


def _assert_requester_matches(ticket: ExecutionTicket, requested_by: str) -> None:
    if requested_by != ticket.requester_id:
        raise ValueError(
            f"Requester {requested_by!r} is not authorized for ticket {ticket.ticket_id} "
            f"(owner: {ticket.requester_id!r})"
        )


def _classify_skills(
    skill_ids: List[str],
) -> tuple[List[str], List[str], List[str], Dict[str, str]]:
    dispatchable: List[str] = []
    preview_only: List[str] = []
    excluded: List[str] = []
    exclusion_reasons: Dict[str, str] = {}

    for skill_id in skill_ids:
        definition = get_skill(skill_id)
        if definition is None:
            excluded.append(skill_id)
            exclusion_reasons[skill_id] = "Unknown skill_id — not in catalog."
            continue
        if definition.risk_category == RISK_CATEGORY_PUBLISH:
            excluded.append(skill_id)
            exclusion_reasons[skill_id] = (
                "Publish risk skills require separate approval."
            )
            continue
        if definition.read_only:
            preview_only.append(skill_id)
            continue
        dispatchable.append(skill_id)

    return dispatchable, preview_only, excluded, exclusion_reasons


def create_dispatch_plan_from_ticket(
    ticket: ExecutionTicket,
    requested_by: str,
    reason: str = "",
) -> ExecutionDispatchPlan:
    """Create a dispatch plan from a CREATED ticket — no execution."""
    _assert_ticket_created(ticket)
    _assert_ticket_safety_flags(ticket)
    _assert_requester_matches(ticket, requested_by)

    dispatchable, preview_only, excluded, exclusion_reasons = _classify_skills(
        list(ticket.selected_skills)
    )

    request = ExecutionDispatchRequest(
        request_id=str(uuid.uuid4()),
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        requested_by=requested_by,
        requested_at=_utc_now_iso(),
        reason=reason,
        auto_apply=False,
        review_required=True,
    )

    mark_dispatch_pending(ticket)

    return ExecutionDispatchPlan(
        plan_id=str(uuid.uuid4()),
        request_id=request.request_id,
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        status=DispatchPlanStatus.PLANNED,
        run_date=ticket.run_date,
        requested_by=request.requested_by,
        requested_at=request.requested_at,
        reason=request.reason,
        dispatchable_skills=dispatchable,
        preview_only_skills=preview_only,
        excluded_skills=excluded,
        exclusion_reasons=exclusion_reasons,
        entrypoints_metadata=list(ticket.entrypoints),
        auto_apply=False,
        review_required=True,
        executed=False,
        repository2_touched=False,
        created_at=_utc_now_iso(),
    )
