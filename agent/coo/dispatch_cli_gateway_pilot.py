"""CLI gateway pilot readiness — Phase 13J.

Read-only gateway pilot readiness for staged mock-only dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_cli_production_cutover import (
    evaluate_production_cutover_checklist,
)
from agent.coo.dispatch_cli_production_signoff import (
    evaluate_dispatch_production_signoff,
)
from agent.coo.dispatch_cli_readiness import evaluate_dispatch_operator_readiness
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_RECOVERY_REQUIRED,
    assess_consume_status,
)
from agent.coo.dispatch_cli_production_readiness import (
    _production_root_hard_deny_active,
)
from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_DISABLED,
    GATEWAY_STATE_ENABLED,
    GATEWAY_STATE_STAGED,
    load_dispatch_gateway_enablement,
)
from agent.coo.dispatch_gateway_execution_facade import (
    evaluate_gateway_execution_facade,
)
from agent.coo.dispatch_pipeline_root_trust import (
    assert_pipeline_root_matches_attestation,
)

EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK = "isolated_gateway_mock"

FAILURE_SESSION_MISSING = "session_missing"
FAILURE_REQUESTER_MISMATCH = "requester_mismatch"
FAILURE_SESSION_TICKET_MISMATCH = "session_ticket_mismatch"
FAILURE_CONFIRMATION_MISMATCH = "confirmation_mismatch"
FAILURE_BUNDLE_MISMATCH = "bundle_mismatch"
FAILURE_ATTESTATION_MISMATCH = "attestation_mismatch"

RECOMMENDED_ACTION_RUN_MOCK_DISPATCH = "run_mock_gateway_dispatch"
RECOMMENDED_ACTION_RESOLVE_FAILURE = "resolve_gateway_pilot_failure"
RECOMMENDED_ACTION_STAGE_GATEWAY = "stage_gateway_for_mock_dispatch"

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class CooDispatchGatewayPilotReadinessSummary:
    """Safe read-only gateway pilot readiness summary."""

    pilot_ready: bool
    gateway_state: str
    facade_connected: bool
    isolated_execution_supported: bool
    signoff_ready: bool
    cutover_ready: bool
    operator_ready: bool
    correlation_ready: bool
    consume_state_clear: bool
    repair_lock_clear: bool
    regression_allowed: bool
    production_root_hard_deny: bool
    production_execution_allowed: bool
    execution_scope: str
    failed_checks: str
    recommended_action: str


def _join_names(names: tuple[str, ...]) -> str:
    return ",".join(names) if names else _NONE_LABEL


def validate_gateway_pilot_correlation(
    *,
    session_id: str,
    ticket_id: str,
    confirmation_id: str,
    unlock_token_id: str,
    requester_id: str,
    pipeline_root: str,
    session_store=None,
    ticket_store=None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> str | None:
    """Validate session/ticket/bundle/confirmation correlation; return failure code or None."""
    from agent.coo.gateway_approval import get_gateway_approval_session

    if not session_id.strip():
        return FAILURE_SESSION_MISSING

    session = get_gateway_approval_session(session_id, store=session_store)
    if session is None:
        return FAILURE_SESSION_MISSING

    if session.get("requester_id") != requester_id:
        return FAILURE_REQUESTER_MISMATCH

    exec_ticket_id = str(session.get("execution_ticket_id", "")).strip()
    if ticket_store is not None:
        ticket = ticket_store.get(ticket_id)
        if ticket is None:
            return FAILURE_SESSION_TICKET_MISMATCH
        if ticket.approval_session_id != session_id:
            return FAILURE_SESSION_TICKET_MISMATCH
        if ticket.requester_id != requester_id:
            return FAILURE_REQUESTER_MISMATCH
    elif not exec_ticket_id or exec_ticket_id != ticket_id:
        return FAILURE_SESSION_TICKET_MISMATCH

    from agent.coo.dispatch_bundle_store import read_bundle
    from agent.coo.production_executor_confirmation import read_confirmation

    try:
        bundle = read_bundle(ticket_id, bundle_dir=bundle_dir)
    except (KeyError, ValueError, OSError):
        return FAILURE_BUNDLE_MISMATCH

    if bundle.ticket_id != ticket_id:
        return FAILURE_BUNDLE_MISMATCH
    if bundle.requester_id != requester_id:
        return FAILURE_BUNDLE_MISMATCH
    if unlock_token_id and bundle.unlock_token_id != unlock_token_id:
        return FAILURE_BUNDLE_MISMATCH

    try:
        confirmation = read_confirmation(
            confirmation_id,
            confirmation_dir=confirmation_dir,
        )
    except (KeyError, ValueError, OSError):
        return FAILURE_CONFIRMATION_MISMATCH

    if confirmation.ticket_id != ticket_id:
        return FAILURE_CONFIRMATION_MISMATCH
    if unlock_token_id and confirmation.unlock_token_id != unlock_token_id:
        return FAILURE_CONFIRMATION_MISMATCH
    if confirmation.dispatch_request_id != bundle.dispatch_request_id:
        return FAILURE_CONFIRMATION_MISMATCH

    try:
        assert_pipeline_root_matches_attestation(
            cli_pipeline_root=pipeline_root,
            attested_pipeline_root=confirmation.attested_pipeline_root,
        )
    except ValueError:
        return FAILURE_ATTESTATION_MISMATCH

    return None


def evaluate_gateway_pilot_readiness(
    *,
    session_id: str,
    ticket_id: str,
    confirmation_id: str,
    unlock_token_id: str = "",
    requester_id: str = "",
    pipeline_root: str,
    merged_config: Mapping[str, Any] | None = None,
    session_store=None,
    ticket_store=None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    dry_run: bool = False,
) -> CooDispatchGatewayPilotReadinessSummary:
    """Evaluate read-only gateway pilot readiness without mutating state."""
    if merged_config is None:
        merged_config = {}

    enablement = load_dispatch_gateway_enablement(merged_config=merged_config)
    facade = evaluate_gateway_execution_facade(merged_config=merged_config)
    signoff = evaluate_dispatch_production_signoff(merged_config=merged_config)
    cutover = evaluate_production_cutover_checklist(merged_config=merged_config)

    failed: list[str] = []

    if not enablement.valid:
        failed.append("gateway_enablement_invalid")
    elif enablement.gateway_state == GATEWAY_STATE_DISABLED:
        failed.append("gateway_disabled")
    elif enablement.gateway_state == GATEWAY_STATE_ENABLED:
        failed.append("gateway_enabled_not_supported")
    elif enablement.gateway_state != GATEWAY_STATE_STAGED:
        failed.append("gateway_state_invalid")

    if not facade.valid or not facade.facade_connected:
        failed.append("facade_not_connected")
    if not facade.isolated_execution_supported:
        failed.append("isolated_execution_not_supported")

    if not signoff.signoff_ready:
        failed.append("production_signoff")
    if not cutover.cutover_ready:
        failed.append("production_cutover")
    if not signoff.production_root_hard_deny:
        failed.append("production_root_hard_deny")
    if signoff.execution_allowed:
        failed.append("production_execution_allowed")

    operator_ready = False
    if ticket_id and confirmation_id and pipeline_root:
        readiness = evaluate_dispatch_operator_readiness(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            pipeline_root=pipeline_root,
            merged_config=merged_config,
        )
        operator_ready = readiness.ready
        if not operator_ready:
            failed.append("operator_readiness")

    resolved_requester_id = requester_id
    if not resolved_requester_id.strip() and session_id.strip():
        from agent.coo.gateway_approval import get_gateway_approval_session

        session = get_gateway_approval_session(session_id, store=session_store)
        if session is not None:
            resolved_requester_id = str(session.get("requester_id", ""))

    correlation_failure = None
    correlation_ready = False
    if session_id and ticket_id and confirmation_id and pipeline_root:
        correlation_failure = validate_gateway_pilot_correlation(
            session_id=session_id,
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            unlock_token_id=unlock_token_id,
            requester_id=resolved_requester_id,
            pipeline_root=pipeline_root,
            session_store=session_store,
            ticket_store=ticket_store,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
        correlation_ready = correlation_failure is None
        if correlation_failure is not None:
            failed.append(correlation_failure)

    consume_clear = True
    if ticket_id and confirmation_id:
        consume = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        if consume.consume_state in {
            CONSUME_STATE_PARTIAL,
            CONSUME_STATE_LEGACY_PARTIAL,
            CONSUME_STATE_RECOVERY_REQUIRED,
            CONSUME_STATE_COMMITTED,
        }:
            consume_clear = False
            failed.append("consume_state_blocked")

    repair_clear = True
    if ticket_id and confirmation_id:
        from agent.coo.dispatch_cli_consume_repair_lock import (
            summarize_consume_repair_lock_status,
        )

        lock_status = summarize_consume_repair_lock_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        if lock_status.repair_in_progress:
            repair_clear = False
            failed.append("repair_lock_held")

    regression_allowed = True
    if ticket_id:
        from agent.coo.dispatch_cli_pilot_regression_gate import (
            evaluate_pilot_regression_gate,
        )

        gate = evaluate_pilot_regression_gate(ticket_id=ticket_id, dry_run=dry_run)
        if not dry_run and not gate.live_pilot_allowed:
            regression_allowed = False
            failed.append("regression_blocked")
        if gate.regression_status == "FAIL" and not dry_run:
            regression_allowed = False
            if "regression_blocked" not in failed:
                failed.append("regression_blocked")

    failed_checks = tuple(dict.fromkeys(failed))
    pilot_ready = not failed_checks

    if enablement.gateway_state == GATEWAY_STATE_DISABLED:
        recommended = RECOMMENDED_ACTION_STAGE_GATEWAY
    elif pilot_ready:
        recommended = RECOMMENDED_ACTION_RUN_MOCK_DISPATCH
    else:
        recommended = RECOMMENDED_ACTION_RESOLVE_FAILURE

    return CooDispatchGatewayPilotReadinessSummary(
        pilot_ready=pilot_ready,
        gateway_state=enablement.gateway_state,
        facade_connected=facade.facade_connected,
        isolated_execution_supported=facade.isolated_execution_supported,
        signoff_ready=signoff.signoff_ready,
        cutover_ready=cutover.cutover_ready,
        operator_ready=operator_ready,
        correlation_ready=correlation_ready,
        consume_state_clear=consume_clear,
        repair_lock_clear=repair_clear,
        regression_allowed=regression_allowed,
        production_root_hard_deny=_production_root_hard_deny_active(),
        production_execution_allowed=False,
        execution_scope=EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
        failed_checks=_join_names(failed_checks),
        recommended_action=recommended,
    )


def format_gateway_pilot_readiness(
    summary: CooDispatchGatewayPilotReadinessSummary,
) -> str:
    """Format safe gateway pilot readiness fields for CLI stdout."""
    lines = [
        "Gateway Pilot Readiness",
        "",
        f"pilot_ready: {str(summary.pilot_ready).lower()}",
        f"gateway_state: {summary.gateway_state}",
        f"facade_connected: {str(summary.facade_connected).lower()}",
        (
            "isolated_execution_supported: "
            f"{str(summary.isolated_execution_supported).lower()}"
        ),
        f"signoff_ready: {str(summary.signoff_ready).lower()}",
        f"cutover_ready: {str(summary.cutover_ready).lower()}",
        f"operator_ready: {str(summary.operator_ready).lower()}",
        f"correlation_ready: {str(summary.correlation_ready).lower()}",
        f"consume_state_clear: {str(summary.consume_state_clear).lower()}",
        f"repair_lock_clear: {str(summary.repair_lock_clear).lower()}",
        f"regression_allowed: {str(summary.regression_allowed).lower()}",
        (
            "production_root_hard_deny: "
            f"{str(summary.production_root_hard_deny).lower()}"
        ),
        (
            "production_execution_allowed: "
            f"{str(summary.production_execution_allowed).lower()}"
        ),
        f"execution_scope: {summary.execution_scope}",
        f"failed_checks: {summary.failed_checks}",
        f"recommended_action: {summary.recommended_action}",
    ]
    return "\n".join(lines)
