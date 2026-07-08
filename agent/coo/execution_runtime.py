"""Execution Runtime — dry-run foundation (Phase 8B/8E).

Creates execution requests and dry-run runs from dispatch plans. Record-only:
no Repository 2 execution, no publish, no subprocess, no terminal, no adapter
dispatch. Skill dry-run results come from PipelineAdapter.dry_run() via
runtime_skill_adapter (Phase 8E).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from agent.coo.execution_dispatcher import (
    DispatchPlanStatus,
    ExecutionDispatchPlan,
)
from agent.coo.execution_ticket import ExecutionTicket, ExecutionTicketStatus
from agent.coo.runtime_skill_adapter import dry_run_skill, to_preview_result
from agent.coo.skills_catalog import RISK_CATEGORY_PUBLISH, get_skill

_DEFAULT_RUN_STORE: Optional["ExecutionRunStore"] = None
_DEFAULT_REQUEST_STORE: Optional["ExecutionRequestStore"] = None


class ExecutionRunMode(str, Enum):
    """Supported execution run modes — Phase 8B: DRY_RUN only."""

    DRY_RUN = "dry_run"


class ExecutionRunStatus(str, Enum):
    """Lifecycle status for an execution run."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ExecutionRequest:
    """Immutable dry-run request audit record — distinct from dispatch plan request."""

    request_id: str
    plan_id: str
    ticket_id: str
    approval_session_id: str
    requested_by: str
    requested_at: str
    reason: str = ""
    mode: ExecutionRunMode = ExecutionRunMode.DRY_RUN
    dispatchable_skills: List[str] = field(default_factory=list)
    preview_only_skills: List[str] = field(default_factory=list)
    excluded_skills: List[str] = field(default_factory=list)
    auto_apply: bool = False
    review_required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "plan_id": self.plan_id,
            "ticket_id": self.ticket_id,
            "approval_session_id": self.approval_session_id,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
            "mode": self.mode.value,
            "dispatchable_skills": list(self.dispatchable_skills),
            "preview_only_skills": list(self.preview_only_skills),
            "excluded_skills": list(self.excluded_skills),
            "auto_apply": self.auto_apply,
            "review_required": self.review_required,
        }


@dataclass
class ExecutionRun:
    """Dry-run execution run — adapter-backed skill results (Phase 8E)."""

    run_id: str
    request_id: str
    plan_id: str
    ticket_id: str
    approval_session_id: str
    status: ExecutionRunStatus
    mode: ExecutionRunMode = ExecutionRunMode.DRY_RUN
    dry_run: bool = True
    run_date: str = ""
    started_at: str = field(default_factory=_utc_now_iso)
    finished_at: str = ""
    dispatchable_results: List[Dict[str, Any]] = field(default_factory=list)
    preview_results: List[Dict[str, Any]] = field(default_factory=list)
    blocked_skills: List[str] = field(default_factory=list)
    summary: str = ""
    repository2_touched: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "plan_id": self.plan_id,
            "ticket_id": self.ticket_id,
            "approval_session_id": self.approval_session_id,
            "status": self.status.value,
            "mode": self.mode.value,
            "dry_run": self.dry_run,
            "run_date": self.run_date,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dispatchable_results": [dict(item) for item in self.dispatchable_results],
            "preview_results": [dict(item) for item in self.preview_results],
            "blocked_skills": list(self.blocked_skills),
            "summary": self.summary,
            "repository2_touched": self.repository2_touched,
            "notes": list(self.notes),
        }


class ExecutionRequestStore:
    """Process-local in-memory dry-run request store — no file persistence."""

    def __init__(self) -> None:
        self._requests: Dict[str, ExecutionRequest] = {}

    def save(self, request: ExecutionRequest) -> None:
        self._requests[request.request_id] = request

    def get(self, request_id: str) -> Optional[ExecutionRequest]:
        return self._requests.get(request_id)

    def list_requests(self) -> List[ExecutionRequest]:
        return list(self._requests.values())

    def clear(self) -> None:
        self._requests.clear()


