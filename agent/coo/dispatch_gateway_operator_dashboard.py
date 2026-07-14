"""Gateway operator dashboard and correlation diff — Phase 13O.

Read-only aggregation of Gateway/Pilot/Dispatch/Recovery/Audit/Correlation
state plus safe diff between two gateway requests on the same ticket.

No writes, subprocess, Repository2 access, or production execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_cli_gateway_pilot import EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK
from agent.coo.dispatch_cli_gateway_readiness import (
    READINESS_LEVEL_READY_FOR_MOCK_DISPATCH,
    evaluate_dispatch_gateway_readiness,
)
from agent.coo.dispatch_cli_pilot_regression import (
    REGRESSION_STATUS_FAIL,
    REGRESSION_STATUS_PASS,
    REGRESSION_STATUS_WARN,
    evaluate_pilot_regression,
)
from agent.coo.dispatch_cli_pilot_runbook import (
    TREND_STATUS_DEGRADED,
    TREND_STATUS_INSUFFICIENT_DATA,
    evaluate_pilot_trend,
)
from agent.coo.dispatch_cli_production_cutover import (
    evaluate_production_cutover_checklist,
)
from agent.coo.dispatch_cli_production_signoff import (
    evaluate_dispatch_production_signoff,
)
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_RECOVERY_REQUIRED,
)
from agent.coo.dispatch_gateway_correlation_explorer import (
    CooDispatchGatewayCorrelationChain,
    GatewayCorrelationExplorerError,
    GatewayCorrelationQuery,
    QUERY_TYPE_GATEWAY_REQUEST,
    explore_gateway_correlation,
)
from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_DISABLED,
    GATEWAY_STATE_ENABLED,
    GATEWAY_STATE_STAGED,
    load_dispatch_gateway_enablement,
)
from agent.coo.dispatch_gateway_execution_facade import (
    evaluate_gateway_execution_facade,
)
from agent.coo.dispatch_gateway_request_store import (
    CooDispatchGatewayRequestRecord,
    DispatchGatewayRequestStoreError,
    default_gateway_request_dir,
    normalize_gateway_request_id,
    read_gateway_request,
)
from agent.coo.dispatch_pilot_history import (
    PILOT_STATUS_DRY_RUN,
    PILOT_STATUS_FAILURE,
    PILOT_STATUS_TIMEOUT,
)
from hermes_constants import get_hermes_home

DEFAULT_DASHBOARD_LIMIT = 20
MAX_DASHBOARD_LIMIT = 100
DEFAULT_CORRELATION_SCAN_LIMIT = 500

DASHBOARD_HEALTH_HEALTHY = "HEALTHY"
DASHBOARD_HEALTH_DEGRADED = "DEGRADED"
DASHBOARD_HEALTH_BLOCKED = "BLOCKED"
DASHBOARD_HEALTH_NOT_CONFIGURED = "NOT_CONFIGURED"

DASHBOARD_ACTION_NO_ACTION_REQUIRED = "no_action_required"
DASHBOARD_ACTION_RUN_GATEWAY_PILOT_DRY_RUN = "run_gateway_pilot_dry_run"
DASHBOARD_ACTION_COLLECT_MORE_HISTORY = "collect_more_history"
DASHBOARD_ACTION_INSPECT_LATEST_FAILURE = "inspect_latest_failure"
DASHBOARD_ACTION_INSPECT_MISSING_EVIDENCE = "inspect_missing_evidence"
DASHBOARD_ACTION_RESOLVE_RECOVERY_REQUIRED = "resolve_recovery_required"
DASHBOARD_ACTION_RESOLVE_CORRELATION_MISMATCH = "resolve_correlation_mismatch"
DASHBOARD_ACTION_STAGE_GATEWAY = "stage_gateway"
DASHBOARD_ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"

DIFF_ACTION_NO_ACTION_REQUIRED = "no_action_required"
DIFF_ACTION_INSPECT_REGRESSION = "inspect_regression"
DIFF_ACTION_INSPECT_CONSUME_DRIFT = "inspect_consume_drift"
DIFF_ACTION_RESOLVE_RECOVERY_REQUIRED = "resolve_recovery_required"
DIFF_ACTION_RESOLVE_CORRELATION_MISMATCH = "resolve_correlation_mismatch"
DIFF_ACTION_PROVIDE_SAME_TICKET_REQUESTS = "provide_same_ticket_requests"

_DIFF_COMPARE_FIELDS = (
    "request_status",
    "pilot_status",
    "execution_status",
    "evidence_present",
    "audit_present",
    "consume_state",
    "consumed",
    "recovery_required",
    "repair_attempt_id",
    "repair_audit_present",
    "repair_lock_held",
    "correlation_valid",
    "chain_complete",
    "failure_reason_code",
    "recommended_action",
)

_NONE_LABEL = "(none)"
_TRANSITION_UNCHANGED = "unchanged"

_FORBIDDEN_OUTPUT_KEYS = frozenset(
    {
        "pipeline_root",
        "unlock_token",
        "unlock_token_id",
        "confirmation_phrase",
        "argv",
        "cwd",
        "env",
        "stdout",
        "stderr",
        "operator_reason",
        "secret",
        "token",
        "snapshot",
        "channel_id",
        "requester_metadata",
    }
)


class GatewayOperatorDashboardError(ValueError):
    """Raised when dashboard or diff cannot proceed safely."""


@dataclass(frozen=True)
class CooDispatchGatewayOperatorDashboardSummary:
    """Safe read-only operator dashboard summary."""

    dashboard_health: str
    gateway_state: str
    readiness_level: str
    signoff_ready: bool
    cutover_ready: bool
    regression_status: str
    trend_status: str
    latest_gateway_request_id: str
    latest_pilot_attempt_id: str
    latest_execution_attempt_id: str
    latest_dispatch_run_id: str
    latest_request_status: str
    evidence_present: bool
    audit_present: bool
    consume_state: str
    consumed: bool
    recovery_required: bool
    repair_lock_held: bool
    correlation_valid: bool
    chain_complete: bool
    consecutive_failures: int
    total_recent_requests: int
    failed_recent_requests: int
    recommended_action: str
    production_execution_allowed: bool
    production_root_hard_deny: bool
    gateway_execution_scope: str
    facade_connected: bool = False
    live_pilot_status: str = ""
    live_pilot_signoff_status: str = ""
    live_pilot_activation_request_id: str = ""
    live_pilot_recommended_action: str = ""
    rollback_validation_status: str = ""
    rollback_ready: bool = False
    rollback_cleanup_required: bool = False
    rollback_recommended_action: str = ""
    production_final_signoff_status: str = ""
    production_release_ready: bool = False
    production_final_signoff_present: bool = False
    production_final_blocking_count: int = 0
    production_final_warning_count: int = 0
    production_final_recommended_action: str = ""
    governed_cutover_status: str = ""
    governed_cutover_ready: bool = False
    governed_cutover_contract_present: bool = False
    governed_cutover_window_valid: bool = False
    governed_cutover_blocking_count: int = 0
    governed_cutover_warning_count: int = 0
    governed_cutover_recommended_action: str = ""
    controlled_window_state: str = ""
    controlled_window_open: bool = False
    controlled_window_expired: bool = False
    controlled_window_contract_id: str = ""
    controlled_window_event_count: int = 0
    controlled_window_blocking_count: int = 0
    controlled_window_warning_count: int = 0
    controlled_window_recommended_action: str = ""
    runtime_permission_state: str = ""
    runtime_permission_ready: bool = False
    runtime_permission_present: bool = False
    runtime_permission_expired: bool = False
    runtime_permission_id: str = ""
    runtime_permission_expires_at: str = ""
    runtime_permission_blocking_count: int = 0
    runtime_permission_warning_count: int = 0
    runtime_permission_recommended_action: str = ""
    governed_runtime_session_state: str = ""
    governed_runtime_session_ready: bool = False
    governed_runtime_session_present: bool = False
    governed_runtime_session_expired: bool = False
    governed_runtime_session_id: str = ""
    governed_runtime_session_expires_at: str = ""
    governed_runtime_session_blocking_count: int = 0
    governed_runtime_session_warning_count: int = 0
    governed_runtime_session_recommended_action: str = ""
    runtime_boundary_state: str = ""
    runtime_boundary_ready: bool = False
    runtime_boundary_present: bool = False
    runtime_boundary_expired: bool = False
    runtime_boundary_id: str = ""
    runtime_boundary_invocation_id: str = ""
    runtime_boundary_expires_at: str = ""
    runtime_boundary_blocking_count: int = 0
    runtime_boundary_warning_count: int = 0
    runtime_boundary_recommended_action: str = ""
    governed_runtime_invocation_state: str = ""
    governed_runtime_invocation_ready: bool = False
    governed_runtime_invocation_present: bool = False
    governed_runtime_invocation_expired: bool = False
    governed_runtime_invocation_id: str = ""
    governed_runtime_invocation_expires_at: str = ""
    governed_runtime_invocation_phrase_verified: bool = False
    governed_runtime_invocation_blocking_count: int = 0
    governed_runtime_invocation_warning_count: int = 0
    governed_runtime_invocation_recommended_action: str = ""
    execution_authorization_state: str = ""
    execution_authorization_ready: bool = False
    execution_authorization_present: bool = False
    execution_authorization_expired: bool = False
    execution_authorization_id: str = ""
    execution_authorization_expires_at: str = ""
    execution_authorization_phrase_verified: bool = False
    execution_authorization_blocking_count: int = 0
    execution_authorization_warning_count: int = 0
    execution_authorization_recommended_action: str = ""
    governed_runtime_start_state: str = ""
    governed_runtime_start_ready: bool = False
    governed_runtime_start_present: bool = False
    governed_runtime_start_expired: bool = False
    governed_runtime_start_id: str = ""
    governed_runtime_start_expires_at: str = ""
    governed_runtime_start_started: bool = False
    governed_runtime_start_blocking_count: int = 0
    governed_runtime_start_warning_count: int = 0
    governed_runtime_start_recommended_action: str = ""


@dataclass(frozen=True)
class CooDispatchGatewayCorrelationDiff:
    """Safe read-only correlation diff between two gateway requests."""

    left_gateway_request_id: str
    right_gateway_request_id: str
    same_ticket: bool
    same_session: bool
    changed_fields_count: int
    changed_fields: tuple[str, ...]
    health_transition: str
    consume_transition: str
    recovery_transition: str
    correlation_transition: str
    regression_detected: bool
    recommended_action: str


def _normalize_filter_id(value: str, *, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise GatewayOperatorDashboardError(
            f"{field_name} must not contain path separators."
        )
    return normalized


def _normalize_dashboard_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_DASHBOARD_LIMIT
    if limit < 1:
        raise GatewayOperatorDashboardError("limit must be at least 1.")
    if limit > MAX_DASHBOARD_LIMIT:
        raise GatewayOperatorDashboardError(
            f"limit must not exceed {MAX_DASHBOARD_LIMIT}."
        )
    return limit


def _assert_within_hermes_home(resolved: Path, *, label: str) -> None:
    hermes_root = get_hermes_home().resolve()
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise GatewayOperatorDashboardError(
            f"Operator dashboard {label} must remain under Hermes home."
        ) from exc


def _read_request_entry(
    path: Path,
    *,
    request_dir: Path,
) -> tuple[CooDispatchGatewayRequestRecord, str]:
    if not path.is_file() or path.is_symlink():
        raise GatewayOperatorDashboardError("Gateway request path is invalid.")
    stem = path.stem
    if not stem or "/" in stem or "\\" in stem or stem in {".", ".."}:
        raise GatewayOperatorDashboardError(
            "Gateway request directory contains an invalid record id."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GatewayOperatorDashboardError(
            f"Gateway request record is corrupted: {stem}"
        ) from exc
    if not isinstance(payload, dict):
        raise GatewayOperatorDashboardError(
            f"Gateway request record is corrupted: {stem}"
        )
    record = read_gateway_request(stem, request_dir=request_dir)
    if record is None:
        raise GatewayOperatorDashboardError(
            f"Gateway request record is corrupted: {stem}"
        )
    updated_at = str(payload.get("updated_at") or "")
    return record, updated_at


def _list_filtered_gateway_requests(
    *,
    ticket_id: str = "",
    session_id: str = "",
    request_dir: Path | None = None,
    limit: int = DEFAULT_CORRELATION_SCAN_LIMIT,
) -> tuple[tuple[CooDispatchGatewayRequestRecord, str], ...]:
    base_dir = request_dir or default_gateway_request_dir()
    resolved_dir = base_dir.resolve()
    _assert_within_hermes_home(resolved_dir, label="request directory")
    if not resolved_dir.is_dir():
        return ()

    normalized_ticket = _normalize_filter_id(ticket_id, field_name="ticket_id")
    normalized_session = _normalize_filter_id(session_id, field_name="session_id")

    entries: list[tuple[CooDispatchGatewayRequestRecord, str]] = []
    for index, path in enumerate(sorted(resolved_dir.glob("*.json"))):
        if index >= limit:
            raise GatewayOperatorDashboardError("Gateway request scan limit exceeded.")
        if not path.is_file() or path.is_symlink():
            continue
        record, updated_at = _read_request_entry(path, request_dir=resolved_dir)
        if normalized_ticket and record.ticket_id != normalized_ticket:
            continue
        if normalized_session and record.session_id != normalized_session:
            continue
        entries.append((record, updated_at))

    entries.sort(
        key=lambda item: (item[1], item[0].gateway_request_id),
        reverse=True,
    )
    return tuple(entries)


def _chain_field_values(chain: CooDispatchGatewayCorrelationChain) -> dict[str, Any]:
    return {
        "request_status": chain.request_status,
        "pilot_status": chain.pilot_status,
        "execution_status": chain.execution_status,
        "evidence_present": chain.evidence_present,
        "audit_present": chain.audit_present,
        "consume_state": chain.consume_state,
        "consumed": chain.consumed,
        "recovery_required": chain.recovery_required,
        "repair_attempt_id": chain.repair_attempt_id,
        "repair_audit_present": chain.repair_audit_present,
        "repair_lock_held": chain.repair_lock_held,
        "correlation_valid": chain.correlation_valid,
        "chain_complete": chain.chain_complete,
        "failure_reason_code": chain.failure_reason_code,
        "recommended_action": chain.recommended_action,
    }


def _mini_health_from_chain(chain: CooDispatchGatewayCorrelationChain) -> str:
    if not chain.production_root_hard_deny or chain.production_execution_allowed:
        return DASHBOARD_HEALTH_BLOCKED
    if chain.recovery_required or chain.repair_lock_held:
        return DASHBOARD_HEALTH_BLOCKED
    if chain.consume_state in {
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_LEGACY_PARTIAL,
        CONSUME_STATE_PREPARED,
        CONSUME_STATE_RECOVERY_REQUIRED,
    }:
        return DASHBOARD_HEALTH_BLOCKED
    if not chain.correlation_valid:
        return DASHBOARD_HEALTH_BLOCKED
    if chain.request_status == "failed" or chain.pilot_status in {
        PILOT_STATUS_FAILURE,
        PILOT_STATUS_TIMEOUT,
    }:
        return DASHBOARD_HEALTH_DEGRADED
    if not chain.evidence_present or not chain.audit_present:
        return DASHBOARD_HEALTH_DEGRADED
    if chain.chain_complete and chain.correlation_valid:
        return DASHBOARD_HEALTH_HEALTHY
    return DASHBOARD_HEALTH_DEGRADED


def _detect_regression(
    left: CooDispatchGatewayCorrelationChain,
    right: CooDispatchGatewayCorrelationChain,
) -> bool:
    if left.request_status == "completed" and right.request_status == "failed":
        return True
    if left.evidence_present and not right.evidence_present:
        return True
    if left.audit_present and not right.audit_present:
        return True
    if left.consume_state == "committed" and right.consume_state in {
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_RECOVERY_REQUIRED,
    }:
        return True
    if left.correlation_valid and not right.correlation_valid:
        return True
    if left.chain_complete and not right.chain_complete:
        return True
    if not left.recovery_required and right.recovery_required:
        return True
    if not left.repair_lock_held and right.repair_lock_held:
        return True
    return False


def _transition_label(left_value: Any, right_value: Any) -> str:
    if left_value == right_value:
        return _TRANSITION_UNCHANGED
    return f"{left_value}->{right_value}"


def _dashboard_recommended_action(
    *,
    dashboard_health: str,
    gateway_state: str,
    regression_status: str,
    total_attempts: int,
    dry_run_count: int,
    consecutive_failures: int,
    correlation_valid: bool,
    recovery_required: bool,
    repair_lock_held: bool,
    evidence_present: bool,
    audit_present: bool,
    production_policy_valid: bool,
) -> str:
    if not production_policy_valid:
        return DASHBOARD_ACTION_MAINTAIN_PRODUCTION_BLOCK
    if gateway_state == GATEWAY_STATE_DISABLED:
        return DASHBOARD_ACTION_STAGE_GATEWAY
    if not correlation_valid:
        return DASHBOARD_ACTION_RESOLVE_CORRELATION_MISMATCH
    if recovery_required or repair_lock_held:
        return DASHBOARD_ACTION_RESOLVE_RECOVERY_REQUIRED
    if total_attempts == 0:
        return DASHBOARD_ACTION_COLLECT_MORE_HISTORY
    if dry_run_count == total_attempts and total_attempts > 0:
        return DASHBOARD_ACTION_RUN_GATEWAY_PILOT_DRY_RUN
    if regression_status == REGRESSION_STATUS_FAIL or consecutive_failures > 0:
        return DASHBOARD_ACTION_INSPECT_LATEST_FAILURE
    if not evidence_present or not audit_present:
        return DASHBOARD_ACTION_INSPECT_MISSING_EVIDENCE
    if dashboard_health == DASHBOARD_HEALTH_HEALTHY:
        return DASHBOARD_ACTION_NO_ACTION_REQUIRED
    if dashboard_health == DASHBOARD_HEALTH_DEGRADED:
        if consecutive_failures == 1:
            return DASHBOARD_ACTION_INSPECT_LATEST_FAILURE
        return DASHBOARD_ACTION_COLLECT_MORE_HISTORY
    if dashboard_health == DASHBOARD_HEALTH_BLOCKED:
        if recovery_required:
            return DASHBOARD_ACTION_RESOLVE_RECOVERY_REQUIRED
        if not correlation_valid:
            return DASHBOARD_ACTION_RESOLVE_CORRELATION_MISMATCH
        return DASHBOARD_ACTION_MAINTAIN_PRODUCTION_BLOCK
    return DASHBOARD_ACTION_COLLECT_MORE_HISTORY


def _dashboard_health(
    *,
    gateway_state: str,
    readiness_level: str,
    signoff_ready: bool,
    cutover_ready: bool,
    regression_status: str,
    trend_status: str,
    consecutive_failures: int,
    total_attempts: int,
    dry_run_count: int,
    correlation_valid: bool,
    recovery_required: bool,
    repair_lock_held: bool,
    consume_state: str,
    evidence_present: bool,
    audit_present: bool,
    latest_request_status: str,
    production_policy_valid: bool,
    history_corrupted: bool,
    request_corrupted: bool,
) -> str:
    if history_corrupted or request_corrupted:
        return DASHBOARD_HEALTH_BLOCKED
    if not production_policy_valid:
        return DASHBOARD_HEALTH_BLOCKED
    if gateway_state == GATEWAY_STATE_DISABLED:
        return DASHBOARD_HEALTH_NOT_CONFIGURED
    if gateway_state == GATEWAY_STATE_ENABLED:
        return DASHBOARD_HEALTH_BLOCKED
    if not correlation_valid:
        return DASHBOARD_HEALTH_BLOCKED
    if recovery_required or repair_lock_held:
        return DASHBOARD_HEALTH_BLOCKED
    if consume_state in {
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_LEGACY_PARTIAL,
        CONSUME_STATE_PREPARED,
        CONSUME_STATE_RECOVERY_REQUIRED,
    }:
        return DASHBOARD_HEALTH_BLOCKED
    if regression_status == REGRESSION_STATUS_FAIL and consecutive_failures >= 2:
        return DASHBOARD_HEALTH_BLOCKED
    if not signoff_ready or not cutover_ready:
        return DASHBOARD_HEALTH_BLOCKED
    if readiness_level != READINESS_LEVEL_READY_FOR_MOCK_DISPATCH:
        return DASHBOARD_HEALTH_BLOCKED

    if total_attempts == 0:
        return DASHBOARD_HEALTH_DEGRADED
    if dry_run_count == total_attempts:
        return DASHBOARD_HEALTH_DEGRADED
    if trend_status == TREND_STATUS_INSUFFICIENT_DATA:
        return DASHBOARD_HEALTH_DEGRADED
    if consecutive_failures == 1:
        return DASHBOARD_HEALTH_DEGRADED
    if regression_status == REGRESSION_STATUS_FAIL and consecutive_failures == 1:
        return DASHBOARD_HEALTH_DEGRADED
    if trend_status == TREND_STATUS_DEGRADED and consecutive_failures > 0:
        return DASHBOARD_HEALTH_DEGRADED
    if latest_request_status == "failed":
        return DASHBOARD_HEALTH_DEGRADED
    if not evidence_present or not audit_present:
        return DASHBOARD_HEALTH_DEGRADED

    if (
        gateway_state == GATEWAY_STATE_STAGED
        and regression_status in {REGRESSION_STATUS_PASS, REGRESSION_STATUS_WARN}
        and correlation_valid
        and not recovery_required
        and not repair_lock_held
    ):
        return DASHBOARD_HEALTH_HEALTHY

    return DASHBOARD_HEALTH_DEGRADED


def build_operator_dashboard_summary(
    *,
    ticket_id: str = "",
    session_id: str = "",
    limit: int | None = None,
    merged_config: Mapping[str, Any] | None = None,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> CooDispatchGatewayOperatorDashboardSummary:
    """Build read-only operator dashboard summary."""
    if merged_config is None:
        merged_config = {}

    normalized_ticket = _normalize_filter_id(ticket_id, field_name="ticket_id")
    normalized_session = _normalize_filter_id(session_id, field_name="session_id")
    scan_limit = _normalize_dashboard_limit(limit)

    history_corrupted = False
    request_corrupted = False

    enablement = load_dispatch_gateway_enablement(merged_config=merged_config)
    facade = evaluate_gateway_execution_facade(merged_config=merged_config)
    gateway_state = enablement.gateway_state

    readiness = evaluate_dispatch_gateway_readiness(merged_config=merged_config)
    readiness_level = readiness.readiness_level

    signoff = evaluate_dispatch_production_signoff(merged_config=merged_config)
    cutover = evaluate_production_cutover_checklist(merged_config=merged_config)

    from agent.coo.dispatch_cli_pilot_regression import CooDispatchPilotRegressionSummary
    from agent.coo.dispatch_cli_pilot_runbook import CooDispatchPilotTrendSummary

    try:
        regression = evaluate_pilot_regression(
            ticket_id=normalized_ticket or None,
            history_dir=history_dir,
        )
    except ValueError:
        history_corrupted = True
        regression = CooDispatchPilotRegressionSummary(
            total_attempts=0,
            completed_count=0,
            failed_count=0,
            timeout_count=0,
            dry_run_count=0,
            consumed_success_count=0,
            latest_status=_NONE_LABEL,
            latest_pilot_attempt_id=_NONE_LABEL,
            consecutive_failures=0,
            evidence_missing_count=0,
            audit_missing_count=0,
            production_policy_violations=0,
            regression_status=REGRESSION_STATUS_FAIL,
        )

    try:
        trend = evaluate_pilot_trend(
            ticket_id=normalized_ticket or None,
            history_dir=history_dir,
        )
    except ValueError:
        history_corrupted = True
        trend = CooDispatchPilotTrendSummary(
            pass_count=0,
            fail_count=0,
            timeout_count=0,
            dry_run_count=0,
            success_rate_percent=0,
            consecutive_failures=0,
            failure_reason_counts=_NONE_LABEL,
            trend_status=TREND_STATUS_INSUFFICIENT_DATA,
        )

    try:
        recent_entries = _list_filtered_gateway_requests(
            ticket_id=normalized_ticket,
            session_id=normalized_session,
            request_dir=request_dir,
            limit=DEFAULT_CORRELATION_SCAN_LIMIT,
        )
    except DispatchGatewayRequestStoreError as exc:
        raise GatewayOperatorDashboardError(str(exc)) from exc

    recent_limited = recent_entries[:scan_limit]
    total_recent_requests = len(recent_limited)
    failed_recent_requests = sum(
        1 for record, _updated_at in recent_limited if record.status == "failed"
    )

    latest_chain: CooDispatchGatewayCorrelationChain | None = None
    if recent_limited:
        latest_record = recent_limited[0][0]
        try:
            latest_chain = explore_gateway_correlation(
                GatewayCorrelationQuery(
                    query_type=QUERY_TYPE_GATEWAY_REQUEST,
                    query_id=latest_record.gateway_request_id,
                ),
                request_dir=request_dir,
                history_dir=history_dir,
                evidence_dir=evidence_dir,
                audit_dir=audit_dir,
                bundle_dir=bundle_dir,
                confirmation_dir=confirmation_dir,
            )
        except (GatewayCorrelationExplorerError, KeyError, ValueError):
            request_corrupted = True

    production_policy_valid = (
        enablement.production_root_hard_deny
        and not enablement.production_execution_allowed
        and regression.production_policy_violations == 0
    )

    correlation_valid = latest_chain.correlation_valid if latest_chain else True
    recovery_required = latest_chain.recovery_required if latest_chain else False
    repair_lock_held = latest_chain.repair_lock_held if latest_chain else False
    consume_state = latest_chain.consume_state if latest_chain else _NONE_LABEL
    evidence_present = latest_chain.evidence_present if latest_chain else False
    audit_present = latest_chain.audit_present if latest_chain else False
    consumed = latest_chain.consumed if latest_chain else False
    chain_complete = latest_chain.chain_complete if latest_chain else False
    latest_request_status = (
        latest_chain.request_status if latest_chain else _NONE_LABEL
    )

    dashboard_health = _dashboard_health(
        gateway_state=gateway_state,
        readiness_level=readiness_level,
        signoff_ready=signoff.signoff_ready,
        cutover_ready=cutover.cutover_ready,
        regression_status=regression.regression_status,
        trend_status=trend.trend_status,
        consecutive_failures=regression.consecutive_failures,
        total_attempts=regression.total_attempts,
        dry_run_count=regression.dry_run_count,
        correlation_valid=correlation_valid,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        consume_state=consume_state,
        evidence_present=evidence_present,
        audit_present=audit_present,
        latest_request_status=latest_request_status,
        production_policy_valid=production_policy_valid,
        history_corrupted=history_corrupted,
        request_corrupted=request_corrupted,
    )

    recommended_action = _dashboard_recommended_action(
        dashboard_health=dashboard_health,
        gateway_state=gateway_state,
        regression_status=regression.regression_status,
        total_attempts=regression.total_attempts,
        dry_run_count=regression.dry_run_count,
        consecutive_failures=regression.consecutive_failures,
        correlation_valid=correlation_valid,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        evidence_present=evidence_present,
        audit_present=audit_present,
        production_policy_valid=production_policy_valid,
    )

    from agent.coo.production_live_operational_signoff import (
        resolve_latest_live_pilot_dashboard_digest,
    )
    from agent.coo.production_live_rollback_validation import (
        resolve_latest_rollback_dashboard_digest,
    )
    from agent.coo.production_final_signoff import (
        resolve_latest_final_signoff_dashboard_digest,
    )
    from agent.coo.production_governed_cutover import (
        resolve_latest_governed_cutover_dashboard_digest,
    )
    from agent.coo.production_controlled_window import (
        resolve_latest_controlled_window_dashboard_digest,
    )
    from agent.coo.production_runtime_permission import (
        resolve_latest_runtime_permission_dashboard_digest,
    )
    from agent.coo.production_governed_runtime_session import (
        resolve_latest_governed_runtime_session_dashboard_digest,
    )
    from agent.coo.production_runtime_boundary import (
        resolve_latest_runtime_boundary_dashboard_digest,
    )
    from agent.coo.production_runtime_invocation import (
        resolve_latest_governed_runtime_invocation_dashboard_digest,
    )
    from agent.coo.production_execution_authorization import (
        resolve_latest_execution_authorization_dashboard_digest,
    )
    from agent.coo.production_runtime_start import (
        resolve_latest_governed_runtime_start_dashboard_digest,
    )

    live_pilot = resolve_latest_live_pilot_dashboard_digest(merged_config=merged_config)
    rollback = resolve_latest_rollback_dashboard_digest(merged_config=merged_config)
    final_signoff = resolve_latest_final_signoff_dashboard_digest(
        merged_config=merged_config
    )
    governed_cutover = resolve_latest_governed_cutover_dashboard_digest(
        merged_config=merged_config
    )
    controlled_window = resolve_latest_controlled_window_dashboard_digest(
        merged_config=merged_config
    )
    runtime_permission = resolve_latest_runtime_permission_dashboard_digest(
        merged_config=merged_config
    )
    governed_runtime_session = resolve_latest_governed_runtime_session_dashboard_digest(
        merged_config=merged_config
    )
    runtime_boundary = resolve_latest_runtime_boundary_dashboard_digest(
        merged_config=merged_config
    )
    governed_runtime_invocation = (
        resolve_latest_governed_runtime_invocation_dashboard_digest(
            merged_config=merged_config
        )
    )
    execution_authorization = resolve_latest_execution_authorization_dashboard_digest(
        merged_config=merged_config
    )
    governed_runtime_start = resolve_latest_governed_runtime_start_dashboard_digest(
        merged_config=merged_config
    )

    return CooDispatchGatewayOperatorDashboardSummary(
        dashboard_health=dashboard_health,
        gateway_state=gateway_state,
        readiness_level=readiness_level,
        signoff_ready=signoff.signoff_ready,
        cutover_ready=cutover.cutover_ready,
        regression_status=regression.regression_status,
        trend_status=trend.trend_status,
        latest_gateway_request_id=(
            latest_chain.gateway_request_id if latest_chain else _NONE_LABEL
        ),
        latest_pilot_attempt_id=(
            latest_chain.pilot_attempt_id if latest_chain else _NONE_LABEL
        ),
        latest_execution_attempt_id=(
            latest_chain.execution_attempt_id if latest_chain else _NONE_LABEL
        ),
        latest_dispatch_run_id=(
            latest_chain.dispatch_run_id if latest_chain else _NONE_LABEL
        ),
        latest_request_status=latest_request_status,
        evidence_present=evidence_present,
        audit_present=audit_present,
        consume_state=consume_state,
        consumed=consumed,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        correlation_valid=correlation_valid,
        chain_complete=chain_complete,
        consecutive_failures=regression.consecutive_failures,
        total_recent_requests=total_recent_requests,
        failed_recent_requests=failed_recent_requests,
        recommended_action=recommended_action,
        production_execution_allowed=False,
        production_root_hard_deny=enablement.production_root_hard_deny,
        gateway_execution_scope=EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
        facade_connected=facade.facade_connected,
        live_pilot_status=live_pilot.live_pilot_status,
        live_pilot_signoff_status=live_pilot.live_pilot_signoff_status,
        live_pilot_activation_request_id=live_pilot.latest_activation_request_id,
        live_pilot_recommended_action=live_pilot.recommended_action,
        rollback_validation_status=rollback.rollback_validation_status,
        rollback_ready=rollback.rollback_ready,
        rollback_cleanup_required=rollback.rollback_cleanup_required,
        rollback_recommended_action=rollback.rollback_recommended_action,
        production_final_signoff_status=final_signoff.production_final_signoff_status,
        production_release_ready=final_signoff.production_release_ready,
        production_final_signoff_present=final_signoff.production_final_signoff_present,
        production_final_blocking_count=final_signoff.production_final_blocking_count,
        production_final_warning_count=final_signoff.production_final_warning_count,
        production_final_recommended_action=final_signoff.production_final_recommended_action,
        governed_cutover_status=governed_cutover.governed_cutover_status,
        governed_cutover_ready=governed_cutover.governed_cutover_ready,
        governed_cutover_contract_present=governed_cutover.governed_cutover_contract_present,
        governed_cutover_window_valid=governed_cutover.governed_cutover_window_valid,
        governed_cutover_blocking_count=governed_cutover.governed_cutover_blocking_count,
        governed_cutover_warning_count=governed_cutover.governed_cutover_warning_count,
        governed_cutover_recommended_action=governed_cutover.governed_cutover_recommended_action,
        controlled_window_state=controlled_window.controlled_window_state,
        controlled_window_open=controlled_window.controlled_window_open,
        controlled_window_expired=controlled_window.controlled_window_expired,
        controlled_window_contract_id=controlled_window.controlled_window_contract_id,
        controlled_window_event_count=controlled_window.controlled_window_event_count,
        controlled_window_blocking_count=controlled_window.controlled_window_blocking_count,
        controlled_window_warning_count=controlled_window.controlled_window_warning_count,
        controlled_window_recommended_action=controlled_window.controlled_window_recommended_action,
        runtime_permission_state=runtime_permission.runtime_permission_state,
        runtime_permission_ready=runtime_permission.runtime_permission_ready,
        runtime_permission_present=runtime_permission.runtime_permission_present,
        runtime_permission_expired=runtime_permission.runtime_permission_expired,
        runtime_permission_id=runtime_permission.runtime_permission_id,
        runtime_permission_expires_at=runtime_permission.runtime_permission_expires_at,
        runtime_permission_blocking_count=runtime_permission.runtime_permission_blocking_count,
        runtime_permission_warning_count=runtime_permission.runtime_permission_warning_count,
        runtime_permission_recommended_action=(
            runtime_permission.runtime_permission_recommended_action
        ),
        governed_runtime_session_state=(
            governed_runtime_session.governed_runtime_session_state
        ),
        governed_runtime_session_ready=(
            governed_runtime_session.governed_runtime_session_ready
        ),
        governed_runtime_session_present=(
            governed_runtime_session.governed_runtime_session_present
        ),
        governed_runtime_session_expired=(
            governed_runtime_session.governed_runtime_session_expired
        ),
        governed_runtime_session_id=governed_runtime_session.governed_runtime_session_id,
        governed_runtime_session_expires_at=(
            governed_runtime_session.governed_runtime_session_expires_at
        ),
        governed_runtime_session_blocking_count=(
            governed_runtime_session.governed_runtime_session_blocking_count
        ),
        governed_runtime_session_warning_count=(
            governed_runtime_session.governed_runtime_session_warning_count
        ),
        governed_runtime_session_recommended_action=(
            governed_runtime_session.governed_runtime_session_recommended_action
        ),
        runtime_boundary_state=(
            runtime_boundary.runtime_boundary_state
        ),
        runtime_boundary_ready=(
            runtime_boundary.runtime_boundary_ready
        ),
        runtime_boundary_present=(
            runtime_boundary.runtime_boundary_present
        ),
        runtime_boundary_expired=(
            runtime_boundary.runtime_boundary_expired
        ),
        runtime_boundary_id=(
            runtime_boundary.runtime_boundary_id
        ),
        runtime_boundary_invocation_id=(
            runtime_boundary.runtime_boundary_invocation_id
        ),
        runtime_boundary_expires_at=(
            runtime_boundary.runtime_boundary_expires_at
        ),
        runtime_boundary_blocking_count=(
            runtime_boundary.runtime_boundary_blocking_count
        ),
        runtime_boundary_warning_count=(
            runtime_boundary.runtime_boundary_warning_count
        ),
        runtime_boundary_recommended_action=(
            runtime_boundary.runtime_boundary_recommended_action
        ),
        governed_runtime_invocation_state=(
            governed_runtime_invocation.governed_runtime_invocation_state
        ),
        governed_runtime_invocation_ready=(
            governed_runtime_invocation.governed_runtime_invocation_ready
        ),
        governed_runtime_invocation_present=(
            governed_runtime_invocation.governed_runtime_invocation_present
        ),
        governed_runtime_invocation_expired=(
            governed_runtime_invocation.governed_runtime_invocation_expired
        ),
        governed_runtime_invocation_id=(
            governed_runtime_invocation.governed_runtime_invocation_id
        ),
        governed_runtime_invocation_expires_at=(
            governed_runtime_invocation.governed_runtime_invocation_expires_at
        ),
        governed_runtime_invocation_phrase_verified=(
            governed_runtime_invocation.governed_runtime_invocation_phrase_verified
        ),
        governed_runtime_invocation_blocking_count=(
            governed_runtime_invocation.governed_runtime_invocation_blocking_count
        ),
        governed_runtime_invocation_warning_count=(
            governed_runtime_invocation.governed_runtime_invocation_warning_count
        ),
        governed_runtime_invocation_recommended_action=(
            governed_runtime_invocation.governed_runtime_invocation_recommended_action
        ),
        execution_authorization_state=(
            execution_authorization.execution_authorization_state
        ),
        execution_authorization_ready=(
            execution_authorization.execution_authorization_ready
        ),
        execution_authorization_present=(
            execution_authorization.execution_authorization_present
        ),
        execution_authorization_expired=(
            execution_authorization.execution_authorization_expired
        ),
        execution_authorization_id=(
            execution_authorization.execution_authorization_id
        ),
        execution_authorization_expires_at=(
            execution_authorization.execution_authorization_expires_at
        ),
        execution_authorization_phrase_verified=(
            execution_authorization.execution_authorization_phrase_verified
        ),
        execution_authorization_blocking_count=(
            execution_authorization.execution_authorization_blocking_count
        ),
        execution_authorization_warning_count=(
            execution_authorization.execution_authorization_warning_count
        ),
        execution_authorization_recommended_action=(
            execution_authorization.execution_authorization_recommended_action
        ),
        governed_runtime_start_state=(
            governed_runtime_start.governed_runtime_start_state
        ),
        governed_runtime_start_ready=(
            governed_runtime_start.governed_runtime_start_ready
        ),
        governed_runtime_start_present=(
            governed_runtime_start.governed_runtime_start_present
        ),
        governed_runtime_start_expired=(
            governed_runtime_start.governed_runtime_start_expired
        ),
        governed_runtime_start_id=(
            governed_runtime_start.governed_runtime_start_id
        ),
        governed_runtime_start_expires_at=(
            governed_runtime_start.governed_runtime_start_expires_at
        ),
        governed_runtime_start_started=(
            governed_runtime_start.governed_runtime_start_started
        ),
        governed_runtime_start_blocking_count=(
            governed_runtime_start.governed_runtime_start_blocking_count
        ),
        governed_runtime_start_warning_count=(
            governed_runtime_start.governed_runtime_start_warning_count
        ),
        governed_runtime_start_recommended_action=(
            governed_runtime_start.governed_runtime_start_recommended_action
        ),
    )


def build_gateway_correlation_diff(
    *,
    left_gateway_request_id: str,
    right_gateway_request_id: str,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> CooDispatchGatewayCorrelationDiff:
    """Build read-only correlation diff for two gateway requests."""
    try:
        left_id = normalize_gateway_request_id(left_gateway_request_id)
        right_id = normalize_gateway_request_id(right_gateway_request_id)
    except DispatchGatewayRequestStoreError as exc:
        raise GatewayOperatorDashboardError(str(exc)) from exc

    if left_id == right_id:
        raise GatewayOperatorDashboardError(
            "left and right gateway_request_id must differ."
        )

    try:
        left_chain = explore_gateway_correlation(
            GatewayCorrelationQuery(
                query_type=QUERY_TYPE_GATEWAY_REQUEST,
                query_id=left_id,
            ),
            request_dir=request_dir,
            history_dir=history_dir,
            evidence_dir=evidence_dir,
            audit_dir=audit_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
        right_chain = explore_gateway_correlation(
            GatewayCorrelationQuery(
                query_type=QUERY_TYPE_GATEWAY_REQUEST,
                query_id=right_id,
            ),
            request_dir=request_dir,
            history_dir=history_dir,
            evidence_dir=evidence_dir,
            audit_dir=audit_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
    except GatewayCorrelationExplorerError as exc:
        raise GatewayOperatorDashboardError(str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise GatewayOperatorDashboardError(str(exc)) from exc

    same_ticket = (
        left_chain.ticket_id not in {"", _NONE_LABEL}
        and left_chain.ticket_id == right_chain.ticket_id
    )
    same_session = (
        left_chain.session_id not in {"", _NONE_LABEL}
        and left_chain.session_id == right_chain.session_id
    )

    left_values = _chain_field_values(left_chain)
    right_values = _chain_field_values(right_chain)
    changed_fields = tuple(
        field
        for field in _DIFF_COMPARE_FIELDS
        if left_values[field] != right_values[field]
    )

    left_health = _mini_health_from_chain(left_chain)
    right_health = _mini_health_from_chain(right_chain)
    health_transition = _transition_label(left_health, right_health)

    consume_transition = _transition_label(
        left_chain.consume_state,
        right_chain.consume_state,
    )
    recovery_transition = _transition_label(
        left_chain.recovery_required,
        right_chain.recovery_required,
    )
    correlation_transition = _transition_label(
        left_chain.correlation_valid,
        right_chain.correlation_valid,
    )

    regression_detected = _detect_regression(left_chain, right_chain)

    if not same_ticket:
        recommended_action = DIFF_ACTION_PROVIDE_SAME_TICKET_REQUESTS
    elif regression_detected:
        if consume_transition != _TRANSITION_UNCHANGED:
            recommended_action = DIFF_ACTION_INSPECT_CONSUME_DRIFT
        elif right_chain.recovery_required:
            recommended_action = DIFF_ACTION_RESOLVE_RECOVERY_REQUIRED
        elif not right_chain.correlation_valid:
            recommended_action = DIFF_ACTION_RESOLVE_CORRELATION_MISMATCH
        else:
            recommended_action = DIFF_ACTION_INSPECT_REGRESSION
    elif not right_chain.correlation_valid:
        recommended_action = DIFF_ACTION_RESOLVE_CORRELATION_MISMATCH
    else:
        recommended_action = DIFF_ACTION_NO_ACTION_REQUIRED

    return CooDispatchGatewayCorrelationDiff(
        left_gateway_request_id=left_id,
        right_gateway_request_id=right_id,
        same_ticket=same_ticket,
        same_session=same_session,
        changed_fields_count=len(changed_fields),
        changed_fields=changed_fields,
        health_transition=health_transition,
        consume_transition=consume_transition,
        recovery_transition=recovery_transition,
        correlation_transition=correlation_transition,
        regression_detected=regression_detected,
        recommended_action=recommended_action,
    )


def dashboard_exit_code(summary: CooDispatchGatewayOperatorDashboardSummary) -> int:
    """Return CLI exit code for dashboard summary."""
    if summary.dashboard_health == DASHBOARD_HEALTH_BLOCKED:
        return 1
    return 0


def correlation_diff_exit_code(diff: CooDispatchGatewayCorrelationDiff) -> int:
    """Return CLI exit code for correlation diff."""
    if not diff.same_ticket:
        return 1
    if diff.regression_detected:
        return 1
    if diff.recommended_action in {
        DIFF_ACTION_RESOLVE_CORRELATION_MISMATCH,
        DIFF_ACTION_RESOLVE_RECOVERY_REQUIRED,
    }:
        return 1
    return 0


def _assert_safe_output(output: str) -> None:
    lowered = output.lower()
    for token in _FORBIDDEN_OUTPUT_KEYS:
        if token in lowered:
            raise GatewayOperatorDashboardError(
                f"Unsafe operator dashboard output field: {token!r}"
            )


def format_operator_dashboard_summary(
    summary: CooDispatchGatewayOperatorDashboardSummary,
) -> str:
    """Format safe operator dashboard output."""
    lines = [
        "Gateway Operator Dashboard",
        "",
        "[Health]",
        f"dashboard_health: {summary.dashboard_health}",
        f"recommended_action: {summary.recommended_action}",
    ]
    from agent.coo.dispatch_operator_guidance import append_guidance_output_lines

    append_guidance_output_lines(lines, summary.recommended_action)
    lines.extend(
        [
            "",
            "[Gateway]",
            f"gateway_state: {summary.gateway_state}",
            f"facade_connected: {str(summary.facade_connected).lower()}",
            f"readiness_level: {summary.readiness_level}",
            f"signoff_ready: {str(summary.signoff_ready).lower()}",
            f"cutover_ready: {str(summary.cutover_ready).lower()}",
            "",
            "[Pilot]",
            f"regression_status: {summary.regression_status}",
            f"trend_status: {summary.trend_status}",
            f"consecutive_failures: {summary.consecutive_failures}",
            "",
            "[Latest Request]",
            f"latest_gateway_request_id: {summary.latest_gateway_request_id}",
            f"latest_pilot_attempt_id: {summary.latest_pilot_attempt_id}",
            f"latest_execution_attempt_id: {summary.latest_execution_attempt_id}",
            f"latest_dispatch_run_id: {summary.latest_dispatch_run_id}",
            f"latest_request_status: {summary.latest_request_status}",
            "",
            "[Evidence & Audit]",
            f"evidence_present: {str(summary.evidence_present).lower()}",
            f"audit_present: {str(summary.audit_present).lower()}",
            "",
            "[Consume & Repair]",
            f"consume_state: {summary.consume_state}",
            f"consumed: {str(summary.consumed).lower()}",
            f"recovery_required: {str(summary.recovery_required).lower()}",
            f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
            "",
            "[Correlation]",
            f"correlation_valid: {str(summary.correlation_valid).lower()}",
            f"chain_complete: {str(summary.chain_complete).lower()}",
            "",
            "[Recent]",
            f"total_recent_requests: {summary.total_recent_requests}",
            f"failed_recent_requests: {summary.failed_recent_requests}",
            "",
            "[Safety]",
            "production_execution_allowed: false",
            f"production_root_hard_deny: {str(summary.production_root_hard_deny).lower()}",
            f"gateway_execution_scope: {summary.gateway_execution_scope}",
            "",
            "[Live Pilot]",
            f"live_pilot_status: {summary.live_pilot_status or _NONE_LABEL}",
            f"live_pilot_signoff_status: {summary.live_pilot_signoff_status or _NONE_LABEL}",
            f"live_pilot_activation_request_id: {summary.live_pilot_activation_request_id or _NONE_LABEL}",
            f"live_pilot_recommended_action: {summary.live_pilot_recommended_action or _NONE_LABEL}",
            f"rollback_validation_status: {summary.rollback_validation_status or _NONE_LABEL}",
            f"rollback_ready: {str(summary.rollback_ready).lower()}",
            f"rollback_cleanup_required: {str(summary.rollback_cleanup_required).lower()}",
            f"rollback_recommended_action: {summary.rollback_recommended_action or _NONE_LABEL}",
            f"production_final_signoff_status: {summary.production_final_signoff_status or _NONE_LABEL}",
            f"production_release_ready: {str(summary.production_release_ready).lower()}",
            "production_final_signoff_present: "
            f"{str(summary.production_final_signoff_present).lower()}",
            f"production_final_blocking_count: {summary.production_final_blocking_count}",
            f"production_final_warning_count: {summary.production_final_warning_count}",
            "production_final_recommended_action: "
            f"{summary.production_final_recommended_action or _NONE_LABEL}",
            f"governed_cutover_status: {summary.governed_cutover_status or _NONE_LABEL}",
            f"governed_cutover_ready: {str(summary.governed_cutover_ready).lower()}",
            "governed_cutover_contract_present: "
            f"{str(summary.governed_cutover_contract_present).lower()}",
            "governed_cutover_window_valid: "
            f"{str(summary.governed_cutover_window_valid).lower()}",
            f"governed_cutover_blocking_count: {summary.governed_cutover_blocking_count}",
            f"governed_cutover_warning_count: {summary.governed_cutover_warning_count}",
            "governed_cutover_recommended_action: "
            f"{summary.governed_cutover_recommended_action or _NONE_LABEL}",
            f"controlled_window_state: {summary.controlled_window_state or _NONE_LABEL}",
            f"controlled_window_open: {str(summary.controlled_window_open).lower()}",
            "controlled_window_expired: "
            f"{str(summary.controlled_window_expired).lower()}",
            "controlled_window_contract_id: "
            f"{summary.controlled_window_contract_id or _NONE_LABEL}",
            f"controlled_window_event_count: {summary.controlled_window_event_count}",
            "controlled_window_blocking_count: "
            f"{summary.controlled_window_blocking_count}",
            "controlled_window_warning_count: "
            f"{summary.controlled_window_warning_count}",
            "controlled_window_recommended_action: "
            f"{summary.controlled_window_recommended_action or _NONE_LABEL}",
            f"runtime_permission_state: {summary.runtime_permission_state or _NONE_LABEL}",
            f"runtime_permission_ready: {str(summary.runtime_permission_ready).lower()}",
            "runtime_permission_present: "
            f"{str(summary.runtime_permission_present).lower()}",
            "runtime_permission_expired: "
            f"{str(summary.runtime_permission_expired).lower()}",
            f"runtime_permission_id: {summary.runtime_permission_id or _NONE_LABEL}",
            "runtime_permission_expires_at: "
            f"{summary.runtime_permission_expires_at or _NONE_LABEL}",
            "runtime_permission_blocking_count: "
            f"{summary.runtime_permission_blocking_count}",
            "runtime_permission_warning_count: "
            f"{summary.runtime_permission_warning_count}",
            "runtime_permission_recommended_action: "
            f"{summary.runtime_permission_recommended_action or _NONE_LABEL}",
            "governed_runtime_session_state: "
            f"{summary.governed_runtime_session_state or _NONE_LABEL}",
            "governed_runtime_session_ready: "
            f"{str(summary.governed_runtime_session_ready).lower()}",
            "governed_runtime_session_present: "
            f"{str(summary.governed_runtime_session_present).lower()}",
            "governed_runtime_session_expired: "
            f"{str(summary.governed_runtime_session_expired).lower()}",
            "governed_runtime_session_id: "
            f"{summary.governed_runtime_session_id or _NONE_LABEL}",
            "governed_runtime_session_expires_at: "
            f"{summary.governed_runtime_session_expires_at or _NONE_LABEL}",
            "governed_runtime_session_blocking_count: "
            f"{summary.governed_runtime_session_blocking_count}",
            "governed_runtime_session_warning_count: "
            f"{summary.governed_runtime_session_warning_count}",
            "governed_runtime_session_recommended_action: "
            f"{summary.governed_runtime_session_recommended_action or _NONE_LABEL}",
            "runtime_boundary_state: "
            f"{summary.runtime_boundary_state or _NONE_LABEL}",
            "runtime_boundary_ready: "
            f"{str(summary.runtime_boundary_ready).lower()}",
            "runtime_boundary_present: "
            f"{str(summary.runtime_boundary_present).lower()}",
            "runtime_boundary_expired: "
            f"{str(summary.runtime_boundary_expired).lower()}",
            "runtime_boundary_id: "
            f"{summary.runtime_boundary_id or _NONE_LABEL}",
            "runtime_boundary_invocation_id: "
            f"{summary.runtime_boundary_invocation_id or _NONE_LABEL}",
            "runtime_boundary_expires_at: "
            f"{summary.runtime_boundary_expires_at or _NONE_LABEL}",
            "runtime_boundary_blocking_count: "
            f"{summary.runtime_boundary_blocking_count}",
            "runtime_boundary_warning_count: "
            f"{summary.runtime_boundary_warning_count}",
            "runtime_boundary_recommended_action: "
            f"{summary.runtime_boundary_recommended_action or _NONE_LABEL}",
            "governed_runtime_invocation_state: "
            f"{summary.governed_runtime_invocation_state or _NONE_LABEL}",
            "governed_runtime_invocation_ready: "
            f"{str(summary.governed_runtime_invocation_ready).lower()}",
            "governed_runtime_invocation_present: "
            f"{str(summary.governed_runtime_invocation_present).lower()}",
            "governed_runtime_invocation_expired: "
            f"{str(summary.governed_runtime_invocation_expired).lower()}",
            "governed_runtime_invocation_id: "
            f"{summary.governed_runtime_invocation_id or _NONE_LABEL}",
            "governed_runtime_invocation_expires_at: "
            f"{summary.governed_runtime_invocation_expires_at or _NONE_LABEL}",
            "governed_runtime_invocation_phrase_verified: "
            f"{str(summary.governed_runtime_invocation_phrase_verified).lower()}",
            "governed_runtime_invocation_blocking_count: "
            f"{summary.governed_runtime_invocation_blocking_count}",
            "governed_runtime_invocation_warning_count: "
            f"{summary.governed_runtime_invocation_warning_count}",
            "governed_runtime_invocation_recommended_action: "
            f"{summary.governed_runtime_invocation_recommended_action or _NONE_LABEL}",
            "execution_authorization_state: "
            f"{summary.execution_authorization_state or _NONE_LABEL}",
            "execution_authorization_ready: "
            f"{str(summary.execution_authorization_ready).lower()}",
            "execution_authorization_present: "
            f"{str(summary.execution_authorization_present).lower()}",
            "execution_authorization_expired: "
            f"{str(summary.execution_authorization_expired).lower()}",
            "execution_authorization_id: "
            f"{summary.execution_authorization_id or _NONE_LABEL}",
            "execution_authorization_expires_at: "
            f"{summary.execution_authorization_expires_at or _NONE_LABEL}",
            "execution_authorization_phrase_verified: "
            f"{str(summary.execution_authorization_phrase_verified).lower()}",
            "execution_authorization_blocking_count: "
            f"{summary.execution_authorization_blocking_count}",
            "execution_authorization_warning_count: "
            f"{summary.execution_authorization_warning_count}",
            "execution_authorization_recommended_action: "
            f"{summary.execution_authorization_recommended_action or _NONE_LABEL}",
            "governed_runtime_start_state: "
            f"{summary.governed_runtime_start_state or _NONE_LABEL}",
            "governed_runtime_start_ready: "
            f"{str(summary.governed_runtime_start_ready).lower()}",
            "governed_runtime_start_present: "
            f"{str(summary.governed_runtime_start_present).lower()}",
            "governed_runtime_start_expired: "
            f"{str(summary.governed_runtime_start_expired).lower()}",
            "governed_runtime_start_id: "
            f"{summary.governed_runtime_start_id or _NONE_LABEL}",
            "governed_runtime_start_expires_at: "
            f"{summary.governed_runtime_start_expires_at or _NONE_LABEL}",
            "governed_runtime_start_started: "
            f"{str(summary.governed_runtime_start_started).lower()}",
            "governed_runtime_start_blocking_count: "
            f"{summary.governed_runtime_start_blocking_count}",
            "governed_runtime_start_warning_count: "
            f"{summary.governed_runtime_start_warning_count}",
            "governed_runtime_start_recommended_action: "
            f"{summary.governed_runtime_start_recommended_action or _NONE_LABEL}",
        ]
    )
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_gateway_correlation_diff(diff: CooDispatchGatewayCorrelationDiff) -> str:
    """Format safe correlation diff output."""
    changed_fields = (
        ", ".join(diff.changed_fields) if diff.changed_fields else _NONE_LABEL
    )
    lines = [
        "Gateway Correlation Diff",
        "",
        "[Requests]",
        f"left_gateway_request_id: {diff.left_gateway_request_id}",
        f"right_gateway_request_id: {diff.right_gateway_request_id}",
        f"same_ticket: {str(diff.same_ticket).lower()}",
        f"same_session: {str(diff.same_session).lower()}",
        "",
        "[Changes]",
        f"changed_fields_count: {diff.changed_fields_count}",
        f"changed_fields: {changed_fields}",
        "",
        "[Transitions]",
        f"health_transition: {diff.health_transition}",
        f"consume_transition: {diff.consume_transition}",
        f"recovery_transition: {diff.recovery_transition}",
        f"correlation_transition: {diff.correlation_transition}",
        f"regression_detected: {str(diff.regression_detected).lower()}",
        f"recommended_action: {diff.recommended_action}",
    ]
    from agent.coo.dispatch_operator_guidance import append_guidance_output_lines

    append_guidance_output_lines(lines, diff.recommended_action)
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output
