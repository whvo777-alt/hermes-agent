"""CLI dispatch consume repair audit read — Phase 12Q."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.coo.dispatch_consume_recovery_required import (
    FAILURE_REASON_TRANSACTION_COMMIT_FAILED,
)
from agent.coo.dispatch_consume_repair_audit import (
    CooDispatchConsumeRepairAuditRecord,
    list_consume_repair_audits,
    read_consume_repair_audit,
)


@dataclass(frozen=True)
class CooDispatchConsumeRepairAuditSummary:
    """Safe read-only repair audit summary."""

    repair_attempt_id: str
    repair_action: str
    outcome: str
    operator_id: str
    ticket_id: str
    confirmation_id: str
    execution_attempt_id: str
    consume_state_before: str
    consume_state_after: str
    correlation_valid: bool
    evidence_success: bool
    phrase_verified: bool
    failure_reason_code: str
    timestamp: str


def _failure_reason_code(record: CooDispatchConsumeRepairAuditRecord) -> str:
    if record.outcome == "applied":
        return "(none)"
    if (
        record.outcome == "failed"
        and record.consume_state_after == "recovery_required"
    ):
        return FAILURE_REASON_TRANSACTION_COMMIT_FAILED
    return "repair_failed"


def _to_summary(record: CooDispatchConsumeRepairAuditRecord) -> CooDispatchConsumeRepairAuditSummary:
    return CooDispatchConsumeRepairAuditSummary(
        repair_attempt_id=record.repair_attempt_id,
        repair_action=record.repair_action,
        outcome=record.outcome,
        operator_id=record.operator_id,
        ticket_id=record.ticket_id,
        confirmation_id=record.confirmation_id,
        execution_attempt_id=record.execution_attempt_id,
        consume_state_before=record.consume_state_before,
        consume_state_after=record.consume_state_after,
        correlation_valid=record.correlation_valid,
        evidence_success=record.evidence_success,
        phrase_verified=record.phrase_verified,
        failure_reason_code=_failure_reason_code(record),
        timestamp=record.applied_at,
    )


def summarize_consume_repair_audit(
    *,
    repair_attempt_id: str,
    audit_dir: Path | None = None,
) -> CooDispatchConsumeRepairAuditSummary:
    """Load and summarize one consume repair audit record."""
    try:
        record = read_consume_repair_audit(
            repair_attempt_id,
            audit_dir=audit_dir,
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    return _to_summary(record)


def list_consume_repair_audit_summaries(
    *,
    audit_dir: Path | None = None,
    ticket_id: str | None = None,
) -> list[CooDispatchConsumeRepairAuditSummary]:
    """List consume repair audit summaries newest-first."""
    return [
        _to_summary(record)
        for record in list_consume_repair_audits(
            audit_dir=audit_dir,
            ticket_id=ticket_id,
        )
    ]


def format_dispatch_consume_repair_audit_summary(
    summary: CooDispatchConsumeRepairAuditSummary,
) -> str:
    """Format safe repair audit fields for CLI stdout."""
    return "\n".join(
        (
            f"repair_attempt_id: {summary.repair_attempt_id}",
            f"repair_action: {summary.repair_action}",
            f"outcome: {summary.outcome}",
            f"operator_id: {summary.operator_id}",
            f"ticket_id: {summary.ticket_id}",
            f"confirmation_id: {summary.confirmation_id}",
            f"execution_attempt_id: {summary.execution_attempt_id}",
            f"consume_state_before: {summary.consume_state_before}",
            f"consume_state_after: {summary.consume_state_after}",
            f"correlation_valid: {str(summary.correlation_valid).lower()}",
            f"evidence_success: {str(summary.evidence_success).lower()}",
            f"phrase_verified: {str(summary.phrase_verified).lower()}",
            f"failure_reason_code: {summary.failure_reason_code}",
            f"timestamp: {summary.timestamp}",
        )
    )
