"""Execution authorization contract — Phase 15G.

One-shot append-only execution authorization prerequisite bound to a
reserved runtime invocation (Phase 15F), a reserved runtime boundary
(Phase 15E), a started governed runtime session, an issued/unconsumed
runtime permission, and an open controlled window. Authorizing an
execution is NOT runtime invocation itself, NOT cutover, and NOT
permission consumption — those remain exclusively Phase 15H's concern.

This is the one phase in the chain that actually verifies the operator
confirmation phrase (``REQUIRED_CONFIRMATION_PHRASE`` from
``agent.coo.production_executor_confirmation``). The phrase is validated
by *exact* equality only — no whitespace stripping, case-sensitive — and
is never written to disk, logged, or echoed back in any CLI output. Only
a boolean ``execution_phrase_verified`` is ever persisted, on the
authorization record itself, once the phrase has been confirmed correct.

Storage layout:
    Like Phase 15E/15F, this module keeps a *single atomic bundle per
    activation request* — the authorization record and its lifecycle
    events live together in one JSON file at
    ``~/.hermes/coo/production-execution-authorization/{activation_request_id}.json``.
    This mirrors the boundary/invocation modules' storage layout exactly
    (rather than dual-writing events to a separate
    `production-execution-authorization-events/{authorization_id}.json`
    path) because a single bundle keeps the record and its events
    atomically consistent under the same flock + O_EXCL write, with no
    risk of the two halves diverging after a crash mid-write.
    `load_execution_authorization_by_id` /
    `load_execution_authorization_events_by_authorization_id` scan the
    store when lookup by opaque id (rather than activation id) is
    required.

Invariants enforced everywhere in this module:
    - production_execution_allowed is always False in every output.
    - cutover_started is always False in every output. This module never
      appends a cutover-start event — cutover remains exclusively out of
      scope here too.
    - runtime_invoked is always False in every output. Authorizing an
      execution is a *prerequisite* for runtime invocation, never the
      invocation itself. No automatic runtime invocation ever follows a
      successful authorize call.
    - permission_consumed and permission_revoked are always False — this
      module never consumes or revokes a runtime permission.
    - consumed and revoked (on the authorization record itself) are
      always False. `AUTHORIZATION_CONSUMED` / `AUTHORIZATION_REVOKED`
      are defined as forward references for Phase 15H only and are never
      persisted by this module.
    - The persisted `authorization_status` is always `AUTHORIZATION_ISSUED`
      on a written record — no other status is ever written to disk.
    - The confirmation phrase itself is NEVER stored (plaintext or
      hashed) in the artifact, events, stdout, or logs. Only the boolean
      `execution_phrase_verified` is persisted, and only on the
      authorization record (never mutated back onto the invocation,
      boundary, session, or permission artifacts).
    - No subprocess, no `create_bounded_subprocess_runner` call, no
      Repository2 execution. The runner-factory check is purely
      structural (is the class importable?) and never invokes the
      runner.
    - Boundary reserved != invocation reserved != authorization issued !=
      cutover started != runtime invoked != permission consumed. These
      are six distinct, sequential gates and this module only ever
      advances the fourth one (issuing an authorization record on top of
      an already-reserved invocation).
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
from agent.coo.production_executor_confirmation import REQUIRED_CONFIRMATION_PHRASE
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
    load_runtime_boundary_record,
)
from agent.coo.production_runtime_invocation import (
    INVOCATION_EXPIRED,
    INVOCATION_RESERVED,
    ProductionRuntimeInvocationError,
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

_AUTHORIZATION_STORE_DIR = "production-execution-authorization"
_AUTHORIZATION_STORE_VERSION = 1
_NEXT_PHASE_15H = "Phase_15H_governed_runtime_start"

AUTHORIZATION_NOT_ISSUED = "AUTHORIZATION_NOT_ISSUED"
AUTHORIZATION_READY = "AUTHORIZATION_READY"
AUTHORIZATION_ISSUED = "AUTHORIZATION_ISSUED"
AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
AUTHORIZATION_BLOCKED = "AUTHORIZATION_BLOCKED"

# Defined for forward reference by later phases only. NEVER persisted by
# this module — see the module docstring and `_record_to_dict` below,
# which always writes `authorization_status=AUTHORIZATION_ISSUED`.
AUTHORIZATION_CONSUMED = "AUTHORIZATION_CONSUMED"
AUTHORIZATION_REVOKED = "AUTHORIZATION_REVOKED"

SCOPE_TYPE_ONE_SHOT = "one_shot"
MIN_AUTHORIZATION_TTL_SECONDS = 5
MAX_AUTHORIZATION_TTL_SECONDS = 30
MIN_TTL_SECONDS = MIN_AUTHORIZATION_TTL_SECONDS
MAX_TTL_SECONDS = MAX_AUTHORIZATION_TTL_SECONDS

# `session_state` mirrors the boundary/invocation modules' derived-label
# convention: the underlying session record never persists a distinct
# "expired" status field, so this module derives the same label locally
# at evaluation time.
SESSION_EXPIRED_LABEL = "SESSION_EXPIRED"

EVENT_EXECUTION_AUTHORIZATION_REQUESTED = "execution_authorization_requested"
EVENT_EXECUTION_PHRASE_VERIFIED = "execution_phrase_verified"
EVENT_EXECUTION_AUTHORIZATION_ISSUED = "execution_authorization_issued"
EVENT_RUNTIME_EXECUTION_BLOCKED = "runtime_execution_blocked_waiting_phase_15h"

RELEASE_EXECUTION_AUTHORIZATION_READY = "EXECUTION_AUTHORIZATION_READY"
RELEASE_EXECUTION_AUTHORIZATION_ISSUED = "EXECUTION_AUTHORIZATION_ISSUED"
RELEASE_EXECUTION_AUTHORIZATION_EXPIRED = "EXECUTION_AUTHORIZATION_EXPIRED"
RELEASE_EXECUTION_AUTHORIZATION_NOT_READY = "EXECUTION_AUTHORIZATION_NOT_READY"
RELEASE_EXECUTION_AUTHORIZATION_RECOVERY_REQUIRED = (
    "EXECUTION_AUTHORIZATION_RECOVERY_REQUIRED"
)

ACTION_AUTHORIZE_GOVERNED_RUNTIME_EXECUTION = "authorize_governed_runtime_execution"
ACTION_EXECUTION_AUTHORIZED_WAIT_FOR_PHASE_15H = (
    "execution_authorized_wait_for_phase_15h"
)
ACTION_REVIEW_AUTHORIZATION_WARNINGS = "review_authorization_warnings"
ACTION_WAIT_FOR_WINDOW_OPEN = "wait_for_window_open"
ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT = "prepare_new_governed_cutover_contract"
ACTION_RESOLVE_RUNTIME_PERMISSION = "resolve_runtime_permission"
ACTION_RESOLVE_GOVERNED_RUNTIME_SESSION = "resolve_governed_runtime_session"
ACTION_RESOLVE_RUNTIME_BOUNDARY = "resolve_runtime_boundary"
ACTION_RESOLVE_RUNTIME_INVOCATION = "resolve_runtime_invocation"
ACTION_RESOLVE_IDENTITY_SEPARATION = "resolve_identity_separation"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_CLOSE_OR_EMERGENCY_CLOSE_WINDOW = "close_or_emergency_close_window"
ACTION_RESOLVE_KILL_SWITCH = "resolve_kill_switch"
ACTION_RESOLVE_RUNTIME_FACTORY = "resolve_runtime_factory"
ACTION_DISABLE_RUNTIME_INVOKER = "disable_runtime_invoker"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_PREPARE_PHASE_15H_GOVERNED_RUNTIME_START = (
    "prepare_phase_15h_governed_runtime_start"
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
BLOCK_RUNTIME_INVOCATION_MISSING = "runtime_invocation_missing"
BLOCK_RUNTIME_INVOCATION_INVALID = "runtime_invocation_invalid"
BLOCK_RUNTIME_INVOCATION_EXPIRED = "runtime_invocation_expired"
BLOCK_RUNTIME_INVOCATION_ID_MISMATCH = "runtime_invocation_id_mismatch"
BLOCK_RUNTIME_INVOCATION_EXECUTOR_MISMATCH = "runtime_invocation_executor_mismatch"
BLOCK_INVOCATION_EXECUTION_PHRASE_NOT_REQUIRED = (
    "invocation_execution_phrase_not_required"
)
BLOCK_INVOCATION_EXECUTION_PHRASE_ALREADY_VERIFIED = (
    "invocation_execution_phrase_already_verified"
)
BLOCK_EXECUTOR_IDENTITY_INVALID = "executor_identity_invalid"
BLOCK_OPERATOR_IDENTITY_INVALID = "operator_identity_invalid"
BLOCK_SIGNER_IDENTITY_INVALID = "signer_identity_invalid"
BLOCK_IDENTITY_SEPARATION_INVALID = "identity_separation_invalid"
BLOCK_AUTHORIZATION_TTL_INVALID = "authorization_ttl_invalid"
BLOCK_AUTHORIZATION_TTL_EXCEEDS_INVOCATION = "authorization_ttl_exceeds_invocation"
BLOCK_AUTHORIZATION_TTL_EXCEEDS_BOUNDARY = "authorization_ttl_exceeds_boundary"
BLOCK_AUTHORIZATION_TTL_EXCEEDS_SESSION = "authorization_ttl_exceeds_session"
BLOCK_AUTHORIZATION_TTL_EXCEEDS_PERMISSION = "authorization_ttl_exceeds_permission"
BLOCK_AUTHORIZATION_TTL_EXCEEDS_WINDOW = "authorization_ttl_exceeds_window"
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
BLOCK_EXECUTION_AUTHORIZATION_ALREADY_ISSUED = "execution_authorization_already_issued"
BLOCK_EXECUTION_AUTHORIZATION_CONFLICT = "execution_authorization_conflict"
BLOCK_EXECUTION_AUTHORIZATION_EXPIRED = "execution_authorization_expired"
BLOCK_AUTHORIZATION_STORE_CORRUPTED = "authorization_store_corrupted"
BLOCK_AUTHORIZATION_WRITE_FAILED = "authorization_write_failed"
BLOCK_UNSAFE_OUTPUT = "unsafe_output"

WARN_AUTHORIZATION_IS_PREREQUISITE_ONLY = "authorization_is_prerequisite_only"
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
WARN_AUTHORIZATION_EXPIRY_REQUIRES_NEW_INVOCATION = (
    "authorization_expiry_requires_new_invocation"
)
WARN_RUNTIME_EXECUTION_BLOCKED_WAITING_PHASE_15H = (
    "runtime_execution_blocked_waiting_phase_15h"
)

# NOTE: bare "phrase" is deliberately NOT in this set (unlike some earlier
# phases). This module legitimately prints safe boolean fields containing
# the substring "phrase" (`execution_phrase_required`,
# `execution_phrase_verified`, and the dashboard digest field
# `execution_authorization_phrase_verified`), so a bare "phrase" token
# would false-positive on those safe lines. The actual secret — the
# literal confirmation phrase text and the generic field name that would
# hold it — remain hard-blocked via `confirmation_phrase` and the
# lowercased phrase literal itself, and "repository2" (already present)
# independently guarantees the phrase text can never leak even if this
# reasoning is ever wrong.
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
        "signed_by",
        "signer_id",
        "operator_id",
        "prepared_by",
        "issued_by",
        "attestation_hash",
        "rollback_commit",
    }
)


class ProductionExecutionAuthorizationError(ValueError):
    """Raised when execution authorization assessment or authorize fails safely."""


@dataclass(frozen=True)
class ProductionExecutionAuthorizationRecord:
    authorization_id: str
    activation_request_id: str
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
    signer_id: str
    authorized_by: str
    signed_by: str
    authorized_at: str
    expires_at: str
    ttl_seconds: int
    scope_type: str
    authorization_status: str
    tested_commit_sha: str
    release_tag: str
    execution_phrase_required: bool = True
    execution_phrase_verified: bool = False
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
class ProductionExecutionAuthorizationEvent:
    event_id: str
    authorization_id: str
    activation_request_id: str
    event_type: str
    actor_role: str
    reason_code: str
    occurred_at: str


@dataclass(frozen=True)
class ProductionExecutionAuthorizationSummary:
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    boundary_id: str
    boundary_invocation_id: str
    runtime_invocation_id: str
    authorization_id: str
    authorization_state: str
    authorization_ready: bool
    authorization_present: bool
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
    one_shot_scope_valid: bool
    ticket_scope_valid: bool
    window_scope_valid: bool
    authorization_ttl_valid: bool
    final_signoff_valid: bool
    rollback_ready: bool
    operational_signoff_valid: bool
    audit_chain_complete: bool
    recovery_required: bool
    repair_lock_held: bool
    executor_identity_valid: bool
    operator_identity_valid: bool
    signer_identity_valid: bool
    identity_separation_valid: bool
    kill_switch_available: bool
    emergency_close_available: bool
    runtime_factory_available: bool
    runtime_invoker_disabled: bool
    authorized_at: str
    expires_at: str
    authorization_issued: bool
    authorization_expired: bool
    execution_phrase_required: bool
    execution_phrase_verified: bool
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
    boundary_runtime_invoked: bool
    boundary_cutover_started: bool
    invocation_runtime_invoked: bool
    invocation_cutover_started: bool
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str
    ttl_seconds: int = 0
    already_authorized: bool = False
    executor_assigned: bool = False
    operator_present: bool = False
    signer_present: bool = False
    tested_commit_sha_short: str = ""
    release_tag: str = ""
    window_remaining_seconds: int = 0
    permission_remaining_seconds: int = 0
    session_remaining_seconds: int = 0
    boundary_remaining_seconds: int = 0
    invocation_remaining_seconds: int = 0


@dataclass(frozen=True)
class ProductionExecutionAuthorizationReleaseSummary:
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    boundary_id: str
    runtime_invocation_id: str
    authorization_id: str
    controlled_window_state: str
    permission_state: str
    session_state: str
    boundary_state: str
    invocation_state: str
    authorization_state: str
    authorization_ready: bool
    authorization_present: bool
    authorization_expired: bool
    execution_phrase_required: bool = True
    execution_phrase_verified: bool = False
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
    release_status: str = RELEASE_EXECUTION_AUTHORIZATION_NOT_READY


@dataclass(frozen=True)
class ProductionExecutionAuthorizationDashboardDigest:
    execution_authorization_state: str
    execution_authorization_ready: bool
    execution_authorization_present: bool
    execution_authorization_expired: bool
    execution_authorization_id: str
    execution_authorization_expires_at: str
    execution_authorization_phrase_verified: bool
    execution_authorization_blocking_count: int
    execution_authorization_warning_count: int
    execution_authorization_recommended_action: str


def default_execution_authorization_store_dir() -> Path:
    return get_hermes_home() / "coo" / _AUTHORIZATION_STORE_DIR


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


def _authorization_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionExecutionAuthorizationError("activation_request_id is required")
    base = (store_dir or default_execution_authorization_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionExecutionAuthorizationError(
            "Execution authorization store must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_execution_authorization_store_available(
    *, store_dir: Path | None = None
) -> bool:
    try:
        base = (store_dir or default_execution_authorization_store_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _record_from_dict(
    payload: Mapping[str, Any]
) -> ProductionExecutionAuthorizationRecord:
    return ProductionExecutionAuthorizationRecord(
        authorization_id=str(payload.get("authorization_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
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
        signer_id=str(payload.get("signer_id", "")),
        authorized_by=str(payload.get("authorized_by") or payload.get("operator_id", "")),
        signed_by=str(payload.get("signed_by") or payload.get("signer_id", "")),
        authorized_at=str(payload.get("authorized_at", "")),
        expires_at=str(payload.get("expires_at", "")),
        ttl_seconds=int(payload.get("ttl_seconds") or 0),
        scope_type=str(payload.get("scope_type") or SCOPE_TYPE_ONE_SHOT),
        authorization_status=str(payload.get("authorization_status", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        execution_phrase_required=True,
        execution_phrase_verified=bool(payload.get("execution_phrase_verified", False)),
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
    record: ProductionExecutionAuthorizationRecord,
) -> dict[str, Any]:
    return {
        "authorization_id": record.authorization_id,
        "activation_request_id": record.activation_request_id,
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
        "signer_id": record.signer_id,
        "authorized_by": record.authorized_by or record.operator_id,
        "signed_by": record.signed_by or record.signer_id,
        "authorized_at": record.authorized_at,
        "expires_at": record.expires_at,
        "ttl_seconds": record.ttl_seconds,
        "scope_type": SCOPE_TYPE_ONE_SHOT,
        "authorization_status": AUTHORIZATION_ISSUED,
        "tested_commit_sha": _short_sha(record.tested_commit_sha),
        "release_tag": record.release_tag,
        "execution_phrase_required": True,
        "execution_phrase_verified": bool(record.execution_phrase_verified),
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


def load_execution_authorization_record(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionExecutionAuthorizationRecord | None:
    path = _authorization_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionExecutionAuthorizationError(
            "authorization_store_corrupted"
        ) from exc
    authorization = payload.get("authorization")
    if not isinstance(authorization, dict):
        raise ProductionExecutionAuthorizationError("authorization_store_corrupted")
    return _record_from_dict(authorization)


def load_execution_authorization_by_id(
    authorization_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionExecutionAuthorizationRecord | None:
    target = (authorization_id or "").strip()
    if not target:
        raise ProductionExecutionAuthorizationError("authorization_id is required")
    base = (store_dir or default_execution_authorization_store_dir()).resolve()
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductionExecutionAuthorizationError(
                "authorization_store_corrupted"
            ) from exc
        authorization = payload.get("authorization")
        if (
            isinstance(authorization, dict)
            and str(authorization.get("authorization_id", "")) == target
        ):
            return _record_from_dict(authorization)
    return None


def load_execution_authorization_events(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> tuple[ProductionExecutionAuthorizationEvent, ...]:
    path = _authorization_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionExecutionAuthorizationError(
            "authorization_store_corrupted"
        ) from exc
    raw = payload.get("events") or []
    if not isinstance(raw, list):
        raise ProductionExecutionAuthorizationError("authorization_store_corrupted")
    events: list[ProductionExecutionAuthorizationEvent] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ProductionExecutionAuthorizationError("authorization_store_corrupted")
        event_id = str(item.get("event_id", ""))
        if not event_id or event_id in seen:
            raise ProductionExecutionAuthorizationError("authorization_store_corrupted")
        seen.add(event_id)
        events.append(
            ProductionExecutionAuthorizationEvent(
                event_id=event_id,
                authorization_id=str(item.get("authorization_id", "")),
                activation_request_id=str(item.get("activation_request_id", "")),
                event_type=str(item.get("event_type", "")),
                actor_role=str(item.get("actor_role", "")),
                reason_code=str(item.get("reason_code", "")),
                occurred_at=str(item.get("occurred_at", "")),
            )
        )
    return tuple(events)


def load_execution_authorization_events_by_authorization_id(
    authorization_id: str,
    *,
    store_dir: Path | None = None,
) -> tuple[
    ProductionExecutionAuthorizationRecord | None,
    tuple[ProductionExecutionAuthorizationEvent, ...],
]:
    """Scan the whole store for the bundle whose authorization matches `authorization_id`."""
    target = (authorization_id or "").strip()
    if not target:
        raise ProductionExecutionAuthorizationError("authorization_id is required")
    base = (store_dir or default_execution_authorization_store_dir()).resolve()
    if not base.is_dir():
        return None, ()
    for path in sorted(base.glob("*.json")):
        activation_id = path.stem
        record = load_execution_authorization_record(activation_id, store_dir=store_dir)
        if record is not None and record.authorization_id == target:
            events = load_execution_authorization_events(activation_id, store_dir=store_dir)
            return record, events
    return None, ()


def _authorizations_equivalent(
    existing: ProductionExecutionAuthorizationRecord,
    candidate: ProductionExecutionAuthorizationRecord,
) -> bool:
    return (
        existing.runtime_invocation_id == candidate.runtime_invocation_id
        and existing.boundary_id == candidate.boundary_id
        and existing.cutover_contract_id == candidate.cutover_contract_id
        and existing.permission_id == candidate.permission_id
        and existing.session_id == candidate.session_id
        and existing.executor_id == candidate.executor_id
        and existing.operator_id == candidate.operator_id
        and existing.signer_id == candidate.signer_id
        and existing.ttl_seconds == candidate.ttl_seconds
        and existing.ticket_id == candidate.ticket_id
        and existing.confirmation_id == candidate.confirmation_id
        and existing.controlled_window_open_event_id
        == candidate.controlled_window_open_event_id
    )


def _write_authorization_bundle(
    record: ProductionExecutionAuthorizationRecord,
    events: tuple[ProductionExecutionAuthorizationEvent, ...],
    *,
    store_dir: Path | None = None,
) -> None:
    path = _authorization_path(record.activation_request_id, store_dir=store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    payload = {
        "version": _AUTHORIZATION_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "authorization": _record_to_dict(record),
        "events": [
            {
                "event_id": event.event_id,
                "authorization_id": event.authorization_id,
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
            existing = load_execution_authorization_record(
                record.activation_request_id,
                store_dir=store_dir,
            )
            if existing is not None:
                if _authorizations_equivalent(existing, record):
                    return
                raise ProductionExecutionAuthorizationError(
                    "execution_authorization_conflict"
                )
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            try:
                fd = os.open(str(path), flags, 0o644)
            except FileExistsError as exc:
                existing_again = load_execution_authorization_record(
                    record.activation_request_id,
                    store_dir=store_dir,
                )
                if existing_again is not None and _authorizations_equivalent(
                    existing_again, record
                ):
                    return
                raise ProductionExecutionAuthorizationError(
                    "execution_authorization_conflict"
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
        raise ProductionExecutionAuthorizationError("authorization_write_failed") from exc
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
    signer_id: str,
    request,
    contract,
    final_record,
    op_record,
    permission_record,
    session_record,
    boundary_record,
    invocation_record,
) -> tuple[bool, bool, bool, bool, list[str]]:
    """Validate provided identities only (3-way: executor/operator/signer).

    Empty executor/operator/signer (status/check paths) does not append
    identity blocks for the missing role. `authorize` supplies all three;
    `check` may supply executor only (or nothing at all). Executor's
    binding correctness against permission/session/boundary/invocation is
    validated separately via the scope-mismatch checks — this function
    only rules out conflicts with other role holders (requester, security
    reviewer, contract preparer, signers, permission issuer, session
    starter, boundary/invocation reservers) and pairwise separation among
    executor/operator/signer themselves.
    """
    blocking: list[str] = []
    executor = (executor_id or "").strip()
    operator = (operator_id or "").strip()
    signer = (signer_id or "").strip()
    require_executor = bool(executor_id)
    require_operator = bool(operator_id)
    require_signer = bool(signer_id)

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
    conflicts_for_operator.discard("")

    operator_valid = True
    if require_operator:
        operator_valid = bool(operator) and operator not in conflicts_for_operator
        if not operator_valid:
            if operator and executor and operator == executor:
                blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
            else:
                blocking.append(BLOCK_OPERATOR_IDENTITY_INVALID)

    conflicts_for_signer = set(conflicts_for_operator)
    if final_record is not None:
        conflicts_for_signer.add((final_record.signed_by or "").strip())
    if op_record is not None:
        conflicts_for_signer.add((op_record.signed_by or "").strip())
    conflicts_for_signer.discard("")

    signer_valid = True
    if require_signer:
        conflicts_for_signer_with_roles = set(conflicts_for_signer)
        if operator:
            conflicts_for_signer_with_roles.add(operator)
        signer_valid = bool(signer) and signer not in conflicts_for_signer_with_roles
        if not signer_valid:
            if signer and (
                (executor and signer == executor) or (operator and signer == operator)
            ):
                blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
            else:
                blocking.append(BLOCK_SIGNER_IDENTITY_INVALID)

    separation = True
    roles_required = [require_executor, require_operator, require_signer]
    if all(roles_required):
        separation = (
            executor_valid
            and operator_valid
            and signer_valid
            and executor != operator
            and operator != signer
            and executor != signer
        )
        if executor_valid and operator_valid and signer_valid and not separation:
            blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
    elif require_executor and require_operator:
        separation = executor_valid and operator_valid and executor != operator
        if executor_valid and operator_valid and not separation:
            blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
    elif require_operator and require_signer:
        separation = operator_valid and signer_valid and operator != signer
        if operator_valid and signer_valid and not separation:
            blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)
    elif require_executor and require_signer:
        separation = executor_valid and signer_valid and executor != signer
        if executor_valid and signer_valid and not separation:
            blocking.append(BLOCK_IDENTITY_SEPARATION_INVALID)

    return executor_valid, operator_valid, signer_valid, separation, blocking


def _validate_authorization_ttl(
    ttl_seconds: int | None,
    *,
    now: datetime,
    invocation_expires_at: datetime | None,
    boundary_expires_at: datetime | None,
    session_expires_at: datetime | None,
    permission_expires_at: datetime | None,
    window_end: datetime | None,
) -> tuple[bool, str, int, int, int, int, int, int, str]:
    """Return (ttl_valid, invalid_reason, effective_ttl, invocation_remaining,
    boundary_remaining, session_remaining, permission_remaining,
    window_remaining, expires_at_iso).

    Fail-closed: a missing invocation/boundary/session/permission/window
    is treated as zero remaining seconds, so a candidate ttl can never be
    accepted against an unknown prerequisite. `expires_at` is always
    clamped to at most `min(invocation.expires_at, boundary.expires_at,
    session.expires_at, permission.expires_at, window_end, now + ttl)`.
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
            "",
        )
    remaining = min(
        invocation_remaining,
        boundary_remaining,
        session_remaining,
        permission_remaining,
        window_remaining,
    )
    if remaining < MIN_AUTHORIZATION_TTL_SECONDS:
        return (
            False,
            "insufficient_remaining",
            ttl_seconds,
            invocation_remaining,
            boundary_remaining,
            session_remaining,
            permission_remaining,
            window_remaining,
            "",
        )
    if (
        ttl_seconds < MIN_AUTHORIZATION_TTL_SECONDS
        or ttl_seconds > MAX_AUTHORIZATION_TTL_SECONDS
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
        expires.isoformat(),
    )


