"""CLI pilot operations history read — Phase 13B."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.coo.dispatch_cli_evidence import _find_audit_for_attempt
from agent.coo.dispatch_cli_pilot import CooDispatchPilotReadinessSummary
from agent.coo.dispatch_cli_run import CooDispatchRunResult
from agent.coo.dispatch_execution_audit import default_audit_dir
from agent.coo.dispatch_pilot_history import (
    EXECUTION_SCOPE_ISOLATED_CLONE,
    EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
    FAILURE_REASON_CONSUME_FAILED,
    FAILURE_REASON_NONE,
    FAILURE_REASON_POLICY_BLOCKED,
    FAILURE_REASON_PREFLIGHT_FAILED,
    FAILURE_REASON_RUNNER_FAILED,
    FAILURE_REASON_TIMEOUT,
    FAILURE_REASON_UNKNOWN_FAILURE,
    PILOT_HISTORY_VERSION,
    PILOT_STATUS_DRY_RUN,
    PILOT_STATUS_FAILURE,
    PILOT_STATUS_SUCCESS,
    PILOT_STATUS_TIMEOUT,
    CooDispatchPilotHistoryRecord,
    default_pilot_history_dir,
    find_pilot_history_records_for_ticket,
    list_pilot_history_records,
    read_pilot_history_record,
)
from agent.coo.production_executor_factory import (
    _TIMEOUT_EXIT_CODE,
    default_evidence_dir,
)
from hermes_constants import get_hermes_home

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class CooDispatchPilotHistorySummary:
    """Safe read-only pilot history summary."""

    pilot_attempt_id: str
    execution_attempt_id: str
    ticket_id: str
    confirmation_id: str
    dispatch_run_id: str
    execution_scope: str
    status: str
    exit_code: int
    dry_run: bool
    started_at: str
    completed_at: str
    evidence_present: bool
    audit_present: bool
    consumed: bool
    failure_reason_code: str
    production_execution_allowed: bool
    production_root_hard_deny: bool
    gateway_enabled: bool


@dataclass(frozen=True)
class CooDispatchPilotHistoryListEntry:
    """Safe read-only pilot history list entry."""

    pilot_attempt_id: str
    ticket_id: str
    confirmation_id: str
    status: str
    dry_run: bool
    completed_at: str
    failure_reason_code: str


def _record_to_summary(record: CooDispatchPilotHistoryRecord) -> CooDispatchPilotHistorySummary:
    return CooDispatchPilotHistorySummary(
        pilot_attempt_id=record.pilot_attempt_id,
        execution_attempt_id=record.execution_attempt_id,
        ticket_id=record.ticket_id,
        confirmation_id=record.confirmation_id,
        dispatch_run_id=record.dispatch_run_id,
        execution_scope=record.execution_scope,
        status=record.status,
        exit_code=record.exit_code,
        dry_run=record.dry_run,
        started_at=record.started_at,
        completed_at=record.completed_at,
        evidence_present=record.evidence_present,
        audit_present=record.audit_present,
        consumed=record.consumed,
        failure_reason_code=record.failure_reason_code,
        production_execution_allowed=record.production_execution_allowed,
        production_root_hard_deny=record.production_root_hard_deny,
        gateway_enabled=record.gateway_enabled,
    )


def _record_to_list_entry(record: CooDispatchPilotHistoryRecord) -> CooDispatchPilotHistoryListEntry:
    return CooDispatchPilotHistoryListEntry(
        pilot_attempt_id=record.pilot_attempt_id,
        ticket_id=record.ticket_id,
        confirmation_id=record.confirmation_id,
        status=record.status,
        dry_run=record.dry_run,
        completed_at=record.completed_at,
        failure_reason_code=record.failure_reason_code,
    )


def _load_evidence_exit_code(execution_attempt_id: str) -> int | None:
    if not execution_attempt_id:
        return None
    evidence_dir = default_evidence_dir()
    meta_path = evidence_dir / f"{execution_attempt_id}.meta.json"
    if not meta_path.is_file():
        return None
    import json

    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if "exit_code" not in payload:
        return None
    return int(payload["exit_code"])


def _evidence_present(execution_attempt_id: str) -> bool:
    if not execution_attempt_id:
        return False
    evidence_dir = default_evidence_dir()
    meta_path = evidence_dir / f"{execution_attempt_id}.meta.json"
    return meta_path.is_file()


def _audit_present(execution_attempt_id: str) -> bool:
    if not execution_attempt_id:
        return False
    audit = _find_audit_for_attempt(execution_attempt_id, audit_dir=default_audit_dir())
    return audit is not None


def _dispatch_run_id_for_attempt(execution_attempt_id: str) -> str:
    if not execution_attempt_id:
        return ""
    audit = _find_audit_for_attempt(execution_attempt_id, audit_dir=default_audit_dir())
    return audit.dispatch_run_id if audit is not None else ""


def _classify_failure_reason(
    *,
    dry_run: bool,
    run_status: str,
    exit_code: int,
    consumed: bool,
    run_error: str,
) -> str:
    lowered = run_error.lower()
    if "consumed" in lowered or "replay" in lowered:
        return FAILURE_REASON_POLICY_BLOCKED
    if "preflight" in lowered or "pre-run" in lowered or "policy" in lowered:
        return FAILURE_REASON_PREFLIGHT_FAILED
    if dry_run and run_status == "preflight_failed":
        return FAILURE_REASON_PREFLIGHT_FAILED
    if exit_code == _TIMEOUT_EXIT_CODE:
        return FAILURE_REASON_TIMEOUT
    if run_status == "completed" and not consumed:
        return FAILURE_REASON_CONSUME_FAILED
    if run_status in {"failed", "preflight_failed"}:
        if exit_code == _TIMEOUT_EXIT_CODE:
            return FAILURE_REASON_TIMEOUT
        return FAILURE_REASON_RUNNER_FAILED
    if run_error:
        return FAILURE_REASON_UNKNOWN_FAILURE
    return FAILURE_REASON_NONE


def _pilot_status_from_run(
    *,
    dry_run: bool,
    run_status: str,
    exit_code: int,
) -> str:
    if dry_run:
        return PILOT_STATUS_DRY_RUN
    if exit_code == _TIMEOUT_EXIT_CODE:
        return PILOT_STATUS_TIMEOUT
    if run_status == "completed" and exit_code == 0:
        return PILOT_STATUS_SUCCESS
    return PILOT_STATUS_FAILURE


def build_pilot_history_record_from_dispatch(
    *,
    pilot_attempt_id: str,
    started_at: str,
    completed_at: str,
    ticket_id: str,
    confirmation_id: str,
    dispatch_request_id: str,
    dry_run: bool,
    run_result: CooDispatchRunResult | None,
    run_error: str,
    pilot_summary: CooDispatchPilotReadinessSummary,
) -> CooDispatchPilotHistoryRecord:
    """Build a safe pilot history record from a dispatch run outcome."""
    execution_attempt_id = run_result.execution_attempt_id if run_result else ""
    run_status = run_result.status if run_result else "failed"
    consumed = bool(run_result and run_result.consumed)
    exit_code = _load_evidence_exit_code(execution_attempt_id)
    if exit_code is None:
        if dry_run:
            exit_code = 0 if run_status == "preflight_passed" else 1
        elif run_status == "completed" and consumed:
            exit_code = 0
        else:
            exit_code = 1
    dispatch_run_id = _dispatch_run_id_for_attempt(execution_attempt_id) or (
        dispatch_request_id if run_result else ""
    )
    status = _pilot_status_from_run(
        dry_run=dry_run,
        run_status=run_status,
        exit_code=exit_code,
    )
    failure_reason_code = _classify_failure_reason(
        dry_run=dry_run,
        run_status=run_status,
        exit_code=exit_code,
        consumed=consumed,
        run_error=run_error,
    )
    if status == PILOT_STATUS_SUCCESS:
        failure_reason_code = FAILURE_REASON_NONE
    if status == PILOT_STATUS_DRY_RUN and run_status == "preflight_passed":
        failure_reason_code = FAILURE_REASON_NONE

    return CooDispatchPilotHistoryRecord(
        version=PILOT_HISTORY_VERSION,
        pilot_attempt_id=pilot_attempt_id,
        execution_attempt_id=execution_attempt_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        dispatch_run_id=dispatch_run_id,
        execution_scope=EXECUTION_SCOPE_ISOLATED_CLONE,
        status=status,
        exit_code=exit_code,
        dry_run=dry_run,
        started_at=started_at,
        completed_at=completed_at,
        evidence_present=_evidence_present(execution_attempt_id),
        audit_present=_audit_present(execution_attempt_id),
        consumed=consumed,
        failure_reason_code=failure_reason_code,
        production_execution_allowed=False,
        production_root_hard_deny=pilot_summary.production_root_hard_deny,
        gateway_enabled=pilot_summary.gateway_enabled,
    )


def build_pilot_history_record_from_policy_block(
    *,
    pilot_attempt_id: str,
    started_at: str,
    completed_at: str,
    ticket_id: str,
    confirmation_id: str,
    pilot_summary: CooDispatchPilotReadinessSummary,
) -> CooDispatchPilotHistoryRecord:
    """Build a policy-blocked pilot history record without dispatch execution."""
    return CooDispatchPilotHistoryRecord(
        version=PILOT_HISTORY_VERSION,
        pilot_attempt_id=pilot_attempt_id,
        execution_attempt_id="",
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        dispatch_run_id="",
        execution_scope=EXECUTION_SCOPE_ISOLATED_CLONE,
        status=PILOT_STATUS_FAILURE,
        exit_code=1,
        dry_run=False,
        started_at=started_at,
        completed_at=completed_at,
        evidence_present=False,
        audit_present=False,
        consumed=False,
        failure_reason_code=FAILURE_REASON_POLICY_BLOCKED,
        production_execution_allowed=False,
        production_root_hard_deny=pilot_summary.production_root_hard_deny,
        gateway_enabled=pilot_summary.gateway_enabled,
    )


def build_gateway_pilot_history_record_from_facade(
    *,
    pilot_attempt_id: str,
    session_id: str,
    gateway_request_id: str,
    started_at: str,
    completed_at: str,
    ticket_id: str,
    confirmation_id: str,
    dry_run: bool,
    facade_result: "CooDispatchGatewayDispatchResult",
    production_root_hard_deny: bool,
) -> CooDispatchPilotHistoryRecord:
    """Build a safe gateway pilot history record from facade dispatch outcome."""
    execution_attempt_id = facade_result.execution_attempt_id
    dispatch_run_id = facade_result.dispatch_run_id
    consumed = facade_result.consumed
    exit_code = _load_evidence_exit_code(execution_attempt_id)
    if exit_code is None:
        if dry_run:
            exit_code = 0 if facade_result.accepted else 1
        elif facade_result.accepted and consumed:
            exit_code = 0
        else:
            exit_code = 1
    run_status = "completed" if facade_result.accepted else "failed"
    if dry_run:
        run_status = "preflight_passed" if facade_result.accepted else "preflight_failed"
    status = _pilot_status_from_run(
        dry_run=dry_run,
        run_status=run_status,
        exit_code=exit_code,
    )
    failure_reason_code = facade_result.failure_reason_code
    if status == PILOT_STATUS_SUCCESS or (
        status == PILOT_STATUS_DRY_RUN and facade_result.accepted
    ):
        failure_reason_code = FAILURE_REASON_NONE

    return CooDispatchPilotHistoryRecord(
        version=PILOT_HISTORY_VERSION,
        pilot_attempt_id=pilot_attempt_id,
        execution_attempt_id=execution_attempt_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        dispatch_run_id=dispatch_run_id,
        execution_scope=EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
        status=status,
        exit_code=exit_code,
        dry_run=dry_run,
        started_at=started_at,
        completed_at=completed_at,
        evidence_present=_evidence_present(execution_attempt_id),
        audit_present=_audit_present(execution_attempt_id),
        consumed=consumed,
        failure_reason_code=failure_reason_code,
        production_execution_allowed=False,
        production_root_hard_deny=production_root_hard_deny,
        gateway_enabled=False,
        gateway_request_id=gateway_request_id,
        session_id=session_id,
    )


def summarize_pilot_history_record(
    pilot_attempt_id: str,
    *,
    history_dir: Path | None = None,
) -> CooDispatchPilotHistorySummary:
    """Load and summarize one pilot history record."""
    record = read_pilot_history_record(pilot_attempt_id, history_dir=history_dir)
    return _record_to_summary(record)


def list_pilot_history_summaries(
    *,
    history_dir: Path | None = None,
) -> tuple[CooDispatchPilotHistoryListEntry, ...]:
    """List pilot history entries newest-first."""
    records = list_pilot_history_records(history_dir=history_dir)
    return tuple(_record_to_list_entry(record) for record in records)


def find_pilot_history_summaries_for_ticket(
    ticket_id: str,
    *,
    history_dir: Path | None = None,
) -> tuple[CooDispatchPilotHistoryListEntry, ...]:
    """Find pilot history entries for one ticket, newest-first."""
    records = find_pilot_history_records_for_ticket(
        ticket_id,
        history_dir=history_dir,
    )
    return tuple(_record_to_list_entry(record) for record in records)


def format_pilot_history_summary(summary: CooDispatchPilotHistorySummary) -> str:
    """Format safe pilot history fields for CLI stdout."""
    lines = [
        "Pilot History",
        "",
        f"pilot_attempt_id: {summary.pilot_attempt_id}",
        f"execution_attempt_id: {summary.execution_attempt_id or _NONE_LABEL}",
        f"ticket_id: {summary.ticket_id}",
        f"confirmation_id: {summary.confirmation_id}",
        f"dispatch_run_id: {summary.dispatch_run_id or _NONE_LABEL}",
        f"execution_scope: {summary.execution_scope}",
        f"status: {summary.status}",
        f"exit_code: {summary.exit_code}",
        f"dry_run: {str(summary.dry_run).lower()}",
        f"started_at: {summary.started_at}",
        f"completed_at: {summary.completed_at}",
        f"evidence_present: {str(summary.evidence_present).lower()}",
        f"audit_present: {str(summary.audit_present).lower()}",
        f"consumed: {str(summary.consumed).lower()}",
        f"failure_reason_code: {summary.failure_reason_code}",
        (
            "production_execution_allowed: "
            f"{str(summary.production_execution_allowed).lower()}"
        ),
        (
            "production_root_hard_deny: "
            f"{str(summary.production_root_hard_deny).lower()}"
        ),
        f"gateway_enabled: {str(summary.gateway_enabled).lower()}",
    ]
    return "\n".join(lines)


def format_pilot_history_list(entries: tuple[CooDispatchPilotHistoryListEntry, ...]) -> str:
    """Format safe pilot history list output."""
    lines = [
        "Pilot History List",
        "",
        f"count: {len(entries)}",
    ]
    if not entries:
        lines.append("records: (none)")
        return "\n".join(lines)
    lines.append("records:")
    for entry in entries:
        lines.append(
            "  - "
            f"pilot_attempt_id={entry.pilot_attempt_id} "
            f"ticket_id={entry.ticket_id} "
            f"status={entry.status} "
            f"dry_run={str(entry.dry_run).lower()} "
            f"completed_at={entry.completed_at} "
            f"failure_reason_code={entry.failure_reason_code}"
        )
    return "\n".join(lines)


def format_pilot_history_find(entries: tuple[CooDispatchPilotHistoryListEntry, ...]) -> str:
    """Format safe pilot history find output."""
    lines = [
        "Pilot History Find",
        "",
        f"count: {len(entries)}",
    ]
    if not entries:
        lines.append("records: (none)")
        return "\n".join(lines)
    lines.append("records:")
    for entry in entries:
        lines.append(
            "  - "
            f"pilot_attempt_id={entry.pilot_attempt_id} "
            f"confirmation_id={entry.confirmation_id} "
            f"status={entry.status} "
            f"dry_run={str(entry.dry_run).lower()} "
            f"completed_at={entry.completed_at}"
        )
    return "\n".join(lines)


def validate_pilot_history_dir_read_path(history_dir: Path | None = None) -> Path:
    """Validate pilot history directory remains under Hermes home."""
    base_dir = history_dir or default_pilot_history_dir()
    hermes_root = get_hermes_home().resolve()
    resolved = base_dir.resolve()
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ValueError("Pilot history directory must remain under Hermes home.") from exc
    return resolved
