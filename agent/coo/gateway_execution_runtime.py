"""Gateway execution runtime bridge — Phase 8C dry-run integration.

Thin Gateway-facing adapter for explicitly starting synthetic execution
runtime dry-runs from existing dispatch plans. No Repository 2 execution, no
publish, no subprocess, no terminal, and no adapter dispatch.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.coo.execution_dispatcher import (
    ExecutionDispatchPlanStore,
    get_default_dispatch_plan_store,
)
from agent.coo.execution_runtime import (
    ExecutionRequestStore,
    ExecutionRunStore,
    create_execution_request_from_plan,
    get_default_execution_request_store,
    get_default_execution_run_store,
    start_dry_run,
)
from agent.coo.execution_ticket import ExecutionTicketStore, get_default_ticket_store


def _assert_gateway_requester_authorized(ticket_requester_id: str, requester_id: str) -> None:
    if requester_id != ticket_requester_id:
        raise ValueError(
            f"Requester {requester_id!r} is not authorized for ticket "
            f"(owner: {ticket_requester_id!r})"
        )


def start_dry_run_for_gateway_ticket(
    ticket_id: str,
    *,
    requester_id: str,
    reason: str = "",
    ticket_store: Optional[ExecutionTicketStore] = None,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
    run_store: Optional[ExecutionRunStore] = None,
    request_store: Optional[ExecutionRequestStore] = None,
) -> Dict[str, Any]:
    """Start an explicit synthetic dry-run for a ticket with an existing plan."""
    tickets = ticket_store or get_default_ticket_store()
    plans = plan_store or get_default_dispatch_plan_store()
    runs = run_store or get_default_execution_run_store()
    requests = request_store or get_default_execution_request_store()

    ticket = tickets.get(ticket_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found: {ticket_id}")

    _assert_gateway_requester_authorized(ticket.requester_id, requester_id)

    plan = plans.get_by_ticket(ticket_id)
    if plan is None:
        raise KeyError(f"Dispatch plan not found for ticket: {ticket_id}")

    request = create_execution_request_from_plan(
        plan,
        ticket,
        requested_by=requester_id,
        reason=reason,
    )
    run = start_dry_run(
        request,
        ticket,
        plan,
        run_store=runs,
        request_store=requests,
    )
    return {
        "request": request.to_dict(),
        "run": run.to_dict(),
    }


def start_dry_run_for_gateway_session(
    session_id: str,
    *,
    requester_id: str,
    reason: str = "",
    ticket_store: Optional[ExecutionTicketStore] = None,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
    run_store: Optional[ExecutionRunStore] = None,
    request_store: Optional[ExecutionRequestStore] = None,
) -> Dict[str, Any]:
    """Start an explicit synthetic dry-run via approval session lookup."""
    tickets = ticket_store or get_default_ticket_store()
    ticket = tickets.get_by_session(session_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found for session: {session_id}")

    return start_dry_run_for_gateway_ticket(
        ticket.ticket_id,
        requester_id=requester_id,
        reason=reason,
        ticket_store=tickets,
        plan_store=plan_store,
        run_store=run_store,
        request_store=request_store,
    )


def get_latest_dry_run_for_gateway_ticket(
    ticket_id: str,
    *,
    run_store: Optional[ExecutionRunStore] = None,
) -> Optional[Dict[str, Any]]:
    """Return the latest dry-run snapshot for a ticket, or ``None`` if missing."""
    runs = run_store or get_default_execution_run_store()
    matching = [run for run in runs.list_runs() if run.ticket_id == ticket_id]
    if not matching:
        return None
    latest = max(matching, key=lambda run: run.started_at)
    return latest.to_dict()
