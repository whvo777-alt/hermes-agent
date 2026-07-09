"""Dispatch unlock runtime — token foundation (Phase 10B).

Record-only layer between approved execute gates and future adapter dispatch.
No Repository 2 execution, no publish, no subprocess, no terminal, and no
adapter dispatch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from agent.coo.execution_dispatcher import DispatchPlanStatus, ExecutionDispatchPlan
from agent.coo.execution_execute import (
    ExecuteGate,
    ExecuteGateStatus,
    ExecuteRequest,
)
from agent.coo.execution_runtime import (
    ExecutionRequest,
    ExecutionRun,
    ExecutionRunMode,
    ExecutionRunStatus,
)
from agent.coo.execution_ticket import ExecutionTicket, ExecutionTicketStatus
from agent.coo.skills_catalog import RISK_CATEGORY_PUBLISH, get_skill

_DEFAULT_TOKEN_STORE: Optional["DispatchUnlockTokenStore"] = None
_DEFAULT_DISPATCH_REQUEST_STORE: Optional["DispatchExecutionRequestStore"] = None
_DEFAULT_DISPATCH_RUN_STORE: Optional["DispatchExecutionRunStore"] = None


class DispatchExecutionMode(str, Enum):
    """Dispatch execution mode — distinct from dry-run ExecutionRunMode."""

    EXECUTE = "execute"


class DispatchExecutionRunStatus(str, Enum):
    """Lifecycle status for a dispatch execution run."""

    QUEUED = "queued"
    RUNNING = "running"  # future reserved — no RUNNING path in Phase 10B
    COMPLETED = "completed"  # future reserved — no adapter dispatch in Phase 10B
    FAILED = "failed"  # future reserved — no adapter dispatch in Phase 10B


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at_iso(minted_at: str, ttl_seconds: int) -> str:
    minted = datetime.fromisoformat(minted_at)
    if minted.tzinfo is None:
        minted = minted.replace(tzinfo=timezone.utc)
    return (minted + timedelta(seconds=ttl_seconds)).isoformat()


def is_dispatch_unlock_token_expired(token: DispatchUnlockToken) -> bool:
    """Return whether a dispatch unlock token has passed its expiry time."""
    expires = datetime.fromisoformat(token.expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires


@dataclass
class DispatchUnlockToken:
    """Scoped one-time unlock credential for a future dispatch attempt."""

    token_id: str
    ticket_id: str
    plan_id: str
    execute_request_id: str
    gate_id: str
    dry_run_run_id: str
    target_skills: tuple[str, ...]
    requested_by: str
    approved_by: str
    minted_at: str
    expires_at: str
    consumed: bool = False
    dispatch_generation: int = 0
    superseded: bool = False
    superseded_by: str = ""
    superseded_at: str = ""
    invalid_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token_id": self.token_id,
            "ticket_id": self.ticket_id,
            "plan_id": self.plan_id,
            "execute_request_id": self.execute_request_id,
            "gate_id": self.gate_id,
            "dry_run_run_id": self.dry_run_run_id,
            "target_skills": list(self.target_skills),
            "requested_by": self.requested_by,
            "approved_by": self.approved_by,
            "minted_at": self.minted_at,
            "expires_at": self.expires_at,
            "consumed": self.consumed,
            "dispatch_generation": self.dispatch_generation,
            "superseded": self.superseded,
            "superseded_by": self.superseded_by,
            "superseded_at": self.superseded_at,
            "invalid_reason": self.invalid_reason,
        }


@dataclass(frozen=True)
class DispatchExecutionRequest:
    """Immutable dispatch execution intent — distinct from execute readiness request."""

    dispatch_request_id: str
    execute_request_id: str
    gate_id: str
    ticket_id: str
    plan_id: str
    dry_run_run_id: str
    unlock_token_id: str
    target_skills: List[str]
    requested_by: str
    requested_at: str
    reason: str = ""
    mode: DispatchExecutionMode = DispatchExecutionMode.EXECUTE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_request_id": self.dispatch_request_id,
            "execute_request_id": self.execute_request_id,
            "gate_id": self.gate_id,
            "ticket_id": self.ticket_id,
            "plan_id": self.plan_id,
            "dry_run_run_id": self.dry_run_run_id,
            "unlock_token_id": self.unlock_token_id,
            "target_skills": list(self.target_skills),
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.reason,
            "mode": self.mode.value,
        }


@dataclass
class DispatchExecutionRun:
    """Dispatch execution run record — no adapter dispatch in Phase 10B."""

    dispatch_run_id: str
    dispatch_request_id: str
    ticket_id: str
    plan_id: str
    status: DispatchExecutionRunStatus
    dry_run: bool = False
    started_at: str = field(default_factory=_utc_now_iso)
    finished_at: str = ""
    skill_results: List[Dict[str, Any]] = field(default_factory=list)
    repository2_touched: bool = False
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_run_id": self.dispatch_run_id,
            "dispatch_request_id": self.dispatch_request_id,
            "ticket_id": self.ticket_id,
            "plan_id": self.plan_id,
            "status": self.status.value,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "skill_results": [dict(item) for item in self.skill_results],
            "repository2_touched": self.repository2_touched,
            "summary": self.summary,
        }


class DispatchUnlockTokenStore:
    """Process-local in-memory dispatch unlock token store — no file persistence."""

    def __init__(self) -> None:
        self._tokens: Dict[str, DispatchUnlockToken] = {}
        self._by_execute_request: Dict[str, str] = {}

    def save(self, token: DispatchUnlockToken) -> None:
        existing_token_id = self._by_execute_request.get(token.execute_request_id)
        if existing_token_id is not None and existing_token_id != token.token_id:
            existing = self._tokens.get(existing_token_id)
            if existing is not None:
                if existing.consumed:
                    raise ValueError(
                        f"Dispatch unlock token for execute request "
                        f"{token.execute_request_id} has already been consumed."
                    )
                if (
                    not existing.superseded
                    and not is_dispatch_unlock_token_expired(existing)
                ):
                    raise ValueError(
                        f"Dispatch unlock token already exists for execute request "
                        f"{token.execute_request_id}: {existing_token_id}"
                    )
        self._tokens[token.token_id] = token
        if not token.superseded:
            self._by_execute_request[token.execute_request_id] = token.token_id

    def get(self, token_id: str) -> Optional[DispatchUnlockToken]:
        return self._tokens.get(token_id)

    def get_by_execute_request(self, execute_request_id: str) -> Optional[DispatchUnlockToken]:
        token_id = self._by_execute_request.get(execute_request_id)
        if token_id is None:
            return None
        token = self._tokens.get(token_id)
        if token is None or token.superseded:
            return None
        return token

    def list_by_execute_request(self, execute_request_id: str) -> List[DispatchUnlockToken]:
        tokens = [
            token
            for token in self._tokens.values()
            if token.execute_request_id == execute_request_id
        ]
        return sorted(tokens, key=lambda item: item.minted_at)

    def list_tokens(self) -> List[DispatchUnlockToken]:
        return list(self._tokens.values())

    def clear(self) -> None:
        self._tokens.clear()
        self._by_execute_request.clear()


class DispatchExecutionRequestStore:
    """Process-local in-memory dispatch execution request store."""

    def __init__(self) -> None:
        self._requests: Dict[str, DispatchExecutionRequest] = {}
        self._by_execute_request: Dict[str, str] = {}

    def save(self, request: DispatchExecutionRequest) -> None:
        existing_request_id = self._by_execute_request.get(request.execute_request_id)
        if (
            existing_request_id is not None
            and existing_request_id != request.dispatch_request_id
        ):
            raise ValueError(
                f"Dispatch execution request already exists for execute request "
                f"{request.execute_request_id}: {existing_request_id}"
            )
        self._requests[request.dispatch_request_id] = request
        self._by_execute_request[request.execute_request_id] = request.dispatch_request_id

    def get(self, dispatch_request_id: str) -> Optional[DispatchExecutionRequest]:
        return self._requests.get(dispatch_request_id)

    def get_by_execute_request(
        self,
        execute_request_id: str,
    ) -> Optional[DispatchExecutionRequest]:
        request_id = self._by_execute_request.get(execute_request_id)
        if request_id is None:
            return None
        return self._requests.get(request_id)

    def list_requests(self) -> List[DispatchExecutionRequest]:
        return list(self._requests.values())

    def clear(self) -> None:
        self._requests.clear()
        self._by_execute_request.clear()


class DispatchExecutionRunStore:
    """Process-local in-memory dispatch execution run store."""

    def __init__(self) -> None:
        self._runs: Dict[str, DispatchExecutionRun] = {}
        self._by_request: Dict[str, str] = {}

    def save(self, run: DispatchExecutionRun) -> None:
        existing_run_id = self._by_request.get(run.dispatch_request_id)
        if existing_run_id is not None and existing_run_id != run.dispatch_run_id:
            raise ValueError(
                f"Dispatch execution run already exists for request "
                f"{run.dispatch_request_id}: {existing_run_id}"
            )
        self._runs[run.dispatch_run_id] = run
        self._by_request[run.dispatch_request_id] = run.dispatch_run_id

    def get(self, dispatch_run_id: str) -> Optional[DispatchExecutionRun]:
        return self._runs.get(dispatch_run_id)

    def get_by_request(self, dispatch_request_id: str) -> Optional[DispatchExecutionRun]:
        run_id = self._by_request.get(dispatch_request_id)
        if run_id is None:
            return None
        return self._runs.get(run_id)

    def list_runs(self) -> List[DispatchExecutionRun]:
        return list(self._runs.values())

    def clear(self) -> None:
        self._runs.clear()
        self._by_request.clear()


def get_default_dispatch_unlock_token_store() -> DispatchUnlockTokenStore:
    """Return the module-level in-memory dispatch unlock token store."""
    global _DEFAULT_TOKEN_STORE
    if _DEFAULT_TOKEN_STORE is None:
        _DEFAULT_TOKEN_STORE = DispatchUnlockTokenStore()
    return _DEFAULT_TOKEN_STORE


def get_default_dispatch_execution_request_store() -> DispatchExecutionRequestStore:
    """Return the module-level in-memory dispatch execution request store."""
    global _DEFAULT_DISPATCH_REQUEST_STORE
    if _DEFAULT_DISPATCH_REQUEST_STORE is None:
        _DEFAULT_DISPATCH_REQUEST_STORE = DispatchExecutionRequestStore()
    return _DEFAULT_DISPATCH_REQUEST_STORE


def get_default_dispatch_execution_run_store() -> DispatchExecutionRunStore:
    """Return the module-level in-memory dispatch execution run store."""
    global _DEFAULT_DISPATCH_RUN_STORE
    if _DEFAULT_DISPATCH_RUN_STORE is None:
        _DEFAULT_DISPATCH_RUN_STORE = DispatchExecutionRunStore()
    return _DEFAULT_DISPATCH_RUN_STORE


def _is_publish_skill(skill_id: str) -> bool:
    definition = get_skill(skill_id)
    if definition is None:
        return False
    return definition.risk_category == RISK_CATEGORY_PUBLISH


def _is_read_only_skill(skill_id: str) -> bool:
    definition = get_skill(skill_id)
    if definition is None:
        return False
    return bool(definition.read_only)


def _assert_token_usable(token: DispatchUnlockToken) -> None:
    if token.superseded:
        raise ValueError(
            f"Dispatch unlock token {token.token_id} has been superseded."
        )
    if token.consumed:
        raise ValueError(
            f"Dispatch unlock token {token.token_id} has already been consumed."
        )
    if is_dispatch_unlock_token_expired(token):
        raise ValueError(f"Dispatch unlock token {token.token_id} has expired.")


def assert_dispatch_generation_matches(
    token: DispatchUnlockToken,
    ticket: ExecutionTicket,
) -> None:
    """Raise when a token's generation snapshot does not match the ticket."""
    if token.dispatch_generation != ticket.dispatch_generation:
        raise ValueError(
            f"Dispatch unlock token generation {token.dispatch_generation} "
            f"does not match ticket dispatch generation {ticket.dispatch_generation}."
        )


