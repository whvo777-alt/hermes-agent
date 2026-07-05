"""Discord COO CEO approval handler entry point — Phase 6C-1.

Prepares in-memory COO approval session payloads from Discord identity and
COO orchestration output. Future Discord handler wiring (embeds, buttons)
should call ``build_coo_approval_session_payload()`` from message/component
handlers — this module does not render UI.

This module is for COO CEO approval sessions only.
This module is unrelated to ``tools/approval.py`` ``resolve_gateway_approval()``.
``resolve_gateway_approval`` is the legacy/general exec approval queue used by
``DiscordAdapter.send_exec_approval()`` and ``ExecApprovalView``.
This module does not dispatch execution.
This module does not create execution tickets.
This module does not auto-approve or auto-publish.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from agent.coo.approval_report import CEOApprovalReport
from agent.coo.discord_approval_adapter import create_discord_approval_session
from agent.coo.models import COOOrchestrationResult

DiscordSnowflake = Union[str, int]

if TYPE_CHECKING:
    from agent.coo.approval_session import CEOApprovalSessionStore


def normalize_discord_snowflake(value: DiscordSnowflake) -> str:
    """Normalize Discord user/channel snowflakes to string IDs."""
    return str(value)


def build_coo_approval_session_payload(
    report: CEOApprovalReport,
    orchestration_result: COOOrchestrationResult,
    discord_user_id: DiscordSnowflake,
    discord_channel_id: DiscordSnowflake,
    store: Optional["CEOApprovalSessionStore"] = None,
) -> Optional[Dict[str, Any]]:
    """Prepare a COO approval session payload for Discord handler wiring.

    Returns a session ``dict`` when Phase 5C policy allows session creation,
    otherwise ``None`` (for example ``NOT_STARTED`` reports). Does not send
    Discord messages or create Execution Tickets.
    """
    return create_discord_approval_session(
        report,
        orchestration_result,
        discord_user_id=normalize_discord_snowflake(discord_user_id),
        discord_channel_id=normalize_discord_snowflake(discord_channel_id),
        store=store,
    )
