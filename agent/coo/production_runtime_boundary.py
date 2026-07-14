"""Runtime boundary contract — Phase 15E.

One-shot append-only runtime boundary prerequisite bound to a started
governed runtime session, an issued/unconsumed runtime permission, and an
open controlled window. Preparing a boundary is NOT cutover, NOT runtime
invocation, and NOT permission consumption — those remain exclusively
Phase 15F's concern.

Invariants enforced everywhere in this module:
    - production_execution_allowed is always False in every output.
    - cutover_started is always False in every output, even after a
      successful prepare. Preparing a boundary only ever appends a
      `cutover_start_requested` *event* — it never flips `cutover_started`.
    - runtime_invoked is always False in every output.
    - permission_consumed and permission_revoked are always False — this
      module never consumes or revokes a runtime permission.
    - No subprocess, no `create_bounded_subprocess_runner` call, no
      Repository2 execution. The runner-factory check below is purely
      structural (is the class importable?) and never invokes the runner.
    - Boundary reserved != cutover started != runtime invoked != permission
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
from agent.coo.production_activation_live_harness import (
    ConfigBoundRunnerFactoryAvailability,
    DisabledProductionLiveRuntimeInvoker,
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
from agent.coo.production_governed_runtime_session import (
    SESSION_STARTED,
    ProductionGovernedRuntimeSessionError,
    load_governed_runtime_session_record,
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

_BOUNDARY_STORE_DIR = "production-runtime-boundary"
_BOUNDARY_STORE_VERSION = 1
_NEXT_PHASE_15F = "Phase_15F_governed_runtime_invocation"

BOUNDARY_NOT_RESERVED = "BOUNDARY_NOT_RESERVED"
BOUNDARY_READY = "BOUNDARY_READY"
BOUNDARY_RESERVED = "BOUNDARY_RESERVED"
BOUNDARY_EXPIRED = "BOUNDARY_EXPIRED"
BOUNDARY_BLOCKED = "BOUNDARY_BLOCKED"
BOUNDARY_CONSUMED = "BOUNDARY_CONSUMED"
BOUNDARY_REVOKED = "BOUNDARY_REVOKED"

SCOPE_TYPE_ONE_SHOT = "one_shot"
MIN_BOUNDARY_TTL_SECONDS = 15
MAX_BOUNDARY_TTL_SECONDS = 120
MIN_TTL_SECONDS = MIN_BOUNDARY_TTL_SECONDS
MAX_TTL_SECONDS = MAX_BOUNDARY_TTL_SECONDS

# `session_state` mirrors `permission_state`'s derived-label convention: the
# underlying session record never persists a distinct "expired" status field
# (it is always written as SESSION_STARTED and expiry is derived at
# evaluation time), so this module derives the same label locally.
SESSION_EXPIRED_LABEL = "SESSION_EXPIRED"

EVENT_BOUNDARY_RESERVE_REQUESTED = "boundary_reserve_requested"
EVENT_BOUNDARY_RESERVED = "boundary_reserved"
EVENT_CUTOVER_START_REQUESTED = "cutover_start_requested"
EVENT_RUNTIME_BOUNDARY_BLOCKED = "runtime_boundary_blocked_waiting_phase_15f"

RELEASE_RUNTIME_BOUNDARY_READY_TO_PREPARE = (
    "RUNTIME_BOUNDARY_READY_TO_PREPARE"
)
RELEASE_RUNTIME_BOUNDARY_RESERVED = "GOVERNED_RUNTIME_BOUNDARY_RESERVED"
RELEASE_RUNTIME_BOUNDARY_EXPIRED = "GOVERNED_RUNTIME_BOUNDARY_EXPIRED"
RELEASE_RUNTIME_BOUNDARY_NOT_READY = "GOVERNED_RUNTIME_BOUNDARY_NOT_READY"
RELEASE_GOVERNED_RUNTIME_BOUNDARY_RECOVERY_REQUIRED = (
    "GOVERNED_RUNTIME_BOUNDARY_RECOVERY_REQUIRED"
)

ACTION_PREPARE_RUNTIME_BOUNDARY = "prepare_runtime_boundary"
ACTION_GOVERNED_RUNTIME_BOUNDARY_RESERVED_WAIT_FOR_PHASE_15F = (
    "governed_runtime_boundary_reserved_wait_for_phase_15f"
)
ACTION_REVIEW_BOUNDARY_WARNINGS = "review_boundary_warnings"
ACTION_WAIT_FOR_WINDOW_OPEN = "wait_for_window_open"
ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT = "prepare_new_governed_cutover_contract"
ACTION_RESOLVE_RUNTIME_PERMISSION = "resolve_runtime_permission"
ACTION_RESOLVE_GOVERNED_RUNTIME_SESSION = "resolve_governed_runtime_session"
ACTION_RESOLVE_IDENTITY_SEPARATION = "resolve_identity_separation"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_CLOSE_OR_EMERGENCY_CLOSE_WINDOW = "close_or_emergency_close_window"
ACTION_RESOLVE_KILL_SWITCH = "resolve_kill_switch"
ACTION_RESOLVE_RUNTIME_FACTORY = "resolve_runtime_factory"
ACTION_CONFIGURE_RUNTIME_FACTORY_CONTRACT = "configure_runtime_factory_contract"
ACTION_DISABLE_RUNTIME_INVOKER = "disable_runtime_invoker"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_PREPARE_PHASE_15F_GOVERNED_RUNTIME_INVOCATION = (
    "prepare_phase_15f_governed_runtime_invocation"
)
ACTION_PREPARE_PHASE_15F_RUNTIME_INVOCATION = (
    ACTION_PREPARE_PHASE_15F_GOVERNED_RUNTIME_INVOCATION
)

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
BLOCK_GOVERNED_RUNTIME_SESSION_MISSING = "governed_runtime_session_missing"
BLOCK_GOVERNED_RUNTIME_SESSION_INVALID = "governed_runtime_session_invalid"
BLOCK_GOVERNED_RUNTIME_SESSION_EXPIRED = "governed_runtime_session_expired"
BLOCK_SESSION_SCOPE_MISMATCH = "session_scope_mismatch"
BLOCK_SESSION_EXECUTOR_MISMATCH = "session_executor_mismatch"
BLOCK_BOUNDARY_ALREADY_RESERVED = "boundary_already_reserved"
BLOCK_BOUNDARY_ALREADY_EXISTS = "boundary_already_exists"
BLOCK_BOUNDARY_EXPIRED = "boundary_expired"
BLOCK_GOVERNED_RUNTIME_BOUNDARY_CONFLICT = "runtime_boundary_conflict"
BLOCK_EXECUTOR_IDENTITY_INVALID = "executor_identity_invalid"
BLOCK_OPERATOR_IDENTITY_INVALID = "operator_identity_invalid"
BLOCK_IDENTITY_SEPARATION_INVALID = "identity_separation_invalid"
BLOCK_BOUNDARY_TTL_INVALID = "boundary_ttl_invalid"
BLOCK_BOUNDARY_TTL_EXCEEDS_SESSION = "boundary_ttl_exceeds_session"
BLOCK_BOUNDARY_TTL_EXCEEDS_PERMISSION = "boundary_ttl_exceeds_permission"
BLOCK_BOUNDARY_TTL_EXCEEDS_WINDOW = "boundary_ttl_exceeds_window"
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
BLOCK_RUNTIME_FACTORY_UNAVAILABLE = "runtime_factory_unavailable"
BLOCK_RUNTIME_INVOKER_ENABLED = "runtime_invoker_enabled"
BLOCK_SOURCE_TREE_MUTATED = "source_tree_mutated"
BLOCK_PRODUCTION_ROOT_TOUCHED = "production_root_touched"
BLOCK_EXTERNAL_PUBLISH_ENABLED = "external_publish_enabled"
BLOCK_GATEWAY_PRODUCTION_ENABLED = "gateway_production_enabled"
BLOCK_DISCORD_PRODUCTION_ENABLED = "discord_production_enabled"
BLOCK_PRODUCTION_EXECUTION_ENABLED = "production_execution_enabled"
BLOCK_CUTOVER_ALREADY_STARTED = "cutover_already_started"
BLOCK_RUNTIME_ALREADY_INVOKED = "runtime_already_invoked"
BLOCK_BOUNDARY_STORE_CORRUPTED = "boundary_store_corrupted"
BLOCK_BOUNDARY_WRITE_FAILED = "boundary_write_failed"
BLOCK_UNSAFE_OUTPUT = "unsafe_output"

WARN_BOUNDARY_IS_PREREQUISITE_ONLY = "boundary_is_prerequisite_only"
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
WARN_BOUNDARY_EXPIRY_REQUIRES_NEW_SESSION = "boundary_expiry_requires_new_session"
WARN_RUNTIME_BOUNDARY_BLOCKED_WAITING_PHASE_15F = (
    "runtime_boundary_blocked_waiting_phase_15f"
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


class RuntimeBoundaryError(ValueError):
    """Raised when runtime boundary assessment or reserve fails safely."""


@dataclass(frozen=True)
class RuntimeBoundaryRecord:
    boundary_id: str
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    ticket_id: str
    confirmation_id: str
    controlled_window_open_event_id: str
    executor_id: str
    operator_id: str
    reserved_by: str
    reserved_at: str
    expires_at: str
    ttl_seconds: int
    scope_type: str
    boundary_status: str
    invocation_id: str
    tested_commit_sha: str
    release_tag: str
    cutover_start_requested_at: str = ""
    consumed: bool = False
    consumed_at: str = ""
    revoked: bool = False
    revoked_at: str = ""
    revoke_reason_code: str = ""
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
class RuntimeBoundaryEvent:
    event_id: str
    boundary_id: str
    activation_request_id: str
    event_type: str
    actor_role: str
    reason_code: str
    occurred_at: str


@dataclass(frozen=True)
class RuntimeBoundarySummary:
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    boundary_id: str
    invocation_id: str
    boundary_state: str
    boundary_ready: bool
    boundary_present: bool
    controlled_window_state: str
    controlled_window_open: bool
    controlled_window_expired: bool
    governed_cutover_contract_valid: bool
    governed_cutover_status: str
    permission_valid: bool
    permission_state: str
    permission_expired: bool
    permission_scope_valid: bool
    session_valid: bool
    session_state: str
    session_expired: bool
    session_context_valid: bool
    session_scope_valid: bool
    one_shot_scope_valid: bool
    ticket_scope_valid: bool
    window_scope_valid: bool
    boundary_ttl_valid: bool
    final_signoff_valid: bool
    rollback_ready: bool
    operational_signoff_valid: bool
    audit_chain_complete: bool
    recovery_required: bool
    repair_lock_held: bool
    executor_identity_valid: bool
    operator_identity_valid: bool
    identity_separation_valid: bool
    kill_switch_available: bool
    emergency_close_available: bool
    runtime_factory_available: bool
    runtime_invoker_disabled: bool
    reserved_at: str
    expires_at: str
    boundary_reserved: bool
    boundary_expired: bool
    cutover_start_event_recorded: bool
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
    already_reserved: bool = False
    executor_assigned: bool = False
    operator_present: bool = False
    tested_commit_sha_short: str = ""
    release_tag: str = ""
    window_remaining_seconds: int = 0
    permission_remaining_seconds: int = 0
    session_remaining_seconds: int = 0


@dataclass(frozen=True)
class RuntimeBoundaryReleaseSummary:
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    boundary_id: str
    controlled_window_state: str
    permission_state: str
    session_state: str
    boundary_state: str
    boundary_ready: bool
    boundary_present: bool
    boundary_expired: bool
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
    release_status: str = RELEASE_RUNTIME_BOUNDARY_NOT_READY


@dataclass(frozen=True)
class RuntimeBoundaryDashboardDigest:
    runtime_boundary_state: str
    runtime_boundary_ready: bool
    runtime_boundary_present: bool
    runtime_boundary_expired: bool
    runtime_boundary_id: str
    runtime_boundary_invocation_id: str
    runtime_boundary_expires_at: str
    runtime_boundary_blocking_count: int
    runtime_boundary_warning_count: int
    runtime_boundary_recommended_action: str


def default_runtime_boundary_store_dir() -> Path:
    return get_hermes_home() / "coo" / _BOUNDARY_STORE_DIR


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


def _boundary_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise RuntimeBoundaryError("activation_request_id is required")
    base = (store_dir or default_runtime_boundary_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise RuntimeBoundaryError(
            "Runtime boundary store must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_runtime_boundary_store_available(
    *, store_dir: Path | None = None
) -> bool:
    try:
        base = (store_dir or default_runtime_boundary_store_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _record_from_dict(
    payload: Mapping[str, Any]
) -> RuntimeBoundaryRecord:
    return RuntimeBoundaryRecord(
        boundary_id=str(payload.get("boundary_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        cutover_contract_id=str(payload.get("cutover_contract_id", "")),
        permission_id=str(payload.get("permission_id", "")),
        session_id=str(payload.get("session_id", "")),
        reservation_id=str(payload.get("reservation_id", "")),
        execution_attempt_id=str(payload.get("execution_attempt_id", "")),
        dispatch_run_id=str(payload.get("dispatch_run_id", "")),
        ticket_id=str(payload.get("ticket_id", "")),
        confirmation_id=str(payload.get("confirmation_id", "")),
        controlled_window_open_event_id=str(
            payload.get("controlled_window_open_event_id", "")
        ),
        executor_id=str(payload.get("executor_id", "")),
        operator_id=str(payload.get("operator_id") or payload.get("reserved_by", "")),
        reserved_by=str(payload.get("reserved_by") or payload.get("operator_id", "")),
        reserved_at=str(payload.get("reserved_at", "")),
        expires_at=str(payload.get("expires_at", "")),
        ttl_seconds=int(payload.get("ttl_seconds") or 0),
        scope_type=str(payload.get("scope_type") or SCOPE_TYPE_ONE_SHOT),
        boundary_status=str(payload.get("boundary_status", "")),
        invocation_id=str(payload.get("invocation_id", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        cutover_start_requested_at=str(payload.get("cutover_start_requested_at", "")),
        consumed=False,
        consumed_at="",
        revoked=False,
        revoked_at="",
        revoke_reason_code="",
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
    record: RuntimeBoundaryRecord,
) -> dict[str, Any]:
    return {
        "boundary_id": record.boundary_id,
        "activation_request_id": record.activation_request_id,
        "cutover_contract_id": record.cutover_contract_id,
        "permission_id": record.permission_id,
        "session_id": record.session_id,
        "reservation_id": record.reservation_id,
        "execution_attempt_id": record.execution_attempt_id,
        "dispatch_run_id": record.dispatch_run_id,
        "ticket_id": record.ticket_id,
        "confirmation_id": record.confirmation_id,
        "controlled_window_open_event_id": record.controlled_window_open_event_id,
        "executor_id": record.executor_id,
        "operator_id": record.operator_id,
        "reserved_by": record.reserved_by or record.operator_id,
        "reserved_at": record.reserved_at,
        "expires_at": record.expires_at,
        "ttl_seconds": record.ttl_seconds,
        "scope_type": SCOPE_TYPE_ONE_SHOT,
        "boundary_status": BOUNDARY_RESERVED,
        "invocation_id": record.invocation_id,
        "tested_commit_sha": _short_sha(record.tested_commit_sha),
        "release_tag": record.release_tag,
        "cutover_start_requested_at": record.cutover_start_requested_at,
        "consumed": False,
        "consumed_at": "",
        "revoked": False,
        "revoked_at": "",
        "revoke_reason_code": "",
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


def load_runtime_boundary_record(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> RuntimeBoundaryRecord | None:
    path = _boundary_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeBoundaryError("boundary_store_corrupted") from exc
    boundary = payload.get("boundary")
    if not isinstance(boundary, dict):
        raise RuntimeBoundaryError("boundary_store_corrupted")
    return _record_from_dict(boundary)


def load_runtime_boundary_by_id(
    boundary_id: str,
    *,
    store_dir: Path | None = None,
) -> RuntimeBoundaryRecord | None:
    target = (boundary_id or "").strip()
    if not target:
        raise RuntimeBoundaryError("boundary_id is required")
    base = (store_dir or default_runtime_boundary_store_dir()).resolve()
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeBoundaryError(
                "boundary_store_corrupted"
            ) from exc
        boundary = payload.get("boundary")
        if isinstance(boundary, dict) and str(boundary.get("boundary_id", "")) == target:
            return _record_from_dict(boundary)
    return None


def load_runtime_boundary_events(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> tuple[RuntimeBoundaryEvent, ...]:
    path = _boundary_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeBoundaryError("boundary_store_corrupted") from exc
    raw = payload.get("events") or []
    if not isinstance(raw, list):
        raise RuntimeBoundaryError("boundary_store_corrupted")
    events: list[RuntimeBoundaryEvent] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeBoundaryError("boundary_store_corrupted")
        event_id = str(item.get("event_id", ""))
        if not event_id or event_id in seen:
            raise RuntimeBoundaryError("boundary_store_corrupted")
        seen.add(event_id)
        events.append(
            RuntimeBoundaryEvent(
                event_id=event_id,
                boundary_id=str(item.get("boundary_id", "")),
                activation_request_id=str(item.get("activation_request_id", "")),
                event_type=str(item.get("event_type", "")),
                actor_role=str(item.get("actor_role", "")),
                reason_code=str(item.get("reason_code", "")),
                occurred_at=str(item.get("occurred_at", "")),
            )
        )
    return tuple(events)


def load_runtime_boundary_events_by_boundary_id(
    boundary_id: str,
    *,
    store_dir: Path | None = None,
) -> tuple[
    RuntimeBoundaryRecord | None,
    tuple[RuntimeBoundaryEvent, ...],
]:
    """Scan the whole store for the bundle whose boundary matches `boundary_id`."""
    target = (boundary_id or "").strip()
    if not target:
        raise RuntimeBoundaryError("boundary_id is required")
    base = (store_dir or default_runtime_boundary_store_dir()).resolve()
    if not base.is_dir():
        return None, ()
    for path in sorted(base.glob("*.json")):
        activation_id = path.stem
        record = load_runtime_boundary_record(activation_id, store_dir=store_dir)
        if record is not None and record.boundary_id == target:
            events = load_runtime_boundary_events(activation_id, store_dir=store_dir)
            return record, events
    return None, ()


def _boundaries_equivalent(
    existing: RuntimeBoundaryRecord,
    candidate: RuntimeBoundaryRecord,
) -> bool:
    return (
        existing.cutover_contract_id == candidate.cutover_contract_id
        and existing.permission_id == candidate.permission_id
        and existing.session_id == candidate.session_id
        and existing.executor_id == candidate.executor_id
        and existing.operator_id == candidate.operator_id
        and existing.ttl_seconds == candidate.ttl_seconds
        and existing.ticket_id == candidate.ticket_id
        and existing.confirmation_id == candidate.confirmation_id
        and existing.controlled_window_open_event_id
        == candidate.controlled_window_open_event_id
    )


def _write_boundary_bundle(
    record: RuntimeBoundaryRecord,
    events: tuple[RuntimeBoundaryEvent, ...],
    *,
    store_dir: Path | None = None,
) -> None:
    path = _boundary_path(record.activation_request_id, store_dir=store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    payload = {
        "version": _BOUNDARY_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "boundary": _record_to_dict(record),
        "events": [
            {
                "event_id": event.event_id,
                "boundary_id": event.boundary_id,
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
            existing = load_runtime_boundary_record(
                record.activation_request_id,
                store_dir=store_dir,
            )
            if existing is not None:
                if _boundaries_equivalent(existing, record):
                    return
                raise RuntimeBoundaryError(
                    "runtime_boundary_conflict"
                )
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            try:
                fd = os.open(str(path), flags, 0o644)
            except FileExistsError as exc:
                existing_again = load_runtime_boundary_record(
                    record.activation_request_id,
                    store_dir=store_dir,
                )
                if existing_again is not None and _boundaries_equivalent(
                    existing_again, record
                ):
                    return
                raise RuntimeBoundaryError(
                    "runtime_boundary_conflict"
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
        raise RuntimeBoundaryError("boundary_write_failed") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


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
    session_record,
) -> tuple[bool, bool, bool, list[str]]:
    """Validate provided identities only.

    Empty executor/operator (status path) does not append identity blocks.
    Check provides executor only; reserve provides both. Executor/session
    and executor/permission binding correctness is validated separately via
    the scope-mismatch checks — this function only rules out conflicts with
    other role holders (requester, preparer, signers, issuer, session
    starter).
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
        if session_record is not None:
            conflicts_for_operator.add(
                (session_record.started_by or session_record.operator_id or "").strip()
            )
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


