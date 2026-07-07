"""Gateway approval bridge — Phase 6A Discord runtime integration.

Thin adapter so the Discord Gateway can create, inspect, approve, reject,
and expire CEO approval sessions without importing session internals.

Approval success creates an execution ticket via the ticket bridge — record
only; no Repository 2 execution, no publish, no dispatch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from agent.coo.approval_report import CEOApprovalReport
from agent.coo.approval_session import (
    CEOApprovalSessionStore,
    approve_session,
    create_approval_session,
    get_default_session_store,
    reject_session,
    should_create_approval_session,
)
from agent.coo.execution_ticket import (
    ExecutionTicketStore,
    create_ticket_from_approval_session,
    get_default_ticket_store,
)
from agent.coo.models import COOOrchestrationResult


def create_gateway_approval_session(
    report: CEOApprovalReport,
    orchestration_result: COOOrchestrationResult,
    *,
    requester_id: str,
    channel_id: str,
    store: Optional[CEOApprovalSessionStore] = None,
) -> Optional[Dict[str, Any]]:
    """Create a pending approval session when Phase 5C policy allows it.

    ``requester_id`` and ``channel_id`` are expected to be Discord user/channel
    identifiers. Returns ``None`` when the report is ``NOT_STARTED`` or when
    neither ``review_required`` nor ``approval_required`` applies.
    """
    if not should_create_approval_session(report):
        return None
    session = create_approval_session(
        report,
        orchestration_result,
        requester_id=requester_id,
        channel_id=channel_id,
        store=store,
    )
    return session.to_dict()


def get_gateway_approval_session(
    session_id: str,
    *,
    store: Optional[CEOApprovalSessionStore] = None,
) -> Optional[Dict[str, Any]]:
    """Return a session snapshot for Gateway lookup, or ``None`` if missing."""
    session_store = store or get_default_session_store()
    session = session_store.get(session_id)
    if session is None:
        return None
    return session.to_dict()


def create_ticket_for_gateway_session(
    session_id: str,
    *,
    session_store: Optional[CEOApprovalSessionStore] = None,
    ticket_store: Optional[ExecutionTicketStore] = None,
) -> Dict[str, Any]:
    """Create or return an execution ticket for an approved session — no dispatch."""
    store = session_store or get_default_session_store()
    session = store.get(session_id)
    if session is None:
        raise KeyError(f"Approval session not found: {session_id}")

    tickets = ticket_store or get_default_ticket_store()
    ticket = create_ticket_from_approval_session(session, store=tickets)

    if session.execution_ticket_id != ticket.ticket_id:
        session.execution_ticket_id = ticket.ticket_id
        store.save(session)

    return ticket.to_dict()


def approve_gateway_session(
    session_id: str,
    *,
    requester_id: str,
    reviewer: Optional[str] = None,
    store: Optional[CEOApprovalSessionStore] = None,
    ticket_store: Optional[ExecutionTicketStore] = None,
) -> Dict[str, Any]:
    """Approve a pending session and create an execution ticket — no dispatch."""
    actor = reviewer or requester_id
    session_store = store or get_default_session_store()
    approve_session(
        session_id,
        reviewer=actor,
        requester_id=requester_id,
        store=session_store,
    )
    create_ticket_for_gateway_session(
        session_id,
        session_store=session_store,
        ticket_store=ticket_store,
    )
    refreshed = session_store.get(session_id)
    if refreshed is None:
        raise KeyError(f"Approval session not found: {session_id}")
    return refreshed.to_dict()


def reject_gateway_session(
    session_id: str,
    *,
    requester_id: str,
    reason: Optional[str] = None,
    reviewer: Optional[str] = None,
    store: Optional[CEOApprovalSessionStore] = None,
) -> Dict[str, Any]:
    """Reject a pending session — record only."""
    actor = reviewer or requester_id
    session = reject_session(
        session_id,
        reviewer=actor,
        requester_id=requester_id,
        reason=reason,
        store=store,
    )
    return session.to_dict()


def expire_gateway_sessions(
    *,
    now: Optional[datetime] = None,
    store: Optional[CEOApprovalSessionStore] = None,
) -> int:
    """Mark overdue pending sessions as EXPIRED. Returns count expired."""
    session_store = store or get_default_session_store()
    return session_store.expire_pending_sessions(now=now)
