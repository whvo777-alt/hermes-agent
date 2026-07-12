"""CLI multi-ticket pilot fleet view — Phase 13D.

Read-only comparison of isolated operational pilot state across tickets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_cli_pilot import evaluate_pilot_readiness
from agent.coo.dispatch_cli_pilot_regression import (
    REGRESSION_STATUS_FAIL,
    REGRESSION_STATUS_PASS,
    REGRESSION_STATUS_WARN,
    evaluate_pilot_regression,
)
from agent.coo.dispatch_cli_pilot_runbook import (
    RECOMMENDED_ACTION_COLLECT_INITIAL_PILOT_HISTORY,
    RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE,
    RECOMMENDED_ACTION_MAINTAIN_PRODUCTION_BLOCK,
    RECOMMENDED_ACTION_RESOLVE_REGRESSION_FAILURE,
    RECOMMENDED_ACTION_RUN_ISOLATED_PILOT,
    RECOMMENDED_ACTION_RUN_PILOT_DRY_RUN,
    TREND_STATUS_DEGRADED,
    TREND_STATUS_INSUFFICIENT_DATA,
    TREND_STATUS_STABLE,
    _audit_integrity,
    _evidence_integrity,
    _last_success_present,
    _policy_violation,
    _resolve_recommended_action,
    evaluate_pilot_trend,
)
from agent.coo.dispatch_cli_production_signoff import evaluate_dispatch_production_signoff
from agent.coo.dispatch_pilot_history import (
    CooDispatchPilotHistoryRecord,
    list_pilot_history_records,
)

MAX_FLEET_TICKETS = 64
MAX_FLEET_LIMIT = 256

FLEET_STATUS_READY = "READY"
FLEET_STATUS_WARN = "WARN"
FLEET_STATUS_NOT_READY = "NOT_READY"

TICKET_DISPOSITION_READY = "READY"
TICKET_DISPOSITION_WARN = "WARN"
TICKET_DISPOSITION_FAILED = "FAILED"

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class CooDispatchPilotFleetTicketSummary:
    """Safe read-only pilot summary for one ticket."""

    ticket_id: str
    total_attempts: int
    latest_status: str
    latest_pilot_attempt_id: str
    regression_status: str
    trend_status: str
    consecutive_failures: int
    last_success_present: bool
    evidence_integrity: bool
    audit_integrity: bool
    production_policy_valid: bool
    recommended_action: str
    pilot_ready: bool
    ticket_disposition: str


@dataclass(frozen=True)
class CooDispatchPilotFleetSummary:
    """Safe read-only multi-ticket pilot fleet summary."""

    ticket_count: int
    ready_ticket_count: int
    warn_ticket_count: int
    failed_ticket_count: int
    stable_ticket_count: int
    degraded_ticket_count: int
    insufficient_data_count: int
    production_policy_violation_count: int
    fleet_status: str
    fleet_non_dry_success_present: bool
    tickets: tuple[CooDispatchPilotFleetTicketSummary, ...]


def _normalize_ticket_ids(ticket_ids: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if not ticket_ids:
        return ()
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in ticket_ids:
        ticket_id = (raw or "").strip()
        if not ticket_id or ticket_id in seen:
            continue
        seen.add(ticket_id)
        normalized.append(ticket_id)
    return tuple(normalized)


def _validate_fleet_limits(
    *,
    ticket_count: int,
    limit: int | None,
) -> None:
    if ticket_count > MAX_FLEET_TICKETS:
        raise ValueError(
            f"Pilot fleet supports at most {MAX_FLEET_TICKETS} tickets."
        )
    if limit is not None and limit > MAX_FLEET_LIMIT:
        raise ValueError(
            f"Pilot fleet limit supports at most {MAX_FLEET_LIMIT} records."
        )
    if limit is not None and limit <= 0:
        raise ValueError("Pilot fleet limit must be positive when provided.")


def _distinct_ticket_ids_from_history(
    records: tuple[CooDispatchPilotHistoryRecord, ...],
    *,
    max_tickets: int,
) -> tuple[str, ...]:
    latest_by_ticket: dict[str, str] = {}
    for record in records:
        if record.ticket_id not in latest_by_ticket:
            latest_by_ticket[record.ticket_id] = record.completed_at
            continue
        if record.completed_at > latest_by_ticket[record.ticket_id]:
            latest_by_ticket[record.ticket_id] = record.completed_at
    ordered = sorted(
        latest_by_ticket,
        key=lambda ticket_id: latest_by_ticket[ticket_id],
        reverse=True,
    )
    return tuple(ordered[:max_tickets])


def resolve_pilot_fleet_ticket_ids(
    *,
    ticket_ids: tuple[str, ...] | list[str] | None = None,
    history_dir: Path | None = None,
    max_tickets: int = MAX_FLEET_TICKETS,
) -> tuple[str, ...]:
    """Resolve fleet ticket ids from explicit input or recent history."""
    explicit = _normalize_ticket_ids(ticket_ids)
    if explicit:
        if len(explicit) > max_tickets:
            raise ValueError(
                f"Pilot fleet supports at most {max_tickets} tickets."
            )
        return explicit
    records = list_pilot_history_records(history_dir=history_dir)
    return _distinct_ticket_ids_from_history(records, max_tickets=max_tickets)


def _production_policy_valid_for_ticket(
    *,
    signoff_production_root_hard_deny: bool,
    signoff_gateway_enabled: bool,
    signoff_execution_allowed: bool,
    records: tuple[CooDispatchPilotHistoryRecord, ...],
    regression_production_policy_violations: int,
) -> bool:
    if (
        not signoff_production_root_hard_deny
        or signoff_gateway_enabled
        or signoff_execution_allowed
        or regression_production_policy_violations > 0
    ):
        return False
    return not any(_policy_violation(record) for record in records)


def _ticket_disposition(
    *,
    regression_status: str,
    trend_status: str,
    consecutive_failures: int,
    last_success_present: bool,
    production_policy_valid: bool,
    evidence_integrity: bool,
    audit_integrity: bool,
    total_attempts: int,
    dry_run_count: int,
) -> str:
    if not production_policy_valid or not evidence_integrity or not audit_integrity:
        return TICKET_DISPOSITION_FAILED
    if regression_status == REGRESSION_STATUS_FAIL:
        return TICKET_DISPOSITION_FAILED
    if (
        trend_status == TREND_STATUS_DEGRADED
        and consecutive_failures > 0
        and not last_success_present
    ):
        return TICKET_DISPOSITION_FAILED
    if total_attempts == 0 or dry_run_count == total_attempts:
        return TICKET_DISPOSITION_WARN
    if trend_status == TREND_STATUS_INSUFFICIENT_DATA:
        return TICKET_DISPOSITION_WARN
    if regression_status == REGRESSION_STATUS_WARN:
        return TICKET_DISPOSITION_WARN
    if regression_status == REGRESSION_STATUS_PASS:
        return TICKET_DISPOSITION_READY
    return TICKET_DISPOSITION_WARN


def summarize_pilot_fleet_ticket(
    ticket_id: str,
    *,
    limit: int | None = None,
    history_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchPilotFleetTicketSummary:
    """Build read-only fleet summary for one ticket."""
    signoff = evaluate_dispatch_production_signoff(merged_config=merged_config)
    readiness = evaluate_pilot_readiness(
        ticket_id=ticket_id,
        merged_config=merged_config,
    )
    regression = evaluate_pilot_regression(
        ticket_id=ticket_id,
        limit=limit,
        history_dir=history_dir,
    )
    trend = evaluate_pilot_trend(
        ticket_id=ticket_id,
        limit=limit,
        history_dir=history_dir,
    )
    records = list_pilot_history_records(history_dir=history_dir, ticket_id=ticket_id)
    if limit is not None and limit > 0:
        records = records[:limit]

    production_policy_valid = _production_policy_valid_for_ticket(
        signoff_production_root_hard_deny=signoff.production_root_hard_deny,
        signoff_gateway_enabled=signoff.gateway_enabled,
        signoff_execution_allowed=signoff.execution_allowed,
        records=records,
        regression_production_policy_violations=regression.production_policy_violations,
    )
    evidence_integrity = _evidence_integrity(
        records,
        regression_status=regression.regression_status,
    )
    audit_integrity = _audit_integrity(
        records,
        regression_status=regression.regression_status,
    )
    last_success = _last_success_present(records)
    recommended_action = _resolve_recommended_action(
        production_policy_valid=production_policy_valid,
        pilot_readiness=readiness.pilot_ready,
        regression_status=regression.regression_status,
        total_attempts=regression.total_attempts,
        dry_run_count=regression.dry_run_count,
        consecutive_failures=regression.consecutive_failures,
        last_success_present=last_success,
        trend_status=trend.trend_status,
    )
    disposition = _ticket_disposition(
        regression_status=regression.regression_status,
        trend_status=trend.trend_status,
        consecutive_failures=regression.consecutive_failures,
        last_success_present=last_success,
        production_policy_valid=production_policy_valid,
        evidence_integrity=evidence_integrity,
        audit_integrity=audit_integrity,
        total_attempts=regression.total_attempts,
        dry_run_count=regression.dry_run_count,
    )

    return CooDispatchPilotFleetTicketSummary(
        ticket_id=ticket_id,
        total_attempts=regression.total_attempts,
        latest_status=regression.latest_status,
        latest_pilot_attempt_id=regression.latest_pilot_attempt_id,
        regression_status=regression.regression_status,
        trend_status=trend.trend_status,
        consecutive_failures=regression.consecutive_failures,
        last_success_present=last_success,
        evidence_integrity=evidence_integrity,
        audit_integrity=audit_integrity,
        production_policy_valid=production_policy_valid,
        recommended_action=recommended_action,
        pilot_ready=readiness.pilot_ready,
        ticket_disposition=disposition,
    )


def _evaluate_fleet_status(
    tickets: tuple[CooDispatchPilotFleetTicketSummary, ...],
    *,
    production_policy_violation_count: int,
    fleet_non_dry_success_present: bool,
) -> str:
    if production_policy_violation_count > 0:
        return FLEET_STATUS_NOT_READY
    if any(ticket.ticket_disposition == TICKET_DISPOSITION_FAILED for ticket in tickets):
        return FLEET_STATUS_NOT_READY
    if not tickets:
        return FLEET_STATUS_WARN
    if any(ticket.ticket_disposition == TICKET_DISPOSITION_WARN for ticket in tickets):
        if fleet_non_dry_success_present:
            return FLEET_STATUS_WARN
        return FLEET_STATUS_WARN
    if not fleet_non_dry_success_present:
        return FLEET_STATUS_WARN
    return FLEET_STATUS_READY


def summarize_pilot_fleet(
    *,
    ticket_ids: tuple[str, ...] | list[str] | None = None,
    limit: int | None = None,
    history_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchPilotFleetSummary:
    """Build read-only multi-ticket pilot fleet summary."""
    resolved_ticket_ids = resolve_pilot_fleet_ticket_ids(
        ticket_ids=ticket_ids,
        history_dir=history_dir,
    )
    _validate_fleet_limits(ticket_count=len(resolved_ticket_ids), limit=limit)

    ticket_summaries = tuple(
        summarize_pilot_fleet_ticket(
            ticket_id,
            limit=limit,
            history_dir=history_dir,
            merged_config=merged_config,
        )
        for ticket_id in resolved_ticket_ids
    )

    ready_ticket_count = sum(
        1
        for ticket in ticket_summaries
        if ticket.ticket_disposition == TICKET_DISPOSITION_READY
    )
    warn_ticket_count = sum(
        1
        for ticket in ticket_summaries
        if ticket.ticket_disposition == TICKET_DISPOSITION_WARN
    )
    failed_ticket_count = sum(
        1
        for ticket in ticket_summaries
        if ticket.ticket_disposition == TICKET_DISPOSITION_FAILED
    )
    stable_ticket_count = sum(
        1 for ticket in ticket_summaries if ticket.trend_status == TREND_STATUS_STABLE
    )
    degraded_ticket_count = sum(
        1
        for ticket in ticket_summaries if ticket.trend_status == TREND_STATUS_DEGRADED
    )
    insufficient_data_count = sum(
        1
        for ticket in ticket_summaries
        if ticket.trend_status == TREND_STATUS_INSUFFICIENT_DATA
    )
    production_policy_violation_count = sum(
        1 for ticket in ticket_summaries if not ticket.production_policy_valid
    )
    fleet_non_dry_success_present = any(
        ticket.last_success_present for ticket in ticket_summaries
    )
    fleet_status = _evaluate_fleet_status(
        ticket_summaries,
        production_policy_violation_count=production_policy_violation_count,
        fleet_non_dry_success_present=fleet_non_dry_success_present,
    )

    return CooDispatchPilotFleetSummary(
        ticket_count=len(ticket_summaries),
        ready_ticket_count=ready_ticket_count,
        warn_ticket_count=warn_ticket_count,
        failed_ticket_count=failed_ticket_count,
        stable_ticket_count=stable_ticket_count,
        degraded_ticket_count=degraded_ticket_count,
        insufficient_data_count=insufficient_data_count,
        production_policy_violation_count=production_policy_violation_count,
        fleet_status=fleet_status,
        fleet_non_dry_success_present=fleet_non_dry_success_present,
        tickets=ticket_summaries,
    )


def format_pilot_fleet_summary(summary: CooDispatchPilotFleetSummary) -> str:
    """Format safe pilot fleet fields for CLI stdout."""
    lines = [
        "Pilot Fleet",
        "",
        f"fleet_status: {summary.fleet_status}",
        f"ticket_count: {summary.ticket_count}",
        f"ready_ticket_count: {summary.ready_ticket_count}",
        f"warn_ticket_count: {summary.warn_ticket_count}",
        f"failed_ticket_count: {summary.failed_ticket_count}",
        f"stable_ticket_count: {summary.stable_ticket_count}",
        f"degraded_ticket_count: {summary.degraded_ticket_count}",
        f"insufficient_data_count: {summary.insufficient_data_count}",
        (
            "production_policy_violation_count: "
            f"{summary.production_policy_violation_count}"
        ),
        (
            "fleet_non_dry_success_present: "
            f"{str(summary.fleet_non_dry_success_present).lower()}"
        ),
    ]
    if not summary.tickets:
        lines.append("tickets: (none)")
        return "\n".join(lines)
    lines.append("tickets:")
    for ticket in summary.tickets:
        lines.append(
            "  - "
            f"ticket_id={ticket.ticket_id} "
            f"disposition={ticket.ticket_disposition} "
            f"regression_status={ticket.regression_status} "
            f"trend_status={ticket.trend_status} "
            f"latest_status={ticket.latest_status} "
            f"latest_pilot_attempt_id={ticket.latest_pilot_attempt_id} "
            f"total_attempts={ticket.total_attempts} "
            f"consecutive_failures={ticket.consecutive_failures} "
            f"last_success_present={str(ticket.last_success_present).lower()} "
            f"evidence_integrity={str(ticket.evidence_integrity).lower()} "
            f"audit_integrity={str(ticket.audit_integrity).lower()} "
            f"production_policy_valid={str(ticket.production_policy_valid).lower()} "
            f"pilot_ready={str(ticket.pilot_ready).lower()} "
            f"recommended_action={ticket.recommended_action}"
        )
    return "\n".join(lines)
