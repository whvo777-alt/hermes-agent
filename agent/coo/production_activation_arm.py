"""Production activation arm/disarm — Phase 14E.

Arms approved activations with executor confirmation and TTL.
Disarms to revoked; no active transition or execution.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.coo.dispatch_cli_production_activation import resolve_git_head_commit
from agent.coo.production_activation_state import (
    ACTIVATION_STATE_APPROVED,
    ACTIVATION_STATE_ARMED,
    ACTIVATION_STATE_REVOKED,
    ActivationRequest,
    ActivationStateTransition,
    ProductionActivationStateError,
    ROLE_INCIDENT_COMMANDER,
    ROLE_OPERATOR,
    ROLE_PRODUCTION_EXECUTOR,
    ROLE_RELEASE_APPROVER,
    ROLE_SECURITY_REVIEWER,
    validate_activation_request,
    validate_activation_transition,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    load_activation_request,
    save_activation_request,
)

MIN_RELEASE_APPROVER_COUNT = 2
MIN_SECURITY_REVIEWER_COUNT = 1
ARM_TTL_MINUTES = 15

CONFIRM_PRODUCTION_ACTIVATION_PHRASE = "CONFIRM-PRODUCTION-ACTIVATION"

REASON_ARM_RECORDED = "arm_recorded"
REASON_MANUAL_DISARM = "manual_disarm"
REASON_ARM_EXPIRED = "arm_expired"
REASON_MAINTENANCE_WINDOW_CANCELLED = "maintenance_window_cancelled"
REASON_POLICY_VIOLATION = "policy_violation"
REASON_OPERATOR_CANCELLED = "operator_cancelled"

ACTION_ACTIVATION_ARMED_WAIT_FOR_EXECUTION_GATE = "activation_armed_wait_for_execution_gate"
ACTION_COLLECT_ARM_CONFIRMATION = "collect_arm_confirmation"
ACTION_ASSIGN_PRODUCTION_EXECUTOR = "assign_production_executor"
ACTION_DISARM_ACTIVATION = "disarm_activation"
ACTION_ACTIVATION_REVOKED_CREATE_NEW_PROPOSAL = "activation_revoked_create_new_proposal"
ACTION_ARM_EXPIRED = "arm_expired"
ACTION_RESOLVE_IDENTITY_CONFLICT = "resolve_identity_conflict"
ACTION_RESOLVE_ACTIVATION_ARTIFACT_ERROR = "resolve_activation_artifact_error"
ACTION_ALREADY_ARMED = "already_armed"
ACTION_ALREADY_REVOKED = "already_revoked"
ACTION_COLLECT_RELEASE_APPROVALS = "collect_release_approvals"
ACTION_COLLECT_SECURITY_REVIEW = "collect_security_review"
ACTION_ACTIVATION_APPROVED_WAIT_FOR_ARM = "activation_approved_wait_for_arm"
ACTION_PROPOSAL_EXPIRED = "proposal_expired"

_APPROVED_DISARM_REASONS = frozenset(
    {
        REASON_OPERATOR_CANCELLED,
        REASON_MAINTENANCE_WINDOW_CANCELLED,
        REASON_POLICY_VIOLATION,
    }
)
_ARMED_DISARM_REASONS = frozenset(
    {
        REASON_MANUAL_DISARM,
        REASON_MAINTENANCE_WINDOW_CANCELLED,
        REASON_POLICY_VIOLATION,
    }
)

_FORBIDDEN_OUTPUT_TOKENS = frozenset(
    {
        "pipeline_root",
        "confirmation_phrase",
        "unlock_token",
        "unlock_token_id",
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
        "operator_reason",
        "requested_by",
        "executor_id",
        "confirm-production-activation",
    }
)


class ProductionActivationArmError(ValueError):
    """Raised when activation arm/disarm cannot be performed safely."""


@dataclass(frozen=True)
class ActivationLifecycleStatus:
    """Safe read-only activation lifecycle status."""

    activation_request_id: str
    state: str
    release_approver_count: int
    security_reviewer_count: int
    quorum_satisfied: bool
    executor_assigned: bool
    phrase_verified: bool
    armed: bool
    armed_expires_at: str
    expired: bool
    tested_commit_sha: str
    release_tag: str
    recommended_action: str
    production_execution_allowed: bool = False
    already_armed: bool = False
    already_revoked: bool = False


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


def _short_sha(value: str, limit: int = 12) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _approval_counts(
    request: ActivationRequest,
) -> tuple[int, int, bool]:
    release_count = sum(
        1
        for record in request.approval_history
        if record.role == ROLE_RELEASE_APPROVER
    )
    security_count = sum(
        1
        for record in request.approval_history
        if record.role == ROLE_SECURITY_REVIEWER
    )
    quorum = (
        release_count >= MIN_RELEASE_APPROVER_COUNT
        and security_count >= MIN_SECURITY_REVIEWER_COUNT
        and len(request.approved_by) >= MIN_RELEASE_APPROVER_COUNT
        and bool(request.security_reviewed_by)
    )
    return release_count, security_count, quorum


def _is_proposal_expired(request: ActivationRequest, *, now: datetime | None = None) -> bool:
    expires_text = (request.expires_at or "").strip()
    if not expires_text:
        return False
    return _utc_now(now) >= _parse_iso(expires_text)


def _is_arm_expired(request: ActivationRequest, *, now: datetime | None = None) -> bool:
    armed_text = (request.armed_expires_at or "").strip()
    if not armed_text:
        return False
    return _utc_now(now) >= _parse_iso(armed_text)


def _compute_armed_expires_at(
    request: ActivationRequest,
    *,
    now: datetime | None = None,
) -> str:
    current = _utc_now(now)
    candidate = current + timedelta(minutes=ARM_TTL_MINUTES)
    expires_text = (request.expires_at or "").strip()
    if expires_text:
        proposal_expires = _parse_iso(expires_text)
        if proposal_expires < candidate:
            candidate = proposal_expires
    return candidate.isoformat()


def _resolve_recommended_action(status: ActivationLifecycleStatus) -> str:
    if status.already_revoked:
        return ACTION_ALREADY_REVOKED
    if status.state == ACTIVATION_STATE_REVOKED:
        return ACTION_ACTIVATION_REVOKED_CREATE_NEW_PROPOSAL
    if status.expired and status.armed:
        return ACTION_ARM_EXPIRED
    if status.expired:
        return ACTION_PROPOSAL_EXPIRED
    if status.already_armed:
        return ACTION_ACTIVATION_ARMED_WAIT_FOR_EXECUTION_GATE
    if status.state == ACTIVATION_STATE_ARMED:
        return ACTION_ACTIVATION_ARMED_WAIT_FOR_EXECUTION_GATE
    if status.state == ACTIVATION_STATE_APPROVED:
        if not status.quorum_satisfied:
            if status.release_approver_count < MIN_RELEASE_APPROVER_COUNT:
                return ACTION_COLLECT_RELEASE_APPROVALS
            return ACTION_COLLECT_SECURITY_REVIEW
        return ACTION_COLLECT_ARM_CONFIRMATION
    return ACTION_RESOLVE_ACTIVATION_ARTIFACT_ERROR


def build_activation_lifecycle_status(
    request: ActivationRequest,
    *,
    now: datetime | None = None,
    already_armed: bool = False,
    already_revoked: bool = False,
) -> ActivationLifecycleStatus:
    release_count, security_count, quorum = _approval_counts(request)
    proposal_expired = _is_proposal_expired(request, now=now)
    arm_expired = request.state == ACTIVATION_STATE_ARMED and _is_arm_expired(
        request,
        now=now,
    )
    armed = request.state == ACTIVATION_STATE_ARMED
    status = ActivationLifecycleStatus(
        activation_request_id=request.activation_request_id,
        state=request.state,
        release_approver_count=release_count,
        security_reviewer_count=security_count,
        quorum_satisfied=quorum,
        executor_assigned=bool((request.executor_id or "").strip()),
        phrase_verified=bool(request.phrase_verified),
        armed=armed,
        armed_expires_at=request.armed_expires_at or "(none)",
        expired=proposal_expired or arm_expired,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        recommended_action="",
        already_armed=already_armed,
        already_revoked=already_revoked,
    )
    return replace(status, recommended_action=_resolve_recommended_action(status))


def _assert_safe_status_output(output: str) -> None:
    lowered = output.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationArmError(
                f"Unsafe activation lifecycle output field: {token!r}"
            )


def format_activation_lifecycle_status(status: ActivationLifecycleStatus) -> str:
    armed_expires = status.armed_expires_at or "(none)"
    lines = [
        "Production Activation Status",
        "",
        f"activation_request_id: {status.activation_request_id}",
        f"state: {status.state}",
        f"tested_commit_sha: {_short_sha(status.tested_commit_sha)}",
        f"release_tag: {status.release_tag}",
        "",
        "[Approvals]",
        f"release_approver_count: {status.release_approver_count}",
        f"security_reviewer_count: {status.security_reviewer_count}",
        f"quorum_satisfied: {str(status.quorum_satisfied).lower()}",
        "",
        "[Arm]",
        f"executor_assigned: {str(status.executor_assigned).lower()}",
        f"phrase_verified: {str(status.phrase_verified).lower()}",
        f"armed: {str(status.armed).lower()}",
        f"armed_expires_at: {armed_expires}",
        f"expired: {str(status.expired).lower()}",
        f"recommended_action: {status.recommended_action}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
    ]
    output = "\n".join(lines)
    _assert_safe_status_output(output)
    return output


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
        raise ProductionActivationArmError(str(exc)) from exc
    except ProductionActivationStateError as exc:
        raise ProductionActivationArmError(str(exc)) from exc


def _validate_phrase(phrase: str) -> None:
    if (phrase or "").strip() != CONFIRM_PRODUCTION_ACTIVATION_PHRASE:
        raise ProductionActivationArmError(
            "arm confirmation phrase is invalid"
        )


def _validate_head_sha_match(
    request: ActivationRequest,
    *,
    repo_root: Path | None,
) -> None:
    head = resolve_git_head_commit(repo_root=repo_root).lower()
    tested = request.tested_commit_sha.lower()
    if len(tested) < 40:
        if not head.startswith(tested):
            raise ProductionActivationArmError(
                "current repository HEAD does not match tested_commit_sha"
            )
        return
    if head != tested:
        raise ProductionActivationArmError(
            "current repository HEAD does not match tested_commit_sha"
        )


def _validate_executor_identity(
    request: ActivationRequest,
    executor_id: str,
) -> None:
    executor = (executor_id or "").strip()
    if not executor:
        raise ProductionActivationArmError("executor_id is required")
    if executor == request.requested_by:
        raise ProductionActivationArmError(
            "executor cannot match activation requester"
        )
    if executor in request.approved_by:
        raise ProductionActivationArmError(
            "executor cannot match release approver identity"
        )
    if executor == request.security_reviewed_by:
        raise ProductionActivationArmError(
            "executor cannot match security reviewer identity"
        )


def _assert_quorum(request: ActivationRequest) -> None:
    _, _, quorum = _approval_counts(request)
    if not quorum:
        raise ProductionActivationArmError(
            "activation approval quorum is not satisfied"
        )


def _transition_to_revoked(
    request: ActivationRequest,
    *,
    actor: str,
    role: str,
    reason_code: str,
    now: datetime | None = None,
) -> ActivationRequest:
    from_state = request.state
    validate_activation_transition(from_state, ACTIVATION_STATE_REVOKED)
    timestamp = _utc_now_iso(now)
    transition = ActivationStateTransition(
        from_state=from_state,
        to_state=ACTIVATION_STATE_REVOKED,
        actor=actor,
        role=role,
        timestamp=timestamp,
        reason_code=reason_code,
    )
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
        state=ACTIVATION_STATE_REVOKED,
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
        disarmed_at=timestamp,
        disarm_reason_code=reason_code,
    )


def maybe_expire_armed_activation(
    request: ActivationRequest,
    *,
    store_dir: Path | None = None,
    now: datetime | None = None,
    persist: bool = True,
) -> tuple[ActivationRequest, bool]:
    """Revoke armed activations whose armed TTL has elapsed."""
    if request.state != ACTIVATION_STATE_ARMED:
        return request, False
    if not _is_arm_expired(request, now=now):
        return request, False
    actor = (request.executor_id or "activation-ttl-guard").strip()
    revoked = _transition_to_revoked(
        request,
        actor=actor,
        role=ROLE_PRODUCTION_EXECUTOR,
        reason_code=REASON_ARM_EXPIRED,
        now=now,
    )
    try:
        validate_activation_request(revoked)
    except ProductionActivationStateError as exc:
        raise ProductionActivationArmError(str(exc)) from exc
    if not persist:
        return revoked, True
    try:
        saved = save_activation_request(revoked, store_dir=store_dir)
    except (ProductionActivationStoreError, ProductionActivationStateError) as exc:
        raise ProductionActivationArmError(str(exc)) from exc
    return saved, True


def refresh_activation_lifecycle(
    *,
    activation_request_id: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ActivationRequest:
    request = _load_request_or_fail(activation_request_id, store_dir=store_dir)
    request, _ = maybe_expire_armed_activation(
        request,
        store_dir=store_dir,
        now=now,
        persist=True,
    )
    return request


def arm_production_activation(
    *,
    activation_request_id: str,
    executor_id: str,
    phrase: str,
    store_dir: Path | None = None,
    repo_root: Path | None = None,
    now: datetime | None = None,
) -> ActivationLifecycleStatus:
    request = refresh_activation_lifecycle(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        now=now,
    )

    if request.state == ACTIVATION_STATE_REVOKED:
        raise ProductionActivationArmError(
            "revoked activation requires a new proposal before arming"
        )

    if request.state == ACTIVATION_STATE_ARMED:
        executor = (executor_id or "").strip()
        if executor and executor == request.executor_id:
            return build_activation_lifecycle_status(
                request,
                now=now,
                already_armed=True,
            )
        raise ProductionActivationArmError(
            "activation is already armed by a different executor"
        )

    if request.state != ACTIVATION_STATE_APPROVED:
        raise ProductionActivationArmError(
            f"activation cannot be armed from state {request.state!r}"
        )

    if _is_proposal_expired(request, now=now):
        raise ProductionActivationArmError(
            "activation proposal has expired; arming is not allowed"
        )

    _assert_quorum(request)
    _validate_phrase(phrase)
    _validate_executor_identity(request, executor_id)
    _validate_head_sha_match(request, repo_root=repo_root)

    timestamp = _utc_now_iso(now)
    armed_expires_at = _compute_armed_expires_at(request, now=now)
    transition = ActivationStateTransition(
        from_state=ACTIVATION_STATE_APPROVED,
        to_state=ACTIVATION_STATE_ARMED,
        actor=(executor_id or "").strip(),
        role=ROLE_PRODUCTION_EXECUTOR,
        timestamp=timestamp,
        reason_code=REASON_ARM_RECORDED,
    )
    armed = ActivationRequest(
        activation_request_id=request.activation_request_id,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        repository_attestation_hash=request.repository_attestation_hash,
        requested_by=request.requested_by,
        approved_by=request.approved_by,
        security_reviewed_by=request.security_reviewed_by,
        activation_scope=request.activation_scope,
        rollback_commit=request.rollback_commit,
        state=ACTIVATION_STATE_ARMED,
        created_at=request.created_at,
        updated_at=timestamp,
        state_history=request.state_history + (transition,),
        approval_history=request.approval_history,
        expires_at=request.expires_at,
        armed_expires_at=armed_expires_at,
        active_expires_at="",
        executor_id=(executor_id or "").strip(),
        phrase_verified=True,
        armed_at=timestamp,
        disarmed_at="",
        disarm_reason_code="",
    )
    try:
        validate_activation_request(armed)
        saved = save_activation_request(armed, store_dir=store_dir)
        validate_activation_request(saved)
    except (ProductionActivationStoreError, ProductionActivationStateError) as exc:
        raise ProductionActivationArmError(str(exc)) from exc
    return build_activation_lifecycle_status(saved, now=now)


def _resolve_disarm_role(
    request: ActivationRequest,
    actor_id: str,
    *,
    actor_role: str | None = None,
) -> str:
    if actor_role:
        normalized = actor_role.strip()
        if normalized not in {
            ROLE_PRODUCTION_EXECUTOR,
            ROLE_OPERATOR,
            ROLE_INCIDENT_COMMANDER,
        }:
            raise ProductionActivationArmError("invalid disarm actor role")
        return normalized
    if request.state == ACTIVATION_STATE_ARMED and actor_id == request.executor_id:
        return ROLE_PRODUCTION_EXECUTOR
    return ROLE_OPERATOR


def disarm_production_activation(
    *,
    activation_request_id: str,
    actor_id: str,
    reason_code: str,
    store_dir: Path | None = None,
    actor_role: str | None = None,
    now: datetime | None = None,
) -> ActivationLifecycleStatus:
    request = refresh_activation_lifecycle(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        now=now,
    )

    if request.state == ACTIVATION_STATE_REVOKED:
        return build_activation_lifecycle_status(
            request,
            now=now,
            already_revoked=True,
        )

    if request.state not in {ACTIVATION_STATE_APPROVED, ACTIVATION_STATE_ARMED}:
        raise ProductionActivationArmError(
            f"activation cannot be disarmed from state {request.state!r}"
        )

    actor = (actor_id or "").strip()
    if not actor:
        raise ProductionActivationArmError("actor_id is required")

    reason = (reason_code or "").strip()
    if not reason:
        raise ProductionActivationArmError("reason_code is required")

    role = _resolve_disarm_role(request, actor, actor_role=actor_role)

    if request.state == ACTIVATION_STATE_APPROVED:
        if reason not in _APPROVED_DISARM_REASONS:
            raise ProductionActivationArmError(
                "reason_code is not allowed for approved-state disarm"
            )
        if role not in {ROLE_OPERATOR, ROLE_INCIDENT_COMMANDER}:
            raise ProductionActivationArmError(
                "approved-state disarm requires operator or incident_commander role"
            )
    else:
        if reason not in _ARMED_DISARM_REASONS:
            raise ProductionActivationArmError(
                "reason_code is not allowed for armed-state disarm"
            )

    revoked = _transition_to_revoked(
        request,
        actor=actor,
        role=role,
        reason_code=reason,
        now=now,
    )
    try:
        validate_activation_request(revoked)
        saved = save_activation_request(revoked, store_dir=store_dir)
        validate_activation_request(saved)
    except (ProductionActivationStoreError, ProductionActivationStateError) as exc:
        raise ProductionActivationArmError(str(exc)) from exc
    return build_activation_lifecycle_status(saved, now=now)


def show_activation_lifecycle_status(
    *,
    activation_request_id: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ActivationLifecycleStatus:
    request = refresh_activation_lifecycle(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        now=now,
    )
    return build_activation_lifecycle_status(request, now=now)


def run_activation_arm(
    *,
    activation_request_id: str,
    executor_id: str,
    phrase: str,
    store_dir: Path | None = None,
    repo_root: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    status = arm_production_activation(
        activation_request_id=activation_request_id,
        executor_id=executor_id,
        phrase=phrase,
        store_dir=store_dir,
        repo_root=repo_root,
        now=now,
    )
    exit_code = 0
    if status.expired:
        exit_code = 1
    return format_activation_lifecycle_status(status), exit_code


def run_activation_disarm(
    *,
    activation_request_id: str,
    actor_id: str,
    reason_code: str,
    store_dir: Path | None = None,
    actor_role: str | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    status = disarm_production_activation(
        activation_request_id=activation_request_id,
        actor_id=actor_id,
        reason_code=reason_code,
        store_dir=store_dir,
        actor_role=actor_role,
        now=now,
    )
    exit_code = 0
    if status.expired:
        exit_code = 1
    return format_activation_lifecycle_status(status), exit_code


def run_activation_status(
    *,
    activation_request_id: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    status = show_activation_lifecycle_status(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        now=now,
    )
    exit_code = 1 if status.expired else 0
    return format_activation_lifecycle_status(status), exit_code