def _supersede_token_for_remint(
    store: DispatchUnlockTokenStore,
    token: DispatchUnlockToken,
    new_token_id: str,
) -> None:
    token.superseded = True
    token.superseded_by = new_token_id
    token.superseded_at = _utc_now_iso()
    token.invalid_reason = "expired_remint"
    store.save(token)


def _assert_ticket_dispatch_pending(ticket: ExecutionTicket) -> None:
    if ticket.status is not ExecutionTicketStatus.DISPATCH_PENDING:
        raise ValueError(
            f"Cannot mint dispatch unlock token for ticket {ticket.ticket_id} "
            f"in status {ticket.status.value}"
        )


def _assert_ticket_safety_flags(ticket: ExecutionTicket) -> None:
    if ticket.execution_dispatched:
        raise ValueError(
            f"Cannot mint dispatch unlock token for ticket {ticket.ticket_id}: "
            "execution_dispatched must remain false."
        )
    if ticket.publish_dispatched:
        raise ValueError(
            f"Cannot mint dispatch unlock token for ticket {ticket.ticket_id}: "
            "publish_dispatched must remain false."
        )
    if ticket.repository2_touched:
        raise ValueError(
            f"Cannot mint dispatch unlock token for ticket {ticket.ticket_id}: "
            "repository2_touched must remain false."
        )


