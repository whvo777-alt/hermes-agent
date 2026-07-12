"""Gateway operational status — Phase 13L.

Read-only cross-reference summary for Gateway/Pilot operational health.
No dispatch execution, file mutation, subprocess, or secret disclosure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_cli_gateway_pilot import (
    EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
    evaluate_gateway_pilot_readiness,
    validate_gateway_pilot_correlation,
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
    TREND_STATUS_STABLE,
    evaluate_pilot_trend,
)
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_RECOVERY_REQUIRED,
    assess_consume_status,
)
from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_DISABLED,
    GATEWAY_STATE_ENABLED,
    GATEWAY_STATE_STAGED,
    load_dispatch_gateway_enablement,
)
from agent.coo.dispatch_gateway_request_store import (
    CooDispatchGatewayRequestRecord,
    DispatchGatewayRequestStoreError,
    read_gateway_request,
)
from agent.coo.dispatch_pilot_history import (
    PILOT_STATUS_DRY_RUN,
    PILOT_STATUS_FAILURE,
    PILOT_STATUS_SUCCESS,
    PILOT_STATUS_TIMEOUT,
    CooDispatchPilotHistoryRecord,
    list_pilot_history_records,
)

HEALTH_STATUS_HEALTHY = "HEALTHY"
HEALTH_STATUS_DEGRADED = "DEGRADED"
HEALTH_STATUS_BLOCKED = "BLOCKED"
HEALTH_STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"

GATEWAY_READINESS_READY = "ready"
GATEWAY_READINESS_BLOCKED = "blocked"
GATEWAY_READINESS_NOT_CONFIGURED = "not_configured"

RECOMMENDED_ACTION_RUN_GATEWAY_PILOT_DRY_RUN = "run_gateway_pilot_dry_run"
RECOMMENDED_ACTION_RUN_GATEWAY_MOCK_PILOT = "run_gateway_mock_pilot"
RECOMMENDED_ACTION_COLLECT_MORE_PILOT_HISTORY = "collect_more_pilot_history"
RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE = "investigate_recent_failure"
RECOMMENDED_ACTION_RESOLVE_REGRESSION_FAILURE = "resolve_regression_failure"
RECOMMENDED_ACTION_RESOLVE_RECOVERY_ISSUE = "resolve_recovery_issue"
RECOMMENDED_ACTION_STAGE_GATEWAY = "stage_gateway"
RECOMMENDED_ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"

TIMELINE_APPROVAL_SESSION_READY = "approval_session_ready"
TIMELINE_DISPATCH_BUNDLE_PREPARED = "dispatch_bundle_prepared"
TIMELINE_CONFIRMATION_AVAILABLE = "confirmation_available"
TIMELINE_GATEWAY_REQUEST_PREPARED = "gateway_request_prepared"
TIMELINE_PILOT_STARTED = "pilot_started"
TIMELINE_AUDIT_WRITTEN = "audit_written"
TIMELINE_EVIDENCE_WRITTEN = "evidence_written"
TIMELINE_CONSUME_COMMITTED = "consume_committed"
TIMELINE_PILOT_COMPLETED = "pilot_completed"
TIMELINE_PILOT_FAILED = "pilot_failed"
TIMELINE_RECOVERY_REQUIRED = "recovery_required"

_FORBIDDEN_OUTPUT_KEYS = frozenset(
    {
        "pipeline_root",
        "unlock_token_id",
        "unlock_token",
        "phrase",
        "confirmation_phrase",
        "argv",
        "cwd",
        "env",
        "stdout",
        "stderr",
        "secret",
        "token",
        "snapshot",
        "channel_id",
        "requester_metadata",
        "operator_reason",
    }
)

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class CooDispatchGatewayTimelineEvent:
    """Safe timeline event with fixed code and ISO timestamp only."""

    event_type: str
    timestamp: str


@dataclass(frozen=True)
class CooDispatchGatewayRequestSummary:
    """Safe read-only gateway request summary."""

    gateway_request_id: str
    request_status: str
    dry_run: bool
    execution_attempt_id: str
    dispatch_run_id: str
    failure_reason_code: str
    consumed: bool


@dataclass(frozen=True)
class CooDispatchGatewayOperationalSummary:
    """Safe read-only Gateway/Pilot operational summary."""

    session_id: str
    ticket_id: str
    gateway_state: str
    facade_connected: bool
    mock_execution_supported: bool
    gateway_readiness: str
    signoff_ready: bool
    cutover_ready: bool
    latest_status: str
    regression_status: str
    trend_status: str
    consecutive_failures: int
    latest_pilot_attempt_id: str
    gateway_request_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    consumed: bool
    production_execution_allowed: bool
    production_root_hard_deny: bool
    gateway_execution_scope: str
    health_status: str
    recommended_action: str
    consume_state: str
    recovery_required: bool
    repair_lock_held: bool
    correlation_ready: bool
    failure_reason_code: str
    timeline: tuple[CooDispatchGatewayTimelineEvent, ...]
    history_corrupted: bool = False
    request_corrupted: bool = False


def _safe_timestamp(value: str) -> str:
    text = str(value or "").strip()
    return text if text else ""


def _list_gateway_request_records(
    *,
    request_dir: Path | None = None,
    ticket_id: str | None = None,
    session_id: str | None = None,
) -> tuple[CooDispatchGatewayRequestRecord, ...]:
    from agent.coo.dispatch_gateway_request_store import default_gateway_request_dir

    base_dir = request_dir or default_gateway_request_dir()
    if not base_dir.is_dir():
        return ()
    normalized_ticket = (ticket_id or "").strip() or None
    normalized_session = (session_id or "").strip() or None
    records: list[CooDispatchGatewayRequestRecord] = []
    for path in sorted(base_dir.glob("*.json")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            payload_id = path.stem
            record = read_gateway_request(payload_id, request_dir=base_dir)
        except DispatchGatewayRequestStoreError:
            raise
        except ValueError:
            raise DispatchGatewayRequestStoreError(
                f"Gateway request record is corrupted: {path.stem}"
            ) from None
        if record is None:
            continue
        if normalized_ticket is not None and record.ticket_id != normalized_ticket:
            continue
        if normalized_session is not None and record.session_id != normalized_session:
            continue
        records.append(record)
    records.sort(key=lambda item: item.gateway_request_id, reverse=True)
    return tuple(records)


def find_latest_gateway_request(
    *,
    gateway_request_id: str = "",
    ticket_id: str = "",
    session_id: str = "",
    request_dir: Path | None = None,
) -> CooDispatchGatewayRequestRecord | None:
    """Find the latest gateway request by explicit id or session/ticket context."""
    normalized_id = (gateway_request_id or "").strip()
    if normalized_id:
        try:
            return read_gateway_request(normalized_id, request_dir=request_dir)
        except DispatchGatewayRequestStoreError:
            return None
    records = _list_gateway_request_records(
        request_dir=request_dir,
        ticket_id=ticket_id or None,
        session_id=session_id or None,
    )
    if not records:
        return None
    return records[0]


def summarize_gateway_request(
    record: CooDispatchGatewayRequestRecord | None,
    *,
    latest_history: CooDispatchPilotHistoryRecord | None = None,
) -> CooDispatchGatewayRequestSummary:
    """Build safe gateway request summary without raw file content."""
    if record is None:
        consumed = bool(latest_history and latest_history.consumed)
        return CooDispatchGatewayRequestSummary(
            gateway_request_id=_NONE_LABEL,
            request_status=_NONE_LABEL,
            dry_run=False,
            execution_attempt_id=(
                latest_history.execution_attempt_id if latest_history else ""
            ),
            dispatch_run_id=latest_history.dispatch_run_id if latest_history else "",
            failure_reason_code=_NONE_LABEL,
            consumed=consumed,
        )
    consumed = bool(
        latest_history
        and latest_history.gateway_request_id == record.gateway_request_id
        and latest_history.consumed
    )
    return CooDispatchGatewayRequestSummary(
        gateway_request_id=record.gateway_request_id,
        request_status=record.status,
        dry_run=record.dry_run,
        execution_attempt_id=record.execution_attempt_id,
        dispatch_run_id=record.dispatch_run_id,
        failure_reason_code=record.failure_reason_code or _NONE_LABEL,
        consumed=consumed,
    )


def _determine_gateway_readiness(
    *,
    gateway_state: str,
    pilot_ready: bool,
    context_present: bool,
) -> str:
    if gateway_state == GATEWAY_STATE_DISABLED or not context_present:
        return GATEWAY_READINESS_NOT_CONFIGURED
    if pilot_ready:
        return GATEWAY_READINESS_READY
    return GATEWAY_READINESS_BLOCKED


def _determine_health_status(
    *,
    gateway_state: str,
    pilot_ready: bool,
    regression_status: str,
    trend_status: str,
    consecutive_failures: int,
    latest_status: str,
    total_attempts: int,
    dry_run_count: int,
    consume_state: str,
    recovery_required: bool,
    repair_lock_held: bool,
    signoff_ready: bool,
    cutover_ready: bool,
    production_policy_valid: bool,
    history_corrupted: bool,
    request_corrupted: bool,
    context_present: bool,
) -> str:
    if history_corrupted or request_corrupted:
        return HEALTH_STATUS_BLOCKED
    if not production_policy_valid:
        return HEALTH_STATUS_BLOCKED
    if gateway_state == GATEWAY_STATE_DISABLED or not context_present:
        return HEALTH_STATUS_NOT_CONFIGURED
    if gateway_state == GATEWAY_STATE_ENABLED:
        return HEALTH_STATUS_BLOCKED
    if recovery_required or repair_lock_held:
        return HEALTH_STATUS_BLOCKED
    if consume_state in {
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_LEGACY_PARTIAL,
        CONSUME_STATE_RECOVERY_REQUIRED,
    }:
        return HEALTH_STATUS_BLOCKED
    if not signoff_ready or not cutover_ready:
        return HEALTH_STATUS_BLOCKED
    if regression_status == REGRESSION_STATUS_FAIL and consecutive_failures >= 2:
        return HEALTH_STATUS_BLOCKED
    if gateway_state != GATEWAY_STATE_STAGED or not pilot_ready:
        if gateway_state == GATEWAY_STATE_ENABLED:
            return HEALTH_STATUS_BLOCKED
        if gateway_state == GATEWAY_STATE_DISABLED or not context_present:
            return HEALTH_STATUS_NOT_CONFIGURED
        return HEALTH_STATUS_BLOCKED

    if regression_status == REGRESSION_STATUS_FAIL and consecutive_failures == 1:
        return HEALTH_STATUS_DEGRADED

    if total_attempts == 0:
        return HEALTH_STATUS_DEGRADED
    if dry_run_count == total_attempts:
        return HEALTH_STATUS_DEGRADED
    if trend_status == TREND_STATUS_INSUFFICIENT_DATA:
        return HEALTH_STATUS_DEGRADED
    if consecutive_failures == 1:
        return HEALTH_STATUS_DEGRADED
    if trend_status == TREND_STATUS_DEGRADED and consecutive_failures > 0:
        return HEALTH_STATUS_DEGRADED
    if latest_status in {PILOT_STATUS_FAILURE, PILOT_STATUS_TIMEOUT}:
        return HEALTH_STATUS_DEGRADED
    if regression_status == REGRESSION_STATUS_WARN:
        return HEALTH_STATUS_DEGRADED

    if (
        regression_status in {REGRESSION_STATUS_PASS, REGRESSION_STATUS_WARN}
        and latest_status in {PILOT_STATUS_SUCCESS, PILOT_STATUS_DRY_RUN, _NONE_LABEL}
    ):
        if latest_status == PILOT_STATUS_SUCCESS or dry_run_count > 0:
            return HEALTH_STATUS_HEALTHY

    if trend_status == TREND_STATUS_STABLE and consecutive_failures == 0:
        return HEALTH_STATUS_HEALTHY

    return HEALTH_STATUS_DEGRADED


def _determine_recommended_action(
    *,
    health_status: str,
    gateway_state: str,
    regression_status: str,
    total_attempts: int,
    dry_run_count: int,
    consecutive_failures: int,
    recovery_required: bool,
    repair_lock_held: bool,
    production_policy_valid: bool,
    pilot_ready: bool,
) -> str:
    if not production_policy_valid:
        return RECOMMENDED_ACTION_MAINTAIN_PRODUCTION_BLOCK
    if gateway_state == GATEWAY_STATE_DISABLED:
        return RECOMMENDED_ACTION_STAGE_GATEWAY
    if recovery_required or repair_lock_held:
        return RECOMMENDED_ACTION_RESOLVE_RECOVERY_ISSUE
    if regression_status == REGRESSION_STATUS_FAIL:
        if consecutive_failures == 1:
            return RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE
        return RECOMMENDED_ACTION_RESOLVE_REGRESSION_FAILURE
    if health_status == HEALTH_STATUS_BLOCKED and gateway_state == GATEWAY_STATE_ENABLED:
        return RECOMMENDED_ACTION_MAINTAIN_PRODUCTION_BLOCK
    if total_attempts == 0:
        return RECOMMENDED_ACTION_COLLECT_MORE_PILOT_HISTORY
    if consecutive_failures > 0:
        return RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE
    if dry_run_count == total_attempts:
        return RECOMMENDED_ACTION_RUN_GATEWAY_PILOT_DRY_RUN
    if pilot_ready and regression_status in {
        REGRESSION_STATUS_PASS,
        REGRESSION_STATUS_WARN,
    }:
        return RECOMMENDED_ACTION_RUN_GATEWAY_MOCK_PILOT
    if health_status == HEALTH_STATUS_DEGRADED:
        return RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE
    return RECOMMENDED_ACTION_RUN_GATEWAY_PILOT_DRY_RUN


def build_gateway_operational_timeline(
    *,
    session_id: str,
    ticket_id: str,
    confirmation_id: str = "",
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    history_dir: Path | None = None,
    request_dir: Path | None = None,
    session_store=None,
    consume_state: str = "",
    recovery_required: bool = False,
) -> tuple[CooDispatchGatewayTimelineEvent, ...]:
    """Build safe timeline from known timestamps only."""
    events: list[CooDispatchGatewayTimelineEvent] = []

    if session_id.strip():
        from agent.coo.gateway_approval import get_gateway_approval_session

        session = get_gateway_approval_session(session_id, store=session_store)
        if session is not None:
            created_at = _safe_timestamp(str(session.get("created_at", "")))
            if created_at:
                events.append(
                    CooDispatchGatewayTimelineEvent(
                        event_type=TIMELINE_APPROVAL_SESSION_READY,
                        timestamp=created_at,
                    )
                )

    if ticket_id.strip():
        from agent.coo.dispatch_bundle_store import read_bundle

        try:
            bundle = read_bundle(ticket_id, bundle_dir=bundle_dir)
        except (KeyError, ValueError, OSError):
            bundle = None
        if bundle is not None:
            ts = _safe_timestamp(bundle.created_at)
            if ts:
                events.append(
                    CooDispatchGatewayTimelineEvent(
                        event_type=TIMELINE_DISPATCH_BUNDLE_PREPARED,
                        timestamp=ts,
                    )
                )

    if confirmation_id.strip():
        from agent.coo.production_executor_confirmation import read_confirmation

        try:
            confirmation = read_confirmation(
                confirmation_id,
                confirmation_dir=confirmation_dir,
            )
        except (KeyError, ValueError, OSError):
            confirmation = None
        if confirmation is not None:
            ts = _safe_timestamp(confirmation.created_at)
            if ts:
                events.append(
                    CooDispatchGatewayTimelineEvent(
                        event_type=TIMELINE_CONFIRMATION_AVAILABLE,
                        timestamp=ts,
                    )
                )

    try:
        request_records = _list_gateway_request_records(
            request_dir=request_dir,
            ticket_id=ticket_id or None,
            session_id=session_id or None,
        )
    except DispatchGatewayRequestStoreError:
        request_records = ()

    if request_dir is not None and request_records:
        import json

        record = request_records[0]
        path = request_dir / f"{record.gateway_request_id}.json"
        if path.is_file() and not path.is_symlink():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                ts = _safe_timestamp(str(payload.get("updated_at", "")))
                if ts:
                    events.append(
                        CooDispatchGatewayTimelineEvent(
                            event_type=TIMELINE_GATEWAY_REQUEST_PREPARED,
                            timestamp=ts,
                        )
                    )
            except json.JSONDecodeError:
                pass

    try:
        history_records = list_pilot_history_records(
            history_dir=history_dir,
            ticket_id=ticket_id or None,
        )
    except ValueError:
        history_records = ()

    for record in history_records[:3]:
        started = _safe_timestamp(record.started_at)
        if started:
            events.append(
                CooDispatchGatewayTimelineEvent(
                    event_type=TIMELINE_PILOT_STARTED,
                    timestamp=started,
                )
            )
        if record.audit_present:
            completed = _safe_timestamp(record.completed_at)
            if completed:
                events.append(
                    CooDispatchGatewayTimelineEvent(
                        event_type=TIMELINE_AUDIT_WRITTEN,
                        timestamp=completed,
                    )
                )
        if record.evidence_present:
            completed = _safe_timestamp(record.completed_at)
            if completed:
                events.append(
                    CooDispatchGatewayTimelineEvent(
                        event_type=TIMELINE_EVIDENCE_WRITTEN,
                        timestamp=completed,
                    )
                )
        if record.consumed:
            completed = _safe_timestamp(record.completed_at)
            if completed:
                events.append(
                    CooDispatchGatewayTimelineEvent(
                        event_type=TIMELINE_CONSUME_COMMITTED,
                        timestamp=completed,
                    )
                )
        completed = _safe_timestamp(record.completed_at)
        if completed:
            if record.status == PILOT_STATUS_SUCCESS:
                events.append(
                    CooDispatchGatewayTimelineEvent(
                        event_type=TIMELINE_PILOT_COMPLETED,
                        timestamp=completed,
                    )
                )
            elif record.status in {PILOT_STATUS_FAILURE, PILOT_STATUS_TIMEOUT}:
                events.append(
                    CooDispatchGatewayTimelineEvent(
                        event_type=TIMELINE_PILOT_FAILED,
                        timestamp=completed,
                    )
                )

    if recovery_required or consume_state == CONSUME_STATE_RECOVERY_REQUIRED:
        recovery_ts = ""
        if history_records:
            recovery_ts = _safe_timestamp(history_records[0].completed_at)
        if recovery_ts:
            events.append(
                CooDispatchGatewayTimelineEvent(
                    event_type=TIMELINE_RECOVERY_REQUIRED,
                    timestamp=recovery_ts,
                )
            )

    deduped: list[CooDispatchGatewayTimelineEvent] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        if event.timestamp == _NONE_LABEL and event.event_type != TIMELINE_RECOVERY_REQUIRED:
            continue
        key = (event.event_type, event.timestamp)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return tuple(deduped)


def build_gateway_operational_summary(
    *,
    session_id: str = "",
    ticket_id: str = "",
    confirmation_id: str = "",
    unlock_token_id: str = "",
    requester_id: str = "",
    pipeline_root: str = "",
    gateway_request_id: str = "",
    merged_config: Mapping[str, Any] | None = None,
    session_store=None,
    ticket_store=None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
) -> CooDispatchGatewayOperationalSummary:
    """Build read-only Gateway/Pilot operational summary."""
    if merged_config is None:
        merged_config = {}

    history_corrupted = False
    request_corrupted = False

    enablement = load_dispatch_gateway_enablement(merged_config=merged_config)
    gateway_state = enablement.gateway_state

    context_present = bool(
        session_id.strip() and ticket_id.strip() and requester_id.strip()
    )

    readiness = None
    correlation_ready = False
    correlation_failure = _NONE_LABEL
    if context_present and confirmation_id and pipeline_root:
        readiness = evaluate_gateway_pilot_readiness(
            session_id=session_id,
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            unlock_token_id=unlock_token_id,
            requester_id=requester_id,
            pipeline_root=pipeline_root,
            merged_config=merged_config,
            session_store=session_store,
            ticket_store=ticket_store,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            dry_run=True,
        )
        correlation_failure = validate_gateway_pilot_correlation(
            session_id=session_id,
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            unlock_token_id=unlock_token_id,
            requester_id=requester_id,
            pipeline_root=pipeline_root,
            session_store=session_store,
            ticket_store=ticket_store,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
        correlation_ready = correlation_failure is None
    elif context_present:
        correlation_failure = validate_gateway_pilot_correlation(
            session_id=session_id,
            ticket_id=ticket_id,
            confirmation_id=confirmation_id or "missing",
            unlock_token_id=unlock_token_id,
            requester_id=requester_id,
            pipeline_root=pipeline_root or "missing",
            session_store=session_store,
            ticket_store=ticket_store,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
        correlation_ready = correlation_failure is None

    pilot_ready = readiness.pilot_ready if readiness is not None else False
    facade_connected = readiness.facade_connected if readiness else False
    mock_execution_supported = (
        readiness.isolated_execution_supported if readiness else False
    )
    signoff_ready = readiness.signoff_ready if readiness else False
    cutover_ready = readiness.cutover_ready if readiness else False
    production_root_hard_deny = (
        readiness.production_root_hard_deny if readiness else True
    )

    from agent.coo.dispatch_cli_pilot_regression import CooDispatchPilotRegressionSummary
    from agent.coo.dispatch_cli_pilot_runbook import CooDispatchPilotTrendSummary

    try:
        regression = evaluate_pilot_regression(
            ticket_id=ticket_id or None,
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
            ticket_id=ticket_id or None,
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

    consume_state = _NONE_LABEL
    recovery_required = False
    if ticket_id and confirmation_id:
        consume = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        consume_state = consume.consume_state
        recovery_required = consume.recovery_required

    repair_lock_held = False
    if ticket_id and confirmation_id:
        from agent.coo.dispatch_cli_consume_repair_lock import (
            summarize_consume_repair_lock_status,
        )

        lock_status = summarize_consume_repair_lock_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        repair_lock_held = lock_status.repair_in_progress

    latest_request = None
    try:
        latest_request = find_latest_gateway_request(
            gateway_request_id=gateway_request_id,
            ticket_id=ticket_id,
            session_id=session_id,
            request_dir=request_dir,
        )
    except DispatchGatewayRequestStoreError:
        request_corrupted = True

    history_records: tuple[CooDispatchPilotHistoryRecord, ...] = ()
    try:
        history_records = list_pilot_history_records(
            history_dir=history_dir,
            ticket_id=ticket_id or None,
        )
    except ValueError:
        history_corrupted = True

    latest_history = history_records[0] if history_records else None
    request_summary = summarize_gateway_request(
        latest_request,
        latest_history=latest_history,
    )

    production_policy_valid = (
        production_root_hard_deny
        and not enablement.production_execution_allowed
        and regression.production_policy_violations == 0
    )

    gateway_readiness = _determine_gateway_readiness(
        gateway_state=gateway_state,
        pilot_ready=pilot_ready,
        context_present=context_present,
    )

    health_status = _determine_health_status(
        gateway_state=gateway_state,
        pilot_ready=pilot_ready,
        regression_status=regression.regression_status,
        trend_status=trend.trend_status,
        consecutive_failures=regression.consecutive_failures,
        latest_status=regression.latest_status,
        total_attempts=regression.total_attempts,
        dry_run_count=regression.dry_run_count,
        consume_state=consume_state,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        signoff_ready=signoff_ready,
        cutover_ready=cutover_ready,
        production_policy_valid=production_policy_valid,
        history_corrupted=history_corrupted,
        request_corrupted=request_corrupted,
        context_present=context_present,
    )

    recommended_action = _determine_recommended_action(
        health_status=health_status,
        gateway_state=gateway_state,
        regression_status=regression.regression_status,
        total_attempts=regression.total_attempts,
        dry_run_count=regression.dry_run_count,
        consecutive_failures=regression.consecutive_failures,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        production_policy_valid=production_policy_valid,
        pilot_ready=pilot_ready,
    )

    failure_reason_code = _NONE_LABEL
    if correlation_failure is not None:
        failure_reason_code = str(correlation_failure)
    elif readiness is not None and readiness.failed_checks != _NONE_LABEL:
        failure_reason_code = readiness.failed_checks
    if history_corrupted:
        failure_reason_code = "corrupted_history"
    if request_corrupted:
        failure_reason_code = "corrupted_request"

    timeline = build_gateway_operational_timeline(
        session_id=session_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        history_dir=history_dir,
        request_dir=request_dir,
        session_store=session_store,
        consume_state=consume_state,
        recovery_required=recovery_required,
    )

    return CooDispatchGatewayOperationalSummary(
        session_id=session_id,
        ticket_id=ticket_id,
        gateway_state=gateway_state,
        facade_connected=facade_connected,
        mock_execution_supported=mock_execution_supported,
        gateway_readiness=gateway_readiness,
        signoff_ready=signoff_ready,
        cutover_ready=cutover_ready,
        latest_status=regression.latest_status,
        regression_status=regression.regression_status,
        trend_status=trend.trend_status,
        consecutive_failures=regression.consecutive_failures,
        latest_pilot_attempt_id=regression.latest_pilot_attempt_id,
        gateway_request_id=request_summary.gateway_request_id,
        execution_attempt_id=request_summary.execution_attempt_id,
        dispatch_run_id=request_summary.dispatch_run_id,
        consumed=request_summary.consumed,
        production_execution_allowed=False,
        production_root_hard_deny=production_root_hard_deny,
        gateway_execution_scope=EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
        health_status=health_status,
        recommended_action=recommended_action,
        consume_state=consume_state,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        correlation_ready=correlation_ready,
        failure_reason_code=failure_reason_code,
        timeline=timeline,
        history_corrupted=history_corrupted,
        request_corrupted=request_corrupted,
    )


def format_gateway_operational_summary(summary: CooDispatchGatewayOperationalSummary) -> str:
    """Format safe operational summary for CLI or logs."""
    lines = [
        "Gateway Operational Summary",
        "",
        "[Gateway]",
        f"state: {summary.gateway_state}",
        f"facade_connected: {str(summary.facade_connected).lower()}",
        f"mock_execution_supported: {str(summary.mock_execution_supported).lower()}",
        "",
        "[Readiness]",
        f"gateway_readiness: {summary.gateway_readiness}",
        f"signoff_ready: {str(summary.signoff_ready).lower()}",
        f"cutover_ready: {str(summary.cutover_ready).lower()}",
        "",
        "[Pilot]",
        f"latest_status: {summary.latest_status}",
        f"regression_status: {summary.regression_status}",
        f"trend_status: {summary.trend_status}",
        f"consecutive_failures: {summary.consecutive_failures}",
        f"latest_pilot_attempt_id: {summary.latest_pilot_attempt_id}",
        "",
        "[Execution]",
        f"gateway_request_id: {summary.gateway_request_id}",
        f"execution_attempt_id: {summary.execution_attempt_id or _NONE_LABEL}",
        f"dispatch_run_id: {summary.dispatch_run_id or _NONE_LABEL}",
        f"consumed: {str(summary.consumed).lower()}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        f"production_root_hard_deny: {str(summary.production_root_hard_deny).lower()}",
        f"gateway_execution_scope: {summary.gateway_execution_scope}",
        "",
        "[Operator]",
        f"health_status: {summary.health_status}",
        f"recommended_action: {summary.recommended_action}",
        f"consume_state: {summary.consume_state}",
        f"recovery_required: {str(summary.recovery_required).lower()}",
        f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
        f"failure_reason_code: {summary.failure_reason_code}",
    ]
    if summary.timeline:
        lines.extend(["", "[Timeline]"])
        for event in summary.timeline:
            lines.append(f"{event.event_type}: {event.timestamp}")
    return "\n".join(lines)


def assert_safe_operational_payload(payload: Mapping[str, Any]) -> None:
    """Fail-closed when unsafe fields appear in operational output."""
    for key in payload:
        normalized = str(key).lower()
        if key in _FORBIDDEN_OUTPUT_KEYS or normalized in _FORBIDDEN_OUTPUT_KEYS:
            raise ValueError(f"Unsafe operational output field: {key!r}")
        value = payload[key]
        if isinstance(value, Mapping):
            assert_safe_operational_payload(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, Mapping):
                    assert_safe_operational_payload(item)
