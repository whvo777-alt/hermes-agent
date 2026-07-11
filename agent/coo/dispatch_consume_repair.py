"""Dispatch consume repair eligibility — Phase 12N.

Read-only dry-run evaluation of repair eligibility. No writes, locks,
audit records, or artifact/transaction mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.coo.dispatch_cli_consume_recovery import (
    CooDispatchConsumeRecoveryAssessment,
    assess_dispatch_consume_recovery,
)
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_LEGACY_COMMITTED,
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_UNCONSUMED,
)

REPAIR_ACTION_NOT_REQUIRED = "repair_not_required"
REPAIR_ACTION_NOT_ALLOWED = "repair_not_allowed"
REPAIR_ACTION_PREPARED_CLEANUP = "repair_action_prepared_cleanup"
REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE = "repair_action_partial_forward_complete"
REPAIR_ACTION_BLOCKED = "repair_action_blocked"

BLOCKED_RETRY_DISPATCH_INSTEAD = "retry_dispatch_instead"
BLOCKED_ALREADY_COMMITTED = "already_committed"
BLOCKED_LEGACY_ALREADY_COMMITTED = "legacy_already_committed"
BLOCKED_LEGACY_PARTIAL_MANUAL_ONLY = "legacy_partial_manual_only"
BLOCKED_PREPARED_ARTIFACT_MISMATCH = "prepared_artifact_mismatch"
BLOCKED_MISSING_AUDIT_FOR_PARTIAL = "missing_audit_for_partial"
BLOCKED_MISSING_EVIDENCE_FOR_PARTIAL = "missing_evidence_for_partial"
BLOCKED_CORRELATION_INVALID = "correlation_invalid"
BLOCKED_EVIDENCE_NOT_SUCCESSFUL = "evidence_not_successful"


@dataclass(frozen=True)
class CooDispatchConsumeRepairEligibility:
    """Read-only repair dry-run eligibility summary."""

    consume_state: str
    repair_eligible: bool
    repair_action: str
    blocked_reason: str
    transaction_id: str
    execution_attempt_id: str
    bundle_consumed: bool
    confirmation_consumed: bool
    audit_present: bool
    evidence_present: bool
    correlation_valid: bool
    evidence_success: bool
    operator_valid: bool
    mutation_planned: bool = False


def validate_repair_operator_fields(
    *,
    operator_id: str,
    operator_name: str,
    reason: str,
) -> bool:
    """Return whether operator dry-run fields are present."""
    return (
        isinstance(operator_id, str)
        and operator_id.strip() != ""
        and isinstance(operator_name, str)
        and operator_name.strip() != ""
        and isinstance(reason, str)
        and reason.strip() != ""
    )


def _partial_blocked_reason(assessment: CooDispatchConsumeRecoveryAssessment) -> str:
    if not assessment.audit_present:
        return BLOCKED_MISSING_AUDIT_FOR_PARTIAL
    if not assessment.evidence_present:
        return BLOCKED_MISSING_EVIDENCE_FOR_PARTIAL
    if not assessment.correlation_valid:
        return BLOCKED_CORRELATION_INVALID
    if not assessment.evidence_success:
        return BLOCKED_EVIDENCE_NOT_SUCCESSFUL
    return BLOCKED_EVIDENCE_NOT_SUCCESSFUL


def _eligibility_from_recovery(
    assessment: CooDispatchConsumeRecoveryAssessment,
    *,
    operator_valid: bool,
) -> CooDispatchConsumeRepairEligibility:
    state = assessment.consume_state
    blocked_reason = ""
    repair_action = REPAIR_ACTION_BLOCKED
    repair_eligible = False

    if state == CONSUME_STATE_UNCONSUMED:
        repair_action = REPAIR_ACTION_NOT_REQUIRED
        blocked_reason = BLOCKED_RETRY_DISPATCH_INSTEAD
    elif state == CONSUME_STATE_COMMITTED:
        repair_action = REPAIR_ACTION_NOT_ALLOWED
        blocked_reason = BLOCKED_ALREADY_COMMITTED
    elif state == CONSUME_STATE_LEGACY_COMMITTED:
        repair_action = REPAIR_ACTION_NOT_ALLOWED
        blocked_reason = BLOCKED_LEGACY_ALREADY_COMMITTED
    elif state == CONSUME_STATE_LEGACY_PARTIAL:
        repair_action = REPAIR_ACTION_BLOCKED
        blocked_reason = BLOCKED_LEGACY_PARTIAL_MANUAL_ONLY
    elif state == CONSUME_STATE_PREPARED:
        if assessment.bundle_consumed or assessment.confirmation_consumed:
            repair_action = REPAIR_ACTION_BLOCKED
            blocked_reason = BLOCKED_PREPARED_ARTIFACT_MISMATCH
        else:
            repair_action = REPAIR_ACTION_PREPARED_CLEANUP
            repair_eligible = operator_valid
    elif state == CONSUME_STATE_PARTIAL:
        if (
            assessment.bundle_consumed
            and not assessment.confirmation_consumed
            and assessment.audit_present
            and assessment.evidence_present
            and assessment.correlation_valid
            and assessment.evidence_success
        ):
            repair_action = REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE
            repair_eligible = operator_valid
        else:
            repair_action = REPAIR_ACTION_BLOCKED
            blocked_reason = _partial_blocked_reason(assessment)
    else:
        raise ValueError(f"Dispatch consume state {state!r} is unknown.")

    if not operator_valid and repair_eligible:
        repair_eligible = False

    return CooDispatchConsumeRepairEligibility(
        consume_state=state,
        repair_eligible=repair_eligible,
        repair_action=repair_action,
        blocked_reason=blocked_reason,
        transaction_id=assessment.transaction_id,
        execution_attempt_id=assessment.execution_attempt_id,
        bundle_consumed=assessment.bundle_consumed,
        confirmation_consumed=assessment.confirmation_consumed,
        audit_present=assessment.audit_present,
        evidence_present=assessment.evidence_present,
        correlation_valid=assessment.correlation_valid,
        evidence_success=assessment.evidence_success,
        operator_valid=operator_valid,
        mutation_planned=False,
    )


def evaluate_consume_repair_eligibility(
    *,
    ticket_id: str,
    confirmation_id: str,
    operator_id: str,
    operator_name: str,
    reason: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    audit_dir: Path | None = None,
    evidence_dir: Path | None = None,
) -> CooDispatchConsumeRepairEligibility:
    """Evaluate read-only repair dry-run eligibility for a consume pair."""
    operator_valid = validate_repair_operator_fields(
        operator_id=operator_id,
        operator_name=operator_name,
        reason=reason,
    )
    if not operator_valid:
        raise ValueError("operator_id, operator_name, and reason are required")

    assessment = assess_dispatch_consume_recovery(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        audit_dir=audit_dir,
        evidence_dir=evidence_dir,
    )
    return _eligibility_from_recovery(assessment, operator_valid=operator_valid)