class ExecutionRunStore:
    """Process-local in-memory execution run store — no file persistence."""

    def __init__(self) -> None:
        self._runs: Dict[str, ExecutionRun] = {}
        self._by_request: Dict[str, str] = {}

    def save(self, run: ExecutionRun) -> None:
        existing_run_id = self._by_request.get(run.request_id)
        if existing_run_id is not None and existing_run_id != run.run_id:
            raise ValueError(
                f"Execution run already exists for request {run.request_id}: "
                f"{existing_run_id}"
            )
        self._runs[run.run_id] = run
        self._by_request[run.request_id] = run.run_id

    def get(self, run_id: str) -> Optional[ExecutionRun]:
        return self._runs.get(run_id)

    def get_by_request(self, request_id: str) -> Optional[ExecutionRun]:
        run_id = self._by_request.get(request_id)
        if run_id is None:
            return None
        return self._runs.get(run_id)

    def list_runs(self) -> List[ExecutionRun]:
        return list(self._runs.values())

    def clear(self) -> None:
        self._runs.clear()
        self._by_request.clear()


def get_default_execution_run_store() -> ExecutionRunStore:
    """Return the module-level in-memory execution run store."""
    global _DEFAULT_RUN_STORE
    if _DEFAULT_RUN_STORE is None:
        _DEFAULT_RUN_STORE = ExecutionRunStore()
    return _DEFAULT_RUN_STORE


def get_default_execution_request_store() -> ExecutionRequestStore:
    """Return the module-level in-memory dry-run request store."""
    global _DEFAULT_REQUEST_STORE
    if _DEFAULT_REQUEST_STORE is None:
        _DEFAULT_REQUEST_STORE = ExecutionRequestStore()
    return _DEFAULT_REQUEST_STORE


def _assert_requester_matches(ticket: ExecutionTicket, requested_by: str) -> None:
    if requested_by != ticket.requester_id:
        raise ValueError(
            f"Requester {requested_by!r} is not authorized for ticket {ticket.ticket_id} "
            f"(owner: {ticket.requester_id!r})"
        )


def _assert_plan_status_planned(plan: ExecutionDispatchPlan) -> None:
    if plan.status is not DispatchPlanStatus.PLANNED:
        raise ValueError(
            f"Cannot create execution request for plan {plan.plan_id} "
            f"in status {plan.status.value}"
        )


def _assert_ticket_dispatch_pending(ticket: ExecutionTicket) -> None:
    if ticket.status is not ExecutionTicketStatus.DISPATCH_PENDING:
        raise ValueError(
            f"Cannot create execution request for ticket {ticket.ticket_id} "
            f"in status {ticket.status.value}"
        )


def _assert_ticket_safety_flags(ticket: ExecutionTicket) -> None:
    if ticket.execution_dispatched:
        raise ValueError(
            f"Cannot create execution request for ticket {ticket.ticket_id}: "
            "execution_dispatched must remain false."
        )
    if ticket.publish_dispatched:
        raise ValueError(
            f"Cannot create execution request for ticket {ticket.ticket_id}: "
            "publish_dispatched must remain false."
        )
    if ticket.repository2_touched:
        raise ValueError(
            f"Cannot create execution request for ticket {ticket.ticket_id}: "
            "repository2_touched must remain false."
        )


def _assert_plan_safety_flags(plan: ExecutionDispatchPlan) -> None:
    if plan.executed:
        raise ValueError(
            f"Cannot create execution request for plan {plan.plan_id}: "
            "executed must remain false."
        )
    if plan.repository2_touched:
        raise ValueError(
            f"Cannot create execution request for plan {plan.plan_id}: "
            "repository2_touched must remain false."
        )


def _is_publish_skill(skill_id: str) -> bool:
    definition = get_skill(skill_id)
    if definition is None:
        return False
    return definition.risk_category == RISK_CATEGORY_PUBLISH


