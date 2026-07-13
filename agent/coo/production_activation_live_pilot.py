"""Production activation live pilot preflight — Phase 14H-3B.

CLI-only preflight, reservation creation, and ephemeral permit evaluation.
No subprocess, runner execution, or Repository2 access.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_pipeline_root_trust import (
    assert_pipeline_root_allowed,
    resolve_pipeline_root,
)
from agent.coo.production_activation_dry_run import (
    compute_dry_run_key,
    default_dry_run_history_dir,
    find_dry_run_record,
)
from agent.coo.production_activation_execution_gate import (
    ProductionActivationExecutionGateError,
    default_execution_gate_history_dir,
    evaluate_production_execution_gate,
    find_ready_execution_gate_record,
)
from agent.coo.production_activation_execution_permit import (
    ActivationExecutionPermitError,
    evaluate_permit_ready,
)
from agent.coo.production_activation_live_harness import (
    ProductionActivationLiveHarnessError,
    run_live_harness_wiring,
)
from agent.coo.production_activation_execution_reservation import (
    ProductionActivationExecutionReservation,
    ProductionActivationExecutionReservationError,
    RESERVATION_STATE_RESERVED,
    _idempotent_failure_for_state,
    _load_activation_or_fail,
    _map_gate_blocking_to_failure,
    _reservation_scope_matches,
    create_execution_reservation,
    default_reservation_store_dir,
    load_execution_reservation,
    probe_reservation_store_available,
    validate_unlock_token_correlation,
)
from agent.coo.production_executor_confirmation import REQUIRED_CONFIRMATION_PHRASE
from hermes_constants import get_hermes_home

FAIL_ACTIVATION_NOT_ACTIVE = "activation_not_active"
FAIL_ACTIVE_EXPIRED = "active_expired"
FAIL_EXECUTION_GATE_NOT_READY = "execution_gate_not_ready"
FAIL_EXECUTION_GATE_CORRELATION_MISMATCH = "execution_gate_correlation_mismatch"
FAIL_DRY_RUN_NOT_READY = "dry_run_not_ready"
FAIL_DRY_RUN_CORRELATION_MISMATCH = "dry_run_correlation_mismatch"
FAIL_TICKET_SCOPE_MISMATCH = "ticket_scope_mismatch"
FAIL_CONFIRMATION_SCOPE_MISMATCH = "confirmation_scope_mismatch"
FAIL_MIRROR_ROOT_NOT_TRUSTED = "mirror_root_not_trusted"
FAIL_PRODUCTION_ROOT_DENIED = "production_root_denied"
FAIL_PUBLISH_NOT_ALLOWED = "publish_not_allowed"
FAIL_BUNDLE_CONSUMED = "bundle_consumed"
FAIL_CONFIRMATION_CONSUMED = "confirmation_consumed"
FAIL_BINDING_NOT_BOUND = "binding_not_bound"
FAIL_RUNNER_CONTRACT_MISSING = "runner_contract_missing"
FAIL_KILL_SWITCH_UNAVAILABLE = "kill_switch_unavailable"
FAIL_RECOVERY_REQUIRED = "recovery_required"
FAIL_REPAIR_LOCK_HELD = "repair_lock_held"
FAIL_SIGNOFF_NOT_READY = "signoff_not_ready"
FAIL_CUTOVER_NOT_READY = "cutover_not_ready"
FAIL_INVALID_EXECUTION_PHRASE = "invalid_execution_phrase"
FAIL_RESERVATION_IN_PROGRESS = "reservation_in_progress"
FAIL_EXECUTION_IN_PROGRESS = "execution_in_progress"
FAIL_ALREADY_COMPLETED = "activation_execution_already_completed"
FAIL_REQUIRES_NEW_PROPOSAL = "activation_execution_requires_new_proposal"
FAIL_RESERVATION_SCOPE_CONFLICT = "reservation_scope_conflict"
FAIL_RESERVATION_WRITE_FAILED = "reservation_write_failed"
FAIL_PERMIT_NOT_READY = "permit_not_ready"
FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C_2 = "blocked_wait_for_phase_14h_3c_2"
FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C = FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C_2

ACTION_CONTINUE_TO_PHASE_14H_3C_2 = "continue_to_phase_14h_3c_2"
ACTION_CONTINUE_TO_PHASE_14H_3C = ACTION_CONTINUE_TO_PHASE_14H_3C_2
ACTION_WAIT_FOR_EXISTING_RESERVATION = "wait_for_existing_reservation"
ACTION_CREATE_NEW_ACTIVATION_PROPOSAL = "create_new_activation_proposal"
ACTION_RESOLVE_EXECUTION_GATE = "resolve_execution_gate"
ACTION_REFRESH_PRODUCTION_DRY_RUN = "refresh_production_dry_run"
ACTION_RESOLVE_TICKET_SCOPE = "resolve_ticket_scope"
ACTION_RESOLVE_CONFIRMATION_SCOPE = "resolve_confirmation_scope"
ACTION_PREPARE_ISOLATED_PRODUCTION_MIRROR = "prepare_isolated_production_mirror"
ACTION_BIND_PRODUCTION_EXECUTOR = "bind_production_executor"
ACTION_RESOLVE_RECOVERY_ISSUE = "resolve_recovery_issue"
ACTION_RESOLVE_REGRESSION_FAILURE = "resolve_regression_failure"
ACTION_SUSPEND_ACTIVE_ACTIVATION = "suspend_active_activation"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"

_PREFLIGHT_STORE_DIR = "production-activation-execution-preflight"
_PREFLIGHT_STORE_VERSION = 1

_EVENT_LIVE_PILOT_PREFLIGHT_EVALUATED = "live_pilot_preflight_evaluated"
_EVENT_RESERVATION_CREATED = "reservation_created"
_EVENT_RESERVATION_BLOCKED = "reservation_blocked"
_EVENT_PERMIT_EVALUATED = "permit_evaluated"
_EVENT_EXECUTION_BLOCKED_WAITING = "execution_blocked_waiting_phase_14h_3c_2"

_FORBIDDEN_OUTPUT_TOKENS = frozenset(
    {
        "pipeline_root",
        "confirmation_phrase",
        "unlock_token",
        "repository2",
        "repository_attestation_hash",
        "rollback_commit",
        "argv",
        "cwd",
        "env",
        "stdout",
        "stderr",
        "secret",
        "token",
        "filesystem",
        "/opt/data/",
        "pipeline.js",
        "requester_id",
        "executor_id",
        "active_actor_id",
        "actor_id",
        "requested_by",
        "dry_run_key",
        "confirm-production-activation",
        "confirm-repository2-execution",
    }
)


class ProductionActivationLivePilotError(ValueError):
    """Raised when live pilot preflight cannot complete safely."""


@dataclass(frozen=True)
class ProductionActivationLivePilotPreflightResult:
    """Safe live pilot preflight result without execution."""

    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    state: str
    preflight_ready: bool
    permit_ready: bool
    phrase_verified: bool
    execution_gate_verified: bool
    dry_run_verified: bool
    single_ticket_scope: bool
    draft_only: bool
    publish_allowed: bool = False
    production_execution_allowed: bool = False
    repository2_execution_attempted: bool = False
    execution_runtime_invoked: bool = False
    harness_ready: bool = False
    runtime_invocation_planned: bool = False
    harness_request_valid: bool = False
    harness_reservation_valid: bool = False
    harness_permit_valid: bool = False
    harness_active_valid: bool = False
    harness_gate_valid: bool = False
    harness_mirror_valid: bool = False
    harness_runner_profile_valid: bool = False
    harness_argv_contract_valid: bool = False
    harness_cwd_contract_valid: bool = False
    harness_env_contract_valid: bool = False
    harness_timeout_valid: bool = False
    failure_reason_code: str = ""
    recommended_action: str = ""


@dataclass(frozen=True)
class ProductionActivationLivePilotPreflightRecord:
    event_id: str
    event_type: str
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    ticket_id: str
    confirmation_id: str
    gate_event_id: str
    dry_run_event_id: str
    result: str
    failure_reason_code: str
    timestamp: str
    production_execution_allowed: bool = False
    repository2_execution_attempted: bool = False


def _utc_now_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.isoformat()


def default_preflight_history_dir() -> Path:
    return get_hermes_home() / "coo" / _PREFLIGHT_STORE_DIR


def _preflight_history_path(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionActivationLivePilotError("activation_request_id is required")
    base = (history_dir or default_preflight_history_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationLivePilotError(
            "Preflight history directory must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def _record_to_dict(record: ProductionActivationLivePilotPreflightRecord) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "event_type": record.event_type,
        "activation_request_id": record.activation_request_id,
        "reservation_id": record.reservation_id,
        "execution_attempt_id": record.execution_attempt_id,
        "ticket_id": record.ticket_id,
        "confirmation_id": record.confirmation_id,
        "gate_event_id": record.gate_event_id,
        "dry_run_event_id": record.dry_run_event_id,
        "result": record.result,
        "failure_reason_code": record.failure_reason_code,
        "timestamp": record.timestamp,
        "production_execution_allowed": False,
        "repository2_execution_attempted": False,
    }


def _load_preflight_records(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> list[ProductionActivationLivePilotPreflightRecord]:
    path = _preflight_history_path(activation_request_id, history_dir=history_dir)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProductionActivationLivePilotError(
            "Preflight history record is corrupted."
        ) from exc
    records_payload = payload.get("records", [])
    if not isinstance(records_payload, list):
        raise ProductionActivationLivePilotError(
            "Preflight history records must be a list."
        )
    records: list[ProductionActivationLivePilotPreflightRecord] = []
    for item in records_payload:
        if not isinstance(item, dict):
            raise ProductionActivationLivePilotError(
                "Preflight history record must be an object."
            )
        records.append(
            ProductionActivationLivePilotPreflightRecord(
                event_id=str(item.get("event_id", "")),
                event_type=str(item.get("event_type", "")),
                activation_request_id=str(item.get("activation_request_id", "")),
                reservation_id=str(item.get("reservation_id", "")),
                execution_attempt_id=str(item.get("execution_attempt_id", "")),
                ticket_id=str(item.get("ticket_id", "")),
                confirmation_id=str(item.get("confirmation_id", "")),
                gate_event_id=str(item.get("gate_event_id", "")),
                dry_run_event_id=str(item.get("dry_run_event_id", "")),
                result=str(item.get("result", "")),
                failure_reason_code=str(item.get("failure_reason_code") or ""),
                timestamp=str(item.get("timestamp", "")),
                production_execution_allowed=False,
                repository2_execution_attempted=False,
            )
        )
    return records


def _atomic_append_preflight_record(
    record: ProductionActivationLivePilotPreflightRecord,
    *,
    history_dir: Path | None = None,
) -> None:
    path = _preflight_history_path(
        record.activation_request_id,
        history_dir=history_dir,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_preflight_records(
        record.activation_request_id,
        history_dir=history_dir,
    )
    for item in existing:
        if item.event_id == record.event_id:
            return
    payload = {
        "version": _PREFLIGHT_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "records": [_record_to_dict(item) for item in existing]
        + [_record_to_dict(record)],
    }
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        raise ProductionActivationLivePilotError(
            "Preflight audit persistence failed."
        ) from exc


def probe_preflight_audit_store_available(*, history_dir: Path | None = None) -> bool:
    base = (history_dir or default_preflight_history_dir()).resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def validate_live_pilot_execution_phrase(phrase: str) -> bool:
    return (phrase or "").strip() == REQUIRED_CONFIRMATION_PHRASE


def _resolve_recommended_action(failure_code: str) -> str:
    mapping = {
        FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C: ACTION_CONTINUE_TO_PHASE_14H_3C,
        FAIL_RESERVATION_IN_PROGRESS: ACTION_WAIT_FOR_EXISTING_RESERVATION,
        FAIL_EXECUTION_IN_PROGRESS: ACTION_WAIT_FOR_EXISTING_RESERVATION,
        FAIL_ALREADY_COMPLETED: ACTION_CREATE_NEW_ACTIVATION_PROPOSAL,
        FAIL_REQUIRES_NEW_PROPOSAL: ACTION_CREATE_NEW_ACTIVATION_PROPOSAL,
        FAIL_EXECUTION_GATE_NOT_READY: ACTION_RESOLVE_EXECUTION_GATE,
        FAIL_EXECUTION_GATE_CORRELATION_MISMATCH: ACTION_RESOLVE_EXECUTION_GATE,
        FAIL_DRY_RUN_NOT_READY: ACTION_REFRESH_PRODUCTION_DRY_RUN,
        FAIL_DRY_RUN_CORRELATION_MISMATCH: ACTION_REFRESH_PRODUCTION_DRY_RUN,
        FAIL_TICKET_SCOPE_MISMATCH: ACTION_RESOLVE_TICKET_SCOPE,
        FAIL_CONFIRMATION_SCOPE_MISMATCH: ACTION_RESOLVE_CONFIRMATION_SCOPE,
        FAIL_MIRROR_ROOT_NOT_TRUSTED: ACTION_PREPARE_ISOLATED_PRODUCTION_MIRROR,
        FAIL_PRODUCTION_ROOT_DENIED: ACTION_MAINTAIN_PRODUCTION_BLOCK,
        FAIL_BINDING_NOT_BOUND: ACTION_BIND_PRODUCTION_EXECUTOR,
        FAIL_RUNNER_CONTRACT_MISSING: ACTION_BIND_PRODUCTION_EXECUTOR,
        FAIL_RECOVERY_REQUIRED: ACTION_RESOLVE_RECOVERY_ISSUE,
        FAIL_REPAIR_LOCK_HELD: ACTION_RESOLVE_RECOVERY_ISSUE,
        FAIL_SIGNOFF_NOT_READY: ACTION_MAINTAIN_PRODUCTION_BLOCK,
        FAIL_CUTOVER_NOT_READY: ACTION_MAINTAIN_PRODUCTION_BLOCK,
        FAIL_KILL_SWITCH_UNAVAILABLE: ACTION_SUSPEND_ACTIVE_ACTIVATION,
        FAIL_ACTIVE_EXPIRED: ACTION_SUSPEND_ACTIVE_ACTIVATION,
        FAIL_RESERVATION_SCOPE_CONFLICT: ACTION_CREATE_NEW_ACTIVATION_PROPOSAL,
    }
    return mapping.get(failure_code, ACTION_MAINTAIN_PRODUCTION_BLOCK)


def _blocked_result(
    *,
    activation_request_id: str,
    failure_code: str,
    phrase_verified: bool = False,
    execution_gate_verified: bool = False,
    dry_run_verified: bool = False,
    single_ticket_scope: bool = False,
    draft_only: bool = False,
) -> ProductionActivationLivePilotPreflightResult:
    return ProductionActivationLivePilotPreflightResult(
        activation_request_id=activation_request_id,
        reservation_id="",
        execution_attempt_id="",
        state="blocked",
        preflight_ready=False,
        permit_ready=False,
        phrase_verified=phrase_verified,
        execution_gate_verified=execution_gate_verified,
        dry_run_verified=dry_run_verified,
        single_ticket_scope=single_ticket_scope,
        draft_only=draft_only,
        failure_reason_code=failure_code,
        recommended_action=_resolve_recommended_action(failure_code),
    )


def _append_preflight_event(
    *,
    event_type: str,
    activation_request_id: str,
    ticket_id: str,
    confirmation_id: str,
    reservation_id: str = "",
    execution_attempt_id: str = "",
    gate_event_id: str = "",
    dry_run_event_id: str = "",
    result: str,
    failure_reason_code: str = "",
    history_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    record = ProductionActivationLivePilotPreflightRecord(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        activation_request_id=activation_request_id,
        reservation_id=reservation_id,
        execution_attempt_id=execution_attempt_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        gate_event_id=gate_event_id,
        dry_run_event_id=dry_run_event_id,
        result=result,
        failure_reason_code=failure_reason_code,
        timestamp=_utc_now_iso(now),
        production_execution_allowed=False,
        repository2_execution_attempted=False,
    )
    _atomic_append_preflight_record(record, history_dir=history_dir)


def run_production_activation_live_pilot_preflight(
    *,
    activation_request_id: str,
    ticket_id: str,
    confirmation_id: str,
    unlock_token_id: str,
    requester_id: str,
    pipeline_root: str,
    phrase: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    gate_history_dir: Path | None = None,
    dry_run_history_dir: Path | None = None,
    reservation_dir: Path | None = None,
    preflight_history_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionActivationLivePilotPreflightResult:
    """Run live pilot preflight without execution or reservation on phrase failure."""
    normalized_activation = (activation_request_id or "").strip()
    normalized_ticket = (ticket_id or "").strip()
    normalized_confirmation = (confirmation_id or "").strip()

    if not validate_live_pilot_execution_phrase(phrase):
        return _blocked_result(
            activation_request_id=normalized_activation,
            failure_code=FAIL_INVALID_EXECUTION_PHRASE,
        )

    if not probe_preflight_audit_store_available(history_dir=preflight_history_dir):
        raise ProductionActivationLivePilotError("Preflight audit store unavailable.")
    if not probe_reservation_store_available(store_dir=reservation_dir):
        raise ProductionActivationLivePilotError("Reservation store unavailable.")

    request = _load_activation_or_fail(normalized_activation, store_dir=store_dir)

    try:
        resolved_root = resolve_pipeline_root(pipeline_root)
        assert_pipeline_root_allowed(resolved_root)
    except ValueError as exc:
        message = str(exc).lower()
        failure = (
            FAIL_PRODUCTION_ROOT_DENIED
            if "hard-denied" in message
            else FAIL_MIRROR_ROOT_NOT_TRUSTED
        )
        _append_preflight_event(
            event_type=_EVENT_RESERVATION_BLOCKED,
            activation_request_id=normalized_activation,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            result="blocked",
            failure_reason_code=failure,
            history_dir=preflight_history_dir,
            now=now,
        )
        return _blocked_result(
            activation_request_id=normalized_activation,
            failure_code=failure,
            phrase_verified=True,
        )

    gate_key = compute_dry_run_key(
        activation_request_id=normalized_activation,
        ticket_id=normalized_ticket,
        confirmation_id=normalized_confirmation,
        pipeline_root_resolved=resolved_root,
    )

    resolved_gate_history = gate_history_dir or default_execution_gate_history_dir()
    resolved_dry_run_history = dry_run_history_dir or default_dry_run_history_dir()

    existing = load_execution_reservation(
        normalized_activation,
        store_dir=reservation_dir,
    )
    if existing is not None:
        if not _reservation_scope_matches(
            existing,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            gate_key=gate_key,
        ):
            failure = FAIL_RESERVATION_SCOPE_CONFLICT
            _append_preflight_event(
                event_type=_EVENT_RESERVATION_BLOCKED,
                activation_request_id=normalized_activation,
                ticket_id=normalized_ticket,
                confirmation_id=normalized_confirmation,
                result="blocked",
                failure_reason_code=failure,
                history_dir=preflight_history_dir,
                now=now,
            )
            return _blocked_result(
                activation_request_id=normalized_activation,
                failure_code=failure,
                phrase_verified=True,
            )
        failure = _idempotent_failure_for_state(existing.state)
        _append_preflight_event(
            event_type=_EVENT_LIVE_PILOT_PREFLIGHT_EVALUATED,
            activation_request_id=normalized_activation,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            reservation_id=existing.reservation_id,
            execution_attempt_id=existing.execution_attempt_id,
            gate_event_id=existing.execution_gate_event_id,
            dry_run_event_id=existing.dry_run_event_id,
            result="blocked",
            failure_reason_code=failure,
            history_dir=preflight_history_dir,
            now=now,
        )
        return _blocked_result(
            activation_request_id=normalized_activation,
            failure_code=failure,
            phrase_verified=True,
        )

    try:
        assessment = evaluate_production_execution_gate(
            request,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            pipeline_root=pipeline_root,
            repo_root=repo_root,
            store_dir=store_dir,
            history_dir=resolved_gate_history,
            dry_run_history_dir=resolved_dry_run_history,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            merged_config=merged_config,
            now=now,
        )
    except ProductionActivationExecutionGateError as exc:
        raise ProductionActivationLivePilotError(str(exc)) from exc

    if not assessment.execution_gate_ready:
        failure = (
            _map_gate_blocking_to_failure(assessment.blocking_reasons[0])
            if assessment.blocking_reasons
            else FAIL_EXECUTION_GATE_NOT_READY
        )
        _append_preflight_event(
            event_type=_EVENT_RESERVATION_BLOCKED,
            activation_request_id=normalized_activation,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            result="blocked",
            failure_reason_code=failure,
            history_dir=preflight_history_dir,
            now=now,
        )
        return _blocked_result(
            activation_request_id=normalized_activation,
            failure_code=failure,
            phrase_verified=True,
            execution_gate_verified=False,
            dry_run_verified=assessment.dry_run_verified,
            single_ticket_scope=assessment.single_ticket_scope,
            draft_only=assessment.draft_only,
        )

    gate_record = find_ready_execution_gate_record(
        normalized_activation,
        gate_key=gate_key,
        history_dir=resolved_gate_history,
    )
    if gate_record is None:
        failure = FAIL_EXECUTION_GATE_CORRELATION_MISMATCH
        _append_preflight_event(
            event_type=_EVENT_RESERVATION_BLOCKED,
            activation_request_id=normalized_activation,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            result="blocked",
            failure_reason_code=failure,
            history_dir=preflight_history_dir,
            now=now,
        )
        return _blocked_result(
            activation_request_id=normalized_activation,
            failure_code=failure,
            phrase_verified=True,
            execution_gate_verified=True,
            dry_run_verified=assessment.dry_run_verified,
            single_ticket_scope=assessment.single_ticket_scope,
            draft_only=assessment.draft_only,
        )

    dry_run_record = find_dry_run_record(
        normalized_activation,
        event_id=request.dry_run_event_id,
        history_dir=resolved_dry_run_history,
    )
    if dry_run_record is None or dry_run_record.result != "ready":
        failure = FAIL_DRY_RUN_NOT_READY
        _append_preflight_event(
            event_type=_EVENT_RESERVATION_BLOCKED,
            activation_request_id=normalized_activation,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            gate_event_id=gate_record.event_id,
            result="blocked",
            failure_reason_code=failure,
            history_dir=preflight_history_dir,
            now=now,
        )
        return _blocked_result(
            activation_request_id=normalized_activation,
            failure_code=failure,
            phrase_verified=True,
            execution_gate_verified=True,
            dry_run_verified=False,
            single_ticket_scope=assessment.single_ticket_scope,
            draft_only=assessment.draft_only,
        )

    if (
        dry_run_record.dry_run_key != gate_key
        or dry_run_record.ticket_id != normalized_ticket
        or dry_run_record.confirmation_id != normalized_confirmation
        or dry_run_record.tested_commit_sha != request.tested_commit_sha
        or dry_run_record.release_tag != request.release_tag
        or dry_run_record.event_id != (request.dry_run_event_id or "")
    ):
        failure = FAIL_DRY_RUN_CORRELATION_MISMATCH
        _append_preflight_event(
            event_type=_EVENT_RESERVATION_BLOCKED,
            activation_request_id=normalized_activation,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            gate_event_id=gate_record.event_id,
            dry_run_event_id=dry_run_record.event_id,
            result="blocked",
            failure_reason_code=failure,
            history_dir=preflight_history_dir,
            now=now,
        )
        return _blocked_result(
            activation_request_id=normalized_activation,
            failure_code=failure,
            phrase_verified=True,
            execution_gate_verified=True,
            dry_run_verified=False,
            single_ticket_scope=assessment.single_ticket_scope,
            draft_only=assessment.draft_only,
        )

    try:
        validate_unlock_token_correlation(
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            unlock_token_id=unlock_token_id,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
    except ProductionActivationExecutionReservationError as exc:
        message = str(exc).lower()
        if "consumed" in message:
            failure = (
                FAIL_BUNDLE_CONSUMED
                if "bundle" in message
                else FAIL_CONFIRMATION_CONSUMED
            )
        else:
            failure = FAIL_CONFIRMATION_SCOPE_MISMATCH
        _append_preflight_event(
            event_type=_EVENT_RESERVATION_BLOCKED,
            activation_request_id=normalized_activation,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            gate_event_id=gate_record.event_id,
            dry_run_event_id=dry_run_record.event_id,
            result="blocked",
            failure_reason_code=failure,
            history_dir=preflight_history_dir,
            now=now,
        )
        return _blocked_result(
            activation_request_id=normalized_activation,
            failure_code=failure,
            phrase_verified=True,
            execution_gate_verified=True,
            dry_run_verified=True,
            single_ticket_scope=assessment.single_ticket_scope,
            draft_only=assessment.draft_only,
        )

    _ = (requester_id or "").strip()

    try:
        reservation = create_execution_reservation(
            request,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            pipeline_root_resolved=resolved_root,
            gate_record=gate_record,
            dry_run_event_id=dry_run_record.event_id,
            gate_key=gate_key,
            store_dir=reservation_dir,
            now=now,
        )
    except ProductionActivationExecutionReservationError as exc:
        message = str(exc)
        if message == "reservation_scope_conflict":
            failure = FAIL_RESERVATION_SCOPE_CONFLICT
        elif message == "reservation_write_failed":
            failure = FAIL_RESERVATION_WRITE_FAILED
        elif message in {
            "reservation_in_progress",
            "execution_in_progress",
            "activation_execution_already_completed",
            "activation_execution_requires_new_proposal",
        }:
            failure = message
        else:
            failure = FAIL_RESERVATION_WRITE_FAILED
        _append_preflight_event(
            event_type=_EVENT_RESERVATION_BLOCKED,
            activation_request_id=normalized_activation,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            gate_event_id=gate_record.event_id,
            dry_run_event_id=dry_run_record.event_id,
            result="blocked",
            failure_reason_code=failure,
            history_dir=preflight_history_dir,
            now=now,
        )
        return _blocked_result(
            activation_request_id=normalized_activation,
            failure_code=failure,
            phrase_verified=True,
            execution_gate_verified=True,
            dry_run_verified=True,
            single_ticket_scope=assessment.single_ticket_scope,
            draft_only=assessment.draft_only,
        )

    _append_preflight_event(
        event_type=_EVENT_RESERVATION_CREATED,
        activation_request_id=normalized_activation,
        ticket_id=normalized_ticket,
        confirmation_id=normalized_confirmation,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        gate_event_id=reservation.execution_gate_event_id,
        dry_run_event_id=reservation.dry_run_event_id,
        result="reserved",
        history_dir=preflight_history_dir,
        now=now,
    )

    permit_ready = evaluate_permit_ready(
        reservation,
        pipeline_root=pipeline_root,
        store_dir=store_dir,
        reservation_dir=reservation_dir,
        gate_history_dir=resolved_gate_history,
        dry_run_history_dir=resolved_dry_run_history,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        merged_config=merged_config,
        now=now,
    )

    _append_preflight_event(
        event_type=_EVENT_PERMIT_EVALUATED,
        activation_request_id=normalized_activation,
        ticket_id=normalized_ticket,
        confirmation_id=normalized_confirmation,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        gate_event_id=reservation.execution_gate_event_id,
        dry_run_event_id=reservation.dry_run_event_id,
        result="ready" if permit_ready else "blocked",
        failure_reason_code="" if permit_ready else FAIL_PERMIT_NOT_READY,
        history_dir=preflight_history_dir,
        now=now,
    )

    if not permit_ready:
        return _blocked_result(
            activation_request_id=normalized_activation,
            failure_code=FAIL_PERMIT_NOT_READY,
            phrase_verified=True,
            execution_gate_verified=True,
            dry_run_verified=True,
            single_ticket_scope=assessment.single_ticket_scope,
            draft_only=assessment.draft_only,
        )

    try:
        harness_result = run_live_harness_wiring(
            request=request,
            reservation=reservation,
            ticket_id=normalized_ticket,
            confirmation_id=normalized_confirmation,
            pipeline_root=pipeline_root,
            permit_ready=True,
            merged_config=merged_config,
            gate_history_dir=resolved_gate_history,
            dry_run_history_dir=resolved_dry_run_history,
            now=now,
        )
    except ProductionActivationLiveHarnessError as exc:
        raise ProductionActivationLivePilotError(str(exc)) from exc

    plan = harness_result.plan
    failure_code = (
        FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C_2
        if harness_result.harness_ready
        else harness_result.failure_reason_code
    )
    recommended = (
        ACTION_CONTINUE_TO_PHASE_14H_3C_2
        if harness_result.harness_ready
        else harness_result.recommended_action
    )

    _append_preflight_event(
        event_type=_EVENT_EXECUTION_BLOCKED_WAITING,
        activation_request_id=normalized_activation,
        ticket_id=normalized_ticket,
        confirmation_id=normalized_confirmation,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        gate_event_id=reservation.execution_gate_event_id,
        dry_run_event_id=reservation.dry_run_event_id,
        result="blocked",
        failure_reason_code=failure_code,
        history_dir=preflight_history_dir,
        now=now,
    )

    return ProductionActivationLivePilotPreflightResult(
        activation_request_id=normalized_activation,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        state=RESERVATION_STATE_RESERVED,
        preflight_ready=harness_result.harness_ready,
        permit_ready=True,
        phrase_verified=True,
        execution_gate_verified=True,
        dry_run_verified=True,
        single_ticket_scope=assessment.single_ticket_scope,
        draft_only=assessment.draft_only,
        harness_ready=harness_result.harness_ready,
        runtime_invocation_planned=plan.runtime_invocation_planned,
        harness_request_valid=plan.request_valid,
        harness_reservation_valid=plan.reservation_valid,
        harness_permit_valid=plan.permit_valid,
        harness_active_valid=plan.active_valid,
        harness_gate_valid=plan.gate_valid,
        harness_mirror_valid=plan.mirror_valid,
        harness_runner_profile_valid=plan.runner_profile_valid,
        harness_argv_contract_valid=plan.argv_contract_valid,
        harness_cwd_contract_valid=plan.cwd_contract_valid,
        harness_env_contract_valid=plan.env_contract_valid,
        harness_timeout_valid=plan.timeout_valid,
        failure_reason_code=failure_code,
        recommended_action=recommended,
    )


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "repository2_execution_attempted: false",
        "production_execution_allowed: false",
        "execution_runtime_invoked: false",
        "runtime_invoked: false",
        "phrase_verified:",
        "execution_gate_verified:",
        "dry_run_verified:",
        "permit_ready:",
        "preflight_ready:",
        "harness_ready:",
        "runtime_invocation_planned:",
        "harness_request_valid:",
        "harness_reservation_valid:",
        "harness_permit_valid:",
        "harness_active_valid:",
        "harness_gate_valid:",
        "harness_mirror_valid:",
        "harness_runner_profile_valid:",
        "harness_argv_contract_valid:",
        "harness_cwd_contract_valid:",
        "harness_env_contract_valid:",
        "harness_timeout_valid:",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationLivePilotError(
                f"Unsafe live pilot output field: {token!r}"
            )


def format_live_pilot_preflight_result(
    result: ProductionActivationLivePilotPreflightResult,
) -> str:
    lines = [
        "Production Activation Live Pilot Preflight",
        "",
        f"activation_request_id: {result.activation_request_id}",
        f"reservation_id: {result.reservation_id or '(none)'}",
        f"execution_attempt_id: {result.execution_attempt_id or '(none)'}",
        f"state: {result.state}",
        f"preflight_ready: {str(result.preflight_ready).lower()}",
        f"permit_ready: {str(result.permit_ready).lower()}",
        f"phrase_verified: {str(result.phrase_verified).lower()}",
        f"execution_gate_verified: {str(result.execution_gate_verified).lower()}",
        f"dry_run_verified: {str(result.dry_run_verified).lower()}",
        f"single_ticket_scope: {str(result.single_ticket_scope).lower()}",
        f"draft_only: {str(result.draft_only).lower()}",
        f"publish_allowed: false",
        f"harness_ready: {str(result.harness_ready).lower()}",
        f"runtime_invocation_planned: {str(result.runtime_invocation_planned).lower()}",
        f"harness_request_valid: {str(result.harness_request_valid).lower()}",
        f"harness_reservation_valid: {str(result.harness_reservation_valid).lower()}",
        f"harness_permit_valid: {str(result.harness_permit_valid).lower()}",
        f"harness_active_valid: {str(result.harness_active_valid).lower()}",
        f"harness_gate_valid: {str(result.harness_gate_valid).lower()}",
        f"harness_mirror_valid: {str(result.harness_mirror_valid).lower()}",
        f"harness_runner_profile_valid: {str(result.harness_runner_profile_valid).lower()}",
        f"harness_argv_contract_valid: {str(result.harness_argv_contract_valid).lower()}",
        f"harness_cwd_contract_valid: {str(result.harness_cwd_contract_valid).lower()}",
        f"harness_env_contract_valid: {str(result.harness_env_contract_valid).lower()}",
        f"harness_timeout_valid: {str(result.harness_timeout_valid).lower()}",
        f"failure_reason_code: {result.failure_reason_code or '(none)'}",
        f"recommended_action: {result.recommended_action}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "repository2_execution_attempted: false",
        "execution_runtime_invoked: false",
        "runtime_invoked: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def run_activation_live_pilot(
    *,
    activation_request_id: str,
    ticket_id: str,
    confirmation_id: str,
    unlock_token_id: str,
    requester_id: str,
    pipeline_root: str,
    phrase: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    gate_history_dir: Path | None = None,
    dry_run_history_dir: Path | None = None,
    reservation_dir: Path | None = None,
    preflight_history_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    result = run_production_activation_live_pilot_preflight(
        activation_request_id=activation_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        unlock_token_id=unlock_token_id,
        requester_id=requester_id,
        pipeline_root=pipeline_root,
        phrase=phrase,
        repo_root=repo_root,
        store_dir=store_dir,
        gate_history_dir=gate_history_dir,
        dry_run_history_dir=dry_run_history_dir,
        reservation_dir=reservation_dir,
        preflight_history_dir=preflight_history_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        merged_config=merged_config,
        now=now,
    )
    exit_code = 1
    return format_live_pilot_preflight_result(result), exit_code


def load_preflight_records(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> list[ProductionActivationLivePilotPreflightRecord]:
    return _load_preflight_records(activation_request_id, history_dir=history_dir)