def _assert_plan_status_planned(plan: ExecutionDispatchPlan) -> None:
    if plan.status is not DispatchPlanStatus.PLANNED:
        raise ValueError(
            f"Cannot mint dispatch unlock token for plan {plan.plan_id} "
            f"in status {plan.status.value}"
        )


def _assert_plan_safety_flags(plan: ExecutionDispatchPlan) -> None:
    if plan.executed:
        raise ValueError(
            f"Cannot mint dispatch unlock token for plan {plan.plan_id}: "
            "executed must remain false."
        )
    if plan.repository2_touched:
        raise ValueError(
            f"Cannot mint dispatch unlock token for plan {plan.plan_id}: "
            "repository2_touched must remain false."
        )


def _assert_run_completed_dry_run(run: ExecutionRun) -> None:
    if run.status is not ExecutionRunStatus.COMPLETED:
        raise ValueError(
            f"Cannot mint dispatch unlock token for run {run.run_id} "
            f"in status {run.status.value}"
        )
    if not run.dry_run:
        raise ValueError(
            f"Cannot mint dispatch unlock token for run {run.run_id}: "
            "dry_run must remain true."
        )
    if run.repository2_touched:
        raise ValueError(
            f"Cannot mint dispatch unlock token for run {run.run_id}: "
            "repository2_touched must remain false."
        )


