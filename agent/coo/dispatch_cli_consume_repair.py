"""CLI dispatch consume repair dry-run — Phase 12N.

Read-only repair eligibility output. No writes, apply path, or secrets.
"""

from __future__ import annotations

from pathlib import Path

from agent.coo.dispatch_consume_repair import (
    CooDispatchConsumeRepairEligibility,
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
