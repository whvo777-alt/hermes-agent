"""Production activation kill switch — Phase 14F.

Suspend/revoke control actions and append-only control audit events.
No process signals, execution, or Repository2 access.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from agent.coo.production_activation_arm import refresh_activation_lifecycle
from agent.coo.production_activation_state import (
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_ARMED,
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
    ActivationControlEvent,
    ActivationRequest,
    ActivationStateTransition,
    ProductionActivationStateError,
    ROLE_INCIDENT_COMMANDER,
    ROLE_OPERATOR,
    ROLE_PRODUCTION_EXECUTOR,
    validate_activation_request,
    validate_activation_transition,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    default_production_activation_dir,
    load_activation_request,
    save_activation_request,
)

EVENT_ACTIVE_GATE_EVALUATED = "active_gate_evaluated"
EVENT_SUSPEND_REQUESTED = "suspend_requested"
EVENT_SUSPENDED = "suspended"
EVENT_REVOKE_REQUESTED = "revoke_requested"
EVENT_REVOKED = "revoked"
EVENT_KILL_SWITCH_CHECKED = "kill_switch_checked"

REASON_MANUAL_SUSPEND = "manual_suspend"
REASON_RECOVERY_REQUIRED = "recovery_required"
REASON_REGRESSION_FAILURE = "regression_failure"
REASON_AUDIT_FAILURE = "audit_failure"
REASON_KILL_SWITCH_TEST = "kill_switch_test"
REASON_MAINTENANCE_WINDOW_CANCELLED = "maintenance_window_cancelled"
REASON_OPERATOR_CANCELLED = "operator_cancelled"
REASON_POLICY_VIOLATION = "policy_violation"

REASON_SUSPENDED_REVOKED = "suspended_revoked"
REASON_INCIDENT_CLOSED = "incident_closed"
REASON_ROLLBACK_REQUIRED = "rollback_required"

ACTION_SUSPEND_ACTIVATION = "suspend_activation"
ACTION_ACTIVATION_SUSPENDED_REVIEW_INCIDENT = "activation_suspended_review_incident"
ACTION_REVOKE_ACTIVATION = "revoke_activation"
ACTION_ACTIVATION_REVOKED_CREATE_NEW_PROPOSAL = "activation_revoked_create_new_proposal"
ACTION_KILL_SWITCH_UNAVAILABLE = "kill_switch_unavailable"
ACTION_RESOLVE_ACTIVATION_ARTIFACT_ERROR = "resolve_activation_artifact_error"
ACTION_ALREADY_SUSPENDED = "already_suspended"
ACTION_ALREADY_REVOKED = "already_revoked"

_SUSPEND_REASON_CODES = frozenset(
    {
        REASON_MANUAL_SUSPEND,
        REASON_POLICY_VIOLATION,
        REASON_RECOVERY_REQUIRED,
        REASON_REGRESSION_FAILURE,
        REASON_AUDIT_FAILURE,
        REASON_KILL_SWITCH_TEST,
        REASON_MAINTENANCE_WINDOW_CANCELLED,
        REASON_OPERATOR_CANCELLED,
    }
)
_REVOKE_REASON_CODES = frozenset(
    {
        REASON_SUSPENDED_REVOKED,
        REASON_POLICY_VIOLATION,
        REASON_INCIDENT_CLOSED,
        REASON_ROLLBACK_REQUIRED,
        REASON_OPERATOR_CANCELLED,
    }
)
_ALLOWED_SUSPEND_ROLES = frozenset({ROLE_OPERATOR, ROLE_INCIDENT_COMMANDER})
_ALLOWED_REVOKE_ROLES = frozenset({ROLE_OPERATOR, ROLE_INCIDENT_COMMANDER})

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
        "executor_id",
        "requested_by",
        "confirm-production-activation",
    }
)


class ProductionActivationKillSwitchError(ValueError):
    """Raised when kill switch control cannot be applied safely."""


@dataclass(frozen=True)
class ProductionActivationKillSwitchStatus:
    """Safe kill switch contract status."""

    activation_request_id: str
    kill_switch_available: bool
    kill_switch_armed: bool
    suspend_available: bool
    suspended: bool
    revoked: bool
    production_execution_allowed: bool = False
    active_gate_ready: bool = False
    recommended_action: str = ""


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def probe_audit_store_available(*, store_dir: Path | None = None) -> bool:
    base = (store_dir or default_production_activation_dir()).resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def is_kill_switch_available(
    request: ActivationRequest,
    *,
    store_dir: Path | None = None,
) -> bool:
    if request.state not in {
        ACTIVATION_STATE_ARMED,
        ACTIVATION_STATE_ACTIVE,
        ACTIVATION_STATE_SUSPENDED,
    }:
        return False
    return probe_audit_store_available(store_dir=store_dir)


def build_kill_switch_status(
    request: ActivationRequest,
    *,
    store_dir: Path | None = None,
    active_gate_ready: bool = False,
    already_suspended: bool = False,
    already_revoked: bool = False,
) -> ProductionActivationKillSwitchStatus:
    available = is_kill_switch_available(request, store_dir=store_dir)
    suspended = request.state == ACTIVATION_STATE_SUSPENDED
    revoked = request.state == ACTIVATION_STATE_REVOKED
    armed = request.state == ACTIVATION_STATE_ARMED
    active = request.state == ACTIVATION_STATE_ACTIVE
    if already_revoked or revoked:
        recommended = ACTION_ALREADY_REVOKED if already_revoked else ACTION_ACTIVATION_REVOKED_CREATE_NEW_PROPOSAL
    elif already_suspended or suspended:
        recommended = (
            ACTION_ALREADY_SUSPENDED
            if already_suspended
            else ACTION_ACTIVATION_SUSPENDED_REVIEW_INCIDENT
        )
    elif not available:
        recommended = ACTION_KILL_SWITCH_UNAVAILABLE
    elif armed or active:
        recommended = ACTION_SUSPEND_ACTIVATION
    else:
        recommended = ACTION_RESOLVE_ACTIVATION_ARTIFACT_ERROR
    return ProductionActivationKillSwitchStatus(
        activation_request_id=request.activation_request_id,
        kill_switch_available=available,
        kill_switch_armed=armed or suspended,
        suspend_available=armed and available,
        suspended=suspended,
        revoked=revoked,
        active_gate_ready=active_gate_ready,
        recommended_action=recommended,
    )


def _load_request_or_fail(
    activation_request_id: str,
    *,
    store_dir: Path | None,
) -> ActivationRequest:
    try:
        return load_activation_request(
            activation_request_id,
            store_dir=store_dir,
        )
    except ProductionActivationStoreError as exc:
        raise ProductionActivationKillSwitchError(str(exc)) from exc
    except ProductionActivationStateError as exc:
        raise ProductionActivationKillSwitchError(str(exc)) from exc


def _validate_actor_role(role: str, *, allowed: frozenset[str]) -> str:
    normalized = (role or "").strip()
    if normalized not in allowed:
        raise ProductionActivationKillSwitchError("invalid actor role for kill switch action")
    if normalized == ROLE_PRODUCTION_EXECUTOR:
        raise ProductionActivationKillSwitchError(
            "production_executor cannot perform suspend or revoke"
        )
    return normalized


def build_control_event(
    request: ActivationRequest,
    *,
    event_type: str,
    from_state: str,
    to_state: str,
    actor_id: str,
    actor_role: str,
    reason_code: str,
    now: datetime | None = None,
    dry_run_event_id: str = "",
) -> ActivationControlEvent:
    return ActivationControlEvent(
        event_id=str(uuid.uuid4()),
        activation_request_id=request.activation_request_id,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        actor_id=actor_id,
        actor_role=actor_role,
        reason_code=reason_code,
        timestamp=_utc_now_iso(now),
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        dry_run_event_id=dry_run_event_id,
    )


def append_control_event(
    request: ActivationRequest,
    event: ActivationControlEvent,
) -> ActivationRequest:
    return ActivationRequest(
        activation_request_id=request.activation_request_id,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        repository_attestation_hash=request.repository_attestation_hash,
        requested_by=request.requested_by,
        approved_by=request.approved_by,
        security_reviewed_by=request.security_reviewed_by,
        activation_scope=request.activation_scope,
        rollback_commit=request.rollback_commit,
        state=request.state,
        created_at=request.created_at,
        updated_at=request.updated_at,
        state_history=request.state_history,
        approval_history=request.approval_history,
        expires_at=request.expires_at,
        armed_expires_at=request.armed_expires_at,
        active_expires_at=request.active_expires_at,
        executor_id=request.executor_id,
        phrase_verified=request.phrase_verified,
        armed_at=request.armed_at,
        disarmed_at=request.disarmed_at,
        disarm_reason_code=request.disarm_reason_code,
        active_at=request.active_at,
        active_actor_id=request.active_actor_id,
        dry_run_event_id=request.dry_run_event_id,
        dry_run_key=request.dry_run_key,
        control_history=request.control_history + (event,),
    )


def persist_activation_request(
    request: ActivationRequest,
    *,
    store_dir: Path | None = None,
) -> ActivationRequest:
    try:
        saved = save_activation_request(request, store_dir=store_dir)
        validate_activation_request(saved)
        return saved
    except (ProductionActivationStoreError, ProductionActivationStateError) as exc:
        raise ProductionActivationKillSwitchError(str(exc)) from exc


def record_control_event(
    request: ActivationRequest,
    *,
    event_type: str,
    from_state: str,
    to_state: str,
    actor_id: str,
    actor_role: str,
    reason_code: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
    updated_at: str | None = None,
) -> ActivationRequest:
    event = build_control_event(
        request,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        actor_id=actor_id,
        actor_role=actor_role,
        reason_code=reason_code,
        now=now,
    )
    updated = append_control_event(request, event)
    if updated_at:
        updated = ActivationRequest(
            **{
                **updated.__dict__,
                "updated_at": updated_at,
            }
        )
    return persist_activation_request(updated, store_dir=store_dir)


def _transition_with_control(
    request: ActivationRequest,
    *,
    to_state: str,
    actor_id: str,
    actor_role: str,
    reason_code: str,
    request_event_type: str,
    result_event_type: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ActivationRequest:
    from_state = request.state
    validate_activation_transition(from_state, to_state)
    timestamp = _utc_now_iso(now)
    transition = ActivationStateTransition(
        from_state=from_state,
        to_state=to_state,
        actor=actor_id,
        role=actor_role,
        timestamp=timestamp,
        reason_code=reason_code,
    )
    disarmed_at = request.disarmed_at
    disarm_reason = request.disarm_reason_code
    if to_state == ACTIVATION_STATE_REVOKED:
        disarmed_at = timestamp
        disarm_reason = reason_code
    events = (
        build_control_event(
            request,
            event_type=request_event_type,
            from_state=from_state,
            to_state=from_state,
            actor_id=actor_id,
            actor_role=actor_role,
            reason_code=reason_code,
            now=now,
        ),
        build_control_event(
            request,
            event_type=result_event_type,
            from_state=from_state,
            to_state=to_state,
            actor_id=actor_id,
            actor_role=actor_role,
            reason_code=reason_code,
            now=now,
        ),
    )
    pending = ActivationRequest(
        activation_request_id=request.activation_request_id,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        repository_attestation_hash=request.repository_attestation_hash,
        requested_by=request.requested_by,
        approved_by=request.approved_by,
        security_reviewed_by=request.security_reviewed_by,
        activation_scope=request.activation_scope,
        rollback_commit=request.rollback_commit,
        state=to_state,
        created_at=request.created_at,
        updated_at=timestamp,
        state_history=request.state_history + (transition,),
        approval_history=request.approval_history,
        expires_at=request.expires_at,
        armed_expires_at=request.armed_expires_at,
        active_expires_at="",
        executor_id=request.executor_id,
        phrase_verified=request.phrase_verified,
        armed_at=request.armed_at,
        disarmed_at=disarmed_at,
        disarm_reason_code=disarm_reason,
        active_at=request.active_at,
        active_actor_id=request.active_actor_id,
        dry_run_event_id=request.dry_run_event_id,
        dry_run_key=request.dry_run_key,
        control_history=request.control_history + events,
    )
    validate_activation_request(pending)
    return persist_activation_request(pending, store_dir=store_dir)


def suspend_production_activation(
    *,
    activation_request_id: str,
    actor_id: str,
    actor_role: str,
    reason_code: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ProductionActivationKillSwitchStatus:
    role = _validate_actor_role(actor_role, allowed=_ALLOWED_SUSPEND_ROLES)
    actor = (actor_id or "").strip()
    if not actor:
        raise ProductionActivationKillSwitchError("actor_id is required")
    reason = (reason_code or "").strip()
    if reason not in _SUSPEND_REASON_CODES:
        raise ProductionActivationKillSwitchError("invalid suspend reason_code")

    request = refresh_activation_lifecycle(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        now=now,
    )
    if request.state == ACTIVATION_STATE_SUSPENDED:
        return replace(
            build_kill_switch_status(request, store_dir=store_dir),
            recommended_action=ACTION_ALREADY_SUSPENDED,
        )
    if request.state == ACTIVATION_STATE_REVOKED:
        raise ProductionActivationKillSwitchError(
            "revoked activation cannot be suspended"
        )
    if request.state not in {ACTIVATION_STATE_ARMED, ACTIVATION_STATE_ACTIVE}:
        raise ProductionActivationKillSwitchError(
            f"activation cannot be suspended from state {request.state!r}"
        )
    if not is_kill_switch_available(request, store_dir=store_dir):
        raise ProductionActivationKillSwitchError("kill switch is unavailable")

    saved = _transition_with_control(
        request,
        to_state=ACTIVATION_STATE_SUSPENDED,
        actor_id=actor,
        actor_role=role,
        reason_code=reason,
        request_event_type=EVENT_SUSPEND_REQUESTED,
        result_event_type=EVENT_SUSPENDED,
        store_dir=store_dir,
        now=now,
    )
    return build_kill_switch_status(saved, store_dir=store_dir)


def revoke_production_activation(
    *,
    activation_request_id: str,
    actor_id: str,
    actor_role: str,
    reason_code: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ProductionActivationKillSwitchStatus:
    role = _validate_actor_role(actor_role, allowed=_ALLOWED_REVOKE_ROLES)
    actor = (actor_id or "").strip()
    if not actor:
        raise ProductionActivationKillSwitchError("actor_id is required")
    reason = (reason_code or "").strip()
    if reason not in _REVOKE_REASON_CODES:
        raise ProductionActivationKillSwitchError("invalid revoke reason_code")

    request = refresh_activation_lifecycle(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        now=now,
    )
    if request.state == ACTIVATION_STATE_REVOKED:
        return replace(
            build_kill_switch_status(request, store_dir=store_dir),
            recommended_action=ACTION_ALREADY_REVOKED,
        )
    if request.state != ACTIVATION_STATE_SUSPENDED:
        raise ProductionActivationKillSwitchError(
            f"activation revoke only supports suspended state, not {request.state!r}"
        )
    if not is_kill_switch_available(request, store_dir=store_dir):
        raise ProductionActivationKillSwitchError("kill switch is unavailable")

    saved = _transition_with_control(
        request,
        to_state=ACTIVATION_STATE_REVOKED,
        actor_id=actor,
        actor_role=role,
        reason_code=reason,
        request_event_type=EVENT_REVOKE_REQUESTED,
        result_event_type=EVENT_REVOKED,
        store_dir=store_dir,
        now=now,
    )
    return build_kill_switch_status(saved, store_dir=store_dir)


def _assert_safe_output(output: str) -> None:
    lowered = output.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationKillSwitchError(
                f"Unsafe kill switch output field: {token!r}"
            )


def format_kill_switch_status(
    status: ProductionActivationKillSwitchStatus,
    *,
    current_state: str,
    blocking_reasons: tuple[str, ...] = (),
) -> str:
    lines = [
        "Production Activation Kill Switch",
        "",
        f"activation_request_id: {status.activation_request_id}",
        f"current_state: {current_state}",
        f"gate_ready: {str(status.active_gate_ready).lower()}",
        f"kill_switch_available: {str(status.kill_switch_available).lower()}",
        f"suspended: {str(status.suspended).lower()}",
        f"revoked: {str(status.revoked).lower()}",
        f"blocking_reasons: {', '.join(blocking_reasons) if blocking_reasons else '(none)'}",
        f"recommended_action: {status.recommended_action}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def run_activation_suspend(
    *,
    activation_request_id: str,
    actor_id: str,
    actor_role: str,
    reason_code: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    status = suspend_production_activation(
        activation_request_id=activation_request_id,
        actor_id=actor_id,
        actor_role=actor_role,
        reason_code=reason_code,
        store_dir=store_dir,
        now=now,
    )
    current = load_activation_request(activation_request_id, store_dir=store_dir).state
    output = format_kill_switch_status(status, current_state=current)
    return output, 0


def run_activation_revoke(
    *,
    activation_request_id: str,
    actor_id: str,
    actor_role: str,
    reason_code: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    status = revoke_production_activation(
        activation_request_id=activation_request_id,
        actor_id=actor_id,
        actor_role=actor_role,
        reason_code=reason_code,
        store_dir=store_dir,
        now=now,
    )
    current = load_activation_request(activation_request_id, store_dir=store_dir).state
    output = format_kill_switch_status(status, current_state=current)
    return output, 0