def _assert_request_is_dry_run(request: ExecutionRequest) -> None:
    if request.mode is not ExecutionRunMode.DRY_RUN:
        raise ValueError(
            f"Cannot mint dispatch unlock token from request {request.request_id} "
            f"with mode {request.mode.value}"
        )


def _assert_run_request_alignment(run: ExecutionRun, request: ExecutionRequest) -> None:
    if run.request_id != request.request_id:
        raise ValueError(
            f"Run {run.run_id} request_id {run.request_id!r} does not match "
            f"request {request.request_id!r}"
        )


def _assert_requester_matches(ticket: ExecutionTicket, requested_by: str) -> None:
    if requested_by != ticket.requester_id:
        raise ValueError(
            f"Requester {requested_by!r} is not authorized for ticket {ticket.ticket_id} "
            f"(owner: {ticket.requester_id!r})"
        )


def _assert_gate_approved_for_unlock(gate: ExecuteGate, execute_request: ExecuteRequest) -> None:
    if gate.status is not ExecuteGateStatus.APPROVED:
        raise ValueError(
            f"Cannot mint dispatch unlock token for gate {gate.gate_id} "
            f"in status {gate.status.value}"
        )
    if gate.execute_request_id != execute_request.execute_request_id:
        raise ValueError(
            f"Gate {gate.gate_id} execute_request_id does not match execute request "
            f"{execute_request.execute_request_id}"
        )


