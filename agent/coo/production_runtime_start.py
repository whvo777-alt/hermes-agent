"""Governed runtime start contract — Phase 15H.

One-shot append-only *contract* prerequisite bound to an issued execution
authorization (Phase 15G), a reserved runtime invocation (Phase 15F), a
reserved runtime boundary (Phase 15E), a started governed runtime session,
an issued/unconsumed runtime permission, and an open controlled window.

Starting a governed runtime start contract is NOT runtime invocation
itself, NOT cutover, and NOT permission/authorization consumption — those
remain exclusively Phase 15I's (and later phases') concern. This module
creates a *contract* that records the fact that the operator/executor
chain is ready to proceed to the next phase; it never touches the
Repository2 runtime, never runs a subprocess, and never mutates any
upstream artifact from Phase 15A-15G.

The one boolean this phase is allowed to flip to true is
``runtime_started`` — and only on the *new* runtime-start record/summary
it creates. Every other safety flag (``production_execution_allowed``,
``cutover_started``, ``runtime_invoked``, ``permission_consumed``,
``authorization_consumed``, ...) remains hard-denied/false in every output
this module ever produces, including the freshly written record.

Storage layout:
    Like Phase 15E/15F/15G, this module keeps a *single atomic bundle per
    activation request* — the runtime-start record and its lifecycle
    events live together in one JSON file at
    ``~/.hermes/coo/production-runtime-start/{activation_request_id}.json``.
    This mirrors the boundary/invocation/authorization modules' storage
    layout exactly (rather than dual-writing events to a separate
    `production-runtime-start-events/{runtime_start_id}.json` path)
    because a single bundle keeps the record and its events atomically
    consistent under the same flock + O_EXCL write, with no risk of the
    two halves diverging after a crash mid-write.
    `load_runtime_start_by_id` / `load_runtime_start_events_by_runtime_start_id`
    scan the store when lookup by opaque id (rather than activation id) is
    required.

Invariants enforced everywhere in this module:
    - production_execution_allowed is always False in every output.
    - production_root_hard_deny is always True in every output.
    - cutover_started is always False in every output. This module never
      appends a cutover-start event — cutover remains exclusively out of
      scope here too.
    - runtime_invoked is always False in every output. Starting a
      governed runtime start contract is a *prerequisite* for runtime
      invocation, never the invocation itself. No automatic runtime
      invocation ever follows a successful start call. Start != invoke.
    - permission_consumed and permission_revoked are always False — this
      module never consumes or revokes a runtime permission.
    - authorization_consumed is always False — this module never
      consumes the underlying execution authorization; it only reads it.
    - consumed and revoked (on the runtime-start record itself) are
      always False. `RUNTIME_START_COMPLETED` / `RUNTIME_START_FAILED` /
      `RUNTIME_START_REVOKED` are defined as forward references for a
      future phase only and are never persisted by this module.
    - The persisted `runtime_start_status` is always
      `RUNTIME_START_STARTED` on a written record — no other status is
      ever written to disk.
    - `runtime_started` is True on the freshly written record/summary
      ONLY — it is never mutated back onto the authorization, invocation,
      boundary, session, or permission artifacts, and it never implies
      `runtime_invoked`.
    - No subprocess, no `create_bounded_subprocess_runner` call, no
      Repository2 execution, no publish, no Gateway/Discord production
      path. The runner-factory check is purely structural (is the class
      importable?) and never invokes the runner.
    - Boundary reserved != invocation reserved != authorization issued !=
      runtime start started != cutover started != runtime invoked !=
      permission consumed. These are seven distinct, sequential gates and
      this module only ever advances the fifth one (creating a
      runtime-start contract record on top of an already-issued execution
      authorization).
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
from agent.coo.production_execution_authorization import (
    AUTHORIZATION_EXPIRED,
    AUTHORIZATION_ISSUED,
    ProductionExecutionAuthorizationError,
    load_execution_authorization_record,
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
from agent.coo.production_runtime_boundary import (
    BOUNDARY_EXPIRED,
    BOUNDARY_RESERVED,
    RuntimeBoundaryError,
    _check_runtime_factory_available,
    _check_runtime_invoker_disabled,
    load_runtime_boundary_consume_record,
    load_runtime_boundary_record,
)
from agent.coo.production_runtime_invocation import (
    INVOCATION_EXPIRED,
    INVOCATION_RESERVED,
    ProductionRuntimeInvocationError,
    load_runtime_invocation_consume_record,
    load_runtime_invocation_record,
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

_RUNTIME_START_STORE_DIR = "production-runtime-start"
_RUNTIME_START_STORE_VERSION = 1
_NEXT_PHASE_15I = "Phase_15I_governed_runtime_invoke"

RUNTIME_START_NOT_STARTED = "RUNTIME_START_NOT_STARTED"
RUNTIME_START_READY = "RUNTIME_START_READY"
RUNTIME_START_STARTED = "RUNTIME_START_STARTED"
RUNTIME_START_EXPIRED = "RUNTIME_START_EXPIRED"
RUNTIME_START_BLOCKED = "RUNTIME_START_BLOCKED"

# Defined for forward reference by later phases only. NEVER persisted by
# this module — see the module docstring and `_record_to_dict` below,
# which always writes `runtime_start_status=RUNTIME_START_STARTED`.
RUNTIME_START_COMPLETED = "RUNTIME_START_COMPLETED"
RUNTIME_START_FAILED = "RUNTIME_START_FAILED"
RUNTIME_START_REVOKED = "RUNTIME_START_REVOKED"

SCOPE_TYPE_ONE_SHOT = "one_shot"
MIN_RUNTIME_START_TTL_SECONDS = 5
MAX_RUNTIME_START_TTL_SECONDS = 30
MIN_TTL_SECONDS = MIN_RUNTIME_START_TTL_SECONDS
MAX_TTL_SECONDS = MAX_RUNTIME_START_TTL_SECONDS

# `session_state` mirrors the boundary/invocation/authorization modules'
# derived-label convention: the underlying session record never persists
# a distinct "expired" status field, so this module derives the same
# label locally at evaluation time.
SESSION_EXPIRED_LABEL = "SESSION_EXPIRED"

EVENT_RUNTIME_START_REQUESTED = "runtime_start_requested"
EVENT_RUNTIME_START_STARTED = "runtime_start_started"
EVENT_RUNTIME_EXECUTION_BLOCKED = "runtime_execution_blocked_waiting_phase_15i"

RELEASE_GOVERNED_RUNTIME_START_READY = "GOVERNED_RUNTIME_START_READY"
RELEASE_GOVERNED_RUNTIME_START_STARTED = "GOVERNED_RUNTIME_START_STARTED"
RELEASE_GOVERNED_RUNTIME_START_EXPIRED = "GOVERNED_RUNTIME_START_EXPIRED"
RELEASE_GOVERNED_RUNTIME_START_NOT_READY = "GOVERNED_RUNTIME_START_NOT_READY"
RELEASE_GOVERNED_RUNTIME_START_RECOVERY_REQUIRED = (
    "GOVERNED_RUNTIME_START_RECOVERY_REQUIRED"
)

ACTION_START_GOVERNED_RUNTIME = "start_governed_runtime"
ACTION_RUNTIME_START_STARTED_WAIT_FOR_PHASE_15I = (
    "governed_runtime_start_started_wait_for_phase_15i"
)
ACTION_REVIEW_RUNTIME_START_WARNINGS = "review_runtime_start_warnings"
ACTION_WAIT_FOR_WINDOW_OPEN = "wait_for_window_open"
ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT = "prepare_new_governed_cutover_contract"
ACTION_RESOLVE_RUNTIME_PERMISSION = "resolve_runtime_permission"
ACTION_RESOLVE_GOVERNED_RUNTIME_SESSION = "resolve_governed_runtime_session"
ACTION_RESOLVE_RUNTIME_BOUNDARY = "resolve_runtime_boundary"
ACTION_RESOLVE_RUNTIME_INVOCATION = "resolve_runtime_invocation"
ACTION_RESOLVE_EXECUTION_AUTHORIZATION = "resolve_execution_authorization"
ACTION_RESOLVE_IDENTITY_SEPARATION = "resolve_identity_separation"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_CLOSE_OR_EMERGENCY_CLOSE_WINDOW = "close_or_emergency_close_window"
ACTION_RESOLVE_KILL_SWITCH = "resolve_kill_switch"
ACTION_RESOLVE_RUNTIME_FACTORY = "resolve_runtime_factory"
ACTION_DISABLE_RUNTIME_INVOKER = "disable_runtime_invoker"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_PREPARE_PHASE_15I_GOVERNED_RUNTIME_INVOKE = (
    "prepare_phase_15i_governed_runtime_invoke"
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
BLOCK_PERMISSION_EXECUTOR_MISMATCH = "permission_executor_mismatch"
BLOCK_GOVERNED_RUNTIME_SESSION_MISSING = "governed_runtime_session_missing"
BLOCK_GOVERNED_RUNTIME_SESSION_INVALID = "governed_runtime_session_invalid"
BLOCK_GOVERNED_RUNTIME_SESSION_EXPIRED = "governed_runtime_session_expired"
BLOCK_SESSION_EXECUTOR_MISMATCH = "session_executor_mismatch"
BLOCK_RUNTIME_BOUNDARY_MISSING = "runtime_boundary_missing"
BLOCK_RUNTIME_BOUNDARY_INVALID = "runtime_boundary_invalid"
BLOCK_RUNTIME_BOUNDARY_EXPIRED = "runtime_boundary_expired"
BLOCK_RUNTIME_BOUNDARY_INVOCATION_ID_MISSING = "runtime_boundary_invocation_id_missing"
BLOCK_RUNTIME_BOUNDARY_RUNTIME_INVOKED = "runtime_boundary_runtime_invoked"
BLOCK_RUNTIME_BOUNDARY_CUTOVER_STARTED = "runtime_boundary_cutover_started"
BLOCK_RUNTIME_BOUNDARY_EXECUTOR_MISMATCH = "runtime_boundary_executor_mismatch"
BLOCK_RUNTIME_BOUNDARY_SCOPE_MISMATCH = "runtime_boundary_scope_mismatch"
BLOCK_RUNTIME_BOUNDARY_CONSUMED = "runtime_boundary_consumed"
BLOCK_RUNTIME_BOUNDARY_REVOKED = "runtime_boundary_revoked"
BLOCK_RUNTIME_INVOCATION_MISSING = "runtime_invocation_missing"
BLOCK_RUNTIME_INVOCATION_INVALID = "runtime_invocation_invalid"
BLOCK_RUNTIME_INVOCATION_EXPIRED = "runtime_invocation_expired"
BLOCK_RUNTIME_INVOCATION_EXECUTOR_MISMATCH = "runtime_invocation_executor_mismatch"
BLOCK_RUNTIME_INVOCATION_CONSUMED = "runtime_invocation_consumed"
BLOCK_RUNTIME_INVOCATION_REVOKED = "runtime_invocation_revoked"
BLOCK_EXECUTION_AUTHORIZATION_MISSING = "execution_authorization_missing"
BLOCK_EXECUTION_AUTHORIZATION_INVALID = "execution_authorization_invalid"
BLOCK_EXECUTION_AUTHORIZATION_EXPIRED = "execution_authorization_expired"
BLOCK_EXECUTION_AUTHORIZATION_ID_MISMATCH = "execution_authorization_id_mismatch"
BLOCK_EXECUTION_AUTHORIZATION_EXECUTOR_MISMATCH = (
    "execution_authorization_executor_mismatch"
)
BLOCK_EXECUTION_AUTHORIZATION_NOT_VERIFIED = "execution_authorization_not_verified"
BLOCK_EXECUTION_AUTHORIZATION_CONSUMED = "execution_authorization_consumed"
BLOCK_EXECUTION_AUTHORIZATION_REVOKED = "execution_authorization_revoked"
BLOCK_EXECUTOR_IDENTITY_INVALID = "executor_identity_invalid"
BLOCK_OPERATOR_IDENTITY_INVALID = "operator_identity_invalid"
BLOCK_SUPERVISOR_IDENTITY_INVALID = "supervisor_identity_invalid"
BLOCK_IDENTITY_SEPARATION_INVALID = "identity_separation_invalid"
BLOCK_RUNTIME_START_TTL_INVALID = "runtime_start_ttl_invalid"
BLOCK_RUNTIME_START_TTL_EXCEEDS_INVOCATION = "runtime_start_ttl_exceeds_invocation"
BLOCK_RUNTIME_START_TTL_EXCEEDS_BOUNDARY = "runtime_start_ttl_exceeds_boundary"
BLOCK_RUNTIME_START_TTL_EXCEEDS_SESSION = "runtime_start_ttl_exceeds_session"
BLOCK_RUNTIME_START_TTL_EXCEEDS_PERMISSION = "runtime_start_ttl_exceeds_permission"
BLOCK_RUNTIME_START_TTL_EXCEEDS_WINDOW = "runtime_start_ttl_exceeds_window"
BLOCK_RUNTIME_START_TTL_EXCEEDS_AUTHORIZATION = "runtime_start_ttl_exceeds_authorization"
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
BLOCK_PRODUCTION_EXECUTION_ENABLED = "production_execution_enabled"
BLOCK_GATEWAY_PRODUCTION_ENABLED = "gateway_production_enabled"
BLOCK_DISCORD_PRODUCTION_ENABLED = "discord_production_enabled"
BLOCK_CUTOVER_ALREADY_STARTED = "cutover_already_started"
BLOCK_RUNTIME_ALREADY_INVOKED = "runtime_already_invoked"
BLOCK_RUNTIME_START_ALREADY_STARTED = "runtime_start_already_started"
BLOCK_RUNTIME_START_CONFLICT = "runtime_start_conflict"
BLOCK_RUNTIME_START_EXPIRED = "runtime_start_expired"
BLOCK_RUNTIME_START_STORE_CORRUPTED = "runtime_start_store_corrupted"
BLOCK_RUNTIME_START_WRITE_FAILED = "runtime_start_write_failed"
BLOCK_UNSAFE_OUTPUT = "unsafe_output"

WARN_RUNTIME_START_IS_PREREQUISITE_ONLY = "runtime_start_is_prerequisite_only"
WARN_RUNTIME_NOT_INVOKED = "runtime_not_invoked"
WARN_CUTOVER_NOT_STARTED = "cutover_not_started"
WARN_PERMISSION_NOT_CONSUMED = "permission_not_consumed"
WARN_AUTHORIZATION_NOT_CONSUMED = "authorization_not_consumed"
WARN_PRODUCTION_EXECUTION_DISABLED = "production_execution_disabled"
WARN_PRODUCTION_ROOT_HARD_DENIED = "production_root_hard_denied"
WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED = "original_repository2_not_executed"
WARN_EXTERNAL_PUBLISH_DISABLED = "external_publish_disabled"
WARN_GATEWAY_PRODUCTION_DISABLED = "gateway_production_disabled"
WARN_DISCORD_PRODUCTION_DISABLED = "discord_production_disabled"
WARN_ONE_SHOT_ONLY = "one_shot_only"
WARN_OPERATOR_SUPERVISION_REQUIRED = "operator_supervision_required"
WARN_RUNTIME_START_EXPIRY_REQUIRES_NEW_AUTHORIZATION = (
    "runtime_start_expiry_requires_new_authorization"
)
WARN_RUNTIME_EXECUTION_BLOCKED_WAITING_PHASE_15I = (
    "runtime_execution_blocked_waiting_phase_15i"
)

# NOTE: bare "phrase" is deliberately NOT in this set (mirrors Phase 15G).
# This module legitimately prints the safe boolean field
# `execution_authorization_phrase_verified` (a pass-through of the
# upstream authorization's own already-verified flag — this module never
# verifies a phrase itself), so a bare "phrase" token would false-positive
# on that safe line. The actual secret — the confirmation phrase text
# itself — is never read, stored, or referenced anywhere in this module;
# "repository2" (already present) independently guarantees it can never
# leak even if this reasoning is ever wrong.
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
        "confirm-repository2-execution",
        "started_by",
        "supervised_by",
        "operator_id",
        "supervisor_id",
        "prepared_by",
        "issued_by",
        "attestation_hash",
        "rollback_commit",
    }
)


class ProductionRuntimeStartError(ValueError):
    """Raised when governed runtime start assessment or start fails safely."""


@dataclass(frozen=True)
class ProductionRuntimeStartRecord:
    runtime_start_id: str
    activation_request_id: str
    authorization_id: str
    boundary_id: str
    boundary_invocation_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    ticket_id: str
    confirmation_id: str
    controlled_window_open_event_id: str
    runtime_invocation_id: str
    executor_id: str
    operator_id: str
    supervisor_id: str
    started_by: str
    supervised_by: str
    started_at: str
    expires_at: str
    ttl_seconds: int
    scope_type: str
    runtime_start_status: str
    tested_commit_sha: str
    release_tag: str
    runtime_started: bool = True
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
    authorization_consumed: bool = False
    original_repository2_execution_attempted: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    external_publish_enabled: bool = False


@dataclass(frozen=True)
class ProductionRuntimeStartEvent:
    event_id: str
    runtime_start_id: str
    activation_request_id: str
    event_type: str
    actor_role: str
    reason_code: str
    occurred_at: str


@dataclass(frozen=True)
class ProductionRuntimeStartSummary:
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    boundary_id: str
    boundary_invocation_id: str
    runtime_invocation_id: str
    authorization_id: str
    runtime_start_id: str
    runtime_start_state: str
    runtime_start_ready: bool
    runtime_start_present: bool
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
    boundary_valid: bool
    boundary_state: str
    boundary_expired: bool
    boundary_scope_valid: bool
    invocation_valid: bool
    invocation_state: str
    invocation_expired: bool
    invocation_scope_valid: bool
    execution_authorization_valid: bool
    execution_authorization_state: str
    execution_authorization_expired: bool
    execution_authorization_scope_valid: bool
    execution_authorization_phrase_verified: bool
    one_shot_scope_valid: bool
    ticket_scope_valid: bool
    window_scope_valid: bool
    runtime_start_ttl_valid: bool
    final_signoff_valid: bool
    rollback_ready: bool
    operational_signoff_valid: bool
    audit_chain_complete: bool
    recovery_required: bool
    repair_lock_held: bool
    executor_identity_valid: bool
    operator_identity_valid: bool
    supervisor_identity_valid: bool
    identity_separation_valid: bool
    kill_switch_available: bool
    emergency_close_available: bool
    runtime_factory_available: bool
    runtime_invoker_disabled: bool
    started_at: str
    expires_at: str
    runtime_start_issued: bool
    runtime_start_expired: bool
    runtime_started: bool
    production_execution_allowed: bool
    cutover_started: bool
    runtime_invoked: bool
    permission_consumed: bool
    permission_revoked: bool
    authorization_consumed: bool
    production_root_hard_deny: bool
    original_repository2_execution_attempted: bool
    external_publish_enabled: bool
    gateway_production_enabled: bool
    discord_production_enabled: bool
    boundary_runtime_invoked: bool
    boundary_cutover_started: bool
    invocation_runtime_invoked: bool
    invocation_cutover_started: bool
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str
    ttl_seconds: int = 0
    already_started: bool = False
    executor_assigned: bool = False
    operator_present: bool = False
    supervisor_present: bool = False
    tested_commit_sha_short: str = ""
    release_tag: str = ""
    window_remaining_seconds: int = 0
    permission_remaining_seconds: int = 0
    session_remaining_seconds: int = 0
    boundary_remaining_seconds: int = 0
    invocation_remaining_seconds: int = 0
    authorization_remaining_seconds: int = 0


@dataclass(frozen=True)
class ProductionRuntimeStartReleaseSummary:
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    boundary_id: str
    runtime_invocation_id: str
    authorization_id: str
    runtime_start_id: str
    controlled_window_state: str
    permission_state: str
    session_state: str
    boundary_state: str
    invocation_state: str
    execution_authorization_state: str
    runtime_start_state: str
    runtime_start_ready: bool
    runtime_start_present: bool
    runtime_start_expired: bool
    runtime_started: bool = False
    production_execution_allowed: bool = False
    cutover_started: bool = False
    runtime_invoked: bool = False
    permission_consumed: bool = False
    permission_revoked: bool = False
    authorization_consumed: bool = False
    production_root_hard_deny: bool = True
    original_repository2_execution_enabled: bool = False
    external_publish_enabled: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    next_phase: str = ""
    release_status: str = RELEASE_GOVERNED_RUNTIME_START_NOT_READY


@dataclass(frozen=True)
class ProductionRuntimeStartDashboardDigest:
    governed_runtime_start_state: str
    governed_runtime_start_ready: bool
    governed_runtime_start_present: bool
    governed_runtime_start_expired: bool
    governed_runtime_start_id: str
    governed_runtime_start_expires_at: str
    governed_runtime_start_started: bool
    governed_runtime_start_blocking_count: int
    governed_runtime_start_warning_count: int
    governed_runtime_start_recommended_action: str


def default_runtime_start_store_dir() -> Path:
    return get_hermes_home() / "coo" / _RUNTIME_START_STORE_DIR


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


def _runtime_start_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionRuntimeStartError("activation_request_id is required")
    base = (store_dir or default_runtime_start_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionRuntimeStartError(
            "Runtime start store must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_runtime_start_store_available(*, store_dir: Path | None = None) -> bool:
    try:
        base = (store_dir or default_runtime_start_store_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _record_from_dict(payload: Mapping[str, Any]) -> ProductionRuntimeStartRecord:
    return ProductionRuntimeStartRecord(
        runtime_start_id=str(payload.get("runtime_start_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        authorization_id=str(payload.get("authorization_id", "")),
        boundary_id=str(payload.get("boundary_id", "")),
        boundary_invocation_id=str(payload.get("boundary_invocation_id", "")),
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
        runtime_invocation_id=str(payload.get("runtime_invocation_id", "")),
        executor_id=str(payload.get("executor_id", "")),
        operator_id=str(payload.get("operator_id", "")),
        supervisor_id=str(payload.get("supervisor_id", "")),
        started_by=str(payload.get("started_by") or payload.get("operator_id", "")),
        supervised_by=str(
            payload.get("supervised_by") or payload.get("supervisor_id", "")
        ),
        started_at=str(payload.get("started_at", "")),
        expires_at=str(payload.get("expires_at", "")),
        ttl_seconds=int(payload.get("ttl_seconds") or 0),
        scope_type=str(payload.get("scope_type") or SCOPE_TYPE_ONE_SHOT),
        runtime_start_status=str(payload.get("runtime_start_status", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        runtime_started=True,
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
        authorization_consumed=False,
        original_repository2_execution_attempted=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        external_publish_enabled=False,
    )


def _record_to_dict(record: ProductionRuntimeStartRecord) -> dict[str, Any]:
    return {
        "runtime_start_id": record.runtime_start_id,
        "activation_request_id": record.activation_request_id,
        "authorization_id": record.authorization_id,
        "boundary_id": record.boundary_id,
        "boundary_invocation_id": record.boundary_invocation_id,
        "cutover_contract_id": record.cutover_contract_id,
        "permission_id": record.permission_id,
        "session_id": record.session_id,
        "reservation_id": record.reservation_id,
        "execution_attempt_id": record.execution_attempt_id,
        "dispatch_run_id": record.dispatch_run_id,
        "ticket_id": record.ticket_id,
        "confirmation_id": record.confirmation_id,
        "controlled_window_open_event_id": record.controlled_window_open_event_id,
        "runtime_invocation_id": record.runtime_invocation_id,
        "executor_id": record.executor_id,
        "operator_id": record.operator_id,
        "supervisor_id": record.supervisor_id,
        "started_by": record.started_by or record.operator_id,
        "supervised_by": record.supervised_by or record.supervisor_id,
        "started_at": record.started_at,
        "expires_at": record.expires_at,
        "ttl_seconds": record.ttl_seconds,
        "scope_type": SCOPE_TYPE_ONE_SHOT,
        "runtime_start_status": RUNTIME_START_STARTED,
        "tested_commit_sha": _short_sha(record.tested_commit_sha),
        "release_tag": record.release_tag,
        "runtime_started": True,
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
        "authorization_consumed": False,
        "original_repository2_execution_attempted": False,
        "gateway_production_enabled": False,
        "discord_production_enabled": False,
        "external_publish_enabled": False,
    }


def load_runtime_start_record(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionRuntimeStartRecord | None:
    path = _runtime_start_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionRuntimeStartError("runtime_start_store_corrupted") from exc
    runtime_start = payload.get("runtime_start")
    if not isinstance(runtime_start, dict):
        raise ProductionRuntimeStartError("runtime_start_store_corrupted")
    return _record_from_dict(runtime_start)


def load_runtime_start_by_id(
    runtime_start_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionRuntimeStartRecord | None:
    target = (runtime_start_id or "").strip()
    if not target:
        raise ProductionRuntimeStartError("runtime_start_id is required")
    base = (store_dir or default_runtime_start_store_dir()).resolve()
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductionRuntimeStartError("runtime_start_store_corrupted") from exc
        runtime_start = payload.get("runtime_start")
        if (
            isinstance(runtime_start, dict)
            and str(runtime_start.get("runtime_start_id", "")) == target
        ):
            return _record_from_dict(runtime_start)
    return None


def load_runtime_start_events(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> tuple[ProductionRuntimeStartEvent, ...]:
    path = _runtime_start_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionRuntimeStartError("runtime_start_store_corrupted") from exc
    raw = payload.get("events") or []
    if not isinstance(raw, list):
        raise ProductionRuntimeStartError("runtime_start_store_corrupted")
    events: list[ProductionRuntimeStartEvent] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ProductionRuntimeStartError("runtime_start_store_corrupted")
        event_id = str(item.get("event_id", ""))
        if not event_id or event_id in seen:
            raise ProductionRuntimeStartError("runtime_start_store_corrupted")
        seen.add(event_id)
        events.append(
            ProductionRuntimeStartEvent(
                event_id=event_id,
                runtime_start_id=str(item.get("runtime_start_id", "")),
                activation_request_id=str(item.get("activation_request_id", "")),
                event_type=str(item.get("event_type", "")),
                actor_role=str(item.get("actor_role", "")),
                reason_code=str(item.get("reason_code", "")),
                occurred_at=str(item.get("occurred_at", "")),
            )
        )
    return tuple(events)


def load_runtime_start_events_by_runtime_start_id(
    runtime_start_id: str,
    *,
    store_dir: Path | None = None,
) -> tuple[
    ProductionRuntimeStartRecord | None,
    tuple[ProductionRuntimeStartEvent, ...],
]:
    """Scan the whole store for the bundle whose runtime-start matches `runtime_start_id`."""
    target = (runtime_start_id or "").strip()
    if not target:
        raise ProductionRuntimeStartError("runtime_start_id is required")
    base = (store_dir or default_runtime_start_store_dir()).resolve()
    if not base.is_dir():
        return None, ()
    for path in sorted(base.glob("*.json")):
        activation_id = path.stem
        record = load_runtime_start_record(activation_id, store_dir=store_dir)
        if record is not None and record.runtime_start_id == target:
            events = load_runtime_start_events(activation_id, store_dir=store_dir)
            return record, events
    return None, ()


def _runtime_starts_equivalent(
    existing: ProductionRuntimeStartRecord,
    candidate: ProductionRuntimeStartRecord,
) -> bool:
    return (
        existing.authorization_id == candidate.authorization_id
        and existing.runtime_invocation_id == candidate.runtime_invocation_id
        and existing.boundary_id == candidate.boundary_id
        and existing.cutover_contract_id == candidate.cutover_contract_id
        and existing.permission_id == candidate.permission_id
        and existing.session_id == candidate.session_id
        and existing.executor_id == candidate.executor_id
        and existing.operator_id == candidate.operator_id
        and existing.supervisor_id == candidate.supervisor_id
        and existing.ttl_seconds == candidate.ttl_seconds
        and existing.ticket_id == candidate.ticket_id
        and existing.confirmation_id == candidate.confirmation_id
        and existing.controlled_window_open_event_id
        == candidate.controlled_window_open_event_id
    )


def _write_runtime_start_bundle(
    record: ProductionRuntimeStartRecord,
    events: tuple[ProductionRuntimeStartEvent, ...],
    *,
    store_dir: Path | None = None,
) -> None:
    path = _runtime_start_path(record.activation_request_id, store_dir=store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    payload = {
        "version": _RUNTIME_START_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "runtime_start": _record_to_dict(record),
        "events": [
            {
                "event_id": event.event_id,
                "runtime_start_id": event.runtime_start_id,
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
            existing = load_runtime_start_record(
                record.activation_request_id,
                store_dir=store_dir,
            )
            if existing is not None:
                if _runtime_starts_equivalent(existing, record):
                    return
                raise ProductionRuntimeStartError("runtime_start_conflict")
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            try:
                fd = os.open(str(path), flags, 0o644)
            except FileExistsError as exc:
                existing_again = load_runtime_start_record(
                    record.activation_request_id,
                    store_dir=store_dir,
                )
                if existing_again is not None and _runtime_starts_equivalent(
                    existing_again, record
                ):
                    return
                raise ProductionRuntimeStartError("runtime_start_conflict") from exc
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
        raise ProductionRuntimeStartError("runtime_start_write_failed") from exc
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
    supervisor_id: str,
    request,
    contract,
    final_record,
    op_record,
    permission_record,
    session_record,
    boundary_record,
    invocation_record,
    authorization_record,
) -> tuple[bool, bool, bool, bool, list[str]]:
    """Validate provided identities only (3-way: executor/operator/supervisor).

    Empty executor/operator/supervisor (status/check paths) does not
    append identity blocks for the missing role. `start` supplies all
    three; `check` may supply executor only (or nothing at all).
    Executor's binding correctness against permission/session/boundary/
    invocation/authorization is validated separately via the
    scope-mismatch checks — this function only rules out conflicts with
    other role holders (requester, security reviewer, contract preparer,
    final/operational signers, permission issuer, session starter,
    boundary/invocation reservers, the execution authorization's own
    operator/signer) and pairwise separation among
    executor/operator/supervisor themselves.
    """
    blocking: list[str] = []
    executor = (executor_id or "").strip()
    operator = (operator_id or "").strip()
    supervisor = (supervisor_id or "").strip()
    require_executor = bool(executor_id)
    require_operator = bool(operator_id)
    require_supervisor = bool(supervisor_id)

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
    if boundary_record is not None:
        conflicts_for_operator.add((boundary_record.reserved_by or "").strip())
        conflicts_for_operator.add((boundary_record.operator_id or "").strip())
    if invocation_record is not None:
        conflicts_for_operator.add((invocation_record.reserved_by or "").strip())
        conflicts_for_operator.add((invocation_record.operator_id or "").strip())
    if authorization_record is not None:
        conflicts_for_operator.add((authorization_record.authorized_by or "").strip())
        conflicts_for_operator.add((authorization_record.operator_id or "").strip())
    conflicts_for_operator.discard("")

    operator_valid = True
    if require_operator:
        operator_valid = bool(operator) and operator not in conflicts_for_operator
        if not operator_valid:
            if operator and executor and operator == executor:
                blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
            else:
                blocking.append(BLOCK_OPERATOR_IDENTITY_INVALID)

    conflicts_for_supervisor = set(conflicts_for_operator)
    if final_record is not None:
        conflicts_for_supervisor.add((final_record.signed_by or "").strip())
    if op_record is not None:
        conflicts_for_supervisor.add((op_record.signed_by or "").strip())
    if authorization_record is not None:
        conflicts_for_supervisor.add((authorization_record.signed_by or "").strip())
        conflicts_for_supervisor.add((authorization_record.signer_id or "").strip())
    conflicts_for_supervisor.discard("")

    supervisor_valid = True
    if require_supervisor:
        conflicts_for_supervisor_with_roles = set(conflicts_for_supervisor)
        if operator:
            conflicts_for_supervisor_with_roles.add(operator)
        supervisor_valid = (
            bool(supervisor) and supervisor not in conflicts_for_supervisor_with_roles
        )
        if not supervisor_valid:
            if supervisor and (
                (executor and supervisor == executor)
                or (operator and supervisor == operator)
            ):
                blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
            else:
                blocking.append(BLOCK_SUPERVISOR_IDENTITY_INVALID)

    separation = True
    roles_required = [require_executor, require_operator, require_supervisor]
    if all(roles_required):
        separation = (
            executor_valid
            and operator_valid
            and supervisor_valid
            and executor != operator
            and operator != supervisor
            and executor != supervisor
        )
        if executor_valid and operator_valid and supervisor_valid and not separation:
            blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
    elif require_executor and require_operator:
        separation = executor_valid and operator_valid and executor != operator
        if executor_valid and operator_valid and not separation:
            blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
    elif require_operator and require_supervisor:
        separation = operator_valid and supervisor_valid and operator != supervisor
        if operator_valid and supervisor_valid and not separation:
            blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
    elif require_executor and require_supervisor:
        separation = executor_valid and supervisor_valid and executor != supervisor
        if executor_valid and supervisor_valid and not separation:
            blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)

    return executor_valid, operator_valid, supervisor_valid, separation, blocking


def _validate_runtime_start_ttl(
    ttl_seconds: int | None,
    *,
    now: datetime,
    invocation_expires_at: datetime | None,
    boundary_expires_at: datetime | None,
    session_expires_at: datetime | None,
    permission_expires_at: datetime | None,
    window_end: datetime | None,
    authorization_expires_at: datetime | None,
) -> tuple[bool, str, int, int, int, int, int, int, int, str]:
    """Return (ttl_valid, invalid_reason, effective_ttl, invocation_remaining,
    boundary_remaining, session_remaining, permission_remaining,
    window_remaining, authorization_remaining, expires_at_iso).

    Fail-closed: a missing invocation/boundary/session/permission/window/
    authorization is treated as zero remaining seconds, so a candidate
    ttl can never be accepted against an unknown prerequisite.
    `expires_at` is always clamped to at most `min(invocation.expires_at,
    boundary.expires_at, session.expires_at, permission.expires_at,
    window_end, authorization.expires_at, now + ttl)`.
    """
    invocation_remaining = (
        max(0, int((invocation_expires_at - now).total_seconds()))
        if invocation_expires_at is not None
        else 0
    )
    boundary_remaining = (
        max(0, int((boundary_expires_at - now).total_seconds()))
        if boundary_expires_at is not None
        else 0
    )
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
    authorization_remaining = (
        max(0, int((authorization_expires_at - now).total_seconds()))
        if authorization_expires_at is not None
        else 0
    )
    if ttl_seconds is None:
        return (
            True,
            "",
            0,
            invocation_remaining,
            boundary_remaining,
            session_remaining,
            permission_remaining,
            window_remaining,
            authorization_remaining,
            "",
        )
    remaining = min(
        invocation_remaining,
        boundary_remaining,
        session_remaining,
        permission_remaining,
        window_remaining,
        authorization_remaining,
    )
    if remaining < MIN_RUNTIME_START_TTL_SECONDS:
        return (
            False,
            "insufficient_remaining",
            ttl_seconds,
            invocation_remaining,
            boundary_remaining,
            session_remaining,
            permission_remaining,
            window_remaining,
            authorization_remaining,
            "",
        )
    if (
        ttl_seconds < MIN_RUNTIME_START_TTL_SECONDS
        or ttl_seconds > MAX_RUNTIME_START_TTL_SECONDS
    ):
        return (
            False,
            "invalid_range",
            ttl_seconds,
            invocation_remaining,
            boundary_remaining,
            session_remaining,
            permission_remaining,
            window_remaining,
            authorization_remaining,
            "",
        )
    if ttl_seconds > invocation_remaining:
        return (
            False,
            "exceeds_invocation",
            ttl_seconds,
            invocation_remaining,
            boundary_remaining,
            session_remaining,
            permission_remaining,
            window_remaining,
            authorization_remaining,
            "",
        )
    if ttl_seconds > boundary_remaining:
        return (
            False,
            "exceeds_boundary",
            ttl_seconds,
            invocation_remaining,
            boundary_remaining,
            session_remaining,
            permission_remaining,
            window_remaining,
            authorization_remaining,
            "",
        )
    if ttl_seconds > session_remaining:
        return (
            False,
            "exceeds_session",
            ttl_seconds,
            invocation_remaining,
            boundary_remaining,
            session_remaining,
            permission_remaining,
            window_remaining,
            authorization_remaining,
            "",
        )
    if ttl_seconds > permission_remaining:
        return (
            False,
            "exceeds_permission",
            ttl_seconds,
            invocation_remaining,
            boundary_remaining,
            session_remaining,
            permission_remaining,
            window_remaining,
            authorization_remaining,
            "",
        )
    if ttl_seconds > window_remaining:
        return (
            False,
            "exceeds_window",
            ttl_seconds,
            invocation_remaining,
            boundary_remaining,
            session_remaining,
            permission_remaining,
            window_remaining,
            authorization_remaining,
            "",
        )
    if ttl_seconds > authorization_remaining:
        return (
            False,
            "exceeds_authorization",
            ttl_seconds,
            invocation_remaining,
            boundary_remaining,
            session_remaining,
            permission_remaining,
            window_remaining,
            authorization_remaining,
            "",
        )
    expires = now + timedelta(seconds=ttl_seconds)
    return (
        True,
        "",
        ttl_seconds,
        invocation_remaining,
        boundary_remaining,
        session_remaining,
        permission_remaining,
        window_remaining,
        authorization_remaining,
        expires.isoformat(),
    )


def _recommended_action(
    state: str,
    blocking: tuple[str, ...],
    *,
    window_open: bool,
    recovery: bool,
) -> str:
    if state == RUNTIME_START_STARTED:
        return ACTION_RUNTIME_START_STARTED_WAIT_FOR_PHASE_15I
    if state == RUNTIME_START_READY:
        return ACTION_START_GOVERNED_RUNTIME
    if state == RUNTIME_START_EXPIRED:
        return ACTION_RESOLVE_EXECUTION_AUTHORIZATION
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
        or BLOCK_SUPERVISOR_IDENTITY_INVALID in blocking
        or BLOCK_IDENTITY_SEPARATION_INVALID in blocking
    ):
        return ACTION_RESOLVE_IDENTITY_SEPARATION
    if (
        BLOCK_EXECUTION_AUTHORIZATION_MISSING in blocking
        or BLOCK_EXECUTION_AUTHORIZATION_INVALID in blocking
        or BLOCK_EXECUTION_AUTHORIZATION_EXPIRED in blocking
        or BLOCK_EXECUTION_AUTHORIZATION_ID_MISMATCH in blocking
        or BLOCK_EXECUTION_AUTHORIZATION_EXECUTOR_MISMATCH in blocking
        or BLOCK_EXECUTION_AUTHORIZATION_NOT_VERIFIED in blocking
        or BLOCK_EXECUTION_AUTHORIZATION_CONSUMED in blocking
        or BLOCK_EXECUTION_AUTHORIZATION_REVOKED in blocking
    ):
        return ACTION_RESOLVE_EXECUTION_AUTHORIZATION
    if (
        BLOCK_RUNTIME_INVOCATION_MISSING in blocking
        or BLOCK_RUNTIME_INVOCATION_INVALID in blocking
        or BLOCK_RUNTIME_INVOCATION_EXPIRED in blocking
        or BLOCK_RUNTIME_INVOCATION_EXECUTOR_MISMATCH in blocking
    ):
        return ACTION_RESOLVE_RUNTIME_INVOCATION
    if (
        BLOCK_RUNTIME_BOUNDARY_MISSING in blocking
        or BLOCK_RUNTIME_BOUNDARY_INVALID in blocking
        or BLOCK_RUNTIME_BOUNDARY_EXPIRED in blocking
        or BLOCK_RUNTIME_BOUNDARY_INVOCATION_ID_MISSING in blocking
        or BLOCK_RUNTIME_BOUNDARY_RUNTIME_INVOKED in blocking
        or BLOCK_RUNTIME_BOUNDARY_CUTOVER_STARTED in blocking
        or BLOCK_RUNTIME_BOUNDARY_EXECUTOR_MISMATCH in blocking
        or BLOCK_RUNTIME_BOUNDARY_SCOPE_MISMATCH in blocking
    ):
        return ACTION_RESOLVE_RUNTIME_BOUNDARY
    if (
        BLOCK_GOVERNED_RUNTIME_SESSION_MISSING in blocking
        or BLOCK_GOVERNED_RUNTIME_SESSION_INVALID in blocking
        or BLOCK_GOVERNED_RUNTIME_SESSION_EXPIRED in blocking
        or BLOCK_SESSION_EXECUTOR_MISMATCH in blocking
    ):
        return ACTION_RESOLVE_GOVERNED_RUNTIME_SESSION
    if (
        BLOCK_RUNTIME_PERMISSION_MISSING in blocking
        or BLOCK_RUNTIME_PERMISSION_INVALID in blocking
        or BLOCK_RUNTIME_PERMISSION_EXPIRED in blocking
        or BLOCK_RUNTIME_PERMISSION_CONSUMED in blocking
        or BLOCK_RUNTIME_PERMISSION_REVOKED in blocking
        or BLOCK_PERMISSION_EXECUTOR_MISMATCH in blocking
    ):
        return ACTION_RESOLVE_RUNTIME_PERMISSION
    if (
        BLOCK_RUNTIME_START_TTL_INVALID in blocking
        or BLOCK_RUNTIME_START_TTL_EXCEEDS_AUTHORIZATION in blocking
    ):
        return ACTION_RESOLVE_EXECUTION_AUTHORIZATION
    if BLOCK_RUNTIME_START_TTL_EXCEEDS_INVOCATION in blocking:
        return ACTION_RESOLVE_RUNTIME_INVOCATION
    if BLOCK_RUNTIME_START_TTL_EXCEEDS_BOUNDARY in blocking:
        return ACTION_RESOLVE_RUNTIME_BOUNDARY
    if BLOCK_RUNTIME_START_TTL_EXCEEDS_SESSION in blocking:
        return ACTION_RESOLVE_GOVERNED_RUNTIME_SESSION
    if BLOCK_RUNTIME_START_TTL_EXCEEDS_PERMISSION in blocking:
        return ACTION_RESOLVE_RUNTIME_PERMISSION
    if BLOCK_RUNTIME_START_TTL_EXCEEDS_WINDOW in blocking:
        return ACTION_WAIT_FOR_WINDOW_OPEN
    if (
        BLOCK_FINAL_SIGNOFF_INVALID in blocking
        or BLOCK_ROLLBACK_VALIDATION_INVALID in blocking
        or BLOCK_OPERATIONAL_SIGNOFF_INVALID in blocking
        or BLOCK_GOVERNED_CUTOVER_CONTRACT_MISSING in blocking
        or BLOCK_GOVERNED_CUTOVER_CONTRACT_INVALID in blocking
    ):
        return ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT
    if BLOCK_RUNTIME_START_ALREADY_STARTED in blocking:
        return ACTION_RUNTIME_START_STARTED_WAIT_FOR_PHASE_15I
    if BLOCK_RUNTIME_START_CONFLICT in blocking:
        return ACTION_MAINTAIN_PRODUCTION_BLOCK
    if not window_open:
        return ACTION_WAIT_FOR_WINDOW_OPEN
    return ACTION_MAINTAIN_PRODUCTION_BLOCK


def evaluate_production_runtime_start(
    *,
    activation_request_id: str,
    authorization_id: str = "",
    executor_id: str = "",
    operator_id: str = "",
    supervisor_id: str = "",
    ttl_seconds: int | None = None,
    runtime_start_store_dir: Path | None = None,
    authorization_store_dir: Path | None = None,
    invocation_store_dir: Path | None = None,
    invocation_consume_store_dir: Path | None = None,
    boundary_store_dir: Path | None = None,
    boundary_consume_store_dir: Path | None = None,
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
    force_authorization_consumed: bool | None = None,
    force_authorization_revoked: bool | None = None,
    force_kill_switch_unavailable: bool = False,
    force_emergency_close_unavailable: bool = False,
    force_runtime_factory_unavailable: bool = False,
    force_runtime_invoker_enabled: bool = False,
) -> ProductionRuntimeStartSummary:
    """Read-only governed runtime start assessment."""
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

    invocation_record = None
    try:
        invocation_record = load_runtime_invocation_record(
            activation_request_id,
            store_dir=invocation_store_dir,
        )
    except ProductionRuntimeInvocationError:
        blocking.append(BLOCK_RUNTIME_START_STORE_CORRUPTED)
        invocation_record = None

    authorization_record = None
    try:
        authorization_record = load_execution_authorization_record(
            activation_request_id,
            store_dir=authorization_store_dir,
        )
    except ProductionExecutionAuthorizationError:
        blocking.append(BLOCK_RUNTIME_START_STORE_CORRUPTED)
        authorization_record = None

    permission_record = None
    try:
        permission_record = load_runtime_permission_record(
            activation_request_id,
            store_dir=permission_store_dir,
        )
    except ProductionRuntimePermissionError:
        blocking.append(BLOCK_RUNTIME_START_STORE_CORRUPTED)
        permission_record = None

    session_record = None
    try:
        session_record = load_governed_runtime_session_record(
            activation_request_id,
            store_dir=session_store_dir,
        )
    except ProductionGovernedRuntimeSessionError:
        blocking.append(BLOCK_RUNTIME_START_STORE_CORRUPTED)
        session_record = None

    boundary_record = None
    try:
        boundary_record = load_runtime_boundary_record(
            activation_request_id,
            store_dir=boundary_store_dir,
        )
    except RuntimeBoundaryError:
        blocking.append(BLOCK_RUNTIME_START_STORE_CORRUPTED)
        boundary_record = None

    existing = None
    try:
        existing = load_runtime_start_record(
            activation_request_id,
            store_dir=runtime_start_store_dir,
        )
    except ProductionRuntimeStartError:
        blocking.append(BLOCK_RUNTIME_START_STORE_CORRUPTED)
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
            blocking.append(BLOCK_RUNTIME_START_STORE_CORRUPTED)

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
            invocation_record is not None
            and invocation_record.permission_id
            and permission_record.permission_id != invocation_record.permission_id
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
        and (not executor_id or permission_record.executor_id == executor_id.strip())
    )
    permission_scope_valid = (
        permission_record is not None and permission_correlation_valid
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
            invocation_record is not None
            and invocation_record.session_id
            and session_record.session_id != invocation_record.session_id
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

    # --- Runtime boundary prerequisite --------------------------------------
    boundary_expires_dt: datetime | None = None
    derived_boundary_expired = False
    boundary_valid = False
    boundary_state_label = ""
    boundary_scope_valid = True
    if boundary_record is None:
        blocking.append(BLOCK_RUNTIME_BOUNDARY_MISSING)
    else:
        boundary_state_label = BOUNDARY_RESERVED
        boundary_expires_dt = _parse_iso(boundary_record.expires_at)
        if boundary_expires_dt is not None and current >= boundary_expires_dt:
            derived_boundary_expired = True
            boundary_state_label = BOUNDARY_EXPIRED
            blocking.append(BLOCK_RUNTIME_BOUNDARY_EXPIRED)
        else:
            boundary_valid = True
        if not boundary_record.invocation_id:
            blocking.append(BLOCK_RUNTIME_BOUNDARY_INVOCATION_ID_MISSING)
            boundary_valid = False
        if boundary_record.runtime_invoked:
            blocking.append(BLOCK_RUNTIME_BOUNDARY_RUNTIME_INVOKED)
            boundary_valid = False
        if boundary_record.cutover_started:
            blocking.append(BLOCK_RUNTIME_BOUNDARY_CUTOVER_STARTED)
            boundary_valid = False
        if boundary_record.revoked:
            blocking.append(BLOCK_RUNTIME_BOUNDARY_REVOKED)
            boundary_valid = False
        try:
            boundary_already_consumed = (
                load_runtime_boundary_consume_record(
                    boundary_record.boundary_id,
                    store_dir=boundary_consume_store_dir,
                )
                is not None
            )
        except RuntimeBoundaryError:
            blocking.append(BLOCK_RUNTIME_START_STORE_CORRUPTED)
            boundary_already_consumed = True
        if boundary_already_consumed:
            blocking.append(BLOCK_RUNTIME_BOUNDARY_CONSUMED)
            boundary_valid = False
        if executor_id and boundary_record.executor_id != executor_id.strip():
            blocking.append(BLOCK_RUNTIME_BOUNDARY_EXECUTOR_MISMATCH)
        if (
            permission_record is not None
            and boundary_record.permission_id
            and boundary_record.permission_id != permission_record.permission_id
        ):
            boundary_scope_valid = False
        if (
            session_record is not None
            and boundary_record.session_id
            and boundary_record.session_id != session_record.session_id
        ):
            boundary_scope_valid = False
        if (
            invocation_record is not None
            and invocation_record.boundary_id
            and boundary_record.boundary_id != invocation_record.boundary_id
        ):
            boundary_scope_valid = False
        if (
            invocation_record is not None
            and invocation_record.boundary_invocation_id
            and boundary_record.invocation_id
            and invocation_record.boundary_invocation_id != boundary_record.invocation_id
        ):
            boundary_scope_valid = False
        if not boundary_scope_valid and BLOCK_RUNTIME_BOUNDARY_SCOPE_MISMATCH not in blocking:
            blocking.append(BLOCK_RUNTIME_BOUNDARY_SCOPE_MISMATCH)
        if not boundary_valid and BLOCK_RUNTIME_BOUNDARY_INVALID not in blocking and (
            BLOCK_RUNTIME_BOUNDARY_EXPIRED not in blocking
        ):
            blocking.append(BLOCK_RUNTIME_BOUNDARY_INVALID)

    # --- Runtime invocation prerequisite (Phase 15F) ------------------------
    invocation_expires_dt: datetime | None = None
    derived_invocation_expired = False
    invocation_valid = False
    invocation_state_label = ""
    invocation_scope_valid = True
    if invocation_record is None:
        blocking.append(BLOCK_RUNTIME_INVOCATION_MISSING)
    else:
        invocation_state_label = INVOCATION_RESERVED
        invocation_expires_dt = _parse_iso(invocation_record.expires_at)
        if invocation_expires_dt is not None and current >= invocation_expires_dt:
            derived_invocation_expired = True
            invocation_state_label = INVOCATION_EXPIRED
            blocking.append(BLOCK_RUNTIME_INVOCATION_EXPIRED)
        else:
            invocation_valid = True
        if invocation_record.revoked:
            blocking.append(BLOCK_RUNTIME_INVOCATION_REVOKED)
            invocation_valid = False
        try:
            invocation_already_consumed = (
                load_runtime_invocation_consume_record(
                    invocation_record.runtime_invocation_id,
                    store_dir=invocation_consume_store_dir,
                )
                is not None
            )
        except ProductionRuntimeInvocationError:
            blocking.append(BLOCK_RUNTIME_START_STORE_CORRUPTED)
            invocation_already_consumed = True
        if invocation_already_consumed:
            blocking.append(BLOCK_RUNTIME_INVOCATION_CONSUMED)
            invocation_valid = False
        if executor_id and invocation_record.executor_id != executor_id.strip():
            blocking.append(BLOCK_RUNTIME_INVOCATION_EXECUTOR_MISMATCH)
        if (
            boundary_record is not None
            and invocation_record.boundary_id
            and invocation_record.boundary_id != boundary_record.boundary_id
        ):
            invocation_scope_valid = False
        if (
            session_record is not None
            and invocation_record.session_id
            and invocation_record.session_id != session_record.session_id
        ):
            invocation_scope_valid = False
        if (
            permission_record is not None
            and invocation_record.permission_id
            and invocation_record.permission_id != permission_record.permission_id
        ):
            invocation_scope_valid = False
        if (
            contract is not None
            and invocation_record.cutover_contract_id
            and invocation_record.cutover_contract_id != contract.cutover_contract_id
        ):
            invocation_scope_valid = False
        if (
            open_event_id
            and invocation_record.controlled_window_open_event_id
            and invocation_record.controlled_window_open_event_id != open_event_id
        ):
            invocation_scope_valid = False
        if not invocation_scope_valid and BLOCK_RUNTIME_INVOCATION_INVALID not in blocking:
            blocking.append(BLOCK_RUNTIME_INVOCATION_INVALID)
        if not invocation_valid and BLOCK_RUNTIME_INVOCATION_INVALID not in blocking and (
            BLOCK_RUNTIME_INVOCATION_EXPIRED not in blocking
        ):
            blocking.append(BLOCK_RUNTIME_INVOCATION_INVALID)

    # --- Execution authorization prerequisite (Phase 15G) -------------------
    authorization_expires_dt: datetime | None = None
    derived_authorization_expired = False
    execution_authorization_valid = False
    execution_authorization_state_label = ""
    execution_authorization_scope_valid = True
    execution_authorization_phrase_verified = False
    if authorization_record is None:
        blocking.append(BLOCK_EXECUTION_AUTHORIZATION_MISSING)
    else:
        execution_authorization_phrase_verified = bool(
            authorization_record.execution_phrase_verified
        )
        execution_authorization_state_label = AUTHORIZATION_ISSUED
        authorization_expires_dt = _parse_iso(authorization_record.expires_at)
        if authorization_expires_dt is not None and current >= authorization_expires_dt:
            derived_authorization_expired = True
            execution_authorization_state_label = AUTHORIZATION_EXPIRED
            blocking.append(BLOCK_EXECUTION_AUTHORIZATION_EXPIRED)
        else:
            execution_authorization_valid = True
        if authorization_record.authorization_status != AUTHORIZATION_ISSUED:
            blocking.append(BLOCK_EXECUTION_AUTHORIZATION_INVALID)
            execution_authorization_valid = False
        if not authorization_record.execution_phrase_verified:
            blocking.append(BLOCK_EXECUTION_AUTHORIZATION_NOT_VERIFIED)
            execution_authorization_valid = False
        if authorization_record.consumed:
            blocking.append(BLOCK_EXECUTION_AUTHORIZATION_CONSUMED)
            execution_authorization_valid = False
        if authorization_record.revoked:
            blocking.append(BLOCK_EXECUTION_AUTHORIZATION_REVOKED)
            execution_authorization_valid = False
        if (
            authorization_id
            and authorization_record.authorization_id != authorization_id.strip()
        ):
            blocking.append(BLOCK_EXECUTION_AUTHORIZATION_ID_MISMATCH)
            execution_authorization_scope_valid = False
        if executor_id and authorization_record.executor_id != executor_id.strip():
            blocking.append(BLOCK_EXECUTION_AUTHORIZATION_EXECUTOR_MISMATCH)
        if (
            invocation_record is not None
            and authorization_record.runtime_invocation_id
            and authorization_record.runtime_invocation_id
            != invocation_record.runtime_invocation_id
        ):
            execution_authorization_scope_valid = False
        if (
            boundary_record is not None
            and authorization_record.boundary_id
            and authorization_record.boundary_id != boundary_record.boundary_id
        ):
            execution_authorization_scope_valid = False
        if (
            session_record is not None
            and authorization_record.session_id
            and authorization_record.session_id != session_record.session_id
        ):
            execution_authorization_scope_valid = False
        if (
            permission_record is not None
            and authorization_record.permission_id
            and authorization_record.permission_id != permission_record.permission_id
        ):
            execution_authorization_scope_valid = False
        if (
            contract is not None
            and authorization_record.cutover_contract_id
            and authorization_record.cutover_contract_id != contract.cutover_contract_id
        ):
            execution_authorization_scope_valid = False
        if (
            open_event_id
            and authorization_record.controlled_window_open_event_id
            and authorization_record.controlled_window_open_event_id != open_event_id
        ):
            execution_authorization_scope_valid = False
        if (
            not execution_authorization_scope_valid
            and BLOCK_EXECUTION_AUTHORIZATION_INVALID not in blocking
        ):
            blocking.append(BLOCK_EXECUTION_AUTHORIZATION_INVALID)
            execution_authorization_valid = False

    window_end = _parse_iso(window.maintenance_window_end)
    (
        ttl_valid,
        ttl_reason,
        effective_ttl,
        invocation_remaining,
        boundary_remaining,
        session_remaining,
        permission_remaining,
        window_remaining,
        authorization_remaining,
        expires_iso,
    ) = _validate_runtime_start_ttl(
        ttl_seconds,
        now=current,
        invocation_expires_at=invocation_expires_dt,
        boundary_expires_at=boundary_expires_dt,
        session_expires_at=session_expires_dt,
        permission_expires_at=permission_expires_dt,
        window_end=window_end,
        authorization_expires_at=authorization_expires_dt,
    )
    if ttl_seconds is not None and not ttl_valid:
        if ttl_reason == "exceeds_invocation":
            blocking.append(BLOCK_RUNTIME_START_TTL_EXCEEDS_INVOCATION)
        elif ttl_reason == "exceeds_boundary":
            blocking.append(BLOCK_RUNTIME_START_TTL_EXCEEDS_BOUNDARY)
        elif ttl_reason == "exceeds_session":
            blocking.append(BLOCK_RUNTIME_START_TTL_EXCEEDS_SESSION)
        elif ttl_reason == "exceeds_permission":
            blocking.append(BLOCK_RUNTIME_START_TTL_EXCEEDS_PERMISSION)
        elif ttl_reason == "exceeds_window":
            blocking.append(BLOCK_RUNTIME_START_TTL_EXCEEDS_WINDOW)
        elif ttl_reason == "exceeds_authorization":
            blocking.append(BLOCK_RUNTIME_START_TTL_EXCEEDS_AUTHORIZATION)
        else:
            blocking.append(BLOCK_RUNTIME_START_TTL_INVALID)

    executor_valid, operator_valid, supervisor_valid, separation_valid, id_blocks = (
        _assess_identities(
            executor_id=executor_id,
            operator_id=operator_id,
            supervisor_id=supervisor_id,
            request=request,
            contract=contract,
            final_record=final_record,
            op_record=op_record,
            permission_record=permission_record,
            session_record=session_record,
            boundary_record=boundary_record,
            invocation_record=invocation_record,
            authorization_record=authorization_record,
        )
    )
    blocking.extend(id_blocks)

    production_execution_allowed = bool(force_production_execution_allowed)
    gateway_enabled = bool(force_gateway_enabled)
    discord_enabled = bool(force_discord_enabled)
    cutover_started_forced = bool(force_cutover_started)
    runtime_invoked_forced = bool(force_runtime_invoked)
    authorization_consumed_forced = bool(force_authorization_consumed)
    authorization_revoked_forced = bool(force_authorization_revoked)
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
    if authorization_consumed_forced:
        blocking.append(BLOCK_EXECUTION_AUTHORIZATION_CONSUMED)
    if authorization_revoked_forced:
        blocking.append(BLOCK_EXECUTION_AUTHORIZATION_REVOKED)

    # --- Existing runtime-start idempotency / conflict -----------------------
    runtime_start_id = ""
    started_at = ""
    expires_at = ""
    already_started = False
    runtime_start_present = existing is not None
    runtime_start_expired_flag = False
    runtime_start_conflict = False
    if existing is not None:
        runtime_start_id = existing.runtime_start_id
        started_at = existing.started_at
        expires_at = existing.expires_at
        existing_expires_dt = _parse_iso(existing.expires_at)
        if existing_expires_dt is not None and current >= existing_expires_dt:
            runtime_start_expired_flag = True
            blocking.append(BLOCK_RUNTIME_START_EXPIRED)
        else:
            already_started = True
            if (
                authorization_id
                or executor_id
                or operator_id
                or supervisor_id
                or ttl_seconds is not None
            ):
                equivalent = (
                    (
                        not authorization_id
                        or existing.authorization_id == authorization_id.strip()
                    )
                    and (not executor_id or existing.executor_id == executor_id.strip())
                    and (not operator_id or existing.operator_id == operator_id.strip())
                    and (
                        not supervisor_id
                        or existing.supervisor_id == supervisor_id.strip()
                    )
                    and (ttl_seconds is None or existing.ttl_seconds == ttl_seconds)
                )
                if not equivalent:
                    runtime_start_conflict = True
                    blocking.append(BLOCK_RUNTIME_START_CONFLICT)
            if not runtime_start_conflict:
                blocking.append(BLOCK_RUNTIME_START_ALREADY_STARTED)

    unique_blocking = tuple(dict.fromkeys(blocking))

    excluded_when_no_ttl = {
        BLOCK_RUNTIME_START_TTL_INVALID,
        BLOCK_RUNTIME_START_TTL_EXCEEDS_INVOCATION,
        BLOCK_RUNTIME_START_TTL_EXCEEDS_BOUNDARY,
        BLOCK_RUNTIME_START_TTL_EXCEEDS_SESSION,
        BLOCK_RUNTIME_START_TTL_EXCEEDS_PERMISSION,
        BLOCK_RUNTIME_START_TTL_EXCEEDS_WINDOW,
        BLOCK_RUNTIME_START_TTL_EXCEEDS_AUTHORIZATION,
    }
    excluded_when_no_identity = {
        BLOCK_EXECUTOR_IDENTITY_INVALID,
        BLOCK_OPERATOR_IDENTITY_INVALID,
        BLOCK_SUPERVISOR_IDENTITY_INVALID,
        BLOCK_IDENTITY_SEPARATION_INVALID,
    }
    hard_ready_blockers = [
        code
        for code in unique_blocking
        if code
        not in {
            BLOCK_RUNTIME_START_ALREADY_STARTED,
        }
        and not (ttl_seconds is None and code in excluded_when_no_ttl)
        and not (
            not executor_id
            and not operator_id
            and not supervisor_id
            and code in excluded_when_no_identity
        )
        and not (not executor_id and code == BLOCK_PERMISSION_EXECUTOR_MISMATCH)
        and not (not executor_id and code == BLOCK_SESSION_EXECUTOR_MISMATCH)
        and not (not executor_id and code == BLOCK_RUNTIME_BOUNDARY_EXECUTOR_MISMATCH)
        and not (not executor_id and code == BLOCK_RUNTIME_INVOCATION_EXECUTOR_MISMATCH)
        and not (
            not executor_id and code == BLOCK_EXECUTION_AUTHORIZATION_EXECUTOR_MISMATCH
        )
        and not (
            not authorization_id and code == BLOCK_EXECUTION_AUTHORIZATION_ID_MISMATCH
        )
    ]

    core_ready = (
        contract_valid
        and window_scope_valid
        and runtime_permission_valid
        and session_valid
        and session_context_valid
        and boundary_valid
        and boundary_scope_valid
        and not derived_boundary_expired
        and invocation_valid
        and invocation_scope_valid
        and not derived_invocation_expired
        and execution_authorization_valid
        and execution_authorization_scope_valid
        and not derived_authorization_expired
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
        and not authorization_consumed_forced
        and not authorization_revoked_forced
        and runtime_factory_available
        and runtime_invoker_disabled
        and existing is None
        and not (kill_switch_required and not kill_switch_available)
        and not (emergency_close_required and not emergency_close_available)
    )

    runtime_start_ready_calc = (
        core_ready
        and executor_valid
        and operator_valid
        and supervisor_valid
        and separation_valid
        and (not executor_id or BLOCK_PERMISSION_EXECUTOR_MISMATCH not in unique_blocking)
        and (not executor_id or BLOCK_SESSION_EXECUTOR_MISMATCH not in unique_blocking)
        and (
            not executor_id
            or BLOCK_RUNTIME_BOUNDARY_EXECUTOR_MISMATCH not in unique_blocking
        )
        and (
            not executor_id
            or BLOCK_RUNTIME_INVOCATION_EXECUTOR_MISMATCH not in unique_blocking
        )
        and (
            not executor_id
            or BLOCK_EXECUTION_AUTHORIZATION_EXECUTOR_MISMATCH not in unique_blocking
        )
        and (
            not authorization_id
            or BLOCK_EXECUTION_AUTHORIZATION_ID_MISMATCH not in unique_blocking
        )
        and ttl_seconds is not None
        and ttl_valid
        and not runtime_start_conflict
    )

    if existing is not None and runtime_start_expired_flag:
        state = RUNTIME_START_EXPIRED
        ready = False
    elif existing is not None and runtime_start_conflict:
        state = RUNTIME_START_BLOCKED
        ready = False
    elif existing is not None and already_started:
        state = RUNTIME_START_STARTED
        ready = False
    elif recovery_required or repair_lock_held:
        state = RUNTIME_START_BLOCKED
        ready = False
    elif runtime_start_ready_calc:
        state = RUNTIME_START_READY
        ready = True
    elif hard_ready_blockers:
        state = RUNTIME_START_BLOCKED
        ready = False
    else:
        state = RUNTIME_START_NOT_STARTED
        ready = False

    warnings.extend(
        [
            WARN_RUNTIME_START_IS_PREREQUISITE_ONLY,
            WARN_RUNTIME_NOT_INVOKED,
            WARN_CUTOVER_NOT_STARTED,
            WARN_PERMISSION_NOT_CONSUMED,
            WARN_AUTHORIZATION_NOT_CONSUMED,
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
    if state == RUNTIME_START_EXPIRED:
        warnings.append(WARN_RUNTIME_START_EXPIRY_REQUIRES_NEW_AUTHORIZATION)
    if state == RUNTIME_START_STARTED:
        warnings.append(WARN_RUNTIME_EXECUTION_BLOCKED_WAITING_PHASE_15I)
    unique_warnings = tuple(dict.fromkeys(warnings))

    recommended = _recommended_action(
        state,
        unique_blocking,
        window_open=window.window_open,
        recovery=recovery_required or repair_lock_held,
    )

    return ProductionRuntimeStartSummary(
        activation_request_id=activation_request_id,
        cutover_contract_id=cutover_contract_id,
        permission_id=permission_record.permission_id if permission_record else "",
        session_id=session_record.session_id if session_record else "",
        boundary_id=boundary_record.boundary_id if boundary_record else "",
        boundary_invocation_id=boundary_record.invocation_id if boundary_record else "",
        runtime_invocation_id=(
            invocation_record.runtime_invocation_id if invocation_record else ""
        ),
        authorization_id=(
            authorization_record.authorization_id if authorization_record else ""
        ),
        runtime_start_id=runtime_start_id,
        runtime_start_state=state,
        runtime_start_ready=ready,
        runtime_start_present=runtime_start_present,
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
        boundary_valid=boundary_valid,
        boundary_state=boundary_state_label,
        boundary_expired=derived_boundary_expired,
        boundary_scope_valid=boundary_scope_valid,
        invocation_valid=invocation_valid,
        invocation_state=invocation_state_label,
        invocation_expired=derived_invocation_expired,
        invocation_scope_valid=invocation_scope_valid,
        execution_authorization_valid=execution_authorization_valid,
        execution_authorization_state=execution_authorization_state_label,
        execution_authorization_expired=derived_authorization_expired,
        execution_authorization_scope_valid=execution_authorization_scope_valid,
        execution_authorization_phrase_verified=execution_authorization_phrase_verified,
        one_shot_scope_valid=one_shot_scope_valid,
        ticket_scope_valid=ticket_scope_valid,
        window_scope_valid=window_scope_valid,
        runtime_start_ttl_valid=ttl_valid if ttl_seconds is not None else True,
        final_signoff_valid=final_signoff_valid,
        rollback_ready=rollback_ready,
        operational_signoff_valid=operational_signoff_valid,
        audit_chain_complete=audit_chain_complete,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        executor_identity_valid=executor_valid if executor_id else True,
        operator_identity_valid=operator_valid if operator_id else True,
        supervisor_identity_valid=supervisor_valid if supervisor_id else True,
        identity_separation_valid=(
            separation_valid if (executor_id or operator_id or supervisor_id) else True
        ),
        kill_switch_available=kill_switch_available,
        emergency_close_available=emergency_close_available,
        runtime_factory_available=runtime_factory_available,
        runtime_invoker_disabled=runtime_invoker_disabled,
        started_at=started_at,
        expires_at=expires_at or expires_iso,
        runtime_start_issued=already_started,
        runtime_start_expired=runtime_start_expired_flag,
        runtime_started=bool(existing.runtime_started) if existing else False,
        production_execution_allowed=False,
        cutover_started=False,
        runtime_invoked=False,
        permission_consumed=False,
        permission_revoked=False,
        authorization_consumed=False,
        production_root_hard_deny=True,
        original_repository2_execution_attempted=False,
        external_publish_enabled=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        boundary_runtime_invoked=False,
        boundary_cutover_started=False,
        invocation_runtime_invoked=False,
        invocation_cutover_started=False,
        blocking_items=unique_blocking,
        warning_items=unique_warnings,
        recommended_action=recommended,
        ttl_seconds=effective_ttl if ttl_seconds is not None else (existing.ttl_seconds if existing else 0),
        already_started=already_started,
        executor_assigned=bool((executor_id or "").strip())
        or (existing is not None and bool(existing.executor_id)),
        operator_present=bool((operator_id or "").strip())
        or (existing is not None and bool(existing.operator_id)),
        supervisor_present=bool((supervisor_id or "").strip())
        or (existing is not None and bool(existing.supervisor_id)),
        tested_commit_sha_short=_short_sha(
            request.tested_commit_sha if request is not None else ""
        ),
        release_tag=request.release_tag if request is not None else "",
        window_remaining_seconds=window_remaining,
        permission_remaining_seconds=permission_remaining,
        session_remaining_seconds=session_remaining,
        boundary_remaining_seconds=boundary_remaining,
        invocation_remaining_seconds=invocation_remaining,
        authorization_remaining_seconds=authorization_remaining,
    )


def start_production_runtime_start(
    *,
    activation_request_id: str,
    authorization_id: str,
    executor_id: str,
    operator_id: str,
    supervisor_id: str,
    ttl_seconds: int,
    runtime_start_store_dir: Path | None = None,
    authorization_store_dir: Path | None = None,
    invocation_store_dir: Path | None = None,
    invocation_consume_store_dir: Path | None = None,
    boundary_store_dir: Path | None = None,
    boundary_consume_store_dir: Path | None = None,
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
) -> ProductionRuntimeStartSummary:
    """Append-only creation of a one-shot governed runtime start contract.

    Never invokes runtime, never starts cutover, never consumes the
    underlying permission or execution authorization — those remain
    exclusively later phases' concern. This is a *contract only*: on
    success, the ONLY boolean that flips to true is `runtime_started`,
    and it is set ONLY on the newly written runtime-start record — the
    upstream authorization, invocation, boundary, session, and
    permission artifacts are never mutated. `runtime_invoked` and
    `cutover_started` remain False on every output this function ever
    produces, including the freshly written record. This module never
    reads, checks, or stores any confirmation phrase itself — it only
    reads the already-persisted `execution_phrase_verified` boolean off
    the upstream Phase 15G authorization record.
    """
    if not probe_runtime_start_store_available(store_dir=runtime_start_store_dir):
        raise ProductionRuntimeStartError("runtime_start_write_failed")

    def _evaluate() -> ProductionRuntimeStartSummary:
        return evaluate_production_runtime_start(
            activation_request_id=activation_request_id,
            authorization_id=authorization_id,
            executor_id=executor_id,
            operator_id=operator_id,
            supervisor_id=supervisor_id,
            ttl_seconds=ttl_seconds,
            runtime_start_store_dir=runtime_start_store_dir,
            authorization_store_dir=authorization_store_dir,
            invocation_store_dir=invocation_store_dir,
            invocation_consume_store_dir=invocation_consume_store_dir,
            boundary_store_dir=boundary_store_dir,
            boundary_consume_store_dir=boundary_consume_store_dir,
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

    existing = load_runtime_start_record(
        activation_request_id,
        store_dir=runtime_start_store_dir,
    )
    if existing is not None:
        expires_dt = _parse_iso(existing.expires_at)
        if expires_dt is not None and _utc_now(now) >= expires_dt:
            raise ProductionRuntimeStartError("runtime_start_expired")
        equivalent = (
            existing.authorization_id == (authorization_id or "").strip()
            and existing.executor_id == (executor_id or "").strip()
            and existing.operator_id == (operator_id or "").strip()
            and existing.supervisor_id == (supervisor_id or "").strip()
            and existing.ttl_seconds == int(ttl_seconds)
        )
        if equivalent:
            return _evaluate()
        raise ProductionRuntimeStartError("runtime_start_conflict")

    if summary.runtime_start_state != RUNTIME_START_READY:
        raise ProductionRuntimeStartError(
            f"governed runtime start blocked for state {summary.runtime_start_state!r}"
        )

    authorization_record = load_execution_authorization_record(
        activation_request_id, store_dir=authorization_store_dir
    )
    invocation_record = load_runtime_invocation_record(
        activation_request_id, store_dir=invocation_store_dir
    )
    boundary_record = load_runtime_boundary_record(
        activation_request_id, store_dir=boundary_store_dir
    )
    session_record = load_governed_runtime_session_record(
        activation_request_id, store_dir=session_store_dir
    )
    permission_record = load_runtime_permission_record(
        activation_request_id, store_dir=permission_store_dir
    )
    contract = load_governed_cutover_contract(
        activation_request_id, store_dir=governed_cutover_store_dir
    )
    reservation = load_execution_reservation(
        activation_request_id, store_dir=reservation_dir
    )
    if (
        authorization_record is None
        or invocation_record is None
        or boundary_record is None
        or session_record is None
        or permission_record is None
        or contract is None
        or reservation is None
    ):
        raise ProductionRuntimeStartError("runtime_start_store_corrupted")
    if (
        authorization_id
        and authorization_record.authorization_id != authorization_id.strip()
    ):
        raise ProductionRuntimeStartError("execution_authorization_id_mismatch")
    if authorization_record.executor_id != (executor_id or "").strip():
        raise ProductionRuntimeStartError("execution_authorization_executor_mismatch")
    if authorization_record.authorization_status != AUTHORIZATION_ISSUED:
        raise ProductionRuntimeStartError("execution_authorization_invalid")
    if not authorization_record.execution_phrase_verified:
        raise ProductionRuntimeStartError("execution_authorization_not_verified")
    if authorization_record.consumed or authorization_record.revoked:
        raise ProductionRuntimeStartError("execution_authorization_invalid")
    if boundary_record.runtime_invoked or boundary_record.cutover_started:
        raise ProductionRuntimeStartError("runtime_boundary_invalid")

    current = _utc_now(now)
    authorization_expires_dt = _parse_iso(authorization_record.expires_at)
    if authorization_expires_dt is None or current >= authorization_expires_dt:
        raise ProductionRuntimeStartError("execution_authorization_expired")
    invocation_expires_dt = _parse_iso(invocation_record.expires_at)
    if invocation_expires_dt is None or current >= invocation_expires_dt:
        raise ProductionRuntimeStartError("runtime_invocation_expired")
    boundary_expires_dt = _parse_iso(boundary_record.expires_at)
    if boundary_expires_dt is None or current >= boundary_expires_dt:
        raise ProductionRuntimeStartError("runtime_boundary_expired")
    session_expires_dt = _parse_iso(session_record.expires_at)
    if session_expires_dt is None or current >= session_expires_dt:
        raise ProductionRuntimeStartError("governed_runtime_session_expired")
    permission_expires_dt = _parse_iso(permission_record.expires_at)
    if permission_expires_dt is None or current >= permission_expires_dt:
        raise ProductionRuntimeStartError("runtime_permission_expired")
    window_end = _parse_iso(contract.maintenance_window_end)
    (
        ttl_valid,
        ttl_reason,
        effective_ttl,
        _invocation_remaining,
        _boundary_remaining,
        _session_remaining,
        _permission_remaining,
        _window_remaining,
        _authorization_remaining,
        expires_iso,
    ) = _validate_runtime_start_ttl(
        ttl_seconds,
        now=current,
        invocation_expires_at=invocation_expires_dt,
        boundary_expires_at=boundary_expires_dt,
        session_expires_at=session_expires_dt,
        permission_expires_at=permission_expires_dt,
        window_end=window_end,
        authorization_expires_at=authorization_expires_dt,
    )
    if not ttl_valid:
        raise ProductionRuntimeStartError(
            ttl_reason and f"runtime_start_ttl_{ttl_reason}" or "runtime_start_ttl_invalid"
        )

    runtime_start_id = str(uuid.uuid4())
    started_at = _utc_now_iso(now)
    record = ProductionRuntimeStartRecord(
        runtime_start_id=runtime_start_id,
        activation_request_id=activation_request_id,
        authorization_id=authorization_record.authorization_id,
        boundary_id=boundary_record.boundary_id,
        boundary_invocation_id=boundary_record.invocation_id,
        cutover_contract_id=contract.cutover_contract_id,
        permission_id=permission_record.permission_id,
        session_id=session_record.session_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=contract.execution_attempt_id,
        dispatch_run_id=contract.dispatch_run_id,
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        controlled_window_open_event_id=authorization_record.controlled_window_open_event_id,
        runtime_invocation_id=authorization_record.runtime_invocation_id,
        executor_id=(executor_id or "").strip(),
        operator_id=(operator_id or "").strip(),
        supervisor_id=(supervisor_id or "").strip(),
        started_by=(operator_id or "").strip(),
        supervised_by=(supervisor_id or "").strip(),
        started_at=started_at,
        expires_at=expires_iso,
        ttl_seconds=effective_ttl,
        scope_type=SCOPE_TYPE_ONE_SHOT,
        runtime_start_status=RUNTIME_START_STARTED,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
        runtime_started=True,
    )
    events = (
        ProductionRuntimeStartEvent(
            event_id=str(uuid.uuid4()),
            runtime_start_id=runtime_start_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_RUNTIME_START_REQUESTED,
            actor_role="operator",
            reason_code="",
            occurred_at=started_at,
        ),
        ProductionRuntimeStartEvent(
            event_id=str(uuid.uuid4()),
            runtime_start_id=runtime_start_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_RUNTIME_START_STARTED,
            actor_role="system",
            reason_code="",
            occurred_at=started_at,
        ),
        ProductionRuntimeStartEvent(
            event_id=str(uuid.uuid4()),
            runtime_start_id=runtime_start_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_RUNTIME_EXECUTION_BLOCKED,
            actor_role="system",
            reason_code="",
            occurred_at=started_at,
        ),
    )
    _write_runtime_start_bundle(record, events, store_dir=runtime_start_store_dir)
    return _evaluate()


class GovernedRuntimeStartContext:
    """Read-only handle onto a started governed runtime start contract.

    Entering re-validates that the contract is still live (not expired,
    the controlled window is still open, the underlying authorization is
    still issued/verified/unexpired, the underlying invocation/boundary
    are still reserved and unexpired, the underlying session is still
    started and unexpired, and the underlying permission is still issued
    and unconsumed) before exposing any state. `runtime_started` on the
    loaded record must be True, or entering raises. Never invokes
    runtime, never starts cutover, never consumes the permission or
    authorization. The context is single-use: it cannot be entered while
    already active and cannot be re-entered after it has been exited.
    """

    def __init__(
        self,
        runtime_start_id: str = "",
        *,
        activation_request_id: str = "",
        runtime_start_store_dir: Path | None = None,
        authorization_store_dir: Path | None = None,
        invocation_store_dir: Path | None = None,
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
        self.runtime_start_id = (runtime_start_id or "").strip()
        self.activation_request_id = (activation_request_id or "").strip()
        if not self.runtime_start_id and not self.activation_request_id:
            raise ProductionRuntimeStartError(
                "runtime_start_id or activation_request_id is required"
            )
        self.authorization_id = ""
        self.runtime_invocation_id = ""
        self.boundary_id = ""
        self.session_id = ""
        self.permission_id = ""
        self._executor_id = ""
        self.entered_at = ""
        self.expires_at = ""
        self.runtime_started = False
        self.active = False
        self.consumed = False
        self._entered_once = False
        self._now = now
        self._runtime_start_store_dir = runtime_start_store_dir
        self._authorization_store_dir = authorization_store_dir
        self._invocation_store_dir = invocation_store_dir
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

    def __enter__(self) -> "GovernedRuntimeStartContext":
        if self.active:
            raise ProductionRuntimeStartError(
                "governed runtime start context is already active"
            )
        if self._entered_once:
            raise ProductionRuntimeStartError(
                "governed runtime start context cannot be reused after exit"
            )

        record: ProductionRuntimeStartRecord | None = None
        if self.activation_request_id:
            record = load_runtime_start_record(
                self.activation_request_id, store_dir=self._runtime_start_store_dir
            )
            if record is None or (
                self.runtime_start_id
                and record.runtime_start_id != self.runtime_start_id
            ):
                raise ProductionRuntimeStartError("runtime_start_not_found")
        else:
            record, _ = load_runtime_start_events_by_runtime_start_id(
                self.runtime_start_id, store_dir=self._runtime_start_store_dir
            )
            if record is None:
                raise ProductionRuntimeStartError("runtime_start_not_found")
            self.activation_request_id = record.activation_request_id

        assert record is not None
        current = _utc_now(self._now)
        expires_dt = _parse_iso(record.expires_at)
        if expires_dt is None or current >= expires_dt:
            raise ProductionRuntimeStartError("runtime_start_expired")
        if not record.runtime_started:
            raise ProductionRuntimeStartError("runtime_start_not_started")

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
            raise ProductionRuntimeStartError("controlled_window_not_open")

        try:
            authorization_record = load_execution_authorization_record(
                self.activation_request_id, store_dir=self._authorization_store_dir
            )
        except ProductionExecutionAuthorizationError as exc:
            raise ProductionRuntimeStartError("runtime_start_store_corrupted") from exc
        if (
            authorization_record is None
            or authorization_record.authorization_id != record.authorization_id
        ):
            raise ProductionRuntimeStartError("execution_authorization_invalid")
        if not authorization_record.execution_phrase_verified:
            raise ProductionRuntimeStartError("execution_authorization_not_verified")
        if authorization_record.consumed or authorization_record.revoked:
            raise ProductionRuntimeStartError("execution_authorization_invalid")
        authorization_expires_dt = _parse_iso(authorization_record.expires_at)
        if authorization_expires_dt is None or current >= authorization_expires_dt:
            raise ProductionRuntimeStartError("execution_authorization_expired")

        try:
            invocation_record = load_runtime_invocation_record(
                self.activation_request_id, store_dir=self._invocation_store_dir
            )
        except ProductionRuntimeInvocationError as exc:
            raise ProductionRuntimeStartError("runtime_start_store_corrupted") from exc
        if (
            invocation_record is None
            or invocation_record.runtime_invocation_id != record.runtime_invocation_id
        ):
            raise ProductionRuntimeStartError("runtime_invocation_invalid")
        invocation_expires_dt = _parse_iso(invocation_record.expires_at)
        if invocation_expires_dt is None or current >= invocation_expires_dt:
            raise ProductionRuntimeStartError("runtime_invocation_expired")

        try:
            boundary_record = load_runtime_boundary_record(
                self.activation_request_id, store_dir=self._boundary_store_dir
            )
        except RuntimeBoundaryError as exc:
            raise ProductionRuntimeStartError("runtime_start_store_corrupted") from exc
        if boundary_record is None or boundary_record.boundary_id != record.boundary_id:
            raise ProductionRuntimeStartError("runtime_boundary_invalid")
        boundary_expires_dt = _parse_iso(boundary_record.expires_at)
        if boundary_expires_dt is None or current >= boundary_expires_dt:
            raise ProductionRuntimeStartError("runtime_boundary_expired")
        if boundary_record.runtime_invoked or boundary_record.cutover_started:
            raise ProductionRuntimeStartError("runtime_boundary_invalid")

        try:
            session_record = load_governed_runtime_session_record(
                self.activation_request_id, store_dir=self._session_store_dir
            )
        except ProductionGovernedRuntimeSessionError as exc:
            raise ProductionRuntimeStartError("runtime_start_store_corrupted") from exc
        if session_record is None or session_record.session_id != record.session_id:
            raise ProductionRuntimeStartError("governed_runtime_session_invalid")
        session_expires_dt = _parse_iso(session_record.expires_at)
        if session_expires_dt is None or current >= session_expires_dt:
            raise ProductionRuntimeStartError("governed_runtime_session_expired")

        try:
            permission_record = load_runtime_permission_record(
                self.activation_request_id, store_dir=self._permission_store_dir
            )
        except ProductionRuntimePermissionError as exc:
            raise ProductionRuntimeStartError("runtime_start_store_corrupted") from exc
        if (
            permission_record is None
            or permission_record.permission_id != record.permission_id
        ):
            raise ProductionRuntimeStartError("runtime_permission_invalid")
        if (
            permission_record.permission_status != PERMISSION_ISSUED
            or permission_record.consumed
            or permission_record.revoked
        ):
            raise ProductionRuntimeStartError("runtime_permission_invalid")
        permission_expires_dt = _parse_iso(permission_record.expires_at)
        if permission_expires_dt is None or current >= permission_expires_dt:
            raise ProductionRuntimeStartError("runtime_permission_expired")

        self.runtime_start_id = record.runtime_start_id
        self.authorization_id = record.authorization_id
        self.runtime_invocation_id = record.runtime_invocation_id
        self.boundary_id = record.boundary_id
        self.session_id = record.session_id
        self.permission_id = record.permission_id
        self._executor_id = record.executor_id
        self.runtime_started = record.runtime_started
        self.entered_at = _utc_now_iso(current)
        self.expires_at = record.expires_at
        self.active = True
        self._entered_once = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.active = False
        return False

    def __getstate__(self):
        raise ProductionRuntimeStartError(
            "governed runtime start context cannot be serialized"
        )

    def __setstate__(self, state):
        raise ProductionRuntimeStartError(
            "governed runtime start context cannot be serialized"
        )

    def __reduce__(self):
        raise ProductionRuntimeStartError(
            "governed runtime start context cannot be serialized"
        )


def enter_governed_runtime_start_context(
    *,
    runtime_start_id: str = "",
    activation_request_id: str = "",
    **kwargs: Any,
) -> GovernedRuntimeStartContext:
    """Factory returning an unentered context manager; use via `with`."""
    return GovernedRuntimeStartContext(
        runtime_start_id,
        activation_request_id=activation_request_id,
        **kwargs,
    )


def build_production_runtime_start_release_summary(
    summary: ProductionRuntimeStartSummary,
) -> ProductionRuntimeStartReleaseSummary:
    if (
        summary.runtime_start_state == RUNTIME_START_STARTED
        and summary.invocation_valid
        and summary.boundary_valid
        and summary.execution_authorization_valid
        and summary.session_state == SESSION_STARTED
        and summary.permission_state == PERMISSION_ISSUED
        and summary.controlled_window_open
    ):
        release_status = RELEASE_GOVERNED_RUNTIME_START_STARTED
        next_phase = _NEXT_PHASE_15I
    elif summary.runtime_start_state == RUNTIME_START_EXPIRED:
        release_status = RELEASE_GOVERNED_RUNTIME_START_EXPIRED
        next_phase = ""
    elif summary.recovery_required or summary.repair_lock_held:
        release_status = RELEASE_GOVERNED_RUNTIME_START_RECOVERY_REQUIRED
        next_phase = ""
    elif summary.runtime_start_ready or summary.runtime_start_state == RUNTIME_START_READY:
        release_status = RELEASE_GOVERNED_RUNTIME_START_READY
        next_phase = ""
    else:
        release_status = RELEASE_GOVERNED_RUNTIME_START_NOT_READY
        next_phase = ""

    return ProductionRuntimeStartReleaseSummary(
        activation_request_id=summary.activation_request_id,
        cutover_contract_id=summary.cutover_contract_id,
        permission_id=summary.permission_id,
        session_id=summary.session_id,
        boundary_id=summary.boundary_id,
        runtime_invocation_id=summary.runtime_invocation_id,
        authorization_id=summary.authorization_id,
        runtime_start_id=summary.runtime_start_id,
        controlled_window_state=summary.controlled_window_state,
        permission_state=summary.permission_state,
        session_state=summary.session_state,
        boundary_state=summary.boundary_state,
        invocation_state=summary.invocation_state,
        execution_authorization_state=summary.execution_authorization_state,
        runtime_start_state=summary.runtime_start_state,
        runtime_start_ready=summary.runtime_start_ready,
        runtime_start_present=summary.runtime_start_present,
        runtime_start_expired=summary.runtime_start_state == RUNTIME_START_EXPIRED,
        runtime_started=summary.runtime_started,
        production_execution_allowed=False,
        cutover_started=False,
        runtime_invoked=False,
        permission_consumed=False,
        permission_revoked=False,
        authorization_consumed=False,
        production_root_hard_deny=True,
        original_repository2_execution_enabled=False,
        external_publish_enabled=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        next_phase=next_phase,
        release_status=release_status,
    )


def resolve_latest_governed_runtime_start_dashboard_digest(
    *,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
    session_store_dir: Path | None = None,
    boundary_store_dir: Path | None = None,
    invocation_store_dir: Path | None = None,
    authorization_store_dir: Path | None = None,
    runtime_start_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> ProductionRuntimeStartDashboardDigest:
    base = (governed_cutover_store_dir or default_governed_cutover_store_dir()).resolve()
    if not base.is_dir():
        return ProductionRuntimeStartDashboardDigest(
            governed_runtime_start_state="not_configured",
            governed_runtime_start_ready=False,
            governed_runtime_start_present=False,
            governed_runtime_start_expired=False,
            governed_runtime_start_id="",
            governed_runtime_start_expires_at="",
            governed_runtime_start_started=False,
            governed_runtime_start_blocking_count=0,
            governed_runtime_start_warning_count=0,
            governed_runtime_start_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )
    paths = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:500]:
        activation_id = path.stem
        try:
            summary = evaluate_production_runtime_start(
                activation_request_id=activation_id,
                governed_cutover_store_dir=governed_cutover_store_dir,
                window_store_dir=window_store_dir,
                permission_store_dir=permission_store_dir,
                session_store_dir=session_store_dir,
                boundary_store_dir=boundary_store_dir,
                invocation_store_dir=invocation_store_dir,
                authorization_store_dir=authorization_store_dir,
                runtime_start_store_dir=runtime_start_store_dir,
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
        if not summary.governed_cutover_contract_valid and not summary.runtime_start_present:
            continue
        return ProductionRuntimeStartDashboardDigest(
            governed_runtime_start_state=summary.runtime_start_state,
            governed_runtime_start_ready=summary.runtime_start_ready,
            governed_runtime_start_present=summary.runtime_start_present,
            governed_runtime_start_expired=(
                summary.runtime_start_state == RUNTIME_START_EXPIRED
            ),
            governed_runtime_start_id=summary.runtime_start_id,
            governed_runtime_start_expires_at=summary.expires_at,
            governed_runtime_start_started=summary.runtime_started,
            governed_runtime_start_blocking_count=len(summary.blocking_items),
            governed_runtime_start_warning_count=len(summary.warning_items),
            governed_runtime_start_recommended_action=summary.recommended_action,
        )
    return ProductionRuntimeStartDashboardDigest(
        governed_runtime_start_state="not_configured",
        governed_runtime_start_ready=False,
        governed_runtime_start_present=False,
        governed_runtime_start_expired=False,
        governed_runtime_start_id="",
        governed_runtime_start_expires_at="",
        governed_runtime_start_started=False,
        governed_runtime_start_blocking_count=0,
        governed_runtime_start_warning_count=0,
        governed_runtime_start_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
    )


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "production_execution_allowed: false",
        "cutover_started: false",
        "runtime_invoked: false",
        "runtime_started: true",
        "runtime_started: false",
        "permission_consumed: false",
        "permission_revoked: false",
        "authorization_consumed: false",
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
        "supervisor_present: true",
        "supervisor_present: false",
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
        "execution_authorization_phrase_verified: true",
        "execution_authorization_phrase_verified: false",
        "boundary_runtime_invoked: false",
        "boundary_cutover_started: false",
        "invocation_runtime_invoked: false",
        "invocation_cutover_started: false",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    for label in (
        "executor_assigned",
        "operator_present",
        "supervisor_present",
        "operator_identity_valid",
        "executor_identity_valid",
        "supervisor_identity_valid",
        "identity_separation_valid",
        "original_repository2_not_executed",
        "original_repository2_execution_attempted",
        "runtime_permission",
        "permission_executor_mismatch",
        "permission_not_consumed",
        "session_executor_mismatch",
        "execution_authorization_phrase_verified",
        "execution_authorization_valid",
        "execution_authorization_state",
        "runtime_boundary_executor_mismatch",
        "runtime_invocation_executor_mismatch",
        "boundary_invocation_id",
        "boundary_runtime_invoked",
        "boundary_cutover_started",
        "invocation_runtime_invoked",
        "invocation_cutover_started",
        "runtime_started",
        "runtime_start_started",
    ):
        lowered = lowered.replace(label, "")
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionRuntimeStartError(
                f"Unsafe governed runtime start output field: {token!r}"
            )


def format_production_runtime_start_status(
    summary: ProductionRuntimeStartSummary,
) -> str:
    lines = [
        "Production Governed Runtime Start Status",
        "",
        f"activation_request_id: {summary.activation_request_id}",
        f"cutover_contract_id: {summary.cutover_contract_id or '(none)'}",
        f"permission_id: {summary.permission_id or '(none)'}",
        f"session_id: {summary.session_id or '(none)'}",
        f"boundary_id: {summary.boundary_id or '(none)'}",
        f"boundary_invocation_id: {summary.boundary_invocation_id or '(none)'}",
        f"runtime_invocation_id: {summary.runtime_invocation_id or '(none)'}",
        f"authorization_id: {summary.authorization_id or '(none)'}",
        f"runtime_start_id: {summary.runtime_start_id or '(none)'}",
        f"runtime_start_state: {summary.runtime_start_state}",
        f"runtime_start_ready: {str(summary.runtime_start_ready).lower()}",
        f"runtime_start_present: {str(summary.runtime_start_present).lower()}",
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
        f"boundary_valid: {str(summary.boundary_valid).lower()}",
        f"boundary_state: {summary.boundary_state or '(none)'}",
        f"boundary_expired: {str(summary.boundary_expired).lower()}",
        f"boundary_scope_valid: {str(summary.boundary_scope_valid).lower()}",
        f"invocation_valid: {str(summary.invocation_valid).lower()}",
        f"invocation_state: {summary.invocation_state or '(none)'}",
        f"invocation_expired: {str(summary.invocation_expired).lower()}",
        f"invocation_scope_valid: {str(summary.invocation_scope_valid).lower()}",
        "execution_authorization_valid: "
        f"{str(summary.execution_authorization_valid).lower()}",
        f"execution_authorization_state: {summary.execution_authorization_state or '(none)'}",
        "execution_authorization_expired: "
        f"{str(summary.execution_authorization_expired).lower()}",
        "execution_authorization_scope_valid: "
        f"{str(summary.execution_authorization_scope_valid).lower()}",
        "execution_authorization_phrase_verified: "
        f"{str(summary.execution_authorization_phrase_verified).lower()}",
        f"final_signoff_valid: {str(summary.final_signoff_valid).lower()}",
        f"rollback_ready: {str(summary.rollback_ready).lower()}",
        "operational_signoff_valid: "
        f"{str(summary.operational_signoff_valid).lower()}",
        f"audit_chain_complete: {str(summary.audit_chain_complete).lower()}",
        f"recovery_required: {str(summary.recovery_required).lower()}",
        f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
        f"executor_identity_valid: {str(summary.executor_identity_valid).lower()}",
        f"operator_identity_valid: {str(summary.operator_identity_valid).lower()}",
        f"supervisor_identity_valid: {str(summary.supervisor_identity_valid).lower()}",
        "identity_separation_valid: "
        f"{str(summary.identity_separation_valid).lower()}",
        f"one_shot_scope_valid: {str(summary.one_shot_scope_valid).lower()}",
        f"ticket_scope_valid: {str(summary.ticket_scope_valid).lower()}",
        f"window_scope_valid: {str(summary.window_scope_valid).lower()}",
        f"runtime_start_ttl_valid: {str(summary.runtime_start_ttl_valid).lower()}",
        f"ttl_seconds: {summary.ttl_seconds}",
        f"kill_switch_available: {str(summary.kill_switch_available).lower()}",
        f"emergency_close_available: {str(summary.emergency_close_available).lower()}",
        f"runtime_factory_available: {str(summary.runtime_factory_available).lower()}",
        f"runtime_invoker_disabled: {str(summary.runtime_invoker_disabled).lower()}",
        f"window_remaining_seconds: {summary.window_remaining_seconds}",
        f"permission_remaining_seconds: {summary.permission_remaining_seconds}",
        f"session_remaining_seconds: {summary.session_remaining_seconds}",
        f"boundary_remaining_seconds: {summary.boundary_remaining_seconds}",
        f"invocation_remaining_seconds: {summary.invocation_remaining_seconds}",
        f"authorization_remaining_seconds: {summary.authorization_remaining_seconds}",
        f"started_at: {summary.started_at or '(none)'}",
        f"expires_at: {summary.expires_at or '(none)'}",
        f"runtime_start_issued: {str(summary.runtime_start_issued).lower()}",
        f"runtime_start_expired: {str(summary.runtime_start_expired).lower()}",
        f"runtime_started: {str(summary.runtime_started).lower()}",
        f"blocking_items_count: {len(summary.blocking_items)}",
        f"warning_items_count: {len(summary.warning_items)}",
        "blocking_items: "
        f"{', '.join(summary.blocking_items) if summary.blocking_items else '(none)'}",
        "warning_items: "
        f"{', '.join(summary.warning_items) if summary.warning_items else '(none)'}",
        f"recommended_action: {summary.recommended_action}",
        f"executor_assigned: {str(summary.executor_assigned).lower()}",
        f"operator_present: {str(summary.operator_present).lower()}",
        f"supervisor_present: {str(summary.supervisor_present).lower()}",
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
        "authorization_consumed: false",
        "original_repository2_execution_attempted: false",
        "external_publish_enabled: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
        "boundary_runtime_invoked: false",
        "boundary_cutover_started: false",
        "invocation_runtime_invoked: false",
        "invocation_cutover_started: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_runtime_start_record(
    record: ProductionRuntimeStartRecord,
    *,
    now: datetime | None = None,
) -> str:
    expires_dt = _parse_iso(record.expires_at)
    expired = bool(expires_dt and _utc_now(now) >= expires_dt)
    state = RUNTIME_START_EXPIRED if expired else RUNTIME_START_STARTED
    lines = [
        "Production Governed Runtime Start",
        "",
        f"runtime_start_id: {record.runtime_start_id}",
        f"activation_request_id: {record.activation_request_id}",
        f"authorization_id: {record.authorization_id}",
        f"boundary_id: {record.boundary_id}",
        f"boundary_invocation_id: {record.boundary_invocation_id}",
        f"cutover_contract_id: {record.cutover_contract_id}",
        f"permission_id: {record.permission_id}",
        f"session_id: {record.session_id}",
        f"runtime_invocation_id: {record.runtime_invocation_id}",
        f"reservation_id: {record.reservation_id}",
        f"execution_attempt_id: {record.execution_attempt_id}",
        f"dispatch_run_id: {record.dispatch_run_id}",
        f"runtime_start_status: {state}",
        f"ttl_seconds: {record.ttl_seconds}",
        f"started_at: {record.started_at}",
        f"expires_at: {record.expires_at}",
        f"runtime_started: {str(record.runtime_started).lower()}",
        "executor_assigned: true",
        "operator_present: true",
        "supervisor_present: true",
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
        "authorization_consumed: false",
        "original_repository2_execution_attempted: false",
        "external_publish_enabled: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_runtime_start_history(
    activation_request_id: str,
    events: tuple[ProductionRuntimeStartEvent, ...],
    *,
    runtime_start_id: str = "",
) -> str:
    lines = [
        "Production Governed Runtime Start History",
        "",
        f"activation_request_id: {activation_request_id}",
        f"runtime_start_id: {runtime_start_id or '(none)'}",
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
            "authorization_consumed: false",
            "production_root_hard_deny: true",
        ]
    )
    output = "\n".join(lines).rstrip()
    _assert_safe_output(output)
    return output


def run_production_runtime_start_status(
    *,
    activation_request_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_runtime_start(
            activation_request_id=activation_request_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionRuntimeStartError:
        return "error: governed runtime start status unavailable", 1
    return format_production_runtime_start_status(summary), 0


def run_production_runtime_start_check(
    *,
    activation_request_id: str,
    authorization_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
    ttl_seconds: int = 15,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_runtime_start(
            activation_request_id=activation_request_id,
            authorization_id=authorization_id,
            ttl_seconds=ttl_seconds,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionRuntimeStartError:
        return "error: governed runtime start check unavailable", 1
    exit_code = 0 if summary.runtime_start_state == RUNTIME_START_READY else 1
    return format_production_runtime_start_status(summary), exit_code


def run_production_runtime_start_start(
    *,
    activation_request_id: str,
    authorization_id: str,
    executor_id: str,
    operator_id: str,
    supervisor_id: str,
    ttl_seconds: int,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = start_production_runtime_start(
            activation_request_id=activation_request_id,
            authorization_id=authorization_id,
            executor_id=executor_id,
            operator_id=operator_id,
            supervisor_id=supervisor_id,
            ttl_seconds=ttl_seconds,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionRuntimeStartError:
        try:
            summary = evaluate_production_runtime_start(
                activation_request_id=activation_request_id,
                authorization_id=authorization_id,
                executor_id=executor_id,
                operator_id=operator_id,
                supervisor_id=supervisor_id,
                ttl_seconds=ttl_seconds,
                merged_config=merged_config,
                repo_root=repo_root,
            )
            return format_production_runtime_start_status(summary), 1
        except ProductionRuntimeStartError:
            return "error: governed runtime start failed", 1
    exit_code = (
        0
        if summary.runtime_start_state == RUNTIME_START_STARTED
        or summary.already_started
        else 1
    )
    return format_production_runtime_start_status(summary), exit_code


def run_production_runtime_start_show(
    *,
    runtime_start_id: str,
) -> tuple[str, int]:
    try:
        record = load_runtime_start_by_id(runtime_start_id)
    except ProductionRuntimeStartError:
        return "error: governed runtime start corrupted", 1
    if record is None:
        return "error: governed runtime start not found", 1
    return format_production_runtime_start_record(record), 0


def run_production_runtime_start_history(
    *,
    activation_request_id: str,
) -> tuple[str, int]:
    try:
        record = load_runtime_start_record(activation_request_id)
        events = load_runtime_start_events(activation_request_id)
    except ProductionRuntimeStartError:
        return "error: governed runtime start history unavailable", 1
    return (
        format_production_runtime_start_history(
            activation_request_id,
            events,
            runtime_start_id=record.runtime_start_id if record else "",
        ),
        0,
    )
