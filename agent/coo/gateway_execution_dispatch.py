"""Gateway dispatch bridge — Phase 10H token/request prepare and runner integration.

Thin Gateway-facing adapter for preparing dispatch unlock tokens and invoking
the token-gated dispatch runner. No Repository 2 execution, no publish, no
subprocess, no terminal allow-path, and no real executor in production defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionRequestStore,
    DispatchExecutionRun,
    DispatchExecutionRunStatus,
    DispatchExecutionRunStore,
    DispatchUnlockToken,
    DispatchUnlockTokenStore,
    create_dispatch_execution_request,
    create_dispatch_unlock_token,
    get_default_dispatch_execution_request_store,
    get_default_dispatch_execution_run_store,
    get_default_dispatch_unlock_token_store,
    is_dispatch_unlock_token_expired,
)
from agent.coo.execution_dispatch_runner import run_approved_dispatch
from agent.coo.execution_dispatcher import (
    ExecutionDispatchPlan,
    ExecutionDispatchPlanStore,
    get_default_dispatch_plan_store,
)
from agent.coo.execution_execute import (
    ExecuteGate,
    ExecuteGateStatus,
    ExecuteGateStore,
    ExecuteRequest,
    ExecuteRequestStore,
    get_default_execute_gate_store,
    get_default_execute_request_store,
)
from agent.coo.execution_runtime import (
    ExecutionRequest,
    ExecutionRequestStore,
    ExecutionRun,
    ExecutionRunStatus,
    ExecutionRunStore,
    get_default_execution_request_store,
    get_default_execution_run_store,
)
from agent.coo.execution_ticket import (
    ExecutionTicket,
    ExecutionTicketStatus,
    ExecutionTicketStore,
    get_default_ticket_store,
)
from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig


@dataclass(frozen=True)
class _ApprovedDispatchContext:
    ticket: ExecutionTicket
    plan: ExecutionDispatchPlan
    dry_run: ExecutionRun
    dry_run_request: ExecutionRequest
    execute_request: ExecuteRequest
    gate: ExecuteGate


def _assert_gateway_requester_authorized(ticket_requester_id: str, requester_id: str) -> None:
    if requester_id != ticket_requester_id:
        raise ValueError(
            f"Requester {requester_id!r} is not authorized for ticket "
            f"(owner: {ticket_requester_id!r})"
        )


def _select_latest_completed_dry_run(
    ticket_id: str,
    run_store: ExecutionRunStore,
) -> ExecutionRun:
    """Return the latest completed dry-run for a ticket."""
    matching = [
        run
        for run in run_store.list_runs()
        if run.ticket_id == ticket_id
        and run.status is ExecutionRunStatus.COMPLETED
        and run.dry_run is True
        and not run.repository2_touched
    ]
    if not matching:
        raise KeyError(f"No completed dry-run found for ticket: {ticket_id}")
    return max(matching, key=lambda run: run.started_at)


def _lookup_dry_run_request(
    run: ExecutionRun,
    request_store: ExecutionRequestStore,
) -> ExecutionRequest:
    request = request_store.get(run.request_id)
    if request is None:
        raise KeyError(
            f"Dry-run request not found for run {run.run_id}: {run.request_id}"
        )
    return request


def _resolve_approved_dispatch_context(
    ticket_id: str,
    *,
    ticket_store: ExecutionTicketStore,
    plan_store: ExecutionDispatchPlanStore,
    run_store: ExecutionRunStore,
    dry_run_request_store: ExecutionRequestStore,
    execute_request_store: ExecuteRequestStore,
    gate_store: ExecuteGateStore,
) -> _ApprovedDispatchContext:
    ticket = ticket_store.get(ticket_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found: {ticket_id}")

    plan = plan_store.get_by_ticket(ticket_id)
    if plan is None:
        raise KeyError(f"Dispatch plan not found for ticket: {ticket_id}")

    dry_run = _select_latest_completed_dry_run(ticket_id, run_store)
    dry_run_request = _lookup_dry_run_request(dry_run, dry_run_request_store)

    execute_request = execute_request_store.get_by_dry_run(dry_run.run_id)
    if execute_request is None:
        raise KeyError(
            f"Execute request not found for dry-run run {dry_run.run_id}"
        )

    if execute_request.dry_run_run_id != dry_run.run_id:
        raise ValueError(
            f"Execute request dry_run_run_id {execute_request.dry_run_run_id!r} "
            f"does not match latest dry-run {dry_run.run_id!r}"
        )

    gate = gate_store.get_by_request(execute_request.execute_request_id)
    if gate is None:
        raise KeyError(
            f"Execute gate not found for request: {execute_request.execute_request_id}"
        )

    if gate.status is not ExecuteGateStatus.APPROVED:
        raise ValueError(
            f"Cannot prepare dispatch for gate {gate.gate_id} "
            f"in status {gate.status.value}"
        )

    if gate.dry_run_run_id != dry_run.run_id:
        raise ValueError(
            f"Gate dry_run_run_id {gate.dry_run_run_id!r} does not match "
            f"latest dry-run {dry_run.run_id!r}"
        )

    if not execute_request.target_skills:
        raise ValueError(
            f"No dispatchable target skills for ticket {ticket_id}"
        )

    return _ApprovedDispatchContext(
        ticket=ticket,
        plan=plan,
        dry_run=dry_run,
        dry_run_request=dry_run_request,
        execute_request=execute_request,
        gate=gate,
    )


def _default_gateway_dispatch_adapter() -> PipelineAdapter:
    """Production gateway adapter — allow_execute without a real executor."""
    return PipelineAdapter(
        PipelineAdapterConfig(allow_execute=True, repository2_read_only=True),
        executor=None,
    )


def _token_is_usable(token: Optional[DispatchUnlockToken]) -> bool:
    if token is None:
        return False
    if token.superseded or token.consumed:
        return False
    return not is_dispatch_unlock_token_expired(token)


def _dispatch_request_aligned(
    dispatch_request,
    token: Optional[DispatchUnlockToken],
) -> bool:
    if dispatch_request is None or token is None:
        return False
    return dispatch_request.unlock_token_id == token.token_id


def _can_prepare_dispatch(context: Optional[_ApprovedDispatchContext]) -> bool:
    if context is None:
        return False
    ticket = context.ticket
    return (
        ticket.status is ExecutionTicketStatus.DISPATCH_PENDING
        and not ticket.execution_dispatched
        and not ticket.publish_dispatched
        and not ticket.repository2_touched
        and context.gate.status is ExecuteGateStatus.APPROVED
        and bool(context.execute_request.target_skills)
    )


def _can_run_dispatch(
    *,
    context: Optional[_ApprovedDispatchContext],
    token: Optional[DispatchUnlockToken],
    dispatch_request,
    dispatch_run: Optional[DispatchExecutionRun],
) -> bool:
    if not _can_prepare_dispatch(context):
        return False
    if not _token_is_usable(token):
        return False
    if dispatch_request is None:
        return False
    if not _dispatch_request_aligned(dispatch_request, token):
        return False
    if dispatch_run is not None:
        return False
    return True


def prepare_dispatch_for_gateway_ticket(
    ticket_id: str,
    *,
    requester_id: str,
    reason: str = "",
    ticket_store: Optional[ExecutionTicketStore] = None,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
    run_store: Optional[ExecutionRunStore] = None,
    dry_run_request_store: Optional[ExecutionRequestStore] = None,
    execute_request_store: Optional[ExecuteRequestStore] = None,
    gate_store: Optional[ExecuteGateStore] = None,
    token_store: Optional[DispatchUnlockTokenStore] = None,
    dispatch_request_store: Optional[DispatchExecutionRequestStore] = None,
) -> Dict[str, Any]:
    """Prepare dispatch unlock token and request — no execution."""
    tickets = ticket_store or get_default_ticket_store()
    plans = plan_store or get_default_dispatch_plan_store()
    runs = run_store or get_default_execution_run_store()
    dry_run_requests = dry_run_request_store or get_default_execution_request_store()
    execute_requests = execute_request_store or get_default_execute_request_store()
    gates = gate_store or get_default_execute_gate_store()
    tokens = token_store or get_default_dispatch_unlock_token_store()
    dispatch_requests = dispatch_request_store or get_default_dispatch_execution_request_store()

    context = _resolve_approved_dispatch_context(
        ticket_id,
        ticket_store=tickets,
        plan_store=plans,
        run_store=runs,
        dry_run_request_store=dry_run_requests,
        execute_request_store=execute_requests,
        gate_store=gates,
    )
    _assert_gateway_requester_authorized(context.ticket.requester_id, requester_id)

    token = create_dispatch_unlock_token(
        context.ticket,
        context.plan,
        context.dry_run,
        context.dry_run_request,
        context.execute_request,
        context.gate,
        requested_by=requester_id,
        token_store=tokens,
    )
    dispatch_request = create_dispatch_execution_request(
        token,
        reason=reason,
        request_store=dispatch_requests,
    )

    return {
        "unlock_token": token.to_dict(),
        "dispatch_request": dispatch_request.to_dict(),
        "execute_request_id": context.execute_request.execute_request_id,
        "gate_id": context.gate.gate_id,
    }


def prepare_dispatch_for_gateway_session(
    session_id: str,
    *,
    requester_id: str,
    reason: str = "",
    ticket_store: Optional[ExecutionTicketStore] = None,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
    run_store: Optional[ExecutionRunStore] = None,
    dry_run_request_store: Optional[ExecutionRequestStore] = None,
    execute_request_store: Optional[ExecuteRequestStore] = None,
    gate_store: Optional[ExecuteGateStore] = None,
    token_store: Optional[DispatchUnlockTokenStore] = None,
    dispatch_request_store: Optional[DispatchExecutionRequestStore] = None,
) -> Dict[str, Any]:
    """Prepare dispatch unlock token and request via approval session lookup."""
    tickets = ticket_store or get_default_ticket_store()
    ticket = tickets.get_by_session(session_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found for session: {session_id}")

    return prepare_dispatch_for_gateway_ticket(
        ticket.ticket_id,
        requester_id=requester_id,
        reason=reason,
        ticket_store=tickets,
        plan_store=plan_store,
        run_store=run_store,
        dry_run_request_store=dry_run_request_store,
        execute_request_store=execute_request_store,
        gate_store=gate_store,
        token_store=token_store,
        dispatch_request_store=dispatch_request_store,
    )


def run_dispatch_for_gateway_request(
    *,
    unlock_token_id: str | None = None,
    dispatch_request_id: str | None = None,
    requester_id: str,
    ticket_store: Optional[ExecutionTicketStore] = None,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
    gate_store: Optional[ExecuteGateStore] = None,
    token_store: Optional[DispatchUnlockTokenStore] = None,
    dispatch_request_store: Optional[DispatchExecutionRequestStore] = None,
    dispatch_run_store: Optional[DispatchExecutionRunStore] = None,
    adapter: Optional[PipelineAdapter] = None,
) -> Dict[str, Any]:
    """Run token-gated dispatch via the runner — default adapter has no executor."""
    if not unlock_token_id and not dispatch_request_id:
        raise ValueError(
            "Either unlock_token_id or dispatch_request_id is required for dispatch."
        )

    tickets = ticket_store or get_default_ticket_store()
    plans = plan_store or get_default_dispatch_plan_store()
    gates = gate_store or get_default_execute_gate_store()
    tokens = token_store or get_default_dispatch_unlock_token_store()
    dispatch_requests = dispatch_request_store or get_default_dispatch_execution_request_store()
    runs = dispatch_run_store or get_default_dispatch_execution_run_store()

    resolved_token_id = unlock_token_id
    if not resolved_token_id:
        assert dispatch_request_id is not None
        dispatch_request = dispatch_requests.get(dispatch_request_id)
        if dispatch_request is None:
            raise KeyError(
                f"Dispatch execution request not found: {dispatch_request_id}"
            )
        resolved_token_id = dispatch_request.unlock_token_id

    token = tokens.get(resolved_token_id)
    if token is None:
        raise KeyError(f"Dispatch unlock token not found: {resolved_token_id}")

    ticket = tickets.get(token.ticket_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found: {token.ticket_id}")

    plan = plans.get(token.plan_id)
    if plan is None:
        raise KeyError(f"Dispatch plan not found: {token.plan_id}")

    _assert_gateway_requester_authorized(ticket.requester_id, requester_id)

    pipeline = adapter if adapter is not None else _default_gateway_dispatch_adapter()

    run = run_approved_dispatch(
        resolved_token_id,
        requested_by=requester_id,
        ticket_store=tickets,
        plan_store=plans,
        gate_store=gates,
        token_store=tokens,
        dispatch_request_store=dispatch_requests,
        dispatch_run_store=runs,
        adapter=pipeline,
    )

    return {
        "dispatch_run": run.to_dict(),
        "ticket": ticket.to_dict(),
        "plan": plan.to_dict(),
    }


def get_latest_dispatch_for_gateway_ticket(
    ticket_id: str,
    *,
    requester_id: str | None = None,
    ticket_store: Optional[ExecutionTicketStore] = None,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
    run_store: Optional[ExecutionRunStore] = None,
    dry_run_request_store: Optional[ExecutionRequestStore] = None,
    execute_request_store: Optional[ExecuteRequestStore] = None,
    gate_store: Optional[ExecuteGateStore] = None,
    token_store: Optional[DispatchUnlockTokenStore] = None,
    dispatch_request_store: Optional[DispatchExecutionRequestStore] = None,
    dispatch_run_store: Optional[DispatchExecutionRunStore] = None,
) -> Optional[Dict[str, Any]]:
    """Return latest dispatch-related records for UI display — no execution."""
    tickets = ticket_store or get_default_ticket_store()
    plans = plan_store or get_default_dispatch_plan_store()
    runs = run_store or get_default_execution_run_store()
    dry_run_requests = dry_run_request_store or get_default_execution_request_store()
    execute_requests = execute_request_store or get_default_execute_request_store()
    gates = gate_store or get_default_execute_gate_store()
    tokens = token_store or get_default_dispatch_unlock_token_store()
    dispatch_requests = dispatch_request_store or get_default_dispatch_execution_request_store()
    dispatch_runs = dispatch_run_store or get_default_dispatch_execution_run_store()

    ticket = tickets.get(ticket_id)
    if ticket is None:
        return None

    if requester_id is not None:
        _assert_gateway_requester_authorized(ticket.requester_id, requester_id)

    context: Optional[_ApprovedDispatchContext]
    try:
        context = _resolve_approved_dispatch_context(
            ticket_id,
            ticket_store=tickets,
            plan_store=plans,
            run_store=runs,
            dry_run_request_store=dry_run_requests,
            execute_request_store=execute_requests,
            gate_store=gates,
        )
    except (KeyError, ValueError):
        context = None

    execute_request = context.execute_request if context is not None else None
    gate = context.gate if context is not None else None
    token = (
        tokens.get_by_execute_request(execute_request.execute_request_id)
        if execute_request is not None
        else None
    )
    dispatch_request = (
        dispatch_requests.get_by_execute_request(execute_request.execute_request_id)
        if execute_request is not None
        else None
    )
    dispatch_run = (
        dispatch_runs.get_by_request(dispatch_request.dispatch_request_id)
        if dispatch_request is not None
        else None
    )

    token_usable = _token_is_usable(token)
    can_prepare = _can_prepare_dispatch(context)
    can_run = _can_run_dispatch(
        context=context,
        token=token,
        dispatch_request=dispatch_request,
        dispatch_run=dispatch_run,
    )

    return {
        "execute_request": execute_request.to_dict() if execute_request else None,
        "gate": gate.to_dict() if gate else None,
        "unlock_token": token.to_dict() if token else None,
        "dispatch_request": dispatch_request.to_dict() if dispatch_request else None,
        "dispatch_run": dispatch_run.to_dict() if dispatch_run else None,
        "token_usable": token_usable,
        "can_prepare": can_prepare,
        "can_run": can_run,
    }


def maybe_remint_dispatch_token_for_gateway_ticket(
    ticket_id: str,
    *,
    requester_id: str,
    reason: str = "",
    ticket_store: Optional[ExecutionTicketStore] = None,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
    run_store: Optional[ExecutionRunStore] = None,
    dry_run_request_store: Optional[ExecutionRequestStore] = None,
    execute_request_store: Optional[ExecuteRequestStore] = None,
    gate_store: Optional[ExecuteGateStore] = None,
    token_store: Optional[DispatchUnlockTokenStore] = None,
) -> Dict[str, Any]:
    """Mint or remint a dispatch unlock token — no dispatch execution.

    Remint updates the active token only. Call prepare_dispatch afterward to mint
    a new active DispatchExecutionRequest aligned with the new token.
    """
    tickets = ticket_store or get_default_ticket_store()
    plans = plan_store or get_default_dispatch_plan_store()
    runs = run_store or get_default_execution_run_store()
    dry_run_requests = dry_run_request_store or get_default_execution_request_store()
    execute_requests = execute_request_store or get_default_execute_request_store()
    gates = gate_store or get_default_execute_gate_store()
    tokens = token_store or get_default_dispatch_unlock_token_store()

    context = _resolve_approved_dispatch_context(
        ticket_id,
        ticket_store=tickets,
        plan_store=plans,
        run_store=runs,
        dry_run_request_store=dry_run_requests,
        execute_request_store=execute_requests,
        gate_store=gates,
    )
    _assert_gateway_requester_authorized(context.ticket.requester_id, requester_id)

    previous = tokens.get_by_execute_request(context.execute_request.execute_request_id)
    reminted = (
        previous is not None
        and not previous.consumed
        and is_dispatch_unlock_token_expired(previous)
    )

    token = create_dispatch_unlock_token(
        context.ticket,
        context.plan,
        context.dry_run,
        context.dry_run_request,
        context.execute_request,
        context.gate,
        requested_by=requester_id,
        token_store=tokens,
    )

    return {
        "unlock_token": token.to_dict(),
        "execute_request_id": context.execute_request.execute_request_id,
        "gate_id": context.gate.gate_id,
        "reminted": reminted,
    }
