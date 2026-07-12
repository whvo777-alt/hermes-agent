"""Discord Gateway operational status — Phase 13L.

Read-only Discord adapter for Gateway/Pilot operational summaries.
No dispatch execution, state mutation, subprocess, or secret disclosure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_gateway_operational_status import (
    CooDispatchGatewayOperationalSummary,
    HEALTH_STATUS_BLOCKED,
    HEALTH_STATUS_DEGRADED,
    HEALTH_STATUS_HEALTHY,
    HEALTH_STATUS_NOT_CONFIGURED,
    assert_safe_operational_payload,
    build_gateway_operational_summary,
    format_gateway_operational_summary,
)

ACTION_GATEWAY_PILOT_STATUS = "gateway_pilot_status"
ACTION_GATEWAY_HEALTH = "gateway_health"
ACTION_PILOT_HISTORY_SUMMARY = "pilot_history_summary"
ACTION_REGRESSION_SUMMARY = "regression_summary"

STATUS_VIEW_FULL = "full"
STATUS_VIEW_HEALTH = "health"
STATUS_VIEW_HISTORY = "history"
STATUS_VIEW_REGRESSION = "regression"

DISCORD_GATEWAY_STATUS_RESULT_KEY = "_coo_gateway_status_result"

FAILURE_UNAUTHORIZED_REQUESTER = "unauthorized"
FAILURE_SESSION_MISSING = "session_missing"
FAILURE_READINESS_NOT_MET = "readiness_not_met"
FAILURE_CORRELATION_FAILED = "correlation_failed"

_STATUS_ACTIONS = frozenset(
    {
        ACTION_GATEWAY_PILOT_STATUS,
        ACTION_GATEWAY_HEALTH,
        ACTION_PILOT_HISTORY_SUMMARY,
        ACTION_REGRESSION_SUMMARY,
    }
)

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class DiscordGatewayStatusResult:
    """Safe Discord operational status result."""

    session_id: str
    ticket_id: str
    view: str
    accepted: bool
    health_status: str
    recommended_action: str
    failure_reason_code: str
    gateway_state: str
    production_execution_allowed: bool = False
    production_root_hard_deny: bool = True
    gateway_execution_scope: str = "isolated_gateway_mock"
    summary: CooDispatchGatewayOperationalSummary | None = None


def _short(value: str, limit: int = 12) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _view_for_action(action: str) -> str:
    mapping = {
        ACTION_GATEWAY_PILOT_STATUS: STATUS_VIEW_FULL,
        ACTION_GATEWAY_HEALTH: STATUS_VIEW_HEALTH,
        ACTION_PILOT_HISTORY_SUMMARY: STATUS_VIEW_HISTORY,
        ACTION_REGRESSION_SUMMARY: STATUS_VIEW_REGRESSION,
    }
    return mapping.get(action, STATUS_VIEW_FULL)


def _blocked_status_result(
    *,
    session_id: str,
    ticket_id: str,
    view: str,
    failure_reason_code: str,
    gateway_state: str = "unknown",
    recommended_action: str = "maintain_production_block",
    health_status: str = HEALTH_STATUS_BLOCKED,
) -> DiscordGatewayStatusResult:
    return DiscordGatewayStatusResult(
        session_id=session_id,
        ticket_id=ticket_id,
        view=view,
        accepted=False,
        health_status=health_status,
        recommended_action=recommended_action,
        failure_reason_code=failure_reason_code,
        gateway_state=gateway_state,
    )


def _resolve_confirmation_context(
    *,
    ticket_id: str,
    requester_id: str,
    unlock_token_id: str,
    bundle_dir: Path | None,
    confirmation_dir: Path | None,
) -> tuple[str, str] | None:
    from agent.coo.dispatch_bundle_store import read_bundle
    from agent.coo.production_executor_confirmation import (
        default_confirmation_dir,
        read_confirmation,
    )

    try:
        bundle = read_bundle(ticket_id, bundle_dir=bundle_dir)
    except (KeyError, ValueError, OSError):
        return None
    if bundle.requester_id != requester_id:
        return None
    resolved_unlock = unlock_token_id or bundle.unlock_token_id
    if not resolved_unlock:
        return None

    base_dir = confirmation_dir or default_confirmation_dir()
    if not base_dir.is_dir():
        return None
    for path in sorted(base_dir.glob("*.json")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            confirmation = read_confirmation(path.stem, confirmation_dir=base_dir)
        except (KeyError, ValueError, OSError):
            continue
        if confirmation.unlock_token_id == resolved_unlock:
            if confirmation.ticket_id != ticket_id:
                continue
            return confirmation.confirmation_id, confirmation.attested_pipeline_root
    return None


def execute_discord_gateway_status_action(
    *,
    action: str,
    session_payload: Mapping[str, Any],
    requester_id: str,
    merged_config: Mapping[str, Any] | None = None,
    session_store=None,
    ticket_store=None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    unlock_token_id: str = "",
    gateway_request_id: str = "",
) -> DiscordGatewayStatusResult:
    """Execute one read-only Discord gateway status action."""
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in _STATUS_ACTIONS:
        raise ValueError(f"Invalid gateway status action: {action!r}")

    view = _view_for_action(normalized_action)
    session_id = str(session_payload.get("session_id") or "").strip()
    ticket_id = str(session_payload.get("execution_ticket_id") or "").strip()
    owner = str(session_payload.get("requester_id") or "").strip()

    if not session_id or not ticket_id:
        return _blocked_status_result(
            session_id=session_id,
            ticket_id=ticket_id,
            view=view,
            failure_reason_code=FAILURE_SESSION_MISSING,
            health_status=HEALTH_STATUS_NOT_CONFIGURED,
        )

    if requester_id != owner:
        return _blocked_status_result(
            session_id=session_id,
            ticket_id=ticket_id,
            view=view,
            failure_reason_code=FAILURE_UNAUTHORIZED_REQUESTER,
        )

    confirmation_context = _resolve_confirmation_context(
        ticket_id=ticket_id,
        requester_id=requester_id,
        unlock_token_id=unlock_token_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
    )
    confirmation_id = ""
    pipeline_root = ""
    if confirmation_context is not None:
        confirmation_id, pipeline_root = confirmation_context

    summary = build_gateway_operational_summary(
        session_id=session_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        unlock_token_id=unlock_token_id,
        requester_id=requester_id,
        pipeline_root=pipeline_root,
        gateway_request_id=gateway_request_id,
        merged_config=merged_config,
        session_store=session_store,
        ticket_store=ticket_store,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        request_dir=request_dir,
        history_dir=history_dir,
    )

    failure_reason_code = summary.failure_reason_code
    if not summary.correlation_ready:
        failure_reason_code = FAILURE_CORRELATION_FAILED

    accepted = summary.health_status in {
        HEALTH_STATUS_HEALTHY,
        HEALTH_STATUS_DEGRADED,
        HEALTH_STATUS_NOT_CONFIGURED,
    }
    if summary.health_status == HEALTH_STATUS_BLOCKED and normalized_action in _STATUS_ACTIONS:
        accepted = True

    return DiscordGatewayStatusResult(
        session_id=session_id,
        ticket_id=ticket_id,
        view=view,
        accepted=accepted,
        health_status=summary.health_status,
        recommended_action=summary.recommended_action,
        failure_reason_code=failure_reason_code,
        gateway_state=summary.gateway_state,
        summary=summary,
    )


def format_discord_gateway_status_response(result: DiscordGatewayStatusResult) -> str:
    """Format safe Discord ephemeral response for one status view."""
    summary = result.summary
    lines = [
        "Gateway Operational Status",
        f"view: {result.view}",
        f"session_id: {_short(result.session_id)}",
        f"ticket_id: {_short(result.ticket_id)}",
        f"health_status: {result.health_status}",
        f"recommended_action: {result.recommended_action}",
        f"failure_reason_code: {result.failure_reason_code}",
        f"gateway_state: {result.gateway_state}",
        "production_execution_allowed: false",
        f"production_root_hard_deny: {str(result.production_root_hard_deny).lower()}",
        f"gateway_execution_scope: {result.gateway_execution_scope}",
    ]
    if summary is None:
        return "\n".join(lines)

    if result.view == STATUS_VIEW_HEALTH:
        lines.extend(
            [
                "",
                "[Gateway]",
                f"state: {summary.gateway_state}",
                f"facade_connected: {str(summary.facade_connected).lower()}",
                f"mock_execution_supported: {str(summary.mock_execution_supported).lower()}",
                "",
                "[Readiness]",
                f"gateway_readiness: {summary.gateway_readiness}",
                f"signoff_ready: {str(summary.signoff_ready).lower()}",
                f"cutover_ready: {str(summary.cutover_ready).lower()}",
                "",
                "[Operator]",
                f"consume_state: {summary.consume_state}",
                f"recovery_required: {str(summary.recovery_required).lower()}",
                f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
            ]
        )
    elif result.view == STATUS_VIEW_HISTORY:
        lines.extend(
            [
                "",
                "[Pilot History]",
                f"latest_status: {summary.latest_status}",
                f"latest_pilot_attempt_id: {summary.latest_pilot_attempt_id}",
                f"trend_status: {summary.trend_status}",
                f"consecutive_failures: {summary.consecutive_failures}",
                "",
                "[Execution]",
                f"gateway_request_id: {summary.gateway_request_id}",
                f"execution_attempt_id: {summary.execution_attempt_id or _NONE_LABEL}",
                f"dispatch_run_id: {summary.dispatch_run_id or _NONE_LABEL}",
                f"consumed: {str(summary.consumed).lower()}",
            ]
        )
        if summary.timeline:
            lines.extend(["", "[Timeline]"])
            for event in summary.timeline:
                lines.append(f"{event.event_type}: {event.timestamp}")
    elif result.view == STATUS_VIEW_REGRESSION:
        lines.extend(
            [
                "",
                "[Regression]",
                f"regression_status: {summary.regression_status}",
                f"trend_status: {summary.trend_status}",
                f"consecutive_failures: {summary.consecutive_failures}",
                f"latest_status: {summary.latest_status}",
            ]
        )
    else:
        lines.append("")
        lines.append(format_gateway_operational_summary(summary))

    payload = result_to_session_payload_fragment(result)
    assert_safe_operational_payload(payload[DISCORD_GATEWAY_STATUS_RESULT_KEY])
    return "\n".join(lines)


def result_to_session_payload_fragment(result: DiscordGatewayStatusResult) -> dict[str, Any]:
    """Return minimal safe correlation for session payload updates."""
    fragment: dict[str, Any] = {
        "view": result.view,
        "health_status": result.health_status,
        "recommended_action": result.recommended_action,
        "failure_reason_code": result.failure_reason_code,
        "gateway_state": result.gateway_state,
        "production_execution_allowed": False,
        "production_root_hard_deny": result.production_root_hard_deny,
        "gateway_execution_scope": result.gateway_execution_scope,
    }
    summary = result.summary
    if summary is not None:
        fragment.update(
            {
                "gateway_readiness": summary.gateway_readiness,
                "regression_status": summary.regression_status,
                "trend_status": summary.trend_status,
                "latest_pilot_attempt_id": summary.latest_pilot_attempt_id,
                "gateway_request_id": summary.gateway_request_id,
            }
        )
    return {DISCORD_GATEWAY_STATUS_RESULT_KEY: fragment}


def is_gateway_status_action(action: str) -> bool:
    """Return whether action is a read-only gateway status action."""
    return str(action or "").strip().lower() in _STATUS_ACTIONS
