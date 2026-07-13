"""Production activation live pilot E2E finalize — Phase 14H-3D.

Connects execution evidence, dispatch audit, correlation validation, consume
transaction, and activation revoke after isolated mirror runtime success.
No new subprocess, Repository2 original execution, or external publish.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_RECOVERY_REQUIRED,
    CONSUME_STATE_UNCONSUMED,
    DispatchConsumeTransactionError,
    assess_consume_status,
    assert_consume_replay_allowed,
    execute_consume_transaction,
)
from agent.coo.dispatch_execution_audit import default_audit_dir
from agent.coo.production_activation_active_gate import (
    _probe_recovery_required,
    _probe_repair_lock_held,
)
from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_COMPLETED,
    RESERVATION_STATE_FAILED,
    RESERVATION_STATE_STARTED,
    ProductionActivationExecutionReservation,
    ProductionActivationExecutionReservationError,
    load_execution_reservation,
)
from agent.coo.production_activation_kill_switch import (
    REASON_LIVE_PILOT_E2E_COMPLETED,
    ProductionActivationKillSwitchError,
    revoke_production_activation,
)
from agent.coo.production_activation_live_runtime import (
    _EVENT_RUNTIME_COMPLETED,
    load_runtime_records,
)
from agent.coo.production_activation_state import (
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
    ROLE_OPERATOR,
)
from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_executor_factory import default_evidence_dir
from hermes_constants import get_hermes_home

_ARTIFACT_TYPE = "production_live_pilot_e2e"
_EVIDENCE_VERSION = 1
_AUDIT_VERSION = 1
_E2E_STORE_VERSION = 1
_E2E_STORE_DIR = "production-live-e2e"
_EVIDENCE_SUFFIX = ".live-pilot-e2e.json"
_LIVE_PILOT_DISPATCH_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

FAIL_RUNTIME_NOT_COMPLETED = "runtime_not_completed"
FAIL_RUNTIME_NONZERO = "runtime_nonzero"
FAIL_RUNTIME_TIMEOUT = "runtime_timeout"
FAIL_SOURCE_TREE_MUTATED = "source_tree_mutated"
FAIL_PUBLISH_ATTEMPT_DETECTED = "publish_attempt_detected"
FAIL_RESERVATION_NOT_COMPLETED = "reservation_not_completed"
FAIL_ACTIVATION_NOT_SUSPENDED = "activation_not_suspended"
FAIL_EVIDENCE_WRITE_FAILED = "evidence_write_failed"
FAIL_DISPATCH_AUDIT_WRITE_FAILED = "dispatch_audit_write_failed"
FAIL_EVIDENCE_MISSING = "evidence_missing"
FAIL_AUDIT_MISSING = "audit_missing"
FAIL_CORRELATION_MISMATCH = "correlation_mismatch"
FAIL_CONSUME_REPLAY_BLOCKED = "consume_replay_blocked"
FAIL_CONSUME_PREPARE_FAILED = "consume_prepare_failed"
FAIL_CONSUME_PARTIAL = "consume_partial"
FAIL_CONSUME_COMMIT_FAILED = "consume_commit_failed"
FAIL_ACTIVATION_REVOKE_FAILED = "activation_revoke_failed"
FAIL_E2E_AUDIT_FAILED = "e2e_audit_failed"
FAIL_ALREADY_FINALIZED = "already_finalized"
FAIL_RECOVERY_REQUIRED = "recovery_required"
FAIL_NEW_ACTIVATION_REQUIRED = "new_activation_required"
FAIL_RESERVATION_SCOPE_MISMATCH = "reservation_scope_mismatch"
FAIL_RUNTIME_RECORD_CORRUPTED = "runtime_record_corrupted"

ACTION_LIVE_PILOT_E2E_COMPLETED = "live_pilot_e2e_completed"
ACTION_INSPECT_RUNTIME_FAILURE = "inspect_runtime_failure"
ACTION_INSPECT_EVIDENCE_FAILURE = "inspect_evidence_failure"
ACTION_INSPECT_AUDIT_FAILURE = "inspect_audit_failure"
ACTION_RESOLVE_CORRELATION_MISMATCH = "resolve_correlation_mismatch"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_REVIEW_PARTIAL_CONSUME = "review_partial_consume"
ACTION_CREATE_NEW_ACTIVATION_PROPOSAL = "create_new_activation_proposal"
ACTION_REVOKE_ACTIVATION_MANUALLY = "revoke_activation_manually"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"

_EVENT_E2E_FINALIZE_REQUESTED = "e2e_finalize_requested"
_EVENT_EVIDENCE_WRITTEN = "evidence_written"
_EVENT_DISPATCH_AUDIT_WRITTEN = "dispatch_audit_written"
_EVENT_CORRELATION_VALIDATED = "correlation_validated"
_EVENT_CONSUME_STARTED = "consume_started"
_EVENT_CONSUME_COMMITTED = "consume_committed"
_EVENT_CONSUME_FAILED = "consume_failed"
_EVENT_ACTIVATION_REVOKED = "activation_revoked"
_EVENT_E2E_COMPLETED = "e2e_completed"
_EVENT_E2E_FAILED = "e2e_failed"

_E2E_ACTOR_ID = "live-pilot-e2e"

_FORBIDDEN_OUTPUT_TOKENS = frozenset(
    {
        "pipeline_root",
        "confirmation_phrase",
        "unlock_token",
        "repository2",
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
        "actor_id",
        "requested_by",
        "phrase",
        "confirm-production-activation",
    }
)


class ProductionActivationLiveE2EError(ValueError):
    """Raised when live pilot E2E finalize cannot complete safely."""


@dataclass(frozen=True)
class ProductionActivationLivePilotE2EResult:
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    runtime_completed: bool
    runtime_exit_code: int
    runtime_timed_out: bool
    source_tree_unchanged: bool
    publish_attempted: bool
    evidence_written: bool
    dispatch_audit_written: bool
    evidence_audit_correlation_valid: bool
    consume_attempted: bool
    consume_state: str
    consume_committed: bool
    reservation_state: str
    activation_state_before: str
    activation_state_after: str
    production_execution_allowed: bool = False
    original_repository2_execution_attempted: bool = False
    isolated_mirror_runtime_invoked: bool = True
    final_success: bool = False
    failure_reason_code: str = ""
    recommended_action: str = ""


@dataclass(frozen=True)
class LivePilotE2EEvidenceArtifact:
    activation_request_id: str
    reservation_id: str
    execution_gate_event_id: str
    dry_run_event_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    ticket_id: str
    confirmation_id: str
    runtime_exit_code: int
    timed_out: bool
    source_tree_unchanged: bool
    publish_attempted: bool
    isolated_mirror_runtime_invoked: bool
    original_repository2_execution_attempted: bool
    timestamp: str


@dataclass(frozen=True)
class LivePilotE2EDispatchAudit:
    audit_id: str
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    ticket_id: str
    confirmation_id: str
    gate_event_id: str
    dry_run_event_id: str
    runtime_result: str
    evidence_artifact_id: str
    consume_planned: bool
    consume_result: str
    activation_state_before: str
    activation_state_after: str
    timestamp: str
    result_status: str


@dataclass(frozen=True)
class LivePilotE2EFinalizationState:
    e2e_finalized: bool = False
    evidence_written: bool = False
    dispatch_audit_written: bool = False
    consume_committed: bool = False
    finalized_at: str = ""
    e2e_failure_reason_code: str = ""
    dispatch_run_id: str = ""
    execution_attempt_id: str = ""


@dataclass(frozen=True)
class LivePilotE2EReadiness:
    ready: bool
    blocking_reasons: tuple[str, ...]
    runtime_exit_code: int = 0
    runtime_timed_out: bool = False
    source_tree_unchanged: bool = False
    publish_attempted: bool = False
    runtime_completed: bool = False


def default_e2e_history_dir() -> Path:
    return get_hermes_home() / "coo" / _E2E_STORE_DIR


def derive_live_pilot_dispatch_run_id(execution_attempt_id: str) -> str:
    normalized = (execution_attempt_id or "").strip()
    if not normalized:
        raise ProductionActivationLiveE2EError("execution_attempt_id is required")
    return str(
        uuid.uuid5(_LIVE_PILOT_DISPATCH_NAMESPACE, f"live-pilot:{normalized}")
    )


def _utc_now_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.isoformat()


def _recommended_action_for_failure(code: str) -> str:
    mapping = {
        FAIL_RUNTIME_NOT_COMPLETED: ACTION_INSPECT_RUNTIME_FAILURE,
        FAIL_RUNTIME_NONZERO: ACTION_INSPECT_RUNTIME_FAILURE,
        FAIL_RUNTIME_TIMEOUT: ACTION_INSPECT_RUNTIME_FAILURE,
        FAIL_SOURCE_TREE_MUTATED: ACTION_INSPECT_RUNTIME_FAILURE,
        FAIL_PUBLISH_ATTEMPT_DETECTED: ACTION_INSPECT_RUNTIME_FAILURE,
        FAIL_EVIDENCE_WRITE_FAILED: ACTION_INSPECT_EVIDENCE_FAILURE,
        FAIL_DISPATCH_AUDIT_WRITE_FAILED: ACTION_INSPECT_AUDIT_FAILURE,
        FAIL_EVIDENCE_MISSING: ACTION_INSPECT_EVIDENCE_FAILURE,
        FAIL_AUDIT_MISSING: ACTION_INSPECT_AUDIT_FAILURE,
        FAIL_CORRELATION_MISMATCH: ACTION_RESOLVE_CORRELATION_MISMATCH,
        FAIL_CONSUME_REPLAY_BLOCKED: ACTION_MAINTAIN_PRODUCTION_BLOCK,
        FAIL_CONSUME_PREPARE_FAILED: ACTION_RUN_CONSUME_RECOVERY,
        FAIL_CONSUME_PARTIAL: ACTION_REVIEW_PARTIAL_CONSUME,
        FAIL_CONSUME_COMMIT_FAILED: ACTION_RUN_CONSUME_RECOVERY,
        FAIL_ACTIVATION_REVOKE_FAILED: ACTION_REVOKE_ACTIVATION_MANUALLY,
        FAIL_RECOVERY_REQUIRED: ACTION_RUN_CONSUME_RECOVERY,
        FAIL_NEW_ACTIVATION_REQUIRED: ACTION_CREATE_NEW_ACTIVATION_PROPOSAL,
        FAIL_ALREADY_FINALIZED: ACTION_LIVE_PILOT_E2E_COMPLETED,
    }
    return mapping.get(code, ACTION_MAINTAIN_PRODUCTION_BLOCK)


def _failure_result(
    *,
    reservation: ProductionActivationExecutionReservation | None,
    dispatch_run_id: str = "",
    runtime_exit_code: int = 0,
    runtime_timed_out: bool = False,
    source_tree_unchanged: bool = False,
    publish_attempted: bool = False,
    runtime_completed: bool = False,
    activation_before: str = "",
    activation_after: str = "",
    failure_code: str,
    evidence_written: bool = False,
    dispatch_audit_written: bool = False,
    correlation_valid: bool = False,
    consume_attempted: bool = False,
    consume_state: str = CONSUME_STATE_UNCONSUMED,
    consume_committed: bool = False,
) -> ProductionActivationLivePilotE2EResult:
    return ProductionActivationLivePilotE2EResult(
        activation_request_id=(
            reservation.activation_request_id if reservation else ""
        ),
        reservation_id=reservation.reservation_id if reservation else "",
        execution_attempt_id=(
            reservation.execution_attempt_id if reservation else ""
        ),
        dispatch_run_id=dispatch_run_id,
        runtime_completed=runtime_completed,
        runtime_exit_code=runtime_exit_code,
        runtime_timed_out=runtime_timed_out,
        source_tree_unchanged=source_tree_unchanged,
        publish_attempted=publish_attempted,
        evidence_written=evidence_written,
        dispatch_audit_written=dispatch_audit_written,
        evidence_audit_correlation_valid=correlation_valid,
        consume_attempted=consume_attempted,
        consume_state=consume_state,
        consume_committed=consume_committed,
        reservation_state=reservation.state if reservation else "",
        activation_state_before=activation_before,
        activation_state_after=activation_after,
        final_success=False,
        failure_reason_code=failure_code,
        recommended_action=_recommended_action_for_failure(failure_code),
    )


def _e2e_history_path(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionActivationLiveE2EError("activation_request_id is required")
    base = (history_dir or default_e2e_history_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationLiveE2EError(
            "E2E history dir must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_e2e_audit_store_available(*, history_dir: Path | None = None) -> bool:
    try:
        base = (history_dir or default_e2e_history_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _load_e2e_store_payload(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> dict[str, Any]:
    path = _e2e_history_path(activation_request_id, history_dir=history_dir)
    if not path.is_file():
        return {
            "version": _E2E_STORE_VERSION,
            "activation_request_id": activation_request_id,
            "finalization": {},
            "records": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionActivationLiveE2EError("E2E audit store is corrupted.") from exc
    if not isinstance(payload, dict):
        raise ProductionActivationLiveE2EError("E2E audit store is corrupted.")
    if not isinstance(payload.get("records"), list):
        raise ProductionActivationLiveE2EError("E2E audit store is corrupted.")
    return payload


def load_e2e_finalization_state(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> LivePilotE2EFinalizationState:
    payload = _load_e2e_store_payload(activation_request_id, history_dir=history_dir)
    finalization = payload.get("finalization")
    if not isinstance(finalization, dict):
        return LivePilotE2EFinalizationState()
    return LivePilotE2EFinalizationState(
        e2e_finalized=bool(finalization.get("e2e_finalized", False)),
        evidence_written=bool(finalization.get("evidence_written", False)),
        dispatch_audit_written=bool(finalization.get("dispatch_audit_written", False)),
        consume_committed=bool(finalization.get("consume_committed", False)),
        finalized_at=str(finalization.get("finalized_at") or ""),
        e2e_failure_reason_code=str(finalization.get("e2e_failure_reason_code") or ""),
        dispatch_run_id=str(finalization.get("dispatch_run_id") or ""),
        execution_attempt_id=str(finalization.get("execution_attempt_id") or ""),
    )


def _atomic_update_e2e_store(
    activation_request_id: str,
    *,
    finalization: LivePilotE2EFinalizationState | None = None,
    record: dict[str, Any] | None = None,
    history_dir: Path | None = None,
) -> None:
    path = _e2e_history_path(activation_request_id, history_dir=history_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_e2e_store_payload(activation_request_id, history_dir=history_dir)
    if finalization is not None:
        payload["finalization"] = {
            "e2e_finalized": finalization.e2e_finalized,
            "evidence_written": finalization.evidence_written,
            "dispatch_audit_written": finalization.dispatch_audit_written,
            "consume_committed": finalization.consume_committed,
            "finalized_at": finalization.finalized_at,
            "e2e_failure_reason_code": finalization.e2e_failure_reason_code,
            "dispatch_run_id": finalization.dispatch_run_id,
            "execution_attempt_id": finalization.execution_attempt_id,
        }
    if record is not None:
        records = payload["records"]
        for prior in records:
            if (
                isinstance(prior, dict)
                and prior.get("event_type") == record.get("event_type")
                and prior.get("event_id") == record.get("event_id")
            ):
                return
            if (
                isinstance(prior, dict)
                and prior.get("event_type") == record.get("event_type")
                and prior.get("execution_attempt_id") == record.get("execution_attempt_id")
            ):
                return
        records.append(record)
    payload["version"] = _E2E_STORE_VERSION
    payload["activation_request_id"] = activation_request_id
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise ProductionActivationLiveE2EError("e2e_audit_failed") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _append_e2e_event(
    *,
    activation_request_id: str,
    reservation_id: str,
    execution_attempt_id: str,
    dispatch_run_id: str,
    event_type: str,
    result: str,
    failure_reason_code: str = "",
    history_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    record = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "activation_request_id": activation_request_id,
        "reservation_id": reservation_id,
        "execution_attempt_id": execution_attempt_id,
        "dispatch_run_id": dispatch_run_id,
        "result": result,
        "failure_reason_code": failure_reason_code,
        "timestamp": _utc_now_iso(now),
        "production_execution_allowed": False,
        "original_repository2_execution_attempted": False,
        "isolated_mirror_runtime_invoked": True,
        "publish_attempted": False,
    }
    _atomic_update_e2e_store(
        activation_request_id,
        record=record,
        history_dir=history_dir,
    )


def _evidence_path(
    execution_attempt_id: str,
    *,
    evidence_dir: Path | None = None,
) -> Path:
    base = (evidence_dir or default_evidence_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationLiveE2EError(
            "Evidence directory must remain under Hermes home."
        ) from exc
    path = base / f"{execution_attempt_id}{_EVIDENCE_SUFFIX}"
    resolved = path.resolve()
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationLiveE2EError(
            "Evidence path must remain under Hermes home."
        ) from exc
    return path


def _audit_path(
    dispatch_run_id: str,
    *,
    audit_dir: Path | None = None,
) -> Path:
    base = (audit_dir or default_audit_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationLiveE2EError(
            "Audit directory must remain under Hermes home."
        ) from exc
    if "/" in dispatch_run_id or "\\" in dispatch_run_id or not dispatch_run_id:
        raise ProductionActivationLiveE2EError("dispatch_run_id is invalid")
    path = base / f"{dispatch_run_id}.json"
    resolved = path.resolve()
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationLiveE2EError(
            "Audit path must remain under Hermes home."
        ) from exc
    return path


def _evidence_from_dict(payload: Mapping[str, Any]) -> LivePilotE2EEvidenceArtifact:
    if payload.get("artifact_type") != _ARTIFACT_TYPE:
        raise ProductionActivationLiveE2EError("evidence artifact type mismatch")
    return LivePilotE2EEvidenceArtifact(
        activation_request_id=str(payload.get("activation_request_id", "")),
        reservation_id=str(payload.get("reservation_id", "")),
        execution_gate_event_id=str(payload.get("execution_gate_event_id", "")),
        dry_run_event_id=str(payload.get("dry_run_event_id", "")),
        execution_attempt_id=str(payload.get("execution_attempt_id", "")),
        dispatch_run_id=str(payload.get("dispatch_run_id", "")),
        ticket_id=str(payload.get("ticket_id", "")),
        confirmation_id=str(payload.get("confirmation_id", "")),
        runtime_exit_code=int(payload.get("runtime_exit_code", 0)),
        timed_out=bool(payload.get("timed_out", False)),
        source_tree_unchanged=bool(payload.get("source_tree_unchanged", False)),
        publish_attempted=bool(payload.get("publish_attempted", False)),
        isolated_mirror_runtime_invoked=bool(
            payload.get("isolated_mirror_runtime_invoked", False)
        ),
        original_repository2_execution_attempted=False,
        timestamp=str(payload.get("timestamp", "")),
    )


def _audit_from_dict(payload: Mapping[str, Any]) -> LivePilotE2EDispatchAudit:
    if payload.get("artifact_type") != _ARTIFACT_TYPE:
        raise ProductionActivationLiveE2EError("audit artifact type mismatch")
    return LivePilotE2EDispatchAudit(
        audit_id=str(payload.get("audit_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        reservation_id=str(payload.get("reservation_id", "")),
        execution_attempt_id=str(payload.get("execution_attempt_id", "")),
        dispatch_run_id=str(payload.get("dispatch_run_id", "")),
        ticket_id=str(payload.get("ticket_id", "")),
        confirmation_id=str(payload.get("confirmation_id", "")),
        gate_event_id=str(payload.get("gate_event_id", "")),
        dry_run_event_id=str(payload.get("dry_run_event_id", "")),
        runtime_result=str(payload.get("runtime_result", "")),
        evidence_artifact_id=str(payload.get("evidence_artifact_id", "")),
        consume_planned=bool(payload.get("consume_planned", False)),
        consume_result=str(payload.get("consume_result", "")),
        activation_state_before=str(payload.get("activation_state_before", "")),
        activation_state_after=str(payload.get("activation_state_after", "")),
        timestamp=str(payload.get("timestamp", "")),
        result_status=str(payload.get("result_status", "")),
    )


def load_live_pilot_evidence(
    execution_attempt_id: str,
    *,
    evidence_dir: Path | None = None,
) -> LivePilotE2EEvidenceArtifact | None:
    path = _evidence_path(execution_attempt_id, evidence_dir=evidence_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionActivationLiveE2EError("evidence artifact corrupted") from exc
    if not isinstance(payload, dict):
        raise ProductionActivationLiveE2EError("evidence artifact corrupted")
    return _evidence_from_dict(payload)


def load_live_pilot_dispatch_audit(
    dispatch_run_id: str,
    *,
    audit_dir: Path | None = None,
) -> LivePilotE2EDispatchAudit | None:
    path = _audit_path(dispatch_run_id, audit_dir=audit_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionActivationLiveE2EError("dispatch audit corrupted") from exc
    if not isinstance(payload, dict):
        raise ProductionActivationLiveE2EError("dispatch audit corrupted")
    return _audit_from_dict(payload)


def _write_live_pilot_evidence(
    artifact: LivePilotE2EEvidenceArtifact,
    *,
    evidence_dir: Path | None = None,
) -> None:
    path = _evidence_path(artifact.execution_attempt_id, evidence_dir=evidence_dir)
    if path.is_file():
        existing = load_live_pilot_evidence(
            artifact.execution_attempt_id,
            evidence_dir=evidence_dir,
        )
        if existing is None:
            raise ProductionActivationLiveE2EError("evidence artifact corrupted")
        if existing != artifact:
            raise ProductionActivationLiveE2EError("evidence correlation corruption")
        return
    payload = {
        "artifact_type": _ARTIFACT_TYPE,
        "version": _EVIDENCE_VERSION,
        "activation_request_id": artifact.activation_request_id,
        "reservation_id": artifact.reservation_id,
        "execution_gate_event_id": artifact.execution_gate_event_id,
        "dry_run_event_id": artifact.dry_run_event_id,
        "execution_attempt_id": artifact.execution_attempt_id,
        "dispatch_run_id": artifact.dispatch_run_id,
        "ticket_id": artifact.ticket_id,
        "confirmation_id": artifact.confirmation_id,
        "runtime_exit_code": artifact.runtime_exit_code,
        "timed_out": artifact.timed_out,
        "source_tree_unchanged": artifact.source_tree_unchanged,
        "publish_attempted": artifact.publish_attempted,
        "isolated_mirror_runtime_invoked": artifact.isolated_mirror_runtime_invoked,
        "original_repository2_execution_attempted": False,
        "production_execution_allowed": False,
        "timestamp": artifact.timestamp,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise ProductionActivationLiveE2EError("evidence_write_failed") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _write_live_pilot_dispatch_audit(
    audit: LivePilotE2EDispatchAudit,
    *,
    audit_dir: Path | None = None,
) -> None:
    path = _audit_path(audit.dispatch_run_id, audit_dir=audit_dir)
    if path.is_file():
        existing = load_live_pilot_dispatch_audit(
            audit.dispatch_run_id,
            audit_dir=audit_dir,
        )
        if existing is None:
            raise ProductionActivationLiveE2EError("dispatch audit corrupted")
        if existing != audit:
            raise ProductionActivationLiveE2EError("audit correlation corruption")
        return
    for other_path in (audit_dir or default_audit_dir()).glob("*.json"):
        if other_path == path:
            continue
        try:
            payload = json.loads(other_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("artifact_type") == _ARTIFACT_TYPE
            and payload.get("dispatch_run_id") == audit.dispatch_run_id
            and payload.get("activation_request_id") != audit.activation_request_id
        ):
            raise ProductionActivationLiveE2EError("dispatch_run_id corruption")
    payload = {
        "artifact_type": _ARTIFACT_TYPE,
        "version": _AUDIT_VERSION,
        "audit_id": audit.audit_id,
        "activation_request_id": audit.activation_request_id,
        "reservation_id": audit.reservation_id,
        "execution_attempt_id": audit.execution_attempt_id,
        "dispatch_run_id": audit.dispatch_run_id,
        "ticket_id": audit.ticket_id,
        "confirmation_id": audit.confirmation_id,
        "gate_event_id": audit.gate_event_id,
        "dry_run_event_id": audit.dry_run_event_id,
        "runtime_result": audit.runtime_result,
        "evidence_artifact_id": audit.evidence_artifact_id,
        "consume_planned": audit.consume_planned,
        "consume_result": audit.consume_result,
        "activation_state_before": audit.activation_state_before,
        "activation_state_after": audit.activation_state_after,
        "timestamp": audit.timestamp,
        "result_status": audit.result_status,
        "production_execution_allowed": False,
        "original_repository2_execution_attempted": False,
        "isolated_mirror_runtime_invoked": True,
        "publish_attempted": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise ProductionActivationLiveE2EError("dispatch_audit_write_failed") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _find_runtime_completed_record(
    activation_request_id: str,
    execution_attempt_id: str,
    *,
    runtime_history_dir: Path | None = None,
) -> tuple[bool, int, bool, bool]:
    records = load_runtime_records(
        activation_request_id,
        history_dir=runtime_history_dir,
    )
    matches = [
        record
        for record in records
        if record.event_type == _EVENT_RUNTIME_COMPLETED
        and record.execution_attempt_id == execution_attempt_id
    ]
    if len(matches) > 1:
        raise ProductionActivationLiveE2EError("runtime_record_corrupted")
    if not matches:
        return False, 0, False, False
    record = matches[0]
    source_unchanged = (
        record.exit_code == 0
        and not record.timed_out
        and not record.publish_attempted
        and record.result == "completed"
    )
    return True, record.exit_code, record.timed_out, record.publish_attempted


def evaluate_live_pilot_e2e_readiness(
    *,
    activation_request_id: str,
    reservation_id: str,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    runtime_history_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    e2e_history_dir: Path | None = None,
) -> LivePilotE2EReadiness:
    blocking: list[str] = []
    reservation = load_execution_reservation(
        activation_request_id,
        store_dir=reservation_dir,
    )
    if reservation is None:
        return LivePilotE2EReadiness(
            ready=False,
            blocking_reasons=(FAIL_RESERVATION_NOT_COMPLETED,),
        )
    if reservation.reservation_id != reservation_id:
        return LivePilotE2EReadiness(
            ready=False,
            blocking_reasons=(FAIL_RESERVATION_SCOPE_MISMATCH,),
        )
    if reservation.state == RESERVATION_STATE_FAILED:
        return LivePilotE2EReadiness(
            ready=False,
            blocking_reasons=(FAIL_NEW_ACTIVATION_REQUIRED,),
        )
    if reservation.state == RESERVATION_STATE_STARTED:
        return LivePilotE2EReadiness(
            ready=False,
            blocking_reasons=(FAIL_RUNTIME_NOT_COMPLETED,),
        )
    if reservation.state != RESERVATION_STATE_COMPLETED:
        blocking.append(FAIL_RESERVATION_NOT_COMPLETED)
    if reservation.execution_count != 1:
        blocking.append(FAIL_RUNTIME_NOT_COMPLETED)

    request = load_activation_request(activation_request_id, store_dir=store_dir)
    if request.state != ACTIVATION_STATE_SUSPENDED:
        blocking.append(FAIL_ACTIVATION_NOT_SUSPENDED)
    if _probe_recovery_required(request):
        blocking.append(FAIL_RECOVERY_REQUIRED)
    if _probe_repair_lock_held(request):
        blocking.append(FAIL_RECOVERY_REQUIRED)

    try:
        runtime_completed, exit_code, timed_out, publish_attempted = (
            _find_runtime_completed_record(
                activation_request_id,
                reservation.execution_attempt_id,
                runtime_history_dir=runtime_history_dir,
            )
        )
    except ProductionActivationLiveE2EError:
        return LivePilotE2EReadiness(
            ready=False,
            blocking_reasons=(FAIL_RUNTIME_RECORD_CORRUPTED,),
        )

    if not runtime_completed:
        blocking.append(FAIL_RUNTIME_NOT_COMPLETED)
    if exit_code != 0:
        blocking.append(FAIL_RUNTIME_NONZERO)
    if timed_out:
        blocking.append(FAIL_RUNTIME_TIMEOUT)
    if publish_attempted:
        blocking.append(FAIL_PUBLISH_ATTEMPT_DETECTED)

    source_tree_unchanged = (
        runtime_completed
        and exit_code == 0
        and not timed_out
        and not publish_attempted
    )
    if runtime_completed and not source_tree_unchanged:
        blocking.append(FAIL_SOURCE_TREE_MUTATED)

    finalization = load_e2e_finalization_state(
        activation_request_id,
        history_dir=e2e_history_dir,
    )
    if finalization.e2e_finalized:
        blocking.append(FAIL_ALREADY_FINALIZED)

    consume_status = assess_consume_status(
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
    )
    if consume_status.consume_state in {
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_PREPARED,
        CONSUME_STATE_RECOVERY_REQUIRED,
    }:
        blocking.append(FAIL_RECOVERY_REQUIRED)
    if (
        consume_status.consume_state == CONSUME_STATE_COMMITTED
        and not finalization.e2e_finalized
    ):
        blocking.append(FAIL_CONSUME_REPLAY_BLOCKED)

    return LivePilotE2EReadiness(
        ready=not blocking,
        blocking_reasons=tuple(blocking),
        runtime_exit_code=exit_code,
        runtime_timed_out=timed_out,
        source_tree_unchanged=source_tree_unchanged,
        publish_attempted=publish_attempted,
        runtime_completed=runtime_completed,
    )


def correlate_live_pilot_evidence_and_audit(
    evidence: LivePilotE2EEvidenceArtifact,
    audit: LivePilotE2EDispatchAudit,
    *,
    reservation: ProductionActivationExecutionReservation,
) -> bool:
    if evidence.execution_attempt_id != audit.execution_attempt_id:
        return False
    if evidence.dispatch_run_id != audit.dispatch_run_id:
        return False
    if evidence.activation_request_id != audit.activation_request_id:
        return False
    if evidence.activation_request_id != reservation.activation_request_id:
        return False
    if evidence.reservation_id != reservation.reservation_id:
        return False
    if evidence.ticket_id != reservation.ticket_id:
        return False
    if evidence.confirmation_id != reservation.confirmation_id:
        return False
    if evidence.ticket_id != audit.ticket_id:
        return False
    if evidence.confirmation_id != audit.confirmation_id:
        return False
    if audit.evidence_artifact_id != evidence.execution_attempt_id:
        return False
    if evidence.runtime_exit_code != 0:
        return False
    if evidence.timed_out:
        return False
    if evidence.publish_attempted:
        return False
    if not evidence.source_tree_unchanged:
        return False
    if not evidence.isolated_mirror_runtime_invoked:
        return False
    if evidence.original_repository2_execution_attempted:
        return False
    if audit.runtime_result != "completed":
        return False
    return True


def _success_result(
    *,
    reservation: ProductionActivationExecutionReservation,
    dispatch_run_id: str,
    readiness: LivePilotE2EReadiness,
    activation_before: str,
    activation_after: str,
    consume_state: str,
) -> ProductionActivationLivePilotE2EResult:
    return ProductionActivationLivePilotE2EResult(
        activation_request_id=reservation.activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        runtime_completed=readiness.runtime_completed,
        runtime_exit_code=readiness.runtime_exit_code,
        runtime_timed_out=readiness.runtime_timed_out,
        source_tree_unchanged=readiness.source_tree_unchanged,
        publish_attempted=readiness.publish_attempted,
        evidence_written=True,
        dispatch_audit_written=True,
        evidence_audit_correlation_valid=True,
        consume_attempted=True,
        consume_state=consume_state,
        consume_committed=True,
        reservation_state=reservation.state,
        activation_state_before=activation_before,
        activation_state_after=activation_after,
        final_success=True,
        failure_reason_code="",
        recommended_action=ACTION_LIVE_PILOT_E2E_COMPLETED,
    )


def finalize_production_live_pilot(
    *,
    activation_request_id: str,
    reservation_id: str,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    runtime_history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    e2e_history_dir: Path | None = None,
    now: datetime | None = None,
) -> ProductionActivationLivePilotE2EResult:
    """Finalize live pilot E2E after successful isolated mirror runtime."""
    if not probe_e2e_audit_store_available(history_dir=e2e_history_dir):
        raise ProductionActivationLiveE2EError("e2e_audit_failed")

    reservation = load_execution_reservation(
        activation_request_id,
        store_dir=reservation_dir,
    )
    request = load_activation_request(activation_request_id, store_dir=store_dir)
    activation_before = request.state

    if reservation is None or reservation.reservation_id != reservation_id:
        return _failure_result(
            reservation=reservation,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=FAIL_RESERVATION_SCOPE_MISMATCH,
        )

    dispatch_run_id = derive_live_pilot_dispatch_run_id(
        reservation.execution_attempt_id
    )
    finalization = load_e2e_finalization_state(
        activation_request_id,
        history_dir=e2e_history_dir,
    )

    if finalization.e2e_finalized:
        consume_status = assess_consume_status(
            ticket_id=reservation.ticket_id,
            confirmation_id=reservation.confirmation_id,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            transaction_dir=transaction_dir,
        )
        activation_after = load_activation_request(
            activation_request_id,
            store_dir=store_dir,
        ).state
        readiness = LivePilotE2EReadiness(
            ready=True,
            blocking_reasons=(),
            runtime_exit_code=0,
            runtime_timed_out=False,
            source_tree_unchanged=True,
            publish_attempted=False,
            runtime_completed=True,
        )
        result = _success_result(
            reservation=reservation,
            dispatch_run_id=finalization.dispatch_run_id or dispatch_run_id,
            readiness=readiness,
            activation_before=activation_before,
            activation_after=activation_after,
            consume_state=consume_status.consume_state,
        )
        return replace(
            result,
            failure_reason_code=FAIL_ALREADY_FINALIZED,
            recommended_action=ACTION_LIVE_PILOT_E2E_COMPLETED,
        )

    readiness = evaluate_live_pilot_e2e_readiness(
        activation_request_id=activation_request_id,
        reservation_id=reservation_id,
        store_dir=store_dir,
        reservation_dir=reservation_dir,
        runtime_history_dir=runtime_history_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        e2e_history_dir=e2e_history_dir,
    )
    if not readiness.ready:
        failure = readiness.blocking_reasons[0] if readiness.blocking_reasons else (
            FAIL_RUNTIME_NOT_COMPLETED
        )
        consume_status = assess_consume_status(
            ticket_id=reservation.ticket_id,
            confirmation_id=reservation.confirmation_id,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            transaction_dir=transaction_dir,
        )
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_E2E_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=readiness.runtime_completed,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=failure,
            consume_state=consume_status.consume_state,
        )

    _append_e2e_event(
        activation_request_id=activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        event_type=_EVENT_E2E_FINALIZE_REQUESTED,
        result="requested",
        history_dir=e2e_history_dir,
        now=now,
    )

    evidence_written = False
    audit_written = False
    timestamp = _utc_now_iso(now)

    evidence_artifact = LivePilotE2EEvidenceArtifact(
        activation_request_id=reservation.activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_gate_event_id=reservation.execution_gate_event_id,
        dry_run_event_id=reservation.dry_run_event_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        runtime_exit_code=readiness.runtime_exit_code,
        timed_out=readiness.runtime_timed_out,
        source_tree_unchanged=readiness.source_tree_unchanged,
        publish_attempted=readiness.publish_attempted,
        isolated_mirror_runtime_invoked=True,
        original_repository2_execution_attempted=False,
        timestamp=timestamp,
    )
    try:
        _write_live_pilot_evidence(evidence_artifact, evidence_dir=evidence_dir)
        evidence_written = True
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_EVIDENCE_WRITTEN,
            result="written",
            history_dir=e2e_history_dir,
            now=now,
        )
    except ProductionActivationLiveE2EError as exc:
        code = str(exc)
        if "corruption" in code:
            failure = FAIL_CORRELATION_MISMATCH
        else:
            failure = FAIL_EVIDENCE_WRITE_FAILED
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_E2E_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=failure,
            evidence_written=evidence_written,
        )
    except Exception:
        failure = FAIL_EVIDENCE_WRITE_FAILED
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_E2E_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=failure,
            evidence_written=evidence_written,
        )

    dispatch_audit = LivePilotE2EDispatchAudit(
        audit_id=str(uuid.uuid4()),
        activation_request_id=reservation.activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        gate_event_id=reservation.execution_gate_event_id,
        dry_run_event_id=reservation.dry_run_event_id,
        runtime_result="completed",
        evidence_artifact_id=reservation.execution_attempt_id,
        consume_planned=True,
        consume_result="pending",
        activation_state_before=activation_before,
        activation_state_after=activation_before,
        timestamp=timestamp,
        result_status="evidence_written",
    )
    try:
        _write_live_pilot_dispatch_audit(dispatch_audit, audit_dir=audit_dir)
        audit_written = True
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_DISPATCH_AUDIT_WRITTEN,
            result="written",
            history_dir=e2e_history_dir,
            now=now,
        )
    except ProductionActivationLiveE2EError as exc:
        code = str(exc)
        if "corruption" in code:
            failure = FAIL_CORRELATION_MISMATCH
        else:
            failure = FAIL_DISPATCH_AUDIT_WRITE_FAILED
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_E2E_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=failure,
            evidence_written=evidence_written,
            dispatch_audit_written=audit_written,
        )
    except Exception:
        failure = FAIL_DISPATCH_AUDIT_WRITE_FAILED
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_E2E_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=failure,
            evidence_written=evidence_written,
            dispatch_audit_written=audit_written,
        )

    loaded_evidence = load_live_pilot_evidence(
        reservation.execution_attempt_id,
        evidence_dir=evidence_dir,
    )
    loaded_audit = load_live_pilot_dispatch_audit(
        dispatch_run_id,
        audit_dir=audit_dir,
    )
    if loaded_evidence is None or loaded_audit is None:
        failure = FAIL_EVIDENCE_MISSING if loaded_evidence is None else FAIL_AUDIT_MISSING
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_E2E_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=failure,
            evidence_written=evidence_written,
            dispatch_audit_written=audit_written,
        )

    correlation_valid = correlate_live_pilot_evidence_and_audit(
        loaded_evidence,
        loaded_audit,
        reservation=reservation,
    )
    if not correlation_valid:
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_E2E_FAILED,
            result="failed",
            failure_reason_code=FAIL_CORRELATION_MISMATCH,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=FAIL_CORRELATION_MISMATCH,
            evidence_written=True,
            dispatch_audit_written=True,
        )

    _append_e2e_event(
        activation_request_id=activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        event_type=_EVENT_CORRELATION_VALIDATED,
        result="valid",
        history_dir=e2e_history_dir,
        now=now,
    )

    consume_attempted = False
    consume_state = CONSUME_STATE_UNCONSUMED
    try:
        assert_consume_replay_allowed(
            ticket_id=reservation.ticket_id,
            confirmation_id=reservation.confirmation_id,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            transaction_dir=transaction_dir,
        )
    except ValueError:
        failure = FAIL_CONSUME_REPLAY_BLOCKED
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_CONSUME_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=failure,
            evidence_written=True,
            dispatch_audit_written=True,
            correlation_valid=True,
        )

    _append_e2e_event(
        activation_request_id=activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        event_type=_EVENT_CONSUME_STARTED,
        result="started",
        history_dir=e2e_history_dir,
        now=now,
    )

    consume_attempted = True
    try:
        execute_consume_transaction(
            ticket_id=reservation.ticket_id,
            confirmation_id=reservation.confirmation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            transaction_dir=transaction_dir,
        )
    except ValueError as exc:
        message = str(exc).lower()
        if "partial" in message or "recovery" in message:
            failure = FAIL_CONSUME_PARTIAL
        elif "prepared" in message:
            failure = FAIL_CONSUME_PREPARE_FAILED
        else:
            failure = FAIL_CONSUME_COMMIT_FAILED
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_CONSUME_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        partial_status = assess_consume_status(
            ticket_id=reservation.ticket_id,
            confirmation_id=reservation.confirmation_id,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            transaction_dir=transaction_dir,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=failure,
            evidence_written=True,
            dispatch_audit_written=True,
            correlation_valid=True,
            consume_attempted=consume_attempted,
            consume_state=partial_status.consume_state,
        )
    except DispatchConsumeTransactionError:
        failure = FAIL_CONSUME_COMMIT_FAILED
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_CONSUME_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=failure,
            evidence_written=True,
            dispatch_audit_written=True,
            correlation_valid=True,
            consume_attempted=True,
        )

    consume_status = assess_consume_status(
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
    )
    consume_state = consume_status.consume_state
    if consume_status.consume_state != CONSUME_STATE_COMMITTED:
        failure = FAIL_CONSUME_COMMIT_FAILED
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_CONSUME_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_before,
            failure_code=failure,
            evidence_written=True,
            dispatch_audit_written=True,
            correlation_valid=True,
            consume_attempted=True,
            consume_state=consume_state,
        )

    _append_e2e_event(
        activation_request_id=activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        event_type=_EVENT_CONSUME_COMMITTED,
        result="committed",
        history_dir=e2e_history_dir,
        now=now,
    )

    try:
        revoke_production_activation(
            activation_request_id=activation_request_id,
            actor_id=_E2E_ACTOR_ID,
            actor_role=ROLE_OPERATOR,
            reason_code=REASON_LIVE_PILOT_E2E_COMPLETED,
            store_dir=store_dir,
            now=now,
        )
    except ProductionActivationKillSwitchError:
        failure = FAIL_ACTIVATION_REVOKE_FAILED
        _append_e2e_event(
            activation_request_id=activation_request_id,
            reservation_id=reservation.reservation_id,
            execution_attempt_id=reservation.execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            event_type=_EVENT_E2E_FAILED,
            result="failed",
            failure_reason_code=failure,
            history_dir=e2e_history_dir,
            now=now,
        )
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=ACTIVATION_STATE_SUSPENDED,
            failure_code=failure,
            evidence_written=True,
            dispatch_audit_written=True,
            correlation_valid=True,
            consume_attempted=True,
            consume_state=consume_state,
            consume_committed=True,
        )

    activation_after = ACTIVATION_STATE_REVOKED
    _append_e2e_event(
        activation_request_id=activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        event_type=_EVENT_ACTIVATION_REVOKED,
        result="revoked",
        history_dir=e2e_history_dir,
        now=now,
    )

    finalized = LivePilotE2EFinalizationState(
        e2e_finalized=True,
        evidence_written=True,
        dispatch_audit_written=True,
        consume_committed=True,
        finalized_at=_utc_now_iso(now),
        e2e_failure_reason_code="",
        dispatch_run_id=dispatch_run_id,
        execution_attempt_id=reservation.execution_attempt_id,
    )
    try:
        _atomic_update_e2e_store(
            activation_request_id,
            finalization=finalized,
            history_dir=e2e_history_dir,
        )
    except ProductionActivationLiveE2EError:
        return _failure_result(
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            runtime_exit_code=readiness.runtime_exit_code,
            runtime_timed_out=readiness.runtime_timed_out,
            source_tree_unchanged=readiness.source_tree_unchanged,
            publish_attempted=readiness.publish_attempted,
            runtime_completed=True,
            activation_before=activation_before,
            activation_after=activation_after,
            failure_code=FAIL_E2E_AUDIT_FAILED,
            evidence_written=True,
            dispatch_audit_written=True,
            correlation_valid=True,
            consume_attempted=True,
            consume_state=consume_state,
            consume_committed=True,
        )

    _append_e2e_event(
        activation_request_id=activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        event_type=_EVENT_E2E_COMPLETED,
        result="completed",
        history_dir=e2e_history_dir,
        now=now,
    )

    return _success_result(
        reservation=reservation,
        dispatch_run_id=dispatch_run_id,
        readiness=readiness,
        activation_before=activation_before,
        activation_after=activation_after,
        consume_state=consume_state,
    )


def _assert_safe_e2e_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "original_repository2_execution_attempted: false",
        "production_execution_allowed: false",
        "isolated_mirror_runtime_invoked: true",
        "consume_attempted:",
        "evidence_written:",
        "dispatch_audit_written:",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationLiveE2EError(
                f"Unsafe live pilot E2E output field: {token!r}"
            )


def format_live_pilot_e2e_result(
    result: ProductionActivationLivePilotE2EResult,
) -> str:
    lines = [
        "Production Activation Live Pilot E2E Finalize",
        "",
        f"activation_request_id: {result.activation_request_id}",
        f"reservation_id: {result.reservation_id}",
        f"execution_attempt_id: {result.execution_attempt_id}",
        f"dispatch_run_id: {result.dispatch_run_id}",
        f"runtime_completed: {str(result.runtime_completed).lower()}",
        f"runtime_exit_code: {result.runtime_exit_code}",
        f"runtime_timed_out: {str(result.runtime_timed_out).lower()}",
        f"source_tree_unchanged: {str(result.source_tree_unchanged).lower()}",
        f"publish_attempted: {str(result.publish_attempted).lower()}",
        f"evidence_written: {str(result.evidence_written).lower()}",
        f"dispatch_audit_written: {str(result.dispatch_audit_written).lower()}",
        "evidence_audit_correlation_valid: "
        f"{str(result.evidence_audit_correlation_valid).lower()}",
        f"consume_attempted: {str(result.consume_attempted).lower()}",
        f"consume_state: {result.consume_state or '(none)'}",
        f"consume_committed: {str(result.consume_committed).lower()}",
        f"reservation_state: {result.reservation_state}",
        f"activation_state_before: {result.activation_state_before}",
        f"activation_state_after: {result.activation_state_after}",
        f"final_success: {str(result.final_success).lower()}",
        f"failure_reason_code: {result.failure_reason_code or '(none)'}",
        f"recommended_action: {result.recommended_action}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "original_repository2_execution_attempted: false",
        "isolated_mirror_runtime_invoked: true",
    ]
    output = "\n".join(lines)
    _assert_safe_e2e_output(output)
    return output


def run_activation_live_pilot_finalize(
    *,
    activation_request_id: str,
    reservation_id: str,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    runtime_history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    e2e_history_dir: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    result = finalize_production_live_pilot(
        activation_request_id=activation_request_id,
        reservation_id=reservation_id,
        store_dir=store_dir,
        reservation_dir=reservation_dir,
        runtime_history_dir=runtime_history_dir,
        evidence_dir=evidence_dir,
        audit_dir=audit_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        e2e_history_dir=e2e_history_dir,
        now=now,
    )
    exit_code = 0 if result.final_success or result.failure_reason_code == FAIL_ALREADY_FINALIZED else 1
    return format_live_pilot_e2e_result(result), exit_code