def _recommended_action(
    state: str,
    blocking: tuple[str, ...],
    *,
    window_open: bool,
    recovery: bool,
) -> str:
    if state == AUTHORIZATION_ISSUED:
        return ACTION_EXECUTION_AUTHORIZED_WAIT_FOR_PHASE_15H
    if state == AUTHORIZATION_READY:
        return ACTION_AUTHORIZE_GOVERNED_RUNTIME_EXECUTION
    if state == AUTHORIZATION_EXPIRED:
        return ACTION_RESOLVE_RUNTIME_INVOCATION
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
        or BLOCK_SIGNER_IDENTITY_INVALID in blocking
        or BLOCK_IDENTITY_SEPARATION_INVALID in blocking
    ):
        return ACTION_RESOLVE_IDENTITY_SEPARATION
    if (
        BLOCK_RUNTIME_INVOCATION_MISSING in blocking
        or BLOCK_RUNTIME_INVOCATION_INVALID in blocking
        or BLOCK_RUNTIME_INVOCATION_EXPIRED in blocking
        or BLOCK_RUNTIME_INVOCATION_ID_MISMATCH in blocking
        or BLOCK_RUNTIME_INVOCATION_EXECUTOR_MISMATCH in blocking
        or BLOCK_INVOCATION_EXECUTION_PHRASE_NOT_REQUIRED in blocking
        or BLOCK_INVOCATION_EXECUTION_PHRASE_ALREADY_VERIFIED in blocking
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
        BLOCK_AUTHORIZATION_TTL_INVALID in blocking
        or BLOCK_AUTHORIZATION_TTL_EXCEEDS_INVOCATION in blocking
    ):
        return ACTION_RESOLVE_RUNTIME_INVOCATION
    if BLOCK_AUTHORIZATION_TTL_EXCEEDS_BOUNDARY in blocking:
        return ACTION_RESOLVE_RUNTIME_BOUNDARY
    if BLOCK_AUTHORIZATION_TTL_EXCEEDS_SESSION in blocking:
        return ACTION_RESOLVE_GOVERNED_RUNTIME_SESSION
    if BLOCK_AUTHORIZATION_TTL_EXCEEDS_PERMISSION in blocking:
        return ACTION_RESOLVE_RUNTIME_PERMISSION
    if BLOCK_AUTHORIZATION_TTL_EXCEEDS_WINDOW in blocking:
        return ACTION_WAIT_FOR_WINDOW_OPEN
    if (
        BLOCK_FINAL_SIGNOFF_INVALID in blocking
        or BLOCK_ROLLBACK_VALIDATION_INVALID in blocking
        or BLOCK_OPERATIONAL_SIGNOFF_INVALID in blocking
        or BLOCK_GOVERNED_CUTOVER_CONTRACT_MISSING in blocking
        or BLOCK_GOVERNED_CUTOVER_CONTRACT_INVALID in blocking
    ):
        return ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT
    if BLOCK_EXECUTION_AUTHORIZATION_ALREADY_ISSUED in blocking:
        return ACTION_EXECUTION_AUTHORIZED_WAIT_FOR_PHASE_15H
    if BLOCK_EXECUTION_AUTHORIZATION_CONFLICT in blocking:
        return ACTION_MAINTAIN_PRODUCTION_BLOCK
    if not window_open:
        return ACTION_WAIT_FOR_WINDOW_OPEN
    return ACTION_MAINTAIN_PRODUCTION_BLOCK


