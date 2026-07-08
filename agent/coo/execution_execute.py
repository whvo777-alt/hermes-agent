"""Execute bridge — readiness request and gate foundation (Phase 9B).

Record-only layer between completed dry-runs and future dispatch. No Repository 2
execution, no publish, no subprocess, no terminal, and no adapter dispatch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from agent.coo.execution_dispatcher import DispatchPlanStatus, ExecutionDispatchPlan
from agent.coo.execution_runtime import (
    ExecutionRequest,
    ExecutionRun,
    ExecutionRunMode,
    ExecutionRunStatus,
)
from agent.coo.execution_ticket import ExecutionTicket, ExecutionTicketStatus
from agent.coo.skills_catalog import RISK_CATEGORY_PUBLISH, get_skill

_DEFAULT_REQUEST_STORE: Optional["ExecuteRequestStore"] = None
_DEFAULT_GATE_STORE: Optional["ExecuteGateStore"] = None


class ExecuteRequestMode(str, Enum):
    """Supported execute request modes — Phase 9B: READINESS_CHECK only."""

    READINESS_CHECK = "readiness_check"


class ExecuteGateStatus(str, Enum):
    """Lifecycle status for an execute approval gate.

    ExecuteGate is the source of truth for gate status — not ExecuteRequest.
    """

    NONE = "none"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"  # future reserved — expiry not implemented in Phase 9B


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ExecuteRequest:
    """Immutable execute readiness request — distinct from dry-run request.

    Audit record for the readiness request only. Gate status lives on ExecuteGate;
    query via ExecuteGateStore.get_by_request(execute_request_id).
    """

    execute_request_id: str
    dry_run_request_id: str
    dry_run_run_id: str
    plan_id: str
    ticket_id: str
    approval_session_id: str
    requested_by: str
    requested_at: str
    reason: str = ""
    mode: ExecuteRequestMode = ExecuteRequestMode.READINESS_CHECK
    target_skills: List[str] = field(default_factory=list)
    auto_apply: bool = False
    review_required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execute_request_id": self.execute_request_id,
            "dry_run_request_id": self.dry_run_request_id,
            "dry_run_run_id": self.dry_run_run_id,
            "plan_id": self.plan_id,
            "ticket_id": self.ticket_id,
            "approval_session_id": self.approval_session_id,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
            "mode": self.mode.value,
            "target_skills": list(self.target_skills),
            "auto_apply": self.auto_apply,
            "review_required": self.review_required,
        }


@dataclass
class ExecuteGate:
    """Execute approval gate — record-only until future dispatch phases.

    ExecuteGate is the source of truth for gate status.
    """

    gate_id: str
    execute_request_id: str
    ticket_id: str
    dry_run_run_id: str
    status: ExecuteGateStatus
    created_at: str
    decided_at: str = ""
    decided_by: str = ""
    reason: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "execute_request_id": self.execute_request_id,
            "ticket_id": self.ticket_id,
            "dry_run_run_id": self.dry_run_run_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "notes": list(self.notes),
        }


class ExecuteRequestStore:
    """Process-local in-memory execute request store — no file persistence."""

    def __init__(self) -> None:
        self._requests: Dict[str, ExecuteRequest] = {}
        self._by_dry_run: Dict[str, str] = {}

    def save(self, request: ExecuteRequest) -> None:
        existing_request_id = self._by_dry_run.get(request.dry_run_run_id)
        if (
            existing_request_id is not None
            and existing_request_id != request.execute_request_id
        ):
            raise ValueError(
                f"Execute request already exists for dry-run run "
                f"{request.dry_run_run_id}: {existing_request_id}"
            )
        self._requests[request.execute_request_id] = request
        self._by_dry_run[request.dry_run_run_id] = request.execute_request_id

    def get(self, execute_request_id: str) -> Optional[ExecuteRequest]:
        return self._requests.get(execute_request_id)

    def get_by_dry_run(self, dry_run_run_id: str) -> Optional[ExecuteRequest]:
        request_id = self._by_dry_run.get(dry_run_run_id)
        if request_id is None:
            return None
        return self._requests.get(request_id)

    def list_requests(self) -> List[ExecuteRequest]:
        return list(self._requests.values())

    def clear(self) -> None:
        self._requests.clear()
        self._by_dry_run.clear()


class ExecuteGateStore:
    """Process-local in-memory execute gate store — no file persistence."""

    def __init__(self) -> None:
        self._gates: Dict[str, ExecuteGate] = {}
        self._by_request: Dict[str, str] = {}

    def save(self, gate: ExecuteGate) -> None:
        existing_gate_id = self._by_request.get(gate.execute_request_id)
        if existing_gate_id is not None and existing_gate_id != gate.gate_id:
            raise ValueError(
                f"Execute gate already exists for request {gate.execute_request_id}: "
                f"{existing_gate_id}"
            )
        self._gates[gate.gate_id] = gate
        self._by_request[gate.execute_request_id] = gate.gate_id

    def get(self, gate_id: str) -> Optional[ExecuteGate]:
        return self._gates.get(gate_id)

    def get_by_request(self, execute_request_id: str) -> Optional[ExecuteGate]:
        gate_id = self._by_request.get(execute_request_id)
        if gate_id is None:
            return None
        return self._gates.get(gate_id)

    def list_gates(self) -> List[ExecuteGate]:
        return list(self._gates.values())

    def clear(self) -> None:
        self._gates.clear()
        self._by_request.clear()


def get_default_execute_request_store() -> ExecuteRequestStore:
    """Return the module-level in-memory execute request store."""
    global _DEFAULT_REQUEST_STORE
    if _DEFAULT_REQUEST_STORE is None:
        _DEFAULT_REQUEST_STORE = ExecuteRequestStore()
    return _DEFAULT_REQUEST_STORE


def get_default_execute_gate_store() -> ExecuteGateStore:
    """Return the module-level in-memory execute gate store."""
    global _DEFAULT_GATE_STORE
    if _DEFAULT_GATE_STORE is None:
        _DEFAULT_GATE_STORE = ExecuteGateStore()
    return _DEFAULT_GATE_STORE


def _is_publish_skill(skill_id: str) -> bool:
    definition = get_skill(skill_id)
    if definition is None:
        return False
    return definition.risk_category == RISK_CATEGORY_PUBLISH


def _assert_run_completed_dry_run(run: ExecutionRun) -> None:
    if run.status is not ExecutionRunStatus.COMPLETED:
        raise ValueError(
            f"Cannot create execute request for run {run.run_id} "
            f"in status {run.status.value}"
        )
    if not run.dry_run:
        raise ValueError(
            f"Cannot create execute request for run {run.run_id}: "
            "dry_run must remain true."
        )
    if run.repository2_touched:
        raise ValueError(
            f"Cannot create execute request for run {run.run_id}: "
            "repository2_touched must remain false."
        )


def _assert_request_is_dry_run(request: ExecutionRequest) -> None:
    if request.mode is not ExecutionRunMode.DRY_RUN:
        raise ValueError(
            f"Cannot create execute request from request {request.request_id} "
            f"with mode {request.mode.value}"
        )


def _assert_run_request_alignment(run: ExecutionRun, request: ExecutionRequest) -> None:
    if run.request_id != request.request_id:
        raise ValueError(
            f"Run {run.run_id} request_id {run.request_id!r} does not match "
            f"request {request.request_id!r}"
        )


def _assert_plan_status_planned(plan: ExecutionDispatchPlan) -> None:
    if plan.status is not DispatchPlanStatus.PLANNED:
        raise ValueError(
            f"Cannot create execute request for plan {plan.plan_id} "
            f"in status {plan.status.value}"
        )


def _assert_ticket_dispatch_pending(ticket: ExecutionTicket) -> None:
    if ticket.status is not ExecutionTicketStatus.DISPATCH_PENDING:
        raise ValueError(
            f"Cannot create execute request for ticket {ticket.ticket_id} "
            f"in status {ticket.status.value}"
        )


def _assert_ticket_safety_flags(ticket: ExecutionTicket) -> None:
    if ticket.execution_dispatched:
        raise ValueError(
            f"Cannot create execute request for ticket {ticket.ticket_id}: "
            "execution_dispatched must remain false."
        )
    if ticket.publish_dispatched:
        raise ValueError(
            f"Cannot create execute request for ticket {ticket.ticket_id}: "
            "publish_dispatched must remain false."
        )
    if ticket.repository2_touched:
        raise ValueError(
            f"Cannot create execute request for ticket {ticket.ticket_id}: "
            "repository2_touched must remain false."
        )


def _assert_plan_safety_flags(plan: ExecutionDispatchPlan) -> None:
    if plan.executed:
        raise ValueError(
            f"Cannot create execute request for plan {plan.plan_id}: "
            "executed must remain false."
        )


def _assert_plan_ticket_alignment(plan: ExecutionDispatchPlan, ticket: ExecutionTicket) -> None:
    if plan.ticket_id != ticket.ticket_id:
        raise ValueError(
            f"Plan {plan.plan_id} ticket_id {plan.ticket_id!r} does not match "
            f"ticket {ticket.ticket_id!r}"
        )
    if plan.approval_session_id != ticket.approval_session_id:
        raise ValueError(
            f"Plan {plan.plan_id} approval_session_id does not match ticket "
            f"{ticket.ticket_id}"
        )


def _assert_requester_matches(ticket: ExecutionTicket, requested_by: str) -> None:
    if requested_by != ticket.requester_id:
        raise ValueError(
            f"Requester {requested_by!r} is not authorized for ticket {ticket.ticket_id} "
            f"(owner: {ticket.requester_id!r})"
        )


def _assert_ids_aligned(
    run: ExecutionRun,
    request: ExecutionRequest,
    plan: ExecutionDispatchPlan,
    ticket: ExecutionTicket,
) -> None:
    if request.plan_id != plan.plan_id:
        raise ValueError(
            f"Request {request.request_id} plan_id {request.plan_id!r} "
            f"does not match plan {plan.plan_id!r}"
        )
    if request.ticket_id != ticket.ticket_id:
        raise ValueError(
            f"Request {request.request_id} ticket_id {request.ticket_id!r} "
            f"does not match ticket {ticket.ticket_id!r}"
        )
    if run.plan_id != plan.plan_id:
        raise ValueError(
            f"Run {run.run_id} plan_id {run.plan_id!r} does not match plan {plan.plan_id!r}"
        )
    if run.ticket_id != ticket.ticket_id:
        raise ValueError(
            f"Run {run.run_id} ticket_id {run.ticket_id!r} does not match ticket {ticket.ticket_id!r}"
        )


def _target_skills_from_run(
    run: ExecutionRun,
    request: ExecutionRequest,
) -> List[str]:
    """Return dispatchable skills with planned dry-run status only."""
    preview_skill_ids = {
        str(result.get("skill_id", ""))
        for result in run.preview_results
        if result.get("skill_id")
    }
    blocked_or_failed: List[str] = []
    target_skills: List[str] = []

    for result in run.dispatchable_results:
        skill_id = str(result.get("skill_id", ""))
        if not skill_id:
            continue
        status = str(result.get("status", ""))
        if status == "planned":
            target_skills.append(skill_id)
        elif status in ("failed", "blocked"):
            blocked_or_failed.append(skill_id)

    for skill_id in target_skills:
        if skill_id in preview_skill_ids:
            raise ValueError(
                f"Preview-only skill {skill_id!r} cannot be an execute target."
            )
        if skill_id == "publish_content" or _is_publish_skill(skill_id):
            raise ValueError(
                f"Publish skill {skill_id!r} cannot be an execute target."
            )
        if skill_id in blocked_or_failed:
            raise ValueError(
                f"Blocked or failed skill {skill_id!r} cannot be an execute target."
            )

    for skill_id in request.preview_only_skills:
        if skill_id in target_skills:
            raise ValueError(
                f"Preview-only skill {skill_id!r} cannot be an execute target."
            )

    return target_skills


def create_execute_request_from_dry_run(
    run: ExecutionRun,
    request: ExecutionRequest,
    plan: ExecutionDispatchPlan,
    ticket: ExecutionTicket,
    *,
    requested_by: str,
    reason: str = "",
    request_store: Optional[ExecuteRequestStore] = None,
) -> ExecuteRequest:
    """Create a readiness execute request from a completed dry-run — no dispatch."""
    _assert_run_completed_dry_run(run)
    _assert_request_is_dry_run(request)
    _assert_run_request_alignment(run, request)
    _assert_plan_status_planned(plan)
    _assert_ticket_dispatch_pending(ticket)
    _assert_plan_ticket_alignment(plan, ticket)
    _assert_requester_matches(ticket, requested_by)
    _assert_ticket_safety_flags(ticket)
    _assert_plan_safety_flags(plan)
    _assert_ids_aligned(run, request, plan, ticket)

    store = request_store or get_default_execute_request_store()
    existing = store.get_by_dry_run(run.run_id)
    if existing is not None:
        if existing.requested_by != requested_by:
            raise ValueError(
                f"Requester {requested_by!r} is not authorized for existing execute "
                f"request {existing.execute_request_id} "
                f"(owner: {existing.requested_by!r})"
            )
        return existing

    target_skills = _target_skills_from_run(run, request)

    execute_request = ExecuteRequest(
        execute_request_id=str(uuid.uuid4()),
        dry_run_request_id=request.request_id,
        dry_run_run_id=run.run_id,
        plan_id=plan.plan_id,
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        requested_by=requested_by,
        requested_at=_utc_now_iso(),
        reason=reason,
        mode=ExecuteRequestMode.READINESS_CHECK,
        target_skills=target_skills,
        auto_apply=False,
        review_required=True,
    )
    store.save(execute_request)
    return execute_request


def create_execute_gate(
    execute_request: ExecuteRequest,
    *,
    gate_store: Optional[ExecuteGateStore] = None,
) -> ExecuteGate:
    """Create a pending execute gate for a readiness request — no dispatch."""
    if execute_request.mode is not ExecuteRequestMode.READINESS_CHECK:
        raise ValueError(
            f"Cannot create execute gate for request {execute_request.execute_request_id} "
            f"with mode {execute_request.mode.value}"
        )

    store = gate_store or get_default_execute_gate_store()
    existing = store.get_by_request(execute_request.execute_request_id)
    if existing is not None:
        return existing

    gate = ExecuteGate(
        gate_id=str(uuid.uuid4()),
        execute_request_id=execute_request.execute_request_id,
        ticket_id=execute_request.ticket_id,
        dry_run_run_id=execute_request.dry_run_run_id,
        status=ExecuteGateStatus.PENDING,
        created_at=_utc_now_iso(),
        notes=["Phase 9B execute gate — readiness only, no dispatch"],
    )
    store.save(gate)
    return gate


def approve_execute_gate(
    gate_id: str,
    reviewer: str,
    *,
    gate_store: Optional[ExecuteGateStore] = None,
) -> ExecuteGate:
    """Approve a pending execute gate — record-only, no dispatch."""
    store = gate_store or get_default_execute_gate_store()
    gate = store.get(gate_id)
    if gate is None:
        raise KeyError(f"Execute gate not found: {gate_id}")
    if gate.status is not ExecuteGateStatus.PENDING:
        raise ValueError(
            f"Cannot approve execute gate {gate_id} in status {gate.status.value}"
        )

    gate.status = ExecuteGateStatus.APPROVED
    gate.decided_by = reviewer
    gate.decided_at = _utc_now_iso()
    store.save(gate)
    return gate


def reject_execute_gate(
    gate_id: str,
    reviewer: str,
    reason: str = "",
    *,
    gate_store: Optional[ExecuteGateStore] = None,
) -> ExecuteGate:
    """Reject a pending execute gate — record-only, no dispatch."""
    store = gate_store or get_default_execute_gate_store()
    gate = store.get(gate_id)
    if gate is None:
        raise KeyError(f"Execute gate not found: {gate_id}")
    if gate.status is not ExecuteGateStatus.PENDING:
        raise ValueError(
            f"Cannot reject execute gate {gate_id} in status {gate.status.value}"
        )

    gate.status = ExecuteGateStatus.REJECTED
    gate.decided_by = reviewer
    gate.decided_at = _utc_now_iso()
    gate.reason = reason
    store.save(gate)
    return gate
