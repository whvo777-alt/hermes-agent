"""Production live pilot rollback validation — Phase 14I.

Read-only correlation, commit/tag/rollback integrity, and rollback readiness checks.
No subprocess, git mutation, Repository2 original execution, or actual rollback.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_cli_production_activation import resolve_git_head_commit
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_RECOVERY_REQUIRED,
    CONSUME_STATE_UNCONSUMED,
    assess_consume_status,
    read_consume_transaction,
)
from agent.coo.dispatch_execution_audit import default_audit_dir
from agent.coo.dispatch_gateway_enablement import load_dispatch_gateway_enablement
from agent.coo.production_activation_active_gate import (
    _probe_recovery_required,
    _probe_repair_lock_held,
)
from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_COMPLETED,
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
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
    ActivationRequest,
    ProductionActivationStateError,
    _SHA_RE,
    _TAG_RE,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    load_activation_request,
)
from agent.coo.production_executor_confirmation import read_confirmation
from agent.coo.production_executor_factory import default_evidence_dir
from agent.coo.production_live_operational_signoff import (
    SIGNOFF_BLOCKED,
    SIGNOFF_READY,
    SIGNOFF_READY_WITH_WARNINGS,
    SIGNOFF_REQUIRES_RECOVERY,
    _PRODUCTION_ROOT_TOUCHED_SENTINEL,
    default_signoff_store_dir,
    load_operational_signoff_record,
)
from hermes_constants import get_hermes_home

_VALIDATION_STORE_DIR = "production-live-rollback-validation"
_VALIDATION_STORE_VERSION = 1

ROLLBACK_READY = "ROLLBACK_READY"
ROLLBACK_READY_WITH_WARNINGS = "ROLLBACK_READY_WITH_WARNINGS"
ROLLBACK_NOT_READY = "ROLLBACK_NOT_READY"
ROLLBACK_REQUIRES_RECOVERY = "ROLLBACK_REQUIRES_RECOVERY"

WARN_MANUAL_ROLLBACK_ONLY = "manual_rollback_only"
WARN_REMOTE_TAG_NOT_VERIFIED = "remote_tag_not_verified"
WARN_LOCAL_OUTPUT_CLEANUP_REQUIRED = "local_output_cleanup_required"
WARN_EXTERNAL_PUBLISH_DISABLED = "external_publish_disabled"
WARN_PRODUCTION_ROOT_HARD_DENIED = "production_root_hard_denied"
WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED = "original_repository2_not_executed"
WARN_SECOND_SUPERVISED_RUN_REQUIRED = "second_supervised_run_required"
WARN_MIRROR_ONLY_VALIDATION = "mirror_only_validation"
WARN_RELEASE_TAG_NOT_PUSHED = "release_tag_not_pushed"

BLOCK_ACTIVATION_MISSING = "activation_missing"
BLOCK_RESERVATION_MISSING = "reservation_missing"
BLOCK_RUNTIME_AUDIT_MISSING = "runtime_audit_missing"
BLOCK_EVIDENCE_MISSING = "evidence_missing"
BLOCK_DISPATCH_AUDIT_MISSING = "dispatch_audit_missing"
BLOCK_CONSUME_TRANSACTION_MISSING = "consume_transaction_missing"
BLOCK_E2E_FINALIZATION_MISSING = "e2e_finalization_missing"
BLOCK_SIGNOFF_MISSING = "signoff_missing"
BLOCK_CORRELATION_MISMATCH = "correlation_mismatch"
BLOCK_TESTED_COMMIT_MISSING = "tested_commit_missing"
BLOCK_TESTED_COMMIT_MISMATCH = "tested_commit_mismatch"
BLOCK_RELEASE_TAG_MISSING = "release_tag_missing"
BLOCK_RELEASE_TAG_MISMATCH = "release_tag_mismatch"
BLOCK_ROLLBACK_COMMIT_MISSING = "rollback_commit_missing"
BLOCK_ROLLBACK_COMMIT_INVALID = "rollback_commit_invalid"
BLOCK_ROLLBACK_COMMIT_EQUALS_TESTED_COMMIT = "rollback_commit_equals_tested_commit"
BLOCK_SOURCE_TREE_MUTATED = "source_tree_mutated"
BLOCK_UNEXPECTED_ARTIFACTS = "unexpected_artifacts"
BLOCK_PRODUCTION_ROOT_TOUCHED = "production_root_touched"
BLOCK_EXTERNAL_PUBLISH_ATTEMPTED = "external_publish_attempted"
BLOCK_CONSUME_NOT_COMMITTED = "consume_not_committed"
BLOCK_RECOVERY_REQUIRED = "recovery_required"
BLOCK_REPAIR_LOCK_HELD = "repair_lock_held"
BLOCK_ACTIVATION_NOT_REVOKED = "activation_not_revoked"
BLOCK_ARTIFACT_CORRUPTED = "artifact_corrupted"

ACTION_ROLLBACK_VALIDATION_COMPLETE = "rollback_validation_complete"
ACTION_REVIEW_ROLLBACK_WARNINGS = "review_rollback_warnings"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_RESOLVE_ARTIFACT_CORRELATION = "resolve_artifact_correlation"
ACTION_RESTORE_TESTED_COMMIT_INTEGRITY = "restore_tested_commit_integrity"
ACTION_RESTORE_RELEASE_TAG_INTEGRITY = "restore_release_tag_integrity"
ACTION_DEFINE_VALID_ROLLBACK_COMMIT = "define_valid_rollback_commit"
ACTION_INSPECT_SOURCE_MUTATION = "inspect_source_mutation"
ACTION_INSPECT_UNEXPECTED_ARTIFACTS = "inspect_unexpected_artifacts"
ACTION_REVOKE_ACTIVATION_MANUALLY = "revoke_activation_manually"
ACTION_CREATE_NEW_ACTIVATION_PROPOSAL = "create_new_activation_proposal"
ACTION_PREPARE_PHASE_14J_PRODUCTION_SIGNOFF = "prepare_phase_14j_production_signoff"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"

_MIRROR_ALLOWED_TOP_LEVEL = frozenset(
    {
        "pipeline.js",
        "package.json",
        "publishers",
        "prompts",
        "config",
        "outputs",
        "reports",
        "node_modules",
        ".hermes-mirror-stamp",
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
        "rollback_commit",
        "attestation_hash",
    }
)


class ProductionLiveRollbackValidationError(ValueError):
    """Raised when rollback validation cannot proceed safely."""


@dataclass(frozen=True)
class ProductionLiveRollbackValidationSummary:
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    validation_status: str
    chain_complete: bool
    activation_valid: bool
    reservation_valid: bool
    runtime_valid: bool
    evidence_valid: bool
    dispatch_audit_valid: bool
    consume_valid: bool
    signoff_valid: bool
    tested_commit_present: bool
    tested_commit_matches: bool
    release_tag_present: bool
    release_tag_matches_tested_commit: bool
    rollback_commit_present: bool
    rollback_commit_valid: bool
    rollback_commit_distinct: bool
    rollback_path_available: bool
    production_root_untouched: bool
    isolated_mirror_only: bool
    source_tree_unchanged: bool
    output_artifacts_identifiable: bool
    external_publish_attempted: bool
    recovery_required: bool
    repair_lock_held: bool
    rollback_ready: bool
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str
    output_artifact_count: int = 0
    report_artifact_count: int = 0
    unexpected_artifact_count: int = 0
    cleanup_required: bool = False
    consume_state: str = ""
    activation_state: str = ""
    tested_commit_sha_short: str = ""
    release_tag: str = ""
    already_validated: bool = False
    production_execution_allowed: bool = False
    original_repository2_execution_attempted: bool = False


@dataclass(frozen=True)
class ProductionLiveRollbackValidationRecord:
    validation_id: str
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    validation_status: str
    rollback_ready: bool
    blocking_item_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    tested_commit_sha: str
    release_tag: str
    rollback_commit_present: bool
    source_tree_unchanged: bool
    production_root_untouched: bool
    cleanup_required: bool
    validated_at: str
    production_execution_allowed: bool = False
    original_repository2_execution_attempted: bool = False


@dataclass(frozen=True)
class ProductionLiveRollbackDashboardDigest:
    rollback_validation_status: str
    rollback_ready: bool
    rollback_cleanup_required: bool
    rollback_recommended_action: str


def default_rollback_validation_store_dir() -> Path:
    return get_hermes_home() / "coo" / _VALIDATION_STORE_DIR


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


def _validation_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionLiveRollbackValidationError("activation_request_id is required")
    base = (store_dir or default_rollback_validation_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionLiveRollbackValidationError(
            "Rollback validation store must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_rollback_validation_store_available(*, store_dir: Path | None = None) -> bool:
    try:
        base = (store_dir or default_rollback_validation_store_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _resolve_git_dirs(repo_root: Path) -> tuple[Path, Path]:
    root = repo_root.resolve()
    git_dir = root / ".git"
    if git_dir.is_file():
        for line in git_dir.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "gitdir" and value.strip():
                git_dir = (root / value.strip()).resolve()
                break
    common_dir = git_dir
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
        if rel:
            common_dir = (git_dir / rel).resolve()
    return git_dir, common_dir


def _read_packed_ref(common_dir: Path, ref: str) -> str | None:
    packed = common_dir / "packed-refs"
    if not packed.is_file():
        return None
    try:
        text = packed.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == ref:
            return parts[0].strip().lower()
    return None


def resolve_git_ref_commit(ref: str, *, repo_root: Path) -> str | None:
    """Resolve a git ref to a commit SHA without subprocess."""
    normalized_ref = (ref or "").strip()
    if not normalized_ref:
        return None
    git_dir, common_dir = _resolve_git_dirs(repo_root)
    if not _SHA_RE.match(normalized_ref.lower()):
        for candidate in (git_dir, common_dir):
            ref_path = candidate / normalized_ref
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8", errors="replace").strip().lower()
        packed = _read_packed_ref(common_dir, normalized_ref)
        if packed:
            return packed
        return None
    return normalized_ref.lower()


def git_commit_object_exists(commit_sha: str, *, repo_root: Path) -> bool:
    """Minimum existence check for a commit object without subprocess."""
    normalized = (commit_sha or "").strip().lower()
    if not _SHA_RE.match(normalized):
        return False
    git_dir, common_dir = _resolve_git_dirs(repo_root)
    if len(normalized) == 40:
        loose = common_dir / "objects" / normalized[:2] / normalized[2:]
        if loose.is_file():
            return True
    for search_dir in (git_dir, common_dir):
        for sub in ("refs/heads", "refs/tags"):
            base = search_dir / sub
            if not base.is_dir():
                continue
            for ref_file in base.rglob("*"):
                if not ref_file.is_file():
                    continue
                try:
                    value = ref_file.read_text(encoding="utf-8", errors="replace").strip().lower()
                except OSError:
                    continue
                if value == normalized or value.startswith(normalized):
                    return True
    packed = common_dir / "packed-refs"
    if packed.is_file():
        try:
            text = packed.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for line in text.splitlines():
            if line.startswith("#") or line.startswith("^"):
                continue
            parts = line.split()
            if parts and parts[0].lower().startswith(normalized):
                return True
    return False


def _validate_commit_sha_format(value: str) -> bool:
    return bool(_SHA_RE.match((value or "").strip().lower()))


def _validate_release_tag_format(value: str) -> bool:
    return bool(_TAG_RE.match((value or "").strip()))


def _release_tag_commit(release_tag: str, *, repo_root: Path) -> str | None:
    tag = (release_tag or "").strip()
    if not tag:
        return None
    return resolve_git_ref_commit(f"refs/tags/{tag}", repo_root=repo_root)


def _commits_equal(left: str, right: str) -> bool:
    a = (left or "").strip().lower()
    b = (right or "").strip().lower()
    if not a or not b:
        return False
    if len(a) < 40 or len(b) < 40:
        return a.startswith(b) or b.startswith(a)
    return a == b


def _probe_production_root_touched(*, signoff_store_dir: Path | None = None) -> bool:
    base = (signoff_store_dir or default_signoff_store_dir()).resolve()
    return (base / _PRODUCTION_ROOT_TOUCHED_SENTINEL).is_file()


def _resolve_mirror_root(
    confirmation_id: str,
    *,
    confirmation_dir: Path | None = None,
) -> Path | None:
    if not confirmation_id:
        return None
    try:
        confirmation = read_confirmation(
            confirmation_id,
            confirmation_dir=confirmation_dir,
            reject_consumed=False,
        )
    except (ValueError, OSError):
        return None
    attested = (confirmation.attested_pipeline_root or "").strip()
    if not attested:
        return None
    try:
        return Path(attested).resolve()
    except OSError:
        return None


def _count_mirror_output_artifacts(mirror_root: Path | None) -> tuple[int, int, int, bool]:
    if mirror_root is None or not mirror_root.is_dir():
        return 0, 0, 0, False

    output_count = 0
    report_count = 0
    unexpected_count = 0

    for child in mirror_root.iterdir():
        name = child.name
        if name in _MIRROR_ALLOWED_TOP_LEVEL:
            if name == "outputs" and child.is_dir():
                output_count = sum(1 for p in child.rglob("*") if p.is_file())
            elif name == "reports" and child.is_dir():
                report_count = sum(1 for p in child.rglob("*") if p.is_file())
            continue
        if child.is_file():
            unexpected_count += 1
        elif child.is_dir():
            unexpected_count += sum(1 for p in child.rglob("*") if p.is_file())

    cleanup_required = output_count > 0 or report_count > 0 or unexpected_count > 0
    return output_count, report_count, unexpected_count, cleanup_required


def _runtime_completion_state(
    activation_request_id: str,
    execution_attempt_id: str,
    *,
    runtime_history_dir: Path | None = None,
) -> tuple[bool, bool, bool, bool]:
    records = load_runtime_records(
        activation_request_id,
        history_dir=runtime_history_dir,
    )
    matches = [
        record
        for record in records
        if record.event_type == _EVENT_RUNTIME_COMPLETED
        and record.execution_attempt_id == execution_attempt_id
    ]
    if len(matches) > 1:
        raise ProductionLiveRollbackValidationError("runtime audit corrupted")
    if not matches:
        return False, False, False, False
    record = matches[0]
    source_unchanged = (
        record.exit_code == 0
        and not record.timed_out
        and not record.publish_attempted
    )
    return (
        True,
        source_unchanged,
        record.publish_attempted,
        record.isolated_mirror_runtime_invoked,
    )


def _validate_full_correlation_chain(
    *,
    request: ActivationRequest,
    reservation: ProductionActivationExecutionReservation,
    dispatch_run_id: str,
    evidence_present: bool,
    evidence_gate_id: str,
    evidence_dry_run_id: str,
    audit_gate_id: str,
    audit_dry_run_id: str,
    signoff_gate_match: bool,
) -> bool:
    if reservation.activation_request_id != request.activation_request_id:
        return False
    if not reservation.execution_attempt_id:
        return False
    if derive_live_pilot_dispatch_run_id(reservation.execution_attempt_id) != dispatch_run_id:
        return False
    if reservation.tested_commit_sha and request.tested_commit_sha:
        if not _commits_equal(reservation.tested_commit_sha, request.tested_commit_sha):
            return False
    if reservation.release_tag and request.release_tag:
        if reservation.release_tag != request.release_tag:
            return False
    if evidence_present:
        if evidence_gate_id != reservation.execution_gate_event_id:
            return False
        if evidence_dry_run_id != reservation.dry_run_event_id:
            return False
        if audit_gate_id != reservation.execution_gate_event_id:
            return False
        if audit_dry_run_id != reservation.dry_run_event_id:
            return False
    if not signoff_gate_match and evidence_present:
        return False
    return True


def load_rollback_validation_record(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> ProductionLiveRollbackValidationRecord | None:
    path = _validation_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionLiveRollbackValidationError(
            "rollback validation artifact corrupted"
        ) from exc
    validation = payload.get("validation")
    if not isinstance(validation, dict):
        raise ProductionLiveRollbackValidationError(
            "rollback validation artifact corrupted"
        )
    return ProductionLiveRollbackValidationRecord(
        validation_id=str(validation.get("validation_id", "")),
        activation_request_id=str(validation.get("activation_request_id", "")),
        reservation_id=str(validation.get("reservation_id", "")),
        execution_attempt_id=str(validation.get("execution_attempt_id", "")),
        dispatch_run_id=str(validation.get("dispatch_run_id", "")),
        validation_status=str(validation.get("validation_status", "")),
        rollback_ready=bool(validation.get("rollback_ready", False)),
        blocking_item_codes=tuple(validation.get("blocking_item_codes", ())),
        warning_codes=tuple(validation.get("warning_codes", ())),
        tested_commit_sha=str(validation.get("tested_commit_sha", "")),
        release_tag=str(validation.get("release_tag", "")),
        rollback_commit_present=bool(validation.get("rollback_commit_present", False)),
        source_tree_unchanged=bool(validation.get("source_tree_unchanged", False)),
        production_root_untouched=bool(validation.get("production_root_untouched", False)),
        cleanup_required=bool(validation.get("cleanup_required", False)),
        validated_at=str(validation.get("validated_at", "")),
        production_execution_allowed=False,
        original_repository2_execution_attempted=False,
    )


def _write_validation_record(
    record: ProductionLiveRollbackValidationRecord,
    *,
    store_dir: Path | None = None,
) -> None:
    path = _validation_path(record.activation_request_id, store_dir=store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _VALIDATION_STORE_VERSION,
        "validation": {
            "validation_id": record.validation_id,
            "activation_request_id": record.activation_request_id,
            "reservation_id": record.reservation_id,
            "execution_attempt_id": record.execution_attempt_id,
            "dispatch_run_id": record.dispatch_run_id,
            "validation_status": record.validation_status,
            "rollback_ready": record.rollback_ready,
            "blocking_item_codes": list(record.blocking_item_codes),
            "warning_codes": list(record.warning_codes),
            "tested_commit_sha": _short_sha(record.tested_commit_sha),
            "release_tag": record.release_tag,
            "rollback_commit_present": record.rollback_commit_present,
            "source_tree_unchanged": record.source_tree_unchanged,
            "production_root_untouched": record.production_root_untouched,
            "cleanup_required": record.cleanup_required,
            "validated_at": record.validated_at,
            "production_execution_allowed": False,
            "original_repository2_execution_attempted": False,
        },
    }
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise ProductionLiveRollbackValidationError(
            "rollback validation report write failed"
        ) from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _recommended_action(status: str, blocking: tuple[str, ...]) -> str:
    if status in {ROLLBACK_READY, ROLLBACK_READY_WITH_WARNINGS}:
        if status == ROLLBACK_READY_WITH_WARNINGS:
            return ACTION_REVIEW_ROLLBACK_WARNINGS
        return ACTION_PREPARE_PHASE_14J_PRODUCTION_SIGNOFF
    if status == ROLLBACK_REQUIRES_RECOVERY:
        return ACTION_RUN_CONSUME_RECOVERY
    if BLOCK_CORRELATION_MISMATCH in blocking:
        return ACTION_RESOLVE_ARTIFACT_CORRELATION
    if BLOCK_TESTED_COMMIT_MISSING in blocking or BLOCK_TESTED_COMMIT_MISMATCH in blocking:
        return ACTION_RESTORE_TESTED_COMMIT_INTEGRITY
    if BLOCK_RELEASE_TAG_MISSING in blocking or BLOCK_RELEASE_TAG_MISMATCH in blocking:
        return ACTION_RESTORE_RELEASE_TAG_INTEGRITY
    if (
        BLOCK_ROLLBACK_COMMIT_MISSING in blocking
        or BLOCK_ROLLBACK_COMMIT_INVALID in blocking
        or BLOCK_ROLLBACK_COMMIT_EQUALS_TESTED_COMMIT in blocking
    ):
        return ACTION_DEFINE_VALID_ROLLBACK_COMMIT
    if BLOCK_SOURCE_TREE_MUTATED in blocking:
        return ACTION_INSPECT_SOURCE_MUTATION
    if BLOCK_UNEXPECTED_ARTIFACTS in blocking:
        return ACTION_INSPECT_UNEXPECTED_ARTIFACTS
    if BLOCK_ACTIVATION_NOT_REVOKED in blocking:
        return ACTION_REVOKE_ACTIVATION_MANUALLY
    if BLOCK_ARTIFACT_CORRUPTED in blocking:
        return ACTION_RESOLVE_ARTIFACT_CORRELATION
    return ACTION_MAINTAIN_PRODUCTION_BLOCK


def evaluate_production_live_rollback_validation(
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
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> ProductionLiveRollbackValidationSummary:
    """Read-only rollback readiness assessment for one live pilot activation."""
    blocking: list[str] = []
    warnings: list[str] = []

    request: ActivationRequest | None = None
    try:
        request = load_activation_request(activation_request_id, store_dir=store_dir)
        activation_valid = True
    except (ProductionActivationStoreError, ProductionActivationStateError):
        blocking.append(BLOCK_ACTIVATION_MISSING)
        activation_valid = False

    reservation: ProductionActivationExecutionReservation | None
    try:
        reservation = load_execution_reservation(
            activation_request_id,
            store_dir=reservation_dir,
        )
    except ProductionActivationExecutionReservationError:
        blocking.append(BLOCK_ARTIFACT_CORRUPTED)
        reservation = None

    reservation_valid = False
    if reservation is None:
        blocking.append(BLOCK_RESERVATION_MISSING)
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
    else:
        reservation_valid = True

    dispatch_run_id = ""
    if reservation.execution_attempt_id:
        dispatch_run_id = derive_live_pilot_dispatch_run_id(
            reservation.execution_attempt_id
        )

    existing_report = load_rollback_validation_record(
        activation_request_id,
        store_dir=validation_store_dir,
    )
    already_validated = False
    if existing_report is not None:
        if (
            existing_report.reservation_id == reservation.reservation_id
            and existing_report.execution_attempt_id == reservation.execution_attempt_id
            and existing_report.dispatch_run_id == dispatch_run_id
        ):
            already_validated = True
        else:
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)

    runtime_valid = False
    source_tree_unchanged = False
    publish_attempted = False
    isolated_mirror_only = False
    if reservation.execution_attempt_id:
        try:
            (
                runtime_completed,
                source_tree_unchanged,
                publish_attempted,
                isolated_mirror_only,
            ) = _runtime_completion_state(
                activation_request_id,
                reservation.execution_attempt_id,
                runtime_history_dir=runtime_history_dir,
            )
            runtime_valid = runtime_completed
            if not runtime_completed:
                blocking.append(BLOCK_RUNTIME_AUDIT_MISSING)
            if publish_attempted:
                blocking.append(BLOCK_EXTERNAL_PUBLISH_ATTEMPTED)
            if runtime_completed and not source_tree_unchanged:
                blocking.append(BLOCK_SOURCE_TREE_MUTATED)
        except ProductionLiveRollbackValidationError:
            blocking.append(BLOCK_ARTIFACT_CORRUPTED)
    else:
        blocking.append(BLOCK_RUNTIME_AUDIT_MISSING)

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

    evidence_valid = evidence is not None
    audit_valid = audit is not None
    if not evidence_valid:
        blocking.append(BLOCK_EVIDENCE_MISSING)
    if not audit_valid:
        blocking.append(BLOCK_DISPATCH_AUDIT_MISSING)

    correlation_valid = False
    if evidence is not None and audit is not None and reservation_valid:
        correlation_valid = correlate_live_pilot_evidence_and_audit(
            evidence,
            audit,
            reservation=reservation,
        )
        if not correlation_valid:
            blocking.append(BLOCK_CORRELATION_MISMATCH)

    finalization = load_e2e_finalization_state(
        activation_request_id,
        history_dir=e2e_history_dir,
    )
    e2e_valid = finalization.e2e_finalized
    if not e2e_valid:
        blocking.append(BLOCK_E2E_FINALIZATION_MISSING)
    elif finalization.execution_attempt_id and reservation.execution_attempt_id:
        if finalization.execution_attempt_id != reservation.execution_attempt_id:
            blocking.append(BLOCK_CORRELATION_MISMATCH)
    elif finalization.dispatch_run_id and dispatch_run_id:
        if finalization.dispatch_run_id != dispatch_run_id:
            blocking.append(BLOCK_CORRELATION_MISMATCH)

    signoff = load_operational_signoff_record(
        activation_request_id,
        store_dir=signoff_store_dir,
    )
    signoff_valid = False
    if signoff is None:
        blocking.append(BLOCK_SIGNOFF_MISSING)
    elif (
        signoff.reservation_id != reservation.reservation_id
        or signoff.execution_attempt_id != reservation.execution_attempt_id
    ):
        blocking.append(BLOCK_CORRELATION_MISMATCH)
    elif signoff.signoff_status == SIGNOFF_BLOCKED:
        blocking.append(BLOCK_SIGNOFF_MISSING)
    else:
        signoff_valid = signoff.signoff_status in {
            SIGNOFF_READY,
            SIGNOFF_READY_WITH_WARNINGS,
            SIGNOFF_REQUIRES_RECOVERY,
        }

    consume_status = assess_consume_status(
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
    )
    transaction = read_consume_transaction(
        reservation.ticket_id,
        reservation.confirmation_id,
        transaction_dir=transaction_dir,
    )
    consume_valid = consume_status.consume_state == CONSUME_STATE_COMMITTED
    if (
        e2e_valid
        and consume_valid
        and transaction is None
        and reservation.ticket_id
        and reservation.confirmation_id
    ):
        blocking.append(BLOCK_CONSUME_TRANSACTION_MISSING)
    if not consume_valid:
        blocking.append(BLOCK_CONSUME_NOT_COMMITTED)

    recovery_required = (
        consume_status.recovery_required
        or (
            _probe_recovery_required(request)
            if request is not None
            else False
        )
        or consume_status.consume_state
        in {
            CONSUME_STATE_PARTIAL,
            CONSUME_STATE_PREPARED,
            CONSUME_STATE_RECOVERY_REQUIRED,
        }
    )
    repair_lock_held = (
        _probe_repair_lock_held(request) if request is not None else False
    )
    if recovery_required:
        blocking.append(BLOCK_RECOVERY_REQUIRED)
    if repair_lock_held:
        blocking.append(BLOCK_REPAIR_LOCK_HELD)

    activation_state = request.state if request is not None else ""
    activation_revoked = activation_state == ACTIVATION_STATE_REVOKED
    activation_suspended = activation_state == ACTIVATION_STATE_SUSPENDED
    if consume_valid and not activation_revoked:
        blocking.append(BLOCK_ACTIVATION_NOT_REVOKED)

    production_root_touched = _probe_production_root_touched(
        signoff_store_dir=signoff_store_dir,
    )
    production_root_untouched = not production_root_touched
    if production_root_touched or reservation.repository2_execution_attempted:
        blocking.append(BLOCK_PRODUCTION_ROOT_TOUCHED)

    tested_commit_present = bool(request and request.tested_commit_sha)
    release_tag_present = bool(request and request.release_tag)
    rollback_commit_present = bool(request and request.rollback_commit)
    tested_commit_matches = False
    release_tag_matches = False
    rollback_commit_valid = False
    rollback_commit_distinct = False

    root = (repo_root or Path.cwd()).resolve()
    if request is not None:
        if not tested_commit_present:
            blocking.append(BLOCK_TESTED_COMMIT_MISSING)
        if not release_tag_present:
            blocking.append(BLOCK_RELEASE_TAG_MISSING)
        if not rollback_commit_present:
            blocking.append(BLOCK_ROLLBACK_COMMIT_MISSING)
        elif not _validate_commit_sha_format(request.rollback_commit):
            blocking.append(BLOCK_ROLLBACK_COMMIT_INVALID)
        else:
            rollback_commit_valid = git_commit_object_exists(
                request.rollback_commit,
                repo_root=root,
            )
            if not rollback_commit_valid:
                blocking.append(BLOCK_ROLLBACK_COMMIT_INVALID)
            if _commits_equal(request.rollback_commit, request.tested_commit_sha):
                blocking.append(BLOCK_ROLLBACK_COMMIT_EQUALS_TESTED_COMMIT)
            else:
                rollback_commit_distinct = True

        if tested_commit_present:
            try:
                head = resolve_git_head_commit(repo_root=root)
                tested_commit_matches = _commits_equal(
                    request.tested_commit_sha,
                    head,
                )
                if signoff is not None and signoff.tested_commit_sha:
                    tested_commit_matches = tested_commit_matches and _commits_equal(
                        request.tested_commit_sha,
                        signoff.tested_commit_sha,
                    )
            except Exception:
                tested_commit_matches = False
            if not tested_commit_matches:
                blocking.append(BLOCK_TESTED_COMMIT_MISMATCH)

        if release_tag_present:
            tag_commit = _release_tag_commit(request.release_tag, repo_root=root)
            release_tag_matches = tag_commit is not None and _commits_equal(
                tag_commit,
                request.tested_commit_sha,
            )
            if not release_tag_matches:
                blocking.append(BLOCK_RELEASE_TAG_MISMATCH)

    signoff_gate_match = True
    if signoff is not None and request is not None:
        if signoff.release_tag and signoff.release_tag != request.release_tag:
            signoff_gate_match = False
        if signoff.tested_commit_sha and not _commits_equal(
            signoff.tested_commit_sha,
            request.tested_commit_sha,
        ):
            signoff_gate_match = False

    chain_complete = all(
        (
            activation_valid,
            reservation_valid,
            runtime_valid,
            evidence_valid,
            audit_valid,
            consume_valid,
            signoff_valid,
            e2e_valid,
            correlation_valid,
        )
    )
    if request is not None and reservation_valid:
        extended_correlation = _validate_full_correlation_chain(
            request=request,
            reservation=reservation,
            dispatch_run_id=dispatch_run_id,
            evidence_present=evidence_valid,
            evidence_gate_id=evidence.execution_gate_event_id if evidence else "",
            evidence_dry_run_id=evidence.dry_run_event_id if evidence else "",
            audit_gate_id=audit.gate_event_id if audit else "",
            audit_dry_run_id=audit.dry_run_event_id if audit else "",
            signoff_gate_match=signoff_gate_match,
        )
        if not extended_correlation:
            blocking.append(BLOCK_CORRELATION_MISMATCH)
            chain_complete = False

    mirror_root = _resolve_mirror_root(
        reservation.confirmation_id,
        confirmation_dir=confirmation_dir,
    )
    output_count, report_count, unexpected_count, cleanup_required = (
        _count_mirror_output_artifacts(mirror_root)
    )
    if unexpected_count > 0:
        blocking.append(BLOCK_UNEXPECTED_ARTIFACTS)
    output_identifiable = mirror_root is not None

    enablement = load_dispatch_gateway_enablement(merged_config=merged_config or {})
    warnings.append(WARN_MANUAL_ROLLBACK_ONLY)
    warnings.append(WARN_REMOTE_TAG_NOT_VERIFIED)
    if cleanup_required:
        warnings.append(WARN_LOCAL_OUTPUT_CLEANUP_REQUIRED)
    warnings.append(WARN_EXTERNAL_PUBLISH_DISABLED)
    warnings.append(WARN_PRODUCTION_ROOT_HARD_DENIED)
    warnings.append(WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED)
    warnings.append(WARN_MIRROR_ONLY_VALIDATION)
    if request is not None and request.release_tag:
        warnings.append(WARN_RELEASE_TAG_NOT_PUSHED)

    rollback_path_available = (
        rollback_commit_present
        and rollback_commit_valid
        and rollback_commit_distinct
        and (activation_revoked or activation_suspended)
        and production_root_untouched
        and source_tree_unchanged
        and not publish_attempted
        and enablement.production_root_hard_deny
    )

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
        validation_status = ROLLBACK_NOT_READY
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
        or (activation_suspended and not activation_revoked)
    ):
        validation_status = ROLLBACK_REQUIRES_RECOVERY
    elif unique_warnings:
        validation_status = ROLLBACK_READY_WITH_WARNINGS
    else:
        validation_status = ROLLBACK_READY

    rollback_ready = validation_status in {
        ROLLBACK_READY,
        ROLLBACK_READY_WITH_WARNINGS,
    }

    if validation_status in {ROLLBACK_READY, ROLLBACK_READY_WITH_WARNINGS}:
        if already_validated:
            pass
        else:
            recommended = _recommended_action(validation_status, unique_blocking)
            if validation_status == ROLLBACK_READY_WITH_WARNINGS:
                recommended = ACTION_REVIEW_ROLLBACK_WARNINGS
    else:
        recommended = _recommended_action(validation_status, unique_blocking)

    if already_validated and validation_status in {
        ROLLBACK_READY,
        ROLLBACK_READY_WITH_WARNINGS,
    }:
        recommended = ACTION_ROLLBACK_VALIDATION_COMPLETE

    return ProductionLiveRollbackValidationSummary(
        activation_request_id=activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        validation_status=validation_status,
        chain_complete=chain_complete and not unique_blocking,
        activation_valid=activation_valid,
        reservation_valid=reservation_valid,
        runtime_valid=runtime_valid,
        evidence_valid=evidence_valid,
        dispatch_audit_valid=audit_valid,
        consume_valid=consume_valid,
        signoff_valid=signoff_valid,
        tested_commit_present=tested_commit_present,
        tested_commit_matches=tested_commit_matches,
        release_tag_present=release_tag_present,
        release_tag_matches_tested_commit=release_tag_matches,
        rollback_commit_present=rollback_commit_present,
        rollback_commit_valid=rollback_commit_valid,
        rollback_commit_distinct=rollback_commit_distinct,
        rollback_path_available=rollback_path_available,
        production_root_untouched=production_root_untouched,
        isolated_mirror_only=isolated_mirror_only,
        source_tree_unchanged=source_tree_unchanged,
        output_artifacts_identifiable=output_identifiable,
        external_publish_attempted=publish_attempted,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        rollback_ready=rollback_ready,
        blocking_items=unique_blocking,
        warning_items=unique_warnings,
        recommended_action=recommended,
        output_artifact_count=output_count,
        report_artifact_count=report_count,
        unexpected_artifact_count=unexpected_count,
        cleanup_required=cleanup_required,
        consume_state=consume_status.consume_state,
        activation_state=activation_state,
        tested_commit_sha_short=_short_sha(request.tested_commit_sha) if request else "",
        release_tag=request.release_tag if request else "",
        already_validated=already_validated,
        production_execution_allowed=False,
        original_repository2_execution_attempted=False,
    )


def record_production_live_rollback_validation(
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
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionLiveRollbackValidationSummary:
    """Append rollback validation report when assessment allows it."""
    if not probe_rollback_validation_store_available(store_dir=validation_store_dir):
        raise ProductionLiveRollbackValidationError(
            "rollback validation report write failed"
        )

    summary = evaluate_production_live_rollback_validation(
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

    if summary.already_validated:
        return summary

    if BLOCK_ARTIFACT_CORRUPTED in summary.blocking_items:
        raise ProductionLiveRollbackValidationError("artifact_corrupted")

    request = load_activation_request(activation_request_id, store_dir=store_dir)
    record = ProductionLiveRollbackValidationRecord(
        validation_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        reservation_id=summary.reservation_id,
        execution_attempt_id=summary.execution_attempt_id,
        dispatch_run_id=summary.dispatch_run_id,
        validation_status=summary.validation_status,
        rollback_ready=summary.rollback_ready,
        blocking_item_codes=summary.blocking_items,
        warning_codes=summary.warning_items,
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        rollback_commit_present=summary.rollback_commit_present,
        source_tree_unchanged=summary.source_tree_unchanged,
        production_root_untouched=summary.production_root_untouched,
        cleanup_required=summary.cleanup_required,
        validated_at=_utc_now_iso(now),
        production_execution_allowed=False,
        original_repository2_execution_attempted=False,
    )
    _write_validation_record(record, store_dir=validation_store_dir)
    return evaluate_production_live_rollback_validation(
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


def resolve_latest_rollback_dashboard_digest(
    *,
    e2e_history_dir: Path | None = None,
    validation_store_dir: Path | None = None,
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
    repo_root: Path | None = None,
) -> ProductionLiveRollbackDashboardDigest:
    """Read-only digest of the newest rollback validation for operator dashboard."""
    base = (e2e_history_dir or default_e2e_history_dir()).resolve()
    if not base.is_dir():
        return ProductionLiveRollbackDashboardDigest(
            rollback_validation_status="not_configured",
            rollback_ready=False,
            rollback_cleanup_required=False,
            rollback_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
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
        except ProductionActivationExecutionReservationError:
            continue
        if reservation is None:
            continue
        try:
            summary = evaluate_production_live_rollback_validation(
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
                repo_root=repo_root,
                merged_config=merged_config,
            )
        except ProductionLiveRollbackValidationError:
            continue
        existing = load_rollback_validation_record(
            activation_id,
            store_dir=validation_store_dir,
        )
        status = (
            existing.validation_status
            if existing is not None
            else summary.validation_status
        )
        return ProductionLiveRollbackDashboardDigest(
            rollback_validation_status=status,
            rollback_ready=summary.rollback_ready,
            rollback_cleanup_required=summary.cleanup_required,
            rollback_recommended_action=summary.recommended_action,
        )

    return ProductionLiveRollbackDashboardDigest(
        rollback_validation_status="not_configured",
        rollback_ready=False,
        rollback_cleanup_required=False,
        rollback_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
    )


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "production_execution_allowed: false",
        "original_repository2_execution_attempted: false",
        "external_publish_attempted: false",
        "original_repository2_not_executed",
        "rollback_commit_present:",
        "rollback_commit_valid:",
        "rollback_commit_distinct:",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionLiveRollbackValidationError(
                f"Unsafe rollback validation output field: {token!r}"
            )


def format_production_live_rollback_check(summary: ProductionLiveRollbackValidationSummary) -> str:
    """Format read-only rollback-check output."""
    lines = [
        "Production Live Rollback Validation",
        "",
        f"activation_request_id: {summary.activation_request_id}",
        f"reservation_id: {summary.reservation_id or '(none)'}",
        f"execution_attempt_id: {summary.execution_attempt_id or '(none)'}",
        f"dispatch_run_id: {summary.dispatch_run_id or '(none)'}",
        f"validation_status: {summary.validation_status}",
        f"chain_complete: {str(summary.chain_complete).lower()}",
        f"rollback_ready: {str(summary.rollback_ready).lower()}",
        f"activation_valid: {str(summary.activation_valid).lower()}",
        f"reservation_valid: {str(summary.reservation_valid).lower()}",
        f"runtime_valid: {str(summary.runtime_valid).lower()}",
        f"evidence_valid: {str(summary.evidence_valid).lower()}",
        f"dispatch_audit_valid: {str(summary.dispatch_audit_valid).lower()}",
        f"consume_valid: {str(summary.consume_valid).lower()}",
        f"signoff_valid: {str(summary.signoff_valid).lower()}",
        f"tested_commit_present: {str(summary.tested_commit_present).lower()}",
        f"tested_commit_matches: {str(summary.tested_commit_matches).lower()}",
        f"release_tag_present: {str(summary.release_tag_present).lower()}",
        "release_tag_matches_tested_commit: "
        f"{str(summary.release_tag_matches_tested_commit).lower()}",
        f"rollback_commit_present: {str(summary.rollback_commit_present).lower()}",
        f"rollback_commit_valid: {str(summary.rollback_commit_valid).lower()}",
        f"rollback_commit_distinct: {str(summary.rollback_commit_distinct).lower()}",
        f"rollback_path_available: {str(summary.rollback_path_available).lower()}",
        f"production_root_untouched: {str(summary.production_root_untouched).lower()}",
        f"isolated_mirror_only: {str(summary.isolated_mirror_only).lower()}",
        f"source_tree_unchanged: {str(summary.source_tree_unchanged).lower()}",
        "output_artifacts_identifiable: "
        f"{str(summary.output_artifacts_identifiable).lower()}",
        f"external_publish_attempted: {str(summary.external_publish_attempted).lower()}",
        f"recovery_required: {str(summary.recovery_required).lower()}",
        f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
        f"consume_state: {summary.consume_state or '(none)'}",
        f"activation_state: {summary.activation_state or '(none)'}",
        f"output_artifact_count: {summary.output_artifact_count}",
        f"report_artifact_count: {summary.report_artifact_count}",
        f"unexpected_artifact_count: {summary.unexpected_artifact_count}",
        f"cleanup_required: {str(summary.cleanup_required).lower()}",
        f"tested_commit_sha: {summary.tested_commit_sha_short or '(none)'}",
        f"release_tag: {summary.release_tag or '(none)'}",
        f"blocking_items_count: {len(summary.blocking_items)}",
        f"warning_items_count: {len(summary.warning_items)}",
        f"blocking_items: {', '.join(summary.blocking_items) if summary.blocking_items else '(none)'}",
        f"warning_items: {', '.join(summary.warning_items) if summary.warning_items else '(none)'}",
        f"recommended_action: {summary.recommended_action}",
        f"already_validated: {str(summary.already_validated).lower()}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "original_repository2_execution_attempted: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_live_rollback_plan(summary: ProductionLiveRollbackValidationSummary) -> str:
    """Format read-only rollback plan summary without executable commands."""
    base = format_production_live_rollback_check(summary)
    plan_lines = [
        "",
        "[Rollback Plan]",
        "manual_rollback_only: true",
        "consume_unconsume_forbidden: true",
        "new_activation_required_after_rollback: true",
        f"rollback_path_available: {str(summary.rollback_path_available).lower()}",
    ]
    output = base + "\n" + "\n".join(plan_lines)
    _assert_safe_output(output)
    return output


def run_activation_rollback_check(
    *,
    activation_request_id: str,
    reservation_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
    write_report: bool = True,
) -> tuple[str, int]:
    if write_report:
        summary = record_production_live_rollback_validation(
            activation_request_id=activation_request_id,
            reservation_id=reservation_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    else:
        summary = evaluate_production_live_rollback_validation(
            activation_request_id=activation_request_id,
            reservation_id=reservation_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    exit_code = 0 if summary.rollback_ready else 1
    return format_production_live_rollback_check(summary), exit_code


def run_activation_rollback_plan(
    *,
    activation_request_id: str,
    reservation_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    summary = evaluate_production_live_rollback_validation(
        activation_request_id=activation_request_id,
        reservation_id=reservation_id,
        merged_config=merged_config,
        repo_root=repo_root,
    )
    exit_code = 0 if summary.rollback_path_available else 1
    return format_production_live_rollback_plan(summary), exit_code
