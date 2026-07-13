"""Production activation active gate — Phase 14F.

Read-only gate assessment for armed activations. No active transition or execution.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_cli_pilot_regression_gate import (
    REGRESSION_GATE_BLOCKED_FOR_LIVE,
    evaluate_pilot_regression_gate,
)
from agent.coo.dispatch_cli_production_cutover import evaluate_production_cutover_checklist
from agent.coo.dispatch_cli_production_signoff import evaluate_dispatch_production_signoff
from agent.coo.dispatch_cli_production_activation import resolve_git_head_commit
from agent.coo.production_activation_arm import refresh_activation_lifecycle
from agent.coo.production_activation_kill_switch import (
    EVENT_ACTIVE_GATE_EVALUATED,
    EVENT_KILL_SWITCH_CHECKED,
    ROLE_OPERATOR,
    ACTION_ACTIVATION_REVOKED_CREATE_NEW_PROPOSAL,
    ACTION_ACTIVATION_SUSPENDED_REVIEW_INCIDENT,
    ACTION_KILL_SWITCH_UNAVAILABLE,
    ACTION_RESOLVE_ACTIVATION_ARTIFACT_ERROR,
    ProductionActivationKillSwitchError,
    append_control_event,
    build_control_event,
    is_kill_switch_available,
    persist_activation_request,
    probe_audit_store_available,
)
from agent.coo.production_activation_state import (
    ACTIVATION_SCOPE_TICKET_SCOPED,
    ACTIVATION_STATE_ARMED,
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
    ActivationRequest,
    ROLE_RELEASE_APPROVER,
    ROLE_SECURITY_REVIEWER,
    ProductionActivationStateError,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    load_activation_request,
)

MIN_RELEASE_APPROVER_COUNT = 2
MIN_SECURITY_REVIEWER_COUNT = 1

BLOCK_WRONG_STATE = "wrong_state"
BLOCK_ARMED_TTL_EXPIRED = "armed_ttl_expired"
BLOCK_QUORUM_INVALID = "quorum_invalid"
BLOCK_EXECUTOR_INVALID = "executor_invalid"
BLOCK_PHRASE_NOT_VERIFIED = "phrase_not_verified"
BLOCK_HEAD_SHA_MISMATCH = "head_sha_mismatch"
BLOCK_RELEASE_TAG_INVALID = "release_tag_invalid"
BLOCK_ROLLBACK_MISSING = "rollback_missing"
BLOCK_ATTESTATION_INVALID = "attestation_invalid"
BLOCK_RECOVERY_REQUIRED = "recovery_required"
BLOCK_REPAIR_LOCK_HELD = "repair_lock_held"
BLOCK_REGRESSION_FAIL = "regression_fail"
BLOCK_SIGNOFF_NOT_READY = "signoff_not_ready"
BLOCK_CUTOVER_NOT_READY = "cutover_not_ready"
BLOCK_KILL_SWITCH_UNAVAILABLE = "kill_switch_unavailable"
BLOCK_AUDIT_STORE_UNAVAILABLE = "audit_store_unavailable"

ACTION_ACTIVE_GATE_READY_WAIT_FOR_PHASE_14G = "active_gate_ready_wait_for_phase_14g"
ACTION_RESOLVE_GATE_BLOCKERS = "resolve_gate_blockers"

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


class ProductionActivationActiveGateError(ValueError):
    """Raised when active gate assessment cannot be performed safely."""


@dataclass(frozen=True)
class ProductionActivationActiveGateAssessment:
    """Safe active gate assessment for armed activations."""

    activation_request_id: str
    current_state: str
    gate_ready: bool
    armed_not_expired: bool
    approval_quorum_valid: bool
    executor_valid: bool
    phrase_verified: bool
    tested_commit_matches: bool
    release_tag_valid: bool
    rollback_commit_present: bool
    attestation_valid: bool
    recovery_clear: bool
    repair_lock_clear: bool
    regression_clear: bool
    signoff_ready: bool
    cutover_ready: bool
    kill_switch_available: bool
    audit_store_available: bool
    production_execution_allowed: bool = False
    blocking_reasons: tuple[str, ...] = ()
    recommended_action: str = ""


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


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


def _approval_quorum_valid(request: ActivationRequest) -> bool:
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
    return (
        release_count >= MIN_RELEASE_APPROVER_COUNT
        and security_count >= MIN_SECURITY_REVIEWER_COUNT
        and len(request.approved_by) >= MIN_RELEASE_APPROVER_COUNT
        and bool(request.security_reviewed_by)
    )


def _executor_valid(request: ActivationRequest) -> bool:
    executor = (request.executor_id or "").strip()
    if not executor:
        return False
    if executor == request.requested_by:
        return False
    if executor in request.approved_by:
        return False
    if executor == request.security_reviewed_by:
        return False
    return True


def _head_sha_matches(request: ActivationRequest, *, repo_root: Path | None) -> bool:
    try:
        head = resolve_git_head_commit(repo_root=repo_root).lower()
    except Exception:
        return False
    tested = request.tested_commit_sha.lower()
    if len(tested) < 40:
        return head.startswith(tested)
    return head == tested


def _release_tag_valid(request: ActivationRequest) -> bool:
    try:
        from agent.coo.production_activation_state import _validate_release_tag

        _validate_release_tag(request.release_tag)
        return True
    except ProductionActivationStateError:
        return False


def _rollback_present(request: ActivationRequest) -> bool:
    return bool((request.rollback_commit or "").strip())


def _attestation_valid(request: ActivationRequest) -> bool:
    try:
        from agent.coo.production_activation_state import _validate_attestation_hash

        _validate_attestation_hash(request.repository_attestation_hash)
        return True
    except ProductionActivationStateError:
        return False


def _probe_recovery_required(request: ActivationRequest) -> bool:
    ticket_id = (request.activation_scope.ticket_id or "").strip()
    if not ticket_id:
        return False
    try:
        from agent.coo.dispatch_cli_consume_recovery import assess_dispatch_consume_recovery

        assessment = assess_dispatch_consume_recovery(
            ticket_id=ticket_id,
            confirmation_id="activation-gate-probe",
        )
        return assessment.recovery_required
    except Exception:
        return True


def _probe_repair_lock_held(request: ActivationRequest) -> bool:
    ticket_id = (request.activation_scope.ticket_id or "").strip()
    if not ticket_id:
        return False
    try:
        from agent.coo.dispatch_cli_consume_repair_lock import (
            summarize_consume_repair_lock_status,
        )

        status = summarize_consume_repair_lock_status(
            ticket_id=ticket_id,
            confirmation_id="activation-gate-probe",
        )
        return status.repair_in_progress
    except Exception:
        return True


def _probe_regression_clear() -> bool:
    gate = evaluate_pilot_regression_gate()
    return gate.regression_gate != REGRESSION_GATE_BLOCKED_FOR_LIVE


def _probe_signoff_ready(*, merged_config: Mapping[str, Any] | None = None) -> bool:
    if merged_config is None:
        from hermes_cli.config import load_config

        merged_config = load_config()
    signoff = evaluate_dispatch_production_signoff(merged_config=merged_config)
    return signoff.signoff_ready


def _probe_cutover_ready(*, merged_config: Mapping[str, Any] | None = None) -> bool:
    if merged_config is None:
        from hermes_cli.config import load_config

        merged_config = load_config()
    cutover = evaluate_production_cutover_checklist(merged_config=merged_config)
    return cutover.cutover_ready


def _resolve_recommended_action(
    request: ActivationRequest,
    assessment: ProductionActivationActiveGateAssessment,
) -> str:
    if request.state == ACTIVATION_STATE_REVOKED:
        return ACTION_ACTIVATION_REVOKED_CREATE_NEW_PROPOSAL
    if request.state == ACTIVATION_STATE_SUSPENDED:
        return ACTION_ACTIVATION_SUSPENDED_REVIEW_INCIDENT
    if not assessment.audit_store_available:
        return ACTION_RESOLVE_ACTIVATION_ARTIFACT_ERROR
    if not assessment.kill_switch_available:
        return ACTION_KILL_SWITCH_UNAVAILABLE
    if assessment.gate_ready:
        return ACTION_ACTIVE_GATE_READY_WAIT_FOR_PHASE_14G
    if request.state == ACTIVATION_STATE_ARMED:
        return ACTION_RESOLVE_GATE_BLOCKERS
    return ACTION_RESOLVE_ACTIVATION_ARTIFACT_ERROR


def evaluate_active_gate(
    request: ActivationRequest,
    *,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionActivationActiveGateAssessment:
    """Evaluate active gate preconditions without transitioning to active."""
    blocking: list[str] = []
    current_state = request.state

    if current_state != ACTIVATION_STATE_ARMED:
        blocking.append(BLOCK_WRONG_STATE)

    armed_not_expired = not _is_arm_expired(request, now=now)
    if not armed_not_expired:
        blocking.append(BLOCK_ARMED_TTL_EXPIRED)

    approval_quorum_valid = _approval_quorum_valid(request)
    if not approval_quorum_valid:
        blocking.append(BLOCK_QUORUM_INVALID)

    executor_valid = _executor_valid(request)
    if not executor_valid:
        blocking.append(BLOCK_EXECUTOR_INVALID)

    phrase_verified = bool(request.phrase_verified)
    if not phrase_verified:
        blocking.append(BLOCK_PHRASE_NOT_VERIFIED)

    tested_commit_matches = _head_sha_matches(request, repo_root=repo_root)
    if not tested_commit_matches:
        blocking.append(BLOCK_HEAD_SHA_MISMATCH)

    release_tag_valid = _release_tag_valid(request)
    if not release_tag_valid:
        blocking.append(BLOCK_RELEASE_TAG_INVALID)

    rollback_commit_present = _rollback_present(request)
    if not rollback_commit_present:
        blocking.append(BLOCK_ROLLBACK_MISSING)

    attestation_valid = _attestation_valid(request)
    if not attestation_valid:
        blocking.append(BLOCK_ATTESTATION_INVALID)

    recovery_required = _probe_recovery_required(request)
    recovery_clear = not recovery_required
    if not recovery_clear:
        blocking.append(BLOCK_RECOVERY_REQUIRED)

    repair_lock_held = _probe_repair_lock_held(request)
    repair_lock_clear = not repair_lock_held
    if not repair_lock_clear:
        blocking.append(BLOCK_REPAIR_LOCK_HELD)

    regression_clear = _probe_regression_clear()
    if not regression_clear:
        blocking.append(BLOCK_REGRESSION_FAIL)

    signoff_ready = _probe_signoff_ready(merged_config=merged_config)
    if not signoff_ready:
        blocking.append(BLOCK_SIGNOFF_NOT_READY)

    cutover_ready = _probe_cutover_ready(merged_config=merged_config)
    if not cutover_ready:
        blocking.append(BLOCK_CUTOVER_NOT_READY)

    audit_store_available = probe_audit_store_available(store_dir=store_dir)
    if not audit_store_available:
        blocking.append(BLOCK_AUDIT_STORE_UNAVAILABLE)

    kill_switch_available = is_kill_switch_available(request, store_dir=store_dir)
    if not kill_switch_available:
        blocking.append(BLOCK_KILL_SWITCH_UNAVAILABLE)

    gate_ready = current_state == ACTIVATION_STATE_ARMED and not blocking
    assessment = ProductionActivationActiveGateAssessment(
        activation_request_id=request.activation_request_id,
        current_state=current_state,
        gate_ready=gate_ready,
        armed_not_expired=armed_not_expired,
        approval_quorum_valid=approval_quorum_valid,
        executor_valid=executor_valid,
        phrase_verified=phrase_verified,
        tested_commit_matches=tested_commit_matches,
        release_tag_valid=release_tag_valid,
        rollback_commit_present=rollback_commit_present,
        attestation_valid=attestation_valid,
        recovery_clear=recovery_clear,
        repair_lock_clear=repair_lock_clear,
        regression_clear=regression_clear,
        signoff_ready=signoff_ready,
        cutover_ready=cutover_ready,
        kill_switch_available=kill_switch_available,
        audit_store_available=audit_store_available,
        blocking_reasons=tuple(blocking),
    )
    return replace(
        assessment,
        recommended_action=_resolve_recommended_action(request, assessment),
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
        raise ProductionActivationActiveGateError(str(exc)) from exc
    except ProductionActivationStateError as exc:
        raise ProductionActivationActiveGateError(str(exc)) from exc


def _assert_safe_output(output: str) -> None:
    lowered = output.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationActiveGateError(
                f"Unsafe active gate output field: {token!r}"
            )


def format_active_gate_assessment(
    assessment: ProductionActivationActiveGateAssessment,
) -> str:
    reasons = ", ".join(assessment.blocking_reasons) if assessment.blocking_reasons else "(none)"
    lines = [
        "Production Activation Active Gate",
        "",
        f"activation_request_id: {assessment.activation_request_id}",
        f"current_state: {assessment.current_state}",
        f"gate_ready: {str(assessment.gate_ready).lower()}",
        f"kill_switch_available: {str(assessment.kill_switch_available).lower()}",
        f"suspended: {str(assessment.current_state == ACTIVATION_STATE_SUSPENDED).lower()}",
        f"revoked: {str(assessment.current_state == ACTIVATION_STATE_REVOKED).lower()}",
        f"blocking_reasons: {reasons}",
        f"recommended_action: {assessment.recommended_action}",
        "",
        "[Checks]",
        f"armed_not_expired: {str(assessment.armed_not_expired).lower()}",
        f"approval_quorum_valid: {str(assessment.approval_quorum_valid).lower()}",
        f"executor_valid: {str(assessment.executor_valid).lower()}",
        f"phrase_verified: {str(assessment.phrase_verified).lower()}",
        f"tested_commit_matches: {str(assessment.tested_commit_matches).lower()}",
        f"recovery_clear: {str(assessment.recovery_clear).lower()}",
        f"repair_lock_clear: {str(assessment.repair_lock_clear).lower()}",
        f"regression_clear: {str(assessment.regression_clear).lower()}",
        f"signoff_ready: {str(assessment.signoff_ready).lower()}",
        f"cutover_ready: {str(assessment.cutover_ready).lower()}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def evaluate_and_record_active_gate(
    *,
    activation_request_id: str,
    store_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionActivationActiveGateAssessment:
    request = refresh_activation_lifecycle(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        now=now,
    )
    assessment = evaluate_active_gate(
        request,
        repo_root=repo_root,
        store_dir=store_dir,
        merged_config=merged_config,
        now=now,
    )
    try:
        updated = request
        for event in (
            build_control_event(
                request,
                event_type=EVENT_KILL_SWITCH_CHECKED,
                from_state=request.state,
                to_state=request.state,
                actor_id="activation-gate",
                actor_role=ROLE_OPERATOR,
                reason_code="kill_switch_checked",
                now=now,
            ),
            build_control_event(
                request,
                event_type=EVENT_ACTIVE_GATE_EVALUATED,
                from_state=request.state,
                to_state=request.state,
                actor_id="activation-gate",
                actor_role=ROLE_OPERATOR,
                reason_code="gate_ready" if assessment.gate_ready else "gate_blocked",
                now=now,
            ),
        ):
            updated = append_control_event(updated, event)
        persist_activation_request(updated, store_dir=store_dir)
    except ProductionActivationKillSwitchError as exc:
        raise ProductionActivationActiveGateError(str(exc)) from exc
    return assessment


def run_activation_gate(
    *,
    activation_request_id: str,
    store_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    assessment = evaluate_and_record_active_gate(
        activation_request_id=activation_request_id,
        store_dir=store_dir,
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )
    exit_code = 0 if assessment.gate_ready else 1
    return format_active_gate_assessment(assessment), exit_code
