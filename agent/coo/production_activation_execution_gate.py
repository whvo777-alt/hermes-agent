"""Production activation execution gate — Phase 14H-2.

Read-only pre-execution gate for active activations without dispatch execution,
subprocess, or Repository2 access.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_executor_config import load_dispatch_executor_policy
from agent.coo.dispatch_pipeline_root_trust import (
    assert_pipeline_root_allowed,
    assert_pipeline_root_matches_attestation,
    resolve_pipeline_root,
)
from agent.coo.dispatch_runner_binding_state import (
    DispatchRunnerBindingStateError,
    RUNNER_BINDING_STATE_BOUND,
    load_dispatch_runner_binding_state,
)
from agent.coo.dispatch_runner_provider import (
    RUNNER_PROVIDER_MODE_BOUNDED,
    assess_dispatch_runner_provider,
)
from agent.coo.production_activation_active_gate import (
    _approval_quorum_valid,
    _attestation_valid,
    _executor_valid,
    _head_sha_matches,
    _probe_cutover_ready,
    _probe_recovery_required,
    _probe_regression_clear,
    _probe_repair_lock_held,
    _probe_signoff_ready,
    _release_tag_valid,
    _rollback_present,
)
from agent.coo.production_activation_dry_run import (
    ProductionActivationDryRunError,
    _probe_publish_intent,
    _snapshot_has_publish_intent,
    compute_dry_run_key,
    default_dry_run_history_dir,
    find_dry_run_record,
    probe_dry_run_audit_store_available,
)
from agent.coo.production_activation_kill_switch import (
    is_kill_switch_available,
    probe_audit_store_available,
)
from agent.coo.production_activation_state import (
    ACTIVATION_SCOPE_MAINTENANCE_WINDOW,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_SCOPE_TICKET_SCOPED,
    ACTIVATION_STATE_ACTIVE,
    ActivationRequest,
    ProductionActivationStateError,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    load_activation_request,
)
from agent.coo.production_executor_confirmation import read_confirmation
from hermes_constants import get_hermes_home

BLOCK_ACTIVATION_NOT_ACTIVE = "activation_not_active"
BLOCK_ACTIVE_EXPIRED = "active_expired"
BLOCK_ACTIVE_ARTIFACT_INVALID = "active_artifact_invalid"
BLOCK_DRY_RUN_MISSING = "dry_run_missing"
BLOCK_DRY_RUN_NOT_READY = "dry_run_not_ready"
BLOCK_DRY_RUN_STALE = "dry_run_stale"
BLOCK_DRY_RUN_CORRELATION_MISMATCH = "dry_run_correlation_mismatch"
BLOCK_TICKET_SCOPE_MISMATCH = "ticket_scope_mismatch"
BLOCK_BUNDLE_MISSING = "bundle_missing"
BLOCK_BUNDLE_CONSUMED = "bundle_consumed"
BLOCK_CONFIRMATION_MISSING = "confirmation_missing"
BLOCK_CONFIRMATION_CONSUMED = "confirmation_consumed"
BLOCK_CONFIRMATION_SCOPE_MISMATCH = "confirmation_scope_mismatch"
BLOCK_MIRROR_ROOT_NOT_TRUSTED = "mirror_root_not_trusted"
BLOCK_PRODUCTION_ROOT_DENIED = "production_root_denied"
BLOCK_PUBLISH_NOT_ALLOWED = "publish_not_allowed"
BLOCK_BINDING_NOT_BOUND = "binding_not_bound"
BLOCK_BOUNDED_RUNNER_CONTRACT_MISSING = "bounded_runner_contract_missing"
BLOCK_RECOVERY_REQUIRED = "recovery_required"
BLOCK_REPAIR_LOCK_HELD = "repair_lock_held"
BLOCK_REGRESSION_BLOCKED = "regression_blocked"
BLOCK_SIGNOFF_NOT_READY = "signoff_not_ready"
BLOCK_CUTOVER_NOT_READY = "cutover_not_ready"
BLOCK_KILL_SWITCH_UNAVAILABLE = "kill_switch_unavailable"
BLOCK_ROLLBACK_NOT_READY = "rollback_not_ready"
BLOCK_AUDIT_STORE_UNAVAILABLE = "audit_store_unavailable"

ACTION_EXECUTION_GATE_READY_WAIT_FOR_PHASE_14H_3 = (
    "execution_gate_ready_wait_for_phase_14h_3"
)
ACTION_REFRESH_PRODUCTION_DRY_RUN = "refresh_production_dry_run"
ACTION_RESOLVE_TICKET_SCOPE = "resolve_ticket_scope"
ACTION_RESOLVE_CONFIRMATION_SCOPE = "resolve_confirmation_scope"
ACTION_PREPARE_ISOLATED_PRODUCTION_MIRROR = "prepare_isolated_production_mirror"
ACTION_BIND_PRODUCTION_EXECUTOR = "bind_production_executor"
ACTION_RESOLVE_RECOVERY_ISSUE = "resolve_recovery_issue"
ACTION_RESOLVE_REGRESSION_FAILURE = "resolve_regression_failure"
ACTION_SUSPEND_ACTIVE_ACTIVATION = "suspend_active_activation"
ACTION_CREATE_NEW_ACTIVATION_PROPOSAL = "create_new_activation_proposal"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_ALREADY_EVALUATED = "already_evaluated"

_DRY_RUN_MAX_AGE_MINUTES = 30
_EXECUTION_GATE_STORE_DIR = "production-execution-gate"
_EXECUTION_GATE_STORE_VERSION = 1

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
        "dry_run_key",
        "confirm-production-activation",
        "confirm-repository2-execution",
    }
)


class ProductionActivationExecutionGateError(ValueError):
    """Raised when execution gate evaluation cannot complete safely."""


@dataclass(frozen=True)
class ProductionActivationExecutionGateAssessment:
    """Safe execution gate assessment for active activations."""

    activation_request_id: str
    activation_state: str
    execution_gate_ready: bool
    active_not_expired: bool
    active_actor_assigned: bool
    active_gate_verified: bool
    dry_run_verified: bool
    dry_run_fresh: bool
    ticket_scope_valid: bool
    confirmation_scope_valid: bool
    mirror_root_trusted: bool
    isolated_mirror_only: bool
    single_ticket_scope: bool
    draft_only: bool
    publish_allowed: bool = False
    executor_binding_ready: bool = False
    bounded_runner_contract_available: bool = False
    confirmation_unconsumed: bool = False
    bundle_unconsumed: bool = False
    recovery_clear: bool = False
    repair_lock_clear: bool = False
    regression_clear: bool = False
    signoff_ready: bool = False
    cutover_ready: bool = False
    kill_switch_available: bool = False
    audit_store_available: bool = False
    rollback_ready: bool = False
    production_execution_allowed: bool = False
    repository2_execution_attempted: bool = False
    execution_runtime_disabled: bool = True
    ticket_id: str = ""
    confirmation_id: str = ""
    blocking_reasons: tuple[str, ...] = ()
    recommended_action: str = ""
    already_evaluated: bool = False


@dataclass(frozen=True)
class ProductionActivationExecutionGateRecord:
    """Append-only execution gate audit record."""

    event_id: str
    activation_request_id: str
    ticket_id: str
    confirmation_id: str
    gate_key: str
    result: str
    blocking_reason_codes: tuple[str, ...]
    timestamp: str
    tested_commit_sha: str
    release_tag: str
    dry_run_event_id: str
    production_execution_allowed: bool = False
    repository2_execution_attempted: bool = False


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def default_execution_gate_history_dir() -> Path:
    return get_hermes_home() / "coo" / _EXECUTION_GATE_STORE_DIR


def _execution_gate_history_path(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionActivationExecutionGateError(
            "activation_request_id is required"
        )
    base = (history_dir or default_execution_gate_history_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationExecutionGateError(
            "Execution gate history directory must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def _gate_key(
    *,
    activation_request_id: str,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root_resolved: str,
) -> str:
    material = "|".join(
        (
            activation_request_id.strip(),
            ticket_id.strip(),
            confirmation_id.strip(),
            pipeline_root_resolved.strip(),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _record_from_dict(payload: Mapping[str, Any]) -> ProductionActivationExecutionGateRecord:
    codes = payload.get("blocking_reason_codes", [])
    if not isinstance(codes, list):
        raise ProductionActivationExecutionGateError(
            "Execution gate record blocking_reason_codes must be a list."
        )
    return ProductionActivationExecutionGateRecord(
        event_id=str(payload.get("event_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        ticket_id=str(payload.get("ticket_id", "")),
        confirmation_id=str(payload.get("confirmation_id", "")),
        gate_key=str(payload.get("gate_key", "")),
        result=str(payload.get("result", "")),
        blocking_reason_codes=tuple(str(item) for item in codes),
        timestamp=str(payload.get("timestamp", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        dry_run_event_id=str(payload.get("dry_run_event_id", "")),
        production_execution_allowed=False,
        repository2_execution_attempted=False,
    )


def _record_to_dict(record: ProductionActivationExecutionGateRecord) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "activation_request_id": record.activation_request_id,
        "ticket_id": record.ticket_id,
        "confirmation_id": record.confirmation_id,
        "gate_key": record.gate_key,
        "result": record.result,
        "blocking_reason_codes": list(record.blocking_reason_codes),
        "timestamp": record.timestamp,
        "tested_commit_sha": record.tested_commit_sha,
        "release_tag": record.release_tag,
        "dry_run_event_id": record.dry_run_event_id,
        "production_execution_allowed": False,
        "repository2_execution_attempted": False,
    }


def _load_execution_gate_records(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> list[ProductionActivationExecutionGateRecord]:
    path = _execution_gate_history_path(
        activation_request_id,
        history_dir=history_dir,
    )
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProductionActivationExecutionGateError(
            "Execution gate history record is corrupted."
        ) from exc
    records_payload = payload.get("records", [])
    if not isinstance(records_payload, list):
        raise ProductionActivationExecutionGateError(
            "Execution gate history records must be a list."
        )
    return [
        _record_from_dict(item)
        for item in records_payload
        if isinstance(item, Mapping)
    ]


def find_ready_execution_gate_record(
    activation_request_id: str,
    *,
    gate_key: str,
    history_dir: Path | None = None,
) -> ProductionActivationExecutionGateRecord | None:
    """Return the latest ready execution gate record for a gate key."""
    normalized_key = (gate_key or "").strip()
    for record in reversed(
        _load_execution_gate_records(activation_request_id, history_dir=history_dir)
    ):
        if record.gate_key == normalized_key and record.result == "ready":
            return record
    return None


def _atomic_append_execution_gate_record(
    record: ProductionActivationExecutionGateRecord,
    *,
    history_dir: Path | None = None,
) -> None:
    path = _execution_gate_history_path(
        record.activation_request_id,
        history_dir=history_dir,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_execution_gate_records(
        record.activation_request_id,
        history_dir=history_dir,
    )
    for item in existing:
        if item.gate_key == record.gate_key:
            return
        if item.event_id == record.event_id:
            raise ProductionActivationExecutionGateError(
                "duplicate execution gate event_id detected"
            )
    payload = {
        "version": _EXECUTION_GATE_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "records": [_record_to_dict(item) for item in existing]
        + [_record_to_dict(record)],
    }
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        raise ProductionActivationExecutionGateError(
            "Execution gate audit persistence failed."
        ) from exc


def probe_execution_gate_audit_store_available(
    *,
    history_dir: Path | None = None,
) -> bool:
    base = (history_dir or default_execution_gate_history_dir()).resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_active_expired(request: ActivationRequest, *, now: datetime | None = None) -> bool:
    expires_text = (request.active_expires_at or "").strip()
    if not expires_text:
        return True
    return _utc_now(now) >= _parse_iso(expires_text)


def _mirror_in_allowlist(
    resolved_root: str,
    *,
    merged_config: Mapping[str, Any] | None,
) -> bool:
    policy = load_dispatch_executor_policy(merged_config=merged_config)
    if not policy.allowed_pipeline_roots:
        return False
    candidate = os.path.realpath(resolved_root)
    for allowed in policy.allowed_pipeline_roots:
        if os.path.realpath(os.path.expanduser(allowed.strip())) == candidate:
            return True
    return False


def _validate_mirror_root(
    pipeline_root: str,
    *,
    merged_config: Mapping[str, Any] | None = None,
) -> tuple[str, bool, bool]:
    resolved = resolve_pipeline_root(pipeline_root)
    assert_pipeline_root_allowed(resolved)
    trusted = _mirror_in_allowlist(resolved, merged_config=merged_config)
    return resolved, trusted, trusted


def _validate_ticket_scope(
    request: ActivationRequest,
    *,
    ticket_id: str,
) -> tuple[bool, bool]:
    scope_type = (request.activation_scope.scope_type or "").strip()
    normalized_ticket = (ticket_id or "").strip()
    if not normalized_ticket:
        return False, False
    if scope_type == ACTIVATION_SCOPE_MAINTENANCE_WINDOW:
        return False, False
    if scope_type not in {ACTIVATION_SCOPE_ONE_SHOT, ACTIVATION_SCOPE_TICKET_SCOPED}:
        return False, False
    if scope_type == ACTIVATION_SCOPE_TICKET_SCOPED:
        scoped_ticket = (request.activation_scope.ticket_id or "").strip()
        if scoped_ticket != normalized_ticket:
            return False, True
    return True, True


def _probe_active_gate_verified(
    request: ActivationRequest,
    *,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> bool:
    checks = (
        _approval_quorum_valid(request),
        _executor_valid(request),
        bool(request.phrase_verified),
        _head_sha_matches(request, repo_root=repo_root),
        _release_tag_valid(request),
        _rollback_present(request),
        _attestation_valid(request),
        _probe_signoff_ready(merged_config=merged_config),
        _probe_cutover_ready(merged_config=merged_config),
        not _probe_recovery_required(request),
        not _probe_repair_lock_held(request),
        _probe_regression_clear(),
    )
    return all(checks)


def _resolve_node_executable_contract(
    merged_config: Mapping[str, Any] | None,
) -> str:
    if not merged_config or not isinstance(merged_config, Mapping):
        return ""
    coo = merged_config.get("coo")
    if not isinstance(coo, dict):
        return ""
    dispatch = coo.get("dispatch")
    if not isinstance(dispatch, dict):
        return ""
    runner = dispatch.get("runner")
    if not isinstance(runner, dict):
        return ""
    node_executable = runner.get("node_executable")
    if not isinstance(node_executable, str):
        return ""
    return node_executable.strip()


def _probe_bounded_runner_contract_available(
    *,
    merged_config: Mapping[str, Any] | None,
) -> bool:
    try:
        binding = load_dispatch_runner_binding_state()
    except DispatchRunnerBindingStateError:
        return False
    if binding.state != RUNNER_BINDING_STATE_BOUND:
        return False
    provider = assess_dispatch_runner_provider(merged_config)
    if not provider.provider_valid:
        return False
    if provider.runner_provider_mode != RUNNER_PROVIDER_MODE_BOUNDED:
        return False
    node_executable = _resolve_node_executable_contract(merged_config)
    if not node_executable:
        return False
    try:
        from agent.coo.bounded_subprocess_runner import (
            _resolve_dispatch_node_executable,
        )

        _resolve_dispatch_node_executable(node_executable)
    except Exception:
        return False
    return True


def _probe_execution_runtime_disabled(
    *,
    merged_config: Mapping[str, Any] | None,
) -> bool:
    policy = load_dispatch_executor_policy(merged_config=merged_config)
    return not policy.enabled


def _is_dry_run_fresh(
    request: ActivationRequest,
    record_timestamp: str,
    *,
    now: datetime | None = None,
) -> bool:
    active_at_text = (request.active_at or "").strip()
    active_expires_text = (request.active_expires_at or "").strip()
    if not active_at_text or not record_timestamp.strip():
        return False
    active_at = _parse_iso(active_at_text)
    record_at = _parse_iso(record_timestamp)
    current = _utc_now(now)
    if record_at > active_at + timedelta(seconds=1):
        return False
    max_age = timedelta(minutes=_DRY_RUN_MAX_AGE_MINUTES)
    if active_expires_text:
        active_expires = _parse_iso(active_expires_text)
        ttl_window = active_expires - active_at
        if ttl_window < max_age:
            max_age = ttl_window
    return current - record_at <= max_age and current <= (
        _parse_iso(active_expires_text) if active_expires_text else current
    )


def _validate_linked_dry_run(
    request: ActivationRequest,
    *,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root_resolved: str,
    dry_run_history_dir: Path | None,
    now: datetime | None,
) -> tuple[bool, bool, list[str]]:
    blocking: list[str] = []
    try:
        record = find_dry_run_record(
            request.activation_request_id,
            event_id=request.dry_run_event_id,
            history_dir=dry_run_history_dir,
        )
    except ProductionActivationDryRunError as exc:
        raise ProductionActivationExecutionGateError(str(exc)) from exc
    if record is None:
        blocking.append(BLOCK_DRY_RUN_MISSING)
        return False, False, blocking
    if record.result != "ready":
        blocking.append(BLOCK_DRY_RUN_NOT_READY)
    if record.activation_request_id != request.activation_request_id:
        blocking.append(BLOCK_DRY_RUN_CORRELATION_MISMATCH)
    if record.tested_commit_sha != request.tested_commit_sha:
        blocking.append(BLOCK_DRY_RUN_CORRELATION_MISMATCH)
    if record.release_tag != request.release_tag:
        blocking.append(BLOCK_DRY_RUN_CORRELATION_MISMATCH)
    if record.ticket_id != ticket_id:
        blocking.append(BLOCK_DRY_RUN_CORRELATION_MISMATCH)
    if record.confirmation_id != confirmation_id:
        blocking.append(BLOCK_DRY_RUN_CORRELATION_MISMATCH)
    expected_key = compute_dry_run_key(
        activation_request_id=request.activation_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root_resolved=pipeline_root_resolved,
    )
    if record.dry_run_key != expected_key:
        blocking.append(BLOCK_DRY_RUN_CORRELATION_MISMATCH)
    if request.dry_run_event_id and record.event_id != request.dry_run_event_id:
        blocking.append(BLOCK_DRY_RUN_CORRELATION_MISMATCH)
    if request.dry_run_key and record.dry_run_key != request.dry_run_key:
        blocking.append(BLOCK_DRY_RUN_CORRELATION_MISMATCH)
    fresh = _is_dry_run_fresh(request, record.timestamp, now=now)
    if not fresh:
        blocking.append(BLOCK_DRY_RUN_STALE)
    verified = (
        record.result == "ready"
        and BLOCK_DRY_RUN_MISSING not in blocking
        and BLOCK_DRY_RUN_NOT_READY not in blocking
        and BLOCK_DRY_RUN_CORRELATION_MISMATCH not in blocking
    )
    return verified and fresh, fresh, blocking


def _resolve_recommended_action(
    assessment: ProductionActivationExecutionGateAssessment,
) -> str:
    if assessment.already_evaluated and assessment.execution_gate_ready:
        return ACTION_ALREADY_EVALUATED
    if assessment.execution_gate_ready:
        return ACTION_EXECUTION_GATE_READY_WAIT_FOR_PHASE_14H_3
    if assessment.activation_state != ACTIVATION_STATE_ACTIVE:
        if assessment.activation_state in {"revoked", "suspended"}:
            return ACTION_CREATE_NEW_ACTIVATION_PROPOSAL
        return ACTION_MAINTAIN_PRODUCTION_BLOCK
    if BLOCK_ACTIVE_EXPIRED in assessment.blocking_reasons:
        return ACTION_SUSPEND_ACTIVE_ACTIVATION
    if BLOCK_PRODUCTION_ROOT_DENIED in assessment.blocking_reasons:
        return ACTION_MAINTAIN_PRODUCTION_BLOCK
    if BLOCK_MIRROR_ROOT_NOT_TRUSTED in assessment.blocking_reasons:
        return ACTION_PREPARE_ISOLATED_PRODUCTION_MIRROR
    if BLOCK_BINDING_NOT_BOUND in assessment.blocking_reasons:
        return ACTION_BIND_PRODUCTION_EXECUTOR
    if BLOCK_BOUNDED_RUNNER_CONTRACT_MISSING in assessment.blocking_reasons:
        return ACTION_BIND_PRODUCTION_EXECUTOR
    if BLOCK_DRY_RUN_MISSING in assessment.blocking_reasons or (
        BLOCK_DRY_RUN_STALE in assessment.blocking_reasons
        or BLOCK_DRY_RUN_CORRELATION_MISMATCH in assessment.blocking_reasons
        or BLOCK_DRY_RUN_NOT_READY in assessment.blocking_reasons
    ):
        return ACTION_REFRESH_PRODUCTION_DRY_RUN
    if BLOCK_TICKET_SCOPE_MISMATCH in assessment.blocking_reasons:
        return ACTION_RESOLVE_TICKET_SCOPE
    if BLOCK_CONFIRMATION_SCOPE_MISMATCH in assessment.blocking_reasons or (
        BLOCK_CONFIRMATION_MISSING in assessment.blocking_reasons
        or BLOCK_CONFIRMATION_CONSUMED in assessment.blocking_reasons
    ):
        return ACTION_RESOLVE_CONFIRMATION_SCOPE
    if BLOCK_RECOVERY_REQUIRED in assessment.blocking_reasons:
        return ACTION_RESOLVE_RECOVERY_ISSUE
    if BLOCK_REGRESSION_BLOCKED in assessment.blocking_reasons:
        return ACTION_RESOLVE_REGRESSION_FAILURE
    return ACTION_MAINTAIN_PRODUCTION_BLOCK


def evaluate_production_execution_gate(
    request: ActivationRequest,
    *,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    dry_run_history_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionActivationExecutionGateAssessment:
    """Evaluate execution gate contract without mutating activation state."""
    blocking: list[str] = []
    normalized_ticket = (ticket_id or "").strip()
    normalized_confirmation = (confirmation_id or "").strip()

    if request.state != ACTIVATION_STATE_ACTIVE:
        blocking.append(BLOCK_ACTIVATION_NOT_ACTIVE)

    active_not_expired = not _is_active_expired(request, now=now)
    if not active_not_expired:
        blocking.append(BLOCK_ACTIVE_EXPIRED)

    if not (request.active_actor_id or "").strip():
        blocking.append(BLOCK_ACTIVE_ARTIFACT_INVALID)
    if not (request.dry_run_event_id or "").strip() or not (request.dry_run_key or "").strip():
        blocking.append(BLOCK_ACTIVE_ARTIFACT_INVALID)

    active_gate_verified = False
    if request.state == ACTIVATION_STATE_ACTIVE:
        active_gate_verified = _probe_active_gate_verified(
            request,
            repo_root=repo_root,
            merged_config=merged_config,
        )
        if not active_gate_verified:
            if not _rollback_present(request):
                blocking.append(BLOCK_ROLLBACK_NOT_READY)
            if not _probe_signoff_ready(merged_config=merged_config):
                blocking.append(BLOCK_SIGNOFF_NOT_READY)
            if not _probe_cutover_ready(merged_config=merged_config):
                blocking.append(BLOCK_CUTOVER_NOT_READY)
            if _probe_recovery_required(request):
                blocking.append(BLOCK_RECOVERY_REQUIRED)
            if _probe_repair_lock_held(request):
                blocking.append(BLOCK_REPAIR_LOCK_HELD)
            if not _probe_regression_clear():
                blocking.append(BLOCK_REGRESSION_BLOCKED)

    ticket_scope_valid, single_ticket_scope = _validate_ticket_scope(
        request,
        ticket_id=normalized_ticket,
    )
    if not ticket_scope_valid:
        blocking.append(BLOCK_TICKET_SCOPE_MISMATCH)

    mirror_root_trusted = False
    isolated_mirror_only = False
    resolved_root = ""
    try:
        resolved_root, mirror_root_trusted, isolated_mirror_only = _validate_mirror_root(
            pipeline_root,
            merged_config=merged_config,
        )
    except ValueError as exc:
        message = str(exc).lower()
        if "hard-denied" in message:
            blocking.append(BLOCK_PRODUCTION_ROOT_DENIED)
        else:
            blocking.append(BLOCK_MIRROR_ROOT_NOT_TRUSTED)
    if resolved_root and not mirror_root_trusted and BLOCK_MIRROR_ROOT_NOT_TRUSTED not in blocking and BLOCK_PRODUCTION_ROOT_DENIED not in blocking:
        blocking.append(BLOCK_MIRROR_ROOT_NOT_TRUSTED)

    resolved_dry_run_history = dry_run_history_dir or default_dry_run_history_dir()
    dry_run_verified = False
    dry_run_fresh = False
    if request.state == ACTIVATION_STATE_ACTIVE and resolved_root:
        try:
            dry_run_verified, dry_run_fresh, dry_blocking = _validate_linked_dry_run(
                request,
                ticket_id=normalized_ticket,
                confirmation_id=normalized_confirmation,
                pipeline_root_resolved=resolved_root,
                dry_run_history_dir=resolved_dry_run_history,
                now=now,
            )
            for code in dry_blocking:
                if code not in blocking:
                    blocking.append(code)
        except ProductionActivationExecutionGateError:
            blocking.append(BLOCK_DRY_RUN_MISSING)

    confirmation_scope_valid = False
    confirmation_unconsumed = False
    if normalized_confirmation and resolved_root:
        try:
            confirmation = read_confirmation(
                normalized_confirmation,
                confirmation_dir=confirmation_dir,
                reject_consumed=True,
            )
            confirmation_unconsumed = True
            if confirmation.ticket_id != normalized_ticket:
                raise ProductionActivationExecutionGateError(
                    "confirmation ticket mismatch"
                )
            assert_pipeline_root_matches_attestation(
                cli_pipeline_root=resolved_root,
                attested_pipeline_root=confirmation.attested_pipeline_root,
            )
            confirmation_scope_valid = True
        except KeyError:
            blocking.append(BLOCK_CONFIRMATION_MISSING)
        except ValueError as exc:
            message = str(exc).lower()
            if "consumed" in message:
                blocking.append(BLOCK_CONFIRMATION_CONSUMED)
            else:
                blocking.append(BLOCK_CONFIRMATION_SCOPE_MISMATCH)
        except ProductionActivationExecutionGateError:
            blocking.append(BLOCK_CONFIRMATION_SCOPE_MISMATCH)
    else:
        blocking.append(BLOCK_CONFIRMATION_MISSING)

    bundle_unconsumed = False
    bundle_present = False
    if normalized_ticket:
        try:
            from agent.coo.dispatch_bundle_store import read_bundle

            bundle = read_bundle(
                normalized_ticket,
                bundle_dir=bundle_dir,
                reject_consumed=True,
            )
            bundle_present = True
            bundle_unconsumed = True
            if bundle.ticket_id != normalized_ticket:
                blocking.append(BLOCK_TICKET_SCOPE_MISMATCH)
            active_at_text = (request.active_at or "").strip()
            if active_at_text and bundle.updated_at:
                if _parse_iso(bundle.updated_at) > _parse_iso(active_at_text):
                    blocking.append(BLOCK_DRY_RUN_STALE)
            if bundle.unlock_token_id and confirmation_unconsumed:
                confirmation = read_confirmation(
                    normalized_confirmation,
                    confirmation_dir=confirmation_dir,
                    reject_consumed=True,
                )
                if bundle.unlock_token_id != confirmation.unlock_token_id:
                    blocking.append(BLOCK_CONFIRMATION_SCOPE_MISMATCH)
            snapshot_publish = _snapshot_has_publish_intent(bundle.snapshot)
            if snapshot_publish:
                blocking.append(BLOCK_PUBLISH_NOT_ALLOWED)
        except KeyError:
            blocking.append(BLOCK_BUNDLE_MISSING)
        except ValueError as exc:
            message = str(exc).lower()
            if "consumed" in message:
                blocking.append(BLOCK_BUNDLE_CONSUMED)
            else:
                blocking.append(BLOCK_BUNDLE_MISSING)
        except OSError:
            blocking.append(BLOCK_BUNDLE_MISSING)
    else:
        blocking.append(BLOCK_BUNDLE_MISSING)

    if _probe_publish_intent(normalized_ticket):
        blocking.append(BLOCK_PUBLISH_NOT_ALLOWED)

    draft_only = not request.activation_scope.publish_allowed
    if not draft_only:
        blocking.append(BLOCK_PUBLISH_NOT_ALLOWED)

    executor_binding_ready = False
    bounded_runner_contract_available = False
    try:
        binding = load_dispatch_runner_binding_state()
        executor_binding_ready = binding.state == RUNNER_BINDING_STATE_BOUND
    except DispatchRunnerBindingStateError:
        executor_binding_ready = False
    if not executor_binding_ready:
        blocking.append(BLOCK_BINDING_NOT_BOUND)

    bounded_runner_contract_available = _probe_bounded_runner_contract_available(
        merged_config=merged_config,
    )
    if not bounded_runner_contract_available:
        blocking.append(BLOCK_BOUNDED_RUNNER_CONTRACT_MISSING)

    recovery_clear = not _probe_recovery_required(request)
    repair_lock_clear = not _probe_repair_lock_held(request)
    regression_clear = _probe_regression_clear()
    signoff_ready = _probe_signoff_ready(merged_config=merged_config)
    cutover_ready = _probe_cutover_ready(merged_config=merged_config)
    rollback_ready = _rollback_present(request)

    kill_switch_available = is_kill_switch_available(request, store_dir=store_dir)
    if not kill_switch_available:
        blocking.append(BLOCK_KILL_SWITCH_UNAVAILABLE)

    audit_store_available = (
        probe_execution_gate_audit_store_available(history_dir=history_dir)
        and probe_dry_run_audit_store_available(history_dir=resolved_dry_run_history)
        and probe_audit_store_available(store_dir=store_dir)
    )
    if not audit_store_available:
        blocking.append(BLOCK_AUDIT_STORE_UNAVAILABLE)

    execution_runtime_disabled = True

    execution_gate_ready = (
        request.state == ACTIVATION_STATE_ACTIVE
        and active_not_expired
        and not blocking
    )
    assessment = ProductionActivationExecutionGateAssessment(
        activation_request_id=request.activation_request_id,
        activation_state=request.state,
        execution_gate_ready=execution_gate_ready,
        active_not_expired=active_not_expired,
        active_actor_assigned=bool((request.active_actor_id or "").strip()),
        active_gate_verified=active_gate_verified,
        dry_run_verified=dry_run_verified,
        dry_run_fresh=dry_run_fresh,
        ticket_scope_valid=ticket_scope_valid,
        confirmation_scope_valid=confirmation_scope_valid,
        mirror_root_trusted=mirror_root_trusted,
        isolated_mirror_only=isolated_mirror_only,
        single_ticket_scope=single_ticket_scope,
        draft_only=draft_only and BLOCK_PUBLISH_NOT_ALLOWED not in blocking,
        publish_allowed=False,
        executor_binding_ready=executor_binding_ready,
        bounded_runner_contract_available=bounded_runner_contract_available,
        confirmation_unconsumed=confirmation_unconsumed,
        bundle_unconsumed=bundle_unconsumed and bundle_present,
        recovery_clear=recovery_clear,
        repair_lock_clear=repair_lock_clear,
        regression_clear=regression_clear,
        signoff_ready=signoff_ready,
        cutover_ready=cutover_ready,
        kill_switch_available=kill_switch_available,
        audit_store_available=audit_store_available,
        rollback_ready=rollback_ready,
        execution_runtime_disabled=execution_runtime_disabled,
        ticket_id=normalized_ticket,
        confirmation_id=normalized_confirmation,
        blocking_reasons=tuple(blocking),
    )
    return replace(
        assessment,
        recommended_action=_resolve_recommended_action(assessment),
    )


def _load_activation_or_fail(
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
        raise ProductionActivationExecutionGateError(str(exc)) from exc
    except ProductionActivationStateError as exc:
        raise ProductionActivationExecutionGateError(str(exc)) from exc


def run_production_execution_gate(
    *,
    activation_request_id: str,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    dry_run_history_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[ProductionActivationExecutionGateAssessment, bool]:
    request = _load_activation_or_fail(activation_request_id, store_dir=store_dir)
    try:
        resolved_root = resolve_pipeline_root(pipeline_root)
        assert_pipeline_root_allowed(resolved_root)
    except ValueError:
        resolved_root = ""
    key = _gate_key(
        activation_request_id=activation_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root_resolved=resolved_root or pipeline_root.strip(),
    )
    existing = _load_execution_gate_records(
        activation_request_id,
        history_dir=history_dir,
    )
    if any(record.gate_key == key for record in existing):
        assessment = evaluate_production_execution_gate(
            request,
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            pipeline_root=pipeline_root,
            repo_root=repo_root,
            store_dir=store_dir,
            history_dir=history_dir,
            dry_run_history_dir=dry_run_history_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            merged_config=merged_config,
            now=now,
        )
        return replace(
            assessment,
            already_evaluated=True,
            recommended_action=ACTION_ALREADY_EVALUATED
            if assessment.execution_gate_ready
            else assessment.recommended_action,
        ), False

    assessment = evaluate_production_execution_gate(
        request,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
        repo_root=repo_root,
        store_dir=store_dir,
        history_dir=history_dir,
        dry_run_history_dir=dry_run_history_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        merged_config=merged_config,
        now=now,
    )
    record = ProductionActivationExecutionGateRecord(
        event_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        gate_key=key,
        result="ready" if assessment.execution_gate_ready else "blocked",
        blocking_reason_codes=assessment.blocking_reasons,
        timestamp=_utc_now_iso(now),
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        dry_run_event_id=request.dry_run_event_id,
        production_execution_allowed=False,
        repository2_execution_attempted=False,
    )
    _atomic_append_execution_gate_record(record, history_dir=history_dir)
    return assessment, True


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "repository2_execution_attempted: false",
        "production_execution_allowed: false",
        "execution_runtime_disabled: true",
        "mirror_root_trusted:",
        "isolated_mirror_only:",
        "dry_run_verified:",
        "dry_run_fresh:",
        "executor_binding_ready:",
        "bounded_runner_contract_available:",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationExecutionGateError(
                f"Unsafe execution gate output field: {token!r}"
            )


def format_production_execution_gate_assessment(
    assessment: ProductionActivationExecutionGateAssessment,
) -> str:
    reasons = (
        ", ".join(assessment.blocking_reasons)
        if assessment.blocking_reasons
        else "(none)"
    )
    lines = [
        "Production Activation Execution Gate",
        "",
        f"activation_request_id: {assessment.activation_request_id}",
        f"activation_state: {assessment.activation_state}",
        f"ticket_id: {assessment.ticket_id}",
        f"confirmation_id: {assessment.confirmation_id}",
        f"execution_gate_ready: {str(assessment.execution_gate_ready).lower()}",
        f"active_not_expired: {str(assessment.active_not_expired).lower()}",
        f"dry_run_verified: {str(assessment.dry_run_verified).lower()}",
        f"dry_run_fresh: {str(assessment.dry_run_fresh).lower()}",
        f"mirror_root_trusted: {str(assessment.mirror_root_trusted).lower()}",
        f"isolated_mirror_only: {str(assessment.isolated_mirror_only).lower()}",
        f"draft_only: {str(assessment.draft_only).lower()}",
        f"executor_binding_ready: {str(assessment.executor_binding_ready).lower()}",
        "bounded_runner_contract_available: "
        f"{str(assessment.bounded_runner_contract_available).lower()}",
        f"publish_allowed: false",
        f"blocking_reasons: {reasons}",
        f"recommended_action: {assessment.recommended_action}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "repository2_execution_attempted: false",
        "execution_runtime_disabled: true",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def run_activation_execution_gate(
    *,
    activation_request_id: str,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    dry_run_history_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    assessment, _ = run_production_execution_gate(
        activation_request_id=activation_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
        repo_root=repo_root,
        store_dir=store_dir,
        history_dir=history_dir,
        dry_run_history_dir=dry_run_history_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        merged_config=merged_config,
        now=now,
    )
    exit_code = 0 if assessment.execution_gate_ready else 1
    return format_production_execution_gate_assessment(assessment), exit_code
