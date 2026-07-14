"""Production runtime permission contract — Phase 15C.

One-shot append-only permission prerequisite bound to an open controlled window.
Does not invoke runtime, start cutover, or grant production execution.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import fcntl

from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_COMPLETED,
    load_execution_reservation,
)
from agent.coo.production_activation_state import ACTIVATION_STATE_REVOKED
from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_controlled_window import (
    EVENT_WINDOW_OPENED,
    WINDOW_CLOSED,
    WINDOW_EMERGENCY_CLOSED,
    WINDOW_EXPIRED,
    WINDOW_OPEN,
    evaluate_production_controlled_window,
    load_window_lifecycle_events,
)
from agent.coo.production_final_signoff import (
    PRODUCTION_FINAL_SIGNOFF_READY,
    PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
    load_final_signoff_record,
)
from agent.coo.production_governed_cutover import (
    CONTRACT_STATUS_PREPARED,
    GOVERNED_CUTOVER_CONTRACT_PREPARED,
    default_governed_cutover_store_dir,
    evaluate_production_governed_cutover,
    load_governed_cutover_contract,
)
from agent.coo.production_live_operational_signoff import (
    load_operational_signoff_record,
)
from hermes_constants import get_hermes_home

_PERMISSION_STORE_DIR = "production-runtime-permission"
_PERMISSION_STORE_VERSION = 1
_NEXT_PHASE_15D = "Phase_15D_governed_runtime_session"

PERMISSION_NOT_ISSUED = "PERMISSION_NOT_ISSUED"
PERMISSION_READY = "PERMISSION_READY"
PERMISSION_ISSUED = "PERMISSION_ISSUED"
PERMISSION_EXPIRED = "PERMISSION_EXPIRED"
PERMISSION_CONSUMED = "PERMISSION_CONSUMED"
PERMISSION_REVOKED = "PERMISSION_REVOKED"
PERMISSION_BLOCKED = "PERMISSION_BLOCKED"

SCOPE_TYPE_ONE_SHOT = "one_shot"
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 900

EVENT_PERMISSION_ISSUE_REQUESTED = "permission_issue_requested"
EVENT_PERMISSION_ISSUED = "permission_issued"
EVENT_PERMISSION_ISSUE_BLOCKED = "permission_issue_blocked"

RELEASE_RUNTIME_PERMISSION_READY_TO_ISSUE = "RUNTIME_PERMISSION_READY_TO_ISSUE"
RELEASE_RUNTIME_PERMISSION_ISSUED = "RUNTIME_PERMISSION_ISSUED"
RELEASE_RUNTIME_PERMISSION_EXPIRED = "RUNTIME_PERMISSION_EXPIRED"
RELEASE_RUNTIME_PERMISSION_NOT_READY = "RUNTIME_PERMISSION_NOT_READY"
RELEASE_RUNTIME_PERMISSION_RECOVERY_REQUIRED = "RUNTIME_PERMISSION_RECOVERY_REQUIRED"

ACTION_ISSUE_PRODUCTION_RUNTIME_PERMISSION = "issue_production_runtime_permission"
ACTION_PRODUCTION_RUNTIME_PERMISSION_ISSUED = "production_runtime_permission_issued"
ACTION_REVIEW_PERMISSION_WARNINGS = "review_permission_warnings"
ACTION_WAIT_FOR_WINDOW_OPEN = "wait_for_window_open"
ACTION_CLOSE_EXPIRED_WINDOW = "close_expired_window"
ACTION_RESOLVE_IDENTITY_SEPARATION = "resolve_identity_separation"
ACTION_RESOLVE_RUNTIME_PERMISSION_SCOPE = "resolve_runtime_permission_scope"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_RESOLVE_FINAL_SIGNOFF = "resolve_final_signoff"
ACTION_RESOLVE_ROLLBACK_VALIDATION = "resolve_rollback_validation"
ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT = "prepare_new_governed_cutover_contract"
ACTION_REVOKE_OR_CLOSE_WINDOW = "revoke_or_close_window"
ACTION_PREPARE_PHASE_15D_GOVERNED_RUNTIME_SESSION = (
    "prepare_phase_15d_governed_runtime_session"
)
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"

BLOCK_GOVERNED_CUTOVER_CONTRACT_MISSING = "governed_cutover_contract_missing"
BLOCK_GOVERNED_CUTOVER_CONTRACT_INVALID = "governed_cutover_contract_invalid"
BLOCK_GOVERNED_CUTOVER_NOT_PREPARED = "governed_cutover_not_prepared"
BLOCK_CONTROLLED_WINDOW_NOT_OPEN = "controlled_window_not_open"
BLOCK_CONTROLLED_WINDOW_CLOSED = "controlled_window_closed"
BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED = "controlled_window_emergency_closed"
BLOCK_CONTROLLED_WINDOW_EXPIRED = "controlled_window_expired"
BLOCK_WINDOW_TIME_OUTSIDE_SCOPE = "window_time_outside_scope"
BLOCK_FINAL_SIGNOFF_INVALID = "final_signoff_invalid"
BLOCK_ROLLBACK_VALIDATION_INVALID = "rollback_validation_invalid"
BLOCK_OPERATIONAL_SIGNOFF_INVALID = "operational_signoff_invalid"
BLOCK_ACTIVATION_NOT_REVOKED = "activation_not_revoked"
BLOCK_RESERVATION_NOT_COMPLETED = "reservation_not_completed"
BLOCK_CONSUME_NOT_COMMITTED = "consume_not_committed"
BLOCK_E2E_NOT_FINALIZED = "e2e_not_finalized"
BLOCK_AUDIT_CHAIN_INCOMPLETE = "audit_chain_incomplete"
BLOCK_RECOVERY_REQUIRED = "recovery_required"
BLOCK_REPAIR_LOCK_HELD = "repair_lock_held"
BLOCK_PRODUCTION_ROOT_TOUCHED = "production_root_touched"
BLOCK_SOURCE_TREE_MUTATED = "source_tree_mutated"
BLOCK_EXTERNAL_PUBLISH_ENABLED = "external_publish_enabled"
BLOCK_GATEWAY_PRODUCTION_ENABLED = "gateway_production_enabled"
BLOCK_DISCORD_PRODUCTION_ENABLED = "discord_production_enabled"
BLOCK_PRODUCTION_EXECUTION_ENABLED = "production_execution_enabled"
BLOCK_CUTOVER_ALREADY_STARTED = "cutover_already_started"
BLOCK_RUNTIME_ALREADY_INVOKED = "runtime_already_invoked"
BLOCK_PERMISSION_ALREADY_EXISTS = "permission_already_exists"
BLOCK_PERMISSION_EXPIRED = "permission_expired"
BLOCK_RUNTIME_PERMISSION_CONFLICT = "runtime_permission_conflict"
BLOCK_EXECUTOR_IDENTITY_INVALID = "executor_identity_invalid"
BLOCK_OPERATOR_IDENTITY_INVALID = "operator_identity_invalid"
BLOCK_IDENTITY_SEPARATION_INVALID = "identity_separation_invalid"
BLOCK_TTL_INVALID = "ttl_invalid"
BLOCK_TTL_EXCEEDS_WINDOW = "ttl_exceeds_window"
BLOCK_ONE_SHOT_SCOPE_INVALID = "one_shot_scope_invalid"
BLOCK_TICKET_SCOPE_INVALID = "ticket_scope_invalid"
BLOCK_PERMISSION_STORE_CORRUPTED = "permission_store_corrupted"
BLOCK_PERMISSION_WRITE_FAILED = "permission_write_failed"
BLOCK_UNSAFE_OUTPUT = "unsafe_output"

WARN_PERMISSION_IS_PREREQUISITE_ONLY = "permission_is_prerequisite_only"
WARN_RUNTIME_NOT_INVOKED = "runtime_not_invoked"
WARN_CUTOVER_NOT_STARTED = "cutover_not_started"
WARN_PRODUCTION_EXECUTION_DISABLED = "production_execution_disabled"
WARN_PRODUCTION_ROOT_HARD_DENIED = "production_root_hard_denied"
WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED = "original_repository2_not_executed"
WARN_EXTERNAL_PUBLISH_DISABLED = "external_publish_disabled"
WARN_GATEWAY_PRODUCTION_DISABLED = "gateway_production_disabled"
WARN_DISCORD_PRODUCTION_DISABLED = "discord_production_disabled"
WARN_MANUAL_RUNTIME_START_REQUIRED = "manual_runtime_start_required"
WARN_PERMISSION_EXPIRY_REQUIRES_NEW_CONTRACT = (
    "permission_expiry_requires_new_contract"
)
WARN_ONE_SHOT_ONLY = "one_shot_only"
WARN_OPERATOR_SUPERVISION_REQUIRED = "operator_supervision_required"

_FORBIDDEN_OUTPUT_TOKENS = frozenset(
    {
        "pipeline_root",
        "confirmation_phrase",
        "unlock_token",
        "repository2",
        "repository_attestation",
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
        "approved_by",
        "security_reviewed",
        "phrase",
        "signed_by",
        "signer_id",
        "operator_id",
        "prepared_by",
        "issued_by",
        "attestation_hash",
        "rollback_commit",
    }
)


class ProductionRuntimePermissionError(ValueError):
    """Raised when runtime permission assessment or issue fails safely."""


@dataclass(frozen=True)
class ProductionRuntimePermissionRecord:
    permission_id: str
    activation_request_id: str
    cutover_contract_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    ticket_id: str
    confirmation_id: str
    final_signoff_id: str
    rollback_validation_id: str
    controlled_window_open_event_id: str
    executor_id: str
    issued_by: str
    issued_at: str
    expires_at: str
    ttl_seconds: int
    scope_type: str
    max_executions: int
    execution_count: int
    consumed: bool
    consumed_at: str
    revoked: bool
    revoked_at: str
    revoke_reason_code: str
    permission_status: str
    tested_commit_sha: str
    release_tag: str
    production_execution_allowed: bool = False
    production_root_hard_deny: bool = True
    cutover_started: bool = False
    runtime_invoked: bool = False
    original_repository2_execution_attempted: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    external_publish_enabled: bool = False


@dataclass(frozen=True)
class ProductionRuntimePermissionEvent:
    event_id: str
    permission_id: str
    activation_request_id: str
    event_type: str
    actor_role: str
    reason_code: str
    occurred_at: str


@dataclass(frozen=True)
class ProductionRuntimePermissionSummary:
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    permission_state: str
    permission_ready: bool
    permission_present: bool
    controlled_window_state: str
    controlled_window_open: bool
    controlled_window_expired: bool
    governed_cutover_contract_valid: bool
    governed_cutover_status: str
    final_signoff_valid: bool
    rollback_ready: bool
    operational_signoff_valid: bool
    audit_chain_complete: bool
    recovery_required: bool
    repair_lock_held: bool
    executor_identity_valid: bool
    operator_identity_valid: bool
    identity_separation_valid: bool
    one_shot_scope_valid: bool
    ticket_scope_valid: bool
    window_scope_valid: bool
    ttl_valid: bool
    issued_at: str
    expires_at: str
    consumed: bool
    revoked: bool
    cutover_started: bool
    runtime_invoked: bool
    production_execution_allowed: bool
    production_root_hard_deny: bool
    original_repository2_execution_attempted: bool
    external_publish_enabled: bool
    gateway_production_enabled: bool
    discord_production_enabled: bool
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str
    ttl_seconds: int = 0
    already_issued: bool = False
    executor_assigned: bool = False
    operator_present: bool = False
    tested_commit_sha_short: str = ""
    release_tag: str = ""
    window_remaining_seconds: int = 0


@dataclass(frozen=True)
class ProductionRuntimePermissionReleaseSummary:
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    controlled_window_state: str
    permission_state: str
    permission_ready: bool
    permission_present: bool
    permission_expired: bool
    production_execution_allowed: bool = False
    cutover_started: bool = False
    runtime_invoked: bool = False
    production_root_hard_deny: bool = True
    original_repository2_execution_enabled: bool = False
    external_publish_enabled: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    next_phase: str = ""
    release_status: str = RELEASE_RUNTIME_PERMISSION_NOT_READY


@dataclass(frozen=True)
class ProductionRuntimePermissionDashboardDigest:
    runtime_permission_state: str
    runtime_permission_ready: bool
    runtime_permission_present: bool
    runtime_permission_expired: bool
    runtime_permission_id: str
    runtime_permission_expires_at: str
    runtime_permission_blocking_count: int
    runtime_permission_warning_count: int
    runtime_permission_recommended_action: str


def default_runtime_permission_store_dir() -> Path:
    return get_hermes_home() / "coo" / _PERMISSION_STORE_DIR


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def _short_sha(value: str, limit: int = 12) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit]


def _parse_iso(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _permission_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionRuntimePermissionError("activation_request_id is required")
    base = (store_dir or default_runtime_permission_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionRuntimePermissionError(
            "Runtime permission store must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_runtime_permission_store_available(*, store_dir: Path | None = None) -> bool:
    try:
        base = (store_dir or default_runtime_permission_store_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _record_from_dict(payload: Mapping[str, Any]) -> ProductionRuntimePermissionRecord:
    return ProductionRuntimePermissionRecord(
        permission_id=str(payload.get("permission_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        cutover_contract_id=str(payload.get("cutover_contract_id", "")),
        reservation_id=str(payload.get("reservation_id", "")),
        execution_attempt_id=str(payload.get("execution_attempt_id", "")),
        dispatch_run_id=str(payload.get("dispatch_run_id", "")),
        ticket_id=str(payload.get("ticket_id", "")),
        confirmation_id=str(payload.get("confirmation_id", "")),
        final_signoff_id=str(payload.get("final_signoff_id", "")),
        rollback_validation_id=str(payload.get("rollback_validation_id", "")),
        controlled_window_open_event_id=str(
            payload.get("controlled_window_open_event_id", "")
        ),
        executor_id=str(payload.get("executor_id", "")),
        issued_by=str(payload.get("issued_by", "")),
        issued_at=str(payload.get("issued_at", "")),
        expires_at=str(payload.get("expires_at", "")),
        ttl_seconds=int(payload.get("ttl_seconds") or 0),
        scope_type=str(payload.get("scope_type", "")),
        max_executions=int(payload.get("max_executions") or 0),
        execution_count=int(payload.get("execution_count") or 0),
        consumed=bool(payload.get("consumed", False)),
        consumed_at=str(payload.get("consumed_at", "")),
        revoked=bool(payload.get("revoked", False)),
        revoked_at=str(payload.get("revoked_at", "")),
        revoke_reason_code=str(payload.get("revoke_reason_code", "")),
        permission_status=str(payload.get("permission_status", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        production_execution_allowed=False,
        production_root_hard_deny=True,
        cutover_started=False,
        runtime_invoked=False,
        original_repository2_execution_attempted=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        external_publish_enabled=False,
    )


def _record_to_dict(record: ProductionRuntimePermissionRecord) -> dict[str, Any]:
    return {
        "permission_id": record.permission_id,
        "activation_request_id": record.activation_request_id,
        "cutover_contract_id": record.cutover_contract_id,
        "reservation_id": record.reservation_id,
        "execution_attempt_id": record.execution_attempt_id,
        "dispatch_run_id": record.dispatch_run_id,
        "ticket_id": record.ticket_id,
        "confirmation_id": record.confirmation_id,
        "final_signoff_id": record.final_signoff_id,
        "rollback_validation_id": record.rollback_validation_id,
        "controlled_window_open_event_id": record.controlled_window_open_event_id,
        "executor_id": record.executor_id,
        "issued_by": record.issued_by,
        "issued_at": record.issued_at,
        "expires_at": record.expires_at,
        "ttl_seconds": record.ttl_seconds,
        "scope_type": record.scope_type,
        "max_executions": 1,
        "execution_count": 0,
        "consumed": False,
        "consumed_at": "",
        "revoked": False,
        "revoked_at": "",
        "revoke_reason_code": "",
        "permission_status": record.permission_status,
        "tested_commit_sha": _short_sha(record.tested_commit_sha),
        "release_tag": record.release_tag,
        "production_execution_allowed": False,
        "production_root_hard_deny": True,
        "cutover_started": False,
        "runtime_invoked": False,
        "original_repository2_execution_attempted": False,
        "gateway_production_enabled": False,
        "discord_production_enabled": False,
        "external_publish_enabled": False,
    }


def load_runtime_permission_record(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionRuntimePermissionRecord | None:
    path = _permission_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionRuntimePermissionError("permission_store_corrupted") from exc
    permission = payload.get("permission")
    if not isinstance(permission, dict):
        raise ProductionRuntimePermissionError("permission_store_corrupted")
    return _record_from_dict(permission)


def load_runtime_permission_by_id(
    permission_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionRuntimePermissionRecord | None:
    target = (permission_id or "").strip()
    if not target:
        raise ProductionRuntimePermissionError("permission_id is required")
    base = (store_dir or default_runtime_permission_store_dir()).resolve()
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductionRuntimePermissionError(
                "permission_store_corrupted"
            ) from exc
        permission = payload.get("permission")
        if isinstance(permission, dict) and str(permission.get("permission_id", "")) == target:
            return _record_from_dict(permission)
    return None


def load_runtime_permission_events(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> tuple[ProductionRuntimePermissionEvent, ...]:
    path = _permission_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionRuntimePermissionError("permission_store_corrupted") from exc
    raw = payload.get("events") or []
    if not isinstance(raw, list):
        raise ProductionRuntimePermissionError("permission_store_corrupted")
    events: list[ProductionRuntimePermissionEvent] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ProductionRuntimePermissionError("permission_store_corrupted")
        event_id = str(item.get("event_id", ""))
        if not event_id or event_id in seen:
            raise ProductionRuntimePermissionError("permission_store_corrupted")
        seen.add(event_id)
        events.append(
            ProductionRuntimePermissionEvent(
                event_id=event_id,
                permission_id=str(item.get("permission_id", "")),
                activation_request_id=str(item.get("activation_request_id", "")),
                event_type=str(item.get("event_type", "")),
                actor_role=str(item.get("actor_role", "")),
                reason_code=str(item.get("reason_code", "")),
                occurred_at=str(item.get("occurred_at", "")),
            )
        )
    return tuple(events)


def _write_permission_bundle(
    record: ProductionRuntimePermissionRecord,
    events: tuple[ProductionRuntimePermissionEvent, ...],
    *,
    store_dir: Path | None = None,
) -> None:
    path = _permission_path(record.activation_request_id, store_dir=store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    payload = {
        "version": _PERMISSION_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "permission": _record_to_dict(record),
        "events": [
            {
                "event_id": event.event_id,
                "permission_id": event.permission_id,
                "activation_request_id": event.activation_request_id,
                "event_type": event.event_type,
                "actor_role": event.actor_role,
                "reason_code": event.reason_code,
                "occurred_at": event.occurred_at,
            }
            for event in events
        ],
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(lock_path, "a", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            existing = load_runtime_permission_record(
                record.activation_request_id,
                store_dir=store_dir,
            )
            if existing is not None:
                if _permissions_equivalent(existing, record):
                    return
                raise ProductionRuntimePermissionError("runtime_permission_conflict")
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            try:
                fd = os.open(str(path), flags, 0o644)
            except FileExistsError as exc:
                existing_again = load_runtime_permission_record(
                    record.activation_request_id,
                    store_dir=store_dir,
                )
                if existing_again is not None and _permissions_equivalent(
                    existing_again, record
                ):
                    return
                raise ProductionRuntimePermissionError(
                    "runtime_permission_conflict"
                ) from exc
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
    except OSError as exc:
        raise ProductionRuntimePermissionError("permission_write_failed") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _permissions_equivalent(
    existing: ProductionRuntimePermissionRecord,
    candidate: ProductionRuntimePermissionRecord,
) -> bool:
    return (
        existing.cutover_contract_id == candidate.cutover_contract_id
        and existing.reservation_id == candidate.reservation_id
        and existing.execution_attempt_id == candidate.execution_attempt_id
        and existing.dispatch_run_id == candidate.dispatch_run_id
        and existing.ticket_id == candidate.ticket_id
        and existing.confirmation_id == candidate.confirmation_id
        and existing.controlled_window_open_event_id
        == candidate.controlled_window_open_event_id
        and existing.executor_id == candidate.executor_id
        and existing.issued_by == candidate.issued_by
        and existing.ttl_seconds == candidate.ttl_seconds
        and existing.final_signoff_id == candidate.final_signoff_id
    )


def _open_event_id(events) -> str:
    for event in reversed(events):
        if event.event_type == EVENT_WINDOW_OPENED:
            return event.event_id
    return ""


def _assess_identities(
    *,
    executor_id: str,
    operator_id: str,
    request,
    contract,
    final_record,
    op_record,
) -> tuple[bool, bool, bool, list[str]]:
    """Validate provided identities only.

    Empty executor/operator (status path) does not append identity blocks.
    Check provides executor only; issue provides both.
    """
    blocking: list[str] = []
    executor = (executor_id or "").strip()
    operator = (operator_id or "").strip()
    require_executor = bool(executor_id)
    require_operator = bool(operator_id)

    conflicts_for_executor: set[str] = set()
    if request is not None:
        conflicts_for_executor.add((request.requested_by or "").strip())
        conflicts_for_executor.add((request.security_reviewed_by or "").strip())
    if contract is not None:
        conflicts_for_executor.add((contract.prepared_by or "").strip())
    if final_record is not None:
        conflicts_for_executor.add((final_record.signed_by or "").strip())
    if op_record is not None:
        conflicts_for_executor.add((op_record.signed_by or "").strip())
    conflicts_for_executor.discard("")

    executor_valid = True
    if require_executor:
        executor_valid = bool(executor) and executor not in conflicts_for_executor
        if not executor_valid:
            blocking.append(BLOCK_EXECUTOR_IDENTITY_INVALID)

    operator_valid = True
    if require_operator:
        conflicts_for_operator = set(conflicts_for_executor)
        if executor:
            conflicts_for_operator.add(executor)
        operator_valid = bool(operator) and operator not in conflicts_for_operator
        if not operator_valid:
            if operator and executor and operator == executor:
                blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
            else:
                blocking.append(BLOCK_OPERATOR_IDENTITY_INVALID)

    separation = True
    if require_executor and require_operator:
        separation = (
            executor_valid
            and operator_valid
            and executor != operator
            and executor not in conflicts_for_executor
        )
        if executor_valid and operator_valid and not separation:
            blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
    return executor_valid, operator_valid, separation, blocking


def _validate_ttl(
    ttl_seconds: int | None,
    *,
    now: datetime,
    window_end: datetime | None,
) -> tuple[bool, bool, int, int, str]:
    """Return (ttl_valid, exceeds_window, effective_ttl, remaining, expires_at_iso)."""
    if ttl_seconds is None:
        return True, False, 0, 0, ""
    if ttl_seconds < MIN_TTL_SECONDS or ttl_seconds > MAX_TTL_SECONDS:
        return False, False, ttl_seconds, 0, ""
    remaining = 0
    if window_end is not None:
        remaining = max(0, int((window_end - now).total_seconds()))
        if remaining < MIN_TTL_SECONDS:
            return False, True, ttl_seconds, remaining, ""
        if ttl_seconds > remaining:
            return False, True, ttl_seconds, remaining, ""
        expires = now + timedelta(seconds=ttl_seconds)
        return True, False, ttl_seconds, remaining, expires.isoformat()
    return False, False, ttl_seconds, 0, ""


def _recommended_action(
    state: str,
    blocking: tuple[str, ...],
    *,
    window_open: bool,
    recovery: bool,
) -> str:
    if state == PERMISSION_ISSUED:
        return ACTION_PREPARE_PHASE_15D_GOVERNED_RUNTIME_SESSION
    if state == PERMISSION_READY:
        return ACTION_ISSUE_PRODUCTION_RUNTIME_PERMISSION
    if state == PERMISSION_EXPIRED:
        return ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT
    if recovery or BLOCK_RECOVERY_REQUIRED in blocking or BLOCK_REPAIR_LOCK_HELD in blocking:
        return ACTION_RUN_CONSUME_RECOVERY
    if BLOCK_CONTROLLED_WINDOW_EXPIRED in blocking:
        return ACTION_CLOSE_EXPIRED_WINDOW
    if BLOCK_CONTROLLED_WINDOW_NOT_OPEN in blocking:
        return ACTION_WAIT_FOR_WINDOW_OPEN
    if BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED in blocking or BLOCK_CONTROLLED_WINDOW_CLOSED in blocking:
        return ACTION_REVOKE_OR_CLOSE_WINDOW
    if (
        BLOCK_EXECUTOR_IDENTITY_INVALID in blocking
        or BLOCK_OPERATOR_IDENTITY_INVALID in blocking
        or BLOCK_IDENTITY_SEPARATION_INVALID in blocking
    ):
        return ACTION_RESOLVE_IDENTITY_SEPARATION
    if BLOCK_TTL_INVALID in blocking or BLOCK_TTL_EXCEEDS_WINDOW in blocking:
        return ACTION_RESOLVE_RUNTIME_PERMISSION_SCOPE
    if BLOCK_FINAL_SIGNOFF_INVALID in blocking:
        return ACTION_RESOLVE_FINAL_SIGNOFF
    if BLOCK_ROLLBACK_VALIDATION_INVALID in blocking:
        return ACTION_RESOLVE_ROLLBACK_VALIDATION
    if BLOCK_GOVERNED_CUTOVER_CONTRACT_MISSING in blocking:
        return ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT
    if BLOCK_PERMISSION_ALREADY_EXISTS in blocking:
        return ACTION_PRODUCTION_RUNTIME_PERMISSION_ISSUED
    if not window_open:
        return ACTION_WAIT_FOR_WINDOW_OPEN
    return ACTION_MAINTAIN_PRODUCTION_BLOCK


def evaluate_production_runtime_permission(
    *,
    activation_request_id: str,
    executor_id: str = "",
    operator_id: str = "",
    ttl_seconds: int | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    runtime_history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    e2e_history_dir: Path | None = None,
    signoff_store_dir: Path | None = None,
    validation_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    preflight_history_dir: Path | None = None,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    force_production_execution_allowed: bool | None = None,
    force_gateway_enabled: bool | None = None,
    force_discord_enabled: bool | None = None,
    force_cutover_started: bool | None = None,
    force_runtime_invoked: bool | None = None,
) -> ProductionRuntimePermissionSummary:
    """Read-only runtime permission assessment."""
    blocking: list[str] = []
    warnings: list[str] = []
    current = _utc_now(now)

    window = evaluate_production_controlled_window(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        reservation_dir=reservation_dir,
        runtime_history_dir=runtime_history_dir,
        evidence_dir=evidence_dir,
        audit_dir=audit_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        e2e_history_dir=e2e_history_dir,
        signoff_store_dir=signoff_store_dir,
        validation_store_dir=validation_store_dir,
        final_signoff_store_dir=final_signoff_store_dir,
        preflight_history_dir=preflight_history_dir,
        governed_cutover_store_dir=governed_cutover_store_dir,
        window_store_dir=window_store_dir,
        repo_root=repo_root,
        merged_config=merged_config,
        now=current,
    )

    contract = load_governed_cutover_contract(
        activation_request_id,
        store_dir=governed_cutover_store_dir,
    )
    existing = load_runtime_permission_record(
        activation_request_id,
        store_dir=permission_store_dir,
    )
    final_record = load_final_signoff_record(
        activation_request_id,
        store_dir=final_signoff_store_dir,
    )
    op_record = load_operational_signoff_record(
        activation_request_id,
        store_dir=signoff_store_dir,
    )
    request = None
    try:
        request = load_activation_request(activation_request_id, store_dir=store_dir)
    except Exception:
        request = None
    reservation = load_execution_reservation(
        activation_request_id,
        store_dir=reservation_dir,
    )
    _, window_events = load_window_lifecycle_events(
        activation_request_id,
        store_dir=window_store_dir,
    )
    open_event_id = _open_event_id(window_events)

    cutover_contract_id = window.cutover_contract_id
    governed_status = ""
    contract_valid = False
    if contract is None:
        blocking.append(BLOCK_GOVERNED_CUTOVER_CONTRACT_MISSING)
    else:
        cutover_contract_id = contract.cutover_contract_id
        if contract.contract_status != CONTRACT_STATUS_PREPARED:
            blocking.append(BLOCK_GOVERNED_CUTOVER_NOT_PREPARED)
        else:
            contract_valid = True
        if (
            not contract.checklist_passed
            or not contract.operator_handoff_ready
            or not contract.rollback_ready
        ):
            blocking.append(BLOCK_GOVERNED_CUTOVER_CONTRACT_INVALID)
            contract_valid = False

    if window.window_state == WINDOW_CLOSED or window.window_closed:
        blocking.append(BLOCK_CONTROLLED_WINDOW_CLOSED)
    if window.window_state == WINDOW_EMERGENCY_CLOSED or window.emergency_closed:
        blocking.append(BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED)
    if window.expired or window.window_state == WINDOW_EXPIRED:
        blocking.append(BLOCK_CONTROLLED_WINDOW_EXPIRED)
    if not window.window_open or window.window_state != WINDOW_OPEN:
        if (
            BLOCK_CONTROLLED_WINDOW_CLOSED not in blocking
            and BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED not in blocking
            and BLOCK_CONTROLLED_WINDOW_EXPIRED not in blocking
        ):
            blocking.append(BLOCK_CONTROLLED_WINDOW_NOT_OPEN)
    window_scope_valid = (
        window.window_open
        and not window.expired
        and window.current_time_within_window
        and bool(open_event_id)
    )
    if window.window_open and not window.current_time_within_window:
        blocking.append(BLOCK_WINDOW_TIME_OUTSIDE_SCOPE)
        window_scope_valid = False

    final_signoff_valid = False
    if final_record is None or final_record.final_signoff_status not in {
        PRODUCTION_FINAL_SIGNOFF_READY,
        PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
    } or not final_record.production_release_ready:
        blocking.append(BLOCK_FINAL_SIGNOFF_INVALID)
    else:
        final_signoff_valid = True

    operational_signoff_valid = op_record is not None
    if not operational_signoff_valid:
        blocking.append(BLOCK_OPERATIONAL_SIGNOFF_INVALID)

    rollback_ready = bool(contract.rollback_ready) if contract else False
    if not rollback_ready:
        blocking.append(BLOCK_ROLLBACK_VALIDATION_INVALID)

    activation_revoked = bool(
        request is not None and request.state == ACTIVATION_STATE_REVOKED
    )
    if not activation_revoked:
        blocking.append(BLOCK_ACTIVATION_NOT_REVOKED)

    reservation_completed = (
        reservation is not None
        and reservation.state == RESERVATION_STATE_COMPLETED
    )
    if not reservation_completed:
        blocking.append(BLOCK_RESERVATION_NOT_COMPLETED)

    recovery_required = False
    repair_lock_held = False
    audit_chain_complete = False
    consume_committed = False
    e2e_finalized = False
    source_unchanged = True
    root_untouched = True
    if contract is not None and reservation is not None:
        try:
            cutover = evaluate_production_governed_cutover(
                activation_request_id=activation_request_id,
                reservation_id=reservation.reservation_id,
                operator_id=contract.prepared_by,
                window_start=contract.maintenance_window_start,
                window_end=contract.maintenance_window_end,
                store_dir=store_dir,
                reservation_dir=reservation_dir,
                runtime_history_dir=runtime_history_dir,
                evidence_dir=evidence_dir,
                audit_dir=audit_dir,
                bundle_dir=bundle_dir,
                confirmation_dir=confirmation_dir,
                transaction_dir=transaction_dir,
                e2e_history_dir=e2e_history_dir,
                signoff_store_dir=signoff_store_dir,
                validation_store_dir=validation_store_dir,
                final_signoff_store_dir=final_signoff_store_dir,
                preflight_history_dir=preflight_history_dir,
                governed_cutover_store_dir=governed_cutover_store_dir
                or default_governed_cutover_store_dir(),
                repo_root=repo_root,
                merged_config=merged_config,
                now=current,
            )
            governed_status = cutover.governed_cutover_status
            recovery_required = cutover.recovery_required
            repair_lock_held = cutover.repair_lock_held
            audit_chain_complete = cutover.audit_chain_complete
            consume_committed = cutover.consume_committed
            e2e_finalized = cutover.e2e_finalized
            source_unchanged = cutover.source_tree_unchanged
            root_untouched = cutover.production_root_untouched
            if cutover.governed_cutover_status != GOVERNED_CUTOVER_CONTRACT_PREPARED:
                if not recovery_required and not repair_lock_held:
                    blocking.append(BLOCK_GOVERNED_CUTOVER_CONTRACT_INVALID)
        except Exception:
            blocking.append(BLOCK_PERMISSION_STORE_CORRUPTED)

    if not consume_committed:
        blocking.append(BLOCK_CONSUME_NOT_COMMITTED)
    if not e2e_finalized:
        blocking.append(BLOCK_E2E_NOT_FINALIZED)
    if not audit_chain_complete:
        blocking.append(BLOCK_AUDIT_CHAIN_INCOMPLETE)
    if recovery_required:
        blocking.append(BLOCK_RECOVERY_REQUIRED)
    if repair_lock_held:
        blocking.append(BLOCK_REPAIR_LOCK_HELD)
    if not source_unchanged:
        blocking.append(BLOCK_SOURCE_TREE_MUTATED)
    if not root_untouched:
        blocking.append(BLOCK_PRODUCTION_ROOT_TOUCHED)

    ticket_scope_valid = bool(reservation and reservation.ticket_id and reservation.confirmation_id)
    if not ticket_scope_valid:
        blocking.append(BLOCK_TICKET_SCOPE_INVALID)

    one_shot_scope_valid = True
    if request is not None:
        one_shot_scope_valid = getattr(request, "scope_type", "") in (
            "",
            "one_shot",
            "ticket_scoped",
            "maintenance_window",
        )
    if not one_shot_scope_valid:
        blocking.append(BLOCK_ONE_SHOT_SCOPE_INVALID)

    window_end = _parse_iso(window.maintenance_window_end)
    ttl_valid, ttl_exceeds, effective_ttl, remaining, expires_iso = _validate_ttl(
        ttl_seconds,
        now=current,
        window_end=window_end,
    )
    if ttl_seconds is not None:
        if not ttl_valid and ttl_exceeds:
            blocking.append(BLOCK_TTL_EXCEEDS_WINDOW)
        elif not ttl_valid:
            blocking.append(BLOCK_TTL_INVALID)

    executor_valid, operator_valid, separation_valid, id_blocks = _assess_identities(
        executor_id=executor_id,
        operator_id=operator_id,
        request=request,
        contract=contract,
        final_record=final_record,
        op_record=op_record,
    )
    blocking.extend(id_blocks)

    production_execution_allowed = bool(force_production_execution_allowed)
    gateway_enabled = bool(force_gateway_enabled)
    discord_enabled = bool(force_discord_enabled)
    cutover_started = bool(force_cutover_started)
    runtime_invoked = bool(force_runtime_invoked)
    if production_execution_allowed:
        blocking.append(BLOCK_PRODUCTION_EXECUTION_ENABLED)
    if gateway_enabled:
        blocking.append(BLOCK_GATEWAY_PRODUCTION_ENABLED)
    if discord_enabled:
        blocking.append(BLOCK_DISCORD_PRODUCTION_ENABLED)
    if cutover_started:
        blocking.append(BLOCK_CUTOVER_ALREADY_STARTED)
    if runtime_invoked:
        blocking.append(BLOCK_RUNTIME_ALREADY_INVOKED)

    permission_id = ""
    issued_at = ""
    expires_at = ""
    already_issued = False
    permission_present = existing is not None
    derived_expired = False
    if existing is not None:
        permission_id = existing.permission_id
        issued_at = existing.issued_at
        expires_at = existing.expires_at
        expires_dt = _parse_iso(existing.expires_at)
        if expires_dt is not None and current >= expires_dt:
            derived_expired = True
            blocking.append(BLOCK_PERMISSION_EXPIRED)
        else:
            already_issued = True
        if ttl_seconds is not None or executor_id or operator_id:
            candidate_conflict = False
            if executor_id and existing.executor_id != executor_id.strip():
                candidate_conflict = True
            if operator_id and existing.issued_by != operator_id.strip():
                candidate_conflict = True
            if ttl_seconds is not None and existing.ttl_seconds != ttl_seconds:
                candidate_conflict = True
            if candidate_conflict and not already_issued:
                blocking.append(BLOCK_RUNTIME_PERMISSION_CONFLICT)
            elif candidate_conflict and already_issued and not derived_expired:
                # Same activation with different params while live permission exists.
                blocking.append(BLOCK_RUNTIME_PERMISSION_CONFLICT)
        if not derived_expired:
            blocking.append(BLOCK_PERMISSION_ALREADY_EXISTS)

    unique_blocking = tuple(dict.fromkeys(blocking))
    hard_ready_blockers = [
        code
        for code in unique_blocking
        if code
        not in {
            BLOCK_PERMISSION_ALREADY_EXISTS,
            BLOCK_TTL_INVALID,  # omitted when ttl_seconds is None for status
        }
        and not (ttl_seconds is None and code in {BLOCK_TTL_INVALID, BLOCK_TTL_EXCEEDS_WINDOW})
        and not (
            not executor_id
            and not operator_id
            and code
            in {
                BLOCK_EXECUTOR_IDENTITY_INVALID,
                BLOCK_OPERATOR_IDENTITY_INVALID,
                BLOCK_IDENTITY_SEPARATION_INVALID,
            }
        )
    ]

    # For status without identities/ttl, still require core chain readiness for READY.
    core_ready = (
        contract_valid
        and window_scope_valid
        and final_signoff_valid
        and operational_signoff_valid
        and rollback_ready
        and reservation_completed
        and consume_committed
        and e2e_finalized
        and audit_chain_complete
        and not recovery_required
        and not repair_lock_held
        and source_unchanged
        and root_untouched
        and ticket_scope_valid
        and one_shot_scope_valid
        and activation_revoked
        and not production_execution_allowed
        and not gateway_enabled
        and not discord_enabled
        and not cutover_started
        and not runtime_invoked
        and existing is None
    )

    issue_ready = (
        core_ready
        and executor_valid
        and operator_valid
        and separation_valid
        and ttl_seconds is not None
        and ttl_valid
        and not any(
            code in unique_blocking
            for code in (
                BLOCK_TTL_INVALID,
                BLOCK_TTL_EXCEEDS_WINDOW,
                BLOCK_EXECUTOR_IDENTITY_INVALID,
                BLOCK_OPERATOR_IDENTITY_INVALID,
                BLOCK_IDENTITY_SEPARATION_INVALID,
                BLOCK_PERMISSION_ALREADY_EXISTS,
                BLOCK_PERMISSION_EXPIRED,
                BLOCK_RUNTIME_PERMISSION_CONFLICT,
            )
        )
    )
    check_ready = (
        core_ready
        and bool(executor_id.strip())
        and executor_valid
        and ttl_seconds is not None
        and ttl_valid
        and existing is None
        and BLOCK_EXECUTOR_IDENTITY_INVALID not in unique_blocking
        and BLOCK_TTL_INVALID not in unique_blocking
        and BLOCK_TTL_EXCEEDS_WINDOW not in unique_blocking
    )

    if existing is not None and derived_expired:
        state = PERMISSION_EXPIRED
        permission_ready = False
    elif existing is not None and already_issued:
        state = PERMISSION_ISSUED
        permission_ready = False
    elif recovery_required or repair_lock_held:
        state = PERMISSION_BLOCKED
        permission_ready = False
    elif issue_ready:
        state = PERMISSION_READY
        permission_ready = True
    elif check_ready and not operator_id:
        state = PERMISSION_READY
        permission_ready = True
    elif hard_ready_blockers and not check_ready:
        state = PERMISSION_BLOCKED
        permission_ready = False
    else:
        state = PERMISSION_NOT_ISSUED
        permission_ready = False

    if existing is None and issue_ready:
        state = PERMISSION_READY
        permission_ready = True

    warnings.extend(
        [
            WARN_PERMISSION_IS_PREREQUISITE_ONLY,
            WARN_RUNTIME_NOT_INVOKED,
            WARN_CUTOVER_NOT_STARTED,
            WARN_PRODUCTION_EXECUTION_DISABLED,
            WARN_PRODUCTION_ROOT_HARD_DENIED,
            WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED,
            WARN_EXTERNAL_PUBLISH_DISABLED,
            WARN_GATEWAY_PRODUCTION_DISABLED,
            WARN_DISCORD_PRODUCTION_DISABLED,
            WARN_ONE_SHOT_ONLY,
            WARN_OPERATOR_SUPERVISION_REQUIRED,
        ]
    )
    if state == PERMISSION_ISSUED:
        warnings.append(WARN_MANUAL_RUNTIME_START_REQUIRED)
    if state == PERMISSION_EXPIRED:
        warnings.append(WARN_PERMISSION_EXPIRY_REQUIRES_NEW_CONTRACT)
    unique_warnings = tuple(dict.fromkeys(warnings))

    recommended = _recommended_action(
        state,
        unique_blocking,
        window_open=window.window_open,
        recovery=recovery_required or repair_lock_held,
    )
    if state == PERMISSION_ISSUED:
        recommended = ACTION_PREPARE_PHASE_15D_GOVERNED_RUNTIME_SESSION
    elif state == PERMISSION_READY:
        recommended = ACTION_ISSUE_PRODUCTION_RUNTIME_PERMISSION

    return ProductionRuntimePermissionSummary(
        activation_request_id=activation_request_id,
        cutover_contract_id=cutover_contract_id,
        permission_id=permission_id,
        permission_state=state,
        permission_ready=permission_ready,
        permission_present=permission_present,
        controlled_window_state=window.window_state,
        controlled_window_open=window.window_open,
        controlled_window_expired=window.expired,
        governed_cutover_contract_valid=contract_valid,
        governed_cutover_status=governed_status,
        final_signoff_valid=final_signoff_valid,
        rollback_ready=rollback_ready,
        operational_signoff_valid=operational_signoff_valid,
        audit_chain_complete=audit_chain_complete,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        executor_identity_valid=executor_valid if executor_id else True,
        operator_identity_valid=operator_valid if operator_id else True,
        identity_separation_valid=separation_valid if (executor_id and operator_id) else True,
        one_shot_scope_valid=one_shot_scope_valid,
        ticket_scope_valid=ticket_scope_valid,
        window_scope_valid=window_scope_valid,
        ttl_valid=ttl_valid if ttl_seconds is not None else True,
        issued_at=issued_at,
        expires_at=expires_at or expires_iso,
        consumed=False,
        revoked=False,
        cutover_started=False,
        runtime_invoked=False,
        production_execution_allowed=False,
        production_root_hard_deny=True,
        original_repository2_execution_attempted=False,
        external_publish_enabled=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        blocking_items=unique_blocking,
        warning_items=unique_warnings,
        recommended_action=recommended,
        ttl_seconds=effective_ttl if ttl_seconds is not None else (existing.ttl_seconds if existing else 0),
        already_issued=already_issued,
        executor_assigned=bool((executor_id or "").strip()) or (
            existing is not None and bool(existing.executor_id)
        ),
        operator_present=bool((operator_id or "").strip()) or (
            existing is not None and bool(existing.issued_by)
        ),
        tested_commit_sha_short=_short_sha(
            request.tested_commit_sha if request is not None else ""
        ),
        release_tag=request.release_tag if request is not None else "",
        window_remaining_seconds=remaining,
    )


def issue_production_runtime_permission(
    *,
    activation_request_id: str,
    executor_id: str,
    operator_id: str,
    ttl_seconds: int,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    runtime_history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    e2e_history_dir: Path | None = None,
    signoff_store_dir: Path | None = None,
    validation_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    preflight_history_dir: Path | None = None,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionRuntimePermissionSummary:
    """Append-only issue of a one-shot runtime permission artifact."""
    if not probe_runtime_permission_store_available(store_dir=permission_store_dir):
        raise ProductionRuntimePermissionError("permission_write_failed")

    summary = evaluate_production_runtime_permission(
        activation_request_id=activation_request_id,
        executor_id=executor_id,
        operator_id=operator_id,
        ttl_seconds=ttl_seconds,
        store_dir=store_dir,
        reservation_dir=reservation_dir,
        runtime_history_dir=runtime_history_dir,
        evidence_dir=evidence_dir,
        audit_dir=audit_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        e2e_history_dir=e2e_history_dir,
        signoff_store_dir=signoff_store_dir,
        validation_store_dir=validation_store_dir,
        final_signoff_store_dir=final_signoff_store_dir,
        preflight_history_dir=preflight_history_dir,
        governed_cutover_store_dir=governed_cutover_store_dir,
        window_store_dir=window_store_dir,
        permission_store_dir=permission_store_dir,
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )

    existing = load_runtime_permission_record(
        activation_request_id,
        store_dir=permission_store_dir,
    )
    if existing is not None:
        expires_dt = _parse_iso(existing.expires_at)
        if expires_dt is not None and _utc_now(now) >= expires_dt:
            raise ProductionRuntimePermissionError("permission_expired")
        candidate = ProductionRuntimePermissionRecord(
            permission_id=existing.permission_id,
            activation_request_id=activation_request_id,
            cutover_contract_id=summary.cutover_contract_id,
            reservation_id=existing.reservation_id,
            execution_attempt_id=existing.execution_attempt_id,
            dispatch_run_id=existing.dispatch_run_id,
            ticket_id=existing.ticket_id,
            confirmation_id=existing.confirmation_id,
            final_signoff_id=existing.final_signoff_id,
            rollback_validation_id=existing.rollback_validation_id,
            controlled_window_open_event_id=existing.controlled_window_open_event_id,
            executor_id=(executor_id or "").strip(),
            issued_by=(operator_id or "").strip(),
            issued_at=existing.issued_at,
            expires_at=existing.expires_at,
            ttl_seconds=int(ttl_seconds),
            scope_type=SCOPE_TYPE_ONE_SHOT,
            max_executions=1,
            execution_count=0,
            consumed=False,
            consumed_at="",
            revoked=False,
            revoked_at="",
            revoke_reason_code="",
            permission_status=PERMISSION_ISSUED,
            tested_commit_sha=existing.tested_commit_sha,
            release_tag=existing.release_tag,
        )
        if _permissions_equivalent(existing, candidate):
            return evaluate_production_runtime_permission(
                activation_request_id=activation_request_id,
                executor_id=executor_id,
                operator_id=operator_id,
                ttl_seconds=ttl_seconds,
                store_dir=store_dir,
                reservation_dir=reservation_dir,
                runtime_history_dir=runtime_history_dir,
                evidence_dir=evidence_dir,
                audit_dir=audit_dir,
                bundle_dir=bundle_dir,
                confirmation_dir=confirmation_dir,
                transaction_dir=transaction_dir,
                e2e_history_dir=e2e_history_dir,
                signoff_store_dir=signoff_store_dir,
                validation_store_dir=validation_store_dir,
                final_signoff_store_dir=final_signoff_store_dir,
                preflight_history_dir=preflight_history_dir,
                governed_cutover_store_dir=governed_cutover_store_dir,
                window_store_dir=window_store_dir,
                permission_store_dir=permission_store_dir,
                repo_root=repo_root,
                merged_config=merged_config,
                now=now,
            )
        raise ProductionRuntimePermissionError("runtime_permission_conflict")

    if summary.permission_state != PERMISSION_READY:
        raise ProductionRuntimePermissionError(
            f"runtime permission blocked for state {summary.permission_state!r}"
        )

    contract = load_governed_cutover_contract(
        activation_request_id,
        store_dir=governed_cutover_store_dir,
    )
    reservation = load_execution_reservation(
        activation_request_id,
        store_dir=reservation_dir,
    )
    final_record = load_final_signoff_record(
        activation_request_id,
        store_dir=final_signoff_store_dir,
    )
    if contract is None or reservation is None or final_record is None:
        raise ProductionRuntimePermissionError("permission_store_corrupted")
    _, window_events = load_window_lifecycle_events(
        activation_request_id,
        store_dir=window_store_dir,
    )
    open_event_id = _open_event_id(window_events)
    if not open_event_id:
        raise ProductionRuntimePermissionError("controlled_window_not_open")

    current = _utc_now(now)
    window_end = _parse_iso(contract.maintenance_window_end)
    ttl_valid, ttl_exceeds, effective_ttl, _, expires_iso = _validate_ttl(
        ttl_seconds,
        now=current,
        window_end=window_end,
    )
    if not ttl_valid:
        raise ProductionRuntimePermissionError(
            "ttl_exceeds_window" if ttl_exceeds else "ttl_invalid"
        )

    permission_id = str(uuid.uuid4())
    issued_at = _utc_now_iso(now)
    record = ProductionRuntimePermissionRecord(
        permission_id=permission_id,
        activation_request_id=activation_request_id,
        cutover_contract_id=contract.cutover_contract_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=contract.execution_attempt_id,
        dispatch_run_id=contract.dispatch_run_id,
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        final_signoff_id=final_record.final_signoff_id,
        rollback_validation_id=contract.rollback_validation_id,
        controlled_window_open_event_id=open_event_id,
        executor_id=(executor_id or "").strip(),
        issued_by=(operator_id or "").strip(),
        issued_at=issued_at,
        expires_at=expires_iso,
        ttl_seconds=effective_ttl,
        scope_type=SCOPE_TYPE_ONE_SHOT,
        max_executions=1,
        execution_count=0,
        consumed=False,
        consumed_at="",
        revoked=False,
        revoked_at="",
        revoke_reason_code="",
        permission_status=PERMISSION_ISSUED,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
    )
    events = (
        ProductionRuntimePermissionEvent(
            event_id=str(uuid.uuid4()),
            permission_id=permission_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_PERMISSION_ISSUE_REQUESTED,
            actor_role="operator",
            reason_code="",
            occurred_at=issued_at,
        ),
        ProductionRuntimePermissionEvent(
            event_id=str(uuid.uuid4()),
            permission_id=permission_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_PERMISSION_ISSUED,
            actor_role="operator",
            reason_code="",
            occurred_at=issued_at,
        ),
    )
    _write_permission_bundle(record, events, store_dir=permission_store_dir)
    return evaluate_production_runtime_permission(
        activation_request_id=activation_request_id,
        executor_id=executor_id,
        operator_id=operator_id,
        ttl_seconds=ttl_seconds,
        store_dir=store_dir,
        reservation_dir=reservation_dir,
        runtime_history_dir=runtime_history_dir,
        evidence_dir=evidence_dir,
        audit_dir=audit_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        e2e_history_dir=e2e_history_dir,
        signoff_store_dir=signoff_store_dir,
        validation_store_dir=validation_store_dir,
        final_signoff_store_dir=final_signoff_store_dir,
        preflight_history_dir=preflight_history_dir,
        governed_cutover_store_dir=governed_cutover_store_dir,
        window_store_dir=window_store_dir,
        permission_store_dir=permission_store_dir,
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )


_PERMISSION_CONSUME_STORE_DIR = "production-runtime-permission-consume"


def default_runtime_permission_consume_store_dir() -> Path:
    return get_hermes_home() / "coo" / _PERMISSION_CONSUME_STORE_DIR


def _permission_consume_path(
    permission_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (permission_id or "").strip()
    if not normalized:
        raise ProductionRuntimePermissionError("permission_id is required")
    base = store_dir or default_runtime_permission_consume_store_dir()
    return base / f"{normalized}.json"


def load_runtime_permission_consume_record(
    permission_id: str,
    *,
    store_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return the consume record for permission_id, or None if unconsumed."""
    from agent.coo.production_runtime_consume_store import read_consume_record

    path = _permission_consume_path(permission_id, store_dir=store_dir)
    try:
        return read_consume_record(path)
    except ValueError as exc:
        raise ProductionRuntimePermissionError(str(exc)) from exc


