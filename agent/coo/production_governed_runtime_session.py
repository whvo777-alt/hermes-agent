"""Governed runtime session contract — Phase 15D.

One-shot append-only runtime session prerequisite bound to an issued,
unconsumed runtime permission and an open controlled window. Starting a
session is NOT cutover, NOT runtime invocation, and NOT permission
consumption — those remain exclusively Phase 15E's concern.

Invariants enforced everywhere in this module:
    - production_execution_allowed is always False in every output.
    - cutover_started is always False in every output.
    - runtime_invoked is always False in every output.
    - permission_consumed and permission_revoked are always False — this
      module never consumes or revokes a runtime permission.
    - No subprocess, no bounded runner, no Repository2 execution.
    - Session start != cutover started != runtime invoked != permission
      consumed. These are four distinct, sequential gates and this module
      only ever advances the first one.
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
from agent.coo.production_activation_kill_switch import probe_audit_store_available
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
from agent.coo.production_runtime_permission import (
    PERMISSION_CONSUMED,
    PERMISSION_EXPIRED,
    PERMISSION_ISSUED,
    PERMISSION_REVOKED,
    ProductionRuntimePermissionError,
    load_runtime_permission_record,
)
from hermes_constants import get_hermes_home

_SESSION_STORE_DIR = "production-governed-runtime-session"
_SESSION_STORE_VERSION = 1
_NEXT_PHASE_15E = "Phase_15E_runtime_boundary"

SESSION_NOT_STARTED = "SESSION_NOT_STARTED"
SESSION_READY = "SESSION_READY"
SESSION_STARTED = "SESSION_STARTED"
SESSION_EXPIRED = "SESSION_EXPIRED"
SESSION_BLOCKED = "SESSION_BLOCKED"
SESSION_CLOSED = "SESSION_CLOSED"
SESSION_ABORTED = "SESSION_ABORTED"

SCOPE_TYPE_ONE_SHOT = "one_shot"
MIN_SESSION_TTL_SECONDS = 30
MAX_SESSION_TTL_SECONDS = 300
MIN_TTL_SECONDS = MIN_SESSION_TTL_SECONDS
MAX_TTL_SECONDS = MAX_SESSION_TTL_SECONDS

EVENT_SESSION_START_REQUESTED = "session_start_requested"
EVENT_SESSION_STARTED = "session_started"
EVENT_SESSION_START_BLOCKED = "session_start_blocked"
EVENT_RUNTIME_BOUNDARY_BLOCKED = "runtime_boundary_blocked_waiting_phase_15e"

RELEASE_GOVERNED_RUNTIME_SESSION_READY_TO_START = (
    "GOVERNED_RUNTIME_SESSION_READY_TO_START"
)
RELEASE_GOVERNED_RUNTIME_SESSION_STARTED = "GOVERNED_RUNTIME_SESSION_STARTED"
RELEASE_GOVERNED_RUNTIME_SESSION_EXPIRED = "GOVERNED_RUNTIME_SESSION_EXPIRED"
RELEASE_GOVERNED_RUNTIME_SESSION_NOT_READY = "GOVERNED_RUNTIME_SESSION_NOT_READY"
RELEASE_GOVERNED_RUNTIME_SESSION_RECOVERY_REQUIRED = (
    "GOVERNED_RUNTIME_SESSION_RECOVERY_REQUIRED"
)

ACTION_START_GOVERNED_RUNTIME_SESSION = "start_governed_runtime_session"
ACTION_GOVERNED_RUNTIME_SESSION_STARTED_WAIT_FOR_PHASE_15E = (
    "governed_runtime_session_started_wait_for_phase_15e"
)
ACTION_REVIEW_SESSION_WARNINGS = "review_session_warnings"
ACTION_WAIT_FOR_WINDOW_OPEN = "wait_for_window_open"
ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT = "prepare_new_governed_cutover_contract"
ACTION_RESOLVE_RUNTIME_PERMISSION = "resolve_runtime_permission"
ACTION_RESOLVE_IDENTITY_SEPARATION = "resolve_identity_separation"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_CLOSE_OR_EMERGENCY_CLOSE_WINDOW = "close_or_emergency_close_window"
ACTION_RESOLVE_KILL_SWITCH = "resolve_kill_switch"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_PREPARE_PHASE_15E_RUNTIME_BOUNDARY = "prepare_phase_15e_runtime_boundary"

BLOCK_GOVERNED_CUTOVER_CONTRACT_MISSING = "governed_cutover_contract_missing"
BLOCK_GOVERNED_CUTOVER_CONTRACT_INVALID = "governed_cutover_contract_invalid"
BLOCK_CONTROLLED_WINDOW_NOT_OPEN = "controlled_window_not_open"
BLOCK_CONTROLLED_WINDOW_EXPIRED = "controlled_window_expired"
BLOCK_CONTROLLED_WINDOW_CLOSED = "controlled_window_closed"
BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED = "controlled_window_emergency_closed"
BLOCK_RUNTIME_PERMISSION_MISSING = "runtime_permission_missing"
BLOCK_RUNTIME_PERMISSION_INVALID = "runtime_permission_invalid"
BLOCK_RUNTIME_PERMISSION_EXPIRED = "runtime_permission_expired"
BLOCK_RUNTIME_PERMISSION_CONSUMED = "runtime_permission_consumed"
BLOCK_RUNTIME_PERMISSION_REVOKED = "runtime_permission_revoked"
BLOCK_PERMISSION_SCOPE_MISMATCH = "permission_scope_mismatch"
BLOCK_PERMISSION_EXECUTOR_MISMATCH = "permission_executor_mismatch"
BLOCK_SESSION_ALREADY_EXISTS = "session_already_exists"
BLOCK_SESSION_EXPIRED = "session_expired"
BLOCK_GOVERNED_RUNTIME_SESSION_CONFLICT = "governed_runtime_session_conflict"
BLOCK_EXECUTOR_IDENTITY_INVALID = "executor_identity_invalid"
BLOCK_OPERATOR_IDENTITY_INVALID = "operator_identity_invalid"
BLOCK_IDENTITY_SEPARATION_INVALID = "identity_separation_invalid"
BLOCK_SESSION_TTL_INVALID = "session_ttl_invalid"
BLOCK_SESSION_TTL_EXCEEDS_PERMISSION = "session_ttl_exceeds_permission"
BLOCK_SESSION_TTL_EXCEEDS_WINDOW = "session_ttl_exceeds_window"
BLOCK_ONE_SHOT_SCOPE_INVALID = "one_shot_scope_invalid"
BLOCK_TICKET_SCOPE_INVALID = "ticket_scope_invalid"
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
BLOCK_KILL_SWITCH_UNAVAILABLE = "kill_switch_unavailable"
BLOCK_EMERGENCY_CLOSE_UNAVAILABLE = "emergency_close_unavailable"
BLOCK_SOURCE_TREE_MUTATED = "source_tree_mutated"
BLOCK_PRODUCTION_ROOT_TOUCHED = "production_root_touched"
BLOCK_EXTERNAL_PUBLISH_ENABLED = "external_publish_enabled"
BLOCK_GATEWAY_PRODUCTION_ENABLED = "gateway_production_enabled"
BLOCK_DISCORD_PRODUCTION_ENABLED = "discord_production_enabled"
BLOCK_PRODUCTION_EXECUTION_ENABLED = "production_execution_enabled"
BLOCK_CUTOVER_ALREADY_STARTED = "cutover_already_started"
BLOCK_RUNTIME_ALREADY_INVOKED = "runtime_already_invoked"
BLOCK_SESSION_STORE_CORRUPTED = "session_store_corrupted"
BLOCK_SESSION_WRITE_FAILED = "session_write_failed"
BLOCK_UNSAFE_OUTPUT = "unsafe_output"

WARN_SESSION_IS_PREREQUISITE_ONLY = "session_is_prerequisite_only"
WARN_RUNTIME_NOT_INVOKED = "runtime_not_invoked"
WARN_CUTOVER_NOT_STARTED = "cutover_not_started"
WARN_PERMISSION_NOT_CONSUMED = "permission_not_consumed"
WARN_PRODUCTION_EXECUTION_DISABLED = "production_execution_disabled"
WARN_PRODUCTION_ROOT_HARD_DENIED = "production_root_hard_denied"
WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED = "original_repository2_not_executed"
WARN_EXTERNAL_PUBLISH_DISABLED = "external_publish_disabled"
WARN_GATEWAY_PRODUCTION_DISABLED = "gateway_production_disabled"
WARN_DISCORD_PRODUCTION_DISABLED = "discord_production_disabled"
WARN_ONE_SHOT_ONLY = "one_shot_only"
WARN_OPERATOR_SUPERVISION_REQUIRED = "operator_supervision_required"
WARN_SESSION_EXPIRY_REQUIRES_NEW_CONTRACT = "session_expiry_requires_new_contract"
WARN_RUNTIME_BOUNDARY_BLOCKED_WAITING_PHASE_15E = (
    "runtime_boundary_blocked_waiting_phase_15e"
)

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


class ProductionGovernedRuntimeSessionError(ValueError):
    """Raised when governed runtime session assessment or start fails safely."""


@dataclass(frozen=True)
class ProductionGovernedRuntimeSessionRecord:
    session_id: str
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    ticket_id: str
    confirmation_id: str
    controlled_window_open_event_id: str
    executor_id: str
    operator_id: str
    started_by: str
    started_at: str
    expires_at: str
    ttl_seconds: int
    scope_type: str
    session_status: str
    tested_commit_sha: str
    release_tag: str
    runtime_invoked_at: str = ""
    closed: bool = False
    closed_at: str = ""
    aborted: bool = False
    aborted_at: str = ""
    abort_reason_code: str = ""
    production_execution_allowed: bool = False
    production_root_hard_deny: bool = True
    cutover_started: bool = False
    runtime_invoked: bool = False
    permission_consumed: bool = False
    permission_revoked: bool = False
    original_repository2_execution_attempted: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    external_publish_enabled: bool = False


@dataclass(frozen=True)
class ProductionGovernedRuntimeSessionEvent:
    event_id: str
    session_id: str
    activation_request_id: str
    event_type: str
    actor_role: str
    reason_code: str
    occurred_at: str


@dataclass(frozen=True)
class ProductionGovernedRuntimeSessionSummary:
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    session_state: str
    session_ready: bool
    session_present: bool
    controlled_window_state: str
    controlled_window_open: bool
    controlled_window_expired: bool
    governed_cutover_contract_valid: bool
    governed_cutover_status: str
    runtime_permission_valid: bool
    runtime_permission_state: str
    permission_valid: bool
    permission_state: str
    permission_expired: bool
    permission_scope_valid: bool
    session_scope_valid: bool
    session_ttl_valid: bool
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
    kill_switch_available: bool
    emergency_close_available: bool
    session_started_at: str
    session_expires_at: str
    started_at: str
    expires_at: str
    session_started: bool
    session_expired: bool
    production_execution_allowed: bool
    cutover_started: bool
    runtime_invoked: bool
    permission_consumed: bool
    permission_revoked: bool
    production_root_hard_deny: bool
    original_repository2_execution_attempted: bool
    external_publish_enabled: bool
    gateway_production_enabled: bool
    discord_production_enabled: bool
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str
    ttl_seconds: int = 0
    already_started: bool = False
    executor_assigned: bool = False
    operator_present: bool = False
    tested_commit_sha_short: str = ""
    release_tag: str = ""
    window_remaining_seconds: int = 0
    permission_remaining_seconds: int = 0


@dataclass(frozen=True)
class ProductionGovernedRuntimeSessionReleaseSummary:
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    controlled_window_state: str
    permission_state: str
    session_state: str
    session_ready: bool
    session_present: bool
    session_expired: bool
    production_execution_allowed: bool = False
    cutover_started: bool = False
    runtime_invoked: bool = False
    permission_consumed: bool = False
    permission_revoked: bool = False
    production_root_hard_deny: bool = True
    original_repository2_execution_enabled: bool = False
    external_publish_enabled: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    next_phase: str = ""
    release_status: str = RELEASE_GOVERNED_RUNTIME_SESSION_NOT_READY


@dataclass(frozen=True)
class ProductionGovernedRuntimeSessionDashboardDigest:
    governed_runtime_session_state: str
    governed_runtime_session_ready: bool
    governed_runtime_session_present: bool
    governed_runtime_session_expired: bool
    governed_runtime_session_id: str
    governed_runtime_session_expires_at: str
    governed_runtime_session_blocking_count: int
    governed_runtime_session_warning_count: int
    governed_runtime_session_recommended_action: str


def default_governed_runtime_session_store_dir() -> Path:
    return get_hermes_home() / "coo" / _SESSION_STORE_DIR


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


def _session_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionGovernedRuntimeSessionError("activation_request_id is required")
    base = (store_dir or default_governed_runtime_session_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionGovernedRuntimeSessionError(
            "Governed runtime session store must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_governed_runtime_session_store_available(
    *, store_dir: Path | None = None
) -> bool:
    try:
        base = (store_dir or default_governed_runtime_session_store_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _record_from_dict(
    payload: Mapping[str, Any]
) -> ProductionGovernedRuntimeSessionRecord:
    return ProductionGovernedRuntimeSessionRecord(
        session_id=str(payload.get("session_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        cutover_contract_id=str(payload.get("cutover_contract_id", "")),
        permission_id=str(payload.get("permission_id", "")),
        reservation_id=str(payload.get("reservation_id", "")),
        execution_attempt_id=str(payload.get("execution_attempt_id", "")),
        dispatch_run_id=str(payload.get("dispatch_run_id", "")),
        ticket_id=str(payload.get("ticket_id", "")),
        confirmation_id=str(payload.get("confirmation_id", "")),
        controlled_window_open_event_id=str(
            payload.get("controlled_window_open_event_id", "")
        ),
        executor_id=str(payload.get("executor_id", "")),
        operator_id=str(payload.get("operator_id") or payload.get("started_by", "")),
        started_by=str(payload.get("started_by") or payload.get("operator_id", "")),
        started_at=str(payload.get("started_at", "")),
        expires_at=str(payload.get("expires_at", "")),
        ttl_seconds=int(payload.get("ttl_seconds") or 0),
        scope_type=str(payload.get("scope_type") or SCOPE_TYPE_ONE_SHOT),
        session_status=str(payload.get("session_status", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        runtime_invoked_at=str(payload.get("runtime_invoked_at", "")),
        closed=bool(payload.get("closed", False)),
        closed_at=str(payload.get("closed_at", "")),
        aborted=bool(payload.get("aborted", False)),
        aborted_at=str(payload.get("aborted_at", "")),
        abort_reason_code=str(payload.get("abort_reason_code", "")),
        production_execution_allowed=False,
        production_root_hard_deny=True,
        cutover_started=False,
        runtime_invoked=False,
        permission_consumed=False,
        permission_revoked=False,
        original_repository2_execution_attempted=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        external_publish_enabled=False,
    )


def _record_to_dict(
    record: ProductionGovernedRuntimeSessionRecord,
) -> dict[str, Any]:
    return {
        "session_id": record.session_id,
        "activation_request_id": record.activation_request_id,
        "cutover_contract_id": record.cutover_contract_id,
        "permission_id": record.permission_id,
        "reservation_id": record.reservation_id,
        "execution_attempt_id": record.execution_attempt_id,
        "dispatch_run_id": record.dispatch_run_id,
        "ticket_id": record.ticket_id,
        "confirmation_id": record.confirmation_id,
        "controlled_window_open_event_id": record.controlled_window_open_event_id,
        "executor_id": record.executor_id,
        "operator_id": record.operator_id,
        "started_by": record.started_by or record.operator_id,
        "started_at": record.started_at,
        "expires_at": record.expires_at,
        "ttl_seconds": record.ttl_seconds,
        "scope_type": SCOPE_TYPE_ONE_SHOT,
        "session_status": SESSION_STARTED,
        "tested_commit_sha": _short_sha(record.tested_commit_sha),
        "release_tag": record.release_tag,
        "runtime_invoked_at": "",
        "closed": False,
        "closed_at": "",
        "aborted": False,
        "aborted_at": "",
        "abort_reason_code": "",
        "production_execution_allowed": False,
        "production_root_hard_deny": True,
        "cutover_started": False,
        "runtime_invoked": False,
        "permission_consumed": False,
        "permission_revoked": False,
        "original_repository2_execution_attempted": False,
        "gateway_production_enabled": False,
        "discord_production_enabled": False,
        "external_publish_enabled": False,
    }


def load_governed_runtime_session_record(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionGovernedRuntimeSessionRecord | None:
    path = _session_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionGovernedRuntimeSessionError("session_store_corrupted") from exc
    session = payload.get("session")
    if not isinstance(session, dict):
        raise ProductionGovernedRuntimeSessionError("session_store_corrupted")
    return _record_from_dict(session)


def load_governed_runtime_session_by_id(
    session_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionGovernedRuntimeSessionRecord | None:
    target = (session_id or "").strip()
    if not target:
        raise ProductionGovernedRuntimeSessionError("session_id is required")
    base = (store_dir or default_governed_runtime_session_store_dir()).resolve()
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductionGovernedRuntimeSessionError(
                "session_store_corrupted"
            ) from exc
        session = payload.get("session")
        if isinstance(session, dict) and str(session.get("session_id", "")) == target:
            return _record_from_dict(session)
    return None


def load_governed_runtime_session_events(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> tuple[ProductionGovernedRuntimeSessionEvent, ...]:
    path = _session_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionGovernedRuntimeSessionError("session_store_corrupted") from exc
    raw = payload.get("events") or []
    if not isinstance(raw, list):
        raise ProductionGovernedRuntimeSessionError("session_store_corrupted")
    events: list[ProductionGovernedRuntimeSessionEvent] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ProductionGovernedRuntimeSessionError("session_store_corrupted")
        event_id = str(item.get("event_id", ""))
        if not event_id or event_id in seen:
            raise ProductionGovernedRuntimeSessionError("session_store_corrupted")
        seen.add(event_id)
        events.append(
            ProductionGovernedRuntimeSessionEvent(
                event_id=event_id,
                session_id=str(item.get("session_id", "")),
                activation_request_id=str(item.get("activation_request_id", "")),
                event_type=str(item.get("event_type", "")),
                actor_role=str(item.get("actor_role", "")),
                reason_code=str(item.get("reason_code", "")),
                occurred_at=str(item.get("occurred_at", "")),
            )
        )
    return tuple(events)


def load_governed_runtime_session_events_by_session_id(
    session_id: str,
    *,
    store_dir: Path | None = None,
) -> tuple[ProductionGovernedRuntimeSessionRecord | None, tuple[ProductionGovernedRuntimeSessionEvent, ...]]:
    """Scan the whole store for the bundle whose session matches `session_id`."""
    target = (session_id or "").strip()
    if not target:
        raise ProductionGovernedRuntimeSessionError("session_id is required")
    base = (store_dir or default_governed_runtime_session_store_dir()).resolve()
    if not base.is_dir():
        return None, ()
    for path in sorted(base.glob("*.json")):
        activation_id = path.stem
        record = load_governed_runtime_session_record(activation_id, store_dir=store_dir)
        if record is not None and record.session_id == target:
            events = load_governed_runtime_session_events(activation_id, store_dir=store_dir)
            return record, events
    return None, ()


def _write_session_bundle(
    record: ProductionGovernedRuntimeSessionRecord,
    events: tuple[ProductionGovernedRuntimeSessionEvent, ...],
    *,
    store_dir: Path | None = None,
) -> None:
    path = _session_path(record.activation_request_id, store_dir=store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    payload = {
        "version": _SESSION_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "session": _record_to_dict(record),
        "events": [
            {
                "event_id": event.event_id,
                "session_id": event.session_id,
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
            existing = load_governed_runtime_session_record(
                record.activation_request_id,
                store_dir=store_dir,
            )
            if existing is not None:
                if _sessions_equivalent(existing, record):
                    return
                raise ProductionGovernedRuntimeSessionError(
                    "governed_runtime_session_conflict"
                )
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            try:
                fd = os.open(str(path), flags, 0o644)
            except FileExistsError as exc:
                existing_again = load_governed_runtime_session_record(
                    record.activation_request_id,
                    store_dir=store_dir,
                )
                if existing_again is not None and _sessions_equivalent(
                    existing_again, record
                ):
                    return
                raise ProductionGovernedRuntimeSessionError(
                    "governed_runtime_session_conflict"
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
        raise ProductionGovernedRuntimeSessionError("session_write_failed") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _sessions_equivalent(
    existing: ProductionGovernedRuntimeSessionRecord,
    candidate: ProductionGovernedRuntimeSessionRecord,
) -> bool:
    return (
        existing.cutover_contract_id == candidate.cutover_contract_id
        and existing.permission_id == candidate.permission_id
        and existing.executor_id == candidate.executor_id
        and existing.operator_id == candidate.operator_id
        and existing.ttl_seconds == candidate.ttl_seconds
        and existing.ticket_id == candidate.ticket_id
        and existing.confirmation_id == candidate.confirmation_id
        and existing.controlled_window_open_event_id
        == candidate.controlled_window_open_event_id
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
    permission_record,
) -> tuple[bool, bool, bool, list[str]]:
    """Validate provided identities only.

    Empty executor/operator (status path) does not append identity blocks.
    Check provides executor only; issue/start provides both.
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
        if request is not None:
            conflicts_for_operator.add((request.requested_by or "").strip())
        if permission_record is not None:
            conflicts_for_operator.add((permission_record.issued_by or "").strip())
        conflicts_for_operator.discard("")
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


