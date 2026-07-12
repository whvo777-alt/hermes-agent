"""Dispatch consume repair — Phase 12N eligibility / 12O prepared cleanup / 12P partial forward-complete.

Dry-run eligibility is read-only. Prepared cleanup mutates only the consume
transaction record (tombstone) plus append-only repair audit records. Partial
forward-complete additionally consumes the missing confirmation side — it never
rolls back an already-consumed artifact.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from agent.coo.dispatch_cli_consume_recovery import (
    CooDispatchConsumeRecoveryAssessment,
    assess_dispatch_consume_recovery,
)
from agent.coo.dispatch_consume_repair_audit import (
    CooDispatchConsumeRepairAuditRecord,
    append_consume_repair_audit,
)
from agent.coo.dispatch_consume_repair_lock import (
    DispatchConsumeRepairLockError,
    consume_repair_pair_lock,
)
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_LEGACY_COMMITTED,
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_UNCONSUMED,
    DispatchConsumeTransactionError,
    abort_prepared_consume_transaction,
    assess_consume_status,
    complete_partial_consume_transaction,
    read_consume_transaction,
)
from agent.coo.production_executor_confirmation import (
    mark_confirmation_consumed_file,
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

REQUIRED_CONSUME_REPAIR_PHRASE = "CONFIRM-CONSUME-REPAIR"


@dataclass(frozen=True)
class CooDispatchConsumeRepairApplyResult:
    """Safe apply summary for consume repair actions."""

    repair_attempt_id: str
    repair_action: str
    consume_state_before: str
    consume_state_after: str
    applied: bool
    bundle_consumed: bool
    confirmation_consumed: bool
    recovery_required: bool
    phrase_verified: bool
    operator_id: str
    correlation_valid: bool = False
    evidence_success: bool = False
    execution_attempt_id: str = ""


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


def validate_consume_repair_phrase(phrase: str) -> bool:
    """Return whether the operator repair phrase matches exactly."""
    return isinstance(phrase, str) and phrase == REQUIRED_CONSUME_REPAIR_PHRASE


def apply_prepared_transaction_cleanup(
    *,
    ticket_id: str,
    confirmation_id: str,
    operator_id: str,
    operator_name: str,
    reason: str,
    phrase: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    audit_dir: Path | None = None,
    evidence_dir: Path | None = None,
    repair_audit_dir: Path | None = None,
) -> CooDispatchConsumeRepairApplyResult:
    """Abort a stale prepared consume transaction without touching artifacts."""
    operator_valid = validate_repair_operator_fields(
        operator_id=operator_id,
        operator_name=operator_name,
        reason=reason,
    )
    if not operator_valid:
        raise ValueError("operator_id, operator_name, and reason are required")

    phrase_verified = validate_consume_repair_phrase(phrase)
    if not phrase_verified:
        raise ValueError(
            f"phrase must equal {REQUIRED_CONSUME_REPAIR_PHRASE!r}"
        )

    resolved_transaction_dir = transaction_dir
    if resolved_transaction_dir is None:
        from agent.coo.dispatch_consume_transaction import default_consume_transaction_dir

        resolved_transaction_dir = default_consume_transaction_dir()

    try:
        with consume_repair_pair_lock(
            ticket_id,
            confirmation_id,
            transaction_dir=resolved_transaction_dir,
        ):
            return _apply_prepared_transaction_cleanup_locked(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                operator_id=operator_id,
                operator_name=operator_name,
                reason=reason,
                bundle_dir=bundle_dir,
                confirmation_dir=confirmation_dir,
                transaction_dir=transaction_dir,
                audit_dir=audit_dir,
                evidence_dir=evidence_dir,
                repair_audit_dir=repair_audit_dir,
            )
    except DispatchConsumeRepairLockError as exc:
        raise ValueError(str(exc)) from exc


def _apply_prepared_transaction_cleanup_locked(
    *,
    ticket_id: str,
    confirmation_id: str,
    operator_id: str,
    operator_name: str,
    reason: str,
    bundle_dir: Path | None,
    confirmation_dir: Path | None,
    transaction_dir: Path | None,
    audit_dir: Path | None,
    evidence_dir: Path | None,
    repair_audit_dir: Path | None,
) -> CooDispatchConsumeRepairApplyResult:
    """Apply prepared cleanup while holding the pair repair lock."""
    eligibility = evaluate_consume_repair_eligibility(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        operator_id=operator_id,
        operator_name=operator_name,
        reason=reason,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        audit_dir=audit_dir,
        evidence_dir=evidence_dir,
    )
    if not eligibility.repair_eligible:
        raise ValueError(
            "Dispatch consume repair apply is not eligible for this pair."
        )
    if eligibility.repair_action != REPAIR_ACTION_PREPARED_CLEANUP:
        raise ValueError(
            "Dispatch consume repair apply only supports prepared cleanup."
        )
    if eligibility.consume_state != CONSUME_STATE_PREPARED:
        raise ValueError(
            f"Dispatch consume repair apply requires prepared state, "
            f"not {eligibility.consume_state!r}."
        )

    try:
        prepared = read_consume_transaction(
            ticket_id,
            confirmation_id,
            transaction_dir=transaction_dir,
        )
    except DispatchConsumeTransactionError as exc:
        raise ValueError(str(exc)) from exc
    if prepared is None:
        raise ValueError("Consume transaction record is missing.")
    if prepared.state != CONSUME_STATE_PREPARED:
        raise ValueError("Consume transaction record is not prepared.")
    if (
        prepared.ticket_id != ticket_id.strip()
        or prepared.confirmation_id != confirmation_id.strip()
    ):
        raise ValueError("Consume transaction record ids do not match lookup keys.")
    if prepared.transaction_id != eligibility.transaction_id:
        raise ValueError("Consume transaction id does not match recovery assessment.")
    if prepared.bundle_consumed or prepared.confirmation_consumed:
        raise ValueError(
            "Prepared cleanup requires both bundle and confirmation unconsumed."
        )

    repair_attempt_id = str(uuid.uuid4())
    consume_state_before = eligibility.consume_state
    try:
        abort_prepared_consume_transaction(
            prepared=prepared,
            repair_attempt_id=repair_attempt_id,
            repair_action=REPAIR_ACTION_PREPARED_CLEANUP,
            operator_id=operator_id.strip(),
            reason=reason.strip(),
            transaction_dir=transaction_dir,
        )
    except (DispatchConsumeTransactionError, OSError) as exc:
        raise ValueError(str(exc)) from exc

    append_consume_repair_audit(
        CooDispatchConsumeRepairAuditRecord(
            repair_attempt_id=repair_attempt_id,
            repair_action=REPAIR_ACTION_PREPARED_CLEANUP,
            ticket_id=prepared.ticket_id,
            confirmation_id=prepared.confirmation_id,
            transaction_id=prepared.transaction_id,
            execution_attempt_id=prepared.execution_attempt_id,
            consume_state_before=consume_state_before,
            consume_state_after=CONSUME_STATE_UNCONSUMED,
            operator_id=operator_id.strip(),
            operator_name=operator_name.strip(),
            reason=reason.strip(),
            phrase_verified=True,
            applied_at=_repair_applied_at(),
        ),
        audit_dir=repair_audit_dir,
    )

    after_status = assess_consume_status(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
    )
    return CooDispatchConsumeRepairApplyResult(
        repair_attempt_id=repair_attempt_id,
        repair_action=REPAIR_ACTION_PREPARED_CLEANUP,
        consume_state_before=consume_state_before,
        consume_state_after=after_status.consume_state,
        applied=True,
        bundle_consumed=after_status.bundle_consumed,
        confirmation_consumed=after_status.confirmation_consumed,
        recovery_required=after_status.recovery_required,
        phrase_verified=True,
        operator_id=operator_id.strip(),
        correlation_valid=eligibility.correlation_valid,
        evidence_success=eligibility.evidence_success,
        execution_attempt_id=prepared.execution_attempt_id,
    )


def apply_consume_repair(
    *,
    ticket_id: str,
    confirmation_id: str,
    operator_id: str,
    operator_name: str,
    reason: str,
    phrase: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    audit_dir: Path | None = None,
    evidence_dir: Path | None = None,
    repair_audit_dir: Path | None = None,
) -> CooDispatchConsumeRepairApplyResult:
    """Apply the repair action selected by dry-run eligibility.

    Dispatches to prepared cleanup or partial forward-complete; every other
    state fails closed with zero mutation.
    """
    operator_valid = validate_repair_operator_fields(
        operator_id=operator_id,
        operator_name=operator_name,
        reason=reason,
    )
    if not operator_valid:
        raise ValueError("operator_id, operator_name, and reason are required")

    phrase_verified = validate_consume_repair_phrase(phrase)
    if not phrase_verified:
        raise ValueError(
            f"phrase must equal {REQUIRED_CONSUME_REPAIR_PHRASE!r}"
        )

    resolved_transaction_dir = transaction_dir
    if resolved_transaction_dir is None:
        from agent.coo.dispatch_consume_transaction import default_consume_transaction_dir

        resolved_transaction_dir = default_consume_transaction_dir()

    try:
        with consume_repair_pair_lock(
            ticket_id,
            confirmation_id,
            transaction_dir=resolved_transaction_dir,
        ):
            eligibility = evaluate_consume_repair_eligibility(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                operator_id=operator_id,
                operator_name=operator_name,
                reason=reason,
                bundle_dir=bundle_dir,
                confirmation_dir=confirmation_dir,
                transaction_dir=transaction_dir,
                audit_dir=audit_dir,
                evidence_dir=evidence_dir,
            )
            if not eligibility.repair_eligible:
                raise ValueError(
                    "Dispatch consume repair apply is not eligible for this pair."
                )
            if eligibility.repair_action == REPAIR_ACTION_PREPARED_CLEANUP:
                return _apply_prepared_transaction_cleanup_locked(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    operator_id=operator_id,
                    operator_name=operator_name,
                    reason=reason,
                    bundle_dir=bundle_dir,
                    confirmation_dir=confirmation_dir,
                    transaction_dir=transaction_dir,
                    audit_dir=audit_dir,
                    evidence_dir=evidence_dir,
                    repair_audit_dir=repair_audit_dir,
                )
            if eligibility.repair_action == REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE:
                return _apply_partial_forward_complete_locked(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    operator_id=operator_id,
                    operator_name=operator_name,
                    reason=reason,
                    bundle_dir=bundle_dir,
                    confirmation_dir=confirmation_dir,
                    transaction_dir=transaction_dir,
                    audit_dir=audit_dir,
                    evidence_dir=evidence_dir,
                    repair_audit_dir=repair_audit_dir,
                )
            raise ValueError(
                f"Dispatch consume repair action {eligibility.repair_action!r} "
                "cannot be applied."
            )
    except DispatchConsumeRepairLockError as exc:
        raise ValueError(str(exc)) from exc


def apply_partial_forward_complete(
    *,
    ticket_id: str,
    confirmation_id: str,
    operator_id: str,
    operator_name: str,
    reason: str,
    phrase: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    audit_dir: Path | None = None,
    evidence_dir: Path | None = None,
    repair_audit_dir: Path | None = None,
) -> CooDispatchConsumeRepairApplyResult:
    """Forward-complete a verified partial consume pair (confirmation side only)."""
    operator_valid = validate_repair_operator_fields(
        operator_id=operator_id,
        operator_name=operator_name,
        reason=reason,
    )
    if not operator_valid:
        raise ValueError("operator_id, operator_name, and reason are required")

    phrase_verified = validate_consume_repair_phrase(phrase)
    if not phrase_verified:
        raise ValueError(
            f"phrase must equal {REQUIRED_CONSUME_REPAIR_PHRASE!r}"
        )

    resolved_transaction_dir = transaction_dir
    if resolved_transaction_dir is None:
        from agent.coo.dispatch_consume_transaction import default_consume_transaction_dir

        resolved_transaction_dir = default_consume_transaction_dir()

    try:
        with consume_repair_pair_lock(
            ticket_id,
            confirmation_id,
            transaction_dir=resolved_transaction_dir,
        ):
            return _apply_partial_forward_complete_locked(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                operator_id=operator_id,
                operator_name=operator_name,
                reason=reason,
                bundle_dir=bundle_dir,
                confirmation_dir=confirmation_dir,
                transaction_dir=transaction_dir,
                audit_dir=audit_dir,
                evidence_dir=evidence_dir,
                repair_audit_dir=repair_audit_dir,
            )
    except DispatchConsumeRepairLockError as exc:
        raise ValueError(str(exc)) from exc


def _apply_partial_forward_complete_locked(
    *,
    ticket_id: str,
    confirmation_id: str,
    operator_id: str,
    operator_name: str,
    reason: str,
    bundle_dir: Path | None,
    confirmation_dir: Path | None,
    transaction_dir: Path | None,
    audit_dir: Path | None,
    evidence_dir: Path | None,
    repair_audit_dir: Path | None,
) -> CooDispatchConsumeRepairApplyResult:
    """Apply partial forward-complete while holding the pair repair lock."""
    eligibility = evaluate_consume_repair_eligibility(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        operator_id=operator_id,
        operator_name=operator_name,
        reason=reason,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        audit_dir=audit_dir,
        evidence_dir=evidence_dir,
    )
    if not eligibility.repair_eligible:
        raise ValueError(
            "Dispatch consume repair apply is not eligible for this pair."
        )
    if eligibility.repair_action != REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE:
        raise ValueError(
            "Dispatch consume repair apply only supports partial forward-complete here."
        )
    if eligibility.consume_state != CONSUME_STATE_PARTIAL:
        raise ValueError(
            f"Dispatch consume repair apply requires partial state, "
            f"not {eligibility.consume_state!r}."
        )
    if not eligibility.bundle_consumed or eligibility.confirmation_consumed:
        raise ValueError(
            "Partial forward-complete requires bundle consumed and confirmation unconsumed."
        )
    if not (
        eligibility.audit_present
        and eligibility.evidence_present
        and eligibility.correlation_valid
        and eligibility.evidence_success
    ):
        raise ValueError(
            "Partial forward-complete requires verified success audit and evidence."
        )

    try:
        partial = read_consume_transaction(
            ticket_id,
            confirmation_id,
            transaction_dir=transaction_dir,
        )
    except DispatchConsumeTransactionError as exc:
        raise ValueError(str(exc)) from exc
    if partial is None:
        raise ValueError("Consume transaction record is missing.")
    if partial.state not in (CONSUME_STATE_PARTIAL, CONSUME_STATE_PREPARED):
        raise ValueError("Consume transaction record is not partial.")
    if partial.state == CONSUME_STATE_PREPARED and partial.confirmation_consumed:
        raise ValueError("Consume transaction record conflicts with partial state.")
    if (
        partial.ticket_id != ticket_id.strip()
        or partial.confirmation_id != confirmation_id.strip()
    ):
        raise ValueError("Consume transaction record ids do not match lookup keys.")
    if partial.transaction_id != eligibility.transaction_id:
        raise ValueError("Consume transaction id does not match recovery assessment.")
    if partial.execution_attempt_id != eligibility.execution_attempt_id:
        raise ValueError(
            "Consume transaction execution_attempt_id does not match recovery assessment."
        )

    repair_attempt_id = str(uuid.uuid4())
    consume_state_before = eligibility.consume_state

    try:
        mark_confirmation_consumed_file(
            confirmation_id.strip(),
            confirmation_dir=confirmation_dir,
        )
    except (ValueError, OSError, KeyError) as exc:
        raise ValueError(
            "Partial forward-complete confirmation consume failed; "
            "transaction remains partial and no state was changed."
        ) from exc

    # Normalize the on-disk record shape for the committed transition. A
    # stale "prepared" record that derives to partial (bundle consumed only)
    # is rewritten through the same partial contract before commit.
    partial_record = partial
    if partial_record.state != CONSUME_STATE_PARTIAL or not partial_record.bundle_consumed:
        from agent.coo.dispatch_consume_transaction import DispatchConsumeTransaction

        partial_record = DispatchConsumeTransaction(
            transaction_id=partial.transaction_id,
            execution_attempt_id=partial.execution_attempt_id,
            ticket_id=partial.ticket_id,
            confirmation_id=partial.confirmation_id,
            state=CONSUME_STATE_PARTIAL,
            prepared_at=partial.prepared_at,
            partial_at=partial.partial_at,
            bundle_consumed=True,
            confirmation_consumed=False,
            failure_reason=partial.failure_reason,
        )

    try:
        complete_partial_consume_transaction(
            partial=partial_record,
            repair_attempt_id=repair_attempt_id,
            repair_action=REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE,
            operator_id=operator_id.strip(),
            reason=reason.strip(),
            transaction_dir=transaction_dir,
        )
    except (DispatchConsumeTransactionError, OSError) as exc:
        # Confirmation is now consumed but the transaction record still says
        # partial. Never roll back the consume; record the failure explicitly
        # and fail closed so an operator resolves the inconsistency.
        append_consume_repair_audit(
            CooDispatchConsumeRepairAuditRecord(
                repair_attempt_id=repair_attempt_id,
                repair_action=REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE,
                ticket_id=partial.ticket_id,
                confirmation_id=partial.confirmation_id,
                transaction_id=partial.transaction_id,
                execution_attempt_id=partial.execution_attempt_id,
                consume_state_before=consume_state_before,
                consume_state_after="recovery_required",
                operator_id=operator_id.strip(),
                operator_name=operator_name.strip(),
                reason=reason.strip(),
                phrase_verified=True,
                applied_at=_repair_applied_at(),
                outcome="failed",
                correlation_valid=eligibility.correlation_valid,
                evidence_success=eligibility.evidence_success,
            ),
            audit_dir=repair_audit_dir,
        )
        raise ValueError(
            "Partial forward-complete consumed the confirmation but failed to "
            "commit the transaction record; manual recovery is required."
        ) from exc

    append_consume_repair_audit(
        CooDispatchConsumeRepairAuditRecord(
            repair_attempt_id=repair_attempt_id,
            repair_action=REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE,
            ticket_id=partial.ticket_id,
            confirmation_id=partial.confirmation_id,
            transaction_id=partial.transaction_id,
            execution_attempt_id=partial.execution_attempt_id,
            consume_state_before=consume_state_before,
            consume_state_after=CONSUME_STATE_COMMITTED,
            operator_id=operator_id.strip(),
            operator_name=operator_name.strip(),
            reason=reason.strip(),
            phrase_verified=True,
            applied_at=_repair_applied_at(),
            outcome="applied",
            correlation_valid=eligibility.correlation_valid,
            evidence_success=eligibility.evidence_success,
        ),
        audit_dir=repair_audit_dir,
    )

    after_status = assess_consume_status(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
    )
    if after_status.consume_state != CONSUME_STATE_COMMITTED:
        raise ValueError(
            "Partial forward-complete did not reach committed state; "
            "manual recovery is required."
        )
    return CooDispatchConsumeRepairApplyResult(
        repair_attempt_id=repair_attempt_id,
        repair_action=REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE,
        consume_state_before=consume_state_before,
        consume_state_after=after_status.consume_state,
        applied=True,
        bundle_consumed=after_status.bundle_consumed,
        confirmation_consumed=after_status.confirmation_consumed,
        recovery_required=after_status.recovery_required,
        phrase_verified=True,
        operator_id=operator_id.strip(),
        correlation_valid=eligibility.correlation_valid,
        evidence_success=eligibility.evidence_success,
        execution_attempt_id=partial.execution_attempt_id,
    )


def _repair_applied_at() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
