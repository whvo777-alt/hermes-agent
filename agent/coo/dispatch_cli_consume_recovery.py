"""CLI dispatch consume recovery assessment — Phase 12L.

Read-only operator runbook for partial/stale/legacy consume states.
No writes, repair, rollback, subprocess, or path/secret disclosure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.coo.dispatch_cli_evidence import (
    _find_audit_for_attempt,
    _load_evidence_meta,
    _normalize_execution_attempt_id,
    _normalize_ticket_id,
    _audit_ticket_id,
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
    assess_consume_status,
)
from agent.coo.dispatch_execution_audit import default_audit_dir
from agent.coo.production_executor_factory import default_evidence_dir

RECOMMENDED_ACTION_RETRY_ALLOWED = "retry_allowed"
RECOMMENDED_ACTION_NONE = "none"
RECOMMENDED_ACTION_INSPECT_STALE_TRANSACTION = "inspect_stale_transaction"
RECOMMENDED_ACTION_MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"

_KNOWN_CONSUME_STATES = frozenset(
    {
        CONSUME_STATE_UNCONSUMED,
        CONSUME_STATE_PREPARED,
        CONSUME_STATE_COMMITTED,
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_LEGACY_COMMITTED,
        CONSUME_STATE_LEGACY_PARTIAL,
        CONSUME_STATE_RECOVERY_REQUIRED,
    }
)


@dataclass(frozen=True)
class CooDispatchConsumeRecoveryAssessment:
    """Safe read-only recovery assessment for a consume pair."""

    consume_state: str
    recovery_required: bool
    recommended_action: str
    transaction_id: str
    execution_attempt_id: str
    bundle_consumed: bool
    confirmation_consumed: bool
    audit_present: bool
    evidence_present: bool
    correlation_valid: bool
    retry_allowed: bool
    recovery_risk: bool
    evidence_success: bool = False
    repair_audit_present: bool = False
    repair_attempt_id: str = ""


def _recovery_required_for_state(consume_state: str, status_recovery_required: bool) -> bool:
    if consume_state == CONSUME_STATE_PREPARED:
        return True
    return status_recovery_required


def _recommended_action_for_state(consume_state: str) -> str:
    if consume_state == CONSUME_STATE_UNCONSUMED:
        return RECOMMENDED_ACTION_RETRY_ALLOWED
    if consume_state in {CONSUME_STATE_COMMITTED, CONSUME_STATE_LEGACY_COMMITTED}:
        return RECOMMENDED_ACTION_NONE
    if consume_state == CONSUME_STATE_PREPARED:
        return RECOMMENDED_ACTION_INSPECT_STALE_TRANSACTION
    if consume_state in {
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_LEGACY_PARTIAL,
        CONSUME_STATE_RECOVERY_REQUIRED,
    }:
        return RECOMMENDED_ACTION_MANUAL_RECOVERY_REQUIRED
    raise ValueError(f"Dispatch consume state {consume_state!r} is unknown.")


def _validate_confirmation_id(confirmation_id: str) -> str:
    normalized = (confirmation_id or "").strip()
    if not normalized:
        raise ValueError("confirmation_id is required")
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise ValueError("confirmation_id must not contain path separators.")
    return normalized


def _evidence_meta_present(
    execution_attempt_id: str,
    *,
    evidence_dir: Path,
) -> tuple[bool, int | None]:
    """Return evidence meta presence and exit_code when readable."""
    try:
        meta = _load_evidence_meta(execution_attempt_id, evidence_dir=evidence_dir)
    except KeyError:
        return False, None
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    raw_exit_code = meta.get("exit_code")
    if raw_exit_code is None:
        raise ValueError("Dispatch evidence meta exit_code is required.")
    return True, int(raw_exit_code)


def _correlate_execution_attempt(
    *,
    ticket_id: str,
    confirmation_id: str,
    execution_attempt_id: str,
    audit_dir: Path,
    evidence_dir: Path,
) -> tuple[bool, bool, bool, bool]:
    """Read-only audit/evidence correlation. Raises on mismatch."""
    normalized_attempt_id = _normalize_execution_attempt_id(execution_attempt_id)
    evidence_present, exit_code = _evidence_meta_present(
        normalized_attempt_id,
        evidence_dir=evidence_dir,
    )
    audit = _find_audit_for_attempt(normalized_attempt_id, audit_dir=audit_dir)
    audit_present = audit is not None
    if audit is not None:
        if audit.execution_attempt_id != normalized_attempt_id:
            raise ValueError("Dispatch audit execution_attempt_id mismatch.")
        if audit.confirmation_id != confirmation_id:
            raise ValueError("Dispatch audit confirmation_id mismatch.")
        audit_ticket_id = _audit_ticket_id(audit)
        if audit_ticket_id != ticket_id:
            raise ValueError("Dispatch audit ticket_id mismatch.")
    correlation_valid = audit_present and evidence_present
    evidence_success = (
        evidence_present and exit_code == 0 and audit_present
    )
    return audit_present, evidence_present, correlation_valid, evidence_success


def _recovery_risk(
    *,
    bundle_consumed: bool,
    confirmation_consumed: bool,
    execution_attempt_id: str,
    evidence_success: bool,
) -> bool:
    if not bundle_consumed and not confirmation_consumed:
        return False
    if not execution_attempt_id:
        return True
    return not evidence_success


def assess_dispatch_consume_recovery(
    *,
    ticket_id: str,
    confirmation_id: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    audit_dir: Path | None = None,
    evidence_dir: Path | None = None,
    repair_audit_dir: Path | None = None,
) -> CooDispatchConsumeRecoveryAssessment:
    """Build read-only recovery assessment for bundle + confirmation pair."""
    normalized_ticket_id = _normalize_ticket_id(ticket_id)
    normalized_confirmation_id = _validate_confirmation_id(confirmation_id)
    try:
        status = assess_consume_status(
            ticket_id=normalized_ticket_id,
            confirmation_id=normalized_confirmation_id,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            transaction_dir=transaction_dir,
            repair_audit_dir=repair_audit_dir,
        )
    except (DispatchConsumeTransactionError, KeyError, ValueError) as exc:
        raise ValueError(str(exc)) from exc

    if status.consume_state not in _KNOWN_CONSUME_STATES:
        raise ValueError(f"Dispatch consume state {status.consume_state!r} is unknown.")

    recommended_action = _recommended_action_for_state(status.consume_state)
    retry_allowed = status.consume_state == CONSUME_STATE_UNCONSUMED
    recovery_required = _recovery_required_for_state(
        status.consume_state,
        status.recovery_required,
    )

    audit_present = False
    evidence_present = False
    correlation_valid = True
    evidence_success = False
    repair_audit_present = False
    repair_attempt_id = status.repair_attempt_id

    if status.consume_state == CONSUME_STATE_RECOVERY_REQUIRED:
        repair_audit_present = bool(repair_attempt_id)

    if status.execution_attempt_id:
        resolved_audit_dir = audit_dir or default_audit_dir()
        resolved_evidence_dir = evidence_dir or default_evidence_dir()
        (
            audit_present,
            evidence_present,
            correlation_valid,
            evidence_success,
        ) = _correlate_execution_attempt(
            ticket_id=normalized_ticket_id,
            confirmation_id=normalized_confirmation_id,
            execution_attempt_id=status.execution_attempt_id,
            audit_dir=resolved_audit_dir,
            evidence_dir=resolved_evidence_dir,
        )

    recovery_risk_flag = _recovery_risk(
        bundle_consumed=status.bundle_consumed,
        confirmation_consumed=status.confirmation_consumed,
        execution_attempt_id=status.execution_attempt_id,
        evidence_success=evidence_success,
    )

    return CooDispatchConsumeRecoveryAssessment(
        consume_state=status.consume_state,
        recovery_required=recovery_required,
        recommended_action=recommended_action,
        transaction_id=status.transaction_id,
        execution_attempt_id=status.execution_attempt_id,
        bundle_consumed=status.bundle_consumed,
        confirmation_consumed=status.confirmation_consumed,
        audit_present=audit_present,
        evidence_present=evidence_present,
        correlation_valid=correlation_valid,
        retry_allowed=retry_allowed,
        recovery_risk=recovery_risk_flag,
        evidence_success=evidence_success,
        repair_audit_present=repair_audit_present,
        repair_attempt_id=repair_attempt_id,
    )


def format_dispatch_consume_recovery_assessment(
    assessment: CooDispatchConsumeRecoveryAssessment,
) -> str:
    """Format safe recovery assessment fields for CLI stdout."""
    lines = (
        f"consume_state: {assessment.consume_state}",
        f"recovery_required: {str(assessment.recovery_required).lower()}",
        f"recommended_action: {assessment.recommended_action}",
        f"transaction_id: {assessment.transaction_id or '(none)'}",
        f"execution_attempt_id: {assessment.execution_attempt_id or '(none)'}",
        f"bundle_consumed: {str(assessment.bundle_consumed).lower()}",
        f"confirmation_consumed: {str(assessment.confirmation_consumed).lower()}",
        f"audit_present: {str(assessment.audit_present).lower()}",
        f"evidence_present: {str(assessment.evidence_present).lower()}",
        f"correlation_valid: {str(assessment.correlation_valid).lower()}",
        f"retry_allowed: {str(assessment.retry_allowed).lower()}",
        f"recovery_risk: {str(assessment.recovery_risk).lower()}",
        f"repair_audit_present: {str(assessment.repair_audit_present).lower()}",
        f"repair_attempt_id: {assessment.repair_attempt_id or '(none)'}",
    )
    return "\n".join(lines)
