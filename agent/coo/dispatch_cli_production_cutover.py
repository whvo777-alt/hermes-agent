"""CLI production cutover checklist — Phase 13D.

Read-only aggregation of sign-off, pilot fleet, recovery, and policy checks.
cutover_ready does not grant production execution permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_cli_consume_recovery import assess_dispatch_consume_recovery
from agent.coo.dispatch_cli_operator_runbook import summarize_dispatch_operator_runbook
from agent.coo.dispatch_cli_pilot_fleet import (
    FLEET_STATUS_NOT_READY,
    FLEET_STATUS_READY,
    FLEET_STATUS_WARN,
    TICKET_DISPOSITION_FAILED,
    summarize_pilot_fleet,
)
from agent.coo.dispatch_cli_production_readiness import (
    CHECK_BLOCKED,
    CHECK_FAIL,
    CHECK_PASS,
    _evaluate_operator_check,
)
from agent.coo.dispatch_cli_production_signoff import (
    evaluate_dispatch_production_signoff,
)
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_LEGACY_COMMITTED,
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_RECOVERY_REQUIRED,
    CONSUME_STATE_UNCONSUMED,
)
from agent.coo.dispatch_pilot_history import list_pilot_history_records

RECOMMENDED_ACTION_CONTINUE_ISOLATED_PILOT = "continue_isolated_pilot"
RECOMMENDED_ACTION_COLLECT_MORE_PILOT_HISTORY = "collect_more_pilot_history"
RECOMMENDED_ACTION_RESOLVE_PILOT_REGRESSIONS = "resolve_pilot_regressions"
RECOMMENDED_ACTION_RESOLVE_RECOVERY_ISSUES = "resolve_recovery_issues"
RECOMMENDED_ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
RECOMMENDED_ACTION_REVIEW_GATEWAY_LATER = "review_gateway_integration_later"

OVERALL_STATUS_READY = "READY"
OVERALL_STATUS_NOT_READY = "NOT_READY"

RECOMMENDED_NEXT_PHASE_READY = "Phase 13E Gateway Integration Readiness Review"
RECOMMENDED_NEXT_PHASE_NOT_READY = "resolve_production_cutover_failures"

_NONE_LABEL = "(none)"

_RECOVERY_FAIL_STATES = frozenset(
    {
        CONSUME_STATE_PREPARED,
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_LEGACY_PARTIAL,
        CONSUME_STATE_RECOVERY_REQUIRED,
    }
)


@dataclass(frozen=True)
class CooDispatchProductionCutoverCheck:
    """One production cutover checklist item."""

    name: str
    status: str


@dataclass(frozen=True)
class CooDispatchProductionCutoverSummary:
    """Safe read-only production cutover checklist summary."""

    cutover_ready: bool
    overall_status: str
    checks_passed_count: int
    checks_blocked_count: int
    checks_failed_count: int
    failed_checks: str
    blocked_checks: str
    fleet_status: str
    ticket_count: int
    ready_ticket_count: int
    failed_ticket_count: int
    production_execution_allowed: bool
    gateway_enabled: bool
    production_root_hard_deny: bool
    recommended_action: str
    recommended_next_phase: str


def _join_check_names(names: tuple[str, ...]) -> str:
    return ",".join(names) if names else _NONE_LABEL


def _latest_confirmation_for_ticket(
    ticket_id: str,
    *,
    history_dir: Path | None = None,
) -> str | None:
    records = list_pilot_history_records(history_dir=history_dir, ticket_id=ticket_id)
    for record in records:
        confirmation_id = (record.confirmation_id or "").strip()
        if confirmation_id:
            return confirmation_id
    return None


def _recovery_status_for_ticket(
    ticket_id: str,
    *,
    history_dir: Path | None = None,
) -> tuple[str, bool]:
    """Return consume recovery status and whether recovery blocks cutover."""
    confirmation_id = _latest_confirmation_for_ticket(
        ticket_id,
        history_dir=history_dir,
    )
    if confirmation_id is None:
        return "not_applicable", False
    try:
        recovery = assess_dispatch_consume_recovery(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
    except (KeyError, ValueError):
        return "not_applicable", False

    if recovery.recovery_required or recovery.consume_state in _RECOVERY_FAIL_STATES:
        return recovery.consume_state, True
    if recovery.consume_state == CONSUME_STATE_UNCONSUMED:
        return recovery.consume_state, False
    if recovery.consume_state in {
        CONSUME_STATE_COMMITTED,
        CONSUME_STATE_LEGACY_COMMITTED,
    }:
        return recovery.consume_state, False
    return recovery.consume_state, True


def _repair_status_for_ticket(
    ticket_id: str,
    *,
    history_dir: Path | None = None,
) -> tuple[str, bool]:
    confirmation_id = _latest_confirmation_for_ticket(
        ticket_id,
        history_dir=history_dir,
    )
    if confirmation_id is None:
        return "not_applicable", False
    try:
        runbook = summarize_dispatch_operator_runbook(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
    except ValueError:
        return "not_applicable", False
    repair_state = runbook.repair_state
    if repair_state in {
        "repair_blocked",
        "repair_prepared_cleanup_available",
        "repair_partial_forward_complete_available",
    }:
        return repair_state, True
    if repair_state in {"repair_not_required", "repair_not_allowed"}:
        return repair_state, False
    return repair_state, True


def _evaluate_consume_recovery_clear(
    *,
    ticket_ids: tuple[str, ...],
    history_dir: Path | None = None,
) -> str:
    if not ticket_ids:
        return CHECK_PASS
    blocking = False
    for ticket_id in ticket_ids:
        _, blocks = _recovery_status_for_ticket(ticket_id, history_dir=history_dir)
        if blocks:
            blocking = True
            break
    return CHECK_FAIL if blocking else CHECK_PASS


def _evaluate_repair_recovery_clear(
    *,
    ticket_ids: tuple[str, ...],
    history_dir: Path | None = None,
) -> str:
    if not ticket_ids:
        return CHECK_PASS
    blocking = False
    for ticket_id in ticket_ids:
        _, blocks = _repair_status_for_ticket(ticket_id, history_dir=history_dir)
        if blocks:
            blocking = True
            break
    return CHECK_FAIL if blocking else CHECK_PASS


def _evaluate_pilot_fleet_ready(fleet_status: str) -> str:
    if fleet_status == FLEET_STATUS_READY:
        return CHECK_PASS
    if fleet_status == FLEET_STATUS_WARN:
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_regression_failures_absent(
    tickets: tuple[Any, ...],
) -> str:
    if any(ticket.regression_status == "FAIL" for ticket in tickets):
        return CHECK_FAIL
    if any(ticket.ticket_disposition == TICKET_DISPOSITION_FAILED for ticket in tickets):
        return CHECK_FAIL
    return CHECK_PASS


def _evaluate_trend_stable_or_acceptable(tickets: tuple[Any, ...]) -> str:
    if not tickets:
        return CHECK_PASS
    for ticket in tickets:
        if ticket.trend_status == "DEGRADED" and ticket.consecutive_failures > 0:
            if not ticket.last_success_present:
                return CHECK_FAIL
    return CHECK_PASS


def _evaluate_evidence_integrity_valid(tickets: tuple[Any, ...]) -> str:
    if not tickets:
        return CHECK_PASS
    if any(not ticket.evidence_integrity for ticket in tickets):
        return CHECK_FAIL
    return CHECK_PASS


def _evaluate_audit_integrity_valid(tickets: tuple[Any, ...]) -> str:
    if not tickets:
        return CHECK_PASS
    if any(not ticket.audit_integrity for ticket in tickets):
        return CHECK_FAIL
    return CHECK_PASS


def _resolve_cutover_recommended_action(
    *,
    cutover_ready: bool,
    failed_checks: tuple[str, ...],
    fleet_status: str,
    ticket_count: int,
    fleet_non_dry_success_present: bool,
) -> str:
    if "production_root_hard_deny" in failed_checks or "execution_disabled" in failed_checks:
        return RECOMMENDED_ACTION_MAINTAIN_PRODUCTION_BLOCK
    if any(
        name in failed_checks
        for name in (
            "consume_recovery_clear",
            "repair_recovery_clear",
        )
    ):
        return RECOMMENDED_ACTION_RESOLVE_RECOVERY_ISSUES
    if any(
        name in failed_checks
        for name in (
            "pilot_fleet_ready",
            "regression_failures_absent",
            "trend_stable_or_acceptable",
            "evidence_integrity_valid",
            "audit_integrity_valid",
        )
    ):
        return RECOMMENDED_ACTION_RESOLVE_PILOT_REGRESSIONS
    if ticket_count == 0 or not fleet_non_dry_success_present:
        return RECOMMENDED_ACTION_COLLECT_MORE_PILOT_HISTORY
    if cutover_ready and fleet_status == FLEET_STATUS_READY:
        return RECOMMENDED_ACTION_CONTINUE_ISOLATED_PILOT
    if cutover_ready:
        return RECOMMENDED_ACTION_REVIEW_GATEWAY_LATER
    return RECOMMENDED_ACTION_RESOLVE_PILOT_REGRESSIONS


def evaluate_production_cutover_checklist(
    *,
    ticket_ids: tuple[str, ...] | list[str] | None = None,
    limit: int | None = None,
    history_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchProductionCutoverSummary:
    """Evaluate read-only production cutover checklist."""
    signoff = evaluate_dispatch_production_signoff(merged_config=merged_config)
    fleet = summarize_pilot_fleet(
        ticket_ids=ticket_ids,
        limit=limit,
        history_dir=history_dir,
        merged_config=merged_config,
    )
    resolved_ticket_ids = tuple(ticket.ticket_id for ticket in fleet.tickets)

    checks = (
        CooDispatchProductionCutoverCheck(
            "production_signoff_ready",
            CHECK_PASS if signoff.signoff_ready else CHECK_FAIL,
        ),
        CooDispatchProductionCutoverCheck(
            "repository_attestation_valid",
            CHECK_PASS if signoff.repository_attested else CHECK_FAIL,
        ),
        CooDispatchProductionCutoverCheck(
            "production_root_hard_deny",
            CHECK_BLOCKED if signoff.production_root_hard_deny else CHECK_FAIL,
        ),
        CooDispatchProductionCutoverCheck(
            "execution_disabled",
            CHECK_BLOCKED if not signoff.execution_allowed else CHECK_FAIL,
        ),
        CooDispatchProductionCutoverCheck(
            "gateway_disabled",
            CHECK_BLOCKED if not signoff.gateway_enabled else CHECK_FAIL,
        ),
        CooDispatchProductionCutoverCheck(
            "pilot_fleet_ready",
            _evaluate_pilot_fleet_ready(fleet.fleet_status),
        ),
        CooDispatchProductionCutoverCheck(
            "regression_failures_absent",
            _evaluate_regression_failures_absent(fleet.tickets),
        ),
        CooDispatchProductionCutoverCheck(
            "trend_stable_or_acceptable",
            _evaluate_trend_stable_or_acceptable(fleet.tickets),
        ),
        CooDispatchProductionCutoverCheck(
            "evidence_integrity_valid",
            _evaluate_evidence_integrity_valid(fleet.tickets),
        ),
        CooDispatchProductionCutoverCheck(
            "audit_integrity_valid",
            _evaluate_audit_integrity_valid(fleet.tickets),
        ),
        CooDispatchProductionCutoverCheck(
            "consume_recovery_clear",
            _evaluate_consume_recovery_clear(
                ticket_ids=resolved_ticket_ids,
                history_dir=history_dir,
            ),
        ),
        CooDispatchProductionCutoverCheck(
            "repair_recovery_clear",
            _evaluate_repair_recovery_clear(
                ticket_ids=resolved_ticket_ids,
                history_dir=history_dir,
            ),
        ),
        CooDispatchProductionCutoverCheck(
            "operator_runbook_available",
            CHECK_PASS if _evaluate_operator_check() == CHECK_PASS else CHECK_FAIL,
        ),
    )

    failed = tuple(check.name for check in checks if check.status == CHECK_FAIL)
    blocked = tuple(check.name for check in checks if check.status == CHECK_BLOCKED)
    passed_count = sum(1 for check in checks if check.status == CHECK_PASS)
    cutover_ready = len(failed) == 0
    overall_status = OVERALL_STATUS_READY if cutover_ready else OVERALL_STATUS_NOT_READY
    recommended_action = _resolve_cutover_recommended_action(
        cutover_ready=cutover_ready,
        failed_checks=failed,
        fleet_status=fleet.fleet_status,
        ticket_count=fleet.ticket_count,
        fleet_non_dry_success_present=fleet.fleet_non_dry_success_present,
    )
    recommended_next_phase = (
        RECOMMENDED_NEXT_PHASE_READY
        if cutover_ready
        else RECOMMENDED_NEXT_PHASE_NOT_READY
    )

    return CooDispatchProductionCutoverSummary(
        cutover_ready=cutover_ready,
        overall_status=overall_status,
        checks_passed_count=passed_count,
        checks_blocked_count=len(blocked),
        checks_failed_count=len(failed),
        failed_checks=_join_check_names(failed),
        blocked_checks=_join_check_names(blocked),
        fleet_status=fleet.fleet_status,
        ticket_count=fleet.ticket_count,
        ready_ticket_count=fleet.ready_ticket_count,
        failed_ticket_count=fleet.failed_ticket_count,
        production_execution_allowed=False,
        gateway_enabled=False,
        production_root_hard_deny=signoff.production_root_hard_deny,
        recommended_action=recommended_action,
        recommended_next_phase=recommended_next_phase,
    )


def format_production_cutover_checklist(
    summary: CooDispatchProductionCutoverSummary,
) -> str:
    """Format safe production cutover checklist fields for CLI stdout."""
    sections = (
        (
            "Production Cutover Checklist",
            (
                f"cutover_ready: {str(summary.cutover_ready).lower()}",
                f"overall_status: {summary.overall_status}",
                f"checks_passed_count: {summary.checks_passed_count}",
                f"checks_blocked_count: {summary.checks_blocked_count}",
                f"checks_failed_count: {summary.checks_failed_count}",
                f"failed_checks: {summary.failed_checks}",
                f"blocked_checks: {summary.blocked_checks}",
                f"recommended_action: {summary.recommended_action}",
                f"recommended_next_phase: {summary.recommended_next_phase}",
            ),
        ),
        (
            "Pilot Fleet",
            (
                f"fleet_status: {summary.fleet_status}",
                f"ticket_count: {summary.ticket_count}",
                f"ready_ticket_count: {summary.ready_ticket_count}",
                f"failed_ticket_count: {summary.failed_ticket_count}",
            ),
        ),
        (
            "Policy",
            (
                "production_execution_allowed: false",
                f"production_root_hard_deny: {str(summary.production_root_hard_deny).lower()}",
                f"gateway_enabled: {str(summary.gateway_enabled).lower()}",
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
