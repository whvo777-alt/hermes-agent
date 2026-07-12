"""CLI pilot regression gate — Phase 13C.

Fail-closed gate before non-dry isolated pilot runs.
Dry-run diagnostic runs remain allowed when regression is FAIL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.coo.dispatch_cli_pilot_regression import (
    REGRESSION_STATUS_FAIL,
    REGRESSION_STATUS_PASS,
    REGRESSION_STATUS_WARN,
    CooDispatchPilotRegressionSummary,
    evaluate_pilot_regression,
)

REGRESSION_GATE_CLEAR = "clear"
REGRESSION_GATE_BLOCKED_FOR_LIVE = "blocked_for_live"
REGRESSION_GATE_WARN_LIVE_ALLOWED = "warn_live_allowed"


@dataclass(frozen=True)
class CooDispatchPilotRegressionGateSummary:
    """Read-only regression gate evaluation for pilot dispatch."""

    regression_status: str
    regression_gate: str
    live_pilot_allowed: bool
    dry_run_allowed: bool
    consecutive_failures: int
    total_attempts: int
    latest_status: str
    latest_pilot_attempt_id: str
    production_policy_violations: int


def evaluate_pilot_regression_gate(
    *,
    ticket_id: str | None = None,
    limit: int | None = None,
    history_dir: Path | None = None,
    dry_run: bool = False,
) -> CooDispatchPilotRegressionGateSummary:
    """Evaluate whether isolated pilot dispatch may proceed."""
    regression = evaluate_pilot_regression(
        ticket_id=ticket_id,
        limit=limit,
        history_dir=history_dir,
    )
    return _gate_from_regression(regression, dry_run=dry_run)


def _gate_from_regression(
    regression: CooDispatchPilotRegressionSummary,
    *,
    dry_run: bool,
) -> CooDispatchPilotRegressionGateSummary:
    dry_run_allowed = True
    if regression.regression_status == REGRESSION_STATUS_FAIL:
        regression_gate = REGRESSION_GATE_BLOCKED_FOR_LIVE
        live_pilot_allowed = False
    elif regression.regression_status == REGRESSION_STATUS_WARN:
        regression_gate = REGRESSION_GATE_WARN_LIVE_ALLOWED
        live_pilot_allowed = True
    elif regression.regression_status == REGRESSION_STATUS_PASS:
        regression_gate = REGRESSION_GATE_CLEAR
        live_pilot_allowed = True
    else:
        regression_gate = REGRESSION_GATE_WARN_LIVE_ALLOWED
        live_pilot_allowed = False

    if dry_run:
        dry_run_allowed = True

    return CooDispatchPilotRegressionGateSummary(
        regression_status=regression.regression_status,
        regression_gate=regression_gate,
        live_pilot_allowed=live_pilot_allowed,
        dry_run_allowed=dry_run_allowed,
        consecutive_failures=regression.consecutive_failures,
        total_attempts=regression.total_attempts,
        latest_status=regression.latest_status,
        latest_pilot_attempt_id=regression.latest_pilot_attempt_id,
        production_policy_violations=regression.production_policy_violations,
    )
