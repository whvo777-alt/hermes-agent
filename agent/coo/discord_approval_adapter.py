"""Discord approval adapter — Phase 6B/6C-Prep runtime wiring.

Maps Discord ``user.id`` / ``channel.id`` values onto the Gateway Approval
Bridge. Discord handlers should call these functions instead of importing
``approval_session`` or ``gateway_approval`` internals directly.

This module is for COO CEO approval sessions.
This module is unrelated to ``tools/approval.py`` ``resolve_gateway_approval()``.
``resolve_gateway_approval`` is the legacy/general exec approval queue.
This module does not dispatch execution.
This module is an approval-session bridge for Discord runtime only.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from agent.coo.approval_report import CEOApprovalReport
from agent.coo.gateway_approval import (
    approve_gateway_session,
    create_gateway_approval_session,
    expire_gateway_sessions,
    get_gateway_approval_session,
    reject_gateway_session,
)
from agent.coo.models import COOOrchestrationResult

if TYPE_CHECKING:
    from agent.coo.approval_session import CEOApprovalSessionStore


def create_discord_approval_session(
    report: CEOApprovalReport,
    orchestration_result: COOOrchestrationResult,
    discord_user_id: str,
    discord_channel_id: str,
    store: Optional["CEOApprovalSessionStore"] = None,
) -> Optional[Dict[str, Any]]:
    """Create a pending CEO approval session for a Discord interaction."""
    return create_gateway_approval_session(
        report,
        orchestration_result,
        requester_id=str(discord_user_id),
        channel_id=str(discord_channel_id),
        store=store,
    )


def get_discord_approval_session(
    session_id: str,
    store: Optional["CEOApprovalSessionStore"] = None,
) -> Optional[Dict[str, Any]]:
    """Return a session snapshot for Discord lookup, or ``None`` if missing."""
    return get_gateway_approval_session(session_id, store=store)


def approve_discord_session(
    session_id: str,
    discord_user_id: str,
    store: Optional["CEOApprovalSessionStore"] = None,
) -> Dict[str, Any]:
    """Approve a pending session when the Discord user is the session owner."""
    return approve_gateway_session(
        session_id,
        requester_id=str(discord_user_id),
        store=store,
    )


def reject_discord_session(
    session_id: str,
    discord_user_id: str,
    reason: str = "",
    store: Optional["CEOApprovalSessionStore"] = None,
) -> Dict[str, Any]:
    """Reject a pending session when the Discord user is the session owner."""
    return reject_gateway_session(
        session_id,
        requester_id=str(discord_user_id),
        reason=reason or None,
        store=store,
    )


def expire_discord_approval_sessions(
    now: Optional[datetime] = None,
    store: Optional["CEOApprovalSessionStore"] = None,
) -> int:
    """Mark overdue pending sessions as EXPIRED. Returns count expired."""
    return expire_gateway_sessions(now=now, store=store)