def _validate_session_ttl(
    ttl_seconds: int | None,
    *,
    now: datetime,
    permission_expires_at: datetime | None,
    window_end: datetime | None,
) -> tuple[bool, str, int, int, int, str]:
    """Return (ttl_valid, invalid_reason, effective_ttl, permission_remaining,
    window_remaining, expires_at_iso).

    Fail-closed: a missing boundary (permission or window) is treated as zero
    remaining seconds, so a candidate ttl can never be accepted against an
    unknown boundary.
    """
    permission_remaining = (
        max(0, int((permission_expires_at - now).total_seconds()))
        if permission_expires_at is not None
        else 0
    )
    window_remaining = (
        max(0, int((window_end - now).total_seconds()))
        if window_end is not None
        else 0
    )
    if ttl_seconds is None:
        return True, "", 0, permission_remaining, window_remaining, ""
    remaining = min(permission_remaining, window_remaining)
    if remaining < MIN_SESSION_TTL_SECONDS:
        return (
            False,
            "insufficient_remaining",
            ttl_seconds,
            permission_remaining,
            window_remaining,
            "",
        )
    if ttl_seconds < MIN_SESSION_TTL_SECONDS or ttl_seconds > MAX_SESSION_TTL_SECONDS:
        return (
            False,
            "invalid_range",
            ttl_seconds,
            permission_remaining,
            window_remaining,
            "",
        )
    if ttl_seconds > permission_remaining:
        return (
            False,
            "exceeds_permission",
            ttl_seconds,
            permission_remaining,
            window_remaining,
            "",
        )
    if ttl_seconds > window_remaining:
        return (
            False,
            "exceeds_window",
            ttl_seconds,
            permission_remaining,
            window_remaining,
            "",
        )
    expires = now + timedelta(seconds=ttl_seconds)
    return (
        True,
        "",
        ttl_seconds,
        permission_remaining,
        window_remaining,
        expires.isoformat(),
    )


