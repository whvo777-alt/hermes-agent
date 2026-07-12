"""CLI pilot drill runbook — Phase 13C.

Read-only cross-reference for isolated operational pilot drill operations.
Integrates sign-off, readiness, regression, history, binding, and trend.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_cli_enablement import evaluate_dispatch_enablement
from agent.coo.dispatch_cli_pilot import (
    EXECUTION_SCOPE_ISOLATED_CLONE,
    evaluate_pilot_readiness,
)
from agent.coo.dispatch_cli_pilot_regression import (
    REGRESSION_STATUS_FAIL,
    REGRESSION_STATUS_PASS,
    REGRESSION_STATUS_WARN,
    evaluate_pilot_regression,
)
from agent.coo.dispatch_cli_pilot_regression_gate import (
    REGRESSION_GATE_BLOCKED_FOR_LIVE,
    evaluate_pilot_regression_gate,
)
from agent.coo.dispatch_cli_production_signoff import evaluate_dispatch_production_signoff
from agent.coo.dispatch_pilot_history import (
    FAILURE_REASON_CONSUME_FAILED,
    FAILURE_REASON_NONE,
    FAILURE_REASON_POLICY_BLOCKED,
    FAILURE_REASON_PREFLIGHT_FAILED,
    FAILURE_REASON_RUNNER_FAILED,
    FAILURE_REASON_TIMEOUT,
    FAILURE_REASON_UNKNOWN_FAILURE,
    PILOT_STATUS_DRY_RUN,
    PILOT_STATUS_FAILURE,
    PILOT_STATUS_SUCCESS,
    PILOT_STATUS_TIMEOUT,
    CooDispatchPilotHistoryRecord,
    list_pilot_history_records,
)
from agent.coo.dispatch_runner_binding_state import (
    CooDispatchRunnerBindingState,
    load_dispatch_runner_binding_state,
)

RECOMMENDED_ACTION_RUN_PILOT_DRY_RUN = "run_pilot_dry_run"
RECOMMENDED_ACTION_RUN_ISOLATED_PILOT = "run_isolated_pilot"
RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE = "investigate_recent_failure"
RECOMMENDED_ACTION_RESOLVE_REGRESSION_FAILURE = "resolve_regression_failure"
RECOMMENDED_ACTION_COLLECT_INITIAL_PILOT_HISTORY = "collect_initial_pilot_history"
RECOMMENDED_ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"

TREND_STATUS_STABLE = "STABLE"
TREND_STATUS_DEGRADED = "DEGRADED"
TREND_STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

_FAILURE_REASON_CODES = (
    FAILURE_REASON_NONE,
    FAILURE_REASON_PREFLIGHT_FAILED,
    FAILURE_REASON_RUNNER_FAILED,
    FAILURE_REASON_TIMEOUT,
    FAILURE_REASON_CONSUME_FAILED,
    FAILURE_REASON_POLICY_BLOCKED,
    FAILURE_REASON_UNKNOWN_FAILURE,
)

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class CooDispatchPilotTrendSummary:
    """Safe read-only pilot trend summary over recent history."""

    pass_count: int
    fail_count: int
    timeout_count: int
    dry_run_count: int
    success_rate_percent: int
    consecutive_failures: int
    failure_reason_counts: str
    trend_status: str


@dataclass(frozen=True)
class CooDispatchPilotRunbookSummary:
    """Safe read-only pilot drill runbook summary."""

    pilot_runbook_ready: bool
    signoff_ready: bool
    pilot_readiness: bool
    regression_status: str
    total_attempts: int
    latest_status: str
    latest_pilot_attempt_id: str
    consecutive_failures: int
    last_success_present: bool
    evidence_integrity: bool
    audit_integrity: bool
    production_policy_valid: bool
    recommended_action: str
    pilot_execution_allowed: bool
    production_execution_allowed: bool
    gateway_enabled: bool
    production_root_hard_deny: bool
    binding_state: str
    runner_provider: str
    regression_gate: str
    trend_status: str
    pass_count: int
    fail_count: int
    timeout_count: int
    dry_run_count: int
    success_rate_percent: int
    failure_reason_counts: str


def _policy_violation(record: CooDispatchPilotHistoryRecord) -> bool:
    return (
        record.production_execution_allowed
        or not record.production_root_hard_deny
        or record.gateway_enabled
    )


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


def _format_failure_reason_counts(counts: Counter[str]) -> str:
    parts = [
        f"{code}={counts.get(code, 0)}"
        for code in _FAILURE_REASON_CODES
    ]
    return ",".join(parts)


def evaluate_pilot_trend(
    *,
    ticket_id: str | None = None,
    limit: int | None = None,
    history_dir: Path | None = None,
) -> CooDispatchPilotTrendSummary:
    """Evaluate read-only pilot trend over recent history."""
    records = list_pilot_history_records(history_dir=history_dir, ticket_id=ticket_id)
    if limit is not None and limit > 0:
        records = records[:limit]

    pass_count = sum(1 for record in records if record.status == PILOT_STATUS_SUCCESS)
    fail_count = sum(1 for record in records if record.status == PILOT_STATUS_FAILURE)
    timeout_count = sum(1 for record in records if record.status == PILOT_STATUS_TIMEOUT)
    dry_run_count = sum(1 for record in records if record.dry_run)
    non_dry = [record for record in records if not record.dry_run]
    non_dry_success = sum(
        1 for record in non_dry if record.status == PILOT_STATUS_SUCCESS
    )
    success_rate_percent = (
        int(round((non_dry_success / len(non_dry)) * 100))
        if non_dry
        else 0
    )

    consecutive_failures = 0
    for record in records:
        if record.dry_run:
            continue
        if record.status in {PILOT_STATUS_FAILURE, PILOT_STATUS_TIMEOUT}:
            consecutive_failures += 1
            continue
        break

    reason_counter: Counter[str] = Counter()
    for record in records:
        reason_counter[record.failure_reason_code] += 1

    trend_status = TREND_STATUS_INSUFFICIENT_DATA
    if not records or dry_run_count == len(records):
        trend_status = TREND_STATUS_INSUFFICIENT_DATA
    elif consecutive_failures > 0 or timeout_count >= 2:
        trend_status = TREND_STATUS_DEGRADED
    elif non_dry and records[0].status == PILOT_STATUS_SUCCESS and consecutive_failures == 0:
        trend_status = TREND_STATUS_STABLE
    elif consecutive_failures == 0 and pass_count > 0:
        trend_status = TREND_STATUS_STABLE
    else:
        trend_status = TREND_STATUS_DEGRADED

    return CooDispatchPilotTrendSummary(
        pass_count=pass_count,
        fail_count=fail_count,
        timeout_count=timeout_count,
        dry_run_count=dry_run_count,
        success_rate_percent=success_rate_percent,
        consecutive_failures=consecutive_failures,
        failure_reason_counts=_format_failure_reason_counts(reason_counter),
        trend_status=trend_status,
    )


def _last_success_present(records: tuple[CooDispatchPilotHistoryRecord, ...]) -> bool:
    return any(
        record.status == PILOT_STATUS_SUCCESS and not record.dry_run
        for record in records
    )


def _evidence_integrity(
    records: tuple[CooDispatchPilotHistoryRecord, ...],
    *,
    regression_status: str,
) -> bool:
    if regression_status == REGRESSION_STATUS_FAIL:
        return False
    for record in records:
        if record.status == PILOT_STATUS_SUCCESS and not record.dry_run:
            return record.evidence_present
    return True


def _audit_integrity(
    records: tuple[CooDispatchPilotHistoryRecord, ...],
    *,
    regression_status: str,
) -> bool:
    if regression_status == REGRESSION_STATUS_FAIL:
        return False
    for record in records:
        if record.status == PILOT_STATUS_SUCCESS and not record.dry_run:
            return record.audit_present
    return True


def _resolve_recommended_action(
    *,
    production_policy_valid: bool,
    pilot_readiness: bool,
    regression_status: str,
    total_attempts: int,
    dry_run_count: int,
    consecutive_failures: int,
    last_success_present: bool,
    trend_status: str,
) -> str:
    if not production_policy_valid:
        return RECOMMENDED_ACTION_MAINTAIN_PRODUCTION_BLOCK
    if regression_status == REGRESSION_STATUS_FAIL:
        if consecutive_failures == 1 and not last_success_present:
            return RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE
        return RECOMMENDED_ACTION_RESOLVE_REGRESSION_FAILURE
    if total_attempts == 0:
        return RECOMMENDED_ACTION_COLLECT_INITIAL_PILOT_HISTORY
    if dry_run_count == total_attempts:
        return RECOMMENDED_ACTION_RUN_PILOT_DRY_RUN
    if consecutive_failures > 0 and not last_success_present:
        return RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE
    if trend_status == TREND_STATUS_DEGRADED and consecutive_failures > 0:
        return RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE
    if regression_status == REGRESSION_STATUS_PASS and pilot_readiness:
        return RECOMMENDED_ACTION_RUN_ISOLATED_PILOT
    if regression_status == REGRESSION_STATUS_WARN and pilot_readiness:
        return RECOMMENDED_ACTION_RUN_ISOLATED_PILOT
    if not pilot_readiness:
        return RECOMMENDED_ACTION_RESOLVE_REGRESSION_FAILURE
    return RECOMMENDED_ACTION_RUN_PILOT_DRY_RUN


def summarize_pilot_drill_runbook(
    *,
    ticket_id: str | None = None,
    confirmation_id: str | None = None,
    pipeline_root: str | None = None,
    limit: int | None = None,
    history_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    binding_state: CooDispatchRunnerBindingState | None = None,
) -> CooDispatchPilotRunbookSummary:
    """Build read-only pilot drill runbook summary."""
    signoff = evaluate_dispatch_production_signoff(merged_config=merged_config)
    readiness = evaluate_pilot_readiness(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
        merged_config=merged_config,
    )
    regression = evaluate_pilot_regression(
        ticket_id=ticket_id,
        limit=limit,
        history_dir=history_dir,
    )
    gate = evaluate_pilot_regression_gate(
        ticket_id=ticket_id,
        limit=limit,
        history_dir=history_dir,
        dry_run=False,
    )
    trend = evaluate_pilot_trend(
        ticket_id=ticket_id,
        limit=limit,
        history_dir=history_dir,
    )
    records = list_pilot_history_records(history_dir=history_dir, ticket_id=ticket_id)
    if limit is not None and limit > 0:
        records = records[:limit]

    production_policy_valid = (
        signoff.production_root_hard_deny
        and not signoff.gateway_enabled
        and not signoff.execution_allowed
        and regression.production_policy_violations == 0
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

    pilot_execution_allowed = (
        production_policy_valid
        and readiness.pilot_ready
        and gate.live_pilot_allowed
        and regression.regression_status != REGRESSION_STATUS_FAIL
    )
    pilot_runbook_ready = (
        production_policy_valid
        and signoff.signoff_ready
        and readiness.runner_bound
        and readiness.runtime_enablement_ready
        and _resolve_binding_state(binding_state) != "invalid"
    )

    return CooDispatchPilotRunbookSummary(
        pilot_runbook_ready=pilot_runbook_ready,
        signoff_ready=signoff.signoff_ready,
        pilot_readiness=readiness.pilot_ready,
        regression_status=regression.regression_status,
        total_attempts=regression.total_attempts,
        latest_status=regression.latest_status,
        latest_pilot_attempt_id=regression.latest_pilot_attempt_id,
        consecutive_failures=regression.consecutive_failures,
        last_success_present=last_success,
        evidence_integrity=evidence_integrity,
        audit_integrity=audit_integrity,
        production_policy_valid=production_policy_valid,
        recommended_action=recommended_action,
        pilot_execution_allowed=pilot_execution_allowed,
        production_execution_allowed=False,
        gateway_enabled=False,
        production_root_hard_deny=signoff.production_root_hard_deny,
        binding_state=_resolve_binding_state(binding_state),
        runner_provider=_resolve_runner_provider(
            merged_config,
            binding_state=binding_state,
        ),
        regression_gate=gate.regression_gate,
        trend_status=trend.trend_status,
        pass_count=trend.pass_count,
        fail_count=trend.fail_count,
        timeout_count=trend.timeout_count,
        dry_run_count=trend.dry_run_count,
        success_rate_percent=trend.success_rate_percent,
        failure_reason_counts=trend.failure_reason_counts,
    )


def format_pilot_drill_runbook(summary: CooDispatchPilotRunbookSummary) -> str:
    """Format safe pilot drill runbook fields for CLI stdout."""
    sections = (
        (
            "Pilot Drill Runbook",
            (
                f"pilot_runbook_ready: {str(summary.pilot_runbook_ready).lower()}",
                f"signoff_ready: {str(summary.signoff_ready).lower()}",
                f"pilot_readiness: {str(summary.pilot_readiness).lower()}",
                f"regression_status: {summary.regression_status}",
                f"regression_gate: {summary.regression_gate}",
                f"recommended_action: {summary.recommended_action}",
                f"pilot_execution_allowed: {str(summary.pilot_execution_allowed).lower()}",
            ),
        ),
        (
            "History",
            (
                f"total_attempts: {summary.total_attempts}",
                f"latest_status: {summary.latest_status}",
                f"latest_pilot_attempt_id: {summary.latest_pilot_attempt_id}",
                f"consecutive_failures: {summary.consecutive_failures}",
                f"last_success_present: {str(summary.last_success_present).lower()}",
            ),
        ),
        (
            "Integrity",
            (
                f"evidence_integrity: {str(summary.evidence_integrity).lower()}",
                f"audit_integrity: {str(summary.audit_integrity).lower()}",
                f"production_policy_valid: {str(summary.production_policy_valid).lower()}",
            ),
        ),
        (
            "Trend",
            (
                f"trend_status: {summary.trend_status}",
                f"pass_count: {summary.pass_count}",
                f"fail_count: {summary.fail_count}",
                f"timeout_count: {summary.timeout_count}",
                f"dry_run_count: {summary.dry_run_count}",
                f"success_rate_percent: {summary.success_rate_percent}",
                f"failure_reason_counts: {summary.failure_reason_counts}",
            ),
        ),
        (
            "Infrastructure",
            (
                f"binding_state: {summary.binding_state}",
                f"runner_provider: {summary.runner_provider}",
                f"execution_scope: {EXECUTION_SCOPE_ISOLATED_CLONE}",
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