def _validate_boundary_ttl(
    ttl_seconds: int | None,
    *,
    now: datetime,
    session_expires_at: datetime | None,
    permission_expires_at: datetime | None,
    window_end: datetime | None,
) -> tuple[bool, str, int, int, int, int, str]:
    """Return (ttl_valid, invalid_reason, effective_ttl, session_remaining,
    permission_remaining, window_remaining, expires_at_iso).

    Fail-closed: a missing boundary (session, permission, or window) is
    treated as zero remaining seconds, so a candidate ttl can never be
    accepted against an unknown boundary. `expires_at` is always clamped to
    at most `min(session.expires_at, permission.expires_at, window_end,
    now + ttl)`.
    """
    session_remaining = (
        max(0, int((session_expires_at - now).total_seconds()))
        if session_expires_at is not None
        else 0
    )
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
        return (
            True,
            "",
            0,
            session_remaining,
            permission_remaining,
            window_remaining,
            "",
        )
    remaining = min(session_remaining, permission_remaining, window_remaining)
    if remaining < MIN_BOUNDARY_TTL_SECONDS:
        return (
            False,
            "insufficient_remaining",
            ttl_seconds,
            session_remaining,
            permission_remaining,
            window_remaining,
            "",
        )
    if ttl_seconds < MIN_BOUNDARY_TTL_SECONDS or ttl_seconds > MAX_BOUNDARY_TTL_SECONDS:
        return (
            False,
            "invalid_range",
            ttl_seconds,
            session_remaining,
            permission_remaining,
            window_remaining,
            "",
        )
    if ttl_seconds > session_remaining:
        return (
            False,
            "exceeds_session",
            ttl_seconds,
            session_remaining,
            permission_remaining,
            window_remaining,
            "",
        )
    if ttl_seconds > permission_remaining:
        return (
            False,
            "exceeds_permission",
            ttl_seconds,
            session_remaining,
            permission_remaining,
            window_remaining,
            "",
        )
    if ttl_seconds > window_remaining:
        return (
            False,
            "exceeds_window",
            ttl_seconds,
            session_remaining,
            permission_remaining,
            window_remaining,
            "",
        )
    expires = now + timedelta(seconds=ttl_seconds)
    return (
        True,
        "",
        ttl_seconds,
        session_remaining,
        permission_remaining,
        window_remaining,
        expires.isoformat(),
    )


