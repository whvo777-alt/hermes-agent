"""Governed production cutover contract — Phase 15A.

Read-only evaluation of cutover readiness after final sign-off, plus append-only
contract artifacts. Never opens a maintenance window or grants execution.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_ENABLED,
    load_dispatch_gateway_enablement,
)
from agent.coo.production_activation_execution_reservation import (
    load_execution_reservation,
)
from agent.coo.production_activation_live_e2e import (
    default_e2e_history_dir,
    load_live_pilot_dispatch_audit,
    load_live_pilot_evidence,
)
from agent.coo.production_activation_state import ACTIVATION_STATE_SUSPENDED
from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_final_signoff import (
    PRODUCTION_FINAL_SIGNOFF_READY,
    PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
    PRODUCTION_FINAL_SIGNOFF_REQUIRES_RECOVERY,
    ProductionFinalSignoffError,
    evaluate_production_final_signoff,
    load_final_signoff_record,
)
from agent.coo.production_live_operational_signoff import (
    load_operational_signoff_record,
)
from agent.coo.production_live_rollback_validation import (
    load_rollback_validation_record,
)
from hermes_constants import get_hermes_home

_GOVERNED_CUTOVER_STORE_DIR = "production-governed-cutover"
_GOVERNED_CUTOVER_STORE_VERSION = 1
_NEXT_PHASE_15B = "Phase_15B_controlled_production_window"

GOVERNED_CUTOVER_READY = "GOVERNED_CUTOVER_READY"
GOVERNED_CUTOVER_READY_WITH_WARNINGS = "GOVERNED_CUTOVER_READY_WITH_WARNINGS"
GOVERNED_CUTOVER_NOT_READY = "GOVERNED_CUTOVER_NOT_READY"
GOVERNED_CUTOVER_REQUIRES_RECOVERY = "GOVERNED_CUTOVER_REQUIRES_RECOVERY"
GOVERNED_CUTOVER_CONTRACT_PREPARED = "GOVERNED_CUTOVER_CONTRACT_PREPARED"

RELEASE_GOVERNED_CUTOVER_CANDIDATE_READY = "GOVERNED_CUTOVER_CANDIDATE_READY"
RELEASE_GOVERNED_CUTOVER_CANDIDATE_READY_WITH_WARNINGS = (
    "GOVERNED_CUTOVER_CANDIDATE_READY_WITH_WARNINGS"
)
RELEASE_GOVERNED_CUTOVER_CONTRACT_PREPARED = "GOVERNED_CUTOVER_CONTRACT_PREPARED"
RELEASE_GOVERNED_CUTOVER_NOT_READY = "GOVERNED_CUTOVER_NOT_READY"
RELEASE_GOVERNED_CUTOVER_RECOVERY_REQUIRED = "GOVERNED_CUTOVER_RECOVERY_REQUIRED"

WARN_ISOLATED_MIRROR_ONLY = "isolated_mirror_only"
WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED = "original_repository2_not_executed"
WARN_EXTERNAL_PUBLISH_DISABLED = "external_publish_disabled"
WARN_GATEWAY_PRODUCTION_DISABLED = "gateway_production_disabled"
WARN_DISCORD_PRODUCTION_DISABLED = "discord_production_disabled"
WARN_PRODUCTION_ROOT_HARD_DENIED = "production_root_hard_denied"
WARN_MANUAL_ROLLBACK_ONLY = "manual_rollback_only"
WARN_REMOTE_TAG_NOT_VERIFIED = "remote_tag_not_verified"
WARN_SECOND_SUPERVISED_RUN_RECOMMENDED = "second_supervised_run_recommended"
WARN_LOCAL_OUTPUT_ONLY = "local_output_only"
WARN_MAINTENANCE_WINDOW_NOT_OPENED = "maintenance_window_not_opened"
WARN_EXECUTION_PERMIT_NOT_CREATED = "execution_permit_not_created"

ACTION_GOVERNED_CUTOVER_READY_PREPARE_CONTRACT = (
    "governed_cutover_ready_prepare_contract"
)
ACTION_GOVERNED_CUTOVER_CONTRACT_PREPARED = "governed_cutover_contract_prepared"
ACTION_REVIEW_GOVERNED_CUTOVER_WARNINGS = "review_governed_cutover_warnings"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_RESOLVE_FINAL_SIGNOFF = "resolve_final_signoff"
ACTION_RESOLVE_ROLLBACK_VALIDATION = "resolve_rollback_validation"
ACTION_RESOLVE_OPERATIONAL_SIGNOFF = "resolve_operational_signoff"
ACTION_RESOLVE_ARTIFACT_CORRELATION = "resolve_artifact_correlation"
ACTION_DEFINE_MAINTENANCE_WINDOW = "define_maintenance_window"
ACTION_RESOLVE_OPERATOR_HANDOFF = "resolve_operator_handoff"
ACTION_PREPARE_ROLLBACK_OWNER = "prepare_rollback_owner"
ACTION_CREATE_NEW_ACTIVATION_PROPOSAL = "create_new_activation_proposal"
ACTION_PREPARE_PHASE_15B_CONTROLLED_PRODUCTION_WINDOW = (
    "prepare_phase_15b_controlled_production_window"
)
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"

BLOCK_FINAL_SIGNOFF_MISSING = "final_signoff_missing"
BLOCK_FINAL_SIGNOFF_INVALID = "final_signoff_invalid"
BLOCK_ROLLBACK_VALIDATION_MISSING = "rollback_validation_missing"
BLOCK_ROLLBACK_VALIDATION_INVALID = "rollback_validation_invalid"
BLOCK_OPERATIONAL_SIGNOFF_MISSING = "operational_signoff_missing"
BLOCK_OPERATIONAL_SIGNOFF_INVALID = "operational_signoff_invalid"
BLOCK_ACTIVATION_NOT_REVOKED = "activation_not_revoked"
BLOCK_RESERVATION_NOT_COMPLETED = "reservation_not_completed"
BLOCK_RUNTIME_NOT_VALIDATED = "runtime_not_validated"
BLOCK_EVIDENCE_MISSING = "evidence_missing"
BLOCK_DISPATCH_AUDIT_MISSING = "dispatch_audit_missing"
BLOCK_CORRELATION_INVALID = "correlation_invalid"
BLOCK_CONSUME_NOT_COMMITTED = "consume_not_committed"
BLOCK_E2E_NOT_FINALIZED = "e2e_not_finalized"
BLOCK_AUDIT_CHAIN_INCOMPLETE = "audit_chain_incomplete"
BLOCK_RECOVERY_REQUIRED = "recovery_required"
BLOCK_REPAIR_LOCK_HELD = "repair_lock_held"
BLOCK_TESTED_COMMIT_INVALID = "tested_commit_invalid"
BLOCK_RELEASE_TAG_INVALID = "release_tag_invalid"
BLOCK_ROLLBACK_NOT_READY = "rollback_not_ready"
BLOCK_SOURCE_TREE_MUTATED = "source_tree_mutated"
BLOCK_PRODUCTION_ROOT_TOUCHED = "production_root_touched"
BLOCK_EXTERNAL_PUBLISH_ATTEMPTED = "external_publish_attempted"
BLOCK_GATEWAY_PRODUCTION_ENABLED = "gateway_production_enabled"
BLOCK_DISCORD_PRODUCTION_ENABLED = "discord_production_enabled"
BLOCK_PRODUCTION_EXECUTION_ENABLED = "production_execution_enabled"
BLOCK_MAINTENANCE_WINDOW_INVALID = "maintenance_window_invalid"
BLOCK_OPERATOR_IDENTITY_INVALID = "operator_identity_invalid"
BLOCK_OPERATOR_HANDOFF_NOT_READY = "operator_handoff_not_ready"
BLOCK_EMERGENCY_CLOSE_UNAVAILABLE = "emergency_close_unavailable"
BLOCK_CHECKLIST_FAILED = "checklist_failed"
BLOCK_ARTIFACT_CORRUPTED = "artifact_corrupted"

CONTRACT_STATUS_PREPARED = "prepared"
MIN_WINDOW_SECONDS = 15 * 60
MAX_WINDOW_SECONDS = 2 * 60 * 60
MAX_WINDOW_START_AHEAD = timedelta(days=7)

_ALLOWED_READY_WARNINGS = frozenset(
    {
        WARN_ISOLATED_MIRROR_ONLY,
        WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED,
        WARN_EXTERNAL_PUBLISH_DISABLED,
        WARN_GATEWAY_PRODUCTION_DISABLED,
        WARN_DISCORD_PRODUCTION_DISABLED,
        WARN_PRODUCTION_ROOT_HARD_DENIED,
        WARN_MANUAL_ROLLBACK_ONLY,
        WARN_REMOTE_TAG_NOT_VERIFIED,
        WARN_SECOND_SUPERVISED_RUN_RECOMMENDED,
        WARN_LOCAL_OUTPUT_ONLY,
        WARN_MAINTENANCE_WINDOW_NOT_OPENED,
        WARN_EXECUTION_PERMIT_NOT_CREATED,
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
        "prepared_by",
        "attestation_hash",
    }
)


class ProductionGovernedCutoverError(ValueError):
    """Raised when governed cutover assessment or contract write fails."""


@dataclass(frozen=True)
class ProductionCutoverOperatorHandoff:
    primary_operator_present: bool
    release_approver_present: bool
    security_reviewer_present: bool
    incident_commander_available: bool
    production_executor_assigned: bool
    rollback_owner_assigned: bool
    handoff_acknowledged: bool
    operator_handoff_ready: bool


@dataclass(frozen=True)
class ProductionGovernedCutoverChecklist:
    final_signoff_ready: bool
    rollback_validation_ready: bool
    operational_signoff_ready: bool
    activation_revoked: bool
    reservation_completed: bool
    runtime_success: bool
    evidence_present: bool
    dispatch_audit_present: bool
    correlation_valid: bool
    consume_committed: bool
    e2e_finalized: bool
    audit_chain_complete: bool
    recovery_clear: bool
    repair_lock_clear: bool
    tested_commit_valid: bool
    release_tag_valid: bool
    rollback_commit_present: bool
    source_tree_unchanged: bool
    production_root_untouched: bool
    isolated_mirror_validated: bool
    external_publish_disabled: bool
    gateway_production_disabled: bool
    discord_production_disabled: bool
    production_execution_disabled: bool
    maintenance_window_valid: bool
    maintenance_window_future_or_active: bool
    maintenance_window_duration_valid: bool
    operator_identity_valid: bool
    operator_handoff_ready: bool
    emergency_close_available: bool
    rollback_plan_available: bool
    one_shot_policy_valid: bool
    checklist_passed: bool


@dataclass(frozen=True)
class ProductionGovernedCutoverSummary:
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    cutover_contract_id: str
    governed_cutover_status: str
    governed_cutover_ready: bool
    final_signoff_present: bool
    final_signoff_valid: bool
    production_release_ready: bool
    rollback_validation_ready: bool
    operational_signoff_ready: bool
    activation_revoked: bool
    reservation_completed: bool
    runtime_validated: bool
    evidence_present: bool
    dispatch_audit_present: bool
    consume_committed: bool
    e2e_finalized: bool
    audit_chain_complete: bool
    recovery_required: bool
    repair_lock_held: bool
    source_tree_unchanged: bool
    production_root_untouched: bool
    isolated_mirror_validated: bool
    external_publish_enabled: bool
    gateway_production_enabled: bool
    discord_production_enabled: bool
    production_execution_allowed: bool
    production_root_hard_deny: bool
    original_repository2_execution_attempted: bool
    maintenance_window_valid: bool
    operator_handoff_ready: bool
    rollback_ready: bool
    checklist_passed: bool
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str
    maintenance_window_start: str = ""
    maintenance_window_end: str = ""
    maintenance_window_duration_seconds: int = 0
    window_opened: bool = False
    window_closed: bool = False
    cutover_started: bool = False
    execution_permit_created: bool = False
    already_prepared: bool = False
    tested_commit_sha_short: str = ""
    release_tag: str = ""
    operator_present: bool = False
    checklist: ProductionGovernedCutoverChecklist | None = None
    handoff: ProductionCutoverOperatorHandoff | None = None


@dataclass(frozen=True)
class ProductionGovernedCutoverContractRecord:
    cutover_contract_id: str
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    final_signoff_id: str
    operational_signoff_id: str
    rollback_validation_id: str
    contract_status: str
    prepared_by: str
    prepared_at: str
    maintenance_window_start: str
    maintenance_window_end: str
    maintenance_window_duration_seconds: int
    checklist_passed: bool
    operator_handoff_ready: bool
    rollback_ready: bool
    blocking_item_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    tested_commit_sha: str
    release_tag: str
    production_execution_allowed: bool = False
    production_root_hard_deny: bool = True
    original_repository2_execution_attempted: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    external_publish_enabled: bool = False
    window_opened: bool = False
    window_closed: bool = False
    cutover_started: bool = False
    execution_permit_created: bool = False


@dataclass(frozen=True)
class ProductionGovernedCutoverReleaseSummary:
    validated_head: str
    release_tag: str
    activation_request_id: str
    final_signoff_status: str
    governed_cutover_status: str
    governed_cutover_ready: bool
    cutover_contract_present: bool
    maintenance_window_valid: bool
    rollback_ready: bool
    production_execution_allowed: bool = False
    production_root_hard_deny: bool = True
    original_repository2_execution_enabled: bool = False
    external_publish_enabled: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    next_phase: str = _NEXT_PHASE_15B
    release_status: str = RELEASE_GOVERNED_CUTOVER_NOT_READY


@dataclass(frozen=True)
class ProductionGovernedCutoverDashboardDigest:
    governed_cutover_status: str
    governed_cutover_ready: bool
    governed_cutover_contract_present: bool
    governed_cutover_window_valid: bool
    governed_cutover_blocking_count: int
    governed_cutover_warning_count: int
    governed_cutover_recommended_action: str


def default_governed_cutover_store_dir() -> Path:
    return get_hermes_home() / "coo" / _GOVERNED_CUTOVER_STORE_DIR


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


def _contract_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionGovernedCutoverError("activation_request_id is required")
    base = (store_dir or default_governed_cutover_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionGovernedCutoverError(
            "Governed cutover store must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_governed_cutover_store_available(*, store_dir: Path | None = None) -> bool:
    try:
        base = (store_dir or default_governed_cutover_store_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _parse_iso8601_required(value: str, *, field_name: str) -> datetime:
    text = (value or "").strip()
    if not text:
        raise ProductionGovernedCutoverError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionGovernedCutoverError(
            f"{field_name} must be timezone-aware ISO8601"
        ) from exc
    if parsed.tzinfo is None:
        raise ProductionGovernedCutoverError(
            f"{field_name} must include timezone offset"
        )
    return parsed


def validate_maintenance_window(
    window_start: str,
    window_end: str,
    *,
    now: datetime | None = None,
) -> tuple[bool, bool, bool, int, str, str]:
    """Return (valid, future_or_active, duration_valid, duration_seconds, start_iso, end_iso)."""
    try:
        start = _parse_iso8601_required(window_start, field_name="window_start")
        end = _parse_iso8601_required(window_end, field_name="window_end")
    except ProductionGovernedCutoverError:
        return False, False, False, 0, "", ""

    if end <= start:
        return False, False, False, 0, start.isoformat(), end.isoformat()

    duration = int((end - start).total_seconds())
    duration_valid = MIN_WINDOW_SECONDS <= duration <= MAX_WINDOW_SECONDS
    current = _utc_now(now)
    future_or_active = end > current
    start_within_horizon = start <= current + MAX_WINDOW_START_AHEAD
    valid = duration_valid and future_or_active and start_within_horizon
    return (
        valid,
        future_or_active,
        duration_valid,
        duration,
        start.isoformat(),
        end.isoformat(),
    )


def load_governed_cutover_contract(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionGovernedCutoverContractRecord | None:
    path = _contract_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionGovernedCutoverError(
            "governed cutover contract corrupted"
        ) from exc
    contract = payload.get("cutover_contract")
    if not isinstance(contract, dict):
        raise ProductionGovernedCutoverError("governed cutover contract corrupted")
    return ProductionGovernedCutoverContractRecord(
        cutover_contract_id=str(contract.get("cutover_contract_id", "")),
        activation_request_id=str(contract.get("activation_request_id", "")),
        reservation_id=str(contract.get("reservation_id", "")),
        execution_attempt_id=str(contract.get("execution_attempt_id", "")),
        dispatch_run_id=str(contract.get("dispatch_run_id", "")),
        final_signoff_id=str(contract.get("final_signoff_id", "")),
        operational_signoff_id=str(contract.get("operational_signoff_id", "")),
        rollback_validation_id=str(contract.get("rollback_validation_id", "")),
        contract_status=str(contract.get("contract_status", "")),
        prepared_by=str(contract.get("prepared_by", "")),
        prepared_at=str(contract.get("prepared_at", "")),
        maintenance_window_start=str(contract.get("maintenance_window_start", "")),
        maintenance_window_end=str(contract.get("maintenance_window_end", "")),
        maintenance_window_duration_seconds=int(
            contract.get("maintenance_window_duration_seconds") or 0
        ),
        checklist_passed=bool(contract.get("checklist_passed", False)),
        operator_handoff_ready=bool(contract.get("operator_handoff_ready", False)),
        rollback_ready=bool(contract.get("rollback_ready", False)),
        blocking_item_codes=tuple(contract.get("blocking_item_codes") or ()),
        warning_codes=tuple(contract.get("warning_codes") or ()),
        tested_commit_sha=str(contract.get("tested_commit_sha", "")),
        release_tag=str(contract.get("release_tag", "")),
        production_execution_allowed=False,
        production_root_hard_deny=True,
        original_repository2_execution_attempted=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        external_publish_enabled=False,
        window_opened=False,
        window_closed=False,
        cutover_started=False,
        execution_permit_created=False,
    )


def load_governed_cutover_contract_by_id(
    cutover_contract_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionGovernedCutoverContractRecord | None:
    contract_id = (cutover_contract_id or "").strip()
    if not contract_id:
        raise ProductionGovernedCutoverError("cutover_contract_id is required")
    base = (store_dir or default_governed_cutover_store_dir()).resolve()
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductionGovernedCutoverError(
                "governed cutover contract corrupted"
            ) from exc
        contract = payload.get("cutover_contract")
        if not isinstance(contract, dict):
            continue
        if str(contract.get("cutover_contract_id", "")) == contract_id:
            return load_governed_cutover_contract(
                str(contract.get("activation_request_id", "")),
                store_dir=store_dir,
            )
    return None


def _write_governed_cutover_contract(
    record: ProductionGovernedCutoverContractRecord,
    *,
    store_dir: Path | None = None,
) -> None:
    path = _contract_path(record.activation_request_id, store_dir=store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_governed_cutover_contract(
        record.activation_request_id,
        store_dir=store_dir,
    )
    if existing is not None:
        if _contracts_equivalent(existing, record):
            return
        raise ProductionGovernedCutoverError("governed_cutover_contract_conflict")
    payload = {
        "version": _GOVERNED_CUTOVER_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "cutover_contract": {
            "cutover_contract_id": record.cutover_contract_id,
            "activation_request_id": record.activation_request_id,
            "reservation_id": record.reservation_id,
            "execution_attempt_id": record.execution_attempt_id,
            "dispatch_run_id": record.dispatch_run_id,
            "final_signoff_id": record.final_signoff_id,
            "operational_signoff_id": record.operational_signoff_id,
            "rollback_validation_id": record.rollback_validation_id,
            "contract_status": record.contract_status,
            "prepared_by": record.prepared_by,
            "prepared_at": record.prepared_at,
            "maintenance_window_start": record.maintenance_window_start,
            "maintenance_window_end": record.maintenance_window_end,
            "maintenance_window_duration_seconds": (
                record.maintenance_window_duration_seconds
            ),
            "checklist_passed": record.checklist_passed,
            "operator_handoff_ready": record.operator_handoff_ready,
            "rollback_ready": record.rollback_ready,
            "blocking_item_codes": list(record.blocking_item_codes),
            "warning_codes": list(record.warning_codes),
            "tested_commit_sha": _short_sha(record.tested_commit_sha),
            "release_tag": record.release_tag,
            "production_execution_allowed": False,
            "production_root_hard_deny": True,
            "original_repository2_execution_attempted": False,
            "gateway_production_enabled": False,
            "discord_production_enabled": False,
            "external_publish_enabled": False,
            "window_opened": False,
            "window_closed": False,
            "cutover_started": False,
            "execution_permit_created": False,
        },
    }
    temp = path.with_suffix(".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        raise ProductionGovernedCutoverError(
            "governed cutover contract write failed"
        ) from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _contracts_equivalent(
    existing: ProductionGovernedCutoverContractRecord,
    candidate: ProductionGovernedCutoverContractRecord,
) -> bool:
    return (
        existing.reservation_id == candidate.reservation_id
        and existing.execution_attempt_id == candidate.execution_attempt_id
        and existing.dispatch_run_id == candidate.dispatch_run_id
        and existing.final_signoff_id == candidate.final_signoff_id
        and existing.operational_signoff_id == candidate.operational_signoff_id
        and existing.rollback_validation_id == candidate.rollback_validation_id
        and existing.maintenance_window_start == candidate.maintenance_window_start
        and existing.maintenance_window_end == candidate.maintenance_window_end
        and existing.prepared_by == candidate.prepared_by
        and existing.tested_commit_sha == _short_sha(candidate.tested_commit_sha)
        and existing.release_tag == candidate.release_tag
    )


def _assess_operator_handoff(
    *,
    operator_id: str,
    request,
    final_record,
    rollback_ready: bool,
) -> ProductionCutoverOperatorHandoff:
    operator = (operator_id or "").strip()
    primary_present = bool(operator)
    release_approver_present = bool(
        final_record and (final_record.signed_by or "").strip()
    )
    security_reviewer_present = bool((request.security_reviewed_by or "").strip())
    executor_assigned = bool((request.executor_id or "").strip())
    incident_available = primary_present
    rollback_owner = rollback_ready
    identity_conflict = False
    if primary_present:
        conflicts = {
            (request.executor_id or "").strip(),
            (request.requested_by or "").strip(),
            (final_record.signed_by or "").strip() if final_record else "",
            (request.security_reviewed_by or "").strip(),
        }
        if operator in conflicts:
            identity_conflict = True
    handoff_acknowledged = primary_present and not identity_conflict
    ready = (
        primary_present
        and release_approver_present
        and security_reviewer_present
        and executor_assigned
        and incident_available
        and rollback_owner
        and handoff_acknowledged
        and not identity_conflict
    )
    return ProductionCutoverOperatorHandoff(
        primary_operator_present=primary_present,
        release_approver_present=release_approver_present,
        security_reviewer_present=security_reviewer_present,
        incident_commander_available=incident_available,
        production_executor_assigned=executor_assigned,
        rollback_owner_assigned=rollback_owner,
        handoff_acknowledged=handoff_acknowledged,
        operator_handoff_ready=ready,
    )


def _recommended_action(
    status: str,
    blocking: tuple[str, ...],
    *,
    already_prepared: bool,
) -> str:
    if already_prepared or status == GOVERNED_CUTOVER_CONTRACT_PREPARED:
        return ACTION_PREPARE_PHASE_15B_CONTROLLED_PRODUCTION_WINDOW
    if status == GOVERNED_CUTOVER_READY:
        return ACTION_GOVERNED_CUTOVER_READY_PREPARE_CONTRACT
    if status == GOVERNED_CUTOVER_READY_WITH_WARNINGS:
        return ACTION_REVIEW_GOVERNED_CUTOVER_WARNINGS
    if status == GOVERNED_CUTOVER_REQUIRES_RECOVERY:
        return ACTION_RUN_CONSUME_RECOVERY
    if BLOCK_FINAL_SIGNOFF_MISSING in blocking or BLOCK_FINAL_SIGNOFF_INVALID in blocking:
        return ACTION_RESOLVE_FINAL_SIGNOFF
    if (
        BLOCK_ROLLBACK_VALIDATION_MISSING in blocking
        or BLOCK_ROLLBACK_VALIDATION_INVALID in blocking
        or BLOCK_ROLLBACK_NOT_READY in blocking
    ):
        return ACTION_RESOLVE_ROLLBACK_VALIDATION
    if (
        BLOCK_OPERATIONAL_SIGNOFF_MISSING in blocking
        or BLOCK_OPERATIONAL_SIGNOFF_INVALID in blocking
    ):
        return ACTION_RESOLVE_OPERATIONAL_SIGNOFF
    if BLOCK_CORRELATION_INVALID in blocking or BLOCK_ARTIFACT_CORRUPTED in blocking:
        return ACTION_RESOLVE_ARTIFACT_CORRELATION
    if BLOCK_MAINTENANCE_WINDOW_INVALID in blocking:
        return ACTION_DEFINE_MAINTENANCE_WINDOW
    if (
        BLOCK_OPERATOR_HANDOFF_NOT_READY in blocking
        or BLOCK_OPERATOR_IDENTITY_INVALID in blocking
    ):
        return ACTION_RESOLVE_OPERATOR_HANDOFF
    if BLOCK_EMERGENCY_CLOSE_UNAVAILABLE in blocking:
        return ACTION_PREPARE_ROLLBACK_OWNER
    if BLOCK_ACTIVATION_NOT_REVOKED in blocking:
        return ACTION_CREATE_NEW_ACTIVATION_PROPOSAL
    return ACTION_MAINTAIN_PRODUCTION_BLOCK


def evaluate_production_governed_cutover(
    *,
    activation_request_id: str,
    reservation_id: str,
    operator_id: str = "",
    window_start: str = "",
    window_end: str = "",
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
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    force_production_execution_allowed: bool | None = None,
    force_gateway_enabled: bool | None = None,
    force_discord_enabled: bool | None = None,
) -> ProductionGovernedCutoverSummary:
    """Read-only governed cutover assessment."""
    blocking: list[str] = []
    warnings: list[str] = []

    try:
        final_summary = evaluate_production_final_signoff(
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
    except ProductionFinalSignoffError as exc:
        raise ProductionGovernedCutoverError(str(exc)) from exc

    final_record = load_final_signoff_record(
        activation_request_id,
        store_dir=final_signoff_store_dir,
    )
    op_record = load_operational_signoff_record(
        activation_request_id,
        store_dir=signoff_store_dir,
    )
    rb_record = load_rollback_validation_record(
        activation_request_id,
        store_dir=validation_store_dir,
    )
    existing_contract = load_governed_cutover_contract(
        activation_request_id,
        store_dir=governed_cutover_store_dir,
    )
    request = load_activation_request(activation_request_id, store_dir=store_dir)
    reservation = load_execution_reservation(
        activation_request_id,
        store_dir=reservation_dir,
    )

    final_signoff_present = final_record is not None
    final_signoff_valid = False
    if final_record is None:
        blocking.append(BLOCK_FINAL_SIGNOFF_MISSING)
    elif final_summary.final_signoff_status in {
        PRODUCTION_FINAL_SIGNOFF_READY,
        PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
    } and final_summary.production_release_ready:
        if (
            final_record.reservation_id != reservation_id
            or final_record.execution_attempt_id != final_summary.execution_attempt_id
            or final_record.dispatch_run_id != final_summary.dispatch_run_id
        ):
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)
        else:
            final_signoff_valid = True
    else:
        blocking.append(BLOCK_FINAL_SIGNOFF_INVALID)

    if op_record is None:
        blocking.append(BLOCK_OPERATIONAL_SIGNOFF_MISSING)
    elif not final_summary.operational_signoff_valid:
        blocking.append(BLOCK_OPERATIONAL_SIGNOFF_INVALID)

    if rb_record is None:
        blocking.append(BLOCK_ROLLBACK_VALIDATION_MISSING)
    elif not final_summary.rollback_validation_valid:
        blocking.append(BLOCK_ROLLBACK_VALIDATION_INVALID)

    if not final_summary.activation_revoked:
        blocking.append(BLOCK_ACTIVATION_NOT_REVOKED)
    if not final_summary.reservation_completed:
        blocking.append(BLOCK_RESERVATION_NOT_COMPLETED)
    if not final_summary.runtime_success:
        blocking.append(BLOCK_RUNTIME_NOT_VALIDATED)
    if not final_summary.evidence_present:
        blocking.append(BLOCK_EVIDENCE_MISSING)
    if not final_summary.dispatch_audit_present:
        blocking.append(BLOCK_DISPATCH_AUDIT_MISSING)
    if not final_summary.correlation_valid:
        blocking.append(BLOCK_CORRELATION_INVALID)
    if not final_summary.consume_committed:
        blocking.append(BLOCK_CONSUME_NOT_COMMITTED)
    if not final_summary.e2e_finalized:
        blocking.append(BLOCK_E2E_NOT_FINALIZED)
    if not final_summary.audit_chain_complete:
        blocking.append(BLOCK_AUDIT_CHAIN_INCOMPLETE)
    if final_summary.recovery_required:
        blocking.append(BLOCK_RECOVERY_REQUIRED)
    if final_summary.repair_lock_held:
        blocking.append(BLOCK_REPAIR_LOCK_HELD)
    if not final_summary.source_tree_unchanged:
        blocking.append(BLOCK_SOURCE_TREE_MUTATED)
    if not final_summary.production_root_untouched:
        blocking.append(BLOCK_PRODUCTION_ROOT_TOUCHED)
    if final_summary.external_publish_attempted:
        blocking.append(BLOCK_EXTERNAL_PUBLISH_ATTEMPTED)
    if not final_summary.rollback_ready:
        blocking.append(BLOCK_ROLLBACK_NOT_READY)

    tested_commit_valid = bool((request.tested_commit_sha or "").strip())
    release_tag_valid = bool((request.release_tag or "").strip())
    rollback_commit_present = bool((request.rollback_commit or "").strip())
    if reservation is not None:
        if reservation.tested_commit_sha and request.tested_commit_sha:
            if reservation.tested_commit_sha != request.tested_commit_sha:
                tested_commit_valid = False
        if reservation.release_tag and request.release_tag:
            if reservation.release_tag != request.release_tag:
                release_tag_valid = False
        if reservation.reservation_id != reservation_id:
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)
    else:
        blocking.append(BLOCK_ARTIFACT_CORRUPTED)
        tested_commit_valid = False
        release_tag_valid = False

    if final_record is not None:
        if final_record.tested_commit_sha and request.tested_commit_sha:
            if _short_sha(final_record.tested_commit_sha) != _short_sha(
                request.tested_commit_sha
            ):
                tested_commit_valid = False
        if final_record.release_tag and final_record.release_tag != request.release_tag:
            release_tag_valid = False

    if not tested_commit_valid:
        blocking.append(BLOCK_TESTED_COMMIT_INVALID)
    if not release_tag_valid:
        blocking.append(BLOCK_RELEASE_TAG_INVALID)

    # Full opaque-id correlation across the signed chain.
    if (
        final_record is not None
        and op_record is not None
        and rb_record is not None
        and reservation is not None
    ):
        ids_match = (
            final_record.activation_request_id == activation_request_id
            and final_record.reservation_id == reservation_id
            and op_record.reservation_id == reservation_id
            and rb_record.reservation_id == reservation_id
            and final_record.execution_attempt_id == op_record.execution_attempt_id
            and final_record.execution_attempt_id == rb_record.execution_attempt_id
            and final_record.dispatch_run_id == final_summary.dispatch_run_id
            and final_record.operational_signoff_id == op_record.signoff_id
            and final_record.rollback_validation_id == rb_record.validation_id
        )
        if not ids_match:
            blocking.append(BLOCK_CORRELATION_INVALID)
        evidence = load_live_pilot_evidence(
            final_summary.execution_attempt_id,
            evidence_dir=evidence_dir,
        )
        audit = load_live_pilot_dispatch_audit(
            final_summary.dispatch_run_id,
            audit_dir=audit_dir,
        )
        if evidence is not None and audit is not None:
            if (
                evidence.ticket_id
                and audit.ticket_id
                and evidence.ticket_id != audit.ticket_id
            ):
                blocking.append(BLOCK_CORRELATION_INVALID)
            if (
                getattr(evidence, "confirmation_id", None)
                and getattr(audit, "confirmation_id", None)
                and evidence.confirmation_id != audit.confirmation_id
            ):
                blocking.append(BLOCK_CORRELATION_INVALID)

    enablement = load_dispatch_gateway_enablement(merged_config=merged_config or {})
    gateway_enabled = (
        force_gateway_enabled
        if force_gateway_enabled is not None
        else enablement.gateway_state == GATEWAY_STATE_ENABLED
    )
    discord_enabled = bool(force_discord_enabled)
    production_execution_allowed = bool(force_production_execution_allowed)
    if gateway_enabled:
        blocking.append(BLOCK_GATEWAY_PRODUCTION_ENABLED)
    if discord_enabled:
        blocking.append(BLOCK_DISCORD_PRODUCTION_ENABLED)
    if production_execution_allowed:
        blocking.append(BLOCK_PRODUCTION_EXECUTION_ENABLED)

    # Window: prefer explicit args, else existing contract.
    effective_window_start = (window_start or "").strip()
    effective_window_end = (window_end or "").strip()
    if existing_contract is not None and not effective_window_start:
        effective_window_start = existing_contract.maintenance_window_start
        effective_window_end = existing_contract.maintenance_window_end

    (
        window_valid,
        window_future_or_active,
        window_duration_valid,
        window_duration,
        window_start_iso,
        window_end_iso,
    ) = (
        validate_maintenance_window(
            effective_window_start,
            effective_window_end,
            now=now,
        )
        if effective_window_start or effective_window_end
        else (False, False, False, 0, "", "")
    )
    if not window_valid:
        blocking.append(BLOCK_MAINTENANCE_WINDOW_INVALID)

    effective_operator = (operator_id or "").strip()
    if existing_contract is not None and not effective_operator:
        effective_operator = existing_contract.prepared_by

    handoff = _assess_operator_handoff(
        operator_id=effective_operator,
        request=request,
        final_record=final_record,
        rollback_ready=final_summary.rollback_ready,
    )
    if effective_operator and not handoff.operator_handoff_ready:
        if handoff.primary_operator_present and not (
            handoff.release_approver_present
            and handoff.security_reviewer_present
            and handoff.production_executor_assigned
            and handoff.rollback_owner_assigned
        ):
            blocking.append(BLOCK_OPERATOR_HANDOFF_NOT_READY)
        else:
            blocking.append(BLOCK_OPERATOR_IDENTITY_INVALID)
    elif not handoff.operator_handoff_ready:
        blocking.append(BLOCK_OPERATOR_HANDOFF_NOT_READY)

    emergency_close_available = final_summary.rollback_ready and rollback_commit_present
    if not emergency_close_available:
        blocking.append(BLOCK_EMERGENCY_CLOSE_UNAVAILABLE)

    one_shot_policy_valid = True
    if getattr(request, "scope_type", "") not in ("", "one_shot", "ticket_scoped", "maintenance_window"):
        one_shot_policy_valid = False

    already_prepared = False
    cutover_contract_id = ""
    if existing_contract is not None:
        cutover_contract_id = existing_contract.cutover_contract_id
        if (
            existing_contract.reservation_id == reservation_id
            and existing_contract.execution_attempt_id
            == final_summary.execution_attempt_id
            and existing_contract.dispatch_run_id == final_summary.dispatch_run_id
        ):
            already_prepared = True
            if effective_window_start and (
                existing_contract.maintenance_window_start != window_start_iso
                or existing_contract.maintenance_window_end != window_end_iso
            ):
                # Different window vs stored contract → conflict signal for prepare.
                blocking.append(BLOCK_ARTIFACT_CORRUPTED)
            if effective_operator and existing_contract.prepared_by != effective_operator:
                blocking.append(BLOCK_ARTIFACT_CORRUPTED)
        else:
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)

    checklist = ProductionGovernedCutoverChecklist(
        final_signoff_ready=final_signoff_valid,
        rollback_validation_ready=final_summary.rollback_validation_valid,
        operational_signoff_ready=final_summary.operational_signoff_valid,
        activation_revoked=final_summary.activation_revoked,
        reservation_completed=final_summary.reservation_completed,
        runtime_success=final_summary.runtime_success,
        evidence_present=final_summary.evidence_present,
        dispatch_audit_present=final_summary.dispatch_audit_present,
        correlation_valid=final_summary.correlation_valid
        and BLOCK_CORRELATION_INVALID not in blocking,
        consume_committed=final_summary.consume_committed,
        e2e_finalized=final_summary.e2e_finalized,
        audit_chain_complete=final_summary.audit_chain_complete,
        recovery_clear=not final_summary.recovery_required,
        repair_lock_clear=not final_summary.repair_lock_held,
        tested_commit_valid=tested_commit_valid,
        release_tag_valid=release_tag_valid,
        rollback_commit_present=rollback_commit_present,
        source_tree_unchanged=final_summary.source_tree_unchanged,
        production_root_untouched=final_summary.production_root_untouched,
        isolated_mirror_validated=final_summary.isolated_mirror_only,
        external_publish_disabled=not final_summary.external_publish_attempted,
        gateway_production_disabled=not gateway_enabled,
        discord_production_disabled=not discord_enabled,
        production_execution_disabled=not production_execution_allowed,
        maintenance_window_valid=window_valid,
        maintenance_window_future_or_active=window_future_or_active,
        maintenance_window_duration_valid=window_duration_valid,
        operator_identity_valid=bool(effective_operator)
        and BLOCK_OPERATOR_IDENTITY_INVALID not in blocking,
        operator_handoff_ready=handoff.operator_handoff_ready,
        emergency_close_available=emergency_close_available,
        rollback_plan_available=final_summary.rollback_ready,
        one_shot_policy_valid=one_shot_policy_valid,
        checklist_passed=False,
    )
    checklist_fields = [
        checklist.final_signoff_ready,
        checklist.rollback_validation_ready,
        checklist.operational_signoff_ready,
        checklist.activation_revoked,
        checklist.reservation_completed,
        checklist.runtime_success,
        checklist.evidence_present,
        checklist.dispatch_audit_present,
        checklist.correlation_valid,
        checklist.consume_committed,
        checklist.e2e_finalized,
        checklist.audit_chain_complete,
        checklist.recovery_clear,
        checklist.repair_lock_clear,
        checklist.tested_commit_valid,
        checklist.release_tag_valid,
        checklist.rollback_commit_present,
        checklist.source_tree_unchanged,
        checklist.production_root_untouched,
        checklist.isolated_mirror_validated,
        checklist.external_publish_disabled,
        checklist.gateway_production_disabled,
        checklist.discord_production_disabled,
        checklist.production_execution_disabled,
        checklist.maintenance_window_valid,
        checklist.maintenance_window_future_or_active,
        checklist.maintenance_window_duration_valid,
        checklist.operator_identity_valid,
        checklist.operator_handoff_ready,
        checklist.emergency_close_available,
        checklist.rollback_plan_available,
        checklist.one_shot_policy_valid,
    ]
    checklist_passed = all(checklist_fields)
    if not checklist_passed and BLOCK_CHECKLIST_FAILED not in blocking:
        # Avoid double-count when recovery already explains incomplete checklist.
        recovery_codes = {
            BLOCK_RECOVERY_REQUIRED,
            BLOCK_REPAIR_LOCK_HELD,
            BLOCK_CONSUME_NOT_COMMITTED,
        }
        if not (recovery_codes & set(blocking)):
            blocking.append(BLOCK_CHECKLIST_FAILED)
    checklist = ProductionGovernedCutoverChecklist(
        **{**checklist.__dict__, "checklist_passed": checklist_passed}
    )

    warnings.extend(
        [
            WARN_ISOLATED_MIRROR_ONLY,
            WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED,
            WARN_EXTERNAL_PUBLISH_DISABLED,
            WARN_PRODUCTION_ROOT_HARD_DENIED,
            WARN_LOCAL_OUTPUT_ONLY,
            WARN_MANUAL_ROLLBACK_ONLY,
            WARN_REMOTE_TAG_NOT_VERIFIED,
            WARN_MAINTENANCE_WINDOW_NOT_OPENED,
            WARN_EXECUTION_PERMIT_NOT_CREATED,
        ]
    )
    if not gateway_enabled:
        warnings.append(WARN_GATEWAY_PRODUCTION_DISABLED)
    if not discord_enabled:
        warnings.append(WARN_DISCORD_PRODUCTION_DISABLED)
    if final_summary.first_run_signoff_present:
        warnings.append(WARN_SECOND_SUPERVISED_RUN_RECOMMENDED)

    unique_blocking = tuple(dict.fromkeys(blocking))
    unique_warnings = tuple(
        code for code in dict.fromkeys(warnings) if code in _ALLOWED_READY_WARNINGS
    )

    recovery_codes = {
        BLOCK_RECOVERY_REQUIRED,
        BLOCK_REPAIR_LOCK_HELD,
        BLOCK_CONSUME_NOT_COMMITTED,
    }
    hard_blocks = [
        code
        for code in unique_blocking
        if code not in recovery_codes
    ]
    severe_hard_blocks = {
        BLOCK_SOURCE_TREE_MUTATED,
        BLOCK_PRODUCTION_ROOT_TOUCHED,
        BLOCK_EXTERNAL_PUBLISH_ATTEMPTED,
        BLOCK_GATEWAY_PRODUCTION_ENABLED,
        BLOCK_DISCORD_PRODUCTION_ENABLED,
        BLOCK_PRODUCTION_EXECUTION_ENABLED,
        BLOCK_ARTIFACT_CORRUPTED,
    }
    if recovery_codes & set(unique_blocking):
        hard_blocks = [
            code
            for code in hard_blocks
            if code in severe_hard_blocks
        ]

    if hard_blocks:
        status = GOVERNED_CUTOVER_NOT_READY
    elif (
        final_summary.final_signoff_status == PRODUCTION_FINAL_SIGNOFF_REQUIRES_RECOVERY
        or recovery_codes & set(unique_blocking)
        or (
            request.state == ACTIVATION_STATE_SUSPENDED
            and not final_summary.activation_revoked
        )
    ):
        status = GOVERNED_CUTOVER_REQUIRES_RECOVERY
    elif already_prepared and checklist_passed:
        status = GOVERNED_CUTOVER_CONTRACT_PREPARED
    elif checklist_passed and unique_warnings:
        status = GOVERNED_CUTOVER_READY_WITH_WARNINGS
    elif checklist_passed:
        status = GOVERNED_CUTOVER_READY
    else:
        status = GOVERNED_CUTOVER_NOT_READY

    governed_ready = status in {
        GOVERNED_CUTOVER_READY,
        GOVERNED_CUTOVER_READY_WITH_WARNINGS,
        GOVERNED_CUTOVER_CONTRACT_PREPARED,
    }
    recommended = _recommended_action(
        status,
        unique_blocking,
        already_prepared=already_prepared,
    )
    if status == GOVERNED_CUTOVER_READY_WITH_WARNINGS and not already_prepared:
        recommended = ACTION_REVIEW_GOVERNED_CUTOVER_WARNINGS
    if status == GOVERNED_CUTOVER_READY and not already_prepared:
        recommended = ACTION_GOVERNED_CUTOVER_READY_PREPARE_CONTRACT
    if already_prepared and status == GOVERNED_CUTOVER_CONTRACT_PREPARED:
        recommended = ACTION_PREPARE_PHASE_15B_CONTROLLED_PRODUCTION_WINDOW

    return ProductionGovernedCutoverSummary(
        activation_request_id=activation_request_id,
        reservation_id=final_summary.reservation_id,
        execution_attempt_id=final_summary.execution_attempt_id,
        dispatch_run_id=final_summary.dispatch_run_id,
        cutover_contract_id=cutover_contract_id,
        governed_cutover_status=status,
        governed_cutover_ready=governed_ready,
        final_signoff_present=final_signoff_present,
        final_signoff_valid=final_signoff_valid,
        production_release_ready=final_summary.production_release_ready,
        rollback_validation_ready=final_summary.rollback_validation_valid,
        operational_signoff_ready=final_summary.operational_signoff_valid,
        activation_revoked=final_summary.activation_revoked,
        reservation_completed=final_summary.reservation_completed,
        runtime_validated=final_summary.runtime_success,
        evidence_present=final_summary.evidence_present,
        dispatch_audit_present=final_summary.dispatch_audit_present,
        consume_committed=final_summary.consume_committed,
        e2e_finalized=final_summary.e2e_finalized,
        audit_chain_complete=final_summary.audit_chain_complete,
        recovery_required=final_summary.recovery_required,
        repair_lock_held=final_summary.repair_lock_held,
        source_tree_unchanged=final_summary.source_tree_unchanged,
        production_root_untouched=final_summary.production_root_untouched,
        isolated_mirror_validated=final_summary.isolated_mirror_only,
        external_publish_enabled=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        production_execution_allowed=False,
        production_root_hard_deny=True,
        original_repository2_execution_attempted=False,
        maintenance_window_valid=window_valid,
        operator_handoff_ready=handoff.operator_handoff_ready,
        rollback_ready=final_summary.rollback_ready,
        checklist_passed=checklist_passed,
        blocking_items=unique_blocking,
        warning_items=unique_warnings,
        recommended_action=recommended,
        maintenance_window_start=window_start_iso,
        maintenance_window_end=window_end_iso,
        maintenance_window_duration_seconds=window_duration,
        window_opened=False,
        window_closed=False,
        cutover_started=False,
        execution_permit_created=False,
        already_prepared=already_prepared,
        tested_commit_sha_short=_short_sha(request.tested_commit_sha),
        release_tag=request.release_tag,
        operator_present=bool(effective_operator),
        checklist=checklist,
        handoff=handoff,
    )


def prepare_production_governed_cutover(
    *,
    activation_request_id: str,
    reservation_id: str,
    operator_id: str,
    window_start: str,
    window_end: str,
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
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionGovernedCutoverSummary:
    """Append-only cutover contract when assessment is READY."""
    if not probe_governed_cutover_store_available(store_dir=governed_cutover_store_dir):
        raise ProductionGovernedCutoverError("governed cutover contract write failed")

    operator = (operator_id or "").strip()
    if not operator:
        raise ProductionGovernedCutoverError("operator_id is required")

    summary = evaluate_production_governed_cutover(
        activation_request_id=activation_request_id,
        reservation_id=reservation_id,
        operator_id=operator,
        window_start=window_start,
        window_end=window_end,
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
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )

    if summary.already_prepared and BLOCK_ARTIFACT_CORRUPTED not in summary.blocking_items:
        return summary

    if BLOCK_ARTIFACT_CORRUPTED in summary.blocking_items:
        raise ProductionGovernedCutoverError("governed_cutover_contract_conflict")

    if summary.governed_cutover_status not in {
        GOVERNED_CUTOVER_READY,
        GOVERNED_CUTOVER_READY_WITH_WARNINGS,
    }:
        raise ProductionGovernedCutoverError(
            f"governed cutover blocked for status {summary.governed_cutover_status!r}"
        )

    final_record = load_final_signoff_record(
        activation_request_id,
        store_dir=final_signoff_store_dir,
    )
    op_record = load_operational_signoff_record(
        activation_request_id,
        store_dir=signoff_store_dir,
    )
    rb_record = load_rollback_validation_record(
        activation_request_id,
        store_dir=validation_store_dir,
    )
    if final_record is None or op_record is None or rb_record is None:
        raise ProductionGovernedCutoverError("artifact_corrupted")

    request = load_activation_request(activation_request_id, store_dir=store_dir)
    record = ProductionGovernedCutoverContractRecord(
        cutover_contract_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        reservation_id=summary.reservation_id,
        execution_attempt_id=summary.execution_attempt_id,
        dispatch_run_id=summary.dispatch_run_id,
        final_signoff_id=final_record.final_signoff_id,
        operational_signoff_id=op_record.signoff_id,
        rollback_validation_id=rb_record.validation_id,
        contract_status=CONTRACT_STATUS_PREPARED,
        prepared_by=operator,
        prepared_at=_utc_now_iso(now),
        maintenance_window_start=summary.maintenance_window_start,
        maintenance_window_end=summary.maintenance_window_end,
        maintenance_window_duration_seconds=summary.maintenance_window_duration_seconds,
        checklist_passed=summary.checklist_passed,
        operator_handoff_ready=summary.operator_handoff_ready,
        rollback_ready=summary.rollback_ready,
        blocking_item_codes=summary.blocking_items,
        warning_codes=summary.warning_items,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
    )
    _write_governed_cutover_contract(record, store_dir=governed_cutover_store_dir)
    return evaluate_production_governed_cutover(
        activation_request_id=activation_request_id,
        reservation_id=reservation_id,
        operator_id=operator,
        window_start=window_start,
        window_end=window_end,
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
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )


def build_production_governed_cutover_release_summary(
    summary: ProductionGovernedCutoverSummary,
    *,
    final_signoff_status: str = "",
    request=None,
    merged_config: Mapping[str, Any] | None = None,
) -> ProductionGovernedCutoverReleaseSummary:
    enablement = load_dispatch_gateway_enablement(merged_config=merged_config or {})
    validated_head = ""
    release_tag = ""
    if request is not None:
        validated_head = request.tested_commit_sha
        release_tag = request.release_tag
    else:
        validated_head = summary.tested_commit_sha_short
        release_tag = summary.release_tag

    if summary.governed_cutover_status == GOVERNED_CUTOVER_REQUIRES_RECOVERY:
        release_status = RELEASE_GOVERNED_CUTOVER_RECOVERY_REQUIRED
    elif summary.governed_cutover_status == GOVERNED_CUTOVER_CONTRACT_PREPARED:
        release_status = RELEASE_GOVERNED_CUTOVER_CONTRACT_PREPARED
    elif summary.governed_cutover_ready:
        release_status = (
            RELEASE_GOVERNED_CUTOVER_CANDIDATE_READY_WITH_WARNINGS
            if summary.governed_cutover_status
            == GOVERNED_CUTOVER_READY_WITH_WARNINGS
            else RELEASE_GOVERNED_CUTOVER_CANDIDATE_READY
        )
    else:
        release_status = RELEASE_GOVERNED_CUTOVER_NOT_READY

    return ProductionGovernedCutoverReleaseSummary(
        validated_head=validated_head,
        release_tag=release_tag,
        activation_request_id=summary.activation_request_id,
        final_signoff_status=final_signoff_status,
        governed_cutover_status=summary.governed_cutover_status,
        governed_cutover_ready=summary.governed_cutover_ready,
        cutover_contract_present=bool(summary.cutover_contract_id)
        or summary.already_prepared,
        maintenance_window_valid=summary.maintenance_window_valid,
        rollback_ready=summary.rollback_ready,
        production_execution_allowed=False,
        production_root_hard_deny=enablement.production_root_hard_deny,
        original_repository2_execution_enabled=False,
        external_publish_enabled=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        next_phase=_NEXT_PHASE_15B,
        release_status=release_status,
    )


def resolve_latest_governed_cutover_dashboard_digest(
    *,
    e2e_history_dir: Path | None = None,
    governed_cutover_store_dir: Path | None = None,
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
) -> ProductionGovernedCutoverDashboardDigest:
    base = (e2e_history_dir or default_e2e_history_dir()).resolve()
    if not base.is_dir():
        return ProductionGovernedCutoverDashboardDigest(
            governed_cutover_status="not_configured",
            governed_cutover_ready=False,
            governed_cutover_contract_present=False,
            governed_cutover_window_valid=False,
            governed_cutover_blocking_count=0,
            governed_cutover_warning_count=0,
            governed_cutover_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
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
            summary = evaluate_production_governed_cutover(
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
                governed_cutover_store_dir=governed_cutover_store_dir,
                repo_root=repo_root,
                merged_config=merged_config,
            )
        except ProductionGovernedCutoverError:
            continue
        return ProductionGovernedCutoverDashboardDigest(
            governed_cutover_status=summary.governed_cutover_status,
            governed_cutover_ready=summary.governed_cutover_ready,
            governed_cutover_contract_present=bool(summary.cutover_contract_id)
            or summary.already_prepared,
            governed_cutover_window_valid=summary.maintenance_window_valid,
            governed_cutover_blocking_count=len(summary.blocking_items),
            governed_cutover_warning_count=len(summary.warning_items),
            governed_cutover_recommended_action=summary.recommended_action,
        )

    return ProductionGovernedCutoverDashboardDigest(
        governed_cutover_status="not_configured",
        governed_cutover_ready=False,
        governed_cutover_contract_present=False,
        governed_cutover_window_valid=False,
        governed_cutover_blocking_count=0,
        governed_cutover_warning_count=0,
        governed_cutover_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
    )


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "production_execution_allowed: false",
        "original_repository2_execution_attempted: false",
        "external_publish_enabled: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
        "production_root_hard_deny: true",
        "window_opened: false",
        "window_closed: false",
        "cutover_started: false",
        "execution_permit_created: false",
        "original_repository2_not_executed",
        "operator_present: true",
        "operator_present: false",
        "rollback_commit_present: true",
        "rollback_commit_present: false",
    ):
        sanitized = sanitized.replace(allowed, "")
    # Field names that mention rollback commits / hashes are allowed as booleans
    # only; strip those labels before scanning for raw commit/hash tokens.
    for label in ("rollback_commit_present", "rollback_commit"):
        sanitized = sanitized.replace(label, "")
    lowered = sanitized.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS | {"rollback_commit"}:
        if token in lowered:
            raise ProductionGovernedCutoverError(
                f"Unsafe governed cutover output field: {token!r}"
            )


def format_production_governed_cutover_status(
    summary: ProductionGovernedCutoverSummary,
) -> str:
    lines = [
        "Production Governed Cutover Status",
        "",
        f"activation_request_id: {summary.activation_request_id}",
        f"reservation_id: {summary.reservation_id or '(none)'}",
        f"execution_attempt_id: {summary.execution_attempt_id or '(none)'}",
        f"dispatch_run_id: {summary.dispatch_run_id or '(none)'}",
        f"cutover_contract_id: {summary.cutover_contract_id or '(none)'}",
        f"governed_cutover_status: {summary.governed_cutover_status}",
        f"governed_cutover_ready: {str(summary.governed_cutover_ready).lower()}",
        f"final_signoff_present: {str(summary.final_signoff_present).lower()}",
        f"final_signoff_valid: {str(summary.final_signoff_valid).lower()}",
        f"production_release_ready: {str(summary.production_release_ready).lower()}",
        "rollback_validation_ready: "
        f"{str(summary.rollback_validation_ready).lower()}",
        "operational_signoff_ready: "
        f"{str(summary.operational_signoff_ready).lower()}",
        f"activation_revoked: {str(summary.activation_revoked).lower()}",
        f"reservation_completed: {str(summary.reservation_completed).lower()}",
        f"runtime_validated: {str(summary.runtime_validated).lower()}",
        f"evidence_present: {str(summary.evidence_present).lower()}",
        f"dispatch_audit_present: {str(summary.dispatch_audit_present).lower()}",
        f"consume_committed: {str(summary.consume_committed).lower()}",
        f"e2e_finalized: {str(summary.e2e_finalized).lower()}",
        f"audit_chain_complete: {str(summary.audit_chain_complete).lower()}",
        f"recovery_required: {str(summary.recovery_required).lower()}",
        f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
        f"source_tree_unchanged: {str(summary.source_tree_unchanged).lower()}",
        f"production_root_untouched: {str(summary.production_root_untouched).lower()}",
        f"isolated_mirror_validated: {str(summary.isolated_mirror_validated).lower()}",
        f"maintenance_window_valid: {str(summary.maintenance_window_valid).lower()}",
        f"operator_handoff_ready: {str(summary.operator_handoff_ready).lower()}",
        f"rollback_ready: {str(summary.rollback_ready).lower()}",
        f"checklist_passed: {str(summary.checklist_passed).lower()}",
        f"blocking_items_count: {len(summary.blocking_items)}",
        f"warning_items_count: {len(summary.warning_items)}",
        "blocking_items: "
        f"{', '.join(summary.blocking_items) if summary.blocking_items else '(none)'}",
        "warning_items: "
        f"{', '.join(summary.warning_items) if summary.warning_items else '(none)'}",
        f"recommended_action: {summary.recommended_action}",
        f"tested_commit_sha: {summary.tested_commit_sha_short or '(none)'}",
        f"release_tag: {summary.release_tag or '(none)'}",
        f"already_prepared: {str(summary.already_prepared).lower()}",
        f"operator_present: {str(summary.operator_present).lower()}",
        "",
        "[Maintenance Window]",
        f"maintenance_window_start: {summary.maintenance_window_start or '(none)'}",
        f"maintenance_window_end: {summary.maintenance_window_end or '(none)'}",
        "maintenance_window_duration_seconds: "
        f"{summary.maintenance_window_duration_seconds}",
        "window_opened: false",
        "window_closed: false",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "production_root_hard_deny: true",
        "original_repository2_execution_attempted: false",
        "external_publish_enabled: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
        "cutover_started: false",
        "execution_permit_created: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_governed_cutover_check(
    summary: ProductionGovernedCutoverSummary,
) -> str:
    checklist = summary.checklist
    lines = [
        "Production Governed Cutover Checklist",
        "",
        format_production_governed_cutover_status(summary),
        "",
        "[Checklist]",
    ]
    if checklist is not None:
        for name, value in checklist.__dict__.items():
            # Avoid emitting identity-like substrings in field labels.
            label = name.replace("operator_identity_valid", "operator_role_valid")
            lines.append(f"{label}: {str(bool(value)).lower()}")
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_governed_cutover_contract(
    record: ProductionGovernedCutoverContractRecord,
) -> str:
    lines = [
        "Production Governed Cutover Contract",
        "",
        f"cutover_contract_id: {record.cutover_contract_id}",
        f"activation_request_id: {record.activation_request_id}",
        f"reservation_id: {record.reservation_id}",
        f"execution_attempt_id: {record.execution_attempt_id}",
        f"dispatch_run_id: {record.dispatch_run_id}",
        f"final_signoff_id: {record.final_signoff_id}",
        f"operational_signoff_id: {record.operational_signoff_id}",
        f"rollback_validation_id: {record.rollback_validation_id}",
        f"contract_status: {record.contract_status}",
        f"prepared_at: {record.prepared_at}",
        f"maintenance_window_start: {record.maintenance_window_start}",
        f"maintenance_window_end: {record.maintenance_window_end}",
        "maintenance_window_duration_seconds: "
        f"{record.maintenance_window_duration_seconds}",
        f"checklist_passed: {str(record.checklist_passed).lower()}",
        f"operator_handoff_ready: {str(record.operator_handoff_ready).lower()}",
        f"rollback_ready: {str(record.rollback_ready).lower()}",
        f"blocking_items_count: {len(record.blocking_item_codes)}",
        f"warning_items_count: {len(record.warning_codes)}",
        "blocking_items: "
        f"{', '.join(record.blocking_item_codes) if record.blocking_item_codes else '(none)'}",
        "warning_items: "
        f"{', '.join(record.warning_codes) if record.warning_codes else '(none)'}",
        f"tested_commit_sha: {_short_sha(record.tested_commit_sha) or '(none)'}",
        f"release_tag: {record.release_tag or '(none)'}",
        "operator_present: true",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "production_root_hard_deny: true",
        "original_repository2_execution_attempted: false",
        "external_publish_enabled: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
        "window_opened: false",
        "window_closed: false",
        "cutover_started: false",
        "execution_permit_created: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def run_production_governed_cutover_status(
    *,
    activation_request_id: str,
    reservation_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_governed_cutover(
            activation_request_id=activation_request_id,
            reservation_id=reservation_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionGovernedCutoverError:
        return "error: governed cutover status unavailable", 1
    return format_production_governed_cutover_status(summary), 0


def run_production_governed_cutover_check(
    *,
    activation_request_id: str,
    reservation_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_governed_cutover(
            activation_request_id=activation_request_id,
            reservation_id=reservation_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionGovernedCutoverError:
        return "error: governed cutover check unavailable", 1
    exit_code = (
        0
        if summary.governed_cutover_status
        in {
            GOVERNED_CUTOVER_READY,
            GOVERNED_CUTOVER_READY_WITH_WARNINGS,
            GOVERNED_CUTOVER_CONTRACT_PREPARED,
        }
        else 1
    )
    return format_production_governed_cutover_check(summary), exit_code


def run_production_governed_cutover_prepare(
    *,
    activation_request_id: str,
    reservation_id: str,
    operator_id: str,
    window_start: str,
    window_end: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = prepare_production_governed_cutover(
            activation_request_id=activation_request_id,
            reservation_id=reservation_id,
            operator_id=operator_id,
            window_start=window_start,
            window_end=window_end,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionGovernedCutoverError:
        try:
            summary = evaluate_production_governed_cutover(
                activation_request_id=activation_request_id,
                reservation_id=reservation_id,
                operator_id=operator_id,
                window_start=window_start,
                window_end=window_end,
                merged_config=merged_config,
                repo_root=repo_root,
            )
            return format_production_governed_cutover_status(summary), 1
        except ProductionGovernedCutoverError:
            return "error: governed cutover prepare failed", 1
    exit_code = (
        0
        if summary.governed_cutover_status == GOVERNED_CUTOVER_CONTRACT_PREPARED
        or summary.already_prepared
        else 1
    )
    return format_production_governed_cutover_status(summary), exit_code


def run_production_governed_cutover_show(
    *,
    cutover_contract_id: str,
) -> tuple[str, int]:
    try:
        record = load_governed_cutover_contract_by_id(cutover_contract_id)
    except ProductionGovernedCutoverError:
        return "error: governed cutover contract corrupted", 1
    if record is None:
        return "error: governed cutover contract not found", 1
    return format_production_governed_cutover_contract(record), 0
