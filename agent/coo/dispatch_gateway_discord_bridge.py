"""Discord Gateway pilot bridge — Phase 13K.

Thin adapter from Discord approval/session state to the staged mock-only
Gateway Pilot Service. No real runner creation, no confirmation phrase
handling, and no Repository2 execution.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from agent.coo.dispatch_gateway_pilot_service import (
    CooDispatchGatewayPilotResult,
    execute_gateway_pilot_dispatch,
)

ACTION_GATEWAY_PILOT_DRY_RUN = "gateway_pilot_dry_run"
ACTION_GATEWAY_PILOT_RUN = "gateway_pilot_run"
ACTION_GATEWAY_PILOT_STATUS = "gateway_pilot_status"

DISCORD_GATEWAY_PILOT_RESULT_KEY = "_coo_gateway_pilot_result"

FAILURE_DISCORD_PILOT_NOT_READY = "discord_gateway_pilot_not_ready"
FAILURE_CONFIRMATION_MISSING = "confirmation_missing"
FAILURE_DISPATCH_NOT_PREPARED = "dispatch_not_prepared"
FAILURE_UNAUTHORIZED_REQUESTER = "unauthorized_requester"

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class DiscordGatewayPilotBridgeResult:
    """Safe bridge result for Discord ephemeral responses."""

    session_id: str
    ticket_id: str
    gateway_request_id: str
    pilot_attempt_id: str
    accepted: bool
    status: str
    dry_run: bool
    regression_gate: str
    failure_reason_code: str
    recommended_action: str
    gateway_state: str
    production_execution_allowed: bool = False
    execution_attempt_id: str = ""
    dispatch_run_id: str = ""
    consumed: bool = False


def _short(value: str, limit: int = 12) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def build_discord_gateway_request_id(
    *,
    session_id: str,
    action: str,
    interaction_id: str,
) -> str:
    """Build an opaque, path-safe idempotency key for one Discord interaction."""
    nonce = (interaction_id or "").strip() or str(uuid.uuid4())
    raw = f"{session_id}:{action}:{nonce}".encode("utf-8")
    return f"discord-{hashlib.sha256(raw).hexdigest()[:32]}"


def _latest_dispatch_snapshot(ticket_id: str, requester_id: str) -> Mapping[str, Any] | None:
    from agent.coo.gateway_execution_dispatch import get_latest_dispatch_for_gateway_ticket

    return get_latest_dispatch_for_gateway_ticket(ticket_id, requester_id=requester_id)


def _resolve_unlock_token_id(snapshot: Mapping[str, Any] | None) -> str:
    if not isinstance(snapshot, Mapping):
        return ""
    token = snapshot.get("unlock_token")
    if not isinstance(token, Mapping):
        return ""
    return str(token.get("token_id") or "").strip()


def _find_confirmation_for_unlock_token(
    unlock_token_id: str,
    *,
    confirmation_dir: Path | None = None,
):
    from agent.coo.production_executor_confirmation import (
        default_confirmation_dir,
        read_confirmation,
    )

    if not unlock_token_id.strip():
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
        if confirmation.unlock_token_id == unlock_token_id:
            return confirmation
    return None


def _blocked_bridge_result(
    *,
    session_id: str,
    ticket_id: str,
    gateway_request_id: str = "",
    dry_run: bool,
    failure_reason_code: str,
    gateway_state: str = "unknown",
    recommended_action: str = "operator_handoff",
    status: str = "blocked",
) -> DiscordGatewayPilotBridgeResult:
    return DiscordGatewayPilotBridgeResult(
        session_id=session_id,
        ticket_id=ticket_id,
        gateway_request_id=gateway_request_id,
        pilot_attempt_id="",
        accepted=False,
        status=status,
        dry_run=dry_run,
        regression_gate="not_evaluated",
        failure_reason_code=failure_reason_code,
        recommended_action=recommended_action,
        gateway_state=gateway_state,
    )


def _bridge_result_from_service(
    result: CooDispatchGatewayPilotResult,
) -> DiscordGatewayPilotBridgeResult:
    return DiscordGatewayPilotBridgeResult(
        session_id=result.session_id,
        ticket_id=result.ticket_id,
        gateway_request_id=result.gateway_request_id,
        pilot_attempt_id=result.pilot_attempt_id,
        accepted=result.accepted,
        status=result.status,
        dry_run=result.dry_run,
        regression_gate=result.regression_gate,
        failure_reason_code=result.failure_reason_code,
        recommended_action=result.recommended_action,
        gateway_state=result.gateway_state,
        production_execution_allowed=False,
        execution_attempt_id=result.execution_attempt_id,
        dispatch_run_id=result.dispatch_run_id,
        consumed=result.consumed,
    )


def execute_discord_gateway_pilot_action(
    *,
    action: str,
    session_payload: Mapping[str, Any],
    requester_id: str,
    interaction_id: str = "",
    merged_config: Mapping[str, Any] | None = None,
    injected_runner: Callable[..., Any] | None = None,
    session_store=None,
    ticket_store=None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
) -> DiscordGatewayPilotBridgeResult:
    """Execute one Discord gateway pilot action through the service layer."""
    normalized_action = str(action or "").strip().lower()
    dry_run = normalized_action != ACTION_GATEWAY_PILOT_RUN
    session_id = str(session_payload.get("session_id") or "").strip()
    ticket_id = str(session_payload.get("execution_ticket_id") or "").strip()
    owner = str(session_payload.get("requester_id") or "").strip()
    if not session_id or not ticket_id:
        return _blocked_bridge_result(
            session_id=session_id,
            ticket_id=ticket_id,
            dry_run=dry_run,
            failure_reason_code=FAILURE_DISCORD_PILOT_NOT_READY,
        )
    if requester_id != owner:
        return _blocked_bridge_result(
            session_id=session_id,
            ticket_id=ticket_id,
            dry_run=dry_run,
            failure_reason_code=FAILURE_UNAUTHORIZED_REQUESTER,
        )

    snapshot = _latest_dispatch_snapshot(ticket_id, requester_id=requester_id)
    unlock_token_id = _resolve_unlock_token_id(snapshot)
    if not unlock_token_id:
        return _blocked_bridge_result(
            session_id=session_id,
            ticket_id=ticket_id,
            dry_run=dry_run,
            failure_reason_code=FAILURE_DISPATCH_NOT_PREPARED,
        )
    confirmation = _find_confirmation_for_unlock_token(
        unlock_token_id,
        confirmation_dir=confirmation_dir,
    )
    if confirmation is None:
        return _blocked_bridge_result(
            session_id=session_id,
            ticket_id=ticket_id,
            dry_run=dry_run,
            failure_reason_code=FAILURE_CONFIRMATION_MISSING,
        )

    gateway_request_id = build_discord_gateway_request_id(
        session_id=session_id,
        action=normalized_action,
        interaction_id=interaction_id,
    )
    if normalized_action == ACTION_GATEWAY_PILOT_STATUS:
        from agent.coo.dispatch_cli_gateway_pilot import evaluate_gateway_pilot_readiness

        summary = evaluate_gateway_pilot_readiness(
            session_id=session_id,
            ticket_id=ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=unlock_token_id,
            requester_id=requester_id,
            pipeline_root=confirmation.attested_pipeline_root,
            merged_config=merged_config,
            session_store=session_store,
            ticket_store=ticket_store,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            dry_run=True,
        )
        return DiscordGatewayPilotBridgeResult(
            session_id=session_id,
            ticket_id=ticket_id,
            gateway_request_id=gateway_request_id,
            pilot_attempt_id="",
            accepted=summary.pilot_ready,
            status="ready" if summary.pilot_ready else "blocked",
            dry_run=True,
            regression_gate="allowed" if summary.regression_allowed else "blocked",
            failure_reason_code="none" if summary.pilot_ready else summary.failed_checks,
            recommended_action=summary.recommended_action,
            gateway_state=summary.gateway_state,
        )

    result = execute_gateway_pilot_dispatch(
        session_id=session_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation.confirmation_id,
        unlock_token_id=unlock_token_id,
        requester_id=requester_id,
        pipeline_root=confirmation.attested_pipeline_root,
        gateway_request_id=gateway_request_id,
        dry_run=dry_run,
        merged_config=merged_config,
        injected_runner=injected_runner,
        allow_mock_gateway_dispatch=dry_run or injected_runner is not None,
        session_store=session_store,
        ticket_store=ticket_store,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        request_dir=request_dir,
        history_dir=history_dir,
    )
    return _bridge_result_from_service(result)


def format_discord_gateway_pilot_response(
    result: DiscordGatewayPilotBridgeResult,
) -> str:
    """Return a safe ephemeral Discord response string."""
    lines = [
        "Gateway Pilot",
        f"session_id: {_short(result.session_id)}",
        f"ticket_id: {_short(result.ticket_id)}",
        f"gateway_request_id: {result.gateway_request_id or _NONE_LABEL}",
        f"pilot_attempt_id: {result.pilot_attempt_id or _NONE_LABEL}",
        f"status: {result.status}",
        f"dry_run: {str(result.dry_run).lower()}",
        f"regression_gate: {result.regression_gate}",
        f"failure_reason_code: {result.failure_reason_code}",
        f"recommended_action: {result.recommended_action}",
        f"production_execution_allowed: {str(result.production_execution_allowed).lower()}",
        f"gateway_state: {result.gateway_state}",
    ]
    if result.execution_attempt_id:
        lines.append(f"execution_attempt_id: {result.execution_attempt_id}")
    if result.dispatch_run_id:
        lines.append(f"dispatch_run_id: {result.dispatch_run_id}")
    lines.append(f"consumed: {str(result.consumed).lower()}")
    return "\n".join(lines)


def result_to_session_payload_fragment(
    result: DiscordGatewayPilotBridgeResult,
) -> dict[str, Any]:
    """Return minimal safe correlation for an ephemeral session payload."""
    return {
        DISCORD_GATEWAY_PILOT_RESULT_KEY: {
            "gateway_request_id": result.gateway_request_id,
            "pilot_attempt_id": result.pilot_attempt_id,
            "execution_attempt_id": result.execution_attempt_id,
            "dispatch_run_id": result.dispatch_run_id,
            "status": result.status,
            "dry_run": result.dry_run,
            "failure_reason_code": result.failure_reason_code,
            "gateway_state": result.gateway_state,
            "production_execution_allowed": False,
        }
    }