def _assert_unlock_alignment(
    ticket: ExecutionTicket,
    plan: ExecutionDispatchPlan,
    dry_run: ExecutionRun,
    dry_run_request: ExecutionRequest,
    execute_request: ExecuteRequest,
    gate: ExecuteGate,
) -> None:
    if execute_request.dry_run_run_id != dry_run.run_id:
        raise ValueError(
            f"Execute request dry_run_run_id {execute_request.dry_run_run_id!r} "
            f"does not match run {dry_run.run_id!r}"
        )
    if execute_request.ticket_id != ticket.ticket_id:
        raise ValueError(
            f"Execute request ticket_id {execute_request.ticket_id!r} does not match "
            f"ticket {ticket.ticket_id!r}"
        )
    if execute_request.plan_id != plan.plan_id:
        raise ValueError(
            f"Execute request plan_id {execute_request.plan_id!r} does not match "
            f"plan {plan.plan_id!r}"
        )
    if gate.dry_run_run_id != dry_run.run_id:
        raise ValueError(
            f"Gate dry_run_run_id {gate.dry_run_run_id!r} does not match run {dry_run.run_id!r}"
        )
    if dry_run.plan_id != plan.plan_id:
        raise ValueError(
            f"Run {dry_run.run_id} plan_id {dry_run.plan_id!r} does not match plan {plan.plan_id!r}"
        )
    if dry_run.ticket_id != ticket.ticket_id:
        raise ValueError(
            f"Run {dry_run.run_id} ticket_id {dry_run.ticket_id!r} does not match "
            f"ticket {ticket.ticket_id!r}"
        )
    if plan.ticket_id != ticket.ticket_id:
        raise ValueError(
            f"Plan {plan.plan_id} ticket_id {plan.ticket_id!r} does not match "
            f"ticket {ticket.ticket_id!r}"
        )


def _assert_target_skills_for_unlock(
    target_skills: List[str],
    plan: ExecutionDispatchPlan,
    dry_run: ExecutionRun,
) -> None:
    if not target_skills:
        raise ValueError("Cannot mint dispatch unlock token with empty target_skills.")

    dispatchable_set = set(plan.dispatchable_skills)
    preview_set = set(plan.preview_only_skills)
    excluded_set = set(plan.excluded_skills)

    planned_status_by_skill: Dict[str, str] = {}
    for result in dry_run.dispatchable_results:
        skill_id = str(result.get("skill_id") or "")
        if skill_id:
            planned_status_by_skill[skill_id] = str(result.get("status") or "")

    for skill_id in target_skills:
        if skill_id not in dispatchable_set:
            raise ValueError(
                f"Target skill {skill_id!r} is not in plan dispatchable_skills."
            )
        if skill_id in preview_set:
            raise ValueError(
                f"Target skill {skill_id!r} is in plan preview_only_skills."
            )
        if skill_id in excluded_set:
            raise ValueError(f"Target skill {skill_id!r} is in plan excluded_skills.")
        if skill_id == "publish_content" or _is_publish_skill(skill_id):
            raise ValueError(f"Publish skill {skill_id!r} cannot be a dispatch target.")
        if _is_read_only_skill(skill_id):
            raise ValueError(f"Read-only skill {skill_id!r} cannot be a dispatch target.")
        if planned_status_by_skill.get(skill_id) != "planned":
            raise ValueError(
                f"Dry-run result for target skill {skill_id!r} is not status 'planned'."
            )


