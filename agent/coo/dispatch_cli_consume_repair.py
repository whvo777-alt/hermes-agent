"""CLI dispatch consume repair dry-run and apply — Phase 12N / 12O / 12P."""

from __future__ import annotations

from pathlib import Path

from agent.coo.dispatch_consume_repair import (
    CooDispatchConsumeRepairApplyResult,
    CooDispatchConsumeRepairEligibility,
    apply_consume_repair,
    evaluate_consume_repair_eligibility,
    validate_repair_operator_fields,
)


def format_dispatch_consume_repair_eligibility(
    eligibility: CooDispatchConsumeRepairEligibility,
) -> str:
    """Format safe repair dry-run fields for CLI stdout."""
    blocked = eligibility.blocked_reason or "(none)"
    return "\n".join(
        (
            f"consume_state: {eligibility.consume_state}",
            f"repair_eligible: {str(eligibility.repair_eligible).lower()}",
            f"repair_action: {eligibility.repair_action}",
            f"blocked_reason: {blocked}",
            f"transaction_id: {eligibility.transaction_id or '(none)'}",
            f"execution_attempt_id: {eligibility.execution_attempt_id or '(none)'}",
            f"bundle_consumed: {str(eligibility.bundle_consumed).lower()}",
            f"confirmation_consumed: {str(eligibility.confirmation_consumed).lower()}",
            f"audit_present: {str(eligibility.audit_present).lower()}",
            f"evidence_present: {str(eligibility.evidence_present).lower()}",
            f"correlation_valid: {str(eligibility.correlation_valid).lower()}",
            f"evidence_success: {str(eligibility.evidence_success).lower()}",
            f"operator_valid: {str(eligibility.operator_valid).lower()}",
            f"mutation_planned: {str(eligibility.mutation_planned).lower()}",
        )
    )


def run_dispatch_consume_repair_dry_run(
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
) -> tuple[CooDispatchConsumeRepairEligibility, int]:
    """Return eligibility summary and CLI exit code."""
    if not validate_repair_operator_fields(
        operator_id=operator_id,
        operator_name=operator_name,
        reason=reason,
    ):
        raise ValueError("operator_id, operator_name, and reason are required")

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
    exit_code = 0 if eligibility.repair_eligible else 1
    return eligibility, exit_code


def format_dispatch_consume_repair_apply_result(
    result: CooDispatchConsumeRepairApplyResult,
) -> str:
    """Format safe repair apply fields for CLI stdout."""
    return "\n".join(
        (
            f"repair_attempt_id: {result.repair_attempt_id}",
            f"repair_action: {result.repair_action}",
            f"consume_state_before: {result.consume_state_before}",
            f"consume_state_after: {result.consume_state_after}",
            f"applied: {str(result.applied).lower()}",
            f"bundle_consumed: {str(result.bundle_consumed).lower()}",
            f"confirmation_consumed: {str(result.confirmation_consumed).lower()}",
            f"recovery_required: {str(result.recovery_required).lower()}",
            f"correlation_valid: {str(result.correlation_valid).lower()}",
            f"evidence_success: {str(result.evidence_success).lower()}",
            f"phrase_verified: {str(result.phrase_verified).lower()}",
            f"operator_id: {result.operator_id}",
            f"execution_attempt_id: {result.execution_attempt_id or '(none)'}",
        )
    )


def run_dispatch_consume_repair_apply(
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
) -> tuple[CooDispatchConsumeRepairApplyResult, int]:
    """Apply the eligible repair action and return safe summary + exit code."""
    result = apply_consume_repair(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        operator_id=operator_id,
        operator_name=operator_name,
        reason=reason,
        phrase=phrase,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        audit_dir=audit_dir,
        evidence_dir=evidence_dir,
        repair_audit_dir=repair_audit_dir,
    )
    exit_code = 0 if result.applied else 1
    return result, exit_code
