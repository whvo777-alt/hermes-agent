"""Production final sign-off gate — Phase 14J.

Read-only synthesis of operational sign-off, rollback validation, and the full
live pilot audit chain. Append-only final sign-off when READY; never grants execution.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_ENABLED,
    load_dispatch_gateway_enablement,
)
from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_COMPLETED,
    load_execution_reservation,
)
from agent.coo.production_activation_live_e2e import (
    _EVENT_ACTIVATION_REVOKED,
    _EVENT_CONSUME_COMMITTED,
    _EVENT_CORRELATION_VALIDATED,
    _EVENT_DISPATCH_AUDIT_WRITTEN,
    _EVENT_E2E_COMPLETED,
    _EVENT_EVIDENCE_WRITTEN,
    _load_e2e_store_payload,
    correlate_live_pilot_evidence_and_audit,
    default_e2e_history_dir,
    derive_live_pilot_dispatch_run_id,
    load_e2e_finalization_state,
    load_live_pilot_dispatch_audit,
    load_live_pilot_evidence,
)
from agent.coo.production_activation_live_pilot import (
    _EVENT_RESERVATION_CREATED,
    _load_preflight_records,
    default_preflight_history_dir,
)
from agent.coo.production_activation_live_runtime import (
    _EVENT_RESERVATION_STARTED,
    _EVENT_RUNTIME_COMPLETED,
    _EVENT_RUNTIME_STARTED,
    load_runtime_records,
)
from agent.coo.production_activation_state import (
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_APPROVED,
    ACTIVATION_STATE_ARMED,
    ACTIVATION_STATE_PROPOSED,
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
)
from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_live_operational_signoff import (
    SIGNOFF_BLOCKED,
    SIGNOFF_READY,
    SIGNOFF_READY_WITH_WARNINGS,
    SIGNOFF_REQUIRES_RECOVERY,
    WARN_DISCORD_PRODUCTION_DISABLED,
    WARN_EXTERNAL_PUBLISH_DISABLED,
    WARN_GATEWAY_PRODUCTION_DISABLED,
    WARN_LOCAL_OUTPUT_ONLY,
    WARN_RELEASE_TAG_NOT_PUSHED,
    WARN_REPOSITORY2_ORIGINAL_NOT_EXECUTED,
    default_signoff_store_dir,
    evaluate_production_live_operational_signoff,
    load_operational_signoff_record,
)
from agent.coo.production_live_rollback_validation import (
    ROLLBACK_NOT_READY,
    ROLLBACK_READY,
    ROLLBACK_READY_WITH_WARNINGS,
    ROLLBACK_REQUIRES_RECOVERY,
    WARN_MANUAL_ROLLBACK_ONLY,
    WARN_MIRROR_ONLY_VALIDATION,
    WARN_REMOTE_TAG_NOT_VERIFIED,
    default_rollback_validation_store_dir,
    evaluate_production_live_rollback_validation,
    load_rollback_validation_record,
)
from hermes_constants import get_hermes_home

_FINAL_SIGNOFF_STORE_DIR = "production-final-signoff"
_FINAL_SIGNOFF_STORE_VERSION = 1
_NEXT_PHASE_15 = "Phase_15_governed_production_cutover"

PRODUCTION_FINAL_SIGNOFF_READY = "PRODUCTION_FINAL_SIGNOFF_READY"
PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS = (
    "PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS"
)
PRODUCTION_FINAL_SIGNOFF_REQUIRES_RECOVERY = "PRODUCTION_FINAL_SIGNOFF_REQUIRES_RECOVERY"
PRODUCTION_FINAL_SIGNOFF_BLOCKED = "PRODUCTION_FINAL_SIGNOFF_BLOCKED"

WARN_ISOLATED_MIRROR_ONLY = "isolated_mirror_only"
WARN_SECOND_SUPERVISED_RUN_RECOMMENDED = "second_supervised_run_recommended"
WARN_PRODUCTION_ROOT_HARD_DENIED = "production_root_hard_denied"

ACTION_PRODUCTION_FINAL_SIGNOFF_COMPLETE = "production_final_signoff_complete"
ACTION_REVIEW_FINAL_SIGNOFF_WARNINGS = "review_final_signoff_warnings"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_RESOLVE_OPERATIONAL_SIGNOFF = "resolve_operational_signoff"
ACTION_RESOLVE_ROLLBACK_VALIDATION = "resolve_rollback_validation"
ACTION_RESOLVE_ARTIFACT_CORRELATION = "resolve_artifact_correlation"
ACTION_INSPECT_RUNTIME_FAILURE = "inspect_runtime_failure"
ACTION_INSPECT_EVIDENCE_FAILURE = "inspect_evidence_failure"
ACTION_INSPECT_AUDIT_FAILURE = "inspect_audit_failure"
ACTION_REVOKE_ACTIVATION_MANUALLY = "revoke_activation_manually"
ACTION_CREATE_NEW_ACTIVATION_PROPOSAL = "create_new_activation_proposal"
ACTION_PREPARE_PHASE_15_GOVERNED_PRODUCTION_CUTOVER = (
    "prepare_phase_15_governed_production_cutover"
)
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"

BLOCK_OPERATIONAL_SIGNOFF_MISSING = "operational_signoff_missing"
BLOCK_OPERATIONAL_SIGNOFF_INVALID = "operational_signoff_invalid"
BLOCK_ROLLBACK_VALIDATION_MISSING = "rollback_validation_missing"
BLOCK_ROLLBACK_VALIDATION_INVALID = "rollback_validation_invalid"
BLOCK_ACTIVATION_NOT_REVOKED = "activation_not_revoked"
BLOCK_RESERVATION_NOT_COMPLETED = "reservation_not_completed"
BLOCK_RUNTIME_NOT_COMPLETED = "runtime_not_completed"
BLOCK_RUNTIME_FAILED = "runtime_failed"
BLOCK_EVIDENCE_MISSING = "evidence_missing"
BLOCK_DISPATCH_AUDIT_MISSING = "dispatch_audit_missing"
BLOCK_CORRELATION_INVALID = "correlation_invalid"
BLOCK_CONSUME_NOT_COMMITTED = "consume_not_committed"
BLOCK_E2E_NOT_FINALIZED = "e2e_not_finalized"
BLOCK_RECOVERY_REQUIRED = "recovery_required"
BLOCK_REPAIR_LOCK_HELD = "repair_lock_held"
BLOCK_SOURCE_TREE_MUTATED = "source_tree_mutated"
BLOCK_PRODUCTION_ROOT_TOUCHED = "production_root_touched"
BLOCK_EXTERNAL_PUBLISH_ATTEMPTED = "external_publish_attempted"
BLOCK_AUDIT_CHAIN_INCOMPLETE = "audit_chain_incomplete"
BLOCK_ARTIFACT_CORRUPTED = "artifact_corrupted"
BLOCK_SIGNER_IDENTITY_CONFLICT = "signer_identity_conflict"
BLOCK_CHECKLIST_FAILED = "checklist_failed"

RELEASE_GOVERNED_CANDIDATE_READY = "GOVERNED_PRODUCTION_CANDIDATE_READY"
RELEASE_GOVERNED_CANDIDATE_READY_WITH_WARNINGS = (
    "GOVERNED_PRODUCTION_CANDIDATE_READY_WITH_WARNINGS"
)
RELEASE_GOVERNED_CANDIDATE_NOT_READY = "GOVERNED_PRODUCTION_CANDIDATE_NOT_READY"
RELEASE_GOVERNED_CANDIDATE_RECOVERY_REQUIRED = (
    "GOVERNED_PRODUCTION_CANDIDATE_RECOVERY_REQUIRED"
)

_ALLOWED_READY_WARNINGS = frozenset(
    {
        WARN_ISOLATED_MIRROR_ONLY,
        WARN_EXTERNAL_PUBLISH_DISABLED,
        WARN_REPOSITORY2_ORIGINAL_NOT_EXECUTED,
        WARN_GATEWAY_PRODUCTION_DISABLED,
        WARN_DISCORD_PRODUCTION_DISABLED,
        WARN_REMOTE_TAG_NOT_VERIFIED,
        WARN_MANUAL_ROLLBACK_ONLY,
        WARN_SECOND_SUPERVISED_RUN_RECOMMENDED,
        WARN_PRODUCTION_ROOT_HARD_DENIED,
        WARN_LOCAL_OUTPUT_ONLY,
        WARN_MIRROR_ONLY_VALIDATION,
        WARN_RELEASE_TAG_NOT_PUSHED,
    }
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
        "rollback_commit",
        "attestation_hash",
    }
)


class ProductionFinalSignoffError(ValueError):
    """Raised when final production sign-off cannot proceed safely."""


@dataclass(frozen=True)
class ProductionFinalSignoffSummary:
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    final_signoff_status: str
    production_release_ready: bool
    operational_signoff_valid: bool
    rollback_validation_valid: bool
    activation_revoked: bool
    reservation_completed: bool
    runtime_completed: bool
    runtime_success: bool
    evidence_present: bool
    dispatch_audit_present: bool
    correlation_valid: bool
    consume_state: str
    consume_committed: bool
    e2e_finalized: bool
    recovery_required: bool
    repair_lock_held: bool
    source_tree_unchanged: bool
    production_root_untouched: bool
    isolated_mirror_only: bool
    external_publish_attempted: bool
    rollback_ready: bool
    first_run_signoff_present: bool
    audit_chain_complete: bool
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str
    tested_commit_sha_short: str = ""
    release_tag: str = ""
    already_final_signed: bool = False
    production_execution_allowed: bool = False
    original_repository2_execution_attempted: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False


@dataclass(frozen=True)
class ProductionFinalSignoffRecord:
    final_signoff_id: str
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    final_signoff_status: str
    signed_by: str
    signed_at: str
    operational_signoff_id: str
    rollback_validation_id: str
    production_release_ready: bool
    blocking_item_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    tested_commit_sha: str
    release_tag: str
    rollback_ready: bool
    production_execution_allowed: bool = False
    original_repository2_execution_attempted: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    external_publish_enabled: bool = False


@dataclass(frozen=True)
class ProductionFinalReleaseSummary:
    validated_head: str
    release_tag: str
    activation_request_id: str
    final_signoff_status: str
    production_release_ready: bool
    isolated_live_pilot_validated: bool
    rollback_validated: bool
    consume_validated: bool
    recovery_clear: bool
    production_root_hard_deny: bool = True
    original_repository2_execution_enabled: bool = False
    external_publish_enabled: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    next_phase: str = _NEXT_PHASE_15
    release_status: str = RELEASE_GOVERNED_CANDIDATE_NOT_READY


@dataclass(frozen=True)
class ProductionFinalSignoffDashboardDigest:
    production_final_signoff_status: str
    production_release_ready: bool
    production_final_signoff_present: bool
    production_final_blocking_count: int
    production_final_warning_count: int
    production_final_recommended_action: str


def default_final_signoff_store_dir() -> Path:
    return get_hermes_home() / "coo" / _FINAL_SIGNOFF_STORE_DIR


def _utc_now_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.isoformat()


def _short_sha(value: str, limit: int = 12) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit]


def _final_signoff_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionFinalSignoffError("activation_request_id is required")
    base = (store_dir or default_final_signoff_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionFinalSignoffError(
            "Final sign-off store must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_final_signoff_store_available(*, store_dir: Path | None = None) -> bool:
    try:
        base = (store_dir or default_final_signoff_store_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _e2e_event_types(
    activation_request_id: str,
    *,
    e2e_history_dir: Path | None = None,
) -> frozenset[str]:
    payload = _load_e2e_store_payload(
        activation_request_id,
        history_dir=e2e_history_dir,
    )
    records = payload.get("records", [])
    types: set[str] = set()
    for item in records:
        if isinstance(item, dict):
            event_type = str(item.get("event_type", "")).strip()
            if event_type:
                types.add(event_type)
    return frozenset(types)


def _runtime_event_types(
    activation_request_id: str,
    execution_attempt_id: str,
    *,
    runtime_history_dir: Path | None = None,
) -> frozenset[str]:
    records = load_runtime_records(
        activation_request_id,
        history_dir=runtime_history_dir,
    )
    types: set[str] = set()
    for record in records:
        if record.execution_attempt_id == execution_attempt_id:
            types.add(record.event_type)
    return frozenset(types)


def _activation_lifecycle_states(request) -> frozenset[str]:
    states = {transition.to_state for transition in request.state_history}
    if request.state:
        states.add(request.state)
    return frozenset(states)


def _assess_audit_chain_complete(
    *,
    request,
    reservation,
    execution_attempt_id: str,
    dispatch_run_id: str,
    evidence_present: bool,
    audit_present: bool,
    correlation_valid: bool,
    consume_committed: bool,
    e2e_finalized: bool,
    operational_signoff_present: bool,
    rollback_validation_present: bool,
    runtime_history_dir: Path | None = None,
    e2e_history_dir: Path | None = None,
    preflight_history_dir: Path | None = None,
) -> bool:
    lifecycle = _activation_lifecycle_states(request)
    required_lifecycle = {
        ACTIVATION_STATE_PROPOSED,
        ACTIVATION_STATE_APPROVED,
        ACTIVATION_STATE_ARMED,
        ACTIVATION_STATE_ACTIVE,
        ACTIVATION_STATE_REVOKED,
    }
    if not required_lifecycle.issubset(lifecycle):
        return False

    if not reservation.reservation_id or not reservation.execution_gate_event_id:
        return False

    preflight_types = {
        record.event_type
        for record in _load_preflight_records(
            request.activation_request_id,
            history_dir=preflight_history_dir,
        )
        if record.reservation_id == reservation.reservation_id
    }
    if _EVENT_RESERVATION_CREATED not in preflight_types and not reservation.reserved_at:
        return False

    runtime_types = _runtime_event_types(
        request.activation_request_id,
        execution_attempt_id,
        runtime_history_dir=runtime_history_dir,
    )
    if _EVENT_RUNTIME_STARTED not in runtime_types:
        return False
    if _EVENT_RUNTIME_COMPLETED not in runtime_types:
        return False
    if _EVENT_RESERVATION_STARTED not in runtime_types:
        return False

    if not evidence_present or not audit_present or not correlation_valid:
        return False
    if not consume_committed or not e2e_finalized:
        return False

    e2e_types = _e2e_event_types(
        request.activation_request_id,
        e2e_history_dir=e2e_history_dir,
    )
    required_e2e = {
        _EVENT_EVIDENCE_WRITTEN,
        _EVENT_DISPATCH_AUDIT_WRITTEN,
        _EVENT_CORRELATION_VALIDATED,
        _EVENT_CONSUME_COMMITTED,
        _EVENT_ACTIVATION_REVOKED,
        _EVENT_E2E_COMPLETED,
    }
    if not required_e2e.issubset(e2e_types):
        return False

    if not operational_signoff_present or not rollback_validation_present:
        return False

    finalization = load_e2e_finalization_state(
        request.activation_request_id,
        history_dir=e2e_history_dir,
    )
    if finalization.execution_attempt_id != execution_attempt_id:
        return False
    if finalization.dispatch_run_id != dispatch_run_id:
        return False

    return True


def load_final_signoff_record(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionFinalSignoffRecord | None:
    path = _final_signoff_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionFinalSignoffError("final sign-off artifact corrupted") from exc
    signoff = payload.get("final_signoff")
    if not isinstance(signoff, dict):
        raise ProductionFinalSignoffError("final sign-off artifact corrupted")
    return ProductionFinalSignoffRecord(
        final_signoff_id=str(signoff.get("final_signoff_id", "")),
        activation_request_id=str(signoff.get("activation_request_id", "")),
        reservation_id=str(signoff.get("reservation_id", "")),
        execution_attempt_id=str(signoff.get("execution_attempt_id", "")),
        dispatch_run_id=str(signoff.get("dispatch_run_id", "")),
        final_signoff_status=str(signoff.get("final_signoff_status", "")),
        signed_by=str(signoff.get("signed_by", "")),
        signed_at=str(signoff.get("signed_at", "")),
        operational_signoff_id=str(signoff.get("operational_signoff_id", "")),
        rollback_validation_id=str(signoff.get("rollback_validation_id", "")),
        production_release_ready=bool(signoff.get("production_release_ready", False)),
        blocking_item_codes=tuple(signoff.get("blocking_item_codes") or ()),
        warning_codes=tuple(signoff.get("warning_codes") or ()),
        tested_commit_sha=str(signoff.get("tested_commit_sha", "")),
        release_tag=str(signoff.get("release_tag", "")),
        rollback_ready=bool(signoff.get("rollback_ready", False)),
        production_execution_allowed=False,
        original_repository2_execution_attempted=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        external_publish_enabled=False,
    )


def _write_final_signoff_record(
    record: ProductionFinalSignoffRecord,
    *,
    store_dir: Path | None = None,
) -> None:
    path = _final_signoff_path(record.activation_request_id, store_dir=store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_final_signoff_record(
        record.activation_request_id,
        store_dir=store_dir,
    )
    if existing is not None:
        if (
            existing.reservation_id == record.reservation_id
            and existing.execution_attempt_id == record.execution_attempt_id
            and existing.dispatch_run_id == record.dispatch_run_id
            and existing.final_signoff_status == record.final_signoff_status
        ):
            return
        raise ProductionFinalSignoffError("signoff_corruption")
    payload = {
        "version": _FINAL_SIGNOFF_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "final_signoff": {
            "final_signoff_id": record.final_signoff_id,
            "activation_request_id": record.activation_request_id,
            "reservation_id": record.reservation_id,
            "execution_attempt_id": record.execution_attempt_id,
            "dispatch_run_id": record.dispatch_run_id,
            "final_signoff_status": record.final_signoff_status,
            "signed_by": record.signed_by,
            "signed_at": record.signed_at,
            "operational_signoff_id": record.operational_signoff_id,
            "rollback_validation_id": record.rollback_validation_id,
            "production_release_ready": record.production_release_ready,
            "blocking_item_codes": list(record.blocking_item_codes),
            "warning_codes": list(record.warning_codes),
            "tested_commit_sha": _short_sha(record.tested_commit_sha),
            "release_tag": record.release_tag,
            "rollback_ready": record.rollback_ready,
            "production_execution_allowed": False,
            "original_repository2_execution_attempted": False,
            "gateway_production_enabled": False,
            "discord_production_enabled": False,
            "external_publish_enabled": False,
        },
    }
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise ProductionFinalSignoffError("final sign-off write failed") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _recommended_action(status: str, blocking: tuple[str, ...]) -> str:
    if status in {
        PRODUCTION_FINAL_SIGNOFF_READY,
        PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
    }:
        if status == PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS:
            return ACTION_REVIEW_FINAL_SIGNOFF_WARNINGS
        return ACTION_PREPARE_PHASE_15_GOVERNED_PRODUCTION_CUTOVER
    if status == PRODUCTION_FINAL_SIGNOFF_REQUIRES_RECOVERY:
        return ACTION_RUN_CONSUME_RECOVERY
    if BLOCK_OPERATIONAL_SIGNOFF_MISSING in blocking or BLOCK_OPERATIONAL_SIGNOFF_INVALID in blocking:
        return ACTION_RESOLVE_OPERATIONAL_SIGNOFF
    if (
        BLOCK_ROLLBACK_VALIDATION_MISSING in blocking
        or BLOCK_ROLLBACK_VALIDATION_INVALID in blocking
    ):
        return ACTION_RESOLVE_ROLLBACK_VALIDATION
    if BLOCK_CORRELATION_INVALID in blocking or BLOCK_AUDIT_CHAIN_INCOMPLETE in blocking:
        return ACTION_RESOLVE_ARTIFACT_CORRELATION
    if BLOCK_RUNTIME_FAILED in blocking or BLOCK_RUNTIME_NOT_COMPLETED in blocking:
        return ACTION_INSPECT_RUNTIME_FAILURE
    if BLOCK_EVIDENCE_MISSING in blocking:
        return ACTION_INSPECT_EVIDENCE_FAILURE
    if BLOCK_DISPATCH_AUDIT_MISSING in blocking:
        return ACTION_INSPECT_AUDIT_FAILURE
    if BLOCK_ACTIVATION_NOT_REVOKED in blocking:
        return ACTION_REVOKE_ACTIVATION_MANUALLY
    if BLOCK_ARTIFACT_CORRUPTED in blocking:
        return ACTION_RESOLVE_ARTIFACT_CORRELATION
    return ACTION_MAINTAIN_PRODUCTION_BLOCK


def evaluate_production_final_signoff(
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
    validation_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    preflight_history_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> ProductionFinalSignoffSummary:
    """Read-only final production sign-off assessment."""
    blocking: list[str] = []
    warnings: list[str] = []

    op_summary = evaluate_production_live_operational_signoff(
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
    rb_summary = evaluate_production_live_rollback_validation(
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
        validation_store_dir=validation_store_dir,
        repo_root=repo_root,
        merged_config=merged_config,
    )

    op_record = load_operational_signoff_record(
        activation_request_id,
        store_dir=signoff_store_dir,
    )
    rb_record = load_rollback_validation_record(
        activation_request_id,
        store_dir=validation_store_dir,
    )
    existing_final = load_final_signoff_record(
        activation_request_id,
        store_dir=final_signoff_store_dir,
    )

    operational_signoff_valid = False
    if op_record is None:
        blocking.append(BLOCK_OPERATIONAL_SIGNOFF_MISSING)
    elif op_summary.signoff_status in {SIGNOFF_READY, SIGNOFF_READY_WITH_WARNINGS}:
        operational_signoff_valid = True
        if (
            op_record.reservation_id != reservation_id
            or op_record.execution_attempt_id != op_summary.execution_attempt_id
        ):
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)
    elif op_summary.signoff_status == SIGNOFF_REQUIRES_RECOVERY:
        if (
            op_record.reservation_id != reservation_id
            or op_record.execution_attempt_id != op_summary.execution_attempt_id
        ):
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)
    elif op_summary.signoff_status == SIGNOFF_BLOCKED:
        if not {
            BLOCK_REPAIR_LOCK_HELD,
            BLOCK_RECOVERY_REQUIRED,
            BLOCK_CONSUME_NOT_COMMITTED,
        } & set(op_summary.blocking_items):
            blocking.append(BLOCK_OPERATIONAL_SIGNOFF_INVALID)
    else:
        blocking.append(BLOCK_OPERATIONAL_SIGNOFF_INVALID)

    rollback_validation_valid = False
    if rb_record is None:
        blocking.append(BLOCK_ROLLBACK_VALIDATION_MISSING)
    elif rb_summary.rollback_ready:
        rollback_validation_valid = True
        if (
            rb_record.reservation_id != reservation_id
            or rb_record.execution_attempt_id != rb_summary.execution_attempt_id
        ):
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)
    elif rb_summary.validation_status == ROLLBACK_REQUIRES_RECOVERY:
        if (
            rb_record.reservation_id != reservation_id
            or rb_record.execution_attempt_id != rb_summary.execution_attempt_id
        ):
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)
    else:
        blocking.append(BLOCK_ROLLBACK_VALIDATION_INVALID)

    already_final_signed = False
    if existing_final is not None:
        if (
            existing_final.reservation_id == op_summary.reservation_id
            and existing_final.execution_attempt_id == op_summary.execution_attempt_id
            and existing_final.dispatch_run_id == op_summary.dispatch_run_id
        ):
            already_final_signed = True
        else:
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)

    if not op_summary.activation_revoked:
        blocking.append(BLOCK_ACTIVATION_NOT_REVOKED)
    if op_summary.reservation_state != RESERVATION_STATE_COMPLETED:
        blocking.append(BLOCK_RESERVATION_NOT_COMPLETED)
    if not op_summary.runtime_completed:
        blocking.append(BLOCK_RUNTIME_NOT_COMPLETED)
    elif op_summary.runtime_exit_code != 0:
        blocking.append(BLOCK_RUNTIME_FAILED)
    if not op_summary.evidence_present:
        blocking.append(BLOCK_EVIDENCE_MISSING)
    if not op_summary.dispatch_audit_present:
        blocking.append(BLOCK_DISPATCH_AUDIT_MISSING)
    if not op_summary.evidence_audit_correlation_valid:
        blocking.append(BLOCK_CORRELATION_INVALID)
    if not op_summary.consume_committed:
        blocking.append(BLOCK_CONSUME_NOT_COMMITTED)
    if not op_summary.e2e_finalized:
        blocking.append(BLOCK_E2E_NOT_FINALIZED)
    if op_summary.recovery_required:
        blocking.append(BLOCK_RECOVERY_REQUIRED)
    if op_summary.repair_lock_held:
        blocking.append(BLOCK_REPAIR_LOCK_HELD)
    if not op_summary.source_tree_unchanged:
        blocking.append(BLOCK_SOURCE_TREE_MUTATED)
    if not op_summary.production_root_untouched:
        blocking.append(BLOCK_PRODUCTION_ROOT_TOUCHED)
    if op_summary.publish_attempted:
        blocking.append(BLOCK_EXTERNAL_PUBLISH_ATTEMPTED)
    if not op_summary.operator_checklist_passed and op_summary.signoff_status not in {
        SIGNOFF_REQUIRES_RECOVERY,
    }:
        blocking.append(BLOCK_CHECKLIST_FAILED)

    request = load_activation_request(activation_request_id, store_dir=store_dir)
    reservation = load_execution_reservation(
        activation_request_id,
        store_dir=reservation_dir,
    )
    if reservation is None or reservation.reservation_id != reservation_id:
        blocking.append(BLOCK_ARTIFACT_CORRUPTED)

    dispatch_run_id = op_summary.dispatch_run_id
    execution_attempt_id = op_summary.execution_attempt_id

    evidence = None
    audit = None
    if execution_attempt_id:
        evidence = load_live_pilot_evidence(
            execution_attempt_id,
            evidence_dir=evidence_dir,
        )
        audit = load_live_pilot_dispatch_audit(
            dispatch_run_id,
            audit_dir=audit_dir,
        )
    correlation_valid = False
    if evidence is not None and audit is not None and reservation is not None:
        correlation_valid = correlate_live_pilot_evidence_and_audit(
            evidence,
            audit,
            reservation=reservation,
        )

    audit_chain_complete = False
    if reservation is not None:
        try:
            audit_chain_complete = _assess_audit_chain_complete(
                request=request,
                reservation=reservation,
                execution_attempt_id=execution_attempt_id,
                dispatch_run_id=dispatch_run_id,
                evidence_present=op_summary.evidence_present,
                audit_present=op_summary.dispatch_audit_present,
                correlation_valid=correlation_valid,
                consume_committed=op_summary.consume_committed,
                e2e_finalized=op_summary.e2e_finalized,
                operational_signoff_present=op_record is not None,
                rollback_validation_present=rb_record is not None,
                runtime_history_dir=runtime_history_dir,
                e2e_history_dir=e2e_history_dir,
                preflight_history_dir=preflight_history_dir or default_preflight_history_dir(),
            )
        except Exception:
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)
    if not audit_chain_complete:
        blocking.append(BLOCK_AUDIT_CHAIN_INCOMPLETE)

    enablement = load_dispatch_gateway_enablement(merged_config=merged_config or {})
    gateway_enabled = enablement.gateway_state == GATEWAY_STATE_ENABLED
    discord_enabled = False

    warnings.append(WARN_ISOLATED_MIRROR_ONLY)
    warnings.append(WARN_EXTERNAL_PUBLISH_DISABLED)
    warnings.append(WARN_REPOSITORY2_ORIGINAL_NOT_EXECUTED)
    warnings.append(WARN_PRODUCTION_ROOT_HARD_DENIED)
    warnings.append(WARN_LOCAL_OUTPUT_ONLY)
    warnings.append(WARN_MIRROR_ONLY_VALIDATION)
    warnings.append(WARN_MANUAL_ROLLBACK_ONLY)
    warnings.append(WARN_REMOTE_TAG_NOT_VERIFIED)
    if not gateway_enabled:
        warnings.append(WARN_GATEWAY_PRODUCTION_DISABLED)
    warnings.append(WARN_DISCORD_PRODUCTION_DISABLED)
    if request.release_tag:
        warnings.append(WARN_RELEASE_TAG_NOT_PUSHED)
    if op_summary.first_run_detected:
        warnings.append(WARN_SECOND_SUPERVISED_RUN_RECOMMENDED)

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
    if {
        BLOCK_RECOVERY_REQUIRED,
        BLOCK_REPAIR_LOCK_HELD,
        BLOCK_CONSUME_NOT_COMMITTED,
    } & set(unique_blocking):
        hard_blocks = [
            code
            for code in hard_blocks
            if code
            not in {
                BLOCK_AUDIT_CHAIN_INCOMPLETE,
                BLOCK_CHECKLIST_FAILED,
                BLOCK_OPERATIONAL_SIGNOFF_INVALID,
            }
        ]
    if hard_blocks:
        final_status = PRODUCTION_FINAL_SIGNOFF_BLOCKED
    elif (
        op_summary.signoff_status == SIGNOFF_REQUIRES_RECOVERY
        or rb_summary.validation_status == ROLLBACK_REQUIRES_RECOVERY
        or BLOCK_RECOVERY_REQUIRED in unique_blocking
        or BLOCK_REPAIR_LOCK_HELD in unique_blocking
        or BLOCK_CONSUME_NOT_COMMITTED in unique_blocking
        or (
            request.state == ACTIVATION_STATE_SUSPENDED
            and request.state != ACTIVATION_STATE_REVOKED
        )
    ):
        final_status = PRODUCTION_FINAL_SIGNOFF_REQUIRES_RECOVERY
    elif unique_warnings:
        final_status = PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS
    else:
        final_status = PRODUCTION_FINAL_SIGNOFF_READY

    production_release_ready = final_status in {
        PRODUCTION_FINAL_SIGNOFF_READY,
        PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
    }

    recommended = _recommended_action(final_status, unique_blocking)
    if already_final_signed and production_release_ready:
        recommended = ACTION_PRODUCTION_FINAL_SIGNOFF_COMPLETE

    return ProductionFinalSignoffSummary(
        activation_request_id=activation_request_id,
        reservation_id=op_summary.reservation_id,
        execution_attempt_id=execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        final_signoff_status=final_status,
        production_release_ready=production_release_ready,
        operational_signoff_valid=operational_signoff_valid,
        rollback_validation_valid=rollback_validation_valid,
        activation_revoked=op_summary.activation_revoked,
        reservation_completed=op_summary.reservation_state == RESERVATION_STATE_COMPLETED,
        runtime_completed=op_summary.runtime_completed,
        runtime_success=op_summary.runtime_completed and op_summary.runtime_exit_code == 0,
        evidence_present=op_summary.evidence_present,
        dispatch_audit_present=op_summary.dispatch_audit_present,
        correlation_valid=correlation_valid,
        consume_state=op_summary.consume_state,
        consume_committed=op_summary.consume_committed,
        e2e_finalized=op_summary.e2e_finalized,
        recovery_required=op_summary.recovery_required,
        repair_lock_held=op_summary.repair_lock_held,
        source_tree_unchanged=op_summary.source_tree_unchanged,
        production_root_untouched=op_summary.production_root_untouched,
        isolated_mirror_only=op_summary.isolated_mirror_only,
        external_publish_attempted=op_summary.publish_attempted,
        rollback_ready=rb_summary.rollback_ready,
        first_run_signoff_present=op_record is not None,
        audit_chain_complete=audit_chain_complete,
        blocking_items=unique_blocking,
        warning_items=unique_warnings,
        recommended_action=recommended,
        tested_commit_sha_short=_short_sha(request.tested_commit_sha),
        release_tag=request.release_tag,
        already_final_signed=already_final_signed,
        production_execution_allowed=False,
        original_repository2_execution_attempted=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
    )


def record_production_final_signoff(
    *,
    activation_request_id: str,
    reservation_id: str,
    signer_id: str,
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
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionFinalSignoffSummary:
    """Append final production sign-off when assessment is READY."""
    if not probe_final_signoff_store_available(store_dir=final_signoff_store_dir):
        raise ProductionFinalSignoffError("final sign-off write failed")

    signed_by = (signer_id or "").strip()
    if not signed_by:
        raise ProductionFinalSignoffError("signer_id is required")

    summary = evaluate_production_final_signoff(
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
        validation_store_dir=validation_store_dir,
        final_signoff_store_dir=final_signoff_store_dir,
        preflight_history_dir=preflight_history_dir,
        repo_root=repo_root,
        merged_config=merged_config,
    )

    if summary.already_final_signed:
        return summary

    if summary.final_signoff_status not in {
        PRODUCTION_FINAL_SIGNOFF_READY,
        PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
    }:
        raise ProductionFinalSignoffError(
            f"final sign-off blocked for status {summary.final_signoff_status!r}"
        )

    if BLOCK_ARTIFACT_CORRUPTED in summary.blocking_items:
        raise ProductionFinalSignoffError("artifact_corrupted")

    request = load_activation_request(activation_request_id, store_dir=store_dir)
    op_record = load_operational_signoff_record(
        activation_request_id,
        store_dir=signoff_store_dir,
    )
    rb_record = load_rollback_validation_record(
        activation_request_id,
        store_dir=validation_store_dir,
    )
    if op_record is None or rb_record is None:
        raise ProductionFinalSignoffError("artifact_corrupted")

    if signed_by == (request.executor_id or "").strip():
        raise ProductionFinalSignoffError("signer_identity_conflict")
    if signed_by == (op_record.signed_by or "").strip():
        raise ProductionFinalSignoffError("signer_identity_conflict")
    if signed_by == (request.requested_by or "").strip():
        raise ProductionFinalSignoffError("signer_identity_conflict")

    record = ProductionFinalSignoffRecord(
        final_signoff_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        reservation_id=summary.reservation_id,
        execution_attempt_id=summary.execution_attempt_id,
        dispatch_run_id=summary.dispatch_run_id,
        final_signoff_status=summary.final_signoff_status,
        signed_by=signed_by,
        signed_at=_utc_now_iso(now),
        operational_signoff_id=op_record.signoff_id,
        rollback_validation_id=rb_record.validation_id,
        production_release_ready=summary.production_release_ready,
        blocking_item_codes=summary.blocking_items,
        warning_codes=summary.warning_items,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        rollback_ready=summary.rollback_ready,
        production_execution_allowed=False,
        original_repository2_execution_attempted=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        external_publish_enabled=False,
    )
    _write_final_signoff_record(record, store_dir=final_signoff_store_dir)
    return evaluate_production_final_signoff(
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
        validation_store_dir=validation_store_dir,
        final_signoff_store_dir=final_signoff_store_dir,
        preflight_history_dir=preflight_history_dir,
        repo_root=repo_root,
        merged_config=merged_config,
    )


def build_production_final_release_summary(
    summary: ProductionFinalSignoffSummary,
    *,
    request=None,
    merged_config: Mapping[str, Any] | None = None,
) -> ProductionFinalReleaseSummary:
    """Build governed production release summary from final sign-off assessment."""
    enablement = load_dispatch_gateway_enablement(merged_config=merged_config or {})
    validated_head = ""
    release_tag = ""
    if request is not None:
        validated_head = request.tested_commit_sha
        release_tag = request.release_tag

    if summary.final_signoff_status == PRODUCTION_FINAL_SIGNOFF_REQUIRES_RECOVERY:
        release_status = RELEASE_GOVERNED_CANDIDATE_RECOVERY_REQUIRED
    elif summary.production_release_ready:
        release_status = (
            RELEASE_GOVERNED_CANDIDATE_READY_WITH_WARNINGS
            if summary.final_signoff_status
            == PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS
            else RELEASE_GOVERNED_CANDIDATE_READY
        )
    else:
        release_status = RELEASE_GOVERNED_CANDIDATE_NOT_READY

    return ProductionFinalReleaseSummary(
        validated_head=validated_head,
        release_tag=release_tag,
        activation_request_id=summary.activation_request_id,
        final_signoff_status=summary.final_signoff_status,
        production_release_ready=summary.production_release_ready,
        isolated_live_pilot_validated=summary.operational_signoff_valid,
        rollback_validated=summary.rollback_validation_valid,
        consume_validated=summary.consume_committed,
        recovery_clear=not summary.recovery_required and not summary.repair_lock_held,
        production_root_hard_deny=enablement.production_root_hard_deny,
        original_repository2_execution_enabled=False,
        external_publish_enabled=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        next_phase=_NEXT_PHASE_15,
        release_status=release_status,
    )


def resolve_latest_final_signoff_dashboard_digest(
    *,
    e2e_history_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    signoff_store_dir: Path | None = None,
    validation_store_dir: Path | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    runtime_history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    preflight_history_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> ProductionFinalSignoffDashboardDigest:
    """Read-only digest of the newest final sign-off for operator dashboard."""
    base = (e2e_history_dir or default_e2e_history_dir()).resolve()
    if not base.is_dir():
        return ProductionFinalSignoffDashboardDigest(
            production_final_signoff_status="not_configured",
            production_release_ready=False,
            production_final_signoff_present=False,
            production_final_blocking_count=0,
            production_final_warning_count=0,
            production_final_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )

    paths = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:500]:
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
        try:
            reservation = load_execution_reservation(
                activation_id,
                store_dir=reservation_dir,
            )
        except Exception:
            continue
        if reservation is None:
            continue
        try:
            summary = evaluate_production_final_signoff(
                activation_request_id=activation_id,
                reservation_id=reservation.reservation_id,
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
                repo_root=repo_root,
                merged_config=merged_config,
            )
        except ProductionFinalSignoffError:
            continue
        existing = load_final_signoff_record(
            activation_id,
            store_dir=final_signoff_store_dir,
        )
        status = (
            existing.final_signoff_status
            if existing is not None
            else summary.final_signoff_status
        )
        return ProductionFinalSignoffDashboardDigest(
            production_final_signoff_status=status,
            production_release_ready=summary.production_release_ready,
            production_final_signoff_present=existing is not None,
            production_final_blocking_count=len(summary.blocking_items),
            production_final_warning_count=len(summary.warning_items),
            production_final_recommended_action=summary.recommended_action,
        )

    return ProductionFinalSignoffDashboardDigest(
        production_final_signoff_status="not_configured",
        production_release_ready=False,
        production_final_signoff_present=False,
        production_final_blocking_count=0,
        production_final_warning_count=0,
        production_final_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
    )


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "production_execution_allowed: false",
        "original_repository2_execution_attempted: false",
        "external_publish_attempted: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
        "original_repository2_not_executed",
        "repository2_original_not_executed",
        "signer_present: true",
        "signer_present: false",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionFinalSignoffError(
                f"Unsafe final sign-off output field: {token!r}"
            )


def format_production_final_signoff_status(
    summary: ProductionFinalSignoffSummary,
    *,
    signer_present: bool = False,
) -> str:
    """Format read-only final sign-off status."""
    lines = [
        "Production Final Sign-off Status",
        "",
        f"activation_request_id: {summary.activation_request_id}",
        f"reservation_id: {summary.reservation_id or '(none)'}",
        f"execution_attempt_id: {summary.execution_attempt_id or '(none)'}",
        f"dispatch_run_id: {summary.dispatch_run_id or '(none)'}",
        f"final_signoff_status: {summary.final_signoff_status}",
        f"production_release_ready: {str(summary.production_release_ready).lower()}",
        "operational_signoff_valid: "
        f"{str(summary.operational_signoff_valid).lower()}",
        "rollback_validation_valid: "
        f"{str(summary.rollback_validation_valid).lower()}",
        f"activation_revoked: {str(summary.activation_revoked).lower()}",
        f"reservation_completed: {str(summary.reservation_completed).lower()}",
        f"runtime_completed: {str(summary.runtime_completed).lower()}",
        f"runtime_success: {str(summary.runtime_success).lower()}",
        f"evidence_present: {str(summary.evidence_present).lower()}",
        f"dispatch_audit_present: {str(summary.dispatch_audit_present).lower()}",
        f"correlation_valid: {str(summary.correlation_valid).lower()}",
        f"consume_state: {summary.consume_state or '(none)'}",
        f"consume_committed: {str(summary.consume_committed).lower()}",
        f"e2e_finalized: {str(summary.e2e_finalized).lower()}",
        f"recovery_required: {str(summary.recovery_required).lower()}",
        f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
        f"source_tree_unchanged: {str(summary.source_tree_unchanged).lower()}",
        f"production_root_untouched: {str(summary.production_root_untouched).lower()}",
        f"isolated_mirror_only: {str(summary.isolated_mirror_only).lower()}",
        "external_publish_attempted: "
        f"{str(summary.external_publish_attempted).lower()}",
        f"rollback_ready: {str(summary.rollback_ready).lower()}",
        "first_run_signoff_present: "
        f"{str(summary.first_run_signoff_present).lower()}",
        f"audit_chain_complete: {str(summary.audit_chain_complete).lower()}",
        f"blocking_items_count: {len(summary.blocking_items)}",
        f"warning_items_count: {len(summary.warning_items)}",
        f"blocking_items: {', '.join(summary.blocking_items) if summary.blocking_items else '(none)'}",
        f"warning_items: {', '.join(summary.warning_items) if summary.warning_items else '(none)'}",
        f"recommended_action: {summary.recommended_action}",
        f"tested_commit_sha: {summary.tested_commit_sha_short or '(none)'}",
        f"release_tag: {summary.release_tag or '(none)'}",
        f"already_final_signed: {str(summary.already_final_signed).lower()}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "original_repository2_execution_attempted: false",
        f"gateway_production_enabled: {str(summary.gateway_production_enabled).lower()}",
        f"discord_production_enabled: {str(summary.discord_production_enabled).lower()}",
        f"signer_present: {str(signer_present).lower()}",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def run_production_final_signoff_status(
    *,
    activation_request_id: str,
    reservation_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    summary = evaluate_production_final_signoff(
        activation_request_id=activation_request_id,
        reservation_id=reservation_id,
        merged_config=merged_config,
        repo_root=repo_root,
    )
    exit_code = 0 if summary.production_release_ready else 1
    return format_production_final_signoff_status(summary), exit_code


def run_production_final_signoff(
    *,
    activation_request_id: str,
    reservation_id: str,
    signer_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = record_production_final_signoff(
            activation_request_id=activation_request_id,
            reservation_id=reservation_id,
            signer_id=signer_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionFinalSignoffError:
        summary = evaluate_production_final_signoff(
            activation_request_id=activation_request_id,
            reservation_id=reservation_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
        return (
            format_production_final_signoff_status(summary, signer_present=False),
            1,
        )
    return (
        format_production_final_signoff_status(summary, signer_present=bool(signer_id.strip())),
        0 if summary.production_release_ready else 1,
    )
