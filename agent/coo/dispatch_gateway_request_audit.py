"""Gateway request audit correlation — Phase 13M.

Read-only correlation summary linking gateway request records to pilot
history, execution evidence, consume state, and repair audit.

No writes, CLI wiring, Discord/Gateway integration, subprocess, or
Repository2 access.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.coo.dispatch_cli_gateway_pilot import EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK
from agent.coo.dispatch_consume_transaction import assess_consume_status
from agent.coo.dispatch_gateway_request_store import (
    CooDispatchGatewayRequestRecord,
    DispatchGatewayRequestStoreError,
    read_gateway_request,
)
from agent.coo.dispatch_pilot_history import (
    CooDispatchPilotHistoryRecord,
    list_pilot_history_records,
)

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class CooDispatchGatewayRequestAuditSummary:
    """Safe read-only gateway request audit correlation summary."""

    gateway_request_id: str
    request_status: str
    dry_run: bool
    gateway_state: str
    session_id: str
    ticket_id: str
    confirmation_id: str
    pilot_attempt_id: str
    pilot_status: str
    pilot_history_present: bool
    execution_attempt_id: str
    dispatch_run_id: str
    execution_status: str
    evidence_present: bool
    audit_present: bool
    consume_state: str
    recovery_required: bool
    consumed: bool
    repair_attempt_id: str
    repair_lock_held: bool
    repair_audit_present: bool
    correlation_valid: bool
    chain_complete: bool
    failure_reason_code: str
    production_execution_allowed: bool
    production_root_hard_deny: bool
    gateway_execution_scope: str


def _find_pilot_history_for_request(
    record: CooDispatchGatewayRequestRecord,
    *,
    history_dir: Path | None = None,
) -> CooDispatchPilotHistoryRecord | None:
    records = list_pilot_history_records(
        history_dir=history_dir,
        ticket_id=record.ticket_id or None,
    )
    for history in records:
        if record.gateway_request_id and history.gateway_request_id == record.gateway_request_id:
            return history
        if record.pilot_attempt_id and history.pilot_attempt_id == record.pilot_attempt_id:
            return history
        if (
            record.execution_attempt_id
            and history.execution_attempt_id == record.execution_attempt_id
        ):
            return history
    return None


def _resolve_repair_audit(
    *,
    ticket_id: str,
    confirmation_id: str,
    execution_attempt_id: str,
) -> tuple[str, bool]:
    from agent.coo.dispatch_cli_consume_repair_audit import (
        list_consume_repair_audit_summaries,
    )

    if not ticket_id:
        return _NONE_LABEL, False
    summaries = list_consume_repair_audit_summaries(ticket_id=ticket_id)
    if not summaries:
        return _NONE_LABEL, False
    if execution_attempt_id:
        for summary in summaries:
            if summary.execution_attempt_id == execution_attempt_id:
                return summary.repair_attempt_id, True
    if confirmation_id:
        for summary in summaries:
            if summary.confirmation_id == confirmation_id:
                return summary.repair_attempt_id, True
    return summaries[0].repair_attempt_id, True


def _resolve_evidence_presence(
    execution_attempt_id: str,
    *,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> tuple[str, bool, bool, str]:
    if not execution_attempt_id.strip():
        return _NONE_LABEL, False, False, _NONE_LABEL
    from agent.coo.dispatch_cli_evidence import summarize_dispatch_evidence_attempt

    try:
        evidence = summarize_dispatch_evidence_attempt(
            execution_attempt_id,
            evidence_dir=evidence_dir,
            audit_dir=audit_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
    except (KeyError, ValueError):
        return _NONE_LABEL, False, False, _NONE_LABEL
    return (
        evidence.status,
        evidence.evidence_files_present,
        evidence.audit_present,
        evidence.dispatch_run_id or _NONE_LABEL,
    )


def _correlation_valid(
    *,
    record: CooDispatchGatewayRequestRecord,
    history: CooDispatchPilotHistoryRecord | None,
    dispatch_run_id: str,
) -> bool:
    if history is None:
        return True
    if record.pilot_attempt_id and history.pilot_attempt_id != record.pilot_attempt_id:
        return False
    if (
        record.execution_attempt_id
        and history.execution_attempt_id
        and history.execution_attempt_id != record.execution_attempt_id
    ):
        return False
    if record.dispatch_run_id and history.dispatch_run_id != record.dispatch_run_id:
        return False
    if (
        dispatch_run_id != _NONE_LABEL
        and record.dispatch_run_id
        and record.dispatch_run_id != dispatch_run_id
    ):
        return False
    if (
        record.gateway_request_id
        and history.gateway_request_id
        and history.gateway_request_id != record.gateway_request_id
    ):
        return False
    return True


def _chain_complete(
    *,
    record: CooDispatchGatewayRequestRecord,
    history: CooDispatchPilotHistoryRecord | None,
    evidence_present: bool,
    audit_present: bool,
) -> bool:
    if not record.gateway_request_id:
        return False
    if not record.pilot_attempt_id and not (history and history.pilot_attempt_id):
        return False
    if not record.execution_attempt_id:
        return False
    if not record.dispatch_run_id:
        return False
    if history is None:
        return False
    return evidence_present and audit_present and history.consumed


def summarize_gateway_request_audit(
    gateway_request_id: str,
    *,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> CooDispatchGatewayRequestAuditSummary:
    """Build read-only audit correlation for one gateway request id."""
    try:
        record = read_gateway_request(gateway_request_id, request_dir=request_dir)
    except DispatchGatewayRequestStoreError as exc:
        raise ValueError(str(exc)) from exc
    if record is None:
        raise KeyError(f"Gateway request not found: {gateway_request_id}")

    history = _find_pilot_history_for_request(record, history_dir=history_dir)
    execution_status, evidence_present, audit_present, evidence_run_id = (
        _resolve_evidence_presence(
            record.execution_attempt_id,
            evidence_dir=evidence_dir,
            audit_dir=audit_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
    )
    dispatch_run_id = record.dispatch_run_id or evidence_run_id or _NONE_LABEL

    consume_state = _NONE_LABEL
    recovery_required = False
    if record.ticket_id and record.confirmation_id:
        consume = assess_consume_status(
            ticket_id=record.ticket_id,
            confirmation_id=record.confirmation_id,
        )
        consume_state = consume.consume_state
        recovery_required = consume.recovery_required

    repair_lock_held = False
    if record.ticket_id and record.confirmation_id:
        from agent.coo.dispatch_cli_consume_repair_lock import (
            summarize_consume_repair_lock_status,
        )

        lock_status = summarize_consume_repair_lock_status(
            ticket_id=record.ticket_id,
            confirmation_id=record.confirmation_id,
        )
        repair_lock_held = lock_status.repair_in_progress

    repair_attempt_id, repair_audit_present = _resolve_repair_audit(
        ticket_id=record.ticket_id,
        confirmation_id=record.confirmation_id,
        execution_attempt_id=record.execution_attempt_id,
    )

    pilot_attempt_id = record.pilot_attempt_id or (
        history.pilot_attempt_id if history is not None else ""
    )
    pilot_status = history.status if history is not None else _NONE_LABEL
    consumed = bool(history.consumed) if history is not None else False

    correlation_valid = _correlation_valid(
        record=record,
        history=history,
        dispatch_run_id=dispatch_run_id,
    )
    chain_complete = _chain_complete(
        record=record,
        history=history,
        evidence_present=evidence_present,
        audit_present=audit_present,
    )

    return CooDispatchGatewayRequestAuditSummary(
        gateway_request_id=record.gateway_request_id,
        request_status=record.status,
        dry_run=record.dry_run,
        gateway_state=record.gateway_state,
        session_id=record.session_id,
        ticket_id=record.ticket_id,
        confirmation_id=record.confirmation_id,
        pilot_attempt_id=pilot_attempt_id or _NONE_LABEL,
        pilot_status=pilot_status,
        pilot_history_present=history is not None,
        execution_attempt_id=record.execution_attempt_id or _NONE_LABEL,
        dispatch_run_id=dispatch_run_id,
        execution_status=execution_status,
        evidence_present=evidence_present,
        audit_present=audit_present,
        consume_state=consume_state,
        recovery_required=recovery_required,
        consumed=consumed,
        repair_attempt_id=repair_attempt_id,
        repair_lock_held=repair_lock_held,
        repair_audit_present=repair_audit_present,
        correlation_valid=correlation_valid,
        chain_complete=chain_complete,
        failure_reason_code=record.failure_reason_code or _NONE_LABEL,
        production_execution_allowed=False,
        production_root_hard_deny=True,
        gateway_execution_scope=EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
    )


def format_gateway_request_audit_summary(
    summary: CooDispatchGatewayRequestAuditSummary,
) -> str:
    """Format safe gateway request audit fields without secrets or paths."""
    return "\n".join(
        (
            "Gateway Request Audit",
            "",
            "[Gateway Request]",
            f"gateway_request_id: {summary.gateway_request_id}",
            f"request_status: {summary.request_status}",
            f"dry_run: {str(summary.dry_run).lower()}",
            f"gateway_state: {summary.gateway_state}",
            f"session_id: {summary.session_id or _NONE_LABEL}",
            f"ticket_id: {summary.ticket_id or _NONE_LABEL}",
            f"confirmation_id: {summary.confirmation_id or _NONE_LABEL}",
            "",
            "[Pilot Attempt]",
            f"pilot_attempt_id: {summary.pilot_attempt_id}",
            f"pilot_status: {summary.pilot_status}",
            f"pilot_history_present: {str(summary.pilot_history_present).lower()}",
            "",
            "[Execution]",
            f"execution_attempt_id: {summary.execution_attempt_id}",
            f"dispatch_run_id: {summary.dispatch_run_id}",
            f"execution_status: {summary.execution_status}",
            f"evidence_present: {str(summary.evidence_present).lower()}",
            f"audit_present: {str(summary.audit_present).lower()}",
            "",
            "[Consume]",
            f"consume_state: {summary.consume_state}",
            f"recovery_required: {str(summary.recovery_required).lower()}",
            f"consumed: {str(summary.consumed).lower()}",
            "",
            "[Repair]",
            f"repair_attempt_id: {summary.repair_attempt_id}",
            f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
            f"repair_audit_present: {str(summary.repair_audit_present).lower()}",
            "",
            "[Correlation]",
            f"correlation_valid: {str(summary.correlation_valid).lower()}",
            f"chain_complete: {str(summary.chain_complete).lower()}",
            f"failure_reason_code: {summary.failure_reason_code}",
            "",
            "[Safety]",
            "production_execution_allowed: false",
            f"production_root_hard_deny: {str(summary.production_root_hard_deny).lower()}",
            f"gateway_execution_scope: {summary.gateway_execution_scope}",
        )
    )