def _recommended_action(
    state: str,
    blocking: tuple[str, ...],
    *,
    window_open: bool,
    recovery: bool,
) -> str:
    if state == SESSION_STARTED:
        return ACTION_GOVERNED_RUNTIME_SESSION_STARTED_WAIT_FOR_PHASE_15E
    if state == SESSION_READY:
        return ACTION_START_GOVERNED_RUNTIME_SESSION
    if state == SESSION_EXPIRED:
        return ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT
    if recovery or BLOCK_RECOVERY_REQUIRED in blocking or BLOCK_REPAIR_LOCK_HELD in blocking:
        return ACTION_RUN_CONSUME_RECOVERY
    if (
        BLOCK_KILL_SWITCH_UNAVAILABLE in blocking
        or BLOCK_EMERGENCY_CLOSE_UNAVAILABLE in blocking
    ):
        return ACTION_RESOLVE_KILL_SWITCH
    if BLOCK_CONTROLLED_WINDOW_EXPIRED in blocking:
        return ACTION_CLOSE_OR_EMERGENCY_CLOSE_WINDOW
    if BLOCK_CONTROLLED_WINDOW_NOT_OPEN in blocking:
        return ACTION_WAIT_FOR_WINDOW_OPEN
    if (
        BLOCK_CONTROLLED_WINDOW_CLOSED in blocking
        or BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED in blocking
    ):
        return ACTION_CLOSE_OR_EMERGENCY_CLOSE_WINDOW
    if (
        BLOCK_EXECUTOR_IDENTITY_INVALID in blocking
        or BLOCK_OPERATOR_IDENTITY_INVALID in blocking
        or BLOCK_IDENTITY_SEPARATION_INVALID in blocking
    ):
        return ACTION_RESOLVE_IDENTITY_SEPARATION
    if (
        BLOCK_RUNTIME_PERMISSION_MISSING in blocking
        or BLOCK_RUNTIME_PERMISSION_INVALID in blocking
        or BLOCK_RUNTIME_PERMISSION_EXPIRED in blocking
        or BLOCK_RUNTIME_PERMISSION_CONSUMED in blocking
        or BLOCK_RUNTIME_PERMISSION_REVOKED in blocking
        or BLOCK_PERMISSION_SCOPE_MISMATCH in blocking
        or BLOCK_PERMISSION_EXECUTOR_MISMATCH in blocking
    ):
        return ACTION_RESOLVE_RUNTIME_PERMISSION
    if (
        BLOCK_SESSION_TTL_INVALID in blocking
        or BLOCK_SESSION_TTL_EXCEEDS_PERMISSION in blocking
        or BLOCK_SESSION_TTL_EXCEEDS_WINDOW in blocking
    ):
        return ACTION_RESOLVE_RUNTIME_PERMISSION
    if (
        BLOCK_FINAL_SIGNOFF_INVALID in blocking
        or BLOCK_ROLLBACK_VALIDATION_INVALID in blocking
        or BLOCK_OPERATIONAL_SIGNOFF_INVALID in blocking
        or BLOCK_GOVERNED_CUTOVER_CONTRACT_MISSING in blocking
        or BLOCK_GOVERNED_CUTOVER_CONTRACT_INVALID in blocking
    ):
        return ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT
    if BLOCK_SESSION_ALREADY_EXISTS in blocking:
        return ACTION_GOVERNED_RUNTIME_SESSION_STARTED_WAIT_FOR_PHASE_15E
    if BLOCK_GOVERNED_RUNTIME_SESSION_CONFLICT in blocking:
        return ACTION_MAINTAIN_PRODUCTION_BLOCK
    if not window_open:
        return ACTION_WAIT_FOR_WINDOW_OPEN
    return ACTION_MAINTAIN_PRODUCTION_BLOCK


