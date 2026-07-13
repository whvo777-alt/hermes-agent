"""Production activation multi-party approval — Phase 14D.

Records release approver and security reviewer approvals append-only.
Transitions proposed → approved when quorum is satisfied. No arm/active/execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from agent.coo.production_activation_state import (
    ACTIVATION_STATE_APPROVED,
    ACTIVATION_STATE_PROPOSED,
    ActivationApprovalRecord,
    ActivationRequest,
    ActivationStateTransition,
    ProductionActivationStateError,
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

REASON_RELEASE_APPROVAL_RECORDED = "release_approval_recorded"
REASON_SECURITY_REVIEW_RECORDED = "security_review_recorded"
REASON_QUORUM_SATISFIED = "quorum_satisfied"
REASON_DUPLICATE_APPROVAL = "duplicate_approval"

ACTION_COLLECT_RELEASE_APPROVALS = "collect_release_approvals"
ACTION_COLLECT_SECURITY_REVIEW = "collect_security_review"
ACTION_ACTIVATION_APPROVED_WAIT_FOR_ARM = "activation_approved_wait_for_arm"
ACTION_PROPOSAL_EXPIRED = "proposal_expired"
ACTION_RESOLVE_IDENTITY_CONFLICT = "resolve_identity_conflict"
ACTION_RESOLVE_ACTIVATION_ARTIFACT_ERROR = "resolve_activation_artifact_error"
ACTION_DUPLICATE_APPROVAL = "duplicate_approval"

_ALLOWED_APPROVAL_ROLES = frozenset(
    {ROLE_RELEASE_APPROVER, ROLE_SECURITY_REVIEWER}
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
    }
)


class ProductionActivationApprovalError(ValueError):
    """Raised when activation approval cannot be recorded safely."""


@dataclass(frozen=True)
class ActivationApprovalStatus:
    """Safe read-only activation approval status."""

    activation_request_id: str
    state: str
    release_approver_count: int
    security_reviewer_count: int
    quorum_satisfied: bool
    expired: bool
    tested_commit_sha: str
    release_tag: str
    recommended_action: str
    production_execution_allowed: bool = False
    duplicate_recorded: bool = False


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def _short_sha(value: str, limit: int = 12) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _approval_counts(
    approval_history: tuple[ActivationApprovalRecord, ...],
) -> tuple[int, int, tuple[str, ...], str]:
    release_approvers: list[str] = []
    security_reviewer = ""
    for record in approval_history:
        if record.role == ROLE_RELEASE_APPROVER:
            release_approvers.append(record.approver_id)
        elif record.role == ROLE_SECURITY_REVIEWER:
            security_reviewer = record.approver_id
    return (
        len(release_approvers),
        1 if security_reviewer else 0,
        tuple(release_approvers),
        security_reviewer,
    )


def _is_expired(request: ActivationRequest, *, now: datetime | None = None) -> bool:
    if not (request.expires_at or "").strip():
        return False
    expires = datetime.fromisoformat(request.expires_at.replace("Z", "+00:00"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return _utc_now(now) >= expires


def _quorum_satisfied(approval_history: tuple[ActivationApprovalRecord, ...]) -> bool:
    release_count, security_count, _, _ = _approval_counts(approval_history)
    return (
        release_count >= MIN_RELEASE_APPROVER_COUNT
        and security_count >= MIN_SECURITY_REVIEWER_COUNT
    )


def _resolve_recommended_action(status: ActivationApprovalStatus) -> str:
    if status.expired:
        return ACTION_PROPOSAL_EXPIRED
    if status.duplicate_recorded:
        return ACTION_DUPLICATE_APPROVAL
    if status.state == ACTIVATION_STATE_APPROVED:
        return ACTION_ACTIVATION_APPROVED_WAIT_FOR_ARM
    if status.quorum_satisfied:
        return ACTION_ACTIVATION_APPROVED_WAIT_FOR_ARM
    if status.security_reviewer_count < MIN_SECURITY_REVIEWER_COUNT:
        if status.release_approver_count < MIN_RELEASE_APPROVER_COUNT:
            return ACTION_COLLECT_RELEASE_APPROVALS
        return ACTION_COLLECT_SECURITY_REVIEW
    return ACTION_COLLECT_RELEASE_APPROVALS


def build_activation_approval_status(
    request: ActivationRequest,
    *,
    now: datetime | None = None,
    duplicate_recorded: bool = False,
) -> ActivationApprovalStatus:
    release_count, security_count, _, _ = _approval_counts(request.approval_history)
    expired = _is_expired(request, now=now)
    quorum = _quorum_satisfied(request.approval_history)
    status = ActivationApprovalStatus(
        activation_request_id=request.activation_request_id,
        state=request.state,
        release_approver_count=release_count,
        security_reviewer_count=security_count,
        quorum_satisfied=quorum,
        expired=expired,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        recommended_action="",
        duplicate_recorded=duplicate_recorded,
    )
    return replace(status, recommended_action=_resolve_recommended_action(status))


def _assert_safe_status_output(output: str) -> None:
    lowered = output.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationApprovalError(
                f"Unsafe activation approval output field: {token!r}"
            )


def format_activation_approval_status(status: ActivationApprovalStatus) -> str:
    """Format safe activation approval status output."""
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
        raise ProductionActivationApprovalError(str(exc)) from exc
    except ProductionActivationStateError as exc:
        raise ProductionActivationApprovalError(str(exc)) from exc


def _validate_actor_role(role: str) -> str:
    normalized = (role or "").strip()
    if normalized not in _ALLOWED_APPROVAL_ROLES:
        raise ProductionActivationApprovalError(
            "approver_role must be release_approver or security_reviewer"
        )
    return normalized


def _validate_identity_for_new_approval(
    request: ActivationRequest,
    *,
    actor_id: str,
    role: str,
    approval_history: tuple[ActivationApprovalRecord, ...],
) -> None:
    if actor_id == request.requested_by:
        raise ProductionActivationApprovalError(
            "requester cannot approve their own activation proposal"
        )

    release_approvers, security_reviewers = set(), set()
    for record in approval_history:
        if record.role == ROLE_RELEASE_APPROVER:
            release_approvers.add(record.approver_id)
        elif record.role == ROLE_SECURITY_REVIEWER:
            security_reviewers.add(record.approver_id)

    if role == ROLE_RELEASE_APPROVER and actor_id in security_reviewers:
        raise ProductionActivationApprovalError(
            "release approver cannot match security reviewer identity"
        )
    if role == ROLE_SECURITY_REVIEWER:
        if security_reviewers:
            raise ProductionActivationApprovalError(
                "security reviewer approval already recorded"
            )
        if actor_id in release_approvers:
            raise ProductionActivationApprovalError(
                "security reviewer cannot match release approver identity"
            )


def _has_duplicate_approval(
    approval_history: tuple[ActivationApprovalRecord, ...],
    *,
    actor_id: str,
    role: str,
) -> bool:
    return any(
        record.approver_id == actor_id and record.role == role
        for record in approval_history
    )


def _build_approval_record(
    request: ActivationRequest,
    *,
    actor_id: str,
    role: str,
    reason_code: str,
    now: datetime | None = None,
) -> ActivationApprovalRecord:
    return ActivationApprovalRecord(
        approver_id=actor_id,
        role=role,
        timestamp=_utc_now_iso(now),
        approval_id=str(uuid.uuid4()),
        activation_request_id=request.activation_request_id,
        decision="approved",
        reason_code=reason_code,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
    )


def _maybe_transition_to_approved(
    request: ActivationRequest,
    *,
    now: datetime | None = None,
) -> ActivationRequest:
    if not _quorum_satisfied(request.approval_history):
        return request

    validate_activation_transition(ACTIVATION_STATE_PROPOSED, ACTIVATION_STATE_APPROVED)
    _, _, release_approvers, security_reviewer = _approval_counts(request.approval_history)
    if len(release_approvers) < MIN_RELEASE_APPROVER_COUNT:
        return request
    if not security_reviewer:
        return request

    timestamp = _utc_now_iso(now)
    transition = ActivationStateTransition(
        from_state=ACTIVATION_STATE_PROPOSED,
        to_state=ACTIVATION_STATE_APPROVED,
        actor=security_reviewer,
        role=ROLE_SECURITY_REVIEWER,
        timestamp=timestamp,
        reason_code=REASON_QUORUM_SATISFIED,
    )
    return ActivationRequest(
        activation_request_id=request.activation_request_id,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        repository_attestation_hash=request.repository_attestation_hash,
        requested_by=request.requested_by,
        approved_by=tuple(release_approvers),
        security_reviewed_by=security_reviewer,
        activation_scope=request.activation_scope,
        rollback_commit=request.rollback_commit,
        state=ACTIVATION_STATE_APPROVED,
        created_at=request.created_at,
        updated_at=timestamp,
        state_history=request.state_history + (transition,),
        approval_history=request.approval_history,
        expires_at=request.expires_at,
        armed_expires_at="",
        active_expires_at="",
    )


def _record_approval(
    *,
    activation_request_id: str,
    actor_id: str,
    role: str,
    reason_code: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ActivationApprovalStatus:
    role = _validate_actor_role(role)
    actor = (actor_id or "").strip()
    if not actor:
        raise ProductionActivationApprovalError("approver_id is required")

    request = _load_request_or_fail(activation_request_id, store_dir=store_dir)
    if request.state not in {ACTIVATION_STATE_PROPOSED, ACTIVATION_STATE_APPROVED}:
        raise ProductionActivationApprovalError(
            f"activation approvals are not accepted in state {request.state!r}"
        )
    if request.state == ACTIVATION_STATE_APPROVED:
        status = build_activation_approval_status(request, now=now)
        return replace(
            status,
            recommended_action=ACTION_ACTIVATION_APPROVED_WAIT_FOR_ARM,
        )

    if _is_expired(request, now=now):
        raise ProductionActivationApprovalError(
            "activation proposal has expired; approval is not allowed"
        )

    if _has_duplicate_approval(
        request.approval_history,
        actor_id=actor,
        role=role,
    ):
        status = build_activation_approval_status(
            request,
            now=now,
            duplicate_recorded=True,
        )
        return replace(
            status,
            recommended_action=ACTION_DUPLICATE_APPROVAL,
        )

    _validate_identity_for_new_approval(
        request,
        actor_id=actor,
        role=role,
        approval_history=request.approval_history,
    )

    updated_history = request.approval_history + (
        _build_approval_record(
            request,
            actor_id=actor,
            role=role,
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
        approved_by=(),
        security_reviewed_by="",
        activation_scope=request.activation_scope,
        rollback_commit=request.rollback_commit,
        state=ACTIVATION_STATE_PROPOSED,
        created_at=request.created_at,
        updated_at=_utc_now_iso(now),
        state_history=request.state_history,
        approval_history=updated_history,
        expires_at=request.expires_at,
        armed_expires_at="",
        active_expires_at="",
    )
    try:
        validate_activation_request(pending)
    except ProductionActivationStateError as exc:
        raise ProductionActivationApprovalError(str(exc)) from exc

    transitioned = _maybe_transition_to_approved(pending, now=now)
    try:
        saved = save_activation_request(transitioned, store_dir=store_dir)
        validate_activation_request(saved)
    except (ProductionActivationStoreError, ProductionActivationStateError) as exc:
        raise ProductionActivationApprovalError(str(exc)) from exc

    return build_activation_approval_status(saved, now=now)


def record_release_approver_approval(
    *,
    activation_request_id: str,
    approver_id: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ActivationApprovalStatus:
    """Record one release approver approval."""
    return _record_approval(
        activation_request_id=activation_request_id,
        actor_id=approver_id,
        role=ROLE_RELEASE_APPROVER,
        reason_code=REASON_RELEASE_APPROVAL_RECORDED,
        store_dir=store_dir,
        now=now,
    )


def record_security_reviewer_approval(
    *,
    activation_request_id: str,
    reviewer_id: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ActivationApprovalStatus:
    """Record one security reviewer approval."""
    return _record_approval(
        activation_request_id=activation_request_id,
        actor_id=reviewer_id,
        role=ROLE_SECURITY_REVIEWER,
        reason_code=REASON_SECURITY_REVIEW_RECORDED,
        store_dir=store_dir,
        now=now,
    )


def show_activation_approval_status(
    *,
    activation_request_id: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ActivationApprovalStatus:
    """Return safe read-only activation approval status."""
    request = _load_request_or_fail(activation_request_id, store_dir=store_dir)
    return build_activation_approval_status(request, now=now)


def run_activation_approve(
    *,
    activation_request_id: str,
    approver_id: str,
    approver_role: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    if approver_role != ROLE_RELEASE_APPROVER:
        raise ProductionActivationApprovalError(
            "approve command only accepts release_approver role"
        )
    status = record_release_approver_approval(
        activation_request_id=activation_request_id,
        approver_id=approver_id,
        store_dir=store_dir,
        now=now,
    )
    exit_code = 0
    if status.expired:
        exit_code = 1
    return format_activation_approval_status(status), exit_code


def run_activation_security_review(
    *,
    activation_request_id: str,
    reviewer_id: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    status = record_security_reviewer_approval(
        activation_request_id=activation_request_id,
        reviewer_id=reviewer_id,
        store_dir=store_dir,
        now=now,
    )
    exit_code = 0
    if status.expired:
        exit_code = 1
    return format_activation_approval_status(status), exit_code


def run_activation_status(
    *,
    activation_request_id: str,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    status = show_activation_approval_status(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        now=now,
    )
    exit_code = 1 if status.expired else 0
    return format_activation_approval_status(status), exit_code
