"""Gateway execution dispatcher bridge — Phase 7G plan-only integration.

Thin adapter so Gateway runtimes can request dispatch plans from execution
tickets without importing dispatcher internals directly.

Explicit invocation only — approval success does not create dispatch plans.
No Repository 2 execution, no publish, no subprocess, no dispatch.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.coo.execution_dispatcher import (
    ExecutionDispatchPlanStore,
    create_dispatch_plan_from_ticket,
    get_default_dispatch_plan_store,
)
from agent.coo.execution_ticket import ExecutionTicketStore, get_default_ticket_store


def _assert_gateway_requester_authorized(ticket_requester_id: str, requester_id: str) -> None:
    if requester_id != ticket_requester_id:
        raise ValueError(
            f"Requester {requester_id!r} is not authorized for ticket "
            f"(owner: {ticket_requester_id!r})"
        )


def create_dispatch_plan_for_gateway_ticket(
    ticket_id: str,
    *,
    requester_id: str,
    reason: str = "",
    ticket_store: Optional[ExecutionTicketStore] = None,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
) -> Dict[str, Any]:
    """Create or return a dispatch plan for a ticket — explicit request only."""
    tickets = ticket_store or get_default_ticket_store()
    plans = plan_store or get_default_dispatch_plan_store()

    ticket = tickets.get(ticket_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found: {ticket_id}")

    _assert_gateway_requester_authorized(ticket.requester_id, requester_id)

    existing = plans.get_by_ticket(ticket_id)
    if existing is not None:
        return existing.to_dict()

    plan = create_dispatch_plan_from_ticket(
        ticket,
        requested_by=requester_id,
        reason=reason,
    )
    plans.save(plan)
    return plan.to_dict()


def get_dispatch_plan_for_gateway_ticket(
    ticket_id: str,
    *,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
) -> Optional[Dict[str, Any]]:
    """Return a dispatch plan snapshot for lookup, or ``None`` if missing."""
    plans = plan_store or get_default_dispatch_plan_store()
    plan = plans.get_by_ticket(ticket_id)
    if plan is None:
        return None
    return plan.to_dict()


def create_dispatch_plan_for_gateway_session(
    session_id: str,
    *,
    requester_id: str,
    reason: str = "",
    ticket_store: Optional[ExecutionTicketStore] = None,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
) -> Dict[str, Any]:
    """Create or return a dispatch plan via approval session lookup."""
    tickets = ticket_store or get_default_ticket_store()
    ticket = tickets.get_by_session(session_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found for session: {session_id}")

    return create_dispatch_plan_for_gateway_ticket(
        ticket.ticket_id,
        requester_id=requester_id,
        reason=reason,
        ticket_store=tickets,
        plan_store=plan_store,
    )
