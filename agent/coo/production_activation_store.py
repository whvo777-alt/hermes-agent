"""Production activation proposal store — Phase 14C.

Append-only JSON artifacts under Hermes home. No subprocess or Repository2 access.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home

from agent.coo.production_activation_state import (
    ACTIVATION_STATE_PROPOSED,
    ActivationApprovalRecord,
    ActivationControlEvent,
    ActivationRequest,
    ActivationScope,
    ActivationStateTransition,
    ProductionActivationStateError,
    validate_activation_request,
)

_ACTIVATION_STORE_VERSION = 1
_STORE_DIR_NAME = "production-activation"

_FORBIDDEN_RECORD_KEYS = frozenset(
    {
        "pipeline_root",
        "confirmation_phrase",
        "unlock_token",
        "unlock_token_id",
        "argv",
        "cwd",
        "env",
        "stdout",
        "stderr",
        "secret",
        "operator_reason",
        "repository2",
        "filesystem",
        "phrase",
    }
)


class ProductionActivationStoreError(ValueError):
    """Raised when activation store operations fail."""


def default_production_activation_dir() -> Path:
    return get_hermes_home() / "coo" / _STORE_DIR_NAME


def _assert_path_within_hermes_home(
    resolved: Path,
    hermes_root: Path,
    *,
    label: str,
) -> None:
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationStoreError(
            f"Activation store {label} must remain under Hermes home."
        ) from exc


def _normalize_activation_request_id(activation_request_id: str) -> str:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionActivationStoreError("activation_request_id is required")
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise ProductionActivationStoreError(
            "activation_request_id must not contain path separators"
        )
    return normalized


def activation_request_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = _normalize_activation_request_id(activation_request_id)
    base_dir = (store_dir or default_production_activation_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    _assert_path_within_hermes_home(base_dir, hermes_root, label="directory")
    if base_dir.is_symlink():
        raise ProductionActivationStoreError(
            "Activation store directory must not be a symlink."
        )
    path = (base_dir / f"{normalized}.json").resolve()
    _assert_path_within_hermes_home(path, hermes_root, label="path")
    if path.is_symlink():
        raise ProductionActivationStoreError("Activation store path must not be a symlink.")
    return path


def _validate_safe_record_payload(payload: Mapping[str, Any]) -> None:
    for key in payload:
        if key in _FORBIDDEN_RECORD_KEYS:
            raise ProductionActivationStoreError(
                f"Activation record must not include {key!r}."
            )


def _scope_from_dict(payload: Mapping[str, Any]) -> ActivationScope:
    return ActivationScope(
        scope_type=str(payload.get("scope_type", "")),
        platform=str(payload.get("platform", "")),
        publish_allowed=bool(payload.get("publish_allowed", False)),
        ticket_id=str(payload.get("ticket_id", "")),
        maintenance_window_start=str(payload.get("maintenance_window_start", "")),
        maintenance_window_end=str(payload.get("maintenance_window_end", "")),
    )


def _transition_from_dict(payload: Mapping[str, Any]) -> ActivationStateTransition:
    return ActivationStateTransition(
        from_state=str(payload.get("from_state", "")),
        to_state=str(payload.get("to_state", "")),
        actor=str(payload.get("actor", "")),
        role=str(payload.get("role", "")),
        timestamp=str(payload.get("timestamp", "")),
        reason_code=str(payload.get("reason_code", "")),
    )


def _approval_from_dict(payload: Mapping[str, Any]) -> ActivationApprovalRecord:
    return ActivationApprovalRecord(
        approver_id=str(payload.get("approver_id", "")),
        role=str(payload.get("role", "")),
        timestamp=str(payload.get("timestamp", "")),
        approval_id=str(payload.get("approval_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        decision=str(payload.get("decision", "approved")),
        reason_code=str(payload.get("reason_code", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
    )


def _control_from_dict(payload: Mapping[str, Any]) -> ActivationControlEvent:
    return ActivationControlEvent(
        event_id=str(payload.get("event_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        event_type=str(payload.get("event_type", "")),
        from_state=str(payload.get("from_state", "")),
        to_state=str(payload.get("to_state", "")),
        actor_id=str(payload.get("actor_id", "")),
        actor_role=str(payload.get("actor_role", "")),
        reason_code=str(payload.get("reason_code", "")),
        timestamp=str(payload.get("timestamp", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        dry_run_event_id=str(payload.get("dry_run_event_id", "")),
    )


def activation_request_to_dict(request: ActivationRequest) -> dict[str, Any]:
    validated = validate_activation_request(request)
    scope = validated.activation_scope
    payload: dict[str, Any] = {
        "version": _ACTIVATION_STORE_VERSION,
        "activation_request_id": validated.activation_request_id,
        "tested_commit_sha": validated.tested_commit_sha,
        "release_tag": validated.release_tag,
        "repository_attestation_hash": validated.repository_attestation_hash,
        "requested_by": validated.requested_by,
        "approved_by": list(validated.approved_by),
        "security_reviewed_by": validated.security_reviewed_by,
        "activation_scope": scope.to_dict(),
        "rollback_commit": validated.rollback_commit,
        "state": validated.state,
        "created_at": validated.created_at,
        "updated_at": validated.updated_at,
        "state_history": [
            {
                "from_state": item.from_state,
                "to_state": item.to_state,
                "actor": item.actor,
                "role": item.role,
                "timestamp": item.timestamp,
                "reason_code": item.reason_code,
            }
            for item in validated.state_history
        ],
        "approval_history": [
            {
                "approver_id": item.approver_id,
                "role": item.role,
                "timestamp": item.timestamp,
                "approval_id": item.approval_id,
                "activation_request_id": item.activation_request_id,
                "decision": item.decision,
                "reason_code": item.reason_code,
                "tested_commit_sha": item.tested_commit_sha,
                "release_tag": item.release_tag,
            }
            for item in validated.approval_history
        ],
        "expires_at": validated.expires_at,
        "armed_expires_at": validated.armed_expires_at,
        "active_expires_at": validated.active_expires_at,
        "executor_id": validated.executor_id,
        "phrase_verified": validated.phrase_verified,
        "armed_at": validated.armed_at,
        "disarmed_at": validated.disarmed_at,
        "disarm_reason_code": validated.disarm_reason_code,
        "active_at": validated.active_at,
        "active_actor_id": validated.active_actor_id,
        "dry_run_event_id": validated.dry_run_event_id,
        "dry_run_key": validated.dry_run_key,
        "control_history": [
            {
                "event_id": item.event_id,
                "activation_request_id": item.activation_request_id,
                "event_type": item.event_type,
                "from_state": item.from_state,
                "to_state": item.to_state,
                "actor_id": item.actor_id,
                "actor_role": item.actor_role,
                "reason_code": item.reason_code,
                "timestamp": item.timestamp,
                "tested_commit_sha": item.tested_commit_sha,
                "release_tag": item.release_tag,
                "dry_run_event_id": item.dry_run_event_id,
            }
            for item in validated.control_history
        ],
        "production_execution_allowed": False,
    }
    _validate_safe_record_payload(payload)
    return payload


def activation_request_from_dict(payload: Mapping[str, Any]) -> ActivationRequest:
    if not isinstance(payload, Mapping):
        raise ProductionActivationStoreError("Activation record must be a mapping.")
    _validate_safe_record_payload(payload)
    scope_payload = payload.get("activation_scope")
    if not isinstance(scope_payload, Mapping):
        raise ProductionActivationStoreError("activation_scope must be a mapping.")

    history_payload = payload.get("state_history", [])
    if not isinstance(history_payload, list):
        raise ProductionActivationStoreError("state_history must be a list.")
    approval_payload = payload.get("approval_history", [])
    if not isinstance(approval_payload, list):
        raise ProductionActivationStoreError("approval_history must be a list.")

    approved_by = payload.get("approved_by", [])
    if not isinstance(approved_by, list):
        raise ProductionActivationStoreError("approved_by must be a list.")

    control_payload = payload.get("control_history", [])
    if not isinstance(control_payload, list):
        raise ProductionActivationStoreError("control_history must be a list.")

    request = ActivationRequest(
        activation_request_id=str(payload.get("activation_request_id", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        repository_attestation_hash=str(payload.get("repository_attestation_hash", "")),
        requested_by=str(payload.get("requested_by", "")),
        approved_by=tuple(str(item) for item in approved_by),
        security_reviewed_by=str(payload.get("security_reviewed_by", "")),
        activation_scope=_scope_from_dict(scope_payload),
        rollback_commit=str(payload.get("rollback_commit", "")),
        state=str(payload.get("state", "")),
        created_at=str(payload.get("created_at", "")),
        updated_at=str(payload.get("updated_at", "")),
        state_history=tuple(
            _transition_from_dict(item)
            for item in history_payload
            if isinstance(item, Mapping)
        ),
        approval_history=tuple(
            _approval_from_dict(item)
            for item in approval_payload
            if isinstance(item, Mapping)
        ),
        expires_at=str(payload.get("expires_at", "")),
        armed_expires_at=str(payload.get("armed_expires_at", "")),
        active_expires_at=str(payload.get("active_expires_at", "")),
        executor_id=str(payload.get("executor_id", "")),
        phrase_verified=bool(payload.get("phrase_verified", False)),
        armed_at=str(payload.get("armed_at", "")),
        disarmed_at=str(payload.get("disarmed_at", "")),
        disarm_reason_code=str(payload.get("disarm_reason_code", "")),
        active_at=str(payload.get("active_at", "")),
        active_actor_id=str(payload.get("active_actor_id", "")),
        dry_run_event_id=str(payload.get("dry_run_event_id", "")),
        dry_run_key=str(payload.get("dry_run_key", "")),
        control_history=tuple(
            _control_from_dict(item)
            for item in control_payload
            if isinstance(item, Mapping)
        ),
    )
    return validate_activation_request(request)


def _atomic_create_json(path: Path, payload: Mapping[str, Any]) -> None:
    hermes_root = get_hermes_home().resolve()
    resolved_path = path.resolve()
    _assert_path_within_hermes_home(resolved_path, hermes_root, label="path")
    if resolved_path.exists():
        raise ProductionActivationStoreError(
            "Activation proposal already exists; overwrite is not allowed."
        )
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = resolved_path.with_name(f".{resolved_path.name}.{uuid.uuid4().hex}.tmp")
    _assert_path_within_hermes_home(tmp_path.resolve(), hermes_root, label="temp file")
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, resolved_path)


def _atomic_replace_json(path: Path, payload: Mapping[str, Any]) -> None:
    hermes_root = get_hermes_home().resolve()
    resolved_path = path.resolve()
    _assert_path_within_hermes_home(resolved_path, hermes_root, label="path")
    if not resolved_path.is_file():
        raise ProductionActivationStoreError(
            "Activation artifact does not exist; create-only writes are required."
        )
    tmp_path = resolved_path.with_name(f".{resolved_path.name}.{uuid.uuid4().hex}.tmp")
    _assert_path_within_hermes_home(tmp_path.resolve(), hermes_root, label="temp file")
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, resolved_path)


def save_activation_request(
    request: ActivationRequest,
    *,
    store_dir: Path | None = None,
) -> ActivationRequest:
    """Atomically replace one existing activation artifact."""
    validated = validate_activation_request(request)
    path = activation_request_path(
        validated.activation_request_id,
        store_dir=store_dir or default_production_activation_dir(),
    )
    payload = activation_request_to_dict(validated)
    try:
        _atomic_replace_json(path, payload)
    except OSError as exc:
        raise ProductionActivationStoreError(
            "Activation artifact update failed."
        ) from exc
    return validated


def list_activation_request_ids(*, store_dir: Path | None = None) -> tuple[str, ...]:
    base_dir = (store_dir or default_production_activation_dir()).resolve()
    if not base_dir.exists():
        return ()
    ids: list[str] = []
    for path in sorted(base_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        ids.append(path.stem)
    return tuple(ids)


def load_activation_request(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ActivationRequest:
    path = activation_request_path(
        activation_request_id,
        store_dir=store_dir,
    )
    if not path.is_file():
        raise ProductionActivationStoreError(
            f"Activation proposal not found: {activation_request_id}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProductionActivationStoreError(
            "Activation proposal record is corrupted."
        ) from exc
    return activation_request_from_dict(payload)


def find_open_proposed_activation_id(
    *,
    store_dir: Path | None = None,
) -> str | None:
    base_dir = store_dir or default_production_activation_dir()
    for activation_id in list_activation_request_ids(store_dir=base_dir):
        try:
            request = load_activation_request(activation_id, store_dir=base_dir)
        except (ProductionActivationStoreError, ProductionActivationStateError):
            continue
        if request.state == ACTIVATION_STATE_PROPOSED:
            return activation_id
    return None


def append_activation_proposal(
    request: ActivationRequest,
    *,
    store_dir: Path | None = None,
) -> ActivationRequest:
    """Persist one new proposed activation artifact; fail closed on overwrite."""
    validated = validate_activation_request(request)
    if validated.state != ACTIVATION_STATE_PROPOSED:
        raise ProductionActivationStoreError(
            "append_activation_proposal only supports proposed state"
        )

    base_dir = store_dir or default_production_activation_dir()
    existing_open = find_open_proposed_activation_id(store_dir=base_dir)
    if existing_open is not None:
        raise ProductionActivationStoreError(
            "An open proposed activation already exists; "
            "only one proposal may be open at a time."
        )

    path = activation_request_path(
        validated.activation_request_id,
        store_dir=base_dir,
    )
    payload = activation_request_to_dict(validated)
    try:
        _atomic_create_json(path, payload)
    except OSError as exc:
        raise ProductionActivationStoreError(
            "Activation proposal write failed."
        ) from exc
    return validated
