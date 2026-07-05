"""Discord approval adapter — Phase 6B runtime wiring.

Maps Discord ``user.id`` / ``channel.id`` values onto the Gateway Approval
Bridge. Discord handlers should call these functions instead of importing
``approval_session`` or ``gateway_approval`` internals directly.

This module does not dispatch execution.
This module does not create execution tickets.
This module is an approval-session bridge for Discord runtime only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from agent.coo.approval_report import CEOApprovalReport
from agent.coo.gateway_approval import (
    approve_gateway_session,
    create_gateway_approval_session,
    expire_gateway_sessions,
    reject_gateway_session,
)
from agent.coo.models import COOOrchestrationResult


def create_discord_approval_session(
    report: CEOApprovalReport,
    orchestration_result: COOOrchestrationResult,
    discord_user_id: str,
    discord_channel_id: str,
    store: Any = None,
) -> Optional[Dict[str, Any]]:
    """Create a pending CEO approval session for a Discord interaction."""
    return create_gateway_approval_session(
        report,
        orchestration_result,
        requester_id=str(discord_user_id),
        channel_id=str(discord_channel_id),
        store=store,
    )


def approve_discord_session(
    session_id: str,
    discord_user_id: str,
    store: Any = None,
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
    store: Any = None,
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
    store: Any = None,
) -> int:
    """Mark overdue pending sessions as EXPIRED. Returns count expired."""
    return expire_gateway_sessions(now=now, store=store)