def _assert_no_publish_skills_in_run_lists(
    dispatchable_skills: List[str],
    preview_only_skills: List[str],
) -> None:
    for skill_id in dispatchable_skills:
        if skill_id == "publish_content" or _is_publish_skill(skill_id):
            raise ValueError(
                f"Publish skill {skill_id!r} cannot appear in dispatchable skills."
            )
    for skill_id in preview_only_skills:
        if skill_id == "publish_content" or _is_publish_skill(skill_id):
            raise ValueError(
                f"Publish skill {skill_id!r} cannot appear in preview-only skills."
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


def create_execution_request_from_plan(
    plan: ExecutionDispatchPlan,
    ticket: ExecutionTicket,
    requested_by: str,
    reason: str = "",
) -> ExecutionRequest:
    """Create a dry-run execution request from a planned dispatch plan — no run."""
    _assert_plan_status_planned(plan)
    _assert_ticket_dispatch_pending(ticket)
    _assert_plan_ticket_alignment(plan, ticket)
    _assert_requester_matches(ticket, requested_by)
    _assert_ticket_safety_flags(ticket)
    _assert_plan_safety_flags(plan)

    dispatchable_skills = list(plan.dispatchable_skills)
    preview_only_skills = list(plan.preview_only_skills)
    excluded_skills = list(plan.excluded_skills)

    _assert_no_publish_skills_in_run_lists(dispatchable_skills, preview_only_skills)

    return ExecutionRequest(
        request_id=str(uuid.uuid4()),
        plan_id=plan.plan_id,
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        requested_by=requested_by,
        requested_at=_utc_now_iso(),
        reason=reason,
        mode=ExecutionRunMode.DRY_RUN,
        dispatchable_skills=dispatchable_skills,
        preview_only_skills=preview_only_skills,
        excluded_skills=excluded_skills,
        auto_apply=False,
        review_required=True,
    )


def _aggregate_run_summary(
    dispatchable_results: List[Dict[str, Any]],
    preview_results: List[Dict[str, Any]],
    blocked_skills: List[str],
) -> str:
    summaries: List[str] = []
    for result in dispatchable_results + preview_results:
        summary = str(result.get("summary", "")).strip()
        if summary:
            summaries.append(summary)
    if blocked_skills:
        summaries.append(f"{len(blocked_skills)} skill(s) blocked")
    return "; ".join(summaries) if summaries else "Dry-run completed (no skills)"


def start_dry_run(
    request: ExecutionRequest,
    ticket: ExecutionTicket,
    plan: ExecutionDispatchPlan,
    run_store: Optional[ExecutionRunStore] = None,
    request_store: Optional[ExecutionRequestStore] = None,
) -> ExecutionRun:
    """Start an adapter-backed dry-run — dry_run() only, no dispatch, no R2 mutation."""
    if request.mode is not ExecutionRunMode.DRY_RUN:
        raise ValueError(
            f"Cannot start dry run for request {request.request_id} "
            f"with mode {request.mode.value}"
        )

    _assert_plan_status_planned(plan)
    _assert_ticket_dispatch_pending(ticket)
    _assert_plan_ticket_alignment(plan, ticket)
    _assert_ticket_safety_flags(ticket)
    _assert_plan_safety_flags(plan)

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

    _assert_no_publish_skills_in_run_lists(
        list(request.dispatchable_skills),
        list(request.preview_only_skills),
    )

    requests = request_store or get_default_execution_request_store()
    requests.save(request)

    store = run_store or get_default_execution_run_store()
    existing = store.get_by_request(request.request_id)
    if existing is not None:
        return existing

    started_at = _utc_now_iso()
    dispatchable_results = [
        dry_run_skill(
            skill_id,
            run_date=plan.run_date,
            ticket_id=ticket.ticket_id,
        )
        for skill_id in request.dispatchable_skills
    ]
    preview_results = [
        to_preview_result(
            dry_run_skill(
                skill_id,
                run_date=plan.run_date,
                ticket_id=ticket.ticket_id,
            )
        )
        for skill_id in request.preview_only_skills
    ]
    blocked_skills = list(request.excluded_skills)
    finished_at = _utc_now_iso()

    summary = _aggregate_run_summary(
        dispatchable_results,
        preview_results,
        blocked_skills,
    )

    run = ExecutionRun(
        run_id=str(uuid.uuid4()),
        request_id=request.request_id,
        plan_id=plan.plan_id,
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        status=ExecutionRunStatus.COMPLETED,
        mode=ExecutionRunMode.DRY_RUN,
        dry_run=True,
        run_date=plan.run_date,
        started_at=started_at,
        finished_at=finished_at,
        dispatchable_results=dispatchable_results,
        preview_results=preview_results,
        blocked_skills=blocked_skills,
        summary=summary,
        repository2_touched=False,
        notes=["Phase 8E adapter dry-run — no subprocess"],
    )
    store.save(run)
    return run
