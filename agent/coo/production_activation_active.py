"""Production activation controlled active transition — Phase 14H-1.

Transitions armed activations to active after dry-run and gate preconditions.
No dispatch execution, subprocess, or Repository2 access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.production_activation_active_gate import evaluate_active_gate
from agent.coo.production_activation_arm import (
    CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
    refresh_activation_lifecycle,
)
from agent.coo.production_activation_dry_run import (
    ProductionActivationDryRunError,
    ProductionActivationDryRunRecord,
    load_latest_dry_run_record,
    probe_dry_run_audit_store_available,
)
from agent.coo.production_activation_kill_switch import build_control_event
from agent.coo.production_activation_state import (
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_ARMED,
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
    ActivationRequest,
    ActivationStateTransition,
    ProductionActivationStateError,
    ROLE_PRODUCTION_EXECUTOR,
    validate_activation_request,
    validate_activation_transition,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    load_activation_request,
    save_activation_request,
)

ACTIVE_MAX_RUNTIME_MINUTES = 60

EVENT_ACTIVE_TRANSITION_EVALUATED = "active_transition_evaluated"
EVENT_ACTIVE_ENTERED = "active_entered"
EVENT_ACTIVE_TRANSITION_BLOCKED = "active_transition_blocked"

REASON_ACTIVE_ENTERED = "active_entered"
REASON_ACTIVE_TRANSITION_BLOCKED = "active_transition_blocked"
REASON_ACTIVE_TRANSITION_EVALUATED = "active_transition_evaluated"

ACTION_ACTIVE_STATE_READY_WAIT_FOR_EXECUTION_GATE = (
    "active_state_ready_wait_for_execution_gate"
)
ACTION_RESOLVE_ACTIVE_TRANSITION_BLOCKERS = "resolve_active_transition_blockers"
ACTION_REFRESH_PRODUCTION_DRY_RUN = "refresh_production_dry_run"
ACTION_ACTIVATION_EXPIRED_CREATE_NEW_PROPOSAL = "activation_expired_create_new_proposal"
ACTION_SUSPEND_ACTIVE_ACTIVATION = "suspend_active_activation"
ACTION_ACTIVATION_ALREADY_ACTIVE = "activation_already_active"
ACTION_RESOLVE_IDENTITY_CONFLICT = "resolve_identity_conflict"
ACTION_RESOLVE_ACTIVATION_ARTIFACT_ERROR = "resolve_activation_artifact_error"

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
        "active_actor_id",
        "actor_id",
        "requested_by",
        "confirm-production-activation",
        "confirm-repository2-execution",
        "dry_run_key",
    }
)


class ProductionActivationActiveError(ValueError):
    """Raised when controlled active transition cannot complete safely."""


@dataclass(frozen=True)
class ActivationActiveTransitionStatus:
    """Safe controlled active transition status."""

    activation_request_id: str
    state: str
    active: bool
    active_at: str
    active_expires_at: str
    dry_run_verified: bool
    active_gate_ready: bool
    executor_assigned: bool
    production_execution_allowed: bool = False
    repository2_execution_attempted: bool = False
    recommended_action: str = ""
    already_active: bool = False


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_arm_expired(request: ActivationRequest, *, now: datetime | None = None) -> bool:
    armed_text = (request.armed_expires_at or "").strip()
    if not armed_text:
        return True
    return _utc_now(now) >= _parse_iso(armed_text)


def _compute_active_expires_at(
    request: ActivationRequest,
    *,
    now: datetime | None = None,
    max_runtime_minutes: int = ACTIVE_MAX_RUNTIME_MINUTES,
) -> str:
    current = _utc_now(now)
    candidate = current + timedelta(minutes=max_runtime_minutes)
    expires_text = (request.expires_at or "").strip()
    if expires_text:
        proposal_expires = _parse_iso(expires_text)
        if proposal_expires < candidate:
            candidate = proposal_expires
    if candidate <= current:
        raise ProductionActivationActiveError(
            "activation expires_at has elapsed; active transition is not allowed"
        )
    return candidate.isoformat()


def _validate_phrase(phrase: str) -> None:
    if (phrase or "").strip() != CONFIRM_PRODUCTION_ACTIVATION_PHRASE:
        raise ProductionActivationActiveError(
            "active transition confirmation phrase is invalid"
        )


def _validate_actor_role(actor_role: str) -> str:
    normalized = (actor_role or "").strip()
    if normalized != ROLE_PRODUCTION_EXECUTOR:
        raise ProductionActivationActiveError(
            "actor_role must be production_executor for active transition"
        )
    return normalized


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
        raise ProductionActivationActiveError(str(exc)) from exc
    except ProductionActivationStateError as exc:
        raise ProductionActivationActiveError(str(exc)) from exc


def _validate_dry_run_record(
    request: ActivationRequest,
    record: ProductionActivationDryRunRecord | None,
) -> ProductionActivationDryRunRecord:
    if record is None:
        raise ProductionActivationActiveError(
            "latest production dry-run record is missing"
        )
    if record.activation_request_id != request.activation_request_id:
        raise ProductionActivationActiveError(
            "dry-run record activation_request_id mismatch"
        )
    if record.result != "ready":
        raise ProductionActivationActiveError(
            "latest production dry-run result is not ready"
        )
    if record.tested_commit_sha != request.tested_commit_sha:
        raise ProductionActivationActiveError(
            "dry-run tested_commit_sha does not match activation artifact"
        )
    if record.release_tag != request.release_tag:
        raise ProductionActivationActiveError(
            "dry-run release_tag does not match activation artifact"
        )
    if not (record.ticket_id or "").strip():
        raise ProductionActivationActiveError(
            "dry-run ticket scope is missing"
        )
    if not (record.confirmation_id or "").strip():
        raise ProductionActivationActiveError(
            "dry-run confirmation scope is missing"
        )
    return record


def _resolve_recommended_action(
    *,
    request: ActivationRequest,
    active_gate_ready: bool,
    dry_run_verified: bool,
    already_active: bool = False,
    identity_conflict: bool = False,
) -> str:
    if already_active:
        return ACTION_ACTIVATION_ALREADY_ACTIVE
    if identity_conflict:
        return ACTION_RESOLVE_IDENTITY_CONFLICT
    if request.state in {ACTIVATION_STATE_REVOKED, ACTIVATION_STATE_SUSPENDED}:
        return ACTION_ACTIVATION_EXPIRED_CREATE_NEW_PROPOSAL
    if request.state == ACTIVATION_STATE_ACTIVE:
        return ACTION_ACTIVE_STATE_READY_WAIT_FOR_EXECUTION_GATE
    if not dry_run_verified:
        return ACTION_REFRESH_PRODUCTION_DRY_RUN
    if not active_gate_ready:
        return ACTION_RESOLVE_ACTIVE_TRANSITION_BLOCKERS
    if request.state == ACTIVATION_STATE_ARMED:
        return ACTION_ACTIVE_STATE_READY_WAIT_FOR_EXECUTION_GATE
    return ACTION_RESOLVE_ACTIVATION_ARTIFACT_ERROR


def build_active_transition_status(
    request: ActivationRequest,
    *,
    active_gate_ready: bool = False,
    dry_run_verified: bool = False,
    already_active: bool = False,
) -> ActivationActiveTransitionStatus:
    active = request.state == ACTIVATION_STATE_ACTIVE
    status = ActivationActiveTransitionStatus(
        activation_request_id=request.activation_request_id,
        state=request.state,
        active=active,
        active_at=request.active_at or "(none)",
        active_expires_at=request.active_expires_at or "(none)",
        dry_run_verified=dry_run_verified,
        active_gate_ready=active_gate_ready,
        executor_assigned=bool((request.executor_id or "").strip()),
        recommended_action="",
        already_active=already_active,
    )
    return replace(
        status,
        recommended_action=_resolve_recommended_action(
            request=request,
            active_gate_ready=active_gate_ready,
            dry_run_verified=dry_run_verified,
            already_active=already_active,
        ),
    )


def evaluate_active_transition(
    request: ActivationRequest,
    *,
    actor_id: str,
    actor_role: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ActivationActiveTransitionStatus:
    """Evaluate active transition preconditions without mutating state."""
    actor = (actor_id or "").strip()
    dry_run_verified = False
    active_gate_ready = False

    if request.state == ACTIVATION_STATE_ACTIVE:
        if actor and actor == request.executor_id:
            return build_active_transition_status(
                request,
                active_gate_ready=True,
                dry_run_verified=bool(request.dry_run_event_id),
                already_active=True,
            )
        return replace(
            build_active_transition_status(
                request,
                active_gate_ready=True,
                dry_run_verified=bool(request.dry_run_event_id),
            ),
            recommended_action=ACTION_RESOLVE_IDENTITY_CONFLICT,
        )

    if request.state != ACTIVATION_STATE_ARMED:
        return build_active_transition_status(request)

    gate = evaluate_active_gate(
        request,
        repo_root=repo_root,
        store_dir=store_dir,
        merged_config=merged_config,
        now=now,
    )
    active_gate_ready = gate.gate_ready

    if not probe_dry_run_audit_store_available(history_dir=history_dir):
        return build_active_transition_status(
            request,
            active_gate_ready=active_gate_ready,
        )

    try:
        record = load_latest_dry_run_record(
            request.activation_request_id,
            history_dir=history_dir,
        )
        _validate_dry_run_record(request, record)
        dry_run_verified = True
    except ProductionActivationActiveError:
        dry_run_verified = False

    if actor_role.strip() != ROLE_PRODUCTION_EXECUTOR:
        active_gate_ready = False
    if actor and actor != request.executor_id:
        active_gate_ready = False

    return build_active_transition_status(
        request,
        active_gate_ready=active_gate_ready and dry_run_verified,
        dry_run_verified=dry_run_verified,
    )


def activate_production_activation(
    *,
    activation_request_id: str,
    actor_id: str,
    actor_role: str,
    phrase: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ActivationActiveTransitionStatus:
    """Transition an armed activation to active without executing production work."""
    role = _validate_actor_role(actor_role)
    actor = (actor_id or "").strip()
    if not actor:
        raise ProductionActivationActiveError("actor_id is required")

    request = refresh_activation_lifecycle(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        now=now,
    )

    if request.state == ACTIVATION_STATE_ACTIVE:
        if actor == request.executor_id:
            return build_active_transition_status(
                request,
                active_gate_ready=True,
                dry_run_verified=bool(request.dry_run_event_id),
                already_active=True,
            )
        raise ProductionActivationActiveError(
            "active activation identity conflict for actor"
        )

    if request.state in {ACTIVATION_STATE_SUSPENDED, ACTIVATION_STATE_REVOKED}:
        raise ProductionActivationActiveError(
            f"activation cannot enter active from state {request.state!r}"
        )
    if request.state != ACTIVATION_STATE_ARMED:
        raise ProductionActivationActiveError(
            f"activation cannot enter active from state {request.state!r}"
        )

    path_before = None
    if store_dir is not None:
        from agent.coo.production_activation_store import activation_request_path

        path_before = activation_request_path(
            activation_request_id,
            store_dir=store_dir,
        )
        digest_before = path_before.read_bytes() if path_before.is_file() else None
    else:
        digest_before = None

    try:
        _validate_phrase(phrase)
    except ProductionActivationActiveError:
        raise

    if actor != request.executor_id:
        raise ProductionActivationActiveError(
            "actor_id must match assigned production executor"
        )

    if _is_arm_expired(request, now=now):
        raise ProductionActivationActiveError("armed activation TTL has expired")

    gate = evaluate_active_gate(
        request,
        repo_root=repo_root,
        store_dir=store_dir,
        merged_config=merged_config,
        now=now,
    )
    if not gate.gate_ready:
        raise ProductionActivationActiveError("active gate is not ready")

    if not probe_dry_run_audit_store_available(history_dir=history_dir):
        raise ProductionActivationActiveError("dry-run audit store is unavailable")

    try:
        dry_run_record = load_latest_dry_run_record(
            activation_request_id,
            history_dir=history_dir,
        )
    except ProductionActivationDryRunError as exc:
        raise ProductionActivationActiveError(str(exc)) from exc
    dry_run_record = _validate_dry_run_record(request, dry_run_record)

    if request.dry_run_event_id and request.dry_run_event_id != dry_run_record.event_id:
        raise ProductionActivationActiveError(
            "activation dry-run reference mismatch"
        )

    validate_activation_transition(request.state, ACTIVATION_STATE_ACTIVE)
    timestamp = _utc_now_iso(now)
    active_expires_at = _compute_active_expires_at(request, now=now)
    transition = ActivationStateTransition(
        from_state=ACTIVATION_STATE_ARMED,
        to_state=ACTIVATION_STATE_ACTIVE,
        actor=actor,
        role=role,
        timestamp=timestamp,
        reason_code=REASON_ACTIVE_ENTERED,
    )
    evaluated_event = build_control_event(
        request,
        event_type=EVENT_ACTIVE_TRANSITION_EVALUATED,
        from_state=ACTIVATION_STATE_ARMED,
        to_state=ACTIVATION_STATE_ARMED,
        actor_id=actor,
        actor_role=role,
        reason_code=REASON_ACTIVE_TRANSITION_EVALUATED,
        now=now,
        dry_run_event_id=dry_run_record.event_id,
    )
    entered_event = build_control_event(
        request,
        event_type=EVENT_ACTIVE_ENTERED,
        from_state=ACTIVATION_STATE_ARMED,
        to_state=ACTIVATION_STATE_ACTIVE,
        actor_id=actor,
        actor_role=role,
        reason_code=REASON_ACTIVE_ENTERED,
        now=now,
        dry_run_event_id=dry_run_record.event_id,
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
        state=ACTIVATION_STATE_ACTIVE,
        created_at=request.created_at,
        updated_at=timestamp,
        state_history=request.state_history + (transition,),
        approval_history=request.approval_history,
        expires_at=request.expires_at,
        armed_expires_at=request.armed_expires_at,
        active_expires_at=active_expires_at,
        executor_id=request.executor_id,
        phrase_verified=request.phrase_verified,
        armed_at=request.armed_at,
        disarmed_at="",
        disarm_reason_code="",
        active_at=timestamp,
        active_actor_id=actor,
        dry_run_event_id=dry_run_record.event_id,
        dry_run_key=dry_run_record.dry_run_key,
        control_history=request.control_history + (evaluated_event, entered_event),
    )
    try:
        validate_activation_request(pending)
        saved = save_activation_request(pending, store_dir=store_dir)
        validate_activation_request(saved)
    except (ProductionActivationStoreError, ProductionActivationStateError) as exc:
        if digest_before is not None and path_before is not None and path_before.is_file():
            current = path_before.read_bytes()
            if current != digest_before:
                raise ProductionActivationActiveError(
                    "activation artifact changed during failed active transition"
                ) from exc
        raise ProductionActivationActiveError(str(exc)) from exc

    return build_active_transition_status(
        saved,
        active_gate_ready=True,
        dry_run_verified=True,
    )


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "repository2_execution_attempted: false",
        "production_execution_allowed: false",
        "dry_run_verified:",
        "active_gate_ready:",
        "executor_assigned:",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationActiveError(
                f"Unsafe active transition output field: {token!r}"
            )


def format_active_transition_status(status: ActivationActiveTransitionStatus) -> str:
    active_at = status.active_at or "(none)"
    active_expires = status.active_expires_at or "(none)"
    lines = [
        "Production Activation Active Transition",
        "",
        f"activation_request_id: {status.activation_request_id}",
        f"state: {status.state}",
        f"active: {str(status.active).lower()}",
        f"active_at: {active_at}",
        f"active_expires_at: {active_expires}",
        f"dry_run_verified: {str(status.dry_run_verified).lower()}",
        f"active_gate_ready: {str(status.active_gate_ready).lower()}",
        f"executor_assigned: {str(status.executor_assigned).lower()}",
        f"recommended_action: {status.recommended_action}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "repository2_execution_attempted: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def show_active_transition_status(
    *,
    activation_request_id: str,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ActivationActiveTransitionStatus:
    request = _load_request_or_fail(activation_request_id, store_dir=store_dir)
    gate_ready = False
    dry_run_verified = False
    if request.state == ACTIVATION_STATE_ARMED:
        gate = evaluate_active_gate(
            request,
            repo_root=repo_root,
            store_dir=store_dir,
            merged_config=merged_config,
            now=now,
        )
        gate_ready = gate.gate_ready
        try:
            record = load_latest_dry_run_record(
                activation_request_id,
                history_dir=history_dir,
            )
            _validate_dry_run_record(request, record)
            dry_run_verified = True
        except (ProductionActivationActiveError, ProductionActivationDryRunError):
            dry_run_verified = False
    elif request.state == ACTIVATION_STATE_ACTIVE:
        gate_ready = True
        dry_run_verified = bool(request.dry_run_event_id)
        return build_active_transition_status(
            request,
            active_gate_ready=gate_ready,
            dry_run_verified=dry_run_verified,
            already_active=True,
        )
    return build_active_transition_status(
        request,
        active_gate_ready=gate_ready,
        dry_run_verified=dry_run_verified,
    )


def run_activation_activate(
    *,
    activation_request_id: str,
    actor_id: str,
    actor_role: str,
    phrase: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    status = activate_production_activation(
        activation_request_id=activation_request_id,
        actor_id=actor_id,
        actor_role=actor_role,
        phrase=phrase,
        repo_root=repo_root,
        store_dir=store_dir,
        history_dir=history_dir,
        merged_config=merged_config,
        now=now,
    )
    exit_code = 0 if status.active or status.already_active else 1
    return format_active_transition_status(status), exit_code


def run_activation_active_status(
    *,
    activation_request_id: str,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    status = show_active_transition_status(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        history_dir=history_dir,
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )
    exit_code = 0 if status.active else 1
    return format_active_transition_status(status), exit_code