def consume_production_runtime_permission(
    activation_request_id: str,
    *,
    permission_id: str,
    consumed_by: str,
    governed_invoke_id: str = "",
    store_dir: Path | None = None,
    consume_store_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One-shot consume transition for an issued runtime permission.

    Never mutates the original write-once permission bundle. Consumption is
    recorded as a separate one-shot artifact whose existence is the sole
    source of truth for "has this permission been consumed" — enforced by
    the underlying O_CREAT|O_EXCL write, which is race-safe under
    concurrent callers.
    """
    normalized_permission_id = (permission_id or "").strip()
    normalized_consumed_by = (consumed_by or "").strip()
    if not normalized_permission_id:
        raise ProductionRuntimePermissionError("permission_id is required")
    if not normalized_consumed_by:
        raise ProductionRuntimePermissionError("consumed_by is required")

    record = load_runtime_permission_record(activation_request_id, store_dir=store_dir)
    if record is None:
        raise ProductionRuntimePermissionError("permission_missing")
    if record.permission_id != normalized_permission_id:
        raise ProductionRuntimePermissionError("permission_id_mismatch")
    if record.permission_status != PERMISSION_ISSUED:
        raise ProductionRuntimePermissionError("permission_not_issued")
    if record.revoked:
        raise ProductionRuntimePermissionError("permission_revoked")

    current = _utc_now(now)
    expires_dt = _parse_iso(record.expires_at)
    if expires_dt is not None and current >= expires_dt:
        raise ProductionRuntimePermissionError("permission_expired")

    if (
        load_runtime_permission_consume_record(
            normalized_permission_id, store_dir=consume_store_dir
        )
        is not None
    ):
        raise ProductionRuntimePermissionError("permission_already_consumed")

    from agent.coo.production_runtime_consume_store import (
        OneShotConsumeWriteConflict,
        write_once_consume_record,
    )

    payload = {
        "version": 1,
        "artifact_type": "runtime_permission_consume",
        "permission_id": normalized_permission_id,
        "activation_request_id": activation_request_id,
        "cutover_contract_id": record.cutover_contract_id,
        "ticket_id": record.ticket_id,
        "confirmation_id": record.confirmation_id,
        "consumed": True,
        "consumed_at": _utc_now_iso(now),
        "consumed_by": normalized_consumed_by,
        "governed_invoke_id": (governed_invoke_id or "").strip(),
    }
    path = _permission_consume_path(normalized_permission_id, store_dir=consume_store_dir)
    try:
        write_once_consume_record(path, payload)
    except OneShotConsumeWriteConflict as exc:
        raise ProductionRuntimePermissionError("permission_already_consumed") from exc
    return payload


def build_production_runtime_permission_release_summary(
    summary: ProductionRuntimePermissionSummary,
) -> ProductionRuntimePermissionReleaseSummary:
    if summary.permission_state == PERMISSION_ISSUED and summary.controlled_window_open:
        release_status = RELEASE_RUNTIME_PERMISSION_ISSUED
        next_phase = _NEXT_PHASE_15D
    elif summary.permission_state == PERMISSION_EXPIRED:
        release_status = RELEASE_RUNTIME_PERMISSION_EXPIRED
        next_phase = ""
    elif summary.recovery_required or summary.repair_lock_held:
        release_status = RELEASE_RUNTIME_PERMISSION_RECOVERY_REQUIRED
        next_phase = ""
    elif summary.permission_ready or summary.permission_state == PERMISSION_READY:
        release_status = RELEASE_RUNTIME_PERMISSION_READY_TO_ISSUE
        next_phase = ""
    else:
        release_status = RELEASE_RUNTIME_PERMISSION_NOT_READY
        next_phase = ""

    return ProductionRuntimePermissionReleaseSummary(
        activation_request_id=summary.activation_request_id,
        cutover_contract_id=summary.cutover_contract_id,
        permission_id=summary.permission_id,
        controlled_window_state=summary.controlled_window_state,
        permission_state=summary.permission_state,
        permission_ready=summary.permission_ready,
        permission_present=summary.permission_present,
        permission_expired=summary.permission_state == PERMISSION_EXPIRED,
        production_execution_allowed=False,
        cutover_started=False,
        runtime_invoked=False,
        production_root_hard_deny=True,
        original_repository2_execution_enabled=False,
        external_publish_enabled=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        next_phase=next_phase,
        release_status=release_status,
    )


def resolve_latest_runtime_permission_dashboard_digest(
    *,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> ProductionRuntimePermissionDashboardDigest:
    base = (governed_cutover_store_dir or default_governed_cutover_store_dir()).resolve()
    if not base.is_dir():
        return ProductionRuntimePermissionDashboardDigest(
            runtime_permission_state="not_configured",
            runtime_permission_ready=False,
            runtime_permission_present=False,
            runtime_permission_expired=False,
            runtime_permission_id="",
            runtime_permission_expires_at="",
            runtime_permission_blocking_count=0,
            runtime_permission_warning_count=0,
            runtime_permission_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )
    paths = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:500]:
        activation_id = path.stem
        try:
            summary = evaluate_production_runtime_permission(
                activation_request_id=activation_id,
                governed_cutover_store_dir=governed_cutover_store_dir,
                window_store_dir=window_store_dir,
                permission_store_dir=permission_store_dir,
                final_signoff_store_dir=final_signoff_store_dir,
                store_dir=store_dir,
                reservation_dir=reservation_dir,
                merged_config=merged_config,
                **{
                    k: v
                    for k, v in kwargs.items()
                    if k
                    in {
                        "runtime_history_dir",
                        "evidence_dir",
                        "audit_dir",
                        "bundle_dir",
                        "confirmation_dir",
                        "transaction_dir",
                        "e2e_history_dir",
                        "signoff_store_dir",
                        "validation_store_dir",
                        "preflight_history_dir",
                        "repo_root",
                        "now",
                    }
                },
            )
        except (ProductionRuntimePermissionError, Exception):
            continue
        if not summary.governed_cutover_contract_valid and not summary.permission_present:
            continue
        return ProductionRuntimePermissionDashboardDigest(
            runtime_permission_state=summary.permission_state,
            runtime_permission_ready=summary.permission_ready,
            runtime_permission_present=summary.permission_present,
            runtime_permission_expired=summary.permission_state == PERMISSION_EXPIRED,
            runtime_permission_id=summary.permission_id,
            runtime_permission_expires_at=summary.expires_at,
            runtime_permission_blocking_count=len(summary.blocking_items),
            runtime_permission_warning_count=len(summary.warning_items),
            runtime_permission_recommended_action=summary.recommended_action,
        )
    return ProductionRuntimePermissionDashboardDigest(
        runtime_permission_state="not_configured",
        runtime_permission_ready=False,
        runtime_permission_present=False,
        runtime_permission_expired=False,
        runtime_permission_id="",
        runtime_permission_expires_at="",
        runtime_permission_blocking_count=0,
        runtime_permission_warning_count=0,
        runtime_permission_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
    )


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "production_execution_allowed: false",
        "cutover_started: false",
        "runtime_invoked: false",
        "production_root_hard_deny: true",
        "original_repository2_execution_attempted: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
        "external_publish_enabled: false",
        "consumed: false",
        "revoked: false",
        "original_repository2_not_executed",
        "executor_assigned: true",
        "executor_assigned: false",
        "operator_present: true",
        "operator_present: false",
        "identity_separation_valid: true",
        "identity_separation_valid: false",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    for label in (
        "executor_assigned",
        "operator_present",
        "operator_identity_valid",
        "executor_identity_valid",
        "identity_separation_valid",
        "original_repository2_not_executed",
        "original_repository2_execution_attempted",
        "runtime_permission",
    ):
        lowered = lowered.replace(label, "")
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionRuntimePermissionError(
                f"Unsafe runtime permission output field: {token!r}"
            )


def format_production_runtime_permission_status(
    summary: ProductionRuntimePermissionSummary,
) -> str:
    lines = [
        "Production Runtime Permission Status",
        "",
        f"activation_request_id: {summary.activation_request_id}",
        f"cutover_contract_id: {summary.cutover_contract_id or '(none)'}",
        f"permission_id: {summary.permission_id or '(none)'}",
        f"permission_state: {summary.permission_state}",
        f"permission_ready: {str(summary.permission_ready).lower()}",
        f"permission_present: {str(summary.permission_present).lower()}",
        f"controlled_window_state: {summary.controlled_window_state or '(none)'}",
        f"controlled_window_open: {str(summary.controlled_window_open).lower()}",
        f"controlled_window_expired: {str(summary.controlled_window_expired).lower()}",
        "governed_cutover_contract_valid: "
        f"{str(summary.governed_cutover_contract_valid).lower()}",
        f"governed_cutover_status: {summary.governed_cutover_status or '(none)'}",
        f"final_signoff_valid: {str(summary.final_signoff_valid).lower()}",
        f"rollback_ready: {str(summary.rollback_ready).lower()}",
        "operational_signoff_valid: "
        f"{str(summary.operational_signoff_valid).lower()}",
        f"audit_chain_complete: {str(summary.audit_chain_complete).lower()}",
        f"recovery_required: {str(summary.recovery_required).lower()}",
        f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
        f"executor_identity_valid: {str(summary.executor_identity_valid).lower()}",
        f"operator_identity_valid: {str(summary.operator_identity_valid).lower()}",
        "identity_separation_valid: "
        f"{str(summary.identity_separation_valid).lower()}",
        f"one_shot_scope_valid: {str(summary.one_shot_scope_valid).lower()}",
        f"ticket_scope_valid: {str(summary.ticket_scope_valid).lower()}",
        f"window_scope_valid: {str(summary.window_scope_valid).lower()}",
        f"ttl_valid: {str(summary.ttl_valid).lower()}",
        f"ttl_seconds: {summary.ttl_seconds}",
        f"window_remaining_seconds: {summary.window_remaining_seconds}",
        f"issued_at: {summary.issued_at or '(none)'}",
        f"expires_at: {summary.expires_at or '(none)'}",
        "consumed: false",
        "revoked: false",
        f"blocking_items_count: {len(summary.blocking_items)}",
        f"warning_items_count: {len(summary.warning_items)}",
        "blocking_items: "
        f"{', '.join(summary.blocking_items) if summary.blocking_items else '(none)'}",
        "warning_items: "
        f"{', '.join(summary.warning_items) if summary.warning_items else '(none)'}",
        f"recommended_action: {summary.recommended_action}",
        f"executor_assigned: {str(summary.executor_assigned).lower()}",
        f"operator_present: {str(summary.operator_present).lower()}",
        f"tested_commit_sha: {summary.tested_commit_sha_short or '(none)'}",
        f"release_tag: {summary.release_tag or '(none)'}",
        f"already_issued: {str(summary.already_issued).lower()}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "production_root_hard_deny: true",
        "cutover_started: false",
        "runtime_invoked: false",
        "original_repository2_execution_attempted: false",
        "external_publish_enabled: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_runtime_permission_record(
    record: ProductionRuntimePermissionRecord,
    *,
    now: datetime | None = None,
) -> str:
    expires_dt = _parse_iso(record.expires_at)
    expired = bool(expires_dt and _utc_now(now) >= expires_dt)
    state = PERMISSION_EXPIRED if expired else PERMISSION_ISSUED
    lines = [
        "Production Runtime Permission",
        "",
        f"permission_id: {record.permission_id}",
        f"activation_request_id: {record.activation_request_id}",
        f"cutover_contract_id: {record.cutover_contract_id}",
        f"reservation_id: {record.reservation_id}",
        f"execution_attempt_id: {record.execution_attempt_id}",
        f"dispatch_run_id: {record.dispatch_run_id}",
        f"permission_status: {state}",
        f"scope_type: {record.scope_type}",
        "max_executions: 1",
        "execution_count: 0",
        f"ttl_seconds: {record.ttl_seconds}",
        f"issued_at: {record.issued_at}",
        f"expires_at: {record.expires_at}",
        "consumed: false",
        "revoked: false",
        "executor_assigned: true",
        "operator_present: true",
        f"tested_commit_sha: {_short_sha(record.tested_commit_sha) or '(none)'}",
        f"release_tag: {record.release_tag or '(none)'}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "production_root_hard_deny: true",
        "cutover_started: false",
        "runtime_invoked: false",
        "original_repository2_execution_attempted: false",
        "external_publish_enabled: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_runtime_permission_history(
    activation_request_id: str,
    events: tuple[ProductionRuntimePermissionEvent, ...],
    *,
    permission_id: str = "",
) -> str:
    lines = [
        "Production Runtime Permission History",
        "",
        f"activation_request_id: {activation_request_id}",
        f"permission_id: {permission_id or '(none)'}",
        f"event_count: {len(events)}",
        "",
    ]
    for index, event in enumerate(events, start=1):
        lines.extend(
            [
                f"event_{index}_id: {event.event_id}",
                f"event_{index}_type: {event.event_type}",
                f"event_{index}_actor_role: {event.actor_role or '(none)'}",
                f"event_{index}_reason_code: {event.reason_code or '(none)'}",
                f"event_{index}_occurred_at: {event.occurred_at}",
                "",
            ]
        )
    lines.extend(
        [
            "[Safety]",
            "production_execution_allowed: false",
            "cutover_started: false",
            "runtime_invoked: false",
            "production_root_hard_deny: true",
        ]
    )
    output = "\n".join(lines).rstrip()
    _assert_safe_output(output)
    return output


def run_production_runtime_permission_status(
    *,
    activation_request_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_runtime_permission(
            activation_request_id=activation_request_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionRuntimePermissionError:
        return "error: runtime permission status unavailable", 1
    return format_production_runtime_permission_status(summary), 0


def run_production_runtime_permission_check(
    *,
    activation_request_id: str,
    executor_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
    ttl_seconds: int = 300,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_runtime_permission(
            activation_request_id=activation_request_id,
            executor_id=executor_id,
            ttl_seconds=ttl_seconds,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionRuntimePermissionError:
        return "error: runtime permission check unavailable", 1
    exit_code = 0 if summary.permission_ready or summary.permission_state == PERMISSION_READY else 1
    return format_production_runtime_permission_status(summary), exit_code


def run_production_runtime_permission_issue(
    *,
    activation_request_id: str,
    executor_id: str,
    operator_id: str,
    ttl_seconds: int,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = issue_production_runtime_permission(
            activation_request_id=activation_request_id,
            executor_id=executor_id,
            operator_id=operator_id,
            ttl_seconds=ttl_seconds,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionRuntimePermissionError:
        try:
            summary = evaluate_production_runtime_permission(
                activation_request_id=activation_request_id,
                executor_id=executor_id,
                operator_id=operator_id,
                ttl_seconds=ttl_seconds,
                merged_config=merged_config,
                repo_root=repo_root,
            )
            return format_production_runtime_permission_status(summary), 1
        except ProductionRuntimePermissionError:
            return "error: runtime permission issue failed", 1
    exit_code = (
        0
        if summary.permission_state == PERMISSION_ISSUED or summary.already_issued
        else 1
    )
    return format_production_runtime_permission_status(summary), exit_code


def run_production_runtime_permission_show(
    *,
    permission_id: str,
) -> tuple[str, int]:
    try:
        record = load_runtime_permission_by_id(permission_id)
    except ProductionRuntimePermissionError:
        return "error: runtime permission corrupted", 1
    if record is None:
        return "error: runtime permission not found", 1
    return format_production_runtime_permission_record(record), 0


def run_production_runtime_permission_history(
    *,
    activation_request_id: str,
) -> tuple[str, int]:
    try:
        record = load_runtime_permission_record(activation_request_id)
        events = load_runtime_permission_events(activation_request_id)
    except ProductionRuntimePermissionError:
        return "error: runtime permission history unavailable", 1
    return (
        format_production_runtime_permission_history(
            activation_request_id,
            events,
            permission_id=record.permission_id if record else "",
        ),
        0,
    )