def create_dispatch_unlock_token(
    ticket: ExecutionTicket,
    plan: ExecutionDispatchPlan,
    dry_run: ExecutionRun,
    dry_run_request: ExecutionRequest,
    execute_request: ExecuteRequest,
    gate: ExecuteGate,
    *,
    requested_by: str,
    token_store: Optional[DispatchUnlockTokenStore] = None,
    ttl_seconds: int = 300,
    dispatch_generation: Optional[int] = None,
) -> DispatchUnlockToken:
    """Mint a scoped dispatch unlock token after gate approval — no dispatch."""
    store = token_store or get_default_dispatch_unlock_token_store()
    effective_generation = (
        ticket.dispatch_generation if dispatch_generation is None else dispatch_generation
    )
    existing = store.get_by_execute_request(execute_request.execute_request_id)
    remint_token_id: Optional[str] = None
    token_to_supersede: Optional[DispatchUnlockToken] = None
    if existing is not None:
        if existing.consumed:
            raise ValueError(
                f"Dispatch unlock token for execute request "
                f"{execute_request.execute_request_id} has already been consumed."
            )
        if not is_dispatch_unlock_token_expired(existing):
            return existing
        remint_token_id = str(uuid.uuid4())
        token_to_supersede = existing

    _assert_ticket_dispatch_pending(ticket)
    _assert_ticket_safety_flags(ticket)
    _assert_plan_status_planned(plan)
    _assert_plan_safety_flags(plan)
    _assert_run_completed_dry_run(dry_run)
    _assert_request_is_dry_run(dry_run_request)
    _assert_run_request_alignment(dry_run, dry_run_request)
    _assert_requester_matches(ticket, requested_by)
    _assert_gate_approved_for_unlock(gate, execute_request)
    _assert_unlock_alignment(ticket, plan, dry_run, dry_run_request, execute_request, gate)

    if gate.decided_by != ticket.requester_id:
        raise ValueError(
            f"Gate {gate.gate_id} decided_by {gate.decided_by!r} does not match ticket owner "
            f"{ticket.requester_id!r}"
        )

    target_skills = list(execute_request.target_skills)
    _assert_target_skills_for_unlock(target_skills, plan, dry_run)

    # All validation passed — only now is it safe to mark the prior token
    # superseded. Superseding earlier would leave a dangling
    # superseded_by reference (and a permanently unusable old token) if any
    # assertion above raised after the supersede but before the new token
    # was minted and saved.
    if token_to_supersede is not None:
        _supersede_token_for_remint(store, token_to_supersede, remint_token_id)

    minted_at = _utc_now_iso()
    token = DispatchUnlockToken(
        token_id=remint_token_id or str(uuid.uuid4()),
        ticket_id=ticket.ticket_id,
        plan_id=plan.plan_id,
        execute_request_id=execute_request.execute_request_id,
        gate_id=gate.gate_id,
        dry_run_run_id=dry_run.run_id,
        target_skills=tuple(target_skills),
        requested_by=requested_by,
        approved_by=gate.decided_by,
        minted_at=minted_at,
        expires_at=_expires_at_iso(minted_at, ttl_seconds),
        consumed=False,
        dispatch_generation=effective_generation,
    )
    store.save(token)
    return token


def create_dispatch_execution_request(
    token: DispatchUnlockToken,
    *,
    reason: str = "",
    request_store: Optional[DispatchExecutionRequestStore] = None,
) -> DispatchExecutionRequest:
    """Create a dispatch execution request from an unlock token — no dispatch."""
    _assert_token_usable(token)

    store = request_store or get_default_dispatch_execution_request_store()
    existing = store.get_by_execute_request(token.execute_request_id)
    if existing is not None:
        return existing

    dispatch_request = DispatchExecutionRequest(
        dispatch_request_id=str(uuid.uuid4()),
        execute_request_id=token.execute_request_id,
        gate_id=token.gate_id,
        ticket_id=token.ticket_id,
        plan_id=token.plan_id,
        dry_run_run_id=token.dry_run_run_id,
        unlock_token_id=token.token_id,
        target_skills=list(token.target_skills),
        requested_by=token.requested_by,
        requested_at=_utc_now_iso(),
        reason=reason,
        mode=DispatchExecutionMode.EXECUTE,
    )
    store.save(dispatch_request)
    return dispatch_request


def consume_dispatch_unlock_token(
    token_id: str,
    *,
    token_store: Optional[DispatchUnlockTokenStore] = None,
) -> DispatchUnlockToken:
    """Mark a dispatch unlock token consumed — no dispatch side effects in Phase 10B."""
    store = token_store or get_default_dispatch_unlock_token_store()
    token = store.get(token_id)
    if token is None:
        raise KeyError(f"Dispatch unlock token not found: {token_id}")

    _assert_token_usable(token)
    token.consumed = True
    store.save(token)
    return token
