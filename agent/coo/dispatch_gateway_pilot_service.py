"""Gateway pilot dispatch service — Phase 13J.

Correlates gateway session/ticket/bundle/confirmation state with mock-only
gateway facade dispatch. No Discord wiring, no real subprocess, no Repository2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agent.coo.dispatch_cli_gateway_pilot import (
    EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
    RECOMMENDED_ACTION_RESOLVE_FAILURE,
    evaluate_gateway_pilot_readiness,
    validate_gateway_pilot_correlation,
)
from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_DISABLED,
    GATEWAY_STATE_ENABLED,
)
from agent.coo.dispatch_gateway_execution_facade import (
    FAILURE_NONE,
    RESULT_STATUS_ALREADY_COMPLETED,
    RESULT_STATUS_BLOCKED,
    RESULT_STATUS_IN_PROGRESS,
    CooDispatchGatewayDispatchResult,
    execute_gateway_dispatch,
)

FAILURE_GATEWAY_DISABLED = "gateway_disabled"
FAILURE_ENABLED_NOT_SUPPORTED = "enabled_state_not_supported_for_gateway_pilot"
FAILURE_MOCK_RUNNER_NOT_CONFIGURED = "gateway_mock_runner_not_configured"
FAILURE_READINESS_FAILED = "gateway_pilot_readiness_failed"
FAILURE_HISTORY_PERSISTENCE_FAILED = "history_persistence_failed"

RECOMMENDED_ACTION_RETRY_NEW_REQUEST = "retry_with_new_gateway_request_id"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CooDispatchGatewayPilotResult:
    """Safe gateway pilot dispatch result."""

    gateway_request_id: str
    pilot_attempt_id: str
    session_id: str
    ticket_id: str
    accepted: bool
    status: str
    dry_run: bool
    execution_attempt_id: str = ""
    dispatch_run_id: str = ""
    consumed: bool = False
    regression_gate: str = "clear"
    failure_reason_code: str = FAILURE_NONE
    gateway_state: str = GATEWAY_STATE_DISABLED
    execution_scope: str = EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK
    production_execution_allowed: bool = False
    recommended_action: str = RECOMMENDED_ACTION_RESOLVE_FAILURE
    history_persisted: bool = False
    history_persistence_failed: bool = False


def _blocked_result(
    *,
    gateway_request_id: str,
    pilot_attempt_id: str,
    session_id: str,
    ticket_id: str,
    dry_run: bool,
    failure_reason_code: str,
    gateway_state: str = GATEWAY_STATE_DISABLED,
    status: str = RESULT_STATUS_BLOCKED,
    recommended_action: str = RECOMMENDED_ACTION_RESOLVE_FAILURE,
) -> CooDispatchGatewayPilotResult:
    return CooDispatchGatewayPilotResult(
        gateway_request_id=gateway_request_id,
        pilot_attempt_id=pilot_attempt_id,
        session_id=session_id,
        ticket_id=ticket_id,
        accepted=False,
        status=status,
        dry_run=dry_run,
        failure_reason_code=failure_reason_code,
        gateway_state=gateway_state,
        recommended_action=recommended_action,
    )


def _result_from_facade(
    *,
    facade_result: CooDispatchGatewayDispatchResult,
    pilot_attempt_id: str,
    session_id: str,
    ticket_id: str,
    history_persisted: bool,
    regression_gate: str,
) -> CooDispatchGatewayPilotResult:
    failure = facade_result.failure_reason_code
    if not history_persisted and facade_result.accepted:
        failure = FAILURE_HISTORY_PERSISTENCE_FAILED
    return CooDispatchGatewayPilotResult(
        gateway_request_id=facade_result.gateway_request_id,
        pilot_attempt_id=pilot_attempt_id,
        session_id=session_id,
        ticket_id=ticket_id,
        accepted=facade_result.accepted and history_persisted,
        status=facade_result.status,
        dry_run=facade_result.dry_run,
        execution_attempt_id=facade_result.execution_attempt_id,
        dispatch_run_id=facade_result.dispatch_run_id,
        consumed=facade_result.consumed,
        regression_gate=regression_gate,
        failure_reason_code=failure,
        gateway_state=facade_result.gateway_state,
        recommended_action=facade_result.recommended_action,
        history_persisted=history_persisted,
        history_persistence_failed=not history_persisted,
    )


def execute_gateway_pilot_dispatch(
    *,
    session_id: str,
    ticket_id: str,
    confirmation_id: str,
    unlock_token_id: str,
    requester_id: str,
    pipeline_root: str,
    gateway_request_id: str,
    dry_run: bool = False,
    merged_config: Mapping[str, Any] | None = None,
    binding_state=None,
    injected_runner: Callable[..., Any] | None = None,
    allow_mock_gateway_dispatch: bool = False,
    session_store=None,
    ticket_store=None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
) -> CooDispatchGatewayPilotResult:
    """Run gateway pilot dispatch via mock-only facade with full correlation."""
    if merged_config is None:
        merged_config = {}

    pilot_attempt_id = str(uuid.uuid4())
    normalized_request_id = (gateway_request_id or "").strip() or str(uuid.uuid4())

    from agent.coo.dispatch_gateway_enablement import load_dispatch_gateway_enablement

    enablement = load_dispatch_gateway_enablement(merged_config=merged_config)
    gateway_state = enablement.gateway_state

    if enablement.gateway_state == GATEWAY_STATE_DISABLED:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            pilot_attempt_id=pilot_attempt_id,
            session_id=session_id,
            ticket_id=ticket_id,
            dry_run=dry_run,
            failure_reason_code=FAILURE_GATEWAY_DISABLED,
            gateway_state=gateway_state,
        )

    if enablement.gateway_state == GATEWAY_STATE_ENABLED:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            pilot_attempt_id=pilot_attempt_id,
            session_id=session_id,
            ticket_id=ticket_id,
            dry_run=dry_run,
            failure_reason_code=FAILURE_ENABLED_NOT_SUPPORTED,
            gateway_state=gateway_state,
        )

    correlation_failure = validate_gateway_pilot_correlation(
        session_id=session_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        unlock_token_id=unlock_token_id,
        requester_id=requester_id,
        pipeline_root=pipeline_root,
        session_store=session_store,
        ticket_store=ticket_store,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
    )
    if correlation_failure is not None:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            pilot_attempt_id=pilot_attempt_id,
            session_id=session_id,
            ticket_id=ticket_id,
            dry_run=dry_run,
            failure_reason_code=correlation_failure,
            gateway_state=gateway_state,
        )

    readiness = evaluate_gateway_pilot_readiness(
        session_id=session_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        unlock_token_id=unlock_token_id,
        requester_id=requester_id,
        pipeline_root=pipeline_root,
        merged_config=merged_config,
        session_store=session_store,
        ticket_store=ticket_store,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        dry_run=dry_run,
    )
    if not readiness.pilot_ready:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            pilot_attempt_id=pilot_attempt_id,
            session_id=session_id,
            ticket_id=ticket_id,
            dry_run=dry_run,
            failure_reason_code=FAILURE_READINESS_FAILED,
            gateway_state=gateway_state,
        )

    if not dry_run and injected_runner is None:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            pilot_attempt_id=pilot_attempt_id,
            session_id=session_id,
            ticket_id=ticket_id,
            dry_run=False,
            failure_reason_code=FAILURE_MOCK_RUNNER_NOT_CONFIGURED,
            gateway_state=gateway_state,
        )

    from agent.coo.dispatch_cli_pilot_regression_gate import (
        evaluate_pilot_regression_gate,
    )

    gate = evaluate_pilot_regression_gate(ticket_id=ticket_id, dry_run=dry_run)
    if not dry_run and not gate.live_pilot_allowed:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            pilot_attempt_id=pilot_attempt_id,
            session_id=session_id,
            ticket_id=ticket_id,
            dry_run=dry_run,
            failure_reason_code="regression_blocked",
            gateway_state=gateway_state,
        )

    started_at = _utc_now_iso()
    mock_allowed = allow_mock_gateway_dispatch or dry_run
    if not dry_run and injected_runner is not None:
        mock_allowed = True
    facade_result = execute_gateway_dispatch(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        unlock_token_id=unlock_token_id,
        requester_id=requester_id,
        pipeline_root=pipeline_root,
        gateway_request_id=normalized_request_id,
        merged_config=merged_config,
        binding_state=binding_state,
        injected_runner=injected_runner,
        dry_run=dry_run,
        allow_mock_gateway_dispatch=mock_allowed,
        session_id=session_id,
        pilot_attempt_id=pilot_attempt_id,
        request_dir=request_dir,
    )
    completed_at = _utc_now_iso()

    if facade_result.status in {
        RESULT_STATUS_BLOCKED,
        RESULT_STATUS_ALREADY_COMPLETED,
        RESULT_STATUS_IN_PROGRESS,
    }:
        return _result_from_facade(
            facade_result=facade_result,
            pilot_attempt_id=pilot_attempt_id,
            session_id=session_id,
            ticket_id=ticket_id,
            history_persisted=False,
            regression_gate=gate.regression_gate,
        )

    from agent.coo.dispatch_cli_pilot_history import (
        build_gateway_pilot_history_record_from_facade,
    )
    from agent.coo.dispatch_pilot_history import write_pilot_history_record

    record = build_gateway_pilot_history_record_from_facade(
        pilot_attempt_id=pilot_attempt_id,
        session_id=session_id,
        gateway_request_id=normalized_request_id,
        started_at=started_at,
        completed_at=completed_at,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        dry_run=dry_run,
        facade_result=facade_result,
        production_root_hard_deny=readiness.production_root_hard_deny,
    )
    history_persisted = False
    try:
        write_pilot_history_record(record, history_dir=history_dir)
        history_persisted = True
    except ValueError:
        history_persisted = False

    return _result_from_facade(
        facade_result=facade_result,
        pilot_attempt_id=pilot_attempt_id,
        session_id=session_id,
        ticket_id=ticket_id,
        history_persisted=history_persisted,
        regression_gate=gate.regression_gate,
    )


def format_gateway_pilot_result(result: CooDispatchGatewayPilotResult) -> str:
    """Format safe gateway pilot result fields for operator review."""
    lines = [
        "Gateway Pilot Dispatch Result",
        "",
        f"gateway_request_id: {result.gateway_request_id}",
        f"pilot_attempt_id: {result.pilot_attempt_id}",
        f"session_id: {result.session_id}",
        f"ticket_id: {result.ticket_id}",
        f"accepted: {str(result.accepted).lower()}",
        f"status: {result.status}",
        f"dry_run: {str(result.dry_run).lower()}",
        f"execution_attempt_id: {result.execution_attempt_id or '(none)'}",
        f"dispatch_run_id: {result.dispatch_run_id or '(none)'}",
        f"consumed: {str(result.consumed).lower()}",
        f"regression_gate: {result.regression_gate}",
        f"failure_reason_code: {result.failure_reason_code}",
        f"gateway_state: {result.gateway_state}",
        f"execution_scope: {result.execution_scope}",
        (
            "production_execution_allowed: "
            f"{str(result.production_execution_allowed).lower()}"
        ),
        f"recommended_action: {result.recommended_action}",
        f"history_persisted: {str(result.history_persisted).lower()}",
    ]
    if result.history_persistence_failed:
        lines.append("history_persistence_failed: true")
    return "\n".join(lines)