def evaluate_production_governed_runtime_session(
    *,
    activation_request_id: str,
    executor_id: str = "",
    operator_id: str = "",
    permission_id: str = "",
    ttl_seconds: int | None = None,
    session_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
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
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    force_production_execution_allowed: bool | None = None,
    force_gateway_enabled: bool | None = None,
    force_discord_enabled: bool | None = None,
    force_cutover_started: bool | None = None,
    force_runtime_invoked: bool | None = None,
    force_permission_consumed: bool | None = None,
    force_permission_revoked: bool | None = None,
    force_kill_switch_unavailable: bool = False,
    force_emergency_close_unavailable: bool = False,
) -> ProductionGovernedRuntimeSessionSummary:
    """Read-only governed runtime session assessment."""
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

    permission_record = None
    try:
        permission_record = load_runtime_permission_record(
            activation_request_id,
            store_dir=permission_store_dir,
        )
    except ProductionRuntimePermissionError:
        blocking.append(BLOCK_SESSION_STORE_CORRUPTED)
        permission_record = None

    existing = None
    try:
        existing = load_governed_runtime_session_record(
            activation_request_id,
            store_dir=session_store_dir,
        )
    except ProductionGovernedRuntimeSessionError:
        blocking.append(BLOCK_SESSION_STORE_CORRUPTED)
        existing = None

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
            blocking.append(BLOCK_GOVERNED_CUTOVER_CONTRACT_INVALID)
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
        reservation is not None and reservation.state == RESERVATION_STATE_COMPLETED
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
            blocking.append(BLOCK_SESSION_STORE_CORRUPTED)

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

    ticket_scope_valid = bool(
        reservation and reservation.ticket_id and reservation.confirmation_id
    )
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

    # --- Kill switch / emergency close failsafe availability ---------------
    kill_switch_available = (
        request is not None
        and request.state == ACTIVATION_STATE_REVOKED
        and window.window_open
        and probe_audit_store_available(store_dir=store_dir)
    )
    if force_kill_switch_unavailable:
        kill_switch_available = False
    kill_switch_required = bool(
        request is not None
        and request.state == ACTIVATION_STATE_REVOKED
        and window.window_open
    )
    if kill_switch_required and not kill_switch_available:
        blocking.append(BLOCK_KILL_SWITCH_UNAVAILABLE)

    emergency_close_available = (
        window.window_open and rollback_ready and not window.emergency_closed
    )
    if force_emergency_close_unavailable:
        emergency_close_available = False
    emergency_close_required = bool(
        window.window_open and rollback_ready and not window.emergency_closed
    )
    if emergency_close_required and not emergency_close_available:
        blocking.append(BLOCK_EMERGENCY_CLOSE_UNAVAILABLE)

    # --- Runtime permission validation --------------------------------------
    runtime_permission_state = ""
    derived_permission_expired = False
    permission_consumed_flag = False
    permission_revoked_flag = False
    correlation_valid = True
    permission_expires_dt: datetime | None = None
    if permission_record is None:
        blocking.append(BLOCK_RUNTIME_PERMISSION_MISSING)
    else:
        runtime_permission_state = permission_record.permission_status
        if permission_record.permission_status != PERMISSION_ISSUED:
            blocking.append(BLOCK_RUNTIME_PERMISSION_INVALID)
        permission_expires_dt = _parse_iso(permission_record.expires_at)
        if permission_expires_dt is not None and current >= permission_expires_dt:
            derived_permission_expired = True
            runtime_permission_state = PERMISSION_EXPIRED
            blocking.append(BLOCK_RUNTIME_PERMISSION_EXPIRED)
        if permission_id and permission_record.permission_id != permission_id.strip():
            blocking.append(BLOCK_PERMISSION_SCOPE_MISMATCH)
        if executor_id and permission_record.executor_id != executor_id.strip():
            blocking.append(BLOCK_PERMISSION_EXECUTOR_MISMATCH)
        permission_consumed_flag = bool(permission_record.consumed) or bool(
            force_permission_consumed
        )
        permission_revoked_flag = bool(permission_record.revoked) or bool(
            force_permission_revoked
        )
        if permission_consumed_flag:
            blocking.append(BLOCK_RUNTIME_PERMISSION_CONSUMED)
            runtime_permission_state = PERMISSION_CONSUMED
        if permission_revoked_flag:
            blocking.append(BLOCK_RUNTIME_PERMISSION_REVOKED)
            runtime_permission_state = PERMISSION_REVOKED

        if (
            contract is not None
            and permission_record.cutover_contract_id
            and permission_record.cutover_contract_id != contract.cutover_contract_id
        ):
            correlation_valid = False
        if (
            open_event_id
            and permission_record.controlled_window_open_event_id
            and permission_record.controlled_window_open_event_id != open_event_id
        ):
            correlation_valid = False
        if reservation is not None:
            if (
                permission_record.ticket_id
                and reservation.ticket_id
                and permission_record.ticket_id != reservation.ticket_id
            ):
                correlation_valid = False
            if (
                permission_record.confirmation_id
                and reservation.confirmation_id
                and permission_record.confirmation_id != reservation.confirmation_id
            ):
                correlation_valid = False
        if not correlation_valid and BLOCK_RUNTIME_PERMISSION_INVALID not in blocking:
            blocking.append(BLOCK_RUNTIME_PERMISSION_INVALID)

    runtime_permission_valid = (
        permission_record is not None
        and permission_record.permission_status == PERMISSION_ISSUED
        and not derived_permission_expired
        and not permission_consumed_flag
        and not permission_revoked_flag
        and permission_record.max_executions == 1
        and permission_record.execution_count == 0
        and correlation_valid
        and (not permission_id or permission_record.permission_id == permission_id.strip())
        and (not executor_id or permission_record.executor_id == executor_id.strip())
    )
    permission_scope_valid = (
        permission_record is not None
        and correlation_valid
        and (
            not permission_id
            or permission_record.permission_id == permission_id.strip()
        )
    )
    session_scope_valid = (
        one_shot_scope_valid
        and ticket_scope_valid
        and permission_scope_valid
        and runtime_permission_valid
    )

    window_end = _parse_iso(window.maintenance_window_end)
    ttl_valid, ttl_reason, effective_ttl, permission_remaining, window_remaining, expires_iso = (
        _validate_session_ttl(
            ttl_seconds,
            now=current,
            permission_expires_at=permission_expires_dt,
            window_end=window_end,
        )
    )
    if ttl_seconds is not None and not ttl_valid:
        if ttl_reason == "exceeds_permission":
            blocking.append(BLOCK_SESSION_TTL_EXCEEDS_PERMISSION)
        elif ttl_reason == "exceeds_window":
            blocking.append(BLOCK_SESSION_TTL_EXCEEDS_WINDOW)
        else:
            blocking.append(BLOCK_SESSION_TTL_INVALID)

    executor_valid, operator_valid, separation_valid, id_blocks = _assess_identities(
        executor_id=executor_id,
        operator_id=operator_id,
        request=request,
        contract=contract,
        final_record=final_record,
        op_record=op_record,
        permission_record=permission_record,
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

    # --- Existing session idempotency / conflict ----------------------------
    session_id = ""
    started_at = ""
    expires_at = ""
    already_started = False
    session_present = existing is not None
    session_expired_flag = False
    session_conflict = False
    if existing is not None:
        session_id = existing.session_id
        started_at = existing.started_at
        expires_at = existing.expires_at
        existing_expires_dt = _parse_iso(existing.expires_at)
        if existing_expires_dt is not None and current >= existing_expires_dt:
            session_expired_flag = True
            blocking.append(BLOCK_SESSION_EXPIRED)
        else:
            already_started = True
            if permission_id or executor_id or operator_id or ttl_seconds is not None:
                equivalent = (
                    (not permission_id or existing.permission_id == permission_id.strip())
                    and (not executor_id or existing.executor_id == executor_id.strip())
                    and (not operator_id or existing.operator_id == operator_id.strip())
                    and (ttl_seconds is None or existing.ttl_seconds == ttl_seconds)
                )
                if not equivalent:
                    session_conflict = True
                    blocking.append(BLOCK_GOVERNED_RUNTIME_SESSION_CONFLICT)
            if not session_conflict:
                blocking.append(BLOCK_SESSION_ALREADY_EXISTS)

    unique_blocking = tuple(dict.fromkeys(blocking))

    excluded_when_no_ttl = {
        BLOCK_SESSION_TTL_INVALID,
        BLOCK_SESSION_TTL_EXCEEDS_PERMISSION,
        BLOCK_SESSION_TTL_EXCEEDS_WINDOW,
    }
    excluded_when_no_identity = {
        BLOCK_EXECUTOR_IDENTITY_INVALID,
        BLOCK_OPERATOR_IDENTITY_INVALID,
        BLOCK_IDENTITY_SEPARATION_INVALID,
    }
    hard_ready_blockers = [
        code
        for code in unique_blocking
        if code
        not in {
            BLOCK_SESSION_ALREADY_EXISTS,
        }
        and not (ttl_seconds is None and code in excluded_when_no_ttl)
        and not (not executor_id and not operator_id and code in excluded_when_no_identity)
        and not (not permission_id and code == BLOCK_PERMISSION_SCOPE_MISMATCH)
        and not (not executor_id and code == BLOCK_PERMISSION_EXECUTOR_MISMATCH)
    ]

    core_ready = (
        contract_valid
        and window_scope_valid
        and runtime_permission_valid
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
        and not (kill_switch_required and not kill_switch_available)
        and not (emergency_close_required and not emergency_close_available)
    )

    session_ready = (
        core_ready
        and executor_valid
        and operator_valid
        and separation_valid
        and (not permission_id or BLOCK_PERMISSION_SCOPE_MISMATCH not in unique_blocking)
        and (not executor_id or BLOCK_PERMISSION_EXECUTOR_MISMATCH not in unique_blocking)
        and ttl_seconds is not None
        and ttl_valid
        and not session_conflict
    )

    check_ready = (
        core_ready
        and bool((executor_id or "").strip())
        and executor_valid
        and bool((permission_id or "").strip())
        and BLOCK_PERMISSION_SCOPE_MISMATCH not in unique_blocking
        and BLOCK_PERMISSION_EXECUTOR_MISMATCH not in unique_blocking
        and (ttl_seconds is None or ttl_valid)
        and existing is None
    )

    if existing is not None and session_expired_flag:
        state = SESSION_EXPIRED
        ready = False
    elif existing is not None and session_conflict:
        state = SESSION_BLOCKED
        ready = False
    elif existing is not None and already_started:
        state = SESSION_STARTED
        ready = False
    elif recovery_required or repair_lock_held:
        state = SESSION_BLOCKED
        ready = False
    elif session_ready:
        state = SESSION_READY
        ready = True
    elif check_ready and not operator_id:
        state = SESSION_READY
        ready = True
    elif hard_ready_blockers and not check_ready:
        state = SESSION_BLOCKED
        ready = False
    else:
        state = SESSION_NOT_STARTED
        ready = False

    if existing is None and session_ready:
        state = SESSION_READY
        ready = True

    warnings.extend(
        [
            WARN_SESSION_IS_PREREQUISITE_ONLY,
            WARN_RUNTIME_NOT_INVOKED,
            WARN_CUTOVER_NOT_STARTED,
            WARN_PERMISSION_NOT_CONSUMED,
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
    if state == SESSION_EXPIRED:
        warnings.append(WARN_SESSION_EXPIRY_REQUIRES_NEW_CONTRACT)
    if state == SESSION_STARTED:
        warnings.append(WARN_RUNTIME_BOUNDARY_BLOCKED_WAITING_PHASE_15E)
    unique_warnings = tuple(dict.fromkeys(warnings))

    recommended = _recommended_action(
        state,
        unique_blocking,
        window_open=window.window_open,
        recovery=recovery_required or repair_lock_held,
    )

    return ProductionGovernedRuntimeSessionSummary(
        activation_request_id=activation_request_id,
        cutover_contract_id=cutover_contract_id,
        permission_id=permission_record.permission_id if permission_record else "",
        session_id=session_id,
        session_state=state,
        session_ready=ready,
        session_present=session_present,
        controlled_window_state=window.window_state,
        controlled_window_open=window.window_open,
        controlled_window_expired=window.expired,
        governed_cutover_contract_valid=contract_valid,
        governed_cutover_status=governed_status,
        runtime_permission_valid=runtime_permission_valid,
        runtime_permission_state=runtime_permission_state,
        permission_valid=runtime_permission_valid,
        permission_state=runtime_permission_state,
        permission_expired=(
            BLOCK_RUNTIME_PERMISSION_EXPIRED in unique_blocking
            or runtime_permission_state == PERMISSION_EXPIRED
        ),
        permission_scope_valid=permission_scope_valid,
        session_scope_valid=session_scope_valid,
        session_ttl_valid=ttl_valid if ttl_seconds is not None else True,
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
        kill_switch_available=kill_switch_available,
        emergency_close_available=emergency_close_available,
        session_started_at=started_at,
        session_expires_at=expires_at or expires_iso,
        started_at=started_at,
        expires_at=expires_at or expires_iso,
        session_started=already_started,
        session_expired=session_expired_flag,
        production_execution_allowed=False,
        cutover_started=False,
        runtime_invoked=False,
        permission_consumed=False,
        permission_revoked=False,
        production_root_hard_deny=True,
        original_repository2_execution_attempted=False,
        external_publish_enabled=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        blocking_items=unique_blocking,
        warning_items=unique_warnings,
        recommended_action=recommended,
        ttl_seconds=effective_ttl if ttl_seconds is not None else (existing.ttl_seconds if existing else 0),
        already_started=already_started,
        executor_assigned=bool((executor_id or "").strip())
        or (existing is not None and bool(existing.executor_id)),
        operator_present=bool((operator_id or "").strip())
        or (existing is not None and bool(existing.operator_id)),
        tested_commit_sha_short=_short_sha(
            request.tested_commit_sha if request is not None else ""
        ),
        release_tag=request.release_tag if request is not None else "",
        window_remaining_seconds=window_remaining,
        permission_remaining_seconds=permission_remaining,
    )


def start_production_governed_runtime_session(
    *,
    activation_request_id: str,
    executor_id: str,
    operator_id: str,
    permission_id: str,
    ttl_seconds: int,
    session_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
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
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionGovernedRuntimeSessionSummary:
    """Append-only start of a one-shot governed runtime session.

    Never starts cutover, never invokes runtime, never consumes the
    underlying permission — those remain exclusively Phase 15E's concern.
    """
    if not probe_governed_runtime_session_store_available(store_dir=session_store_dir):
        raise ProductionGovernedRuntimeSessionError("session_write_failed")

    def _evaluate() -> ProductionGovernedRuntimeSessionSummary:
        return evaluate_production_governed_runtime_session(
            activation_request_id=activation_request_id,
            executor_id=executor_id,
            operator_id=operator_id,
            permission_id=permission_id,
            ttl_seconds=ttl_seconds,
            session_store_dir=session_store_dir,
            permission_store_dir=permission_store_dir,
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
            now=now,
        )

    summary = _evaluate()

    existing = load_governed_runtime_session_record(
        activation_request_id,
        store_dir=session_store_dir,
    )
    if existing is not None:
        expires_dt = _parse_iso(existing.expires_at)
        if expires_dt is not None and _utc_now(now) >= expires_dt:
            raise ProductionGovernedRuntimeSessionError("session_expired")
        equivalent = (
            existing.permission_id == (permission_id or "").strip()
            and existing.executor_id == (executor_id or "").strip()
            and existing.operator_id == (operator_id or "").strip()
            and existing.ttl_seconds == int(ttl_seconds)
        )
        if equivalent:
            return _evaluate()
        raise ProductionGovernedRuntimeSessionError("governed_runtime_session_conflict")

    if summary.session_state != SESSION_READY:
        raise ProductionGovernedRuntimeSessionError(
            f"governed runtime session blocked for state {summary.session_state!r}"
        )

    contract = load_governed_cutover_contract(
        activation_request_id,
        store_dir=governed_cutover_store_dir,
    )
    permission_record = load_runtime_permission_record(
        activation_request_id,
        store_dir=permission_store_dir,
    )
    reservation = load_execution_reservation(
        activation_request_id,
        store_dir=reservation_dir,
    )
    if contract is None or permission_record is None or reservation is None:
        raise ProductionGovernedRuntimeSessionError("session_store_corrupted")
    _, window_events = load_window_lifecycle_events(
        activation_request_id,
        store_dir=window_store_dir,
    )
    open_event_id = _open_event_id(window_events)
    if not open_event_id:
        raise ProductionGovernedRuntimeSessionError("controlled_window_not_open")

    current = _utc_now(now)
    permission_expires_dt = _parse_iso(permission_record.expires_at)
    window_end = _parse_iso(contract.maintenance_window_end)
    ttl_valid, ttl_reason, effective_ttl, _, _, expires_iso = _validate_session_ttl(
        ttl_seconds,
        now=current,
        permission_expires_at=permission_expires_dt,
        window_end=window_end,
    )
    if not ttl_valid:
        raise ProductionGovernedRuntimeSessionError(
            ttl_reason and f"session_ttl_{ttl_reason}" or "session_ttl_invalid"
        )

    session_id = str(uuid.uuid4())
    started_at = _utc_now_iso(now)
    record = ProductionGovernedRuntimeSessionRecord(
        session_id=session_id,
        activation_request_id=activation_request_id,
        cutover_contract_id=contract.cutover_contract_id,
        permission_id=permission_record.permission_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=contract.execution_attempt_id,
        dispatch_run_id=contract.dispatch_run_id,
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        controlled_window_open_event_id=open_event_id,
        executor_id=(executor_id or "").strip(),
        operator_id=(operator_id or "").strip(),
        started_by=(operator_id or "").strip(),
        started_at=started_at,
        expires_at=expires_iso,
        ttl_seconds=effective_ttl,
        scope_type=SCOPE_TYPE_ONE_SHOT,
        session_status=SESSION_STARTED,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
    )
    events = (
        ProductionGovernedRuntimeSessionEvent(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_SESSION_START_REQUESTED,
            actor_role="operator",
            reason_code="",
            occurred_at=started_at,
        ),
        ProductionGovernedRuntimeSessionEvent(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_SESSION_STARTED,
            actor_role="operator",
            reason_code="",
            occurred_at=started_at,
        ),
        ProductionGovernedRuntimeSessionEvent(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_RUNTIME_BOUNDARY_BLOCKED,
            actor_role="system",
            reason_code="",
            occurred_at=started_at,
        ),
    )
    _write_session_bundle(record, events, store_dir=session_store_dir)
    return _evaluate()


class GovernedRuntimeSessionContext:
    """Read-only handle onto a started governed runtime session.

    Entering re-validates that the session is still live (not expired, the
    controlled window is still open, and the underlying permission is still
    issued and unconsumed) before exposing any state. Never invokes
    runtime, never starts cutover, never consumes the permission. The
    context is single-use: it cannot be entered while already active and
    cannot be re-entered after it has been exited.
    """

    def __init__(
        self,
        session_id: str = "",
        *,
        activation_request_id: str = "",
        session_store_dir: Path | None = None,
        permission_store_dir: Path | None = None,
        governed_cutover_store_dir: Path | None = None,
        window_store_dir: Path | None = None,
        final_signoff_store_dir: Path | None = None,
        signoff_store_dir: Path | None = None,
        store_dir: Path | None = None,
        reservation_dir: Path | None = None,
        runtime_history_dir: Path | None = None,
        evidence_dir: Path | None = None,
        audit_dir: Path | None = None,
        bundle_dir: Path | None = None,
        confirmation_dir: Path | None = None,
        transaction_dir: Path | None = None,
        e2e_history_dir: Path | None = None,
        validation_store_dir: Path | None = None,
        preflight_history_dir: Path | None = None,
        repo_root: Path | None = None,
        merged_config: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.session_id = (session_id or "").strip()
        self.activation_request_id = (activation_request_id or "").strip()
        if not self.session_id and not self.activation_request_id:
            raise ProductionGovernedRuntimeSessionError(
                "session_id or activation_request_id is required"
            )
        self.permission_id = ""
        self._executor_id = ""
        self.entered_at = ""
        self.expires_at = ""
        self.active = False
        self.consumed = False
        self._entered_once = False
        self._now = now
        self._session_store_dir = session_store_dir
        self._permission_store_dir = permission_store_dir
        self._governed_cutover_store_dir = governed_cutover_store_dir
        self._window_store_dir = window_store_dir
        self._final_signoff_store_dir = final_signoff_store_dir
        self._signoff_store_dir = signoff_store_dir
        self._store_dir = store_dir
        self._reservation_dir = reservation_dir
        self._runtime_history_dir = runtime_history_dir
        self._evidence_dir = evidence_dir
        self._audit_dir = audit_dir
        self._bundle_dir = bundle_dir
        self._confirmation_dir = confirmation_dir
        self._transaction_dir = transaction_dir
        self._e2e_history_dir = e2e_history_dir
        self._validation_store_dir = validation_store_dir
        self._preflight_history_dir = preflight_history_dir
        self._repo_root = repo_root
        self._merged_config = merged_config

    def __enter__(self) -> "GovernedRuntimeSessionContext":
        if self.active:
            raise ProductionGovernedRuntimeSessionError(
                "governed runtime session context is already active"
            )
        if self._entered_once:
            raise ProductionGovernedRuntimeSessionError(
                "governed runtime session context cannot be reused after exit"
            )

        record: ProductionGovernedRuntimeSessionRecord | None = None
        if self.activation_request_id:
            record = load_governed_runtime_session_record(
                self.activation_request_id, store_dir=self._session_store_dir
            )
            if record is None or (
                self.session_id and record.session_id != self.session_id
            ):
                raise ProductionGovernedRuntimeSessionError(
                    "governed_runtime_session_not_found"
                )
        else:
            record, _ = load_governed_runtime_session_events_by_session_id(
                self.session_id, store_dir=self._session_store_dir
            )
            if record is None:
                raise ProductionGovernedRuntimeSessionError(
                    "governed_runtime_session_not_found"
                )
            self.activation_request_id = record.activation_request_id

        assert record is not None
        current = _utc_now(self._now)
        expires_dt = _parse_iso(record.expires_at)
        if expires_dt is None or current >= expires_dt:
            raise ProductionGovernedRuntimeSessionError("session_expired")

        window = evaluate_production_controlled_window(
            activation_request_id=self.activation_request_id,
            store_dir=self._store_dir,
            reservation_dir=self._reservation_dir,
            runtime_history_dir=self._runtime_history_dir,
            evidence_dir=self._evidence_dir,
            audit_dir=self._audit_dir,
            bundle_dir=self._bundle_dir,
            confirmation_dir=self._confirmation_dir,
            transaction_dir=self._transaction_dir,
            e2e_history_dir=self._e2e_history_dir,
            signoff_store_dir=self._signoff_store_dir,
            validation_store_dir=self._validation_store_dir,
            final_signoff_store_dir=self._final_signoff_store_dir,
            preflight_history_dir=self._preflight_history_dir,
            governed_cutover_store_dir=self._governed_cutover_store_dir,
            window_store_dir=self._window_store_dir,
            repo_root=self._repo_root,
            merged_config=self._merged_config,
            now=current,
        )
        if not window.window_open:
            raise ProductionGovernedRuntimeSessionError("controlled_window_not_open")

        try:
            permission_record = load_runtime_permission_record(
                self.activation_request_id, store_dir=self._permission_store_dir
            )
        except ProductionRuntimePermissionError as exc:
            raise ProductionGovernedRuntimeSessionError(
                "session_store_corrupted"
            ) from exc
        if permission_record is None or permission_record.permission_id != record.permission_id:
            raise ProductionGovernedRuntimeSessionError("runtime_permission_invalid")
        if (
            permission_record.permission_status != PERMISSION_ISSUED
            or permission_record.consumed
            or permission_record.revoked
        ):
            raise ProductionGovernedRuntimeSessionError("runtime_permission_invalid")
        permission_expires_dt = _parse_iso(permission_record.expires_at)
        if permission_expires_dt is None or current >= permission_expires_dt:
            raise ProductionGovernedRuntimeSessionError("runtime_permission_expired")

        self.session_id = record.session_id
        self.permission_id = record.permission_id
        self._executor_id = record.executor_id
        self.entered_at = _utc_now_iso(current)
        self.expires_at = record.expires_at
        self.active = True
        self._entered_once = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.active = False
        return False

    def __getstate__(self):
        raise ProductionGovernedRuntimeSessionError(
            "governed runtime session context cannot be serialized"
        )

    def __setstate__(self, state):
        raise ProductionGovernedRuntimeSessionError(
            "governed runtime session context cannot be serialized"
        )

    def __reduce__(self):
        raise ProductionGovernedRuntimeSessionError(
            "governed runtime session context cannot be serialized"
        )


def enter_governed_runtime_session_context(
    *,
    session_id: str = "",
    activation_request_id: str = "",
    **kwargs: Any,
) -> GovernedRuntimeSessionContext:
    """Factory returning an unentered context manager; use via `with`."""
    return GovernedRuntimeSessionContext(
        session_id,
        activation_request_id=activation_request_id,
        **kwargs,
    )


def build_production_governed_runtime_session_release_summary(
    summary: ProductionGovernedRuntimeSessionSummary,
) -> ProductionGovernedRuntimeSessionReleaseSummary:
    if (
        summary.session_state == SESSION_STARTED
        and summary.runtime_permission_valid
        and summary.controlled_window_open
    ):
        release_status = RELEASE_GOVERNED_RUNTIME_SESSION_STARTED
        next_phase = _NEXT_PHASE_15E
    elif summary.session_state == SESSION_EXPIRED:
        release_status = RELEASE_GOVERNED_RUNTIME_SESSION_EXPIRED
        next_phase = ""
    elif summary.recovery_required or summary.repair_lock_held:
        release_status = RELEASE_GOVERNED_RUNTIME_SESSION_RECOVERY_REQUIRED
        next_phase = ""
    elif summary.session_ready or summary.session_state == SESSION_READY:
        release_status = RELEASE_GOVERNED_RUNTIME_SESSION_READY_TO_START
        next_phase = ""
    else:
        release_status = RELEASE_GOVERNED_RUNTIME_SESSION_NOT_READY
        next_phase = ""

    return ProductionGovernedRuntimeSessionReleaseSummary(
        activation_request_id=summary.activation_request_id,
        cutover_contract_id=summary.cutover_contract_id,
        permission_id=summary.permission_id,
        session_id=summary.session_id,
        controlled_window_state=summary.controlled_window_state,
        permission_state=summary.permission_state or summary.runtime_permission_state,
        session_state=summary.session_state,
        session_ready=summary.session_ready,
        session_present=summary.session_present,
        session_expired=summary.session_state == SESSION_EXPIRED,
        production_execution_allowed=False,
        cutover_started=False,
        runtime_invoked=False,
        permission_consumed=False,
        permission_revoked=False,
        production_root_hard_deny=True,
        original_repository2_execution_enabled=False,
        external_publish_enabled=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        next_phase=next_phase,
        release_status=release_status,
    )


def resolve_latest_governed_runtime_session_dashboard_digest(
    *,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
    session_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> ProductionGovernedRuntimeSessionDashboardDigest:
    base = (governed_cutover_store_dir or default_governed_cutover_store_dir()).resolve()
    if not base.is_dir():
        return ProductionGovernedRuntimeSessionDashboardDigest(
            governed_runtime_session_state="not_configured",
            governed_runtime_session_ready=False,
            governed_runtime_session_present=False,
            governed_runtime_session_expired=False,
            governed_runtime_session_id="",
            governed_runtime_session_expires_at="",
            governed_runtime_session_blocking_count=0,
            governed_runtime_session_warning_count=0,
            governed_runtime_session_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )
    paths = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:500]:
        activation_id = path.stem
        try:
            summary = evaluate_production_governed_runtime_session(
                activation_request_id=activation_id,
                governed_cutover_store_dir=governed_cutover_store_dir,
                window_store_dir=window_store_dir,
                permission_store_dir=permission_store_dir,
                session_store_dir=session_store_dir,
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
        except Exception:
            continue
        if not summary.governed_cutover_contract_valid and not summary.session_present:
            continue
        return ProductionGovernedRuntimeSessionDashboardDigest(
            governed_runtime_session_state=summary.session_state,
            governed_runtime_session_ready=summary.session_ready,
            governed_runtime_session_present=summary.session_present,
            governed_runtime_session_expired=summary.session_state == SESSION_EXPIRED,
            governed_runtime_session_id=summary.session_id,
            governed_runtime_session_expires_at=summary.expires_at,
            governed_runtime_session_blocking_count=len(summary.blocking_items),
            governed_runtime_session_warning_count=len(summary.warning_items),
            governed_runtime_session_recommended_action=summary.recommended_action,
        )
    return ProductionGovernedRuntimeSessionDashboardDigest(
        governed_runtime_session_state="not_configured",
        governed_runtime_session_ready=False,
        governed_runtime_session_present=False,
        governed_runtime_session_expired=False,
        governed_runtime_session_id="",
        governed_runtime_session_expires_at="",
        governed_runtime_session_blocking_count=0,
        governed_runtime_session_warning_count=0,
        governed_runtime_session_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
    )


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "production_execution_allowed: false",
        "cutover_started: false",
        "runtime_invoked: false",
        "permission_consumed: false",
        "permission_revoked: false",
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
        "kill_switch_available: true",
        "kill_switch_available: false",
        "emergency_close_available: true",
        "emergency_close_available: false",
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
        "permission_scope_mismatch",
        "permission_executor_mismatch",
        "permission_not_consumed",
    ):
        lowered = lowered.replace(label, "")
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionGovernedRuntimeSessionError(
                f"Unsafe governed runtime session output field: {token!r}"
            )


def format_production_governed_runtime_session_status(
    summary: ProductionGovernedRuntimeSessionSummary,
) -> str:
    lines = [
        "Production Governed Runtime Session Status",
        "",
        f"activation_request_id: {summary.activation_request_id}",
        f"cutover_contract_id: {summary.cutover_contract_id or '(none)'}",
        f"permission_id: {summary.permission_id or '(none)'}",
        f"session_id: {summary.session_id or '(none)'}",
        f"session_state: {summary.session_state}",
        f"session_ready: {str(summary.session_ready).lower()}",
        f"session_present: {str(summary.session_present).lower()}",
        f"controlled_window_state: {summary.controlled_window_state or '(none)'}",
        f"controlled_window_open: {str(summary.controlled_window_open).lower()}",
        f"controlled_window_expired: {str(summary.controlled_window_expired).lower()}",
        "governed_cutover_contract_valid: "
        f"{str(summary.governed_cutover_contract_valid).lower()}",
        f"governed_cutover_status: {summary.governed_cutover_status or '(none)'}",
        f"runtime_permission_valid: {str(summary.runtime_permission_valid).lower()}",
        f"runtime_permission_state: {summary.runtime_permission_state or '(none)'}",
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
        f"kill_switch_available: {str(summary.kill_switch_available).lower()}",
        f"emergency_close_available: {str(summary.emergency_close_available).lower()}",
        f"window_remaining_seconds: {summary.window_remaining_seconds}",
        f"permission_remaining_seconds: {summary.permission_remaining_seconds}",
        f"started_at: {summary.started_at or '(none)'}",
        f"expires_at: {summary.expires_at or '(none)'}",
        f"session_started: {str(summary.session_started).lower()}",
        f"session_expired: {str(summary.session_expired).lower()}",
        "permission_consumed: false",
        "permission_revoked: false",
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
        f"already_started: {str(summary.already_started).lower()}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "production_root_hard_deny: true",
        "cutover_started: false",
        "runtime_invoked: false",
        "permission_consumed: false",
        "permission_revoked: false",
        "original_repository2_execution_attempted: false",
        "external_publish_enabled: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_governed_runtime_session_record(
    record: ProductionGovernedRuntimeSessionRecord,
    *,
    now: datetime | None = None,
) -> str:
    expires_dt = _parse_iso(record.expires_at)
    expired = bool(expires_dt and _utc_now(now) >= expires_dt)
    state = SESSION_EXPIRED if expired else SESSION_STARTED
    lines = [
        "Production Governed Runtime Session",
        "",
        f"session_id: {record.session_id}",
        f"activation_request_id: {record.activation_request_id}",
        f"cutover_contract_id: {record.cutover_contract_id}",
        f"permission_id: {record.permission_id}",
        f"reservation_id: {record.reservation_id}",
        f"execution_attempt_id: {record.execution_attempt_id}",
        f"dispatch_run_id: {record.dispatch_run_id}",
        f"session_status: {state}",
        f"ttl_seconds: {record.ttl_seconds}",
        f"started_at: {record.started_at}",
        f"expires_at: {record.expires_at}",
        "permission_consumed: false",
        "permission_revoked: false",
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
        "permission_consumed: false",
        "permission_revoked: false",
        "original_repository2_execution_attempted: false",
        "external_publish_enabled: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_governed_runtime_session_history(
    activation_request_id: str,
    events: tuple[ProductionGovernedRuntimeSessionEvent, ...],
    *,
    session_id: str = "",
) -> str:
    lines = [
        "Production Governed Runtime Session History",
        "",
        f"activation_request_id: {activation_request_id}",
        f"session_id: {session_id or '(none)'}",
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
            "permission_consumed: false",
            "permission_revoked: false",
            "production_root_hard_deny: true",
        ]
    )
    output = "\n".join(lines).rstrip()
    _assert_safe_output(output)
    return output


def run_production_governed_runtime_session_status(
    *,
    activation_request_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_governed_runtime_session(
            activation_request_id=activation_request_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionGovernedRuntimeSessionError:
        return "error: governed runtime session status unavailable", 1
    return format_production_governed_runtime_session_status(summary), 0


def run_production_governed_runtime_session_check(
    *,
    activation_request_id: str,
    permission_id: str,
    executor_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
    ttl_seconds: int = 120,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_governed_runtime_session(
            activation_request_id=activation_request_id,
            executor_id=executor_id,
            permission_id=permission_id,
            ttl_seconds=ttl_seconds,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionGovernedRuntimeSessionError:
        return "error: governed runtime session check unavailable", 1
    exit_code = 0 if summary.session_state == SESSION_READY else 1
    return format_production_governed_runtime_session_status(summary), exit_code


def run_production_governed_runtime_session_start(
    *,
    activation_request_id: str,
    permission_id: str,
    executor_id: str,
    operator_id: str,
    ttl_seconds: int,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = start_production_governed_runtime_session(
            activation_request_id=activation_request_id,
            executor_id=executor_id,
            operator_id=operator_id,
            permission_id=permission_id,
            ttl_seconds=ttl_seconds,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionGovernedRuntimeSessionError:
        try:
            summary = evaluate_production_governed_runtime_session(
                activation_request_id=activation_request_id,
                executor_id=executor_id,
                operator_id=operator_id,
                permission_id=permission_id,
                ttl_seconds=ttl_seconds,
                merged_config=merged_config,
                repo_root=repo_root,
            )
            return format_production_governed_runtime_session_status(summary), 1
        except ProductionGovernedRuntimeSessionError:
            return "error: governed runtime session start failed", 1
    exit_code = (
        0
        if summary.session_state == SESSION_STARTED or summary.already_started
        else 1
    )
    return format_production_governed_runtime_session_status(summary), exit_code


def run_production_governed_runtime_session_show(
    *,
    session_id: str,
) -> tuple[str, int]:
    try:
        record = load_governed_runtime_session_by_id(session_id)
    except ProductionGovernedRuntimeSessionError:
        return "error: governed runtime session corrupted", 1
    if record is None:
        return "error: governed runtime session not found", 1
    return format_production_governed_runtime_session_record(record), 0


def run_production_governed_runtime_session_history(
    *,
    activation_request_id: str,
) -> tuple[str, int]:
    try:
        record = load_governed_runtime_session_record(activation_request_id)
        events = load_governed_runtime_session_events(activation_request_id)
    except ProductionGovernedRuntimeSessionError:
        return "error: governed runtime session history unavailable", 1
    return (
        format_production_governed_runtime_session_history(
            activation_request_id,
            events,
            session_id=record.session_id if record else "",
        ),
        0,
    )
