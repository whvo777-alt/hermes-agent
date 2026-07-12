"""CLI dispatch operator runbook — Phase 12R.

Read-only cross-reference summary for operator next-step guidance.
No writes, repair, dispatch, subprocess, lock probe, or secret disclosure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_cli_consume_recovery import (
    CooDispatchConsumeRecoveryAssessment,
    assess_dispatch_consume_recovery,
)
from agent.coo.dispatch_cli_enablement import evaluate_dispatch_enablement
from agent.coo.dispatch_consume_repair import (
    REPAIR_ACTION_BLOCKED,
    REPAIR_ACTION_NOT_ALLOWED,
    REPAIR_ACTION_NOT_REQUIRED,
    REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE,
    REPAIR_ACTION_PREPARED_CLEANUP,
    CooDispatchConsumeRepairEligibility,
    _eligibility_from_recovery,
)
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_LEGACY_COMMITTED,
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_RECOVERY_REQUIRED,
    CONSUME_STATE_UNCONSUMED,
    DispatchConsumeTransactionError,
    assert_consume_replay_allowed,
)
from agent.coo.dispatch_runner_binding_state import (
    CooDispatchRunnerBindingState,
    load_dispatch_runner_binding_state,
)

RUNBOOK_ACTION_RETRY_DISPATCH = "retry_dispatch"
RUNBOOK_ACTION_MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"
RUNBOOK_ACTION_INSPECT_STALE_TRANSACTION = "inspect_stale_transaction"
RUNBOOK_ACTION_ALREADY_COMPLETED = "already_completed"
RUNBOOK_ACTION_REPAIR_NOT_REQUIRED = "repair_not_required"
RUNBOOK_ACTION_REPAIR_BLOCKED = "repair_blocked"

_DISPATCH_STATUS_READY = "ready_for_dispatch"
_DISPATCH_STATUS_BLOCKED = "dispatch_blocked"
_DISPATCH_STATUS_COMPLETED = "already_completed"

_ACTION_DESCRIPTIONS: dict[str, str] = {
    RUNBOOK_ACTION_RETRY_DISPATCH: (
        "Bundle and confirmation are unconsumed; dispatch run is permitted."
    ),
    RUNBOOK_ACTION_MANUAL_RECOVERY_REQUIRED: (
        "Consume state requires manual operator recovery before replay."
    ),
    RUNBOOK_ACTION_INSPECT_STALE_TRANSACTION: (
        "A prepared consume transaction exists; inspect before retrying dispatch."
    ),
    RUNBOOK_ACTION_ALREADY_COMPLETED: (
        "Bundle and confirmation are fully consumed; replay is not permitted."
    ),
    RUNBOOK_ACTION_REPAIR_NOT_REQUIRED: (
        "No consume repair action is required for this pair."
    ),
    RUNBOOK_ACTION_REPAIR_BLOCKED: (
        "Automated consume repair is blocked for this pair."
    ),
}


@dataclass(frozen=True)
class CooDispatchOperatorRunbookSummary:
    """Safe read-only operator runbook summary."""

    consume_state: str
    repair_state: str
    binding_state: str
    runner_provider: str
    dispatch_status: str
    execution_attempt_id: str
    repair_attempt_id: str
    audit_present: bool
    evidence_present: bool
    correlation_valid: bool
    evidence_success: bool
    retry_allowed: bool
    replay_allowed: bool
    recommended_action: str
    action_description: str


def _repair_state_label(repair: CooDispatchConsumeRepairEligibility) -> str:
    if repair.repair_action == REPAIR_ACTION_NOT_REQUIRED:
        return "repair_not_required"
    if repair.repair_action == REPAIR_ACTION_NOT_ALLOWED:
        return "repair_not_allowed"
    if repair.repair_action == REPAIR_ACTION_BLOCKED:
        return "repair_blocked"
    if repair.repair_action == REPAIR_ACTION_PREPARED_CLEANUP:
        return "repair_prepared_cleanup_available"
    if repair.repair_action == REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE:
        return "repair_partial_forward_complete_available"
    return repair.repair_action or "unknown"


def _dispatch_status(
    *,
    consume_state: str,
    replay_allowed: bool,
) -> str:
    if consume_state in {CONSUME_STATE_COMMITTED, CONSUME_STATE_LEGACY_COMMITTED}:
        return _DISPATCH_STATUS_COMPLETED
    if replay_allowed and consume_state == CONSUME_STATE_UNCONSUMED:
        return _DISPATCH_STATUS_READY
    return _DISPATCH_STATUS_BLOCKED


def _operator_recommended_action(
    recovery: CooDispatchConsumeRecoveryAssessment,
    repair: CooDispatchConsumeRepairEligibility,
) -> str:
    state = recovery.consume_state
    if state == CONSUME_STATE_UNCONSUMED:
        return RUNBOOK_ACTION_RETRY_DISPATCH
    if state in {CONSUME_STATE_COMMITTED, CONSUME_STATE_LEGACY_COMMITTED}:
        return RUNBOOK_ACTION_ALREADY_COMPLETED
    if state == CONSUME_STATE_PREPARED:
        return RUNBOOK_ACTION_INSPECT_STALE_TRANSACTION
    if state in {
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_LEGACY_PARTIAL,
        CONSUME_STATE_RECOVERY_REQUIRED,
    }:
        return RUNBOOK_ACTION_MANUAL_RECOVERY_REQUIRED
    if repair.repair_action == REPAIR_ACTION_NOT_REQUIRED:
        return RUNBOOK_ACTION_REPAIR_NOT_REQUIRED
    if repair.repair_action == REPAIR_ACTION_BLOCKED:
        return RUNBOOK_ACTION_REPAIR_BLOCKED
    return RUNBOOK_ACTION_MANUAL_RECOVERY_REQUIRED


def _replay_allowed(
    *,
    ticket_id: str,
    confirmation_id: str,
    bundle_dir: Path | None,
    confirmation_dir: Path | None,
    transaction_dir: Path | None,
    repair_audit_dir: Path | None,
) -> bool:
    try:
        assert_consume_replay_allowed(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            transaction_dir=transaction_dir,
            repair_audit_dir=repair_audit_dir,
        )
    except (DispatchConsumeTransactionError, ValueError):
        return False
    return True


def _resolve_binding_state(
    binding_state: CooDispatchRunnerBindingState | None,
) -> str:
    if binding_state is not None:
        return binding_state.state if binding_state.state_valid else "invalid"
    try:
        binding = load_dispatch_runner_binding_state()
    except ValueError:
        return "invalid"
    return binding.state if binding.state_valid else "invalid"


def _resolve_runner_provider(
    merged_config: Mapping[str, Any] | None,
    *,
    binding_state: CooDispatchRunnerBindingState | None,
) -> str:
    try:
        enablement = evaluate_dispatch_enablement(
            merged_config,
            binding_state=binding_state,
        )
    except ValueError:
        return "unknown"
    if enablement.runner_provider_mode:
        return enablement.runner_provider_mode
    if enablement.runner_provider_configured:
        return "configured"
    return "none"


def summarize_dispatch_operator_runbook(
    *,
    ticket_id: str,
    confirmation_id: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    audit_dir: Path | None = None,
    evidence_dir: Path | None = None,
    repair_audit_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    binding_state: CooDispatchRunnerBindingState | None = None,
) -> CooDispatchOperatorRunbookSummary:
    """Build read-only operator runbook summary for a consume pair."""
    try:
        recovery = assess_dispatch_consume_recovery(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            transaction_dir=transaction_dir,
            audit_dir=audit_dir,
            evidence_dir=evidence_dir,
            repair_audit_dir=repair_audit_dir,
        )
    except (DispatchConsumeTransactionError, KeyError, ValueError) as exc:
        raise ValueError(str(exc)) from exc

    repair = _eligibility_from_recovery(recovery, operator_valid=False)
    replay_allowed = _replay_allowed(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        repair_audit_dir=repair_audit_dir,
    )
    recommended_action = _operator_recommended_action(recovery, repair)
    return CooDispatchOperatorRunbookSummary(
        consume_state=recovery.consume_state,
        repair_state=_repair_state_label(repair),
        binding_state=_resolve_binding_state(binding_state),
        runner_provider=_resolve_runner_provider(
            merged_config,
            binding_state=binding_state,
        ),
        dispatch_status=_dispatch_status(
            consume_state=recovery.consume_state,
            replay_allowed=replay_allowed,
        ),
        execution_attempt_id=recovery.execution_attempt_id,
        repair_attempt_id=recovery.repair_attempt_id,
        audit_present=recovery.audit_present,
        evidence_present=recovery.evidence_present,
        correlation_valid=recovery.correlation_valid,
        evidence_success=recovery.evidence_success,
        retry_allowed=recovery.retry_allowed,
        replay_allowed=replay_allowed,
        recommended_action=recommended_action,
        action_description=_ACTION_DESCRIPTIONS.get(
            recommended_action,
            "Review consume and repair state before proceeding.",
        ),
    )


def format_dispatch_operator_runbook(summary: CooDispatchOperatorRunbookSummary) -> str:
    """Format safe operator runbook fields for CLI stdout."""
    execution_attempt_id = summary.execution_attempt_id or "(none)"
    repair_attempt_id = summary.repair_attempt_id or "(none)"
    sections = (
        (
            "Dispatch Status",
            (
                f"consume_state: {summary.consume_state}",
                f"repair_state: {summary.repair_state}",
                f"binding_state: {summary.binding_state}",
                f"runner_provider: {summary.runner_provider}",
                f"dispatch_status: {summary.dispatch_status}",
            ),
        ),
        (
            "Execution",
            (
                f"execution_attempt_id: {execution_attempt_id}",
                f"repair_attempt_id: {repair_attempt_id}",
            ),
        ),
        (
            "Evidence",
            (
                f"audit_present: {str(summary.audit_present).lower()}",
                f"evidence_present: {str(summary.evidence_present).lower()}",
                f"correlation_valid: {str(summary.correlation_valid).lower()}",
                f"evidence_success: {str(summary.evidence_success).lower()}",
            ),
        ),
        (
            "Replay",
            (
                f"retry_allowed: {str(summary.retry_allowed).lower()}",
                f"replay_allowed: {str(summary.replay_allowed).lower()}",
            ),
        ),
        (
            "Operator Action",
            (
                f"recommended_action: {summary.recommended_action}",
                f"description: {summary.action_description}",
            ),
        ),
    )
    rendered: list[str] = []
    for title, lines in sections:
        rendered.append(title)
        rendered.append("-" * len(title))
        rendered.extend(lines)
        rendered.append("")
    return "\n".join(rendered).rstrip()