def _check_runtime_factory_available(
    force_unavailable: bool,
) -> bool:
    """Structural-only availability check.

    This NEVER calls `.is_available(...)` (which resolves pipeline roots and
    executor policy) and NEVER calls `create_bounded_subprocess_runner`. It
    only proves the factory class is importable and constructible; the
    default is available=True once import has succeeded.
    """
    if force_unavailable:
        return False
    try:
        ConfigBoundRunnerFactoryAvailability()
    except Exception:
        return False
    return True


def _check_runtime_invoker_disabled(
    force_enabled: bool,
) -> bool:
    if force_enabled:
        return False
    try:
        invoker = DisabledProductionLiveRuntimeInvoker()
        return not invoker.is_enabled()
    except Exception:
        return False


def _recommended_action(
    state: str,
    blocking: tuple[str, ...],
    *,
    window_open: bool,
    recovery: bool,
) -> str:
    if state == BOUNDARY_RESERVED:
        return ACTION_GOVERNED_RUNTIME_BOUNDARY_RESERVED_WAIT_FOR_PHASE_15F
    if state == BOUNDARY_READY:
        return ACTION_PREPARE_RUNTIME_BOUNDARY
    if state == BOUNDARY_EXPIRED:
        return ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT
    if recovery or BLOCK_RECOVERY_REQUIRED in blocking or BLOCK_REPAIR_LOCK_HELD in blocking:
        return ACTION_RUN_CONSUME_RECOVERY
    if (
        BLOCK_KILL_SWITCH_UNAVAILABLE in blocking
        or BLOCK_EMERGENCY_CLOSE_UNAVAILABLE in blocking
    ):
        return ACTION_RESOLVE_KILL_SWITCH
    if BLOCK_RUNTIME_FACTORY_UNAVAILABLE in blocking:
        return ACTION_RESOLVE_RUNTIME_FACTORY
    if BLOCK_RUNTIME_INVOKER_ENABLED in blocking:
        return ACTION_DISABLE_RUNTIME_INVOKER
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
        BLOCK_GOVERNED_RUNTIME_SESSION_MISSING in blocking
        or BLOCK_GOVERNED_RUNTIME_SESSION_INVALID in blocking
        or BLOCK_GOVERNED_RUNTIME_SESSION_EXPIRED in blocking
        or BLOCK_SESSION_SCOPE_MISMATCH in blocking
        or BLOCK_SESSION_EXECUTOR_MISMATCH in blocking
    ):
        return ACTION_RESOLVE_GOVERNED_RUNTIME_SESSION
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
        BLOCK_BOUNDARY_TTL_INVALID in blocking
        or BLOCK_BOUNDARY_TTL_EXCEEDS_SESSION in blocking
        or BLOCK_BOUNDARY_TTL_EXCEEDS_PERMISSION in blocking
        or BLOCK_BOUNDARY_TTL_EXCEEDS_WINDOW in blocking
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
    if BLOCK_BOUNDARY_ALREADY_RESERVED in blocking:
        return ACTION_GOVERNED_RUNTIME_BOUNDARY_RESERVED_WAIT_FOR_PHASE_15F
    if BLOCK_GOVERNED_RUNTIME_BOUNDARY_CONFLICT in blocking:
        return ACTION_MAINTAIN_PRODUCTION_BLOCK
    if not window_open:
        return ACTION_WAIT_FOR_WINDOW_OPEN
    return ACTION_MAINTAIN_PRODUCTION_BLOCK


