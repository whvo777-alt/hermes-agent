"""Governed runtime closure & consistency validation — Phase 15J.

Read-only cross-validation of the entire Phase 15A-15I governed runtime
chain (cutover contract -> controlled window -> runtime permission ->
governed runtime session -> runtime boundary -> runtime invocation
reservation -> execution authorization -> runtime start -> governed
runtime invoke) plus the four Phase 15I consume artifacts. Detects
partial consume, replay, correlation mismatch, and consume-order
violations, and — only on a fully consistent chain — writes a single
append-only closure artifact.

This module never mutates any Phase 15A-15I artifact, never mutates any
consume record, never creates a bounded subprocess runner, and never
calls subprocess/node/npm/npx/pipeline.js. It never touches Gateway,
Discord, or external publish. ``governed_runtime_invoked`` remains
strictly bookkeeping-only here as it was in Phase 15I — this module does
not, and cannot, cause any actual OS-level runtime execution.

Invariants enforced everywhere in this module:
    - production_execution_allowed is always False in every output.
    - production_root_hard_deny is always True in every output.
    - original_repository2_execution_attempted is always False.
    - gateway_production_enabled / discord_production_enabled /
      external_publish_enabled are always False.
    - governed_runtime_invoked, phase14_runtime_invoked,
      isolated_mirror_runtime_invoked, and runtime_started are always
      kept as four distinct fields — never collapsed into one ambiguous
      "runtime_invoked" key anywhere in this module's output.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.production_activation_execution_reservation import (
    load_execution_reservation,
)
from agent.coo.production_activation_live_runtime import (
    load_runtime_records as load_live_runtime_records,
)
from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_controlled_window import (
    WINDOW_CLOSED,
    WINDOW_EMERGENCY_CLOSED,
    WINDOW_OPEN,
    evaluate_production_controlled_window,
)
from agent.coo.production_execution_authorization import (
    ProductionExecutionAuthorizationError,
    load_execution_authorization_consume_record,
    load_execution_authorization_record,
)
from agent.coo.production_governed_cutover import load_governed_cutover_contract
from agent.coo.production_governed_runtime_invoke import (
    BLOCK_ALREADY_INVOKED,
    GovernedRuntimeInvokeError,
    evaluate_governed_runtime_invoke,
    load_governed_runtime_invoke_record,
)
from agent.coo.production_governed_runtime_session import (
    load_governed_runtime_session_record,
)
from agent.coo.production_runtime_boundary import (
    RuntimeBoundaryError,
    load_runtime_boundary_consume_record,
    load_runtime_boundary_record,
)
from agent.coo.production_runtime_consume_store import (
    OneShotConsumeWriteConflict,
    read_consume_record,
    write_once_consume_record,
)
from agent.coo.production_runtime_invocation import (
    ProductionRuntimeInvocationError,
    load_runtime_invocation_consume_record,
    load_runtime_invocation_record,
)
from agent.coo.production_runtime_permission import (
    ProductionRuntimePermissionError,
    load_runtime_permission_consume_record,
    load_runtime_permission_record,
)
from agent.coo.production_runtime_start import (
    ProductionRuntimeStartError,
    evaluate_production_runtime_start,
    load_runtime_start_record,
)
from hermes_constants import get_hermes_home

_CLOSURE_STORE_DIR = "production-governed-runtime-closure"

CLOSURE_NOT_READY = "CLOSURE_NOT_READY"
CLOSURE_READY = "CLOSURE_READY"
CLOSURE_COMPLETED = "CLOSURE_COMPLETED"
CLOSURE_REQUIRES_RECOVERY = "CLOSURE_REQUIRES_RECOVERY"
CLOSURE_BLOCKED = "CLOSURE_BLOCKED"
CLOSURE_CORRUPTED = "CLOSURE_CORRUPTED"

# -- Blocking codes (Section 19) --------------------------------------------
BLOCK_GOVERNED_CUTOVER_CONTRACT_MISSING = "governed_cutover_contract_missing"
BLOCK_CONTROLLED_WINDOW_INVALID = "controlled_window_invalid"
BLOCK_RUNTIME_PERMISSION_MISSING = "runtime_permission_missing"
BLOCK_RUNTIME_SESSION_MISSING = "runtime_session_missing"
BLOCK_RUNTIME_BOUNDARY_MISSING = "runtime_boundary_missing"
BLOCK_RUNTIME_INVOCATION_MISSING = "runtime_invocation_missing"
BLOCK_EXECUTION_AUTHORIZATION_MISSING = "execution_authorization_missing"
BLOCK_RUNTIME_START_MISSING = "runtime_start_missing"
BLOCK_GOVERNED_RUNTIME_INVOKE_MISSING = "governed_runtime_invoke_missing"
BLOCK_PERMISSION_CONSUME_MISSING = "permission_consume_missing"
BLOCK_BOUNDARY_CONSUME_MISSING = "boundary_consume_missing"
BLOCK_INVOCATION_CONSUME_MISSING = "invocation_consume_missing"
BLOCK_AUTHORIZATION_CONSUME_MISSING = "authorization_consume_missing"
BLOCK_PARTIAL_CONSUME_DETECTED = "partial_consume_detected"
BLOCK_CONSUME_ORDER_INVALID = "consume_order_invalid"
BLOCK_CONSUME_CORRELATION_MISMATCH = "consume_correlation_mismatch"
BLOCK_CONSUME_REPLAY_DETECTED = "consume_replay_detected"
BLOCK_CORRELATION_INVALID = "correlation_invalid"
BLOCK_FINAL_SIGNOFF_INVALID = "final_signoff_invalid"
BLOCK_ROLLBACK_VALIDATION_INVALID = "rollback_validation_invalid"
BLOCK_OPERATIONAL_SIGNOFF_INVALID = "operational_signoff_invalid"
BLOCK_ACTIVATION_INVALID = "activation_invalid"
BLOCK_RESERVATION_INVALID = "reservation_invalid"
BLOCK_EVIDENCE_MISSING = "evidence_missing"
BLOCK_DISPATCH_AUDIT_MISSING = "dispatch_audit_missing"
BLOCK_E2E_INCOMPLETE = "e2e_incomplete"
BLOCK_RECOVERY_REQUIRED = "recovery_required"
BLOCK_REPAIR_LOCK_HELD = "repair_lock_held"
BLOCK_ORIGINAL_REPOSITORY2_EXECUTION_DETECTED = "original_repository2_execution_detected"
BLOCK_PRODUCTION_EXECUTION_ENABLED = "production_execution_enabled"
BLOCK_EXTERNAL_PUBLISH_ENABLED = "external_publish_enabled"
BLOCK_GATEWAY_PRODUCTION_ENABLED = "gateway_production_enabled"
BLOCK_DISCORD_PRODUCTION_ENABLED = "discord_production_enabled"
BLOCK_CLOSURE_STORE_CORRUPTED = "closure_store_corrupted"
BLOCK_CLOSURE_CONFLICT = "closure_conflict"
BLOCK_CLOSURE_WRITE_FAILED = "closure_write_failed"
BLOCK_UNSAFE_OUTPUT = "unsafe_output"

# -- Warning codes (Section 20) ----------------------------------------------
WARN_SESSION_CLOSE_PENDING = "session_close_pending"
WARN_INVOCATION_COMPLETION_PENDING = "invocation_completion_pending"
WARN_WINDOW_CLOSE_REQUIRED = "window_close_required"
WARN_ISOLATED_MIRROR_RUNTIME_WAS_SEPARATE = "isolated_mirror_runtime_was_separate"
WARN_GOVERNED_RUNTIME_INVOKE_IS_BOOKKEEPING_ONLY = "governed_runtime_invoke_is_bookkeeping_only"
WARN_PRODUCTION_EXECUTION_DISABLED = "production_execution_disabled"
WARN_PRODUCTION_ROOT_HARD_DENIED = "production_root_hard_denied"
WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED = "original_repository2_not_executed"
WARN_EXTERNAL_PUBLISH_DISABLED = "external_publish_disabled"
WARN_GATEWAY_PRODUCTION_DISABLED = "gateway_production_disabled"
WARN_DISCORD_PRODUCTION_DISABLED = "discord_production_disabled"
WARN_MANUAL_OPERATOR_CLOSURE_REQUIRED = "manual_operator_closure_required"
WARN_EMERGENCY_CLOSED_WINDOW = "emergency_closed_window"

# -- Recommended actions (Section 21) ----------------------------------------
ACTION_RECORD_GOVERNED_RUNTIME_CLOSURE = "record_governed_runtime_closure"
ACTION_GOVERNED_RUNTIME_CLOSURE_COMPLETED = "governed_runtime_closure_completed"
ACTION_INSPECT_PARTIAL_GOVERNED_CONSUME = "inspect_partial_governed_consume"
ACTION_RESOLVE_GOVERNED_RUNTIME_REPLAY = "resolve_governed_runtime_replay"
ACTION_RESOLVE_RUNTIME_CHAIN_CORRELATION = "resolve_runtime_chain_correlation"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_CLOSE_CONTROLLED_PRODUCTION_WINDOW = "close_controlled_production_window"
ACTION_CLOSE_GOVERNED_RUNTIME_SESSION = "close_governed_runtime_session"
ACTION_COMPLETE_INVOCATION_LIFECYCLE = "complete_invocation_lifecycle"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_PREPARE_PHASE_16_V1_RELEASE_CANDIDATE_VALIDATION = (
    "prepare_phase_16_v1_release_candidate_validation"
)
ACTION_CREATE_NEW_GOVERNED_CUTOVER_CHAIN = "create_new_governed_cutover_chain"
ACTION_EMERGENCY_CLOSE_WINDOW = "emergency_close_window"

_CONSUME_ORDER = ("permission", "boundary", "invocation", "authorization")


class ProductionGovernedRuntimeClosureError(ValueError):
    """Raised when closure evaluation/recording cannot proceed safely."""


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _short_sha(value: str, limit: int = 12) -> str:
    return (value or "").strip()[:limit]


def default_closure_store_dir() -> Path:
    return get_hermes_home() / "coo" / _CLOSURE_STORE_DIR


def _closure_path(activation_request_id: str, *, store_dir: Path | None = None) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionGovernedRuntimeClosureError("activation_request_id is required")
    base = store_dir or default_closure_store_dir()
    return base / f"{normalized}.json"


@dataclass(frozen=True)
class ProductionGovernedRuntimeClosureRecord:
    """Immutable, append-only terminal closure artifact for one activation."""

    closure_id: str
    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    boundary_id: str
    runtime_invocation_id: str
    authorization_id: str
    runtime_start_id: str
    governed_runtime_invoke_id: str
    permission_consume_record_id: str
    boundary_consume_record_id: str
    invocation_consume_record_id: str
    authorization_consume_record_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    ticket_id: str
    confirmation_id: str
    closure_status: str
    correlation_valid: bool
    consume_chain_complete: bool
    governed_runtime_invoked: bool
    runtime_started: bool
    completed_at: str
    tested_commit_sha: str
    release_tag: str
    warning_codes: tuple[str, ...]
    blocking_codes: tuple[str, ...]
    original_repository2_execution_attempted: bool = False
    production_execution_allowed: bool = False
    production_root_hard_deny: bool = True
    external_publish_enabled: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "closure_id": self.closure_id,
            "activation_request_id": self.activation_request_id,
            "cutover_contract_id": self.cutover_contract_id,
            "permission_id": self.permission_id,
            "session_id": self.session_id,
            "boundary_id": self.boundary_id,
            "runtime_invocation_id": self.runtime_invocation_id,
            "authorization_id": self.authorization_id,
            "runtime_start_id": self.runtime_start_id,
            "governed_runtime_invoke_id": self.governed_runtime_invoke_id,
            "permission_consume_record_id": self.permission_consume_record_id,
            "boundary_consume_record_id": self.boundary_consume_record_id,
            "invocation_consume_record_id": self.invocation_consume_record_id,
            "authorization_consume_record_id": self.authorization_consume_record_id,
            "reservation_id": self.reservation_id,
            "execution_attempt_id": self.execution_attempt_id,
            "dispatch_run_id": self.dispatch_run_id,
            "ticket_id": self.ticket_id,
            "confirmation_id": self.confirmation_id,
            "closure_status": self.closure_status,
            "correlation_valid": self.correlation_valid,
            "consume_chain_complete": self.consume_chain_complete,
            "governed_runtime_invoked": self.governed_runtime_invoked,
            "runtime_started": self.runtime_started,
            "original_repository2_execution_attempted": False,
            "production_execution_allowed": False,
            "production_root_hard_deny": True,
            "external_publish_enabled": False,
            "gateway_production_enabled": False,
            "discord_production_enabled": False,
            "completed_at": self.completed_at,
            "tested_commit_sha": self.tested_commit_sha,
            "release_tag": self.release_tag,
            "warning_codes": list(self.warning_codes),
            "blocking_codes": list(self.blocking_codes),
        }


def _record_from_dict(payload: Mapping[str, Any]) -> ProductionGovernedRuntimeClosureRecord:
    return ProductionGovernedRuntimeClosureRecord(
        closure_id=str(payload.get("closure_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        cutover_contract_id=str(payload.get("cutover_contract_id", "")),
        permission_id=str(payload.get("permission_id", "")),
        session_id=str(payload.get("session_id", "")),
        boundary_id=str(payload.get("boundary_id", "")),
        runtime_invocation_id=str(payload.get("runtime_invocation_id", "")),
        authorization_id=str(payload.get("authorization_id", "")),
        runtime_start_id=str(payload.get("runtime_start_id", "")),
        governed_runtime_invoke_id=str(payload.get("governed_runtime_invoke_id", "")),
        permission_consume_record_id=str(payload.get("permission_consume_record_id", "")),
        boundary_consume_record_id=str(payload.get("boundary_consume_record_id", "")),
        invocation_consume_record_id=str(payload.get("invocation_consume_record_id", "")),
        authorization_consume_record_id=str(
            payload.get("authorization_consume_record_id", "")
        ),
        reservation_id=str(payload.get("reservation_id", "")),
        execution_attempt_id=str(payload.get("execution_attempt_id", "")),
        dispatch_run_id=str(payload.get("dispatch_run_id", "")),
        ticket_id=str(payload.get("ticket_id", "")),
        confirmation_id=str(payload.get("confirmation_id", "")),
        closure_status=str(payload.get("closure_status", "")),
        correlation_valid=bool(payload.get("correlation_valid", False)),
        consume_chain_complete=bool(payload.get("consume_chain_complete", False)),
        governed_runtime_invoked=bool(payload.get("governed_runtime_invoked", False)),
        runtime_started=bool(payload.get("runtime_started", False)),
        completed_at=str(payload.get("completed_at", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        warning_codes=tuple(payload.get("warning_codes") or ()),
        blocking_codes=tuple(payload.get("blocking_codes") or ()),
    )


def load_production_governed_runtime_closure(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionGovernedRuntimeClosureRecord | None:
    """Load the closure artifact for an activation, or None if absent.

    Raises ProductionGovernedRuntimeClosureError (fail-closed) if the
    stored artifact exists but is corrupted.
    """
    path = _closure_path(activation_request_id, store_dir=store_dir)
    try:
        payload = read_consume_record(path)
    except ValueError as exc:
        raise ProductionGovernedRuntimeClosureError(
            BLOCK_CLOSURE_STORE_CORRUPTED
        ) from exc
    if payload is None:
        return None
    return _record_from_dict(payload)


@dataclass(frozen=True)
class ProductionGovernedRuntimeClosureSummary:
    """Read-only closure readiness assessment."""

    activation_request_id: str
    cutover_contract_id: str
    permission_id: str
    session_id: str
    boundary_id: str
    runtime_invocation_id: str
    authorization_id: str
    runtime_start_id: str
    governed_runtime_invoke_id: str
    closure_id: str
    closure_state: str
    closure_ready: bool
    closure_present: bool
    chain_complete: bool
    correlation_valid: bool
    consume_chain_complete: bool
    partial_consume_detected: bool
    replay_detected: bool
    permission_consumed: bool
    boundary_consumed: bool
    invocation_consumed: bool
    authorization_consumed: bool
    runtime_started: bool
    governed_runtime_invoked: bool
    phase14_runtime_invoked: bool
    isolated_mirror_runtime_invoked: bool
    original_repository2_execution_attempted: bool
    session_close_ready: bool
    boundary_consumed_state_valid: bool
    invocation_completion_ready: bool
    authorization_consumed_state_valid: bool
    window_close_required: bool
    window_state: str
    recovery_required: bool
    repair_lock_held: bool
    audit_chain_complete: bool
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str
    production_execution_allowed: bool = False
    production_root_hard_deny: bool = True
    external_publish_enabled: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    tested_commit_sha: str = ""
    release_tag: str = ""


def _consume_payload_get(payload: Mapping[str, Any] | None, key: str, default: str = "") -> str:
    if payload is None:
        return default
    return str(payload.get(key, default) or default)


def evaluate_production_governed_runtime_closure(
    *,
    activation_request_id: str,
    authorization_id: str = "",
    executor_id: str = "",
    operator_id: str = "",
    supervisor_id: str = "",
    closure_store_dir: Path | None = None,
    invoke_store_dir: Path | None = None,
    runtime_start_store_dir: Path | None = None,
    authorization_store_dir: Path | None = None,
    authorization_consume_store_dir: Path | None = None,
    invocation_store_dir: Path | None = None,
    invocation_consume_store_dir: Path | None = None,
    boundary_store_dir: Path | None = None,
    boundary_consume_store_dir: Path | None = None,
    session_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
    permission_consume_store_dir: Path | None = None,
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
) -> ProductionGovernedRuntimeClosureSummary:
    """Read-only cross-validation of the full Phase 15A-15I governed chain."""
    blocking: list[str] = []
    warnings: list[str] = []

    # -- 1. Reuse Phase 15I's own readiness evaluation (which itself reuses
    # Phase 15H's, which reuses 15A-15G's) rather than re-implementing the
    # full upstream chain validation here. -----------------------------------
    try:
        invoke_summary = evaluate_governed_runtime_invoke(
            activation_request_id=activation_request_id,
            authorization_id=authorization_id,
            executor_id=executor_id,
            operator_id=operator_id,
            supervisor_id=supervisor_id,
            invoke_store_dir=invoke_store_dir,
            runtime_start_store_dir=runtime_start_store_dir,
            authorization_store_dir=authorization_store_dir,
            authorization_consume_store_dir=authorization_consume_store_dir,
            invocation_store_dir=invocation_store_dir,
            invocation_consume_store_dir=invocation_consume_store_dir,
            boundary_store_dir=boundary_store_dir,
            boundary_consume_store_dir=boundary_consume_store_dir,
            session_store_dir=session_store_dir,
            permission_store_dir=permission_store_dir,
            permission_consume_store_dir=permission_consume_store_dir,
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
    except Exception:
        invoke_summary = None

    # already_invoked=True is the EXPECTED healthy state for closure (Phase
    # 15I already completed) — only its OWN BLOCK_ALREADY_INVOKED marker is
    # acceptable; anything else means the upstream chain regressed.
    invoke_ok = (
        invoke_summary is not None
        and invoke_summary.already_invoked
        and set(invoke_summary.blocking_items) <= {BLOCK_ALREADY_INVOKED}
    )
    if invoke_summary is None or not invoke_summary.already_invoked:
        blocking.append(BLOCK_GOVERNED_RUNTIME_INVOKE_MISSING)
    elif not invoke_ok:
        blocking.append(BLOCK_CORRELATION_INVALID)

    invoke_record = load_governed_runtime_invoke_record(
        activation_request_id, store_dir=invoke_store_dir
    )
    if invoke_record is None:
        if BLOCK_GOVERNED_RUNTIME_INVOKE_MISSING not in blocking:
            blocking.append(BLOCK_GOVERNED_RUNTIME_INVOKE_MISSING)

    runtime_start_record = load_runtime_start_record(
        activation_request_id, store_dir=runtime_start_store_dir
    )
    if runtime_start_record is None:
        blocking.append(BLOCK_RUNTIME_START_MISSING)

    # -- 2. Upstream chain presence (mostly already re-validated above via
    # evaluate_governed_runtime_invoke -> evaluate_production_runtime_start,
    # but re-checked directly here for a precise closure-owned blocking
    # vocabulary and to source concrete IDs). --------------------------------
    contract = load_governed_cutover_contract(
        activation_request_id, store_dir=governed_cutover_store_dir
    )
    if contract is None:
        blocking.append(BLOCK_GOVERNED_CUTOVER_CONTRACT_MISSING)

    try:
        window_summary = evaluate_production_controlled_window(
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
            now=now,
        )
    except Exception:
        window_summary = None
        blocking.append(BLOCK_CONTROLLED_WINDOW_INVALID)

    permission_record = load_runtime_permission_record(
        activation_request_id, store_dir=permission_store_dir
    )
    if permission_record is None:
        blocking.append(BLOCK_RUNTIME_PERMISSION_MISSING)

    session_record = load_governed_runtime_session_record(
        activation_request_id, store_dir=session_store_dir
    )
    if session_record is None:
        blocking.append(BLOCK_RUNTIME_SESSION_MISSING)

    boundary_record = load_runtime_boundary_record(
        activation_request_id, store_dir=boundary_store_dir
    )
    if boundary_record is None:
        blocking.append(BLOCK_RUNTIME_BOUNDARY_MISSING)

    invocation_record = load_runtime_invocation_record(
        activation_request_id, store_dir=invocation_store_dir
    )
    if invocation_record is None:
        blocking.append(BLOCK_RUNTIME_INVOCATION_MISSING)

    authorization_record = load_execution_authorization_record(
        activation_request_id, store_dir=authorization_store_dir
    )
    if authorization_record is None:
        blocking.append(BLOCK_EXECUTION_AUTHORIZATION_MISSING)

    activation_request = load_activation_request(activation_request_id, store_dir=store_dir)
    if activation_request is None:
        blocking.append(BLOCK_ACTIVATION_INVALID)

    reservation = load_execution_reservation(activation_request_id, store_dir=reservation_dir)
    if reservation is None:
        blocking.append(BLOCK_RESERVATION_INVALID)

    # -- 3. Consume chain: load all four consume records directly. -----------
    permission_consume = None
    boundary_consume = None
    invocation_consume = None
    authorization_consume = None
    try:
        if permission_record is not None:
            permission_consume = load_runtime_permission_consume_record(
                permission_record.permission_id, store_dir=permission_consume_store_dir
            )
    except ProductionRuntimePermissionError:
        blocking.append(BLOCK_CLOSURE_STORE_CORRUPTED)
    try:
        if boundary_record is not None:
            boundary_consume = load_runtime_boundary_consume_record(
                boundary_record.boundary_id, store_dir=boundary_consume_store_dir
            )
    except RuntimeBoundaryError:
        blocking.append(BLOCK_CLOSURE_STORE_CORRUPTED)
    try:
        if invocation_record is not None:
            invocation_consume = load_runtime_invocation_consume_record(
                invocation_record.runtime_invocation_id,
                store_dir=invocation_consume_store_dir,
            )
    except ProductionRuntimeInvocationError:
        blocking.append(BLOCK_CLOSURE_STORE_CORRUPTED)
    try:
        if authorization_record is not None:
            authorization_consume = load_execution_authorization_consume_record(
                authorization_record.authorization_id,
                store_dir=authorization_consume_store_dir,
            )
    except ProductionExecutionAuthorizationError:
        blocking.append(BLOCK_CLOSURE_STORE_CORRUPTED)

    consume_presence = {
        "permission": permission_consume,
        "boundary": boundary_consume,
        "invocation": invocation_consume,
        "authorization": authorization_consume,
    }
    present_count = sum(1 for v in consume_presence.values() if v is not None)
    consume_chain_complete = present_count == 4
    partial_consume_detected = 0 < present_count < 4

    if permission_consume is None:
        blocking.append(BLOCK_PERMISSION_CONSUME_MISSING)
    if boundary_consume is None:
        blocking.append(BLOCK_BOUNDARY_CONSUME_MISSING)
    if invocation_consume is None:
        blocking.append(BLOCK_INVOCATION_CONSUME_MISSING)
    if authorization_consume is None:
        blocking.append(BLOCK_AUTHORIZATION_CONSUME_MISSING)
    if partial_consume_detected:
        blocking.append(BLOCK_PARTIAL_CONSUME_DETECTED)

    # -- 4. Consume order / timestamp validation (only meaningful once all
    # four are present). ------------------------------------------------------
    consume_order_invalid = False
    if consume_chain_complete:
        timestamps = []
        for key in _CONSUME_ORDER:
            parsed = _parse_iso(_consume_payload_get(consume_presence[key], "consumed_at"))
            timestamps.append(parsed)
        if any(t is None for t in timestamps):
            consume_order_invalid = True
        else:
            for earlier, later in zip(timestamps, timestamps[1:]):
                if later < earlier:  # strictly earlier than its predecessor
                    consume_order_invalid = True
                    break
        if consume_order_invalid:
            blocking.append(BLOCK_CONSUME_ORDER_INVALID)

    # -- 5. Correlation validation across consume records, upstream records,
    # and runtime_start_record (the chain's own aggregation point). ----------
    correlation_valid = True
    if runtime_start_record is not None:
        for key, payload in consume_presence.items():
            if payload is None:
                continue
            if (
                _consume_payload_get(payload, "activation_request_id")
                != activation_request_id
            ):
                correlation_valid = False
            if (
                _consume_payload_get(payload, "cutover_contract_id")
                and _consume_payload_get(payload, "cutover_contract_id")
                != runtime_start_record.cutover_contract_id
            ):
                correlation_valid = False
            if (
                _consume_payload_get(payload, "ticket_id")
                and _consume_payload_get(payload, "ticket_id")
                != runtime_start_record.ticket_id
            ):
                correlation_valid = False
            if (
                _consume_payload_get(payload, "confirmation_id")
                and _consume_payload_get(payload, "confirmation_id")
                != runtime_start_record.confirmation_id
            ):
                correlation_valid = False
        if (
            permission_record is not None
            and permission_record.permission_id != runtime_start_record.permission_id
        ):
            correlation_valid = False
        if (
            boundary_record is not None
            and boundary_record.boundary_id != runtime_start_record.boundary_id
        ):
            correlation_valid = False
        if (
            invocation_record is not None
            and invocation_record.runtime_invocation_id
            != runtime_start_record.runtime_invocation_id
        ):
            correlation_valid = False
        if (
            authorization_record is not None
            and authorization_record.authorization_id
            != runtime_start_record.authorization_id
        ):
            correlation_valid = False
    if not correlation_valid:
        blocking.append(BLOCK_CORRELATION_INVALID)

    # -- 6. Replay detection: each consume record's governed_invoke_id (when
    # populated) must match the activation's single governed invoke record. --
    replay_detected = False
    if invoke_record is not None:
        for payload in consume_presence.values():
            if payload is None:
                continue
            recorded_invoke_id = _consume_payload_get(payload, "governed_invoke_id")
            if recorded_invoke_id and recorded_invoke_id != invoke_record.invoke_id:
                replay_detected = True
    if replay_detected:
        blocking.append(BLOCK_CONSUME_REPLAY_DETECTED)

    # -- 7. Runtime meaning separation (Phase 14 vs Phase 15 bookkeeping). ----
    live_runtime_records = load_live_runtime_records(
        activation_request_id, history_dir=runtime_history_dir
    )
    phase14_signal = any(r.isolated_mirror_runtime_invoked for r in live_runtime_records)
    governed_runtime_invoked = bool(
        invoke_record is not None and invoke_record.governed_runtime_invoked
    )

    # -- 8. Window closure policy. ---------------------------------------------
    window_state = window_summary.window_state if window_summary is not None else ""
    window_close_required = window_state == WINDOW_OPEN
    if window_close_required:
        warnings.append(WARN_WINDOW_CLOSE_REQUIRED)
    if window_state == WINDOW_EMERGENCY_CLOSED:
        warnings.append(WARN_EMERGENCY_CLOSED_WINDOW)

    recovery_required = bool(window_summary is not None and window_summary.recovery_required)
    repair_lock_held = bool(window_summary is not None and window_summary.repair_lock_held)
    if recovery_required:
        blocking.append(BLOCK_RECOVERY_REQUIRED)
    if repair_lock_held:
        blocking.append(BLOCK_REPAIR_LOCK_HELD)

    # -- 9. Production safety flags (fail-closed; force_* is test-only). -----
    production_execution_allowed = bool(force_production_execution_allowed)
    gateway_enabled = bool(force_gateway_enabled)
    discord_enabled = bool(force_discord_enabled)
    if production_execution_allowed:
        blocking.append(BLOCK_PRODUCTION_EXECUTION_ENABLED)
    if gateway_enabled:
        blocking.append(BLOCK_GATEWAY_PRODUCTION_ENABLED)
    if discord_enabled:
        blocking.append(BLOCK_DISCORD_PRODUCTION_ENABLED)

    # -- 10. Existing closure artifact check (idempotency). -------------------
    existing_closure = load_production_governed_runtime_closure(
        activation_request_id, store_dir=closure_store_dir
    )
    closure_present = existing_closure is not None

    session_close_ready = session_record is not None
    boundary_consumed = boundary_consume is not None
    invocation_consumed = invocation_consume is not None
    authorization_consumed = authorization_consume is not None
    permission_consumed = permission_consume is not None

    if not closure_present:
        warnings.append(WARN_SESSION_CLOSE_PENDING)
        if invocation_consumed:
            warnings.append(WARN_INVOCATION_COMPLETION_PENDING)
        warnings.append(WARN_ISOLATED_MIRROR_RUNTIME_WAS_SEPARATE)
        warnings.append(WARN_GOVERNED_RUNTIME_INVOKE_IS_BOOKKEEPING_ONLY)
        warnings.append(WARN_PRODUCTION_EXECUTION_DISABLED)
        warnings.append(WARN_PRODUCTION_ROOT_HARD_DENIED)
        warnings.append(WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED)
        warnings.append(WARN_EXTERNAL_PUBLISH_DISABLED)
        warnings.append(WARN_GATEWAY_PRODUCTION_DISABLED)
        warnings.append(WARN_DISCORD_PRODUCTION_DISABLED)
        warnings.append(WARN_MANUAL_OPERATOR_CLOSURE_REQUIRED)

    chain_complete = (
        contract is not None
        and permission_record is not None
        and session_record is not None
        and boundary_record is not None
        and invocation_record is not None
        and authorization_record is not None
        and runtime_start_record is not None
        and invoke_record is not None
        and activation_request is not None
        and reservation is not None
    )

    unique_blocking = tuple(dict.fromkeys(blocking))
    unique_warnings = tuple(dict.fromkeys(warnings))

    if replay_detected:
        closure_state = CLOSURE_BLOCKED
        recommended_action = ACTION_RESOLVE_GOVERNED_RUNTIME_REPLAY
    elif closure_present:
        closure_state = CLOSURE_COMPLETED
        recommended_action = ACTION_GOVERNED_RUNTIME_CLOSURE_COMPLETED
    elif not correlation_valid:
        closure_state = CLOSURE_BLOCKED
        recommended_action = ACTION_RESOLVE_RUNTIME_CHAIN_CORRELATION
    elif recovery_required or repair_lock_held:
        closure_state = CLOSURE_BLOCKED
        recommended_action = ACTION_RUN_CONSUME_RECOVERY
    elif production_execution_allowed or gateway_enabled or discord_enabled:
        closure_state = CLOSURE_BLOCKED
        recommended_action = ACTION_MAINTAIN_PRODUCTION_BLOCK
    elif partial_consume_detected or consume_order_invalid:
        closure_state = CLOSURE_REQUIRES_RECOVERY
        recommended_action = ACTION_INSPECT_PARTIAL_GOVERNED_CONSUME
    elif not chain_complete or not consume_chain_complete:
        closure_state = CLOSURE_NOT_READY
        recommended_action = ACTION_MAINTAIN_PRODUCTION_BLOCK
    elif unique_blocking:
        closure_state = CLOSURE_BLOCKED
        recommended_action = ACTION_MAINTAIN_PRODUCTION_BLOCK
    else:
        closure_state = CLOSURE_READY
        recommended_action = ACTION_RECORD_GOVERNED_RUNTIME_CLOSURE
        if window_close_required:
            recommended_action = ACTION_CLOSE_CONTROLLED_PRODUCTION_WINDOW

    closure_ready = closure_state == CLOSURE_READY

    return ProductionGovernedRuntimeClosureSummary(
        activation_request_id=activation_request_id,
        cutover_contract_id=contract.cutover_contract_id if contract else "",
        permission_id=permission_record.permission_id if permission_record else "",
        session_id=session_record.session_id if session_record else "",
        boundary_id=boundary_record.boundary_id if boundary_record else "",
        runtime_invocation_id=(
            invocation_record.runtime_invocation_id if invocation_record else ""
        ),
        authorization_id=(
            authorization_record.authorization_id if authorization_record else ""
        ),
        runtime_start_id=(
            runtime_start_record.runtime_start_id if runtime_start_record else ""
        ),
        governed_runtime_invoke_id=invoke_record.invoke_id if invoke_record else "",
        closure_id=existing_closure.closure_id if existing_closure else "",
        closure_state=closure_state,
        closure_ready=closure_ready,
        closure_present=closure_present,
        chain_complete=chain_complete,
        correlation_valid=correlation_valid,
        consume_chain_complete=consume_chain_complete,
        partial_consume_detected=partial_consume_detected,
        replay_detected=replay_detected,
        permission_consumed=permission_consumed,
        boundary_consumed=boundary_consumed,
        invocation_consumed=invocation_consumed,
        authorization_consumed=authorization_consumed,
        runtime_started=bool(
            runtime_start_record is not None and runtime_start_record.runtime_started
        ),
        governed_runtime_invoked=governed_runtime_invoked,
        phase14_runtime_invoked=phase14_signal,
        isolated_mirror_runtime_invoked=phase14_signal,
        original_repository2_execution_attempted=False,
        session_close_ready=session_close_ready,
        boundary_consumed_state_valid=boundary_consumed and correlation_valid,
        invocation_completion_ready=invocation_consumed,
        authorization_consumed_state_valid=authorization_consumed and correlation_valid,
        window_close_required=window_close_required,
        window_state=window_state,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        audit_chain_complete=chain_complete and consume_chain_complete,
        blocking_items=unique_blocking,
        warning_items=unique_warnings,
        recommended_action=recommended_action,
        tested_commit_sha=_short_sha(
            runtime_start_record.tested_commit_sha if runtime_start_record else ""
        ),
        release_tag=runtime_start_record.release_tag if runtime_start_record else "",
    )


def record_production_governed_runtime_closure(
    activation_request_id: str,
    **kwargs: Any,
) -> ProductionGovernedRuntimeClosureRecord:
    """Append-only creation of the terminal governed runtime closure artifact.

    Only proceeds from CLOSURE_READY. Raises for PARTIAL/RECOVERY/BLOCKED/
    CORRUPTED states — never writes a closure artifact in those cases and
    never mutates any upstream artifact or consume record. A duplicate call
    for an activation that already has a matching closure is idempotent
    (returns the existing record, writes nothing). A duplicate call whose
    freshly computed IDs disagree with an existing closure artifact raises
    closure_conflict.
    """
    closure_store_dir = kwargs.get("closure_store_dir")
    now = kwargs.get("now")

    summary = evaluate_production_governed_runtime_closure(
        activation_request_id=activation_request_id, **kwargs
    )

    if summary.closure_present:
        existing = load_production_governed_runtime_closure(
            activation_request_id, store_dir=closure_store_dir
        )
        assert existing is not None
        if (
            existing.permission_id == summary.permission_id
            and existing.boundary_id == summary.boundary_id
            and existing.runtime_invocation_id == summary.runtime_invocation_id
            and existing.authorization_id == summary.authorization_id
            and existing.runtime_start_id == summary.runtime_start_id
            and existing.governed_runtime_invoke_id == summary.governed_runtime_invoke_id
        ):
            return existing
        raise ProductionGovernedRuntimeClosureError(BLOCK_CLOSURE_CONFLICT)

    if summary.closure_state != CLOSURE_READY:
        raise ProductionGovernedRuntimeClosureError(
            f"governed_runtime_closure_not_ready:{','.join(summary.blocking_items) or summary.closure_state}"
        )

    permission_consume = load_runtime_permission_consume_record(
        summary.permission_id, store_dir=kwargs.get("permission_consume_store_dir")
    )
    boundary_consume = load_runtime_boundary_consume_record(
        summary.boundary_id, store_dir=kwargs.get("boundary_consume_store_dir")
    )
    invocation_consume = load_runtime_invocation_consume_record(
        summary.runtime_invocation_id, store_dir=kwargs.get("invocation_consume_store_dir")
    )
    authorization_consume = load_execution_authorization_consume_record(
        summary.authorization_id, store_dir=kwargs.get("authorization_consume_store_dir")
    )
    runtime_start_record = load_runtime_start_record(
        activation_request_id, store_dir=kwargs.get("runtime_start_store_dir")
    )
    reservation = load_execution_reservation(
        activation_request_id, store_dir=kwargs.get("reservation_dir")
    )

    record = ProductionGovernedRuntimeClosureRecord(
        closure_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        cutover_contract_id=summary.cutover_contract_id,
        permission_id=summary.permission_id,
        session_id=summary.session_id,
        boundary_id=summary.boundary_id,
        runtime_invocation_id=summary.runtime_invocation_id,
        authorization_id=summary.authorization_id,
        runtime_start_id=summary.runtime_start_id,
        governed_runtime_invoke_id=summary.governed_runtime_invoke_id,
        permission_consume_record_id=_consume_payload_get(
            permission_consume, "permission_id"
        ),
        boundary_consume_record_id=_consume_payload_get(boundary_consume, "boundary_id"),
        invocation_consume_record_id=_consume_payload_get(
            invocation_consume, "runtime_invocation_id"
        ),
        authorization_consume_record_id=_consume_payload_get(
            authorization_consume, "authorization_id"
        ),
        reservation_id=reservation.reservation_id if reservation else "",
        execution_attempt_id=(
            runtime_start_record.execution_attempt_id if runtime_start_record else ""
        ),
        dispatch_run_id=(
            runtime_start_record.dispatch_run_id if runtime_start_record else ""
        ),
        ticket_id=runtime_start_record.ticket_id if runtime_start_record else "",
        confirmation_id=(
            runtime_start_record.confirmation_id if runtime_start_record else ""
        ),
        closure_status=CLOSURE_COMPLETED,
        correlation_valid=summary.correlation_valid,
        consume_chain_complete=summary.consume_chain_complete,
        governed_runtime_invoked=summary.governed_runtime_invoked,
        runtime_started=summary.runtime_started,
        completed_at=_utc_now_iso(now),
        tested_commit_sha=summary.tested_commit_sha,
        release_tag=summary.release_tag,
        warning_codes=summary.warning_items,
        blocking_codes=summary.blocking_items,
    )

    path = _closure_path(activation_request_id, store_dir=closure_store_dir)
    try:
        write_once_consume_record(path, record.to_dict())
    except OneShotConsumeWriteConflict as exc:
        raise ProductionGovernedRuntimeClosureError(BLOCK_CLOSURE_CONFLICT) from exc
    except OSError as exc:
        raise ProductionGovernedRuntimeClosureError(BLOCK_CLOSURE_WRITE_FAILED) from exc
    return record


def _assert_safe_output(output: str) -> None:
    lowered = output.lower()
    forbidden = (
        "password",
        "secret",
        "phrase",
        "token=",
        "argv",
        "cwd=",
        "stdout",
        "stderr",
        "executor_id",
        "operator_id",
        "signer_id",
        "/opt/data/multi-content-pipeline",
    )
    for marker in forbidden:
        if marker in lowered:
            raise ProductionGovernedRuntimeClosureError(BLOCK_UNSAFE_OUTPUT)
    if "runtime_invoked:" in lowered and "governed_runtime_invoked:" not in lowered:
        raise ProductionGovernedRuntimeClosureError(BLOCK_UNSAFE_OUTPUT)


def format_production_governed_runtime_closure(
    summary: ProductionGovernedRuntimeClosureSummary,
) -> str:
    lines = [
        f"activation_request_id: {summary.activation_request_id}",
        f"closure_state: {summary.closure_state}",
        f"closure_ready: {str(summary.closure_ready).lower()}",
        f"closure_present: {str(summary.closure_present).lower()}",
        f"chain_complete: {str(summary.chain_complete).lower()}",
        f"correlation_valid: {str(summary.correlation_valid).lower()}",
        f"consume_chain_complete: {str(summary.consume_chain_complete).lower()}",
        f"partial_consume_detected: {str(summary.partial_consume_detected).lower()}",
        f"replay_detected: {str(summary.replay_detected).lower()}",
        f"governed_runtime_invoked: {str(summary.governed_runtime_invoked).lower()}",
        f"phase14_runtime_invoked: {str(summary.phase14_runtime_invoked).lower()}",
        f"isolated_mirror_runtime_invoked: {str(summary.isolated_mirror_runtime_invoked).lower()}",
        f"runtime_started: {str(summary.runtime_started).lower()}",
        f"original_repository2_execution_attempted: {str(summary.original_repository2_execution_attempted).lower()}",
        f"window_state: {summary.window_state}",
        f"window_close_required: {str(summary.window_close_required).lower()}",
        f"blocking_items: {', '.join(summary.blocking_items) or 'none'}",
        f"warning_items: {', '.join(summary.warning_items) or 'none'}",
        f"recommended_action: {summary.recommended_action}",
        "production_execution_allowed: false",
        "production_root_hard_deny: true",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
        "external_publish_enabled: false",
        f"tested_commit_sha: {summary.tested_commit_sha}",
        f"release_tag: {summary.release_tag}",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


@dataclass(frozen=True)
class ProductionGovernedRuntimeClosureReleaseSummary:
    activation_request_id: str
    release_status: str
    phase15_chain_complete: bool
    closure_completed: bool
    consume_chain_complete: bool
    replay_free: bool
    correlation_valid: bool
    window_close_required: bool
    session_close_required: bool
    next_phase: str
    original_repository2_execution_attempted: bool = False
    production_execution_allowed: bool = False


_RELEASE_STATUS_BY_STATE = {
    CLOSURE_READY: "GOVERNED_RUNTIME_CLOSURE_READY",
    CLOSURE_COMPLETED: "GOVERNED_RUNTIME_CLOSURE_COMPLETED",
    CLOSURE_REQUIRES_RECOVERY: "GOVERNED_RUNTIME_CLOSURE_REQUIRES_RECOVERY",
    CLOSURE_BLOCKED: "GOVERNED_RUNTIME_CLOSURE_BLOCKED",
    CLOSURE_NOT_READY: "GOVERNED_RUNTIME_CLOSURE_BLOCKED",
    CLOSURE_CORRUPTED: "GOVERNED_RUNTIME_CLOSURE_BLOCKED",
}


def build_production_governed_runtime_closure_release_summary(
    summary: ProductionGovernedRuntimeClosureSummary,
) -> ProductionGovernedRuntimeClosureReleaseSummary:
    release_status = _RELEASE_STATUS_BY_STATE.get(
        summary.closure_state, "GOVERNED_RUNTIME_CLOSURE_BLOCKED"
    )
    closure_completed = summary.closure_state == CLOSURE_COMPLETED
    next_phase = (
        ACTION_PREPARE_PHASE_16_V1_RELEASE_CANDIDATE_VALIDATION
        if closure_completed
        else ""
    )
    return ProductionGovernedRuntimeClosureReleaseSummary(
        activation_request_id=summary.activation_request_id,
        release_status=release_status,
        phase15_chain_complete=summary.chain_complete,
        closure_completed=closure_completed,
        consume_chain_complete=summary.consume_chain_complete,
        replay_free=not summary.replay_detected,
        correlation_valid=summary.correlation_valid,
        window_close_required=summary.window_close_required,
        session_close_required=not summary.closure_present or True,
        next_phase=next_phase,
    )


@dataclass(frozen=True)
class ProductionGovernedRuntimeClosureAuditSummary:
    activation_request_id: str
    total_chain_artifact_count: int
    consume_record_count: int
    missing_artifact_count: int
    mismatch_count: int
    replay_count: int
    partial_consume_count: int
    warning_count: int
    blocking_count: int


def build_production_governed_runtime_closure_audit_summary(
    summary: ProductionGovernedRuntimeClosureSummary,
) -> ProductionGovernedRuntimeClosureAuditSummary:
    chain_ids = (
        summary.cutover_contract_id,
        summary.permission_id,
        summary.session_id,
        summary.boundary_id,
        summary.runtime_invocation_id,
        summary.authorization_id,
        summary.runtime_start_id,
        summary.governed_runtime_invoke_id,
    )
    present_artifacts = sum(1 for value in chain_ids if value)
    consume_flags = (
        summary.permission_consumed,
        summary.boundary_consumed,
        summary.invocation_consumed,
        summary.authorization_consumed,
    )
    consume_record_count = sum(1 for flag in consume_flags if flag)
    missing_artifact_count = len(chain_ids) - present_artifacts
    return ProductionGovernedRuntimeClosureAuditSummary(
        activation_request_id=summary.activation_request_id,
        total_chain_artifact_count=present_artifacts,
        consume_record_count=consume_record_count,
        missing_artifact_count=missing_artifact_count,
        mismatch_count=0 if summary.correlation_valid else 1,
        replay_count=1 if summary.replay_detected else 0,
        partial_consume_count=1 if summary.partial_consume_detected else 0,
        warning_count=len(summary.warning_items),
        blocking_count=len(summary.blocking_items),
    )
