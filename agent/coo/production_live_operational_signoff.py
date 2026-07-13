"""Production live pilot operational sign-off — Phase 14H-3E.

Read-only cross-reference of live pilot artifacts plus append-only operator sign-off.
No subprocess, Repository2 original execution, or production permission grants.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_RECOVERY_REQUIRED,
    CONSUME_STATE_UNCONSUMED,
    assess_consume_status,
)
from agent.coo.dispatch_execution_audit import default_audit_dir
from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_ENABLED,
    load_dispatch_gateway_enablement,
)
from agent.coo.production_activation_active_gate import (
    _probe_recovery_required,
    _probe_repair_lock_held,
)
from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_COMPLETED,
    RESERVATION_STATE_FAILED,
    RESERVATION_STATE_STARTED,
    ProductionActivationExecutionReservation,
    ProductionActivationExecutionReservationError,
    load_execution_reservation,
)
from agent.coo.production_activation_live_e2e import (
    _EVENT_RUNTIME_COMPLETED,
    correlate_live_pilot_evidence_and_audit,
    default_e2e_history_dir,
    derive_live_pilot_dispatch_run_id,
    load_e2e_finalization_state,
    load_live_pilot_dispatch_audit,
    load_live_pilot_evidence,
)
from agent.coo.production_activation_live_runtime import load_runtime_records
from agent.coo.production_activation_state import (
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
    ActivationRequest,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    load_activation_request,
)
from agent.coo.production_executor_factory import default_evidence_dir
from hermes_constants import get_hermes_home

_SIGNOFF_STORE_DIR = "production-live-signoff"
_SIGNOFF_STORE_VERSION = 1
_FIRST_RUN_SCAN_LIMIT = 500
_PRODUCTION_ROOT_TOUCHED_SENTINEL = ".production-root-touched"

SIGNOFF_READY = "SIGNOFF_READY"
SIGNOFF_READY_WITH_WARNINGS = "SIGNOFF_READY_WITH_WARNINGS"
SIGNOFF_BLOCKED = "SIGNOFF_BLOCKED"
SIGNOFF_REQUIRES_RECOVERY = "SIGNOFF_REQUIRES_RECOVERY"

WARN_FIRST_SUPERVISED_RUN = "first_supervised_run"
WARN_LOCAL_OUTPUT_ONLY = "local_output_only"
WARN_EXTERNAL_PUBLISH_DISABLED = "external_publish_disabled"
WARN_PRODUCTION_ROOT_HARD_DENIED = "production_root_hard_denied"
WARN_GATEWAY_PRODUCTION_DISABLED = "gateway_production_disabled"
WARN_DISCORD_PRODUCTION_DISABLED = "discord_production_disabled"
WARN_RELEASE_TAG_NOT_PUSHED = "release_tag_not_pushed"
WARN_MANUAL_REVIEW_REQUIRED = "manual_review_required"
WARN_REPOSITORY2_ORIGINAL_NOT_EXECUTED = "repository2_original_not_executed"

BLOCK_RUNTIME_NOT_COMPLETED = "runtime_not_completed"
BLOCK_RUNTIME_FAILED = "runtime_failed"
BLOCK_RUNTIME_TIMEOUT = "runtime_timeout"
BLOCK_SOURCE_TREE_MUTATED = "source_tree_mutated"
BLOCK_PUBLISH_ATTEMPT_DETECTED = "publish_attempt_detected"
BLOCK_EVIDENCE_MISSING = "evidence_missing"
BLOCK_DISPATCH_AUDIT_MISSING = "dispatch_audit_missing"
BLOCK_CORRELATION_INVALID = "correlation_invalid"
BLOCK_CONSUME_NOT_COMMITTED = "consume_not_committed"
BLOCK_RECOVERY_REQUIRED = "recovery_required"
BLOCK_REPAIR_LOCK_HELD = "repair_lock_held"
BLOCK_ACTIVATION_NOT_REVOKED = "activation_not_revoked"
BLOCK_RESERVATION_NOT_COMPLETED = "reservation_not_completed"
BLOCK_E2E_NOT_FINALIZED = "e2e_not_finalized"
BLOCK_PRODUCTION_ROOT_TOUCHED = "production_root_touched"
BLOCK_ROLLBACK_NOT_READY = "rollback_not_ready"
BLOCK_ARTIFACT_CORRUPTED = "artifact_corrupted"
BLOCK_CHECKLIST_FAILED = "checklist_failed"
BLOCK_SIGNER_IDENTITY_CONFLICT = "signer_identity_conflict"
BLOCK_SIGNOFF_ALREADY_MISMATCH = "signoff_corruption"

ACTION_SIGNOFF_COMPLETE = "production_live_pilot_signoff_complete"
ACTION_REVIEW_FIRST_RUN = "review_first_run_artifacts"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_INSPECT_RUNTIME_FAILURE = "inspect_runtime_failure"
ACTION_INSPECT_EVIDENCE_FAILURE = "inspect_evidence_failure"
ACTION_INSPECT_AUDIT_FAILURE = "inspect_audit_failure"
ACTION_RESOLVE_CORRELATION_MISMATCH = "resolve_correlation_mismatch"
ACTION_REVOKE_ACTIVATION_MANUALLY = "revoke_activation_manually"
ACTION_CREATE_NEW_ACTIVATION = "create_new_activation_proposal"
ACTION_PREPARE_PHASE_14I = "prepare_phase_14i_audit_rollback_validation"

RELEASE_LIVE_PILOT_VALIDATED = "LIVE_PILOT_VALIDATED"
RELEASE_LIVE_PILOT_VALIDATED_WITH_WARNINGS = "LIVE_PILOT_VALIDATED_WITH_WARNINGS"
RELEASE_LIVE_PILOT_NOT_READY = "LIVE_PILOT_NOT_READY"
RELEASE_LIVE_PILOT_RECOVERY_REQUIRED = "LIVE_PILOT_RECOVERY_REQUIRED"

NEXT_PHASE_14I = "Phase_14I_audit_rollback_validation"

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
        "rollback_commit",
        "attestation_hash",
    }
)


class ProductionLiveOperationalSignoffError(ValueError):
    """Raised when operational sign-off cannot proceed safely."""


@dataclass(frozen=True)
class ProductionFirstRunOperatorChecklist:
    tested_commit_matches: bool
    release_tag_present: bool
    working_tree_clean: bool
    activation_was_active: bool
    activation_now_revoked: bool
    reservation_completed: bool
    runtime_success: bool
    timeout_false: bool
    source_unchanged: bool
    publish_false: bool
    local_output_only: bool
    evidence_present: bool
    audit_present: bool
    correlation_valid: bool
    consume_committed: bool
    recovery_clear: bool
    repair_lock_clear: bool
    rollback_commit_present: bool
    production_root_untouched: bool
    isolated_mirror_used: bool
    one_shot_enforced: bool
    production_execution_allowed_false: bool = True

    @property
    def passed(self) -> bool:
        return all(
            (
                self.tested_commit_matches,
                self.release_tag_present,
                self.working_tree_clean,
                self.activation_was_active,
                self.activation_now_revoked,
                self.reservation_completed,
                self.runtime_success,
                self.timeout_false,
                self.source_unchanged,
                self.publish_false,
                self.local_output_only,
                self.evidence_present,
                self.audit_present,
                self.correlation_valid,
                self.consume_committed,
                self.recovery_clear,
                self.repair_lock_clear,
                self.rollback_commit_present,
                self.production_root_untouched,
                self.isolated_mirror_used,
                self.one_shot_enforced,
                self.production_execution_allowed_false,
            )
        )


@dataclass(frozen=True)
class ProductionLiveOperationalSignoffSummary:
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    signoff_status: str
    first_run_detected: bool
    runtime_completed: bool
    runtime_exit_code: int
    runtime_timed_out: bool
    source_tree_unchanged: bool
    publish_attempted: bool
    evidence_present: bool
    dispatch_audit_present: bool
    evidence_audit_correlation_valid: bool
    consume_state: str
    consume_committed: bool
    activation_state: str
    activation_revoked: bool
    reservation_state: str
    e2e_finalized: bool
    recovery_required: bool
    repair_lock_held: bool
    production_root_untouched: bool
    isolated_mirror_only: bool
    draft_only: bool
    external_publish_attempted: bool
    rollback_ready: bool
    operator_checklist_passed: bool
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str
    production_execution_allowed: bool = False
    original_repository2_execution_attempted: bool = False
    signer_present: bool = False
    already_signed_off: bool = False


@dataclass(frozen=True)
class ProductionLiveOperationalSignoffRecord:
    signoff_id: str
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    signoff_status: str
    signed_by: str
    signed_at: str
    checklist_passed: bool
    blocking_item_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    tested_commit_sha: str
    release_tag: str
    rollback_commit_present: bool
    production_execution_allowed: bool = False
    original_repository2_execution_attempted: bool = False
    external_publish_allowed: bool = False


@dataclass(frozen=True)
class ProductionLiveReleaseValidationSummary:
    validated_head: str
    release_tag: str
    activation_request_id: str
    signoff_status: str
    first_run_complete: bool
    local_output_validated: bool
    external_publish_enabled: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    original_repository2_execution_enabled: bool = False
    production_root_hard_deny: bool = True
    rollback_ready: bool = False
    next_phase: str = NEXT_PHASE_14I
    release_candidate_status: str = RELEASE_LIVE_PILOT_NOT_READY


@dataclass(frozen=True)
class ProductionLivePilotDashboardDigest:
    live_pilot_status: str
    live_pilot_signoff_status: str
    latest_activation_request_id: str
    latest_execution_attempt_id: str
    consume_state: str
    activation_state: str
    recovery_required: bool
    recommended_action: str


def default_signoff_store_dir() -> Path:
    return get_hermes_home() / "coo" / _SIGNOFF_STORE_DIR


def _utc_now_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.isoformat()


def _signoff_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionLiveOperationalSignoffError("activation_request_id is required")
    base = (store_dir or default_signoff_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionLiveOperationalSignoffError(
            "Signoff store must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_signoff_store_available(*, store_dir: Path | None = None) -> bool:
    try:
        base = (store_dir or default_signoff_store_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _probe_production_root_touched(*, store_dir: Path | None = None) -> bool:
    base = (store_dir or default_signoff_store_dir()).resolve()
    return (base / _PRODUCTION_ROOT_TOUCHED_SENTINEL).is_file()


def _activation_was_active(request: ActivationRequest) -> bool:
    if request.active_at:
        return True
    for transition in request.state_history:
        if transition.to_state == ACTIVATION_STATE_ACTIVE:
            return True
    for event in request.control_history:
        if event.to_state == ACTIVATION_STATE_ACTIVE:
            return True
    return False


def _scan_finalized_e2e_count(
    *,
    e2e_history_dir: Path | None = None,
    scan_limit: int = _FIRST_RUN_SCAN_LIMIT,
) -> tuple[int, bool]:
    base = (e2e_history_dir or default_e2e_history_dir()).resolve()
    if not base.is_dir():
        return 0, False
    paths = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    count = 0
    corrupted = False
    for path in paths[:scan_limit]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            corrupted = True
            continue
        if not isinstance(payload, dict):
            corrupted = True
            continue
        finalization = payload.get("finalization")
        if not isinstance(finalization, dict):
            continue
        if bool(finalization.get("e2e_finalized", False)):
            count += 1
    return count, corrupted


def _runtime_completion_state(
    activation_request_id: str,
    execution_attempt_id: str,
    *,
    runtime_history_dir: Path | None = None,
) -> tuple[bool, int, bool, bool, bool]:
    try:
        records = load_runtime_records(
            activation_request_id,
            history_dir=runtime_history_dir,
        )
    except Exception:
        raise ProductionLiveOperationalSignoffError("runtime audit corrupted") from None
    matches = [
        record
        for record in records
        if record.event_type == _EVENT_RUNTIME_COMPLETED
        and record.execution_attempt_id == execution_attempt_id
    ]
    if len(matches) > 1:
        raise ProductionLiveOperationalSignoffError("runtime audit corrupted")
    if not matches:
        return False, 0, False, False, False
    record = matches[0]
    source_unchanged = (
        record.exit_code == 0
        and not record.timed_out
        and not record.publish_attempted
    )
    return (
        True,
        record.exit_code,
        record.timed_out,
        record.publish_attempted,
        source_unchanged,
    )


def load_operational_signoff_record(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionLiveOperationalSignoffRecord | None:
    path = _signoff_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionLiveOperationalSignoffError("signoff artifact corrupted") from exc
    signoff = payload.get("signoff")
    if not isinstance(signoff, dict):
        raise ProductionLiveOperationalSignoffError("signoff artifact corrupted")
    return ProductionLiveOperationalSignoffRecord(
        signoff_id=str(signoff.get("signoff_id", "")),
        activation_request_id=str(signoff.get("activation_request_id", "")),
        reservation_id=str(signoff.get("reservation_id", "")),
        execution_attempt_id=str(signoff.get("execution_attempt_id", "")),
        dispatch_run_id=str(signoff.get("dispatch_run_id", "")),
        signoff_status=str(signoff.get("signoff_status", "")),
        signed_by=str(signoff.get("signed_by", "")),
        signed_at=str(signoff.get("signed_at", "")),
        checklist_passed=bool(signoff.get("checklist_passed", False)),
        blocking_item_codes=tuple(signoff.get("blocking_item_codes") or ()),
        warning_codes=tuple(signoff.get("warning_codes") or ()),
        tested_commit_sha=str(signoff.get("tested_commit_sha", "")),
        release_tag=str(signoff.get("release_tag", "")),
        rollback_commit_present=bool(signoff.get("rollback_commit_present", False)),
        production_execution_allowed=False,
        original_repository2_execution_attempted=False,
        external_publish_allowed=False,
    )


def _write_signoff_record(
    record: ProductionLiveOperationalSignoffRecord,
    *,
    store_dir: Path | None = None,
) -> None:
    path = _signoff_path(record.activation_request_id, store_dir=store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_operational_signoff_record(
        record.activation_request_id,
        store_dir=store_dir,
    )
    if existing is not None:
        if (
            existing.reservation_id == record.reservation_id
            and existing.execution_attempt_id == record.execution_attempt_id
            and existing.signoff_status == record.signoff_status
        ):
            return
        raise ProductionLiveOperationalSignoffError("signoff_corruption")
    payload = {
        "version": _SIGNOFF_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "signoff": {
            "signoff_id": record.signoff_id,
            "activation_request_id": record.activation_request_id,
            "reservation_id": record.reservation_id,
            "execution_attempt_id": record.execution_attempt_id,
            "dispatch_run_id": record.dispatch_run_id,
            "signoff_status": record.signoff_status,
            "signed_by": record.signed_by,
            "signed_at": record.signed_at,
            "checklist_passed": record.checklist_passed,
            "blocking_item_codes": list(record.blocking_item_codes),
            "warning_codes": list(record.warning_codes),
            "tested_commit_sha": record.tested_commit_sha,
            "release_tag": record.release_tag,
            "rollback_commit_present": record.rollback_commit_present,
            "production_execution_allowed": False,
            "original_repository2_execution_attempted": False,
            "external_publish_allowed": False,
        },
    }
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise ProductionLiveOperationalSignoffError("signoff_write_failed") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _recommended_action(
    status: str,
    blocking: tuple[str, ...],
) -> str:
    if status in {SIGNOFF_READY, SIGNOFF_READY_WITH_WARNINGS}:
        return ACTION_SIGNOFF_COMPLETE
    if status == SIGNOFF_REQUIRES_RECOVERY:
        return ACTION_RUN_CONSUME_RECOVERY
    if BLOCK_RUNTIME_FAILED in blocking or BLOCK_RUNTIME_NOT_COMPLETED in blocking:
        return ACTION_INSPECT_RUNTIME_FAILURE
    if BLOCK_EVIDENCE_MISSING in blocking:
        return ACTION_INSPECT_EVIDENCE_FAILURE
    if BLOCK_DISPATCH_AUDIT_MISSING in blocking:
        return ACTION_INSPECT_AUDIT_FAILURE
    if BLOCK_CORRELATION_INVALID in blocking:
        return ACTION_RESOLVE_CORRELATION_MISMATCH
    if BLOCK_ACTIVATION_NOT_REVOKED in blocking:
        return ACTION_REVOKE_ACTIVATION_MANUALLY
    if BLOCK_RESERVATION_NOT_COMPLETED in blocking:
        return ACTION_CREATE_NEW_ACTIVATION
    if status == SIGNOFF_BLOCKED and blocking:
        return ACTION_MAINTAIN_PRODUCTION_BLOCK
    return ACTION_REVIEW_FIRST_RUN


def evaluate_production_live_operational_signoff(
    *,
    activation_request_id: str,
    reservation_id: str,
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
    merged_config: Mapping[str, Any] | None = None,
) -> ProductionLiveOperationalSignoffSummary:
    """Read-only operational sign-off assessment for one live pilot activation."""
    blocking: list[str] = []
    warnings: list[str] = []

    try:
        request = load_activation_request(activation_request_id, store_dir=store_dir)
    except ProductionActivationStoreError as exc:
        raise ProductionLiveOperationalSignoffError(str(exc)) from exc

    reservation = load_execution_reservation(
        activation_request_id,
        store_dir=reservation_dir,
    )
    if reservation is None:
        blocking.append(BLOCK_ARTIFACT_CORRUPTED)
        reservation = ProductionActivationExecutionReservation(
            reservation_id="",
            activation_request_id=activation_request_id,
            ticket_id="",
            confirmation_id="",
            execution_attempt_id="",
            execution_gate_event_id="",
            dry_run_event_id="",
            state="",
            reserved_at="",
        )
    elif reservation.reservation_id != reservation_id:
        blocking.append(BLOCK_ARTIFACT_CORRUPTED)

    dispatch_run_id = ""
    if reservation.execution_attempt_id:
        dispatch_run_id = derive_live_pilot_dispatch_run_id(
            reservation.execution_attempt_id
        )

    finalized_count, e2e_scan_corrupted = _scan_finalized_e2e_count(
        e2e_history_dir=e2e_history_dir,
    )
    if e2e_scan_corrupted:
        blocking.append(BLOCK_ARTIFACT_CORRUPTED)
    first_run_detected = finalized_count == 1
    if first_run_detected:
        warnings.append(WARN_FIRST_SUPERVISED_RUN)

    existing_signoff = load_operational_signoff_record(
        activation_request_id,
        store_dir=signoff_store_dir,
    )
    already_signed_off = False
    if existing_signoff is not None:
        if (
            existing_signoff.reservation_id == reservation.reservation_id
            and existing_signoff.execution_attempt_id == reservation.execution_attempt_id
        ):
            already_signed_off = True
        else:
            blocking.append(BLOCK_SIGNOFF_ALREADY_MISMATCH)

    runtime_completed = False
    runtime_exit_code = 0
    runtime_timed_out = False
    publish_attempted = False
    source_tree_unchanged = False
    if reservation.execution_attempt_id:
        try:
            (
                runtime_completed,
                runtime_exit_code,
                runtime_timed_out,
                publish_attempted,
                source_tree_unchanged,
            ) = _runtime_completion_state(
                activation_request_id,
                reservation.execution_attempt_id,
                runtime_history_dir=runtime_history_dir,
            )
        except ProductionLiveOperationalSignoffError:
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)

    if not runtime_completed:
        blocking.append(BLOCK_RUNTIME_NOT_COMPLETED)
    elif runtime_exit_code != 0:
        blocking.append(BLOCK_RUNTIME_FAILED)
    if runtime_timed_out:
        blocking.append(BLOCK_RUNTIME_TIMEOUT)
    if publish_attempted:
        blocking.append(BLOCK_PUBLISH_ATTEMPT_DETECTED)
    if runtime_completed and not source_tree_unchanged:
        blocking.append(BLOCK_SOURCE_TREE_MUTATED)

    evidence = None
    audit = None
    if reservation.execution_attempt_id:
        try:
            evidence = load_live_pilot_evidence(
                reservation.execution_attempt_id,
                evidence_dir=evidence_dir,
            )
        except Exception:
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)
        try:
            audit = load_live_pilot_dispatch_audit(
                dispatch_run_id,
                audit_dir=audit_dir,
            )
        except Exception:
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)

    evidence_present = evidence is not None
    audit_present = audit is not None
    if not evidence_present:
        blocking.append(BLOCK_EVIDENCE_MISSING)
    if not audit_present:
        blocking.append(BLOCK_DISPATCH_AUDIT_MISSING)

    correlation_valid = False
    if evidence is not None and audit is not None and reservation.reservation_id:
        correlation_valid = correlate_live_pilot_evidence_and_audit(
            evidence,
            audit,
            reservation=reservation,
        )
        if not correlation_valid:
            blocking.append(BLOCK_CORRELATION_INVALID)

    consume_status = assess_consume_status(
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
    )
    consume_committed = consume_status.consume_state == CONSUME_STATE_COMMITTED
    recovery_required = (
        consume_status.recovery_required
        or _probe_recovery_required(request)
        or consume_status.consume_state
        in {
            CONSUME_STATE_PARTIAL,
            CONSUME_STATE_PREPARED,
            CONSUME_STATE_RECOVERY_REQUIRED,
        }
    )
    repair_lock_held = _probe_repair_lock_held(request)

    if recovery_required:
        blocking.append(BLOCK_RECOVERY_REQUIRED)
    if repair_lock_held:
        blocking.append(BLOCK_REPAIR_LOCK_HELD)
    if not consume_committed:
        blocking.append(BLOCK_CONSUME_NOT_COMMITTED)

    if reservation.state != RESERVATION_STATE_COMPLETED:
        if reservation.state == RESERVATION_STATE_FAILED:
            blocking.append(BLOCK_RUNTIME_FAILED)
        elif reservation.state == RESERVATION_STATE_STARTED:
            blocking.append(BLOCK_RUNTIME_NOT_COMPLETED)
        else:
            blocking.append(BLOCK_RESERVATION_NOT_COMPLETED)

    finalization = load_e2e_finalization_state(
        activation_request_id,
        history_dir=e2e_history_dir,
    )
    e2e_finalized = finalization.e2e_finalized
    if not e2e_finalized:
        blocking.append(BLOCK_E2E_NOT_FINALIZED)

    activation_revoked = request.state == ACTIVATION_STATE_REVOKED
    if consume_committed and not activation_revoked:
        blocking.append(BLOCK_ACTIVATION_NOT_REVOKED)

    production_root_touched = _probe_production_root_touched(
        store_dir=signoff_store_dir,
    )
    if production_root_touched or reservation.repository2_execution_attempted:
        blocking.append(BLOCK_PRODUCTION_ROOT_TOUCHED)

    rollback_ready = bool(request.rollback_commit and request.tested_commit_sha)
    if not rollback_ready:
        blocking.append(BLOCK_ROLLBACK_NOT_READY)

    enablement = load_dispatch_gateway_enablement(merged_config=merged_config or {})
    warnings.append(WARN_EXTERNAL_PUBLISH_DISABLED)
    warnings.append(WARN_LOCAL_OUTPUT_ONLY)
    warnings.append(WARN_PRODUCTION_ROOT_HARD_DENIED)
    warnings.append(WARN_REPOSITORY2_ORIGINAL_NOT_EXECUTED)
    if enablement.gateway_state != GATEWAY_STATE_ENABLED:
        warnings.append(WARN_GATEWAY_PRODUCTION_DISABLED)
    warnings.append(WARN_DISCORD_PRODUCTION_DISABLED)
    if request.release_tag:
        warnings.append(WARN_RELEASE_TAG_NOT_PUSHED)
    warnings.append(WARN_MANUAL_REVIEW_REQUIRED)

    checklist = ProductionFirstRunOperatorChecklist(
        tested_commit_matches=bool(request.tested_commit_sha),
        release_tag_present=bool(request.release_tag),
        working_tree_clean=bool(request.tested_commit_sha and request.rollback_commit),
        activation_was_active=_activation_was_active(request),
        activation_now_revoked=activation_revoked,
        reservation_completed=reservation.state == RESERVATION_STATE_COMPLETED,
        runtime_success=runtime_completed and runtime_exit_code == 0,
        timeout_false=not runtime_timed_out,
        source_unchanged=source_tree_unchanged,
        publish_false=not publish_attempted,
        local_output_only=True,
        evidence_present=evidence_present,
        audit_present=audit_present,
        correlation_valid=correlation_valid,
        consume_committed=consume_committed,
        recovery_clear=not recovery_required,
        repair_lock_clear=not repair_lock_held,
        rollback_commit_present=bool(request.rollback_commit),
        production_root_untouched=not production_root_touched,
        isolated_mirror_used=bool(
            evidence is not None and evidence.isolated_mirror_runtime_invoked
        ),
        one_shot_enforced=request.activation_scope.scope_type == ACTIVATION_SCOPE_ONE_SHOT,
    )
    if not checklist.passed:
        if consume_status.consume_state not in {
            CONSUME_STATE_PARTIAL,
            CONSUME_STATE_PREPARED,
            CONSUME_STATE_RECOVERY_REQUIRED,
        }:
            blocking.append(BLOCK_CHECKLIST_FAILED)

    unique_blocking = tuple(dict.fromkeys(blocking))
    unique_warnings = tuple(dict.fromkeys(warnings))

    hard_blocks = [
        code
        for code in unique_blocking
        if code
        not in {
            BLOCK_RECOVERY_REQUIRED,
            BLOCK_REPAIR_LOCK_HELD,
            BLOCK_CONSUME_NOT_COMMITTED,
        }
    ]
    if hard_blocks:
        signoff_status = SIGNOFF_BLOCKED
    elif (
        consume_status.consume_state
        in {
            CONSUME_STATE_PARTIAL,
            CONSUME_STATE_PREPARED,
            CONSUME_STATE_RECOVERY_REQUIRED,
        }
        or BLOCK_RECOVERY_REQUIRED in unique_blocking
        or BLOCK_REPAIR_LOCK_HELD in unique_blocking
        or BLOCK_CONSUME_NOT_COMMITTED in unique_blocking
    ):
        signoff_status = SIGNOFF_REQUIRES_RECOVERY
    elif unique_warnings:
        signoff_status = SIGNOFF_READY_WITH_WARNINGS
    else:
        signoff_status = SIGNOFF_READY

    return ProductionLiveOperationalSignoffSummary(
        activation_request_id=activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        signoff_status=signoff_status,
        first_run_detected=first_run_detected,
        runtime_completed=runtime_completed,
        runtime_exit_code=runtime_exit_code,
        runtime_timed_out=runtime_timed_out,
        source_tree_unchanged=source_tree_unchanged,
        publish_attempted=publish_attempted,
        evidence_present=evidence_present,
        dispatch_audit_present=audit_present,
        evidence_audit_correlation_valid=correlation_valid,
        consume_state=consume_status.consume_state,
        consume_committed=consume_committed,
        activation_state=request.state,
        activation_revoked=activation_revoked,
        reservation_state=reservation.state,
        e2e_finalized=e2e_finalized,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        production_root_untouched=not production_root_touched,
        isolated_mirror_only=True,
        draft_only=True,
        external_publish_attempted=False,
        rollback_ready=rollback_ready,
        operator_checklist_passed=checklist.passed,
        blocking_items=unique_blocking,
        warning_items=unique_warnings,
        recommended_action=_recommended_action(signoff_status, unique_blocking),
        already_signed_off=already_signed_off,
    )


def record_production_live_operational_signoff(
    *,
    activation_request_id: str,
    reservation_id: str,
    operator_id: str,
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
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionLiveOperationalSignoffSummary:
    """Append operator sign-off artifact when assessment is ready."""
    if not probe_signoff_store_available(store_dir=signoff_store_dir):
        raise ProductionLiveOperationalSignoffError("signoff_write_failed")

    signed_by = (operator_id or "").strip()
    if not signed_by:
        raise ProductionLiveOperationalSignoffError("operator_id is required")

    summary = evaluate_production_live_operational_signoff(
        activation_request_id=activation_request_id,
        reservation_id=reservation_id,
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
        merged_config=merged_config,
    )

    if summary.already_signed_off:
        return summary

    if summary.signoff_status not in {
        SIGNOFF_READY,
        SIGNOFF_READY_WITH_WARNINGS,
    }:
        raise ProductionLiveOperationalSignoffError(
            f"signoff blocked for status {summary.signoff_status!r}"
        )

    request = load_activation_request(activation_request_id, store_dir=store_dir)
    if signed_by == (request.executor_id or "").strip():
        raise ProductionLiveOperationalSignoffError("signer_identity_conflict")

    reservation = load_execution_reservation(
        activation_request_id,
        store_dir=reservation_dir,
    )
    if reservation is None or reservation.reservation_id != reservation_id:
        raise ProductionLiveOperationalSignoffError("reservation_scope_mismatch")

    record = ProductionLiveOperationalSignoffRecord(
        signoff_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=summary.execution_attempt_id,
        dispatch_run_id=summary.dispatch_run_id,
        signoff_status=summary.signoff_status,
        signed_by=signed_by,
        signed_at=_utc_now_iso(now),
        checklist_passed=summary.operator_checklist_passed,
        blocking_item_codes=summary.blocking_items,
        warning_codes=summary.warning_items,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        rollback_commit_present=bool(request.rollback_commit),
        production_execution_allowed=False,
        original_repository2_execution_attempted=False,
        external_publish_allowed=False,
    )
    _write_signoff_record(record, store_dir=signoff_store_dir)
    return evaluate_production_live_operational_signoff(
        activation_request_id=activation_request_id,
        reservation_id=reservation_id,
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
        merged_config=merged_config,
    )


def build_production_live_release_validation_summary(
    summary: ProductionLiveOperationalSignoffSummary,
    *,
    request: ActivationRequest | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> ProductionLiveReleaseValidationSummary:
    """Build release validation summary from sign-off assessment."""
    enablement = load_dispatch_gateway_enablement(merged_config=merged_config or {})
    validated_head = ""
    release_tag = ""
    if request is not None:
        validated_head = request.tested_commit_sha
        release_tag = request.release_tag

    if summary.signoff_status == SIGNOFF_REQUIRES_RECOVERY:
        candidate = RELEASE_LIVE_PILOT_RECOVERY_REQUIRED
    elif summary.signoff_status in {SIGNOFF_READY, SIGNOFF_READY_WITH_WARNINGS}:
        candidate = (
            RELEASE_LIVE_PILOT_VALIDATED_WITH_WARNINGS
            if summary.warning_items
            else RELEASE_LIVE_PILOT_VALIDATED
        )
    else:
        candidate = RELEASE_LIVE_PILOT_NOT_READY

    return ProductionLiveReleaseValidationSummary(
        validated_head=validated_head,
        release_tag=release_tag,
        activation_request_id=summary.activation_request_id,
        signoff_status=summary.signoff_status,
        first_run_complete=summary.first_run_detected and summary.e2e_finalized,
        local_output_validated=summary.draft_only and summary.source_tree_unchanged,
        external_publish_enabled=False,
        gateway_production_enabled=enablement.gateway_state == GATEWAY_STATE_ENABLED,
        discord_production_enabled=False,
        original_repository2_execution_enabled=False,
        production_root_hard_deny=enablement.production_root_hard_deny,
        rollback_ready=summary.rollback_ready,
        next_phase=NEXT_PHASE_14I,
        release_candidate_status=candidate,
    )


def resolve_latest_live_pilot_dashboard_digest(
    *,
    e2e_history_dir: Path | None = None,
    signoff_store_dir: Path | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    runtime_history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> ProductionLivePilotDashboardDigest:
    """Read-only digest of the newest finalized live pilot for operator dashboard."""
    base = (e2e_history_dir or default_e2e_history_dir()).resolve()
    if not base.is_dir():
        return ProductionLivePilotDashboardDigest(
            live_pilot_status="not_configured",
            live_pilot_signoff_status="not_configured",
            latest_activation_request_id="",
            latest_execution_attempt_id="",
            consume_state="",
            activation_state="",
            recovery_required=False,
            recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )

    paths = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:_FIRST_RUN_SCAN_LIMIT]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        finalization = payload.get("finalization")
        if not isinstance(finalization, dict) or not finalization.get("e2e_finalized"):
            continue
        activation_id = str(payload.get("activation_request_id", ""))
        reservation_id = ""
        try:
            reservation = load_execution_reservation(
                activation_id,
                store_dir=reservation_dir,
            )
        except ProductionActivationExecutionReservationError:
            continue
        if reservation is None:
            continue
        reservation_id = reservation.reservation_id
        try:
            summary = evaluate_production_live_operational_signoff(
                activation_request_id=activation_id,
                reservation_id=reservation_id,
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
                merged_config=merged_config,
            )
        except ProductionLiveOperationalSignoffError:
            continue
        existing = load_operational_signoff_record(
            activation_id,
            store_dir=signoff_store_dir,
        )
        signoff_status = (
            existing.signoff_status if existing is not None else summary.signoff_status
        )
        return ProductionLivePilotDashboardDigest(
            live_pilot_status=summary.signoff_status,
            live_pilot_signoff_status=signoff_status,
            latest_activation_request_id=activation_id,
            latest_execution_attempt_id=summary.execution_attempt_id,
            consume_state=summary.consume_state,
            activation_state=summary.activation_state,
            recovery_required=summary.recovery_required,
            recommended_action=summary.recommended_action,
        )

    return ProductionLivePilotDashboardDigest(
        live_pilot_status="not_configured",
        live_pilot_signoff_status="not_configured",
        latest_activation_request_id="",
        latest_execution_attempt_id="",
        consume_state="",
        activation_state="",
        recovery_required=False,
        recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
    )


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "production_execution_allowed: false",
        "original_repository2_execution_attempted: false",
        "external_publish_attempted: false",
        "signer_present:",
        "repository2_original_not_executed",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionLiveOperationalSignoffError(
                f"Unsafe operational signoff output field: {token!r}"
            )


def format_production_live_operational_status(
    summary: ProductionLiveOperationalSignoffSummary,
) -> str:
    """Format read-only live pilot operational status."""
    lines = [
        "Production Live Pilot Operational Status",
        "",
        f"activation_request_id: {summary.activation_request_id}",
        f"reservation_id: {summary.reservation_id or '(none)'}",
        f"execution_attempt_id: {summary.execution_attempt_id or '(none)'}",
        f"dispatch_run_id: {summary.dispatch_run_id or '(none)'}",
        f"signoff_status: {summary.signoff_status}",
        f"first_run_detected: {str(summary.first_run_detected).lower()}",
        f"runtime_completed: {str(summary.runtime_completed).lower()}",
        f"runtime_exit_code: {summary.runtime_exit_code}",
        f"runtime_timed_out: {str(summary.runtime_timed_out).lower()}",
        f"source_tree_unchanged: {str(summary.source_tree_unchanged).lower()}",
        f"publish_attempted: {str(summary.publish_attempted).lower()}",
        f"evidence_present: {str(summary.evidence_present).lower()}",
        f"dispatch_audit_present: {str(summary.dispatch_audit_present).lower()}",
        "evidence_audit_correlation_valid: "
        f"{str(summary.evidence_audit_correlation_valid).lower()}",
        f"consume_state: {summary.consume_state or '(none)'}",
        f"consume_committed: {str(summary.consume_committed).lower()}",
        f"activation_state: {summary.activation_state or '(none)'}",
        f"activation_revoked: {str(summary.activation_revoked).lower()}",
        f"reservation_state: {summary.reservation_state or '(none)'}",
        f"e2e_finalized: {str(summary.e2e_finalized).lower()}",
        f"recovery_required: {str(summary.recovery_required).lower()}",
        f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
        f"production_root_untouched: {str(summary.production_root_untouched).lower()}",
        f"isolated_mirror_only: {str(summary.isolated_mirror_only).lower()}",
        f"draft_only: {str(summary.draft_only).lower()}",
        f"external_publish_attempted: {str(summary.external_publish_attempted).lower()}",
        f"rollback_ready: {str(summary.rollback_ready).lower()}",
        f"operator_checklist_passed: {str(summary.operator_checklist_passed).lower()}",
        f"blocking_items_count: {len(summary.blocking_items)}",
        f"warning_items_count: {len(summary.warning_items)}",
        f"blocking_items: {', '.join(summary.blocking_items) if summary.blocking_items else '(none)'}",
        f"warning_items: {', '.join(summary.warning_items) if summary.warning_items else '(none)'}",
        f"recommended_action: {summary.recommended_action}",
        f"already_signed_off: {str(summary.already_signed_off).lower()}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "original_repository2_execution_attempted: false",
        "signer_present: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_live_operational_signoff_result(
    summary: ProductionLiveOperationalSignoffSummary,
    *,
    signer_present: bool = False,
) -> str:
    """Format sign-off command result with signer_present flag only."""
    base = format_production_live_operational_status(summary)
    return base.replace("signer_present: false", f"signer_present: {str(signer_present).lower()}")


def run_activation_live_pilot_status(
    *,
    activation_request_id: str,
    reservation_id: str,
    merged_config: Mapping[str, Any] | None = None,
) -> tuple[str, int]:
    summary = evaluate_production_live_operational_signoff(
        activation_request_id=activation_request_id,
        reservation_id=reservation_id,
        merged_config=merged_config,
    )
    exit_code = 0 if summary.signoff_status in {
        SIGNOFF_READY,
        SIGNOFF_READY_WITH_WARNINGS,
    } else 1
    return format_production_live_operational_status(summary), exit_code


def run_activation_live_pilot_signoff(
    *,
    activation_request_id: str,
    reservation_id: str,
    operator_id: str,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    try:
        summary = record_production_live_operational_signoff(
            activation_request_id=activation_request_id,
            reservation_id=reservation_id,
            operator_id=operator_id,
            merged_config=merged_config,
            now=now,
        )
    except ProductionLiveOperationalSignoffError:
        summary = evaluate_production_live_operational_signoff(
            activation_request_id=activation_request_id,
            reservation_id=reservation_id,
            merged_config=merged_config,
        )
        return (
            format_production_live_operational_signoff_result(summary, signer_present=False),
            1,
        )
    return (
        format_production_live_operational_signoff_result(
            summary,
            signer_present=bool(operator_id.strip()),
        ),
        0 if summary.signoff_status in {SIGNOFF_READY, SIGNOFF_READY_WITH_WARNINGS} else 1,
    )
