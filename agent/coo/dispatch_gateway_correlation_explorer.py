"""Gateway correlation explorer — Phase 13N.

Read-only reverse lookup from gateway/pilot/execution/run/ticket IDs to a
full Gateway correlation chain. No writes, subprocess, or Repository2 access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.coo.dispatch_cli_gateway_pilot import EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK
from agent.coo.dispatch_gateway_request_audit import (
    CooDispatchGatewayRequestAuditSummary,
    summarize_gateway_request_audit,
)
from agent.coo.dispatch_gateway_request_store import (
    CooDispatchGatewayRequestRecord,
    DispatchGatewayRequestStoreError,
    default_gateway_request_dir,
    normalize_gateway_request_id,
    read_gateway_request,
)
from agent.coo.dispatch_pilot_history import (
    CooDispatchPilotHistoryRecord,
    list_pilot_history_records,
    read_pilot_history_record,
)
from hermes_constants import get_hermes_home

DEFAULT_CORRELATION_SCAN_LIMIT = 500

QUERY_TYPE_GATEWAY_REQUEST = "gateway_request_id"
QUERY_TYPE_PILOT_ATTEMPT = "pilot_attempt_id"
QUERY_TYPE_EXECUTION_ATTEMPT = "execution_attempt_id"
QUERY_TYPE_DISPATCH_RUN = "dispatch_run_id"
QUERY_TYPE_TICKET = "ticket_id"

RECOMMENDED_ACTION_NO_ACTION_REQUIRED = "no_action_required"
RECOMMENDED_ACTION_INSPECT_EXECUTION_FAILURE = "inspect_execution_failure"
RECOMMENDED_ACTION_INSPECT_MISSING_EVIDENCE = "inspect_missing_evidence"
RECOMMENDED_ACTION_INSPECT_CONSUME_STATE = "inspect_consume_state"
RECOMMENDED_ACTION_RESOLVE_RECOVERY_REQUIRED = "resolve_recovery_required"
RECOMMENDED_ACTION_RESOLVE_CORRELATION_MISMATCH = "resolve_correlation_mismatch"
RECOMMENDED_ACTION_PROVIDE_MORE_SPECIFIC_ID = "provide_more_specific_id"
RECOMMENDED_ACTION_REQUEST_NOT_FOUND = "request_not_found"

_FAILURE_AMBIGUOUS_LOOKUP = "ambiguous_correlation_lookup"
_FAILURE_REQUEST_NOT_FOUND = "request_not_found"
_FAILURE_CORRELATION_MISMATCH = "correlation_mismatch"

_NONE_LABEL = "(none)"

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
    }
)


@dataclass(frozen=True)
class CooDispatchGatewayCorrelationChain:
    """Safe read-only Gateway correlation chain summary."""

    query_type: str
    query_id: str
    gateway_request_id: str
    session_id: str
    ticket_id: str
    confirmation_id: str
    pilot_attempt_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    request_status: str
    pilot_status: str
    execution_status: str
    audit_present: bool
    evidence_present: bool
    consume_state: str
    consumed: bool
    recovery_required: bool
    repair_attempt_id: str
    repair_audit_present: bool
    repair_lock_held: bool
    correlation_valid: bool
    chain_complete: bool
    ambiguity_detected: bool
    failure_reason_code: str
    production_execution_allowed: bool
    production_root_hard_deny: bool
    gateway_execution_scope: str
    recommended_action: str


@dataclass(frozen=True)
class GatewayCorrelationQuery:
    """One validated correlation explorer query."""

    query_type: str
    query_id: str


class GatewayCorrelationExplorerError(ValueError):
    """Raised when correlation exploration cannot proceed safely."""


def _normalize_opaque_id(value: str, *, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise GatewayCorrelationExplorerError(f"{field_name} is required")
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise GatewayCorrelationExplorerError(
            f"{field_name} must not contain path separators."
        )
    return normalized


def normalize_gateway_correlation_query(
    *,
    gateway_request_id: str = "",
    pilot_attempt_id: str = "",
    execution_attempt_id: str = "",
    dispatch_run_id: str = "",
    ticket_id: str = "",
) -> GatewayCorrelationQuery:
    """Validate exactly one correlation query id."""
    candidates: list[tuple[str, str]] = []
    if gateway_request_id.strip():
        try:
            normalized_id = normalize_gateway_request_id(gateway_request_id)
        except DispatchGatewayRequestStoreError as exc:
            raise GatewayCorrelationExplorerError(str(exc)) from exc
        candidates.append((QUERY_TYPE_GATEWAY_REQUEST, normalized_id))
    if pilot_attempt_id.strip():
        candidates.append(
            (
                QUERY_TYPE_PILOT_ATTEMPT,
                _normalize_opaque_id(pilot_attempt_id, field_name="pilot_attempt_id"),
            )
        )
    if execution_attempt_id.strip():
        candidates.append(
            (
                QUERY_TYPE_EXECUTION_ATTEMPT,
                _normalize_opaque_id(
                    execution_attempt_id,
                    field_name="execution_attempt_id",
                ),
            )
        )
    if dispatch_run_id.strip():
        candidates.append(
            (
                QUERY_TYPE_DISPATCH_RUN,
                _normalize_opaque_id(dispatch_run_id, field_name="dispatch_run_id"),
            )
        )
    if ticket_id.strip():
        candidates.append(
            (
                QUERY_TYPE_TICKET,
                _normalize_opaque_id(ticket_id, field_name="ticket_id"),
            )
        )
    if not candidates:
        raise GatewayCorrelationExplorerError(
            "Exactly one correlation query id is required."
        )
    if len(candidates) > 1:
        raise GatewayCorrelationExplorerError(
            "Only one correlation query id may be provided."
        )
    query_type, query_id = candidates[0]
    return GatewayCorrelationQuery(query_type=query_type, query_id=query_id)


def _assert_within_hermes_home(resolved: Path, *, label: str) -> None:
    hermes_root = get_hermes_home().resolve()
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise GatewayCorrelationExplorerError(
            f"Correlation explorer {label} must remain under Hermes home."
        ) from exc


def _read_gateway_request_entry(
    path: Path,
    *,
    request_dir: Path,
) -> tuple[CooDispatchGatewayRequestRecord, str]:
    if not path.is_file() or path.is_symlink():
        raise GatewayCorrelationExplorerError("Gateway request path is invalid.")
    stem = path.stem
    if not stem or "/" in stem or "\\" in stem or stem in {".", ".."}:
        raise GatewayCorrelationExplorerError(
            "Gateway request directory contains an invalid record id."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GatewayCorrelationExplorerError(
            f"Gateway request record is corrupted: {stem}"
        ) from exc
    if not isinstance(payload, dict):
        raise GatewayCorrelationExplorerError(
            f"Gateway request record is corrupted: {stem}"
        )
    record = read_gateway_request(stem, request_dir=request_dir)
    if record is None:
        raise GatewayCorrelationExplorerError(
            f"Gateway request record is corrupted: {stem}"
        )
    updated_at = str(payload.get("updated_at") or "")
    return record, updated_at


def _scan_gateway_request_entries(
    *,
    request_dir: Path | None = None,
    limit: int = DEFAULT_CORRELATION_SCAN_LIMIT,
) -> tuple[tuple[CooDispatchGatewayRequestRecord, str], ...]:
    base_dir = request_dir or default_gateway_request_dir()
    resolved_dir = base_dir.resolve()
    _assert_within_hermes_home(resolved_dir, label="directory")
    if not resolved_dir.is_dir():
        return ()
    entries: list[tuple[CooDispatchGatewayRequestRecord, str]] = []
    for index, path in enumerate(sorted(resolved_dir.glob("*.json"))):
        if index >= limit:
            raise GatewayCorrelationExplorerError(
                "Gateway request scan limit exceeded."
            )
        if not path.is_file() or path.is_symlink():
            continue
        entries.append(_read_gateway_request_entry(path, request_dir=resolved_dir))
    entries.sort(
        key=lambda item: (item[1], item[0].gateway_request_id),
        reverse=True,
    )
    return tuple(entries)


def _scan_pilot_history_records(
    *,
    history_dir: Path | None = None,
    limit: int = DEFAULT_CORRELATION_SCAN_LIMIT,
) -> tuple[CooDispatchPilotHistoryRecord, ...]:
    records = list_pilot_history_records(history_dir=history_dir)
    if len(records) > limit:
        raise GatewayCorrelationExplorerError("Pilot history scan limit exceeded.")
    return records


def _unique_gateway_request_ids(
    records: tuple[CooDispatchGatewayRequestRecord, ...],
) -> tuple[str, ...]:
    seen: list[str] = []
    for record in records:
        if record.gateway_request_id and record.gateway_request_id not in seen:
            seen.append(record.gateway_request_id)
    return tuple(seen)


def _require_single_request_id(
    records: tuple[CooDispatchGatewayRequestRecord, ...],
    *,
    query_type: str,
    query_id: str,
) -> str:
    request_ids = _unique_gateway_request_ids(records)
    if not request_ids:
        raise GatewayCorrelationExplorerError(
            f"Gateway request not found for {query_type}: {query_id}"
        )
    if len(request_ids) > 1:
        raise GatewayCorrelationExplorerError(
            f"Ambiguous correlation lookup for {query_type}: {query_id}"
        )
    return request_ids[0]


def _resolve_from_pilot_attempt_id(
    pilot_attempt_id: str,
    *,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
) -> tuple[str, bool]:
    history = None
    try:
        history = read_pilot_history_record(pilot_attempt_id, history_dir=history_dir)
    except (KeyError, ValueError):
        history = None
    if history is not None and history.gateway_request_id:
        return history.gateway_request_id, False

    matches = tuple(
        record
        for record, _updated_at in _scan_gateway_request_entries(request_dir=request_dir)
        if record.pilot_attempt_id == pilot_attempt_id
    )
    return _require_single_request_id(
        matches,
        query_type=QUERY_TYPE_PILOT_ATTEMPT,
        query_id=pilot_attempt_id,
    ), False


def _resolve_from_execution_attempt_id(
    execution_attempt_id: str,
    *,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
) -> tuple[str, bool]:
    request_matches = tuple(
        record
        for record, _updated_at in _scan_gateway_request_entries(request_dir=request_dir)
        if record.execution_attempt_id == execution_attempt_id
    )
    request_ids = _unique_gateway_request_ids(request_matches)
    if len(request_ids) > 1:
        raise GatewayCorrelationExplorerError(
            f"Ambiguous correlation lookup for {QUERY_TYPE_EXECUTION_ATTEMPT}: "
            f"{execution_attempt_id}"
        )
    if len(request_ids) == 1:
        return request_ids[0], False

    history_matches = tuple(
        record
        for record in _scan_pilot_history_records(history_dir=history_dir)
        if record.execution_attempt_id == execution_attempt_id
    )
    history_request_ids = tuple(
        dict.fromkeys(
            record.gateway_request_id
            for record in history_matches
            if record.gateway_request_id
        )
    )
    if len(history_request_ids) > 1:
        raise GatewayCorrelationExplorerError(
            f"Ambiguous correlation lookup for {QUERY_TYPE_EXECUTION_ATTEMPT}: "
            f"{execution_attempt_id}"
        )
    if len(history_request_ids) == 1:
        return history_request_ids[0], False

    raise GatewayCorrelationExplorerError(
        f"Gateway request not found for {QUERY_TYPE_EXECUTION_ATTEMPT}: "
        f"{execution_attempt_id}"
    )


def _resolve_from_dispatch_run_id(
    dispatch_run_id: str,
    *,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    audit_dir: Path | None = None,
) -> tuple[str, bool]:
    request_matches = tuple(
        record
        for record, _updated_at in _scan_gateway_request_entries(request_dir=request_dir)
        if record.dispatch_run_id == dispatch_run_id
    )
    request_ids = _unique_gateway_request_ids(request_matches)

    history_matches = tuple(
        record
        for record in _scan_pilot_history_records(history_dir=history_dir)
        if record.dispatch_run_id == dispatch_run_id
    )
    history_request_ids = tuple(
        dict.fromkeys(
            record.gateway_request_id
            for record in history_matches
            if record.gateway_request_id
        )
    )

    audit_request_ids: list[str] = []
    from agent.coo.dispatch_execution_audit import default_audit_dir, read_dispatch_execution_audit

    resolved_audit_dir = audit_dir or default_audit_dir()
    audit_path = resolved_audit_dir / f"{dispatch_run_id}.json"
    if audit_path.is_file() and not audit_path.is_symlink():
        try:
            audit = read_dispatch_execution_audit(
                dispatch_run_id,
                audit_dir=resolved_audit_dir,
            )
        except (KeyError, ValueError, OSError) as exc:
            raise GatewayCorrelationExplorerError(
                f"Dispatch audit record is corrupted: {dispatch_run_id}"
            ) from exc
        for record, _updated_at in _scan_gateway_request_entries(request_dir=request_dir):
            if (
                record.execution_attempt_id
                and record.execution_attempt_id == audit.execution_attempt_id
            ):
                audit_request_ids.append(record.gateway_request_id)
            elif record.dispatch_run_id == dispatch_run_id:
                audit_request_ids.append(record.gateway_request_id)

    combined = tuple(
        dict.fromkeys([*request_ids, *history_request_ids, *audit_request_ids])
    )
    if not combined:
        raise GatewayCorrelationExplorerError(
            f"Gateway request not found for {QUERY_TYPE_DISPATCH_RUN}: {dispatch_run_id}"
        )
    if len(combined) > 1:
        raise GatewayCorrelationExplorerError(
            f"Ambiguous correlation lookup for {QUERY_TYPE_DISPATCH_RUN}: {dispatch_run_id}"
        )
    return combined[0], False


def _resolve_from_ticket_id(
    ticket_id: str,
    *,
    request_dir: Path | None = None,
) -> tuple[str, bool]:
    matches = tuple(
        (record, updated_at)
        for record, updated_at in _scan_gateway_request_entries(request_dir=request_dir)
        if record.ticket_id == ticket_id
    )
    if not matches:
        raise GatewayCorrelationExplorerError(
            f"Gateway request not found for {QUERY_TYPE_TICKET}: {ticket_id}"
        )
    newest_record, newest_updated_at = matches[0]
    if len(matches) > 1:
        second_record, second_updated_at = matches[1]
        if (
            newest_updated_at
            and newest_updated_at == second_updated_at
            and newest_record.gateway_request_id != second_record.gateway_request_id
        ):
            return newest_record.gateway_request_id, True
    return newest_record.gateway_request_id, False


def resolve_gateway_request_id_for_query(
    query: GatewayCorrelationQuery,
    *,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    audit_dir: Path | None = None,
) -> tuple[str, bool]:
    """Resolve one gateway_request_id from a validated query."""
    if query.query_type == QUERY_TYPE_GATEWAY_REQUEST:
        record = read_gateway_request(query.query_id, request_dir=request_dir)
        if record is None:
            raise GatewayCorrelationExplorerError(
                f"Gateway request not found for {QUERY_TYPE_GATEWAY_REQUEST}: "
                f"{query.query_id}"
            )
        return query.query_id, False
    if query.query_type == QUERY_TYPE_PILOT_ATTEMPT:
        return _resolve_from_pilot_attempt_id(
            query.query_id,
            request_dir=request_dir,
            history_dir=history_dir,
        )
    if query.query_type == QUERY_TYPE_EXECUTION_ATTEMPT:
        return _resolve_from_execution_attempt_id(
            query.query_id,
            request_dir=request_dir,
            history_dir=history_dir,
        )
    if query.query_type == QUERY_TYPE_DISPATCH_RUN:
        return _resolve_from_dispatch_run_id(
            query.query_id,
            request_dir=request_dir,
            history_dir=history_dir,
            audit_dir=audit_dir,
        )
    if query.query_type == QUERY_TYPE_TICKET:
        return _resolve_from_ticket_id(query.query_id, request_dir=request_dir)
    raise GatewayCorrelationExplorerError(f"Unknown correlation query type: {query.query_type}")


def _recommended_action(
    *,
    audit: CooDispatchGatewayRequestAuditSummary,
    ambiguity_detected: bool,
) -> str:
    if ambiguity_detected:
        return RECOMMENDED_ACTION_PROVIDE_MORE_SPECIFIC_ID
    if not audit.correlation_valid:
        return RECOMMENDED_ACTION_RESOLVE_CORRELATION_MISMATCH
    if audit.recovery_required:
        return RECOMMENDED_ACTION_RESOLVE_RECOVERY_REQUIRED
    if audit.request_status == "failed" or audit.pilot_status in {"failure", "timeout"}:
        return RECOMMENDED_ACTION_INSPECT_EXECUTION_FAILURE
    if audit.execution_attempt_id not in {"", _NONE_LABEL} and not audit.evidence_present:
        return RECOMMENDED_ACTION_INSPECT_MISSING_EVIDENCE
    if audit.consume_state in {"partial", "legacy_partial", "recovery_required", "prepared"}:
        return RECOMMENDED_ACTION_INSPECT_CONSUME_STATE
    if audit.chain_complete and audit.correlation_valid:
        return RECOMMENDED_ACTION_NO_ACTION_REQUIRED
    if not audit.evidence_present or not audit.audit_present:
        return RECOMMENDED_ACTION_INSPECT_MISSING_EVIDENCE
    return RECOMMENDED_ACTION_INSPECT_CONSUME_STATE


def _failure_reason_code(
    *,
    audit: CooDispatchGatewayRequestAuditSummary,
    ambiguity_detected: bool,
) -> str:
    if ambiguity_detected:
        return _FAILURE_AMBIGUOUS_LOOKUP
    if not audit.correlation_valid:
        return _FAILURE_CORRELATION_MISMATCH
    return audit.failure_reason_code or _NONE_LABEL


def _chain_from_audit(
    *,
    query: GatewayCorrelationQuery,
    audit: CooDispatchGatewayRequestAuditSummary,
    ambiguity_detected: bool,
) -> CooDispatchGatewayCorrelationChain:
    recommended = _recommended_action(audit=audit, ambiguity_detected=ambiguity_detected)
    return CooDispatchGatewayCorrelationChain(
        query_type=query.query_type,
        query_id=query.query_id,
        gateway_request_id=audit.gateway_request_id,
        session_id=audit.session_id or _NONE_LABEL,
        ticket_id=audit.ticket_id or _NONE_LABEL,
        confirmation_id=audit.confirmation_id or _NONE_LABEL,
        pilot_attempt_id=audit.pilot_attempt_id,
        execution_attempt_id=audit.execution_attempt_id,
        dispatch_run_id=audit.dispatch_run_id,
        request_status=audit.request_status,
        pilot_status=audit.pilot_status,
        execution_status=audit.execution_status,
        audit_present=audit.audit_present,
        evidence_present=audit.evidence_present,
        consume_state=audit.consume_state,
        consumed=audit.consumed,
        recovery_required=audit.recovery_required,
        repair_attempt_id=audit.repair_attempt_id,
        repair_audit_present=audit.repair_audit_present,
        repair_lock_held=audit.repair_lock_held,
        correlation_valid=audit.correlation_valid,
        chain_complete=audit.chain_complete,
        ambiguity_detected=ambiguity_detected,
        failure_reason_code=_failure_reason_code(
            audit=audit,
            ambiguity_detected=ambiguity_detected,
        ),
        production_execution_allowed=False,
        production_root_hard_deny=True,
        gateway_execution_scope=EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
        recommended_action=recommended,
    )


def explore_gateway_correlation(
    query: GatewayCorrelationQuery,
    *,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> CooDispatchGatewayCorrelationChain:
    """Explore read-only Gateway correlation chain for one query."""
    try:
        gateway_request_id, ambiguity_detected = resolve_gateway_request_id_for_query(
            query,
            request_dir=request_dir,
            history_dir=history_dir,
            audit_dir=audit_dir,
        )
        audit = summarize_gateway_request_audit(
            gateway_request_id,
            request_dir=request_dir,
            history_dir=history_dir,
            evidence_dir=evidence_dir,
            audit_dir=audit_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
    except DispatchGatewayRequestStoreError as exc:
        raise GatewayCorrelationExplorerError(str(exc)) from exc
    except KeyError as exc:
        raise GatewayCorrelationExplorerError(
            f"Gateway request not found for {query.query_type}: {query.query_id}"
        ) from exc
    return _chain_from_audit(
        query=query,
        audit=audit,
        ambiguity_detected=ambiguity_detected,
    )


def correlation_chain_exit_code(chain: CooDispatchGatewayCorrelationChain) -> int:
    """Return CLI exit code for one correlation chain."""
    if chain.ambiguity_detected:
        return 1
    if not chain.correlation_valid:
        return 1
    if chain.failure_reason_code in {
        _FAILURE_AMBIGUOUS_LOOKUP,
        _FAILURE_REQUEST_NOT_FOUND,
        _FAILURE_CORRELATION_MISMATCH,
    }:
        return 1
    if chain.recommended_action in {
        RECOMMENDED_ACTION_INSPECT_EXECUTION_FAILURE,
        RECOMMENDED_ACTION_RESOLVE_RECOVERY_REQUIRED,
        RECOMMENDED_ACTION_RESOLVE_CORRELATION_MISMATCH,
        RECOMMENDED_ACTION_PROVIDE_MORE_SPECIFIC_ID,
    }:
        return 1
    return 0


def format_gateway_correlation_chain(
    chain: CooDispatchGatewayCorrelationChain,
) -> str:
    """Format safe correlation chain output without secrets or paths."""
    lines = [
        "Gateway Correlation Chain",
        "",
        "[Query]",
        f"query_type: {chain.query_type}",
        f"query_id: {chain.query_id}",
        "",
        "[Gateway Request]",
        f"gateway_request_id: {chain.gateway_request_id}",
        f"request_status: {chain.request_status}",
        f"session_id: {chain.session_id}",
        f"ticket_id: {chain.ticket_id}",
        f"confirmation_id: {chain.confirmation_id}",
        "",
        "[Pilot]",
        f"pilot_attempt_id: {chain.pilot_attempt_id}",
        f"pilot_status: {chain.pilot_status}",
        "",
        "[Execution]",
        f"execution_attempt_id: {chain.execution_attempt_id}",
        f"dispatch_run_id: {chain.dispatch_run_id}",
        f"execution_status: {chain.execution_status}",
        f"audit_present: {str(chain.audit_present).lower()}",
        f"evidence_present: {str(chain.evidence_present).lower()}",
        "",
        "[Consume]",
        f"consume_state: {chain.consume_state}",
        f"recovery_required: {str(chain.recovery_required).lower()}",
        f"consumed: {str(chain.consumed).lower()}",
        "",
        "[Repair]",
        f"repair_attempt_id: {chain.repair_attempt_id}",
        f"repair_audit_present: {str(chain.repair_audit_present).lower()}",
        f"repair_lock_held: {str(chain.repair_lock_held).lower()}",
        "",
        "[Correlation]",
        f"correlation_valid: {str(chain.correlation_valid).lower()}",
        f"chain_complete: {str(chain.chain_complete).lower()}",
        f"ambiguity_detected: {str(chain.ambiguity_detected).lower()}",
        f"failure_reason_code: {chain.failure_reason_code}",
        f"recommended_action: {chain.recommended_action}",
    ]
    from agent.coo.dispatch_operator_guidance import append_guidance_output_lines

    append_guidance_output_lines(lines, chain.recommended_action)
    lines.extend(
        [
            "",
            "[Safety]",
            "production_execution_allowed: false",
            f"production_root_hard_deny: {str(chain.production_root_hard_deny).lower()}",
            f"gateway_execution_scope: {chain.gateway_execution_scope}",
        ]
    )
    output = "\n".join(lines)
    lowered = output.lower()
    for token in _FORBIDDEN_OUTPUT_KEYS:
        if token in lowered:
            raise GatewayCorrelationExplorerError(
                f"Unsafe correlation explorer output field: {token!r}"
            )
    return output