def evaluate_production_runtime_boundary(
    *,
    activation_request_id: str,
    executor_id: str = "",
    operator_id: str = "",
    permission_id: str = "",
    session_id: str = "",
    ttl_seconds: int | None = None,
    boundary_store_dir: Path | None = None,
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
    force_runtime_factory_unavailable: bool = False,
    force_runtime_invoker_enabled: bool = False,
) -> RuntimeBoundarySummary:
    """Read-only runtime boundary assessment."""
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
        blocking.append(BLOCK_BOUNDARY_STORE_CORRUPTED)
        permission_record = None

    session_record = None
    try:
        session_record = load_governed_runtime_session_record(
            activation_request_id,
            store_dir=session_store_dir,
        )
    except ProductionGovernedRuntimeSessionError:
        blocking.append(BLOCK_BOUNDARY_STORE_CORRUPTED)
        session_record = None

    existing = None
    try:
        existing = load_runtime_boundary_record(
            activation_request_id,
            store_dir=boundary_store_dir,
        )
    except RuntimeBoundaryError:
        blocking.append(BLOCK_BOUNDARY_STORE_CORRUPTED)
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
            blocking.append(BLOCK_BOUNDARY_STORE_CORRUPTED)

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

    # --- Runtime factory / invoker structural availability ------------------
    runtime_factory_available = _check_runtime_factory_available(
        force_runtime_factory_unavailable
    )
    if not runtime_factory_available:
        blocking.append(BLOCK_RUNTIME_FACTORY_UNAVAILABLE)
    runtime_invoker_disabled = _check_runtime_invoker_disabled(
        force_runtime_invoker_enabled
    )
    if not runtime_invoker_disabled:
        blocking.append(BLOCK_RUNTIME_INVOKER_ENABLED)

    # --- Runtime permission validation --------------------------------------
    runtime_permission_state = ""
    derived_permission_expired = False
    permission_consumed_flag = False
    permission_revoked_flag = False
    permission_correlation_valid = True
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
            permission_correlation_valid = False
        if (
            open_event_id
            and permission_record.controlled_window_open_event_id
            and permission_record.controlled_window_open_event_id != open_event_id
        ):
            permission_correlation_valid = False
        if reservation is not None:
            if (
                permission_record.ticket_id
                and reservation.ticket_id
                and permission_record.ticket_id != reservation.ticket_id
            ):
                permission_correlation_valid = False
            if (
                permission_record.confirmation_id
                and reservation.confirmation_id
                and permission_record.confirmation_id != reservation.confirmation_id
            ):
                permission_correlation_valid = False
        if (
            not permission_correlation_valid
            and BLOCK_RUNTIME_PERMISSION_INVALID not in blocking
        ):
            blocking.append(BLOCK_RUNTIME_PERMISSION_INVALID)

    runtime_permission_valid = (
        permission_record is not None
        and permission_record.permission_status == PERMISSION_ISSUED
        and not derived_permission_expired
        and not permission_consumed_flag
        and not permission_revoked_flag
        and permission_record.max_executions == 1
        and permission_record.execution_count == 0
        and permission_correlation_valid
        and (not permission_id or permission_record.permission_id == permission_id.strip())
        and (not executor_id or permission_record.executor_id == executor_id.strip())
    )
    permission_scope_valid = (
        permission_record is not None
        and permission_correlation_valid
        and (
            not permission_id
            or permission_record.permission_id == permission_id.strip()
        )
    )

    # --- Governed runtime session validation --------------------------------
    session_expires_dt: datetime | None = None
    derived_session_expired = False
    session_correlation_valid = True
    if session_record is None:
        blocking.append(BLOCK_GOVERNED_RUNTIME_SESSION_MISSING)
    else:
        session_expires_dt = _parse_iso(session_record.expires_at)
        if session_expires_dt is not None and current >= session_expires_dt:
            derived_session_expired = True
            blocking.append(BLOCK_GOVERNED_RUNTIME_SESSION_EXPIRED)
        if session_id and session_record.session_id != session_id.strip():
            blocking.append(BLOCK_SESSION_SCOPE_MISMATCH)
        if executor_id and session_record.executor_id != executor_id.strip():
            blocking.append(BLOCK_SESSION_EXECUTOR_MISMATCH)
        if (
            permission_record is not None
            and session_record.permission_id
            and session_record.permission_id != permission_record.permission_id
        ):
            session_correlation_valid = False
        if (
            contract is not None
            and session_record.cutover_contract_id
            and session_record.cutover_contract_id != contract.cutover_contract_id
        ):
            session_correlation_valid = False
        if (
            open_event_id
            and session_record.controlled_window_open_event_id
            and session_record.controlled_window_open_event_id != open_event_id
        ):
            session_correlation_valid = False
        if (
            not session_correlation_valid
            and BLOCK_GOVERNED_RUNTIME_SESSION_INVALID not in blocking
        ):
            blocking.append(BLOCK_GOVERNED_RUNTIME_SESSION_INVALID)

    session_valid = (
        session_record is not None
        and not derived_session_expired
        and (not session_id or session_record.session_id == session_id.strip())
        and (not executor_id or session_record.executor_id == executor_id.strip())
    )
    session_context_valid = session_record is not None and session_correlation_valid
    session_state = ""
    if session_record is not None:
        session_state = SESSION_EXPIRED_LABEL if derived_session_expired else SESSION_STARTED
    session_scope_valid = (
        one_shot_scope_valid
        and ticket_scope_valid
        and permission_scope_valid
        and session_valid
        and session_context_valid
        and runtime_permission_valid
    )

    window_end = _parse_iso(window.maintenance_window_end)
    ttl_valid, ttl_reason, effective_ttl, session_remaining, permission_remaining, window_remaining, expires_iso = (
        _validate_boundary_ttl(
            ttl_seconds,
            now=current,
            session_expires_at=session_expires_dt,
            permission_expires_at=permission_expires_dt,
            window_end=window_end,
        )
    )
    if ttl_seconds is not None and not ttl_valid:
        if ttl_reason == "exceeds_session":
            blocking.append(BLOCK_BOUNDARY_TTL_EXCEEDS_SESSION)
        elif ttl_reason == "exceeds_permission":
            blocking.append(BLOCK_BOUNDARY_TTL_EXCEEDS_PERMISSION)
        elif ttl_reason == "exceeds_window":
            blocking.append(BLOCK_BOUNDARY_TTL_EXCEEDS_WINDOW)
        else:
            blocking.append(BLOCK_BOUNDARY_TTL_INVALID)

    executor_valid, operator_valid, separation_valid, id_blocks = _assess_identities(
        executor_id=executor_id,
        operator_id=operator_id,
        request=request,
        contract=contract,
        final_record=final_record,
        op_record=op_record,
        permission_record=permission_record,
        session_record=session_record,
    )
    blocking.extend(id_blocks)

    production_execution_allowed = bool(force_production_execution_allowed)
    gateway_enabled = bool(force_gateway_enabled)
    discord_enabled = bool(force_discord_enabled)
    cutover_started_forced = bool(force_cutover_started)
    runtime_invoked_forced = bool(force_runtime_invoked)
    if production_execution_allowed:
        blocking.append(BLOCK_PRODUCTION_EXECUTION_ENABLED)
    if gateway_enabled:
        blocking.append(BLOCK_GATEWAY_PRODUCTION_ENABLED)
    if discord_enabled:
        blocking.append(BLOCK_DISCORD_PRODUCTION_ENABLED)
    if cutover_started_forced:
        blocking.append(BLOCK_CUTOVER_ALREADY_STARTED)
    if runtime_invoked_forced:
        blocking.append(BLOCK_RUNTIME_ALREADY_INVOKED)

    # --- Existing boundary idempotency / conflict ---------------------------
    boundary_id = ""
    invocation_id = ""
    reserved_at = ""
    expires_at = ""
    already_reserved = False
    boundary_present = existing is not None
    boundary_expired_flag = False
    boundary_conflict = False
    if existing is not None:
        boundary_id = existing.boundary_id
        invocation_id = existing.invocation_id
        reserved_at = existing.reserved_at
        expires_at = existing.expires_at
        existing_expires_dt = _parse_iso(existing.expires_at)
        if existing_expires_dt is not None and current >= existing_expires_dt:
            boundary_expired_flag = True
            blocking.append(BLOCK_BOUNDARY_EXPIRED)
        else:
            already_reserved = True
            if (
                permission_id
                or session_id
                or executor_id
                or operator_id
                or ttl_seconds is not None
            ):
                equivalent = (
                    (not permission_id or existing.permission_id == permission_id.strip())
                    and (not session_id or existing.session_id == session_id.strip())
                    and (not executor_id or existing.executor_id == executor_id.strip())
                    and (not operator_id or existing.operator_id == operator_id.strip())
                    and (ttl_seconds is None or existing.ttl_seconds == ttl_seconds)
                )
                if not equivalent:
                    boundary_conflict = True
                    blocking.append(BLOCK_GOVERNED_RUNTIME_BOUNDARY_CONFLICT)
            if not boundary_conflict:
                blocking.append(BLOCK_BOUNDARY_ALREADY_RESERVED)

    unique_blocking = tuple(dict.fromkeys(blocking))

    excluded_when_no_ttl = {
        BLOCK_BOUNDARY_TTL_INVALID,
        BLOCK_BOUNDARY_TTL_EXCEEDS_SESSION,
        BLOCK_BOUNDARY_TTL_EXCEEDS_PERMISSION,
        BLOCK_BOUNDARY_TTL_EXCEEDS_WINDOW,
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
            BLOCK_BOUNDARY_ALREADY_RESERVED,
        }
        and not (ttl_seconds is None and code in excluded_when_no_ttl)
        and not (not executor_id and not operator_id and code in excluded_when_no_identity)
        and not (not permission_id and code == BLOCK_PERMISSION_SCOPE_MISMATCH)
        and not (not executor_id and code == BLOCK_PERMISSION_EXECUTOR_MISMATCH)
        and not (not session_id and code == BLOCK_SESSION_SCOPE_MISMATCH)
        and not (not executor_id and code == BLOCK_SESSION_EXECUTOR_MISMATCH)
    ]

    core_ready = (
        contract_valid
        and window_scope_valid
        and runtime_permission_valid
        and session_valid
        and session_context_valid
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
        and not cutover_started_forced
        and not runtime_invoked_forced
        and runtime_factory_available
        and runtime_invoker_disabled
        and existing is None
        and not (kill_switch_required and not kill_switch_available)
        and not (emergency_close_required and not emergency_close_available)
    )

    boundary_ready = (
        core_ready
        and executor_valid
        and operator_valid
        and separation_valid
        and (not permission_id or BLOCK_PERMISSION_SCOPE_MISMATCH not in unique_blocking)
        and (not executor_id or BLOCK_PERMISSION_EXECUTOR_MISMATCH not in unique_blocking)
        and (not session_id or BLOCK_SESSION_SCOPE_MISMATCH not in unique_blocking)
        and (not executor_id or BLOCK_SESSION_EXECUTOR_MISMATCH not in unique_blocking)
        and ttl_seconds is not None
        and ttl_valid
        and not boundary_conflict
    )

    check_ready = (
        core_ready
        and bool((session_id or "").strip())
        and BLOCK_SESSION_SCOPE_MISMATCH not in unique_blocking
        and bool((permission_id or "").strip())
        and BLOCK_PERMISSION_SCOPE_MISMATCH not in unique_blocking
        and (
            not executor_id
            or (
                executor_valid
                and BLOCK_PERMISSION_EXECUTOR_MISMATCH not in unique_blocking
                and BLOCK_SESSION_EXECUTOR_MISMATCH not in unique_blocking
            )
        )
        and ttl_seconds is not None
        and ttl_valid
        and existing is None
    )

    if existing is not None and boundary_expired_flag:
        state = BOUNDARY_EXPIRED
        ready = False
    elif existing is not None and boundary_conflict:
        state = BOUNDARY_BLOCKED
        ready = False
    elif existing is not None and already_reserved:
        state = BOUNDARY_RESERVED
        ready = False
    elif recovery_required or repair_lock_held:
        state = BOUNDARY_BLOCKED
        ready = False
    elif boundary_ready:
        state = BOUNDARY_READY
        ready = True
    elif check_ready and not operator_id:
        state = BOUNDARY_READY
        ready = True
    elif hard_ready_blockers and not check_ready:
        state = BOUNDARY_BLOCKED
        ready = False
    else:
        state = BOUNDARY_NOT_RESERVED
        ready = False

    if existing is None and boundary_ready:
        state = BOUNDARY_READY
        ready = True

    # --- Cutover-start-requested event recorded ------------------------------
    cutover_start_event_recorded = False
    if existing is not None:
        try:
            existing_events = load_runtime_boundary_events(
                activation_request_id,
                store_dir=boundary_store_dir,
            )
            cutover_start_event_recorded = any(
                event.event_type == EVENT_CUTOVER_START_REQUESTED
                for event in existing_events
            )
        except RuntimeBoundaryError:
            cutover_start_event_recorded = False

    warnings.extend(
        [
            WARN_BOUNDARY_IS_PREREQUISITE_ONLY,
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
    if state == BOUNDARY_EXPIRED:
        warnings.append(WARN_BOUNDARY_EXPIRY_REQUIRES_NEW_SESSION)
    if state == BOUNDARY_RESERVED:
        warnings.append(WARN_RUNTIME_BOUNDARY_BLOCKED_WAITING_PHASE_15F)
    unique_warnings = tuple(dict.fromkeys(warnings))

    recommended = _recommended_action(
        state,
        unique_blocking,
        window_open=window.window_open,
        recovery=recovery_required or repair_lock_held,
    )

    return RuntimeBoundarySummary(
        activation_request_id=activation_request_id,
        cutover_contract_id=cutover_contract_id,
        permission_id=permission_record.permission_id if permission_record else "",
        session_id=session_record.session_id if session_record else "",
        boundary_id=boundary_id,
        invocation_id=invocation_id,
        boundary_state=state,
        boundary_ready=ready,
        boundary_present=boundary_present,
        controlled_window_state=window.window_state,
        controlled_window_open=window.window_open,
        controlled_window_expired=window.expired,
        governed_cutover_contract_valid=contract_valid,
        governed_cutover_status=governed_status,
        permission_valid=runtime_permission_valid,
        permission_state=runtime_permission_state,
        permission_expired=(
            BLOCK_RUNTIME_PERMISSION_EXPIRED in unique_blocking
            or runtime_permission_state == PERMISSION_EXPIRED
        ),
        permission_scope_valid=permission_scope_valid,
        session_valid=session_valid,
        session_state=session_state,
        session_expired=derived_session_expired,
        session_context_valid=session_context_valid,
        session_scope_valid=session_scope_valid,
        one_shot_scope_valid=one_shot_scope_valid,
        ticket_scope_valid=ticket_scope_valid,
        window_scope_valid=window_scope_valid,
        boundary_ttl_valid=ttl_valid if ttl_seconds is not None else True,
        final_signoff_valid=final_signoff_valid,
        rollback_ready=rollback_ready,
        operational_signoff_valid=operational_signoff_valid,
        audit_chain_complete=audit_chain_complete,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        executor_identity_valid=executor_valid if executor_id else True,
        operator_identity_valid=operator_valid if operator_id else True,
        identity_separation_valid=separation_valid if (executor_id and operator_id) else True,
        kill_switch_available=kill_switch_available,
        emergency_close_available=emergency_close_available,
        runtime_factory_available=runtime_factory_available,
        runtime_invoker_disabled=runtime_invoker_disabled,
        reserved_at=reserved_at,
        expires_at=expires_at or expires_iso,
        boundary_reserved=already_reserved,
        boundary_expired=boundary_expired_flag,
        cutover_start_event_recorded=cutover_start_event_recorded,
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
        already_reserved=already_reserved,
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
        session_remaining_seconds=session_remaining,
    )


def prepare_production_runtime_boundary(
    *,
    activation_request_id: str,
    session_id: str,
    permission_id: str,
    executor_id: str,
    operator_id: str,
    ttl_seconds: int,
    boundary_store_dir: Path | None = None,
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
) -> RuntimeBoundarySummary:
    """Append-only reserve of a one-shot runtime boundary.

    Never starts cutover (only appends a `cutover_start_requested` *event*
    while `cutover_started` stays False everywhere), never invokes runtime,
    never consumes the underlying permission — those remain exclusively
    Phase 15F's concern.
    """
    if not probe_runtime_boundary_store_available(store_dir=boundary_store_dir):
        raise RuntimeBoundaryError("boundary_write_failed")

    def _evaluate() -> RuntimeBoundarySummary:
        return evaluate_production_runtime_boundary(
            activation_request_id=activation_request_id,
            executor_id=executor_id,
            operator_id=operator_id,
            permission_id=permission_id,
            session_id=session_id,
            ttl_seconds=ttl_seconds,
            boundary_store_dir=boundary_store_dir,
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

    existing = load_runtime_boundary_record(
        activation_request_id,
        store_dir=boundary_store_dir,
    )
    if existing is not None:
        expires_dt = _parse_iso(existing.expires_at)
        if expires_dt is not None and _utc_now(now) >= expires_dt:
            raise RuntimeBoundaryError("boundary_expired")
        equivalent = (
            existing.permission_id == (permission_id or "").strip()
            and existing.session_id == (session_id or "").strip()
            and existing.executor_id == (executor_id or "").strip()
            and existing.operator_id == (operator_id or "").strip()
            and existing.ttl_seconds == int(ttl_seconds)
        )
        if equivalent:
            return _evaluate()
        raise RuntimeBoundaryError("runtime_boundary_conflict")

    if summary.boundary_state != BOUNDARY_READY:
        raise RuntimeBoundaryError(
            f"runtime boundary blocked for state {summary.boundary_state!r}"
        )

    contract = load_governed_cutover_contract(
        activation_request_id,
        store_dir=governed_cutover_store_dir,
    )
    permission_record = load_runtime_permission_record(
        activation_request_id,
        store_dir=permission_store_dir,
    )
    session_record = load_governed_runtime_session_record(
        activation_request_id,
        store_dir=session_store_dir,
    )
    reservation = load_execution_reservation(
        activation_request_id,
        store_dir=reservation_dir,
    )
    if (
        contract is None
        or permission_record is None
        or session_record is None
        or reservation is None
    ):
        raise RuntimeBoundaryError("boundary_store_corrupted")
    if session_record.session_id != (session_id or "").strip():
        raise RuntimeBoundaryError("session_scope_mismatch")
    _, window_events = load_window_lifecycle_events(
        activation_request_id,
        store_dir=window_store_dir,
    )
    open_event_id = _open_event_id(window_events)
    if not open_event_id:
        raise RuntimeBoundaryError("controlled_window_not_open")

    current = _utc_now(now)
    session_expires_dt = _parse_iso(session_record.expires_at)
    permission_expires_dt = _parse_iso(permission_record.expires_at)
    window_end = _parse_iso(contract.maintenance_window_end)
    ttl_valid, ttl_reason, effective_ttl, _, _, _, expires_iso = _validate_boundary_ttl(
        ttl_seconds,
        now=current,
        session_expires_at=session_expires_dt,
        permission_expires_at=permission_expires_dt,
        window_end=window_end,
    )
    if not ttl_valid:
        raise RuntimeBoundaryError(
            ttl_reason and f"boundary_ttl_{ttl_reason}" or "boundary_ttl_invalid"
        )

    boundary_id = str(uuid.uuid4())
    invocation_id = str(uuid.uuid4())
    reserved_at = _utc_now_iso(now)
    record = RuntimeBoundaryRecord(
        boundary_id=boundary_id,
        activation_request_id=activation_request_id,
        cutover_contract_id=contract.cutover_contract_id,
        permission_id=permission_record.permission_id,
        session_id=session_record.session_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=contract.execution_attempt_id,
        dispatch_run_id=contract.dispatch_run_id,
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        controlled_window_open_event_id=open_event_id,
        executor_id=(executor_id or "").strip(),
        operator_id=(operator_id or "").strip(),
        reserved_by=(operator_id or "").strip(),
        reserved_at=reserved_at,
        expires_at=expires_iso,
        ttl_seconds=effective_ttl,
        scope_type=SCOPE_TYPE_ONE_SHOT,
        boundary_status=BOUNDARY_RESERVED,
        invocation_id=invocation_id,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
        cutover_start_requested_at=reserved_at,
    )
    events = (
        RuntimeBoundaryEvent(
            event_id=str(uuid.uuid4()),
            boundary_id=boundary_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_BOUNDARY_RESERVE_REQUESTED,
            actor_role="operator",
            reason_code="",
            occurred_at=reserved_at,
        ),
        RuntimeBoundaryEvent(
            event_id=str(uuid.uuid4()),
            boundary_id=boundary_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_BOUNDARY_RESERVED,
            actor_role="operator",
            reason_code="",
            occurred_at=reserved_at,
        ),
        RuntimeBoundaryEvent(
            event_id=str(uuid.uuid4()),
            boundary_id=boundary_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_CUTOVER_START_REQUESTED,
            actor_role="operator",
            reason_code="",
            occurred_at=reserved_at,
        ),
        RuntimeBoundaryEvent(
            event_id=str(uuid.uuid4()),
            boundary_id=boundary_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_RUNTIME_BOUNDARY_BLOCKED,
            actor_role="system",
            reason_code="",
            occurred_at=reserved_at,
        ),
    )
    _write_boundary_bundle(record, events, store_dir=boundary_store_dir)
    return _evaluate()


class RuntimeBoundaryContext:
    """Read-only handle onto a reserved runtime boundary.

    Entering re-validates that the boundary is still live (not expired, the
    controlled window is still open, the underlying session is still
    started and unexpired, and the underlying permission is still issued and
    unconsumed) before exposing any state. Never invokes runtime, never
    starts cutover, never consumes the permission. The context is single-use:
    it cannot be entered while already active and cannot be re-entered after
    it has been exited.
    """

    def __init__(
        self,
        boundary_id: str = "",
        *,
        activation_request_id: str = "",
        boundary_store_dir: Path | None = None,
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
        self.boundary_id = (boundary_id or "").strip()
        self.activation_request_id = (activation_request_id or "").strip()
        if not self.boundary_id and not self.activation_request_id:
            raise RuntimeBoundaryError(
                "boundary_id or activation_request_id is required"
            )
        self.session_id = ""
        self.permission_id = ""
        self.invocation_id = ""
        self._executor_id = ""
        self.entered_at = ""
        self.expires_at = ""
        self.active = False
        self.consumed = False
        self._entered_once = False
        self._now = now
        self._boundary_store_dir = boundary_store_dir
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

    def __enter__(self) -> "RuntimeBoundaryContext":
        if self.active:
            raise RuntimeBoundaryError(
                "runtime boundary context is already active"
            )
        if self._entered_once:
            raise RuntimeBoundaryError(
                "runtime boundary context cannot be reused after exit"
            )

        record: RuntimeBoundaryRecord | None = None
        if self.activation_request_id:
            record = load_runtime_boundary_record(
                self.activation_request_id, store_dir=self._boundary_store_dir
            )
            if record is None or (
                self.boundary_id and record.boundary_id != self.boundary_id
            ):
                raise RuntimeBoundaryError(
                    "governed_runtime_boundary_not_found"
                )
        else:
            record, _ = load_runtime_boundary_events_by_boundary_id(
                self.boundary_id, store_dir=self._boundary_store_dir
            )
            if record is None:
                raise RuntimeBoundaryError(
                    "governed_runtime_boundary_not_found"
                )
            self.activation_request_id = record.activation_request_id

        assert record is not None
        current = _utc_now(self._now)
        expires_dt = _parse_iso(record.expires_at)
        if expires_dt is None or current >= expires_dt:
            raise RuntimeBoundaryError("boundary_expired")

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
            raise RuntimeBoundaryError("controlled_window_not_open")

        try:
            session_record = load_governed_runtime_session_record(
                self.activation_request_id, store_dir=self._session_store_dir
            )
        except ProductionGovernedRuntimeSessionError as exc:
            raise RuntimeBoundaryError(
                "boundary_store_corrupted"
            ) from exc
        if session_record is None or session_record.session_id != record.session_id:
            raise RuntimeBoundaryError(
                "governed_runtime_session_invalid"
            )
        session_expires_dt = _parse_iso(session_record.expires_at)
        if session_expires_dt is None or current >= session_expires_dt:
            raise RuntimeBoundaryError("governed_runtime_session_expired")

        try:
            permission_record = load_runtime_permission_record(
                self.activation_request_id, store_dir=self._permission_store_dir
            )
        except ProductionRuntimePermissionError as exc:
            raise RuntimeBoundaryError(
                "boundary_store_corrupted"
            ) from exc
        if permission_record is None or permission_record.permission_id != record.permission_id:
            raise RuntimeBoundaryError("runtime_permission_invalid")
        if (
            permission_record.permission_status != PERMISSION_ISSUED
            or permission_record.consumed
            or permission_record.revoked
        ):
            raise RuntimeBoundaryError("runtime_permission_invalid")
        permission_expires_dt = _parse_iso(permission_record.expires_at)
        if permission_expires_dt is None or current >= permission_expires_dt:
            raise RuntimeBoundaryError("runtime_permission_expired")

        self.boundary_id = record.boundary_id
        self.session_id = record.session_id
        self.permission_id = record.permission_id
        self.invocation_id = record.invocation_id
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
        raise RuntimeBoundaryError(
            "runtime boundary context cannot be serialized"
        )

    def __setstate__(self, state):
        raise RuntimeBoundaryError(
            "runtime boundary context cannot be serialized"
        )

    def __reduce__(self):
        raise RuntimeBoundaryError(
            "runtime boundary context cannot be serialized"
        )


def enter_governed_runtime_boundary_context(
    *,
    boundary_id: str = "",
    activation_request_id: str = "",
    **kwargs: Any,
) -> RuntimeBoundaryContext:
    """Factory returning an unentered context manager; use via `with`."""
    return RuntimeBoundaryContext(
        boundary_id,
        activation_request_id=activation_request_id,
        **kwargs,
    )


_BOUNDARY_CONSUME_STORE_DIR = "production-runtime-boundary-consume"


def default_runtime_boundary_consume_store_dir() -> Path:
    return get_hermes_home() / "coo" / _BOUNDARY_CONSUME_STORE_DIR


def _boundary_consume_path(
    boundary_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (boundary_id or "").strip()
    if not normalized:
        raise RuntimeBoundaryError("boundary_id is required")
    base = store_dir or default_runtime_boundary_consume_store_dir()
    return base / f"{normalized}.json"


def load_runtime_boundary_consume_record(
    boundary_id: str,
    *,
    store_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return the consume record for boundary_id, or None if unconsumed."""
    from agent.coo.production_runtime_consume_store import read_consume_record

    path = _boundary_consume_path(boundary_id, store_dir=store_dir)
    try:
        return read_consume_record(path)
    except ValueError as exc:
        raise RuntimeBoundaryError(str(exc)) from exc


def consume_runtime_boundary(
    activation_request_id: str,
    *,
    boundary_id: str,
    consumed_by: str,
    store_dir: Path | None = None,
    consume_store_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One-shot consume transition for a reserved runtime boundary.

    Never mutates the original write-once boundary bundle. Consumption is
    recorded as a separate one-shot artifact, mirroring
    ``consume_production_runtime_permission``.
    """
    normalized_boundary_id = (boundary_id or "").strip()
    normalized_consumed_by = (consumed_by or "").strip()
    if not normalized_boundary_id:
        raise RuntimeBoundaryError("boundary_id is required")
    if not normalized_consumed_by:
        raise RuntimeBoundaryError("consumed_by is required")

    record = load_runtime_boundary_record(activation_request_id, store_dir=store_dir)
    if record is None:
        raise RuntimeBoundaryError("boundary_missing")
    if record.boundary_id != normalized_boundary_id:
        raise RuntimeBoundaryError("boundary_id_mismatch")
    if record.boundary_status != BOUNDARY_RESERVED:
        raise RuntimeBoundaryError("boundary_not_reserved")
    if record.revoked:
        raise RuntimeBoundaryError("boundary_revoked")

    current = _utc_now(now)
    expires_dt = _parse_iso(record.expires_at)
    if expires_dt is not None and current >= expires_dt:
        raise RuntimeBoundaryError("boundary_expired")

    if (
        load_runtime_boundary_consume_record(
            normalized_boundary_id, store_dir=consume_store_dir
        )
        is not None
    ):
        raise RuntimeBoundaryError("boundary_already_consumed")

    from agent.coo.production_runtime_consume_store import (
        OneShotConsumeWriteConflict,
        write_once_consume_record,
    )

    payload = {
        "version": 1,
        "boundary_id": normalized_boundary_id,
        "activation_request_id": activation_request_id,
        "consumed": True,
        "consumed_at": _utc_now_iso(now),
        "consumed_by": normalized_consumed_by,
    }
    path = _boundary_consume_path(normalized_boundary_id, store_dir=consume_store_dir)
    try:
        write_once_consume_record(path, payload)
    except OneShotConsumeWriteConflict as exc:
        raise RuntimeBoundaryError("boundary_already_consumed") from exc
    return payload


def build_production_runtime_boundary_release_summary(
    summary: RuntimeBoundarySummary,
) -> RuntimeBoundaryReleaseSummary:
    if (
        summary.boundary_state == BOUNDARY_RESERVED
        and summary.session_state == SESSION_STARTED
        and summary.permission_state == PERMISSION_ISSUED
        and summary.controlled_window_open
    ):
        release_status = RELEASE_RUNTIME_BOUNDARY_RESERVED
        next_phase = _NEXT_PHASE_15F
    elif summary.boundary_state == BOUNDARY_EXPIRED:
        release_status = RELEASE_RUNTIME_BOUNDARY_EXPIRED
        next_phase = ""
    elif summary.recovery_required or summary.repair_lock_held:
        release_status = RELEASE_GOVERNED_RUNTIME_BOUNDARY_RECOVERY_REQUIRED
        next_phase = ""
    elif summary.boundary_ready or summary.boundary_state == BOUNDARY_READY:
        release_status = RELEASE_RUNTIME_BOUNDARY_READY_TO_PREPARE
        next_phase = ""
    else:
        release_status = RELEASE_RUNTIME_BOUNDARY_NOT_READY
        next_phase = ""

    return RuntimeBoundaryReleaseSummary(
        activation_request_id=summary.activation_request_id,
        cutover_contract_id=summary.cutover_contract_id,
        permission_id=summary.permission_id,
        session_id=summary.session_id,
        boundary_id=summary.boundary_id,
        controlled_window_state=summary.controlled_window_state,
        permission_state=summary.permission_state,
        session_state=summary.session_state,
        boundary_state=summary.boundary_state,
        boundary_ready=summary.boundary_ready,
        boundary_present=summary.boundary_present,
        boundary_expired=summary.boundary_state == BOUNDARY_EXPIRED,
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


def resolve_latest_runtime_boundary_dashboard_digest(
    *,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
    session_store_dir: Path | None = None,
    boundary_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> RuntimeBoundaryDashboardDigest:
    base = (governed_cutover_store_dir or default_governed_cutover_store_dir()).resolve()
    if not base.is_dir():
        return RuntimeBoundaryDashboardDigest(
            runtime_boundary_state="not_configured",
            runtime_boundary_ready=False,
            runtime_boundary_present=False,
            runtime_boundary_expired=False,
            runtime_boundary_id="",
            runtime_boundary_invocation_id="",
            runtime_boundary_expires_at="",
            runtime_boundary_blocking_count=0,
            runtime_boundary_warning_count=0,
            runtime_boundary_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )
    paths = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:500]:
        activation_id = path.stem
        try:
            summary = evaluate_production_runtime_boundary(
                activation_request_id=activation_id,
                governed_cutover_store_dir=governed_cutover_store_dir,
                window_store_dir=window_store_dir,
                permission_store_dir=permission_store_dir,
                session_store_dir=session_store_dir,
                boundary_store_dir=boundary_store_dir,
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
        if not summary.governed_cutover_contract_valid and not summary.boundary_present:
            continue
        return RuntimeBoundaryDashboardDigest(
            runtime_boundary_state=summary.boundary_state,
            runtime_boundary_ready=summary.boundary_ready,
            runtime_boundary_present=summary.boundary_present,
            runtime_boundary_expired=summary.boundary_state == BOUNDARY_EXPIRED,
            runtime_boundary_id=summary.boundary_id,
            runtime_boundary_invocation_id=summary.invocation_id,
            runtime_boundary_expires_at=summary.expires_at,
            runtime_boundary_blocking_count=len(summary.blocking_items),
            runtime_boundary_warning_count=len(summary.warning_items),
            runtime_boundary_recommended_action=summary.recommended_action,
        )
    return RuntimeBoundaryDashboardDigest(
        runtime_boundary_state="not_configured",
        runtime_boundary_ready=False,
        runtime_boundary_present=False,
        runtime_boundary_expired=False,
        runtime_boundary_id="",
        runtime_boundary_invocation_id="",
        runtime_boundary_expires_at="",
        runtime_boundary_blocking_count=0,
        runtime_boundary_warning_count=0,
        runtime_boundary_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
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
        "runtime_factory_available: true",
        "runtime_factory_available: false",
        "runtime_invoker_disabled: true",
        "runtime_invoker_disabled: false",
        "cutover_start_event_recorded: true",
        "cutover_start_event_recorded: false",
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
        "session_scope_mismatch",
        "session_executor_mismatch",
    ):
        lowered = lowered.replace(label, "")
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise RuntimeBoundaryError(
                f"Unsafe runtime boundary output field: {token!r}"
            )


def format_production_runtime_boundary_status(
    summary: RuntimeBoundarySummary,
) -> str:
    lines = [
        "Production Runtime Boundary Status",
        "",
        f"activation_request_id: {summary.activation_request_id}",
        f"cutover_contract_id: {summary.cutover_contract_id or '(none)'}",
        f"permission_id: {summary.permission_id or '(none)'}",
        f"session_id: {summary.session_id or '(none)'}",
        f"boundary_id: {summary.boundary_id or '(none)'}",
        f"invocation_id: {summary.invocation_id or '(none)'}",
        f"boundary_state: {summary.boundary_state}",
        f"boundary_ready: {str(summary.boundary_ready).lower()}",
        f"boundary_present: {str(summary.boundary_present).lower()}",
        f"controlled_window_state: {summary.controlled_window_state or '(none)'}",
        f"controlled_window_open: {str(summary.controlled_window_open).lower()}",
        f"controlled_window_expired: {str(summary.controlled_window_expired).lower()}",
        "governed_cutover_contract_valid: "
        f"{str(summary.governed_cutover_contract_valid).lower()}",
        f"governed_cutover_status: {summary.governed_cutover_status or '(none)'}",
        f"permission_valid: {str(summary.permission_valid).lower()}",
        f"permission_state: {summary.permission_state or '(none)'}",
        f"permission_scope_valid: {str(summary.permission_scope_valid).lower()}",
        f"session_valid: {str(summary.session_valid).lower()}",
        f"session_state: {summary.session_state or '(none)'}",
        f"session_context_valid: {str(summary.session_context_valid).lower()}",
        f"session_scope_valid: {str(summary.session_scope_valid).lower()}",
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
        f"boundary_ttl_valid: {str(summary.boundary_ttl_valid).lower()}",
        f"ttl_seconds: {summary.ttl_seconds}",
        f"kill_switch_available: {str(summary.kill_switch_available).lower()}",
        f"emergency_close_available: {str(summary.emergency_close_available).lower()}",
        f"runtime_factory_available: {str(summary.runtime_factory_available).lower()}",
        f"runtime_invoker_disabled: {str(summary.runtime_invoker_disabled).lower()}",
        f"window_remaining_seconds: {summary.window_remaining_seconds}",
        f"permission_remaining_seconds: {summary.permission_remaining_seconds}",
        f"session_remaining_seconds: {summary.session_remaining_seconds}",
        f"reserved_at: {summary.reserved_at or '(none)'}",
        f"expires_at: {summary.expires_at or '(none)'}",
        f"boundary_reserved: {str(summary.boundary_reserved).lower()}",
        f"boundary_expired: {str(summary.boundary_expired).lower()}",
        f"cutover_start_event_recorded: {str(summary.cutover_start_event_recorded).lower()}",
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
        f"already_reserved: {str(summary.already_reserved).lower()}",
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


def format_production_runtime_boundary_record(
    record: RuntimeBoundaryRecord,
    *,
    now: datetime | None = None,
) -> str:
    expires_dt = _parse_iso(record.expires_at)
    expired = bool(expires_dt and _utc_now(now) >= expires_dt)
    state = BOUNDARY_EXPIRED if expired else BOUNDARY_RESERVED
    lines = [
        "Production Runtime Boundary",
        "",
        f"boundary_id: {record.boundary_id}",
        f"activation_request_id: {record.activation_request_id}",
        f"cutover_contract_id: {record.cutover_contract_id}",
        f"permission_id: {record.permission_id}",
        f"session_id: {record.session_id}",
        f"reservation_id: {record.reservation_id}",
        f"execution_attempt_id: {record.execution_attempt_id}",
        f"dispatch_run_id: {record.dispatch_run_id}",
        f"invocation_id: {record.invocation_id}",
        f"boundary_status: {state}",
        f"ttl_seconds: {record.ttl_seconds}",
        f"reserved_at: {record.reserved_at}",
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


def format_production_runtime_boundary_history(
    activation_request_id: str,
    events: tuple[RuntimeBoundaryEvent, ...],
    *,
    boundary_id: str = "",
) -> str:
    lines = [
        "Production Runtime Boundary History",
        "",
        f"activation_request_id: {activation_request_id}",
        f"boundary_id: {boundary_id or '(none)'}",
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


def run_production_runtime_boundary_status(
    *,
    activation_request_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_runtime_boundary(
            activation_request_id=activation_request_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except RuntimeBoundaryError:
        return "error: runtime boundary status unavailable", 1
    return format_production_runtime_boundary_status(summary), 0


def run_production_runtime_boundary_check(
    *,
    activation_request_id: str,
    session_id: str,
    permission_id: str,
    executor_id: str = "",
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
    ttl_seconds: int = 60,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_runtime_boundary(
            activation_request_id=activation_request_id,
            executor_id=executor_id,
            session_id=session_id,
            permission_id=permission_id,
            ttl_seconds=ttl_seconds,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except RuntimeBoundaryError:
        return "error: runtime boundary check unavailable", 1
    exit_code = 0 if summary.boundary_state == BOUNDARY_READY else 1
    return format_production_runtime_boundary_status(summary), exit_code


def run_production_runtime_boundary_prepare(
    *,
    activation_request_id: str,
    session_id: str,
    permission_id: str,
    executor_id: str,
    operator_id: str,
    ttl_seconds: int,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = prepare_production_runtime_boundary(
            activation_request_id=activation_request_id,
            session_id=session_id,
            permission_id=permission_id,
            executor_id=executor_id,
            operator_id=operator_id,
            ttl_seconds=ttl_seconds,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except RuntimeBoundaryError:
        try:
            summary = evaluate_production_runtime_boundary(
                activation_request_id=activation_request_id,
                executor_id=executor_id,
                operator_id=operator_id,
                session_id=session_id,
                permission_id=permission_id,
                ttl_seconds=ttl_seconds,
                merged_config=merged_config,
                repo_root=repo_root,
            )
            return format_production_runtime_boundary_status(summary), 1
        except RuntimeBoundaryError:
            return "error: runtime boundary reserve failed", 1
    exit_code = (
        0
        if summary.boundary_state == BOUNDARY_RESERVED or summary.already_reserved
        else 1
    )
    return format_production_runtime_boundary_status(summary), exit_code


def run_production_runtime_boundary_show(
    *,
    boundary_id: str,
) -> tuple[str, int]:
    try:
        record = load_runtime_boundary_by_id(boundary_id)
    except RuntimeBoundaryError:
        return "error: runtime boundary corrupted", 1
    if record is None:
        return "error: runtime boundary not found", 1
    return format_production_runtime_boundary_record(record), 0


def run_production_runtime_boundary_history(
    *,
    activation_request_id: str,
) -> tuple[str, int]:
    try:
        record = load_runtime_boundary_record(activation_request_id)
        events = load_runtime_boundary_events(activation_request_id)
    except RuntimeBoundaryError:
        return "error: runtime boundary history unavailable", 1
    return (
        format_production_runtime_boundary_history(
            activation_request_id,
            events,
            boundary_id=record.boundary_id if record else "",
        ),
        0,
    )
