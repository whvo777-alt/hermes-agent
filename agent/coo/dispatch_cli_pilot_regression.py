"""CLI pilot operations regression summary — Phase 13B."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.coo.dispatch_pilot_history import (
    PILOT_STATUS_DRY_RUN,
    PILOT_STATUS_FAILURE,
    PILOT_STATUS_SUCCESS,
    PILOT_STATUS_TIMEOUT,
    CooDispatchPilotHistoryRecord,
    list_pilot_history_records,
)

REGRESSION_STATUS_PASS = "PASS"
REGRESSION_STATUS_WARN = "WARN"
REGRESSION_STATUS_FAIL = "FAIL"

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class CooDispatchPilotRegressionSummary:
    """Safe read-only pilot operations regression summary."""

    total_attempts: int
    completed_count: int
    failed_count: int
    timeout_count: int
    dry_run_count: int
    consumed_success_count: int
    latest_status: str
    latest_pilot_attempt_id: str
    consecutive_failures: int
    evidence_missing_count: int
    audit_missing_count: int
    production_policy_violations: int
    regression_status: str


def _policy_violation(record: CooDispatchPilotHistoryRecord) -> bool:
    return (
        record.production_execution_allowed
        or not record.production_root_hard_deny
        or record.gateway_enabled
    )


def _trailing_consecutive_failures(
    records: tuple[CooDispatchPilotHistoryRecord, ...],
) -> int:
    count = 0
    for record in records:
        if record.dry_run:
            continue
        if record.status in {PILOT_STATUS_FAILURE, PILOT_STATUS_TIMEOUT}:
            count += 1
            continue
        break
    return count


def _had_single_failure_before_latest_success(
    records: tuple[CooDispatchPilotHistoryRecord, ...],
) -> bool:
    if not records:
        return False
    latest = records[0]
    if latest.status != PILOT_STATUS_SUCCESS or latest.dry_run:
        return False
    non_dry = [record for record in records if not record.dry_run]
    if len(non_dry) < 2:
        return False
    previous = non_dry[1]
    return previous.status in {PILOT_STATUS_FAILURE, PILOT_STATUS_TIMEOUT}


def _success_integrity_failure(record: CooDispatchPilotHistoryRecord) -> bool:
    if record.status != PILOT_STATUS_SUCCESS or record.dry_run:
        return False
    return not (record.consumed and record.evidence_present and record.audit_present)


def evaluate_pilot_regression(
    *,
    ticket_id: str | None = None,
    limit: int | None = None,
    history_dir: Path | None = None,
) -> CooDispatchPilotRegressionSummary:
    """Evaluate read-only pilot operations regression from persisted history."""
    records = list_pilot_history_records(history_dir=history_dir, ticket_id=ticket_id)
    if limit is not None and limit > 0:
        records = records[:limit]

    total_attempts = len(records)
    completed_count = sum(1 for record in records if record.status == PILOT_STATUS_SUCCESS)
    failed_count = sum(1 for record in records if record.status == PILOT_STATUS_FAILURE)
    timeout_count = sum(1 for record in records if record.status == PILOT_STATUS_TIMEOUT)
    dry_run_count = sum(1 for record in records if record.dry_run)
    consumed_success_count = sum(
        1
        for record in records
        if record.status == PILOT_STATUS_SUCCESS and record.consumed
    )
    evidence_missing_count = sum(
        1
        for record in records
        if record.status == PILOT_STATUS_SUCCESS and not record.evidence_present
    )
    audit_missing_count = sum(
        1
        for record in records
        if record.status == PILOT_STATUS_SUCCESS and not record.audit_present
    )
    production_policy_violations = sum(1 for record in records if _policy_violation(record))
    consecutive_failures = _trailing_consecutive_failures(records)
    latest_status = records[0].status if records else _NONE_LABEL
    latest_pilot_attempt_id = records[0].pilot_attempt_id if records else _NONE_LABEL

    regression_status = REGRESSION_STATUS_WARN
    if production_policy_violations > 0:
        regression_status = REGRESSION_STATUS_FAIL
    elif consecutive_failures >= 2:
        regression_status = REGRESSION_STATUS_FAIL
    elif records and _success_integrity_failure(records[0]):
        regression_status = REGRESSION_STATUS_FAIL
    elif records and not records[0].dry_run and records[0].status in {
        PILOT_STATUS_FAILURE,
        PILOT_STATUS_TIMEOUT,
    }:
        regression_status = REGRESSION_STATUS_FAIL
    elif total_attempts == 0:
        regression_status = REGRESSION_STATUS_WARN
    elif dry_run_count == total_attempts:
        regression_status = REGRESSION_STATUS_WARN
    elif _had_single_failure_before_latest_success(records):
        regression_status = REGRESSION_STATUS_WARN
    elif (
        records
        and records[0].status == PILOT_STATUS_SUCCESS
        and not records[0].dry_run
        and records[0].evidence_present
        and records[0].audit_present
        and records[0].consumed
        and production_policy_violations == 0
    ):
        regression_status = REGRESSION_STATUS_PASS
    else:
        regression_status = REGRESSION_STATUS_WARN

    return CooDispatchPilotRegressionSummary(
        total_attempts=total_attempts,
        completed_count=completed_count,
        failed_count=failed_count,
        timeout_count=timeout_count,
        dry_run_count=dry_run_count,
        consumed_success_count=consumed_success_count,
        latest_status=latest_status,
        latest_pilot_attempt_id=latest_pilot_attempt_id,
        consecutive_failures=consecutive_failures,
        evidence_missing_count=evidence_missing_count,
        audit_missing_count=audit_missing_count,
        production_policy_violations=production_policy_violations,
        regression_status=regression_status,
    )


def format_pilot_regression_summary(summary: CooDispatchPilotRegressionSummary) -> str:
    """Format safe pilot regression fields for CLI stdout."""
    lines = [
        "Pilot Regression",
        "",
        f"regression_status: {summary.regression_status}",
        f"total_attempts: {summary.total_attempts}",
        f"completed_count: {summary.completed_count}",
        f"failed_count: {summary.failed_count}",
        f"timeout_count: {summary.timeout_count}",
        f"dry_run_count: {summary.dry_run_count}",
        f"consumed_success_count: {summary.consumed_success_count}",
        f"latest_status: {summary.latest_status}",
        f"latest_pilot_attempt_id: {summary.latest_pilot_attempt_id}",
        f"consecutive_failures: {summary.consecutive_failures}",
        f"evidence_missing_count: {summary.evidence_missing_count}",
        f"audit_missing_count: {summary.audit_missing_count}",
        (
            "production_policy_violations: "
            f"{summary.production_policy_violations}"
        ),
    ]
    return "\n".join(lines)
