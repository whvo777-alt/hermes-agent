"""Production activation dry-run contract — Phase 14G.

Read-only pre-execution validation without active transition, subprocess,
or Repository2 execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_executor_config import load_dispatch_executor_policy
from agent.coo.dispatch_pipeline_root_trust import (
    assert_pipeline_root_allowed,
    assert_pipeline_root_matches_attestation,
    resolve_pipeline_root,
)
from agent.coo.production_activation_active_gate import evaluate_active_gate
from agent.coo.production_activation_state import (
    ACTIVATION_SCOPE_MAINTENANCE_WINDOW,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_SCOPE_TICKET_SCOPED,
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_ARMED,
    ActivationRequest,
    ProductionActivationStateError,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    load_activation_request,
)
from agent.coo.production_executor_confirmation import read_confirmation
from hermes_constants import get_hermes_home

BLOCK_ACTIVATION_NOT_ARMED = "activation_not_armed"
BLOCK_ARMED_EXPIRED = "armed_expired"
BLOCK_ACTIVE_GATE_NOT_READY = "active_gate_not_ready"
BLOCK_TICKET_SCOPE_MISMATCH = "ticket_scope_mismatch"
BLOCK_CONFIRMATION_SCOPE_MISMATCH = "confirmation_scope_mismatch"
BLOCK_MIRROR_ROOT_NOT_TRUSTED = "mirror_root_not_trusted"
BLOCK_PRODUCTION_ROOT_DENIED = "production_root_denied"
BLOCK_PUBLISH_NOT_ALLOWED = "publish_not_allowed"
BLOCK_RECOVERY_REQUIRED = "recovery_required"
BLOCK_REPAIR_LOCK_HELD = "repair_lock_held"
BLOCK_REGRESSION_BLOCKED = "regression_blocked"
BLOCK_SIGNOFF_NOT_READY = "signoff_not_ready"
BLOCK_CUTOVER_NOT_READY = "cutover_not_ready"
BLOCK_KILL_SWITCH_UNAVAILABLE = "kill_switch_unavailable"
BLOCK_AUDIT_STORE_UNAVAILABLE = "audit_store_unavailable"
BLOCK_ACTIVATION_ARTIFACT_INVALID = "activation_artifact_invalid"

ACTION_PRODUCTION_DRY_RUN_READY_WAIT_FOR_PHASE_14H = (
    "production_dry_run_ready_wait_for_phase_14h"
)
ACTION_RESOLVE_ACTIVATION_GATE = "resolve_activation_gate"
ACTION_PREPARE_ISOLATED_PRODUCTION_MIRROR = "prepare_isolated_production_mirror"
ACTION_RESOLVE_TICKET_SCOPE = "resolve_ticket_scope"
ACTION_RESOLVE_CONFIRMATION_SCOPE = "resolve_confirmation_scope"
ACTION_RESOLVE_RECOVERY_ISSUE = "resolve_recovery_issue"
ACTION_RESOLVE_REGRESSION_FAILURE = "resolve_regression_failure"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_CREATE_NEW_ACTIVATION_PROPOSAL = "create_new_activation_proposal"
ACTION_ALREADY_EVALUATED = "already_evaluated"

_DRY_RUN_STORE_DIR = "production-activation-dry-run"
_DRY_RUN_STORE_VERSION = 1

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
        "confirm-repository2-execution",
    }
)


class ProductionActivationDryRunError(ValueError):
    """Raised when production dry-run evaluation cannot complete safely."""


@dataclass(frozen=True)
class ProductionActivationDryRunAssessment:
    """Safe production dry-run assessment."""

    activation_request_id: str
    activation_state: str
    dry_run_ready: bool
    active_gate_ready: bool
    ticket_scope_valid: bool
    confirmation_scope_valid: bool
    pipeline_root_trusted: bool
    isolated_mirror_only: bool
    single_ticket_scope: bool
    draft_only: bool
    publish_allowed: bool = False
    executor_assigned: bool = False
    armed_not_expired: bool = False
    tested_commit_matches: bool = False
    release_tag_valid: bool = False
    attestation_valid: bool = False
    rollback_commit_present: bool = False
    recovery_clear: bool = False
    repair_lock_clear: bool = False
    regression_clear: bool = False
    signoff_ready: bool = False
    cutover_ready: bool = False
    kill_switch_available: bool = False
    audit_store_available: bool = False
    production_execution_allowed: bool = False
    repository2_execution_attempted: bool = False
    ticket_id: str = ""
    confirmation_id: str = ""
    blocking_reasons: tuple[str, ...] = ()
    recommended_action: str = ""
    already_evaluated: bool = False


@dataclass(frozen=True)
class ProductionActivationDryRunRecord:
    """Append-only dry-run audit record."""

    event_id: str
    activation_request_id: str
    ticket_id: str
    confirmation_id: str
    dry_run_key: str
    result: str
    blocking_reason_codes: tuple[str, ...]
    timestamp: str
    tested_commit_sha: str
    release_tag: str
    repository2_execution_attempted: bool = False


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def default_dry_run_history_dir() -> Path:
    return get_hermes_home() / "coo" / _DRY_RUN_STORE_DIR


def _dry_run_history_path(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionActivationDryRunError("activation_request_id is required")
    base = (history_dir or default_dry_run_history_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationDryRunError(
            "Dry-run history directory must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def _dry_run_key(
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


def _record_from_dict(payload: Mapping[str, Any]) -> ProductionActivationDryRunRecord:
    codes = payload.get("blocking_reason_codes", [])
    if not isinstance(codes, list):
        raise ProductionActivationDryRunError(
            "Dry-run record blocking_reason_codes must be a list."
        )
    return ProductionActivationDryRunRecord(
        event_id=str(payload.get("event_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        ticket_id=str(payload.get("ticket_id", "")),
        confirmation_id=str(payload.get("confirmation_id", "")),
        dry_run_key=str(payload.get("dry_run_key", "")),
        result=str(payload.get("result", "")),
        blocking_reason_codes=tuple(str(item) for item in codes),
        timestamp=str(payload.get("timestamp", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        repository2_execution_attempted=bool(
            payload.get("repository2_execution_attempted", False)
        ),
    )


def _record_to_dict(record: ProductionActivationDryRunRecord) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "activation_request_id": record.activation_request_id,
        "ticket_id": record.ticket_id,
        "confirmation_id": record.confirmation_id,
        "dry_run_key": record.dry_run_key,
        "result": record.result,
        "blocking_reason_codes": list(record.blocking_reason_codes),
        "timestamp": record.timestamp,
        "tested_commit_sha": record.tested_commit_sha,
        "release_tag": record.release_tag,
        "repository2_execution_attempted": False,
    }


def _load_dry_run_records(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> list[ProductionActivationDryRunRecord]:
    path = _dry_run_history_path(activation_request_id, history_dir=history_dir)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProductionActivationDryRunError(
            "Dry-run history record is corrupted."
        ) from exc
    records_payload = payload.get("records", [])
    if not isinstance(records_payload, list):
        raise ProductionActivationDryRunError("Dry-run history records must be a list.")
    return [
        _record_from_dict(item)
        for item in records_payload
        if isinstance(item, Mapping)
    ]


def _atomic_append_dry_run_record(
    record: ProductionActivationDryRunRecord,
    *,
    history_dir: Path | None = None,
) -> None:
    path = _dry_run_history_path(
        record.activation_request_id,
        history_dir=history_dir,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_dry_run_records(
        record.activation_request_id,
        history_dir=history_dir,
    )
    for item in existing:
        if item.dry_run_key == record.dry_run_key:
            return
        if item.event_id == record.event_id:
            raise ProductionActivationDryRunError(
                "duplicate dry-run event_id detected"
            )
    payload = {
        "version": _DRY_RUN_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "records": [_record_to_dict(item) for item in existing] + [_record_to_dict(record)],
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
        raise ProductionActivationDryRunError(
            "Dry-run audit persistence failed."
        ) from exc


def probe_dry_run_audit_store_available(*, history_dir: Path | None = None) -> bool:
    base = (history_dir or default_dry_run_history_dir()).resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def load_latest_dry_run_record(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> ProductionActivationDryRunRecord | None:
    """Return the latest append-only dry-run record for an activation."""
    records = _load_dry_run_records(
        activation_request_id,
        history_dir=history_dir,
    )
    if not records:
        return None
    return records[-1]


def find_dry_run_record(
    activation_request_id: str,
    *,
    event_id: str = "",
    dry_run_key: str = "",
    history_dir: Path | None = None,
) -> ProductionActivationDryRunRecord | None:
    """Return a dry-run record linked by event id and/or dry-run key."""
    records = _load_dry_run_records(
        activation_request_id,
        history_dir=history_dir,
    )
    normalized_event = (event_id or "").strip()
    normalized_key = (dry_run_key or "").strip().lower()
    for record in reversed(records):
        if normalized_event and record.event_id != normalized_event:
            continue
        if normalized_key and record.dry_run_key != normalized_key:
            continue
        return record
    return None


def compute_dry_run_key(
    *,
    activation_request_id: str,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root_resolved: str,
) -> str:
    """Compute the dry-run idempotency key for an evaluation input set."""
    return _dry_run_key(
        activation_request_id=activation_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root_resolved=pipeline_root_resolved,
    )


def _mirror_in_allowlist(resolved_root: str, *, merged_config: Mapping[str, Any] | None) -> bool:
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
    try:
        resolved = resolve_pipeline_root(pipeline_root)
        assert_pipeline_root_allowed(resolved)
    except ValueError as exc:
        message = str(exc).lower()
        if "hard-denied" in message or "production" in message:
            raise ProductionActivationDryRunError(str(exc)) from exc
        raise ProductionActivationDryRunError(str(exc)) from exc
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


def _validate_confirmation_scope(
    *,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root_resolved: str,
    confirmation_dir: Path | None = None,
) -> bool:
    confirmation = read_confirmation(
        confirmation_id,
        confirmation_dir=confirmation_dir,
        reject_consumed=True,
    )
    if confirmation.ticket_id != ticket_id:
        raise ProductionActivationDryRunError(
            "confirmation ticket_id does not match dry-run ticket scope"
        )
    assert_pipeline_root_matches_attestation(
        cli_pipeline_root=pipeline_root_resolved,
        attested_pipeline_root=confirmation.attested_pipeline_root,
    )
    try:
        from agent.coo.dispatch_bundle_store import read_bundle

        bundle = read_bundle(ticket_id)
    except (KeyError, ValueError, OSError):
        return True
    if bundle.unlock_token_id != confirmation.unlock_token_id:
        raise ProductionActivationDryRunError(
            "confirmation unlock_token_id does not match dispatch bundle"
        )
    return True


def _snapshot_has_publish_intent(snapshot: Mapping[str, Any]) -> bool:
    def walk(value: object) -> bool:
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key).lower()
                if key_text == "publish_allowed":
                    continue
                if "publish" in key_text and item is True:
                    return True
                if walk(item):
                    return True
        elif isinstance(value, list):
            for item in value:
                if walk(item):
                    return True
        return False

    return walk(snapshot)


def _probe_publish_intent(ticket_id: str) -> bool:
    try:
        from agent.coo.dispatch_bundle_store import read_bundle

        bundle = read_bundle(ticket_id)
    except (KeyError, ValueError, OSError):
        return False
    return _snapshot_has_publish_intent(bundle.snapshot)


def _is_arm_expired(request: ActivationRequest, *, now: datetime | None = None) -> bool:
    armed_text = (request.armed_expires_at or "").strip()
    if not armed_text:
        return True
    expires = datetime.fromisoformat(armed_text.replace("Z", "+00:00"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return _utc_now(now) >= expires


def _resolve_recommended_action(assessment: ProductionActivationDryRunAssessment) -> str:
    if assessment.already_evaluated and assessment.dry_run_ready:
        return ACTION_ALREADY_EVALUATED
    if assessment.activation_state not in {ACTIVATION_STATE_ARMED}:
        if assessment.activation_state in {"revoked", "suspended"}:
            return ACTION_CREATE_NEW_ACTIVATION_PROPOSAL
        return ACTION_RESOLVE_ACTIVATION_GATE
    if assessment.dry_run_ready:
        return ACTION_PRODUCTION_DRY_RUN_READY_WAIT_FOR_PHASE_14H
    if BLOCK_PRODUCTION_ROOT_DENIED in assessment.blocking_reasons:
        return ACTION_MAINTAIN_PRODUCTION_BLOCK
    if BLOCK_MIRROR_ROOT_NOT_TRUSTED in assessment.blocking_reasons:
        return ACTION_PREPARE_ISOLATED_PRODUCTION_MIRROR
    if BLOCK_TICKET_SCOPE_MISMATCH in assessment.blocking_reasons:
        return ACTION_RESOLVE_TICKET_SCOPE
    if BLOCK_CONFIRMATION_SCOPE_MISMATCH in assessment.blocking_reasons:
        return ACTION_RESOLVE_CONFIRMATION_SCOPE
    if BLOCK_RECOVERY_REQUIRED in assessment.blocking_reasons:
        return ACTION_RESOLVE_RECOVERY_ISSUE
    if BLOCK_REGRESSION_BLOCKED in assessment.blocking_reasons:
        return ACTION_RESOLVE_REGRESSION_FAILURE
    if BLOCK_ACTIVE_GATE_NOT_READY in assessment.blocking_reasons:
        return ACTION_RESOLVE_ACTIVATION_GATE
    return ACTION_RESOLVE_ACTIVATION_GATE


def evaluate_production_dry_run(
    request: ActivationRequest,
    *,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionActivationDryRunAssessment:
    """Evaluate production dry-run contract without mutating activation state."""
    blocking: list[str] = []
    normalized_ticket = (ticket_id or "").strip()
    normalized_confirmation = (confirmation_id or "").strip()

    if request.state != ACTIVATION_STATE_ARMED:
        if request.state == ACTIVATION_STATE_ACTIVE:
            blocking.append(BLOCK_ACTIVATION_ARTIFACT_INVALID)
        else:
            blocking.append(BLOCK_ACTIVATION_NOT_ARMED)

    armed_not_expired = not _is_arm_expired(request, now=now)
    if not armed_not_expired:
        blocking.append(BLOCK_ARMED_EXPIRED)

    gate = evaluate_active_gate(
        request,
        repo_root=repo_root,
        store_dir=store_dir,
        merged_config=merged_config,
        now=now,
    )
    active_gate_ready = gate.gate_ready
    if not active_gate_ready:
        blocking.append(BLOCK_ACTIVE_GATE_NOT_READY)
        for reason in gate.blocking_reasons:
            mapped = {
                "recovery_required": BLOCK_RECOVERY_REQUIRED,
                "repair_lock_held": BLOCK_REPAIR_LOCK_HELD,
                "regression_fail": BLOCK_REGRESSION_BLOCKED,
                "signoff_not_ready": BLOCK_SIGNOFF_NOT_READY,
                "cutover_not_ready": BLOCK_CUTOVER_NOT_READY,
                "kill_switch_unavailable": BLOCK_KILL_SWITCH_UNAVAILABLE,
                "audit_store_unavailable": BLOCK_AUDIT_STORE_UNAVAILABLE,
            }.get(reason)
            if mapped and mapped not in blocking:
                blocking.append(mapped)

    ticket_scope_valid, single_ticket_scope = _validate_ticket_scope(
        request,
        ticket_id=normalized_ticket,
    )
    if not ticket_scope_valid:
        blocking.append(BLOCK_TICKET_SCOPE_MISMATCH)

    pipeline_root_trusted = False
    isolated_mirror_only = False
    resolved_root = ""
    try:
        resolved_root, pipeline_root_trusted, isolated_mirror_only = _validate_mirror_root(
            pipeline_root,
            merged_config=merged_config,
        )
    except ProductionActivationDryRunError as exc:
        message = str(exc).lower()
        if "hard-denied" in message:
            blocking.append(BLOCK_PRODUCTION_ROOT_DENIED)
        else:
            blocking.append(BLOCK_MIRROR_ROOT_NOT_TRUSTED)

    confirmation_scope_valid = False
    if normalized_confirmation and resolved_root:
        try:
            confirmation_scope_valid = _validate_confirmation_scope(
                ticket_id=normalized_ticket,
                confirmation_id=normalized_confirmation,
                pipeline_root_resolved=resolved_root,
                confirmation_dir=confirmation_dir,
            )
        except (ProductionActivationDryRunError, ValueError, KeyError):
            blocking.append(BLOCK_CONFIRMATION_SCOPE_MISMATCH)
    else:
        blocking.append(BLOCK_CONFIRMATION_SCOPE_MISMATCH)

    if resolved_root and not pipeline_root_trusted:
        blocking.append(BLOCK_MIRROR_ROOT_NOT_TRUSTED)

    draft_only = not request.activation_scope.publish_allowed
    if not draft_only:
        blocking.append(BLOCK_PUBLISH_NOT_ALLOWED)
    if _probe_publish_intent(normalized_ticket):
        blocking.append(BLOCK_PUBLISH_NOT_ALLOWED)
        draft_only = False

    audit_store_available = probe_dry_run_audit_store_available(history_dir=history_dir)
    if not audit_store_available:
        blocking.append(BLOCK_AUDIT_STORE_UNAVAILABLE)

    dry_run_ready = request.state == ACTIVATION_STATE_ARMED and not blocking
    assessment = ProductionActivationDryRunAssessment(
        activation_request_id=request.activation_request_id,
        activation_state=request.state,
        dry_run_ready=dry_run_ready,
        active_gate_ready=active_gate_ready,
        ticket_scope_valid=ticket_scope_valid,
        confirmation_scope_valid=confirmation_scope_valid,
        pipeline_root_trusted=pipeline_root_trusted,
        isolated_mirror_only=isolated_mirror_only,
        single_ticket_scope=single_ticket_scope,
        draft_only=draft_only and BLOCK_PUBLISH_NOT_ALLOWED not in blocking,
        publish_allowed=False,
        executor_assigned=bool((request.executor_id or "").strip()),
        armed_not_expired=armed_not_expired,
        tested_commit_matches=gate.tested_commit_matches,
        release_tag_valid=gate.release_tag_valid,
        attestation_valid=gate.attestation_valid,
        rollback_commit_present=gate.rollback_commit_present,
        recovery_clear=gate.recovery_clear,
        repair_lock_clear=gate.repair_lock_clear,
        regression_clear=gate.regression_clear,
        signoff_ready=gate.signoff_ready,
        cutover_ready=gate.cutover_ready,
        kill_switch_available=gate.kill_switch_available,
        audit_store_available=audit_store_available,
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
        raise ProductionActivationDryRunError(str(exc)) from exc
    except ProductionActivationStateError as exc:
        raise ProductionActivationDryRunError(str(exc)) from exc


def run_production_activation_dry_run(
    *,
    activation_request_id: str,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[ProductionActivationDryRunAssessment, bool]:
    request = _load_activation_or_fail(activation_request_id, store_dir=store_dir)
    try:
        resolved_root = resolve_pipeline_root(pipeline_root)
        assert_pipeline_root_allowed(resolved_root)
    except ValueError:
        resolved_root = ""
    key = _dry_run_key(
        activation_request_id=activation_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root_resolved=resolved_root or pipeline_root.strip(),
    )
    existing = _load_dry_run_records(activation_request_id, history_dir=history_dir)
    if any(record.dry_run_key == key for record in existing):
        assessment = evaluate_production_dry_run(
            request,
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            pipeline_root=pipeline_root,
            repo_root=repo_root,
            store_dir=store_dir,
            history_dir=history_dir,
            confirmation_dir=confirmation_dir,
            merged_config=merged_config,
            now=now,
        )
        return replace(
            assessment,
            already_evaluated=True,
            recommended_action=ACTION_ALREADY_EVALUATED
            if assessment.dry_run_ready
            else assessment.recommended_action,
        ), False

    assessment = evaluate_production_dry_run(
        request,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
        repo_root=repo_root,
        store_dir=store_dir,
        history_dir=history_dir,
        confirmation_dir=confirmation_dir,
        merged_config=merged_config,
        now=now,
    )
    record = ProductionActivationDryRunRecord(
        event_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        dry_run_key=key,
        result="ready" if assessment.dry_run_ready else "blocked",
        blocking_reason_codes=assessment.blocking_reasons,
        timestamp=_utc_now_iso(now),
        tested_commit_sha=request.tested_commit_sha,
        release_tag=request.release_tag,
        repository2_execution_attempted=False,
    )
    _atomic_append_dry_run_record(record, history_dir=history_dir)
    return assessment, True


def _assert_safe_output(output: str) -> None:
    # Required safety sentinels and boolean gate flags are allowed.
    sanitized = output
    for allowed in (
        "repository2_execution_attempted: false",
        "production_execution_allowed: false",
        "pipeline_root_trusted:",
        "isolated_mirror_only:",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationDryRunError(
                f"Unsafe production dry-run output field: {token!r}"
            )


def format_production_dry_run_assessment(
    assessment: ProductionActivationDryRunAssessment,
) -> str:
    reasons = (
        ", ".join(assessment.blocking_reasons)
        if assessment.blocking_reasons
        else "(none)"
    )
    lines = [
        "Production Activation Dry-Run",
        "",
        f"activation_request_id: {assessment.activation_request_id}",
        f"activation_state: {assessment.activation_state}",
        f"ticket_id: {assessment.ticket_id}",
        f"confirmation_id: {assessment.confirmation_id}",
        f"dry_run_ready: {str(assessment.dry_run_ready).lower()}",
        f"active_gate_ready: {str(assessment.active_gate_ready).lower()}",
        f"ticket_scope_valid: {str(assessment.ticket_scope_valid).lower()}",
        f"confirmation_scope_valid: {str(assessment.confirmation_scope_valid).lower()}",
        f"pipeline_root_trusted: {str(assessment.pipeline_root_trusted).lower()}",
        f"isolated_mirror_only: {str(assessment.isolated_mirror_only).lower()}",
        f"draft_only: {str(assessment.draft_only).lower()}",
        f"publish_allowed: false",
        f"blocking_reasons: {reasons}",
        f"recommended_action: {assessment.recommended_action}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "repository2_execution_attempted: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def run_activation_dry_run(
    *,
    activation_request_id: str,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root: str,
    repo_root: Path | None = None,
    store_dir: Path | None = None,
    history_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    assessment, _ = run_production_activation_dry_run(
        activation_request_id=activation_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
        repo_root=repo_root,
        store_dir=store_dir,
        history_dir=history_dir,
        confirmation_dir=confirmation_dir,
        merged_config=merged_config,
        now=now,
    )
    exit_code = 0 if assessment.dry_run_ready else 1
    return format_production_dry_run_assessment(assessment), exit_code
