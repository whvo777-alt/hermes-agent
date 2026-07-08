"""Gateway execute bridge — Phase 9C readiness request and gate integration.

Thin Gateway-facing adapter for creating and managing ExecuteRequest /
ExecuteGate records from completed dry-runs. No Repository 2 execution, no
publish, no subprocess, no terminal, and no adapter dispatch.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.coo.execution_dispatcher import (
    ExecutionDispatchPlanStore,
    get_default_dispatch_plan_store,
)
from agent.coo.execution_execute import (
    ExecuteGateStore,
    ExecuteRequestStore,
    approve_execute_gate,
    create_execute_gate,
    create_execute_request_from_dry_run,
    get_default_execute_gate_store,
    get_default_execute_request_store,
    reject_execute_gate,
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
from agent.coo.execution_ticket import ExecutionTicketStore, get_default_ticket_store


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


def create_execute_request_for_gateway_ticket(
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
) -> Dict[str, Any]:
    """Create an execute readiness request and gate from the latest completed dry-run."""
    tickets = ticket_store or get_default_ticket_store()
    plans = plan_store or get_default_dispatch_plan_store()
    runs = run_store or get_default_execution_run_store()
    dry_run_requests = dry_run_request_store or get_default_execution_request_store()
    execute_requests = execute_request_store or get_default_execute_request_store()
    gates = gate_store or get_default_execute_gate_store()

    ticket = tickets.get(ticket_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found: {ticket_id}")

    _assert_gateway_requester_authorized(ticket.requester_id, requester_id)

    plan = plans.get_by_ticket(ticket_id)
    if plan is None:
        raise KeyError(f"Dispatch plan not found for ticket: {ticket_id}")

    run = _select_latest_completed_dry_run(ticket_id, runs)
    dry_run_request = _lookup_dry_run_request(run, dry_run_requests)

    execute_request = create_execute_request_from_dry_run(
        run,
        dry_run_request,
        plan,
        ticket,
        requested_by=requester_id,
        reason=reason,
        request_store=execute_requests,
    )

    if not execute_request.target_skills:
        raise ValueError(
            f"No dispatchable target skills for execute gate on ticket {ticket_id}"
        )

    gate = create_execute_gate(execute_request, gate_store=gates)
    return {
        "execute_request": execute_request.to_dict(),
        "gate": gate.to_dict(),
    }


def create_execute_request_for_gateway_session(
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
) -> Dict[str, Any]:
    """Create an execute readiness request and gate via approval session lookup."""
    tickets = ticket_store or get_default_ticket_store()
    ticket = tickets.get_by_session(session_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found for session: {session_id}")

    return create_execute_request_for_gateway_ticket(
        ticket.ticket_id,
        requester_id=requester_id,
        reason=reason,
        ticket_store=tickets,
        plan_store=plan_store,
        run_store=run_store,
        dry_run_request_store=dry_run_request_store,
        execute_request_store=execute_request_store,
        gate_store=gate_store,
    )


def get_latest_execute_request_for_gateway_ticket(
    ticket_id: str,
    *,
    execute_request_store: Optional[ExecuteRequestStore] = None,
    gate_store: Optional[ExecuteGateStore] = None,
) -> Optional[Dict[str, Any]]:
    """Return the latest execute request for a ticket, with gate if present."""
    execute_requests = execute_request_store or get_default_execute_request_store()
    gates = gate_store or get_default_execute_gate_store()

    matching = [
        request
        for request in execute_requests.list_requests()
        if request.ticket_id == ticket_id
    ]
    if not matching:
        return None

    latest = max(matching, key=lambda request: request.requested_at)
    gate = gates.get_by_request(latest.execute_request_id)
    return {
        "execute_request": latest.to_dict(),
        "gate": gate.to_dict() if gate is not None else None,
    }


def approve_execute_gate_for_gateway_request(
    execute_request_id: str,
    *,
    reviewer_id: str,
    gate_store: Optional[ExecuteGateStore] = None,
    ticket_store: Optional[ExecutionTicketStore] = None,
) -> Dict[str, Any]:
    """Approve a pending execute gate — record-only, no dispatch."""
    gates = gate_store or get_default_execute_gate_store()
    tickets = ticket_store or get_default_ticket_store()
    gate = gates.get_by_request(execute_request_id)
    if gate is None:
        raise KeyError(f"Execute gate not found for request: {execute_request_id}")

    ticket = tickets.get(gate.ticket_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found: {gate.ticket_id}")

    _assert_gateway_requester_authorized(ticket.requester_id, reviewer_id)

    approved = approve_execute_gate(gate.gate_id, reviewer_id, gate_store=gates)
    return {"gate": approved.to_dict()}


def reject_execute_gate_for_gateway_request(
    execute_request_id: str,
    *,
    reviewer_id: str,
    reason: str = "",
    gate_store: Optional[ExecuteGateStore] = None,
    ticket_store: Optional[ExecutionTicketStore] = None,
) -> Dict[str, Any]:
    """Reject a pending execute gate — record-only, no dispatch."""
    gates = gate_store or get_default_execute_gate_store()
    tickets = ticket_store or get_default_ticket_store()
    gate = gates.get_by_request(execute_request_id)
    if gate is None:
        raise KeyError(f"Execute gate not found for request: {execute_request_id}")

    ticket = tickets.get(gate.ticket_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found: {gate.ticket_id}")

    _assert_gateway_requester_authorized(ticket.requester_id, reviewer_id)

    rejected = reject_execute_gate(
        gate.gate_id,
        reviewer_id,
        reason=reason,
        gate_store=gates,
    )
    return {"gate": rejected.to_dict()}