def evaluate_production_execution_authorization(
    *,
    activation_request_id: str,
    runtime_invocation_id: str = "",
    executor_id: str = "",
    operator_id: str = "",
    signer_id: str = "",
    ttl_seconds: int | None = None,
    authorization_store_dir: Path | None = None,
    invocation_store_dir: Path | None = None,
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
) -> ProductionExecutionAuthorizationSummary:
    """Read-only execution authorization assessment."""
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
        blocking.append(BLOCK_AUTHORIZATION_STORE_CORRUPTED)
        invocation_record = None

    permission_record = None
    try:
        permission_record = load_runtime_permission_record(
            activation_request_id,
            store_dir=permission_store_dir,
        )
    except ProductionRuntimePermissionError:
        blocking.append(BLOCK_AUTHORIZATION_STORE_CORRUPTED)
        permission_record = None

    session_record = None
    try:
        session_record = load_governed_runtime_session_record(
            activation_request_id,
            store_dir=session_store_dir,
        )
    except ProductionGovernedRuntimeSessionError:
        blocking.append(BLOCK_AUTHORIZATION_STORE_CORRUPTED)
        session_record = None

    boundary_record = None
    try:
        boundary_record = load_runtime_boundary_record(
            activation_request_id,
            store_dir=boundary_store_dir,
        )
    except RuntimeBoundaryError:
        blocking.append(BLOCK_AUTHORIZATION_STORE_CORRUPTED)
        boundary_record = None

    existing = None
    try:
        existing = load_execution_authorization_record(
            activation_request_id,
            store_dir=authorization_store_dir,
        )
    except ProductionExecutionAuthorizationError:
        blocking.append(BLOCK_AUTHORIZATION_STORE_CORRUPTED)
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
            blocking.append(BLOCK_AUTHORIZATION_STORE_CORRUPTED)

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
        if (
            runtime_invocation_id
            and invocation_record.runtime_invocation_id != runtime_invocation_id.strip()
        ):
            blocking.append(BLOCK_RUNTIME_INVOCATION_ID_MISMATCH)
            invocation_scope_valid = False
        if not invocation_record.execution_phrase_required:
            blocking.append(BLOCK_INVOCATION_EXECUTION_PHRASE_NOT_REQUIRED)
            invocation_valid = False
        if invocation_record.execution_phrase_verified:
            blocking.append(BLOCK_INVOCATION_EXECUTION_PHRASE_ALREADY_VERIFIED)
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
        expires_iso,
    ) = _validate_authorization_ttl(
        ttl_seconds,
        now=current,
        invocation_expires_at=invocation_expires_dt,
        boundary_expires_at=boundary_expires_dt,
        session_expires_at=session_expires_dt,
        permission_expires_at=permission_expires_dt,
        window_end=window_end,
    )
    if ttl_seconds is not None and not ttl_valid:
        if ttl_reason == "exceeds_invocation":
            blocking.append(BLOCK_AUTHORIZATION_TTL_EXCEEDS_INVOCATION)
        elif ttl_reason == "exceeds_boundary":
            blocking.append(BLOCK_AUTHORIZATION_TTL_EXCEEDS_BOUNDARY)
        elif ttl_reason == "exceeds_session":
            blocking.append(BLOCK_AUTHORIZATION_TTL_EXCEEDS_SESSION)
        elif ttl_reason == "exceeds_permission":
            blocking.append(BLOCK_AUTHORIZATION_TTL_EXCEEDS_PERMISSION)
        elif ttl_reason == "exceeds_window":
            blocking.append(BLOCK_AUTHORIZATION_TTL_EXCEEDS_WINDOW)
        else:
            blocking.append(BLOCK_AUTHORIZATION_TTL_INVALID)

    executor_valid, operator_valid, signer_valid, separation_valid, id_blocks = (
        _assess_identities(
            executor_id=executor_id,
            operator_id=operator_id,
            signer_id=signer_id,
            request=request,
            contract=contract,
            final_record=final_record,
            op_record=op_record,
            permission_record=permission_record,
            session_record=session_record,
            boundary_record=boundary_record,
            invocation_record=invocation_record,
        )
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

    # --- Existing authorization idempotency / conflict -------------------------
    authorization_id = ""
    authorized_at = ""
    expires_at = ""
    already_authorized = False
    authorization_present = existing is not None
    authorization_expired_flag = False
    authorization_conflict = False
    if existing is not None:
        authorization_id = existing.authorization_id
        authorized_at = existing.authorized_at
        expires_at = existing.expires_at
        existing_expires_dt = _parse_iso(existing.expires_at)
        if existing_expires_dt is not None and current >= existing_expires_dt:
            authorization_expired_flag = True
            blocking.append(BLOCK_EXECUTION_AUTHORIZATION_EXPIRED)
        else:
            already_authorized = True
            if (
                runtime_invocation_id
                or executor_id
                or operator_id
                or signer_id
                or ttl_seconds is not None
            ):
                equivalent = (
                    (
                        not runtime_invocation_id
                        or existing.runtime_invocation_id
                        == runtime_invocation_id.strip()
                    )
                    and (not executor_id or existing.executor_id == executor_id.strip())
                    and (not operator_id or existing.operator_id == operator_id.strip())
                    and (not signer_id or existing.signer_id == signer_id.strip())
                    and (ttl_seconds is None or existing.ttl_seconds == ttl_seconds)
                )
                if not equivalent:
                    authorization_conflict = True
                    blocking.append(BLOCK_EXECUTION_AUTHORIZATION_CONFLICT)
            if not authorization_conflict:
                blocking.append(BLOCK_EXECUTION_AUTHORIZATION_ALREADY_ISSUED)

    unique_blocking = tuple(dict.fromkeys(blocking))

    excluded_when_no_ttl = {
        BLOCK_AUTHORIZATION_TTL_INVALID,
        BLOCK_AUTHORIZATION_TTL_EXCEEDS_INVOCATION,
        BLOCK_AUTHORIZATION_TTL_EXCEEDS_BOUNDARY,
        BLOCK_AUTHORIZATION_TTL_EXCEEDS_SESSION,
        BLOCK_AUTHORIZATION_TTL_EXCEEDS_PERMISSION,
        BLOCK_AUTHORIZATION_TTL_EXCEEDS_WINDOW,
    }
    excluded_when_no_identity = {
        BLOCK_EXECUTOR_IDENTITY_INVALID,
        BLOCK_OPERATOR_IDENTITY_INVALID,
        BLOCK_SIGNER_IDENTITY_INVALID,
        BLOCK_IDENTITY_SEPARATION_INVALID,
    }
    hard_ready_blockers = [
        code
        for code in unique_blocking
        if code
        not in {
            BLOCK_EXECUTION_AUTHORIZATION_ALREADY_ISSUED,
        }
        and not (ttl_seconds is None and code in excluded_when_no_ttl)
        and not (
            not executor_id
            and not operator_id
            and not signer_id
            and code in excluded_when_no_identity
        )
        and not (not executor_id and code == BLOCK_PERMISSION_EXECUTOR_MISMATCH)
        and not (not executor_id and code == BLOCK_SESSION_EXECUTOR_MISMATCH)
        and not (not executor_id and code == BLOCK_RUNTIME_BOUNDARY_EXECUTOR_MISMATCH)
        and not (not executor_id and code == BLOCK_RUNTIME_INVOCATION_EXECUTOR_MISMATCH)
        and not (
            not runtime_invocation_id and code == BLOCK_RUNTIME_INVOCATION_ID_MISMATCH
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

    authorization_ready_calc = (
        core_ready
        and executor_valid
        and operator_valid
        and signer_valid
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
            not runtime_invocation_id
            or BLOCK_RUNTIME_INVOCATION_ID_MISMATCH not in unique_blocking
        )
        and ttl_seconds is not None
        and ttl_valid
        and not authorization_conflict
    )

    if existing is not None and authorization_expired_flag:
        state = AUTHORIZATION_EXPIRED
        ready = False
    elif existing is not None and authorization_conflict:
        state = AUTHORIZATION_BLOCKED
        ready = False
    elif existing is not None and already_authorized:
        state = AUTHORIZATION_ISSUED
        ready = False
    elif recovery_required or repair_lock_held:
        state = AUTHORIZATION_BLOCKED
        ready = False
    elif authorization_ready_calc:
        state = AUTHORIZATION_READY
        ready = True
    elif hard_ready_blockers:
        state = AUTHORIZATION_BLOCKED
        ready = False
    else:
        state = AUTHORIZATION_NOT_ISSUED
        ready = False

    warnings.extend(
        [
            WARN_AUTHORIZATION_IS_PREREQUISITE_ONLY,
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
    if state == AUTHORIZATION_EXPIRED:
        warnings.append(WARN_AUTHORIZATION_EXPIRY_REQUIRES_NEW_INVOCATION)
    if state == AUTHORIZATION_ISSUED:
        warnings.append(WARN_RUNTIME_EXECUTION_BLOCKED_WAITING_PHASE_15H)
    unique_warnings = tuple(dict.fromkeys(warnings))

    recommended = _recommended_action(
        state,
        unique_blocking,
        window_open=window.window_open,
        recovery=recovery_required or repair_lock_held,
    )

    return ProductionExecutionAuthorizationSummary(
        activation_request_id=activation_request_id,
        cutover_contract_id=cutover_contract_id,
        permission_id=permission_record.permission_id if permission_record else "",
        session_id=session_record.session_id if session_record else "",
        boundary_id=boundary_record.boundary_id if boundary_record else "",
        boundary_invocation_id=boundary_record.invocation_id if boundary_record else "",
        runtime_invocation_id=(
            invocation_record.runtime_invocation_id if invocation_record else ""
        ),
        authorization_id=authorization_id,
        authorization_state=state,
        authorization_ready=ready,
        authorization_present=authorization_present,
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
        one_shot_scope_valid=one_shot_scope_valid,
        ticket_scope_valid=ticket_scope_valid,
        window_scope_valid=window_scope_valid,
        authorization_ttl_valid=ttl_valid if ttl_seconds is not None else True,
        final_signoff_valid=final_signoff_valid,
        rollback_ready=rollback_ready,
        operational_signoff_valid=operational_signoff_valid,
        audit_chain_complete=audit_chain_complete,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        executor_identity_valid=executor_valid if executor_id else True,
        operator_identity_valid=operator_valid if operator_id else True,
        signer_identity_valid=signer_valid if signer_id else True,
        identity_separation_valid=(
            separation_valid if (executor_id or operator_id or signer_id) else True
        ),
        kill_switch_available=kill_switch_available,
        emergency_close_available=emergency_close_available,
        runtime_factory_available=runtime_factory_available,
        runtime_invoker_disabled=runtime_invoker_disabled,
        authorized_at=authorized_at,
        expires_at=expires_at or expires_iso,
        authorization_issued=already_authorized,
        authorization_expired=authorization_expired_flag,
        execution_phrase_required=True,
        execution_phrase_verified=bool(existing.execution_phrase_verified) if existing else False,
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
        boundary_runtime_invoked=False,
        boundary_cutover_started=False,
        invocation_runtime_invoked=False,
        invocation_cutover_started=False,
        blocking_items=unique_blocking,
        warning_items=unique_warnings,
        recommended_action=recommended,
        ttl_seconds=effective_ttl if ttl_seconds is not None else (existing.ttl_seconds if existing else 0),
        already_authorized=already_authorized,
        executor_assigned=bool((executor_id or "").strip())
        or (existing is not None and bool(existing.executor_id)),
        operator_present=bool((operator_id or "").strip())
        or (existing is not None and bool(existing.operator_id)),
        signer_present=bool((signer_id or "").strip())
        or (existing is not None and bool(existing.signer_id)),
        tested_commit_sha_short=_short_sha(
            request.tested_commit_sha if request is not None else ""
        ),
        release_tag=request.release_tag if request is not None else "",
        window_remaining_seconds=window_remaining,
        permission_remaining_seconds=permission_remaining,
        session_remaining_seconds=session_remaining,
        boundary_remaining_seconds=boundary_remaining,
        invocation_remaining_seconds=invocation_remaining,
    )


def authorize_production_execution_authorization(
    *,
    activation_request_id: str,
    runtime_invocation_id: str,
    executor_id: str,
    operator_id: str,
    signer_id: str,
    ttl_seconds: int,
    phrase: str,
    authorization_store_dir: Path | None = None,
    invocation_store_dir: Path | None = None,
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
) -> ProductionExecutionAuthorizationSummary:
    """Append-only issue of a one-shot execution authorization record.

    Never invokes runtime, never starts cutover, never consumes the
    underlying permission — those remain exclusively Phase 15H's
    concern. The confirmation phrase is validated by *exact* equality
    (no whitespace stripping, case-sensitive) BEFORE any write happens;
    a wrong phrase raises with zero mutation and never appears in the
    raised error message. On success, only the boolean
    `execution_phrase_verified=True` is persisted on the new
    authorization record — the phrase text itself is never written
    anywhere.
    """
    if not probe_execution_authorization_store_available(
        store_dir=authorization_store_dir
    ):
        raise ProductionExecutionAuthorizationError("authorization_write_failed")

    def _evaluate() -> ProductionExecutionAuthorizationSummary:
        return evaluate_production_execution_authorization(
            activation_request_id=activation_request_id,
            runtime_invocation_id=runtime_invocation_id,
            executor_id=executor_id,
            operator_id=operator_id,
            signer_id=signer_id,
            ttl_seconds=ttl_seconds,
            authorization_store_dir=authorization_store_dir,
            invocation_store_dir=invocation_store_dir,
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

    existing = load_execution_authorization_record(
        activation_request_id,
        store_dir=authorization_store_dir,
    )
    if existing is not None:
        expires_dt = _parse_iso(existing.expires_at)
        if expires_dt is not None and _utc_now(now) >= expires_dt:
            raise ProductionExecutionAuthorizationError("execution_authorization_expired")
        equivalent = (
            existing.runtime_invocation_id == (runtime_invocation_id or "").strip()
            and existing.executor_id == (executor_id or "").strip()
            and existing.operator_id == (operator_id or "").strip()
            and existing.signer_id == (signer_id or "").strip()
            and existing.ttl_seconds == int(ttl_seconds)
        )
        if equivalent:
            return _evaluate()
        raise ProductionExecutionAuthorizationError("execution_authorization_conflict")

    if summary.authorization_state != AUTHORIZATION_READY:
        raise ProductionExecutionAuthorizationError(
            f"execution authorization blocked for state {summary.authorization_state!r}"
        )

    # Phrase re-validated here, BEFORE any load-for-write or write happens
    # below. Exact equality only — no strip(), case-sensitive. Never
    # echoed in the raised error.
    if phrase != REQUIRED_CONFIRMATION_PHRASE:
        raise ProductionExecutionAuthorizationError("execution_phrase_invalid")

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
        invocation_record is None
        or boundary_record is None
        or session_record is None
        or permission_record is None
        or contract is None
        or reservation is None
    ):
        raise ProductionExecutionAuthorizationError("authorization_store_corrupted")
    if (
        runtime_invocation_id
        and invocation_record.runtime_invocation_id != runtime_invocation_id.strip()
    ):
        raise ProductionExecutionAuthorizationError("runtime_invocation_id_mismatch")
    if invocation_record.executor_id != (executor_id or "").strip():
        raise ProductionExecutionAuthorizationError(
            "runtime_invocation_executor_mismatch"
        )
    if boundary_record.runtime_invoked or boundary_record.cutover_started:
        raise ProductionExecutionAuthorizationError("runtime_boundary_invalid")

    current = _utc_now(now)
    invocation_expires_dt = _parse_iso(invocation_record.expires_at)
    if invocation_expires_dt is None or current >= invocation_expires_dt:
        raise ProductionExecutionAuthorizationError("runtime_invocation_expired")
    boundary_expires_dt = _parse_iso(boundary_record.expires_at)
    if boundary_expires_dt is None or current >= boundary_expires_dt:
        raise ProductionExecutionAuthorizationError("runtime_boundary_expired")
    session_expires_dt = _parse_iso(session_record.expires_at)
    if session_expires_dt is None or current >= session_expires_dt:
        raise ProductionExecutionAuthorizationError("governed_runtime_session_expired")
    permission_expires_dt = _parse_iso(permission_record.expires_at)
    if permission_expires_dt is None or current >= permission_expires_dt:
        raise ProductionExecutionAuthorizationError("runtime_permission_expired")
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
        expires_iso,
    ) = _validate_authorization_ttl(
        ttl_seconds,
        now=current,
        invocation_expires_at=invocation_expires_dt,
        boundary_expires_at=boundary_expires_dt,
        session_expires_at=session_expires_dt,
        permission_expires_at=permission_expires_dt,
        window_end=window_end,
    )
    if not ttl_valid:
        raise ProductionExecutionAuthorizationError(
            ttl_reason and f"authorization_ttl_{ttl_reason}" or "authorization_ttl_invalid"
        )

    authorization_id = str(uuid.uuid4())
    authorized_at = _utc_now_iso(now)
    record = ProductionExecutionAuthorizationRecord(
        authorization_id=authorization_id,
        activation_request_id=activation_request_id,
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
        controlled_window_open_event_id=invocation_record.controlled_window_open_event_id,
        runtime_invocation_id=invocation_record.runtime_invocation_id,
        executor_id=(executor_id or "").strip(),
        operator_id=(operator_id or "").strip(),
        signer_id=(signer_id or "").strip(),
        authorized_by=(operator_id or "").strip(),
        signed_by=(signer_id or "").strip(),
        authorized_at=authorized_at,
        expires_at=expires_iso,
        ttl_seconds=effective_ttl,
        scope_type=SCOPE_TYPE_ONE_SHOT,
        authorization_status=AUTHORIZATION_ISSUED,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
        execution_phrase_verified=True,
    )
    events = (
        ProductionExecutionAuthorizationEvent(
            event_id=str(uuid.uuid4()),
            authorization_id=authorization_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_EXECUTION_AUTHORIZATION_REQUESTED,
            actor_role="operator",
            reason_code="",
            occurred_at=authorized_at,
        ),
        ProductionExecutionAuthorizationEvent(
            event_id=str(uuid.uuid4()),
            authorization_id=authorization_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_EXECUTION_PHRASE_VERIFIED,
            actor_role="operator",
            reason_code="",
            occurred_at=authorized_at,
        ),
        ProductionExecutionAuthorizationEvent(
            event_id=str(uuid.uuid4()),
            authorization_id=authorization_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_EXECUTION_AUTHORIZATION_ISSUED,
            actor_role="system",
            reason_code="",
            occurred_at=authorized_at,
        ),
        ProductionExecutionAuthorizationEvent(
            event_id=str(uuid.uuid4()),
            authorization_id=authorization_id,
            activation_request_id=activation_request_id,
            event_type=EVENT_RUNTIME_EXECUTION_BLOCKED,
            actor_role="system",
            reason_code="",
            occurred_at=authorized_at,
        ),
    )
    _write_authorization_bundle(record, events, store_dir=authorization_store_dir)
    return _evaluate()


class ExecutionAuthorizationContext:
    """Read-only handle onto an issued execution authorization.

    Entering re-validates that the authorization is still live (not
    expired, the controlled window is still open, the underlying
    invocation/boundary are still reserved and unexpired, the underlying
    session is still started and unexpired, and the underlying permission
    is still issued and unconsumed) before exposing any state. The
    execution phrase is re-checked only via the persisted
    `execution_phrase_verified` boolean on the authorization record — no
    invoker call, no re-prompt, no re-comparison against the phrase text
    (which this module never stores). Never invokes runtime, never starts
    cutover, never consumes the permission. The context is single-use: it
    cannot be entered while already active and cannot be re-entered after
    it has been exited.
    """

    def __init__(
        self,
        authorization_id: str = "",
        *,
        activation_request_id: str = "",
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
        self.authorization_id = (authorization_id or "").strip()
        self.activation_request_id = (activation_request_id or "").strip()
        if not self.authorization_id and not self.activation_request_id:
            raise ProductionExecutionAuthorizationError(
                "authorization_id or activation_request_id is required"
            )
        self.runtime_invocation_id = ""
        self.boundary_id = ""
        self.session_id = ""
        self.permission_id = ""
        self._executor_id = ""
        self.entered_at = ""
        self.expires_at = ""
        self.execution_phrase_verified = False
        self.active = False
        self.consumed = False
        self._entered_once = False
        self._now = now
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

    def __enter__(self) -> "ExecutionAuthorizationContext":
        if self.active:
            raise ProductionExecutionAuthorizationError(
                "execution authorization context is already active"
            )
        if self._entered_once:
            raise ProductionExecutionAuthorizationError(
                "execution authorization context cannot be reused after exit"
            )

        record: ProductionExecutionAuthorizationRecord | None = None
        if self.activation_request_id:
            record = load_execution_authorization_record(
                self.activation_request_id, store_dir=self._authorization_store_dir
            )
            if record is None or (
                self.authorization_id
                and record.authorization_id != self.authorization_id
            ):
                raise ProductionExecutionAuthorizationError(
                    "execution_authorization_not_found"
                )
        else:
            record, _ = load_execution_authorization_events_by_authorization_id(
                self.authorization_id, store_dir=self._authorization_store_dir
            )
            if record is None:
                raise ProductionExecutionAuthorizationError(
                    "execution_authorization_not_found"
                )
            self.activation_request_id = record.activation_request_id

        assert record is not None
        current = _utc_now(self._now)
        expires_dt = _parse_iso(record.expires_at)
        if expires_dt is None or current >= expires_dt:
            raise ProductionExecutionAuthorizationError("execution_authorization_expired")
        if not record.execution_phrase_verified:
            raise ProductionExecutionAuthorizationError("execution_phrase_not_verified")

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
            raise ProductionExecutionAuthorizationError("controlled_window_not_open")

        try:
            invocation_record = load_runtime_invocation_record(
                self.activation_request_id, store_dir=self._invocation_store_dir
            )
        except ProductionRuntimeInvocationError as exc:
            raise ProductionExecutionAuthorizationError(
                "authorization_store_corrupted"
            ) from exc
        if (
            invocation_record is None
            or invocation_record.runtime_invocation_id != record.runtime_invocation_id
        ):
            raise ProductionExecutionAuthorizationError("runtime_invocation_invalid")
        invocation_expires_dt = _parse_iso(invocation_record.expires_at)
        if invocation_expires_dt is None or current >= invocation_expires_dt:
            raise ProductionExecutionAuthorizationError("runtime_invocation_expired")

        try:
            boundary_record = load_runtime_boundary_record(
                self.activation_request_id, store_dir=self._boundary_store_dir
            )
        except RuntimeBoundaryError as exc:
            raise ProductionExecutionAuthorizationError(
                "authorization_store_corrupted"
            ) from exc
        if boundary_record is None or boundary_record.boundary_id != record.boundary_id:
            raise ProductionExecutionAuthorizationError("runtime_boundary_invalid")
        boundary_expires_dt = _parse_iso(boundary_record.expires_at)
        if boundary_expires_dt is None or current >= boundary_expires_dt:
            raise ProductionExecutionAuthorizationError("runtime_boundary_expired")
        if boundary_record.runtime_invoked or boundary_record.cutover_started:
            raise ProductionExecutionAuthorizationError("runtime_boundary_invalid")

        try:
            session_record = load_governed_runtime_session_record(
                self.activation_request_id, store_dir=self._session_store_dir
            )
        except ProductionGovernedRuntimeSessionError as exc:
            raise ProductionExecutionAuthorizationError(
                "authorization_store_corrupted"
            ) from exc
        if session_record is None or session_record.session_id != record.session_id:
            raise ProductionExecutionAuthorizationError(
                "governed_runtime_session_invalid"
            )
        session_expires_dt = _parse_iso(session_record.expires_at)
        if session_expires_dt is None or current >= session_expires_dt:
            raise ProductionExecutionAuthorizationError("governed_runtime_session_expired")

        try:
            permission_record = load_runtime_permission_record(
                self.activation_request_id, store_dir=self._permission_store_dir
            )
        except ProductionRuntimePermissionError as exc:
            raise ProductionExecutionAuthorizationError(
                "authorization_store_corrupted"
            ) from exc
        if (
            permission_record is None
            or permission_record.permission_id != record.permission_id
        ):
            raise ProductionExecutionAuthorizationError("runtime_permission_invalid")
        if (
            permission_record.permission_status != PERMISSION_ISSUED
            or permission_record.consumed
            or permission_record.revoked
        ):
            raise ProductionExecutionAuthorizationError("runtime_permission_invalid")
        permission_expires_dt = _parse_iso(permission_record.expires_at)
        if permission_expires_dt is None or current >= permission_expires_dt:
            raise ProductionExecutionAuthorizationError("runtime_permission_expired")

        self.authorization_id = record.authorization_id
        self.runtime_invocation_id = record.runtime_invocation_id
        self.boundary_id = record.boundary_id
        self.session_id = record.session_id
        self.permission_id = record.permission_id
        self._executor_id = record.executor_id
        self.execution_phrase_verified = record.execution_phrase_verified
        self.entered_at = _utc_now_iso(current)
        self.expires_at = record.expires_at
        self.active = True
        self._entered_once = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.active = False
        return False

    def __getstate__(self):
        raise ProductionExecutionAuthorizationError(
            "execution authorization context cannot be serialized"
        )

    def __setstate__(self, state):
        raise ProductionExecutionAuthorizationError(
            "execution authorization context cannot be serialized"
        )

    def __reduce__(self):
        raise ProductionExecutionAuthorizationError(
            "execution authorization context cannot be serialized"
        )


def enter_execution_authorization_context(
    *,
    authorization_id: str = "",
    activation_request_id: str = "",
    **kwargs: Any,
) -> ExecutionAuthorizationContext:
    """Factory returning an unentered context manager; use via `with`."""
    return ExecutionAuthorizationContext(
        authorization_id,
        activation_request_id=activation_request_id,
        **kwargs,
    )


_AUTHORIZATION_CONSUME_STORE_DIR = "production-execution-authorization-consume"


def default_execution_authorization_consume_store_dir() -> Path:
    return get_hermes_home() / "coo" / _AUTHORIZATION_CONSUME_STORE_DIR


def _authorization_consume_path(
    authorization_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (authorization_id or "").strip()
    if not normalized:
        raise ProductionExecutionAuthorizationError("authorization_id is required")
    base = store_dir or default_execution_authorization_consume_store_dir()
    return base / f"{normalized}.json"


def load_execution_authorization_consume_record(
    authorization_id: str,
    *,
    store_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return the consume record for authorization_id, or None if unconsumed."""
    from agent.coo.production_runtime_consume_store import read_consume_record

    path = _authorization_consume_path(authorization_id, store_dir=store_dir)
    try:
        return read_consume_record(path)
    except ValueError as exc:
        raise ProductionExecutionAuthorizationError(str(exc)) from exc


def consume_execution_authorization(
    activation_request_id: str,
    *,
    authorization_id: str,
    consumed_by: str,
    store_dir: Path | None = None,
    consume_store_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One-shot consume transition for an issued execution authorization.

    Never mutates the original write-once authorization bundle. Consumption
    is recorded as a separate one-shot artifact, mirroring
    ``consume_production_runtime_permission``.
    """
    normalized_authorization_id = (authorization_id or "").strip()
    normalized_consumed_by = (consumed_by or "").strip()
    if not normalized_authorization_id:
        raise ProductionExecutionAuthorizationError("authorization_id is required")
    if not normalized_consumed_by:
        raise ProductionExecutionAuthorizationError("consumed_by is required")

    record = load_execution_authorization_record(
        activation_request_id, store_dir=store_dir
    )
    if record is None:
        raise ProductionExecutionAuthorizationError("authorization_missing")
    if record.authorization_id != normalized_authorization_id:
        raise ProductionExecutionAuthorizationError("authorization_id_mismatch")
    if record.authorization_status != AUTHORIZATION_ISSUED:
        raise ProductionExecutionAuthorizationError("authorization_not_issued")
    if record.revoked:
        raise ProductionExecutionAuthorizationError("authorization_revoked")

    current = _utc_now(now)
    expires_dt = _parse_iso(record.expires_at)
    if expires_dt is not None and current >= expires_dt:
        raise ProductionExecutionAuthorizationError("authorization_expired")

    if (
        load_execution_authorization_consume_record(
            normalized_authorization_id, store_dir=consume_store_dir
        )
        is not None
    ):
        raise ProductionExecutionAuthorizationError("authorization_already_consumed")

    from agent.coo.production_runtime_consume_store import (
        OneShotConsumeWriteConflict,
        write_once_consume_record,
    )

    payload = {
        "version": 1,
        "authorization_id": normalized_authorization_id,
        "activation_request_id": activation_request_id,
        "consumed": True,
        "consumed_at": _utc_now_iso(now),
        "consumed_by": normalized_consumed_by,
    }
    path = _authorization_consume_path(
        normalized_authorization_id, store_dir=consume_store_dir
    )
    try:
        write_once_consume_record(path, payload)
    except OneShotConsumeWriteConflict as exc:
        raise ProductionExecutionAuthorizationError(
            "authorization_already_consumed"
        ) from exc
    return payload


def build_production_execution_authorization_release_summary(
    summary: ProductionExecutionAuthorizationSummary,
) -> ProductionExecutionAuthorizationReleaseSummary:
    if (
        summary.authorization_state == AUTHORIZATION_ISSUED
        and summary.invocation_valid
        and summary.boundary_valid
        and summary.session_state == SESSION_STARTED
        and summary.permission_state == PERMISSION_ISSUED
        and summary.controlled_window_open
    ):
        release_status = RELEASE_EXECUTION_AUTHORIZATION_ISSUED
        next_phase = _NEXT_PHASE_15H
    elif summary.authorization_state == AUTHORIZATION_EXPIRED:
        release_status = RELEASE_EXECUTION_AUTHORIZATION_EXPIRED
        next_phase = ""
    elif summary.recovery_required or summary.repair_lock_held:
        release_status = RELEASE_EXECUTION_AUTHORIZATION_RECOVERY_REQUIRED
        next_phase = ""
    elif summary.authorization_ready or summary.authorization_state == AUTHORIZATION_READY:
        release_status = RELEASE_EXECUTION_AUTHORIZATION_READY
        next_phase = ""
    else:
        release_status = RELEASE_EXECUTION_AUTHORIZATION_NOT_READY
        next_phase = ""

    return ProductionExecutionAuthorizationReleaseSummary(
        activation_request_id=summary.activation_request_id,
        cutover_contract_id=summary.cutover_contract_id,
        permission_id=summary.permission_id,
        session_id=summary.session_id,
        boundary_id=summary.boundary_id,
        runtime_invocation_id=summary.runtime_invocation_id,
        authorization_id=summary.authorization_id,
        controlled_window_state=summary.controlled_window_state,
        permission_state=summary.permission_state,
        session_state=summary.session_state,
        boundary_state=summary.boundary_state,
        invocation_state=summary.invocation_state,
        authorization_state=summary.authorization_state,
        authorization_ready=summary.authorization_ready,
        authorization_present=summary.authorization_present,
        authorization_expired=summary.authorization_state == AUTHORIZATION_EXPIRED,
        execution_phrase_required=True,
        execution_phrase_verified=summary.execution_phrase_verified,
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


def resolve_latest_execution_authorization_dashboard_digest(
    *,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
    session_store_dir: Path | None = None,
    boundary_store_dir: Path | None = None,
    invocation_store_dir: Path | None = None,
    authorization_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> ProductionExecutionAuthorizationDashboardDigest:
    base = (governed_cutover_store_dir or default_governed_cutover_store_dir()).resolve()
    if not base.is_dir():
        return ProductionExecutionAuthorizationDashboardDigest(
            execution_authorization_state="not_configured",
            execution_authorization_ready=False,
            execution_authorization_present=False,
            execution_authorization_expired=False,
            execution_authorization_id="",
            execution_authorization_expires_at="",
            execution_authorization_phrase_verified=False,
            execution_authorization_blocking_count=0,
            execution_authorization_warning_count=0,
            execution_authorization_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )
    paths = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:500]:
        activation_id = path.stem
        try:
            summary = evaluate_production_execution_authorization(
                activation_request_id=activation_id,
                governed_cutover_store_dir=governed_cutover_store_dir,
                window_store_dir=window_store_dir,
                permission_store_dir=permission_store_dir,
                session_store_dir=session_store_dir,
                boundary_store_dir=boundary_store_dir,
                invocation_store_dir=invocation_store_dir,
                authorization_store_dir=authorization_store_dir,
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
        if not summary.governed_cutover_contract_valid and not summary.authorization_present:
            continue
        return ProductionExecutionAuthorizationDashboardDigest(
            execution_authorization_state=summary.authorization_state,
            execution_authorization_ready=summary.authorization_ready,
            execution_authorization_present=summary.authorization_present,
            execution_authorization_expired=(
                summary.authorization_state == AUTHORIZATION_EXPIRED
            ),
            execution_authorization_id=summary.authorization_id,
            execution_authorization_expires_at=summary.expires_at,
            execution_authorization_phrase_verified=summary.execution_phrase_verified,
            execution_authorization_blocking_count=len(summary.blocking_items),
            execution_authorization_warning_count=len(summary.warning_items),
            execution_authorization_recommended_action=summary.recommended_action,
        )
    return ProductionExecutionAuthorizationDashboardDigest(
        execution_authorization_state="not_configured",
        execution_authorization_ready=False,
        execution_authorization_present=False,
        execution_authorization_expired=False,
        execution_authorization_id="",
        execution_authorization_expires_at="",
        execution_authorization_phrase_verified=False,
        execution_authorization_blocking_count=0,
        execution_authorization_warning_count=0,
        execution_authorization_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
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
        "signer_present: true",
        "signer_present: false",
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
        "execution_phrase_required: true",
        "execution_phrase_required: false",
        "execution_phrase_verified: true",
        "execution_phrase_verified: false",
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
        "signer_present",
        "operator_identity_valid",
        "executor_identity_valid",
        "signer_identity_valid",
        "identity_separation_valid",
        "original_repository2_not_executed",
        "original_repository2_execution_attempted",
        "runtime_permission",
        "permission_executor_mismatch",
        "permission_not_consumed",
        "session_executor_mismatch",
        "execution_phrase_required",
        "execution_phrase_verified",
        "execution_authorization_phrase_verified",
        "runtime_boundary_executor_mismatch",
        "runtime_invocation_executor_mismatch",
        "boundary_invocation_id",
        "boundary_runtime_invoked",
        "boundary_cutover_started",
        "invocation_runtime_invoked",
        "invocation_cutover_started",
    ):
        lowered = lowered.replace(label, "")
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionExecutionAuthorizationError(
                f"Unsafe execution authorization output field: {token!r}"
            )


def format_production_execution_authorization_status(
    summary: ProductionExecutionAuthorizationSummary,
) -> str:
    lines = [
        "Production Execution Authorization Status",
        "",
        f"activation_request_id: {summary.activation_request_id}",
        f"cutover_contract_id: {summary.cutover_contract_id or '(none)'}",
        f"permission_id: {summary.permission_id or '(none)'}",
        f"session_id: {summary.session_id or '(none)'}",
        f"boundary_id: {summary.boundary_id or '(none)'}",
        f"boundary_invocation_id: {summary.boundary_invocation_id or '(none)'}",
        f"runtime_invocation_id: {summary.runtime_invocation_id or '(none)'}",
        f"authorization_id: {summary.authorization_id or '(none)'}",
        f"authorization_state: {summary.authorization_state}",
        f"authorization_ready: {str(summary.authorization_ready).lower()}",
        f"authorization_present: {str(summary.authorization_present).lower()}",
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
        f"final_signoff_valid: {str(summary.final_signoff_valid).lower()}",
        f"rollback_ready: {str(summary.rollback_ready).lower()}",
        "operational_signoff_valid: "
        f"{str(summary.operational_signoff_valid).lower()}",
        f"audit_chain_complete: {str(summary.audit_chain_complete).lower()}",
        f"recovery_required: {str(summary.recovery_required).lower()}",
        f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
        f"executor_identity_valid: {str(summary.executor_identity_valid).lower()}",
        f"operator_identity_valid: {str(summary.operator_identity_valid).lower()}",
        f"signer_identity_valid: {str(summary.signer_identity_valid).lower()}",
        "identity_separation_valid: "
        f"{str(summary.identity_separation_valid).lower()}",
        f"one_shot_scope_valid: {str(summary.one_shot_scope_valid).lower()}",
        f"ticket_scope_valid: {str(summary.ticket_scope_valid).lower()}",
        f"window_scope_valid: {str(summary.window_scope_valid).lower()}",
        f"authorization_ttl_valid: {str(summary.authorization_ttl_valid).lower()}",
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
        f"authorized_at: {summary.authorized_at or '(none)'}",
        f"expires_at: {summary.expires_at or '(none)'}",
        f"authorization_issued: {str(summary.authorization_issued).lower()}",
        "execution_phrase_required: true",
        "execution_phrase_verified: "
        f"{str(summary.execution_phrase_verified).lower()}",
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
        f"signer_present: {str(summary.signer_present).lower()}",
        f"tested_commit_sha: {summary.tested_commit_sha_short or '(none)'}",
        f"release_tag: {summary.release_tag or '(none)'}",
        f"already_authorized: {str(summary.already_authorized).lower()}",
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
        "boundary_runtime_invoked: false",
        "boundary_cutover_started: false",
        "invocation_runtime_invoked: false",
        "invocation_cutover_started: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_execution_authorization_record(
    record: ProductionExecutionAuthorizationRecord,
    *,
    now: datetime | None = None,
) -> str:
    expires_dt = _parse_iso(record.expires_at)
    expired = bool(expires_dt and _utc_now(now) >= expires_dt)
    state = AUTHORIZATION_EXPIRED if expired else AUTHORIZATION_ISSUED
    lines = [
        "Production Execution Authorization",
        "",
        f"authorization_id: {record.authorization_id}",
        f"activation_request_id: {record.activation_request_id}",
        f"boundary_id: {record.boundary_id}",
        f"boundary_invocation_id: {record.boundary_invocation_id}",
        f"cutover_contract_id: {record.cutover_contract_id}",
        f"permission_id: {record.permission_id}",
        f"session_id: {record.session_id}",
        f"runtime_invocation_id: {record.runtime_invocation_id}",
        f"reservation_id: {record.reservation_id}",
        f"execution_attempt_id: {record.execution_attempt_id}",
        f"dispatch_run_id: {record.dispatch_run_id}",
        f"authorization_status: {state}",
        f"ttl_seconds: {record.ttl_seconds}",
        f"authorized_at: {record.authorized_at}",
        f"expires_at: {record.expires_at}",
        "execution_phrase_required: true",
        "execution_phrase_verified: "
        f"{str(record.execution_phrase_verified).lower()}",
        "permission_consumed: false",
        "permission_revoked: false",
        "executor_assigned: true",
        "operator_present: true",
        "signer_present: true",
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


def format_production_execution_authorization_history(
    activation_request_id: str,
    events: tuple[ProductionExecutionAuthorizationEvent, ...],
    *,
    authorization_id: str = "",
) -> str:
    lines = [
        "Production Execution Authorization History",
        "",
        f"activation_request_id: {activation_request_id}",
        f"authorization_id: {authorization_id or '(none)'}",
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


def run_production_execution_authorization_status(
    *,
    activation_request_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_execution_authorization(
            activation_request_id=activation_request_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionExecutionAuthorizationError:
        return "error: execution authorization status unavailable", 1
    return format_production_execution_authorization_status(summary), 0


def run_production_execution_authorization_check(
    *,
    activation_request_id: str,
    runtime_invocation_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
    ttl_seconds: int = 15,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_execution_authorization(
            activation_request_id=activation_request_id,
            runtime_invocation_id=runtime_invocation_id,
            ttl_seconds=ttl_seconds,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionExecutionAuthorizationError:
        return "error: execution authorization check unavailable", 1
    exit_code = 0 if summary.authorization_state == AUTHORIZATION_READY else 1
    return format_production_execution_authorization_status(summary), exit_code


def run_production_execution_authorization_authorize(
    *,
    activation_request_id: str,
    runtime_invocation_id: str,
    executor_id: str,
    operator_id: str,
    signer_id: str,
    ttl_seconds: int,
    phrase: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = authorize_production_execution_authorization(
            activation_request_id=activation_request_id,
            runtime_invocation_id=runtime_invocation_id,
            executor_id=executor_id,
            operator_id=operator_id,
            signer_id=signer_id,
            ttl_seconds=ttl_seconds,
            phrase=phrase,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionExecutionAuthorizationError:
        try:
            summary = evaluate_production_execution_authorization(
                activation_request_id=activation_request_id,
                runtime_invocation_id=runtime_invocation_id,
                executor_id=executor_id,
                operator_id=operator_id,
                signer_id=signer_id,
                ttl_seconds=ttl_seconds,
                merged_config=merged_config,
                repo_root=repo_root,
            )
            return format_production_execution_authorization_status(summary), 1
        except ProductionExecutionAuthorizationError:
            return "error: execution authorization authorize failed", 1
    exit_code = (
        0
        if summary.authorization_state == AUTHORIZATION_ISSUED
        or summary.already_authorized
        else 1
    )
    return format_production_execution_authorization_status(summary), exit_code


def run_production_execution_authorization_show(
    *,
    authorization_id: str,
) -> tuple[str, int]:
    try:
        record = load_execution_authorization_by_id(authorization_id)
    except ProductionExecutionAuthorizationError:
        return "error: execution authorization corrupted", 1
    if record is None:
        return "error: execution authorization not found", 1
    return format_production_execution_authorization_record(record), 0


def run_production_execution_authorization_history(
    *,
    activation_request_id: str,
) -> tuple[str, int]:
    try:
        record = load_execution_authorization_record(activation_request_id)
        events = load_execution_authorization_events(activation_request_id)
    except ProductionExecutionAuthorizationError:
        return "error: execution authorization history unavailable", 1
    return (
        format_production_execution_authorization_history(
            activation_request_id,
            events,
            authorization_id=record.authorization_id if record else "",
        ),
        0,
    )
