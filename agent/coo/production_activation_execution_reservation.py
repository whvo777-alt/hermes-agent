"""Production activation execution reservation — Phase 14H-3B.

One-shot reservation artifact and atomic store for live pilot preflight.
No subprocess, runner execution, or Repository2 access.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
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
    BLOCK_ACTIVATION_NOT_ACTIVE,
    BLOCK_ACTIVE_EXPIRED,
    BLOCK_BINDING_NOT_BOUND,
    BLOCK_BOUNDED_RUNNER_CONTRACT_MISSING,
    BLOCK_BUNDLE_CONSUMED,
    BLOCK_CONFIRMATION_CONSUMED,
    BLOCK_CONFIRMATION_SCOPE_MISMATCH,
    BLOCK_CUTOVER_NOT_READY,
    BLOCK_DRY_RUN_CORRELATION_MISMATCH,
    BLOCK_DRY_RUN_MISSING,
    BLOCK_DRY_RUN_NOT_READY,
    BLOCK_DRY_RUN_STALE,
    BLOCK_KILL_SWITCH_UNAVAILABLE,
    BLOCK_MIRROR_ROOT_NOT_TRUSTED,
    BLOCK_PRODUCTION_ROOT_DENIED,
    BLOCK_PUBLISH_NOT_ALLOWED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_REGRESSION_BLOCKED,
    BLOCK_REPAIR_LOCK_HELD,
    BLOCK_SIGNOFF_NOT_READY,
    BLOCK_TICKET_SCOPE_MISMATCH,
    ProductionActivationExecutionGateError,
    ProductionActivationExecutionGateRecord,
    _load_execution_gate_records,
    default_execution_gate_history_dir,
    evaluate_production_execution_gate,
    find_ready_execution_gate_record,
)
from agent.coo.production_activation_state import ActivationRequest
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    load_activation_request,
)
from agent.coo.production_activation_state import ProductionActivationStateError
from hermes_constants import get_hermes_home

RESERVATION_STATE_RESERVED = "reserved"
RESERVATION_STATE_STARTED = "started"
RESERVATION_STATE_COMPLETED = "completed"
RESERVATION_STATE_FAILED = "failed"

_KNOWN_RESERVATION_STATES = frozenset(
    {
        RESERVATION_STATE_RESERVED,
        RESERVATION_STATE_STARTED,
        RESERVATION_STATE_COMPLETED,
        RESERVATION_STATE_FAILED,
    }
)

_RESERVATION_STORE_DIR = "production-activation-execution-reservation"
_RESERVATION_STORE_VERSION = 1
_MAX_EXECUTIONS = 1


class ProductionActivationExecutionReservationError(ValueError):
    """Raised when reservation operations cannot complete safely."""


@dataclass(frozen=True)
class ProductionActivationExecutionReservation:
    """One-shot live pilot execution reservation artifact."""

    reservation_id: str
    activation_request_id: str
    ticket_id: str
    confirmation_id: str
    execution_attempt_id: str
    execution_gate_event_id: str
    dry_run_event_id: str
    state: str
    reserved_at: str
    started_at: str = ""
    completed_at: str = ""
    failed_at: str = ""
    max_executions: int = _MAX_EXECUTIONS
    execution_count: int = 0
    failure_reason_code: str = ""
    tested_commit_sha: str = ""
    release_tag: str = ""
    gate_key: str = ""
    production_execution_allowed: bool = False
    repository2_execution_attempted: bool = False


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def default_reservation_store_dir() -> Path:
    return get_hermes_home() / "coo" / _RESERVATION_STORE_DIR


def _reservation_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionActivationExecutionReservationError(
            "activation_request_id is required"
        )
    base = (store_dir or default_reservation_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationExecutionReservationError(
            "Reservation store directory must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def _reservation_from_dict(payload: Mapping[str, Any]) -> ProductionActivationExecutionReservation:
    state = str(payload.get("state", ""))
    if state not in _KNOWN_RESERVATION_STATES:
        raise ProductionActivationExecutionReservationError(
            f"Unsupported reservation state: {state!r}"
        )
    return ProductionActivationExecutionReservation(
        reservation_id=str(payload.get("reservation_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        ticket_id=str(payload.get("ticket_id", "")),
        confirmation_id=str(payload.get("confirmation_id", "")),
        execution_attempt_id=str(payload.get("execution_attempt_id", "")),
        execution_gate_event_id=str(payload.get("execution_gate_event_id", "")),
        dry_run_event_id=str(payload.get("dry_run_event_id", "")),
        state=state,
        reserved_at=str(payload.get("reserved_at", "")),
        started_at=str(payload.get("started_at") or ""),
        completed_at=str(payload.get("completed_at") or ""),
        failed_at=str(payload.get("failed_at") or ""),
        max_executions=int(payload.get("max_executions", _MAX_EXECUTIONS)),
        execution_count=int(payload.get("execution_count", 0)),
        failure_reason_code=str(payload.get("failure_reason_code") or ""),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        gate_key=str(payload.get("gate_key", "")),
        production_execution_allowed=False,
        repository2_execution_attempted=False,
    )


def _reservation_to_dict(
    reservation: ProductionActivationExecutionReservation,
) -> dict[str, Any]:
    return {
        "reservation_id": reservation.reservation_id,
        "activation_request_id": reservation.activation_request_id,
        "ticket_id": reservation.ticket_id,
        "confirmation_id": reservation.confirmation_id,
        "execution_attempt_id": reservation.execution_attempt_id,
        "execution_gate_event_id": reservation.execution_gate_event_id,
        "dry_run_event_id": reservation.dry_run_event_id,
        "state": reservation.state,
        "reserved_at": reservation.reserved_at,
        "started_at": reservation.started_at,
        "completed_at": reservation.completed_at,
        "failed_at": reservation.failed_at,
        "max_executions": reservation.max_executions,
        "execution_count": reservation.execution_count,
        "failure_reason_code": reservation.failure_reason_code,
        "tested_commit_sha": reservation.tested_commit_sha,
        "release_tag": reservation.release_tag,
        "gate_key": reservation.gate_key,
        "production_execution_allowed": False,
        "repository2_execution_attempted": False,
    }


def load_execution_reservation(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionActivationExecutionReservation | None:
    path = _reservation_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProductionActivationExecutionReservationError(
            "Reservation artifact is corrupted."
        ) from exc
    if not isinstance(payload, dict):
        raise ProductionActivationExecutionReservationError(
            "Reservation artifact must be a JSON object."
        )
    reservation_payload = payload.get("reservation")
    if not isinstance(reservation_payload, dict):
        raise ProductionActivationExecutionReservationError(
            "Reservation artifact missing reservation object."
        )
    return _reservation_from_dict(reservation_payload)


def _atomic_create_reservation(
    reservation: ProductionActivationExecutionReservation,
    *,
    store_dir: Path | None = None,
) -> None:
    path = _reservation_path(
        reservation.activation_request_id,
        store_dir=store_dir,
    )
    if path.exists():
        raise ProductionActivationExecutionReservationError(
            "Reservation artifact already exists."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _RESERVATION_STORE_VERSION,
        "reservation": _reservation_to_dict(reservation),
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
        raise ProductionActivationExecutionReservationError(
            "Reservation write failed."
        ) from exc


def _load_activation_or_fail(
    activation_request_id: str,
    *,
    store_dir: Path | None,
) -> ActivationRequest:
    try:
        return load_activation_request(activation_request_id, store_dir=store_dir)
    except ProductionActivationStoreError as exc:
        raise ProductionActivationExecutionReservationError(str(exc)) from exc
    except ProductionActivationStateError as exc:
        raise ProductionActivationExecutionReservationError(str(exc)) from exc


def _map_gate_blocking_to_failure(code: str) -> str:
    mapping = {
        BLOCK_ACTIVATION_NOT_ACTIVE: "activation_not_active",
        BLOCK_ACTIVE_EXPIRED: "active_expired",
        BLOCK_TICKET_SCOPE_MISMATCH: "ticket_scope_mismatch",
        BLOCK_CONFIRMATION_SCOPE_MISMATCH: "confirmation_scope_mismatch",
        BLOCK_CONFIRMATION_CONSUMED: "confirmation_consumed",
        BLOCK_BUNDLE_CONSUMED: "bundle_consumed",
        BLOCK_MIRROR_ROOT_NOT_TRUSTED: "mirror_root_not_trusted",
        BLOCK_PRODUCTION_ROOT_DENIED: "production_root_denied",
        BLOCK_PUBLISH_NOT_ALLOWED: "publish_not_allowed",
        BLOCK_BINDING_NOT_BOUND: "binding_not_bound",
        BLOCK_BOUNDED_RUNNER_CONTRACT_MISSING: "runner_contract_missing",
        BLOCK_RECOVERY_REQUIRED: "recovery_required",
        BLOCK_REPAIR_LOCK_HELD: "repair_lock_held",
        BLOCK_REGRESSION_BLOCKED: "resolve_regression_failure",
        BLOCK_SIGNOFF_NOT_READY: "signoff_not_ready",
        BLOCK_CUTOVER_NOT_READY: "cutover_not_ready",
        BLOCK_KILL_SWITCH_UNAVAILABLE: "kill_switch_unavailable",
        BLOCK_DRY_RUN_MISSING: "dry_run_not_ready",
        BLOCK_DRY_RUN_NOT_READY: "dry_run_not_ready",
        BLOCK_DRY_RUN_STALE: "dry_run_correlation_mismatch",
        BLOCK_DRY_RUN_CORRELATION_MISMATCH: "dry_run_correlation_mismatch",
    }
    return mapping.get(code, "execution_gate_not_ready")


def _reservation_scope_matches(
    existing: ProductionActivationExecutionReservation,
    *,
    ticket_id: str,
    confirmation_id: str,
    gate_key: str,
) -> bool:
    return (
        existing.ticket_id == ticket_id
        and existing.confirmation_id == confirmation_id
        and existing.gate_key == gate_key
    )


def _idempotent_failure_for_state(state: str) -> str:
    if state == RESERVATION_STATE_RESERVED:
        return "reservation_in_progress"
    if state == RESERVATION_STATE_STARTED:
        return "execution_in_progress"
    if state == RESERVATION_STATE_COMPLETED:
        return "activation_execution_already_completed"
    if state == RESERVATION_STATE_FAILED:
        return "activation_execution_requires_new_proposal"
    return "reservation_scope_conflict"


def validate_unlock_token_correlation(
    *,
    ticket_id: str,
    confirmation_id: str,
    unlock_token_id: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> None:
    from agent.coo.dispatch_bundle_store import read_bundle
    from agent.coo.production_executor_confirmation import read_confirmation

    normalized_token = (unlock_token_id or "").strip()
    if not normalized_token:
        raise ProductionActivationExecutionReservationError(
            "unlock_token_id is required"
        )
    try:
        bundle = read_bundle(
            ticket_id,
            bundle_dir=bundle_dir,
            reject_consumed=True,
        )
        confirmation = read_confirmation(
            confirmation_id,
            confirmation_dir=confirmation_dir,
            reject_consumed=True,
        )
    except KeyError as exc:
        raise ProductionActivationExecutionReservationError(
            "bundle or confirmation missing for unlock token validation"
        ) from exc
    except ValueError as exc:
        message = str(exc).lower()
        if "consumed" in message:
            raise ProductionActivationExecutionReservationError(
                "bundle or confirmation consumed"
            ) from exc
        raise ProductionActivationExecutionReservationError(str(exc)) from exc
    if bundle.unlock_token_id != normalized_token:
        raise ProductionActivationExecutionReservationError(
            "unlock token does not match bundle"
        )
    if confirmation.unlock_token_id != normalized_token:
        raise ProductionActivationExecutionReservationError(
            "unlock token does not match confirmation"
        )


def create_execution_reservation(
    request: ActivationRequest,
    *,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root_resolved: str,
    gate_record: ProductionActivationExecutionGateRecord,
    dry_run_event_id: str,
    gate_key: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ProductionActivationExecutionReservation:
    """Atomically create a reserved one-shot reservation artifact."""
    existing = load_execution_reservation(
        request.activation_request_id,
        store_dir=store_dir,
    )
    if existing is not None:
        if not _reservation_scope_matches(
            existing,
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            gate_key=gate_key,
        ):
            raise ProductionActivationExecutionReservationError(
                "reservation_scope_conflict"
            )
        raise ProductionActivationExecutionReservationError(
            _idempotent_failure_for_state(existing.state)
        )

    reservation = ProductionActivationExecutionReservation(
        reservation_id=str(uuid.uuid4()),
        activation_request_id=request.activation_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        execution_attempt_id=str(uuid.uuid4()),
        execution_gate_event_id=gate_record.event_id,
        dry_run_event_id=dry_run_event_id,
        state=RESERVATION_STATE_RESERVED,
        reserved_at=_utc_now_iso(now),
        max_executions=_MAX_EXECUTIONS,
        execution_count=0,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        gate_key=gate_key,
        production_execution_allowed=False,
        repository2_execution_attempted=False,
    )
    try:
        _atomic_create_reservation(reservation, store_dir=store_dir)
    except ProductionActivationExecutionReservationError:
        raise
    except Exception as exc:
        raise ProductionActivationExecutionReservationError(
            "reservation_write_failed"
        ) from exc
    return reservation


def probe_reservation_store_available(*, store_dir: Path | None = None) -> bool:
    base = (store_dir or default_reservation_store_dir()).resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _atomic_update_reservation(
    reservation: ProductionActivationExecutionReservation,
    *,
    store_dir: Path | None = None,
) -> None:
    path = _reservation_path(
        reservation.activation_request_id,
        store_dir=store_dir,
    )
    if not path.is_file():
        raise ProductionActivationExecutionReservationError("reservation_missing")
    payload = {
        "version": _RESERVATION_STORE_VERSION,
        "reservation": _reservation_to_dict(reservation),
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
        raise ProductionActivationExecutionReservationError(
            "reservation_write_failed"
        ) from exc


def transition_execution_reservation_to_started(
    reservation: ProductionActivationExecutionReservation,
    *,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ProductionActivationExecutionReservation:
    """Atomically transition reserved → started before runtime invocation."""
    if reservation.state != RESERVATION_STATE_RESERVED:
        raise ProductionActivationExecutionReservationError(
            "reservation_not_reserved"
        )
    if reservation.max_executions != _MAX_EXECUTIONS or reservation.execution_count != 0:
        raise ProductionActivationExecutionReservationError("reservation_start_failed")
    updated = ProductionActivationExecutionReservation(
        reservation_id=reservation.reservation_id,
        activation_request_id=reservation.activation_request_id,
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        execution_gate_event_id=reservation.execution_gate_event_id,
        dry_run_event_id=reservation.dry_run_event_id,
        state=RESERVATION_STATE_STARTED,
        reserved_at=reservation.reserved_at,
        started_at=_utc_now_iso(now),
        completed_at="",
        failed_at="",
        max_executions=reservation.max_executions,
        execution_count=1,
        failure_reason_code="",
        tested_commit_sha=reservation.tested_commit_sha,
        release_tag=reservation.release_tag,
        gate_key=reservation.gate_key,
        production_execution_allowed=False,
        repository2_execution_attempted=False,
    )
    _atomic_update_reservation(updated, store_dir=store_dir)
    return updated


def transition_execution_reservation_to_completed(
    reservation: ProductionActivationExecutionReservation,
    *,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ProductionActivationExecutionReservation:
    """Atomically transition started → completed after successful runtime."""
    if reservation.state != RESERVATION_STATE_STARTED:
        raise ProductionActivationExecutionReservationError(
            "reservation_completion_failed"
        )
    updated = ProductionActivationExecutionReservation(
        reservation_id=reservation.reservation_id,
        activation_request_id=reservation.activation_request_id,
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        execution_gate_event_id=reservation.execution_gate_event_id,
        dry_run_event_id=reservation.dry_run_event_id,
        state=RESERVATION_STATE_COMPLETED,
        reserved_at=reservation.reserved_at,
        started_at=reservation.started_at,
        completed_at=_utc_now_iso(now),
        failed_at="",
        max_executions=reservation.max_executions,
        execution_count=reservation.execution_count,
        failure_reason_code="",
        tested_commit_sha=reservation.tested_commit_sha,
        release_tag=reservation.release_tag,
        gate_key=reservation.gate_key,
        production_execution_allowed=False,
        repository2_execution_attempted=False,
    )
    _atomic_update_reservation(updated, store_dir=store_dir)
    return updated


def transition_execution_reservation_to_failed(
    reservation: ProductionActivationExecutionReservation,
    *,
    failure_reason_code: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ProductionActivationExecutionReservation:
    """Atomically transition started → failed after runtime failure."""
    if reservation.state != RESERVATION_STATE_STARTED:
        raise ProductionActivationExecutionReservationError(
            "reservation_failure_write_failed"
        )
    updated = ProductionActivationExecutionReservation(
        reservation_id=reservation.reservation_id,
        activation_request_id=reservation.activation_request_id,
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        execution_gate_event_id=reservation.execution_gate_event_id,
        dry_run_event_id=reservation.dry_run_event_id,
        state=RESERVATION_STATE_FAILED,
        reserved_at=reservation.reserved_at,
        started_at=reservation.started_at,
        completed_at="",
        failed_at=_utc_now_iso(now),
        max_executions=reservation.max_executions,
        execution_count=reservation.execution_count,
        failure_reason_code=(failure_reason_code or "").strip(),
        tested_commit_sha=reservation.tested_commit_sha,
        release_tag=reservation.release_tag,
        gate_key=reservation.gate_key,
        production_execution_allowed=False,
        repository2_execution_attempted=False,
    )
    _atomic_update_reservation(updated, store_dir=store_dir)
    return updated
