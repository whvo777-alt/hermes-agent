"""Controlled production window lifecycle — Phase 15B.

Append-only window open/close/emergency-close events bound to an immutable
governed cutover contract. Never starts cutover or grants execution.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_final_signoff import (
    PRODUCTION_FINAL_SIGNOFF_READY,
    PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
    load_final_signoff_record,
)
from agent.coo.production_governed_cutover import (
    CONTRACT_STATUS_PREPARED,
    GOVERNED_CUTOVER_CONTRACT_PREPARED,
    default_governed_cutover_store_dir,
    evaluate_production_governed_cutover,
    load_governed_cutover_contract,
)
from hermes_constants import get_hermes_home

_WINDOW_STORE_DIR = "production-controlled-window"
_WINDOW_STORE_VERSION = 1
_NEXT_PHASE_15C = "Phase_15C_production_runtime_permission"

WINDOW_NOT_OPENED = "WINDOW_NOT_OPENED"
WINDOW_OPEN = "WINDOW_OPEN"
WINDOW_CLOSED = "WINDOW_CLOSED"
WINDOW_EMERGENCY_CLOSED = "WINDOW_EMERGENCY_CLOSED"
WINDOW_EXPIRED = "WINDOW_EXPIRED"
WINDOW_BLOCKED = "WINDOW_BLOCKED"

EVENT_WINDOW_OPEN_REQUESTED = "window_open_requested"
EVENT_WINDOW_OPENED = "window_opened"
EVENT_WINDOW_CLOSE_REQUESTED = "window_close_requested"
EVENT_WINDOW_CLOSED = "window_closed"
EVENT_EMERGENCY_CLOSE_REQUESTED = "emergency_close_requested"
EVENT_WINDOW_EMERGENCY_CLOSED = "window_emergency_closed"
EVENT_WINDOW_EXPIRED_OBSERVED = "window_expired_observed"
EVENT_WINDOW_ACTION_BLOCKED = "window_action_blocked"

ACTOR_ROLE_OPERATOR = "operator"
ACTOR_ROLE_INCIDENT_COMMANDER = "incident_commander"
_ALLOWED_ACTOR_ROLES = frozenset({ACTOR_ROLE_OPERATOR, ACTOR_ROLE_INCIDENT_COMMANDER})

REASON_MAINTENANCE_WINDOW_COMPLETED = "maintenance_window_completed"
REASON_OPERATOR_CLOSE = "operator_close"
REASON_CUTOVER_DEFERRED = "cutover_deferred"
REASON_CHECKLIST_REVALIDATION_REQUIRED = "checklist_revalidation_required"
REASON_EXECUTION_NOT_STARTED = "execution_not_started"
REASON_POLICY_HOLD = "policy_hold"
REASON_MAINTENANCE_WINDOW_EXPIRED = "maintenance_window_expired"
_CLOSE_REASONS = frozenset(
    {
        REASON_MAINTENANCE_WINDOW_COMPLETED,
        REASON_OPERATOR_CLOSE,
        REASON_CUTOVER_DEFERRED,
        REASON_CHECKLIST_REVALIDATION_REQUIRED,
        REASON_EXECUTION_NOT_STARTED,
        REASON_POLICY_HOLD,
        REASON_MAINTENANCE_WINDOW_EXPIRED,
    }
)

REASON_INCIDENT_DETECTED = "incident_detected"
REASON_POLICY_VIOLATION = "policy_violation"
REASON_RECOVERY_REQUIRED = "recovery_required"
REASON_REPAIR_LOCK_DETECTED = "repair_lock_detected"
REASON_SOURCE_INTEGRITY_WARNING = "source_integrity_warning"
REASON_PRODUCTION_ROOT_RISK = "production_root_risk"
REASON_EXTERNAL_PUBLISH_RISK = "external_publish_risk"
REASON_KILL_SWITCH_TRIGGERED = "kill_switch_triggered"
REASON_OPERATOR_EMERGENCY_CLOSE = "operator_emergency_close"
_EMERGENCY_REASONS = frozenset(
    {
        REASON_INCIDENT_DETECTED,
        REASON_POLICY_VIOLATION,
        REASON_RECOVERY_REQUIRED,
        REASON_REPAIR_LOCK_DETECTED,
        REASON_SOURCE_INTEGRITY_WARNING,
        REASON_PRODUCTION_ROOT_RISK,
        REASON_EXTERNAL_PUBLISH_RISK,
        REASON_KILL_SWITCH_TRIGGERED,
        REASON_OPERATOR_EMERGENCY_CLOSE,
    }
)

RELEASE_CONTROLLED_WINDOW_READY_TO_OPEN = "CONTROLLED_WINDOW_READY_TO_OPEN"
RELEASE_CONTROLLED_WINDOW_OPEN = "CONTROLLED_WINDOW_OPEN"
RELEASE_CONTROLLED_WINDOW_CLOSED = "CONTROLLED_WINDOW_CLOSED"
RELEASE_CONTROLLED_WINDOW_EMERGENCY_CLOSED = "CONTROLLED_WINDOW_EMERGENCY_CLOSED"
RELEASE_CONTROLLED_WINDOW_EXPIRED = "CONTROLLED_WINDOW_EXPIRED"
RELEASE_CONTROLLED_WINDOW_NOT_READY = "CONTROLLED_WINDOW_NOT_READY"

ACTION_OPEN_CONTROLLED_PRODUCTION_WINDOW = "open_controlled_production_window"
ACTION_CONTROLLED_WINDOW_OPEN_WAIT_FOR_PHASE_15C = (
    "controlled_window_open_wait_for_phase_15c"
)
ACTION_CLOSE_CONTROLLED_PRODUCTION_WINDOW = "close_controlled_production_window"
ACTION_CLOSE_EXPIRED_WINDOW = "close_expired_window"
ACTION_REVIEW_EMERGENCY_CLOSE = "review_emergency_close"
ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT = "prepare_new_governed_cutover_contract"
ACTION_RUN_CONSUME_RECOVERY = "run_consume_recovery"
ACTION_RESOLVE_OPERATOR_HANDOFF = "resolve_operator_handoff"
ACTION_RESOLVE_ROLLBACK_READINESS = "resolve_rollback_readiness"
ACTION_RESOLVE_FINAL_SIGNOFF = "resolve_final_signoff"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_PREPARE_PHASE_15C_RUNTIME_PERMISSION = "prepare_phase_15c_runtime_permission"

BLOCK_CUTOVER_CONTRACT_MISSING = "cutover_contract_missing"
BLOCK_CUTOVER_CONTRACT_INVALID = "cutover_contract_invalid"
BLOCK_GOVERNED_CUTOVER_NOT_PREPARED = "governed_cutover_not_prepared"
BLOCK_CHECKLIST_FAILED = "checklist_failed"
BLOCK_OPERATOR_HANDOFF_NOT_READY = "operator_handoff_not_ready"
BLOCK_ROLLBACK_NOT_READY = "rollback_not_ready"
BLOCK_FINAL_SIGNOFF_INVALID = "final_signoff_invalid"
BLOCK_PRODUCTION_RELEASE_NOT_READY = "production_release_not_ready"
BLOCK_RECOVERY_REQUIRED = "recovery_required"
BLOCK_REPAIR_LOCK_HELD = "repair_lock_held"
BLOCK_WINDOW_NOT_STARTED = "window_not_started"
BLOCK_WINDOW_EXPIRED = "window_expired"
BLOCK_WINDOW_NOT_OPEN = "window_not_open"
BLOCK_WINDOW_ALREADY_CLOSED = "window_already_closed"
BLOCK_WINDOW_EMERGENCY_CLOSED = "window_emergency_closed"
BLOCK_REOPEN_NOT_ALLOWED = "reopen_not_allowed"
BLOCK_OPERATOR_IDENTITY_INVALID = "operator_identity_invalid"
BLOCK_ACTOR_ROLE_INVALID = "actor_role_invalid"
BLOCK_REASON_CODE_INVALID = "reason_code_invalid"
BLOCK_CONTRACT_CORRELATION_MISMATCH = "contract_correlation_mismatch"
BLOCK_LIFECYCLE_CORRUPTED = "lifecycle_corrupted"
BLOCK_LIFECYCLE_WRITE_FAILED = "lifecycle_write_failed"
BLOCK_PRODUCTION_EXECUTION_ENABLED = "production_execution_enabled"
BLOCK_CUTOVER_ALREADY_STARTED = "cutover_already_started"
BLOCK_EXECUTION_PERMIT_ALREADY_CREATED = "execution_permit_already_created"
BLOCK_PRODUCTION_ROOT_TOUCHED = "production_root_touched"
BLOCK_EXTERNAL_PUBLISH_ENABLED = "external_publish_enabled"
BLOCK_GATEWAY_PRODUCTION_ENABLED = "gateway_production_enabled"
BLOCK_DISCORD_PRODUCTION_ENABLED = "discord_production_enabled"

WARN_WINDOW_NOT_OPENED = "window_not_opened"
WARN_WINDOW_OPEN_NO_EXECUTION_PERMISSION = "window_open_no_execution_permission"
WARN_EXECUTION_PERMIT_NOT_CREATED = "execution_permit_not_created"
WARN_CUTOVER_NOT_STARTED = "cutover_not_started"
WARN_PRODUCTION_EXECUTION_DISABLED = "production_execution_disabled"
WARN_PRODUCTION_ROOT_HARD_DENIED = "production_root_hard_denied"
WARN_GATEWAY_PRODUCTION_DISABLED = "gateway_production_disabled"
WARN_DISCORD_PRODUCTION_DISABLED = "discord_production_disabled"
WARN_EXTERNAL_PUBLISH_DISABLED = "external_publish_disabled"
WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED = "original_repository2_not_executed"
WARN_MANUAL_CLOSE_REQUIRED = "manual_close_required"
WARN_EXPIRED_WINDOW_REQUIRES_CLOSE = "expired_window_requires_close"
WARN_SECOND_SUPERVISED_RUN_RECOMMENDED = "second_supervised_run_recommended"

_TERMINAL_STATES = frozenset(
    {WINDOW_CLOSED, WINDOW_EMERGENCY_CLOSED}
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
        "operator_id",
        "prepared_by",
        "attestation_hash",
        "rollback_commit",
    }
)


class ProductionControlledWindowError(ValueError):
    """Raised when controlled window lifecycle cannot proceed safely."""


@dataclass(frozen=True)
class ProductionWindowLifecycleEvent:
    event_id: str
    activation_request_id: str
    cutover_contract_id: str
    event_type: str
    from_state: str
    to_state: str
    actor_id: str
    actor_role: str
    reason_code: str
    occurred_at: str
    maintenance_window_start: str
    maintenance_window_end: str
    tested_commit_sha: str
    release_tag: str
    production_execution_allowed: bool = False
    cutover_started: bool = False
    execution_permit_created: bool = False


@dataclass(frozen=True)
class ProductionControlledWindowSummary:
    activation_request_id: str
    cutover_contract_id: str
    window_state: str
    window_open: bool
    window_closed: bool
    emergency_closed: bool
    expired: bool
    contract_present: bool
    contract_valid: bool
    contract_status: str
    governed_cutover_ready: bool
    maintenance_window_start: str
    maintenance_window_end: str
    maintenance_window_duration_seconds: int
    current_time_within_window: bool
    open_event_present: bool
    close_event_present: bool
    emergency_close_event_present: bool
    lifecycle_valid: bool
    operator_identity_valid: bool
    recovery_required: bool
    repair_lock_held: bool
    final_signoff_valid: bool
    rollback_ready: bool
    production_execution_allowed: bool
    production_root_hard_deny: bool
    cutover_started: bool
    execution_permit_created: bool
    original_repository2_execution_attempted: bool
    gateway_production_enabled: bool
    discord_production_enabled: bool
    external_publish_enabled: bool
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str
    event_count: int = 0
    already_open: bool = False
    already_closed: bool = False
    already_emergency_closed: bool = False
    actor_present: bool = False
    actor_role: str = ""
    tested_commit_sha_short: str = ""
    release_tag: str = ""


@dataclass(frozen=True)
class ProductionControlledWindowReleaseSummary:
    activation_request_id: str
    cutover_contract_id: str
    governed_cutover_status: str
    controlled_window_state: str
    window_open: bool
    window_expired: bool
    production_execution_allowed: bool = False
    execution_permit_created: bool = False
    cutover_started: bool = False
    production_root_hard_deny: bool = True
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    external_publish_enabled: bool = False
    next_phase: str = ""
    release_status: str = RELEASE_CONTROLLED_WINDOW_NOT_READY


@dataclass(frozen=True)
class ProductionControlledWindowDashboardDigest:
    controlled_window_state: str
    controlled_window_open: bool
    controlled_window_expired: bool
    controlled_window_contract_id: str
    controlled_window_event_count: int
    controlled_window_blocking_count: int
    controlled_window_warning_count: int
    controlled_window_recommended_action: str


def default_controlled_window_store_dir() -> Path:
    return get_hermes_home() / "coo" / _WINDOW_STORE_DIR


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


def _parse_iso(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _lifecycle_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionControlledWindowError("activation_request_id is required")
    base = (store_dir or default_controlled_window_store_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionControlledWindowError(
            "Controlled window store must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_controlled_window_store_available(*, store_dir: Path | None = None) -> bool:
    try:
        base = (store_dir or default_controlled_window_store_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _event_from_dict(payload: Mapping[str, Any]) -> ProductionWindowLifecycleEvent:
    return ProductionWindowLifecycleEvent(
        event_id=str(payload.get("event_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        cutover_contract_id=str(payload.get("cutover_contract_id", "")),
        event_type=str(payload.get("event_type", "")),
        from_state=str(payload.get("from_state", "")),
        to_state=str(payload.get("to_state", "")),
        actor_id=str(payload.get("actor_id", "")),
        actor_role=str(payload.get("actor_role", "")),
        reason_code=str(payload.get("reason_code", "")),
        occurred_at=str(payload.get("occurred_at", "")),
        maintenance_window_start=str(payload.get("maintenance_window_start", "")),
        maintenance_window_end=str(payload.get("maintenance_window_end", "")),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
        production_execution_allowed=False,
        cutover_started=False,
        execution_permit_created=False,
    )


def _event_to_dict(event: ProductionWindowLifecycleEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "activation_request_id": event.activation_request_id,
        "cutover_contract_id": event.cutover_contract_id,
        "event_type": event.event_type,
        "from_state": event.from_state,
        "to_state": event.to_state,
        "actor_id": event.actor_id,
        "actor_role": event.actor_role,
        "reason_code": event.reason_code,
        "occurred_at": event.occurred_at,
        "maintenance_window_start": event.maintenance_window_start,
        "maintenance_window_end": event.maintenance_window_end,
        "tested_commit_sha": _short_sha(event.tested_commit_sha),
        "release_tag": event.release_tag,
        "production_execution_allowed": False,
        "cutover_started": False,
        "execution_permit_created": False,
    }


def load_window_lifecycle_events(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> tuple[str, tuple[ProductionWindowLifecycleEvent, ...]]:
    """Return (cutover_contract_id, events). Empty when store file missing."""
    path = _lifecycle_path(activation_request_id, store_dir=store_dir)
    if not path.is_file():
        return "", ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionControlledWindowError("lifecycle_corrupted") from exc
    if not isinstance(payload, dict):
        raise ProductionControlledWindowError("lifecycle_corrupted")
    contract_id = str(payload.get("cutover_contract_id", ""))
    raw_events = payload.get("events")
    if raw_events is None:
        raw_events = []
    if not isinstance(raw_events, list):
        raise ProductionControlledWindowError("lifecycle_corrupted")
    events: list[ProductionWindowLifecycleEvent] = []
    seen_ids: set[str] = set()
    for item in raw_events:
        if not isinstance(item, dict):
            raise ProductionControlledWindowError("lifecycle_corrupted")
        event = _event_from_dict(item)
        if not event.event_id:
            raise ProductionControlledWindowError("lifecycle_corrupted")
        if event.event_id in seen_ids:
            raise ProductionControlledWindowError("lifecycle_corrupted")
        seen_ids.add(event.event_id)
        events.append(event)
    return contract_id, tuple(events)


def _derive_lifecycle_state(
    events: tuple[ProductionWindowLifecycleEvent, ...],
) -> str:
    state = WINDOW_NOT_OPENED
    for event in events:
        if event.event_type == EVENT_WINDOW_OPENED:
            state = WINDOW_OPEN
        elif event.event_type == EVENT_WINDOW_CLOSED:
            state = WINDOW_CLOSED
        elif event.event_type == EVENT_WINDOW_EMERGENCY_CLOSED:
            state = WINDOW_EMERGENCY_CLOSED
    return state


def _append_lifecycle_event(
    event: ProductionWindowLifecycleEvent,
    *,
    store_dir: Path | None = None,
) -> None:
    path = _lifecycle_path(event.activation_request_id, store_dir=store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_contract_id = ""
    existing_events: list[ProductionWindowLifecycleEvent] = []
    if path.is_file():
        existing_contract_id, loaded = load_window_lifecycle_events(
            event.activation_request_id,
            store_dir=store_dir,
        )
        existing_events = list(loaded)
        if existing_contract_id and existing_contract_id != event.cutover_contract_id:
            raise ProductionControlledWindowError("contract_correlation_mismatch")
        if any(item.event_id == event.event_id for item in existing_events):
            raise ProductionControlledWindowError("lifecycle_corrupted")
    existing_events.append(event)
    payload = {
        "version": _WINDOW_STORE_VERSION,
        "activation_request_id": event.activation_request_id,
        "cutover_contract_id": event.cutover_contract_id,
        "events": [_event_to_dict(item) for item in existing_events],
    }
    temp = path.with_suffix(".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        raise ProductionControlledWindowError("lifecycle_write_failed") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _recommended_action(summary_bits: Mapping[str, Any]) -> str:
    state = summary_bits["window_state"]
    blocking = summary_bits["blocking_items"]
    if state == WINDOW_OPEN:
        if summary_bits["expired"]:
            return ACTION_CLOSE_EXPIRED_WINDOW
        if summary_bits["recovery_required"] or summary_bits["repair_lock_held"]:
            return ACTION_REVIEW_EMERGENCY_CLOSE
        return ACTION_PREPARE_PHASE_15C_RUNTIME_PERMISSION
    if state == WINDOW_CLOSED:
        return ACTION_MAINTAIN_PRODUCTION_BLOCK
    if state == WINDOW_EMERGENCY_CLOSED:
        return ACTION_REVIEW_EMERGENCY_CLOSE
    if state == WINDOW_EXPIRED or summary_bits["expired"]:
        return ACTION_CLOSE_EXPIRED_WINDOW
    if BLOCK_RECOVERY_REQUIRED in blocking or BLOCK_REPAIR_LOCK_HELD in blocking:
        return ACTION_RUN_CONSUME_RECOVERY
    if BLOCK_CUTOVER_CONTRACT_MISSING in blocking:
        return ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT
    if BLOCK_FINAL_SIGNOFF_INVALID in blocking:
        return ACTION_RESOLVE_FINAL_SIGNOFF
    if BLOCK_ROLLBACK_NOT_READY in blocking:
        return ACTION_RESOLVE_ROLLBACK_READINESS
    if BLOCK_OPERATOR_HANDOFF_NOT_READY in blocking:
        return ACTION_RESOLVE_OPERATOR_HANDOFF
    if BLOCK_GOVERNED_CUTOVER_NOT_PREPARED in blocking:
        return ACTION_PREPARE_NEW_GOVERNED_CUTOVER_CONTRACT
    if summary_bits["contract_valid"] and state == WINDOW_NOT_OPENED:
        if summary_bits["current_time_within_window"]:
            return ACTION_OPEN_CONTROLLED_PRODUCTION_WINDOW
        if summary_bits["expired"]:
            return ACTION_CLOSE_EXPIRED_WINDOW
        return ACTION_CONTROLLED_WINDOW_OPEN_WAIT_FOR_PHASE_15C
    return ACTION_MAINTAIN_PRODUCTION_BLOCK


def evaluate_production_controlled_window(
    *,
    activation_request_id: str,
    operator_id: str = "",
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
    window_store_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    force_production_execution_allowed: bool | None = None,
    force_gateway_enabled: bool | None = None,
    force_discord_enabled: bool | None = None,
    force_cutover_started: bool | None = None,
    force_execution_permit_created: bool | None = None,
) -> ProductionControlledWindowSummary:
    """Read-only controlled window assessment."""
    blocking: list[str] = []
    warnings: list[str] = []
    current = _utc_now(now)

    contract = load_governed_cutover_contract(
        activation_request_id,
        store_dir=governed_cutover_store_dir,
    )
    lifecycle_contract_id, events = load_window_lifecycle_events(
        activation_request_id,
        store_dir=window_store_dir,
    )
    lifecycle_valid = True
    if lifecycle_contract_id and contract is not None:
        if lifecycle_contract_id != contract.cutover_contract_id:
            blocking.append(BLOCK_CONTRACT_CORRELATION_MISMATCH)
            lifecycle_valid = False
    elif lifecycle_contract_id and contract is None:
        blocking.append(BLOCK_CONTRACT_CORRELATION_MISMATCH)
        lifecycle_valid = False

    contract_present = contract is not None
    contract_valid = False
    contract_status = ""
    cutover_contract_id = ""
    window_start = ""
    window_end = ""
    window_duration = 0
    governed_ready = False
    checklist_passed = False
    handoff_ready = False
    rollback_ready = False
    reservation_id = ""

    if contract is None:
        blocking.append(BLOCK_CUTOVER_CONTRACT_MISSING)
    else:
        cutover_contract_id = contract.cutover_contract_id
        contract_status = contract.contract_status
        window_start = contract.maintenance_window_start
        window_end = contract.maintenance_window_end
        window_duration = int(contract.maintenance_window_duration_seconds or 0)
        checklist_passed = bool(contract.checklist_passed)
        handoff_ready = bool(contract.operator_handoff_ready)
        rollback_ready = bool(contract.rollback_ready)
        reservation_id = contract.reservation_id
        if contract.contract_status != CONTRACT_STATUS_PREPARED:
            blocking.append(BLOCK_GOVERNED_CUTOVER_NOT_PREPARED)
        if not checklist_passed:
            blocking.append(BLOCK_CHECKLIST_FAILED)
        if not handoff_ready:
            blocking.append(BLOCK_OPERATOR_HANDOFF_NOT_READY)
        if not rollback_ready:
            blocking.append(BLOCK_ROLLBACK_NOT_READY)
        if contract.window_opened or contract.cutover_started or contract.execution_permit_created:
            blocking.append(BLOCK_CUTOVER_CONTRACT_INVALID)
        if contract.production_execution_allowed:
            blocking.append(BLOCK_PRODUCTION_EXECUTION_ENABLED)
        if contract.gateway_production_enabled:
            blocking.append(BLOCK_GATEWAY_PRODUCTION_ENABLED)
        if contract.discord_production_enabled:
            blocking.append(BLOCK_DISCORD_PRODUCTION_ENABLED)
        if contract.external_publish_enabled:
            blocking.append(BLOCK_EXTERNAL_PUBLISH_ENABLED)
        contract_valid = (
            contract.contract_status == CONTRACT_STATUS_PREPARED
            and checklist_passed
            and handoff_ready
            and rollback_ready
            and not contract.window_opened
            and not contract.cutover_started
            and not contract.execution_permit_created
            and BLOCK_CUTOVER_CONTRACT_INVALID not in blocking
        )
        governed_ready = contract_valid

    final_record = load_final_signoff_record(
        activation_request_id,
        store_dir=final_signoff_store_dir,
    )
    final_signoff_valid = False
    if final_record is None:
        blocking.append(BLOCK_FINAL_SIGNOFF_INVALID)
    elif final_record.final_signoff_status not in {
        PRODUCTION_FINAL_SIGNOFF_READY,
        PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
    }:
        blocking.append(BLOCK_FINAL_SIGNOFF_INVALID)
    elif not final_record.production_release_ready:
        blocking.append(BLOCK_PRODUCTION_RELEASE_NOT_READY)
    elif contract is not None and (
        final_record.reservation_id != contract.reservation_id
        or final_record.execution_attempt_id != contract.execution_attempt_id
        or final_record.dispatch_run_id != contract.dispatch_run_id
        or final_record.final_signoff_id != contract.final_signoff_id
    ):
        blocking.append(BLOCK_CONTRACT_CORRELATION_MISMATCH)
    else:
        final_signoff_valid = True

    recovery_required = False
    repair_lock_held = False
    if contract is not None and reservation_id:
        try:
            cutover_summary = evaluate_production_governed_cutover(
                activation_request_id=activation_request_id,
                reservation_id=reservation_id,
                operator_id=contract.prepared_by,
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
                governed_cutover_store_dir=governed_cutover_store_dir
                or default_governed_cutover_store_dir(),
                repo_root=repo_root,
                merged_config=merged_config,
                now=current,
            )
            recovery_required = bool(cutover_summary.recovery_required)
            repair_lock_held = bool(cutover_summary.repair_lock_held)
            if cutover_summary.governed_cutover_status != GOVERNED_CUTOVER_CONTRACT_PREPARED:
                # Contract prepared but live chain may have degraded.
                if recovery_required or repair_lock_held:
                    pass
                elif not cutover_summary.source_tree_unchanged:
                    blocking.append(BLOCK_PRODUCTION_ROOT_TOUCHED)
                elif cutover_summary.external_publish_enabled:
                    blocking.append(BLOCK_EXTERNAL_PUBLISH_ENABLED)
        except Exception:
            blocking.append(BLOCK_LIFECYCLE_CORRUPTED)
            lifecycle_valid = False

    if recovery_required:
        blocking.append(BLOCK_RECOVERY_REQUIRED)
    if repair_lock_held:
        blocking.append(BLOCK_REPAIR_LOCK_HELD)

    start_dt = _parse_iso(window_start)
    end_dt = _parse_iso(window_end)
    within_window = False
    expired = False
    not_started = False
    lifecycle_state = _derive_lifecycle_state(events)
    if start_dt is not None and end_dt is not None and end_dt > start_dt:
        within_window = start_dt <= current < end_dt
        expired = current >= end_dt
        not_started = current < start_dt
        if not_started and lifecycle_state == WINDOW_NOT_OPENED:
            blocking.append(BLOCK_WINDOW_NOT_STARTED)
        if expired and lifecycle_state in {WINDOW_NOT_OPENED, WINDOW_OPEN}:
            blocking.append(BLOCK_WINDOW_EXPIRED)
    elif contract is not None:
        blocking.append(BLOCK_CUTOVER_CONTRACT_INVALID)
        contract_valid = False

    open_present = any(e.event_type == EVENT_WINDOW_OPENED for e in events)
    close_present = any(e.event_type == EVENT_WINDOW_CLOSED for e in events)
    emergency_present = any(
        e.event_type == EVENT_WINDOW_EMERGENCY_CLOSED for e in events
    )

    display_state = lifecycle_state
    if lifecycle_state == WINDOW_NOT_OPENED and expired:
        display_state = WINDOW_EXPIRED
    elif lifecycle_state == WINDOW_OPEN and expired:
        display_state = WINDOW_EXPIRED
    elif (
        recovery_required or repair_lock_held
    ) and lifecycle_state == WINDOW_OPEN and not expired:
        display_state = WINDOW_BLOCKED

    production_execution_allowed = bool(force_production_execution_allowed)
    gateway_enabled = bool(force_gateway_enabled)
    discord_enabled = bool(force_discord_enabled)
    cutover_started = bool(force_cutover_started)
    execution_permit_created = bool(force_execution_permit_created)
    if production_execution_allowed:
        blocking.append(BLOCK_PRODUCTION_EXECUTION_ENABLED)
    if gateway_enabled:
        blocking.append(BLOCK_GATEWAY_PRODUCTION_ENABLED)
    if discord_enabled:
        blocking.append(BLOCK_DISCORD_PRODUCTION_ENABLED)
    if cutover_started:
        blocking.append(BLOCK_CUTOVER_ALREADY_STARTED)
    if execution_permit_created:
        blocking.append(BLOCK_EXECUTION_PERMIT_ALREADY_CREATED)

    operator = (operator_id or "").strip()
    operator_identity_valid = True
    if operator:
        try:
            request = load_activation_request(activation_request_id, store_dir=store_dir)
            conflicts = {
                (request.executor_id or "").strip(),
                (request.requested_by or "").strip(),
            }
            if final_record is not None:
                conflicts.add((final_record.signed_by or "").strip())
            if operator in conflicts:
                operator_identity_valid = False
                blocking.append(BLOCK_OPERATOR_IDENTITY_INVALID)
        except Exception:
            operator_identity_valid = False
            blocking.append(BLOCK_OPERATOR_IDENTITY_INVALID)

    unique_blocking = tuple(dict.fromkeys(blocking))
    warnings.extend(
        [
            WARN_EXECUTION_PERMIT_NOT_CREATED,
            WARN_CUTOVER_NOT_STARTED,
            WARN_PRODUCTION_EXECUTION_DISABLED,
            WARN_PRODUCTION_ROOT_HARD_DENIED,
            WARN_GATEWAY_PRODUCTION_DISABLED,
            WARN_DISCORD_PRODUCTION_DISABLED,
            WARN_EXTERNAL_PUBLISH_DISABLED,
            WARN_ORIGINAL_REPOSITORY2_NOT_EXECUTED,
            WARN_WINDOW_OPEN_NO_EXECUTION_PERMISSION,
        ]
    )
    if display_state == WINDOW_NOT_OPENED:
        warnings.append(WARN_WINDOW_NOT_OPENED)
    if display_state == WINDOW_EXPIRED or (lifecycle_state == WINDOW_OPEN and expired):
        warnings.append(WARN_EXPIRED_WINDOW_REQUIRES_CLOSE)
        warnings.append(WARN_MANUAL_CLOSE_REQUIRED)
    unique_warnings = tuple(dict.fromkeys(warnings))

    recommended = _recommended_action(
        {
            "window_state": display_state,
            "blocking_items": unique_blocking,
            "expired": expired,
            "recovery_required": recovery_required,
            "repair_lock_held": repair_lock_held,
            "contract_valid": contract_valid,
            "current_time_within_window": within_window,
        }
    )

    tested_short = _short_sha(contract.tested_commit_sha) if contract else ""
    release_tag = contract.release_tag if contract else ""

    return ProductionControlledWindowSummary(
        activation_request_id=activation_request_id,
        cutover_contract_id=cutover_contract_id,
        window_state=display_state,
        window_open=lifecycle_state == WINDOW_OPEN and not close_present and not emergency_present,
        window_closed=lifecycle_state == WINDOW_CLOSED,
        emergency_closed=lifecycle_state == WINDOW_EMERGENCY_CLOSED,
        expired=expired,
        contract_present=contract_present,
        contract_valid=contract_valid,
        contract_status=contract_status,
        governed_cutover_ready=governed_ready,
        maintenance_window_start=window_start,
        maintenance_window_end=window_end,
        maintenance_window_duration_seconds=window_duration,
        current_time_within_window=within_window,
        open_event_present=open_present,
        close_event_present=close_present,
        emergency_close_event_present=emergency_present,
        lifecycle_valid=lifecycle_valid,
        operator_identity_valid=operator_identity_valid if operator else True,
        recovery_required=recovery_required,
        repair_lock_held=repair_lock_held,
        final_signoff_valid=final_signoff_valid,
        rollback_ready=rollback_ready,
        production_execution_allowed=False,
        production_root_hard_deny=True,
        cutover_started=False,
        execution_permit_created=False,
        original_repository2_execution_attempted=False,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        external_publish_enabled=False,
        blocking_items=unique_blocking,
        warning_items=unique_warnings,
        recommended_action=recommended,
        event_count=len(events),
        already_open=lifecycle_state == WINDOW_OPEN,
        already_closed=lifecycle_state == WINDOW_CLOSED,
        already_emergency_closed=lifecycle_state == WINDOW_EMERGENCY_CLOSED,
        actor_present=bool(operator),
        actor_role="",
        tested_commit_sha_short=tested_short,
        release_tag=release_tag,
    )


def open_production_controlled_window(
    *,
    activation_request_id: str,
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
    validation_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    preflight_history_dir: Path | None = None,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionControlledWindowSummary:
    """Open controlled window via append-only lifecycle event."""
    if not probe_controlled_window_store_available(store_dir=window_store_dir):
        raise ProductionControlledWindowError("lifecycle_write_failed")

    operator = (operator_id or "").strip()
    if not operator:
        raise ProductionControlledWindowError("operator_identity_invalid")

    summary = evaluate_production_controlled_window(
        activation_request_id=activation_request_id,
        operator_id=operator,
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
        window_store_dir=window_store_dir,
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )

    lifecycle_state = _derive_lifecycle_state(
        load_window_lifecycle_events(
            activation_request_id, store_dir=window_store_dir
        )[1]
    )
    if lifecycle_state == WINDOW_OPEN:
        return summary
    if lifecycle_state in _TERMINAL_STATES:
        raise ProductionControlledWindowError("reopen_not_allowed")
    if summary.expired or summary.window_state == WINDOW_EXPIRED:
        raise ProductionControlledWindowError("window_expired")
    if not summary.current_time_within_window:
        start_dt = _parse_iso(summary.maintenance_window_start)
        if start_dt is not None and _utc_now(now) < start_dt:
            raise ProductionControlledWindowError("window_not_started")
        raise ProductionControlledWindowError("window_expired")
    if not summary.contract_valid or not summary.final_signoff_valid:
        raise ProductionControlledWindowError("cutover_contract_invalid")
    if summary.recovery_required or summary.repair_lock_held:
        raise ProductionControlledWindowError("recovery_required")
    if not summary.operator_identity_valid:
        raise ProductionControlledWindowError("operator_identity_invalid")
    if BLOCK_CONTRACT_CORRELATION_MISMATCH in summary.blocking_items:
        raise ProductionControlledWindowError("contract_correlation_mismatch")

    contract = load_governed_cutover_contract(
        activation_request_id,
        store_dir=governed_cutover_store_dir,
    )
    if contract is None:
        raise ProductionControlledWindowError("cutover_contract_missing")

    # Re-check lifecycle atomically before write.
    _, events = load_window_lifecycle_events(
        activation_request_id, store_dir=window_store_dir
    )
    if _derive_lifecycle_state(events) == WINDOW_OPEN:
        return evaluate_production_controlled_window(
            activation_request_id=activation_request_id,
            operator_id=operator,
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
            window_store_dir=window_store_dir,
            repo_root=repo_root,
            merged_config=merged_config,
            now=now,
        )
    if _derive_lifecycle_state(events) in _TERMINAL_STATES:
        raise ProductionControlledWindowError("reopen_not_allowed")

    occurred = _utc_now_iso(now)
    requested = ProductionWindowLifecycleEvent(
        event_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        cutover_contract_id=contract.cutover_contract_id,
        event_type=EVENT_WINDOW_OPEN_REQUESTED,
        from_state=WINDOW_NOT_OPENED,
        to_state=WINDOW_NOT_OPENED,
        actor_id=operator,
        actor_role=ACTOR_ROLE_OPERATOR,
        reason_code="",
        occurred_at=occurred,
        maintenance_window_start=contract.maintenance_window_start,
        maintenance_window_end=contract.maintenance_window_end,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
    )
    opened = ProductionWindowLifecycleEvent(
        event_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        cutover_contract_id=contract.cutover_contract_id,
        event_type=EVENT_WINDOW_OPENED,
        from_state=WINDOW_NOT_OPENED,
        to_state=WINDOW_OPEN,
        actor_id=operator,
        actor_role=ACTOR_ROLE_OPERATOR,
        reason_code="",
        occurred_at=occurred,
        maintenance_window_start=contract.maintenance_window_start,
        maintenance_window_end=contract.maintenance_window_end,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
    )
    _append_lifecycle_event(requested, store_dir=window_store_dir)
    _append_lifecycle_event(opened, store_dir=window_store_dir)
    return evaluate_production_controlled_window(
        activation_request_id=activation_request_id,
        operator_id=operator,
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
        window_store_dir=window_store_dir,
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )


def close_production_controlled_window(
    *,
    activation_request_id: str,
    operator_id: str,
    reason_code: str,
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
    window_store_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionControlledWindowSummary:
    """Normal close of an open controlled window."""
    operator = (operator_id or "").strip()
    if not operator:
        raise ProductionControlledWindowError("operator_identity_invalid")
    reason = (reason_code or "").strip()
    if reason not in _CLOSE_REASONS:
        raise ProductionControlledWindowError("reason_code_invalid")

    _, events = load_window_lifecycle_events(
        activation_request_id, store_dir=window_store_dir
    )
    lifecycle_state = _derive_lifecycle_state(events)
    if lifecycle_state == WINDOW_CLOSED:
        return evaluate_production_controlled_window(
            activation_request_id=activation_request_id,
            operator_id=operator,
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
            window_store_dir=window_store_dir,
            repo_root=repo_root,
            merged_config=merged_config,
            now=now,
        )
    if lifecycle_state == WINDOW_EMERGENCY_CLOSED:
        raise ProductionControlledWindowError("window_emergency_closed")
    if lifecycle_state != WINDOW_OPEN:
        raise ProductionControlledWindowError("window_not_open")

    contract = load_governed_cutover_contract(
        activation_request_id,
        store_dir=governed_cutover_store_dir,
    )
    if contract is None:
        raise ProductionControlledWindowError("cutover_contract_missing")

    occurred = _utc_now_iso(now)
    requested = ProductionWindowLifecycleEvent(
        event_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        cutover_contract_id=contract.cutover_contract_id,
        event_type=EVENT_WINDOW_CLOSE_REQUESTED,
        from_state=WINDOW_OPEN,
        to_state=WINDOW_OPEN,
        actor_id=operator,
        actor_role=ACTOR_ROLE_OPERATOR,
        reason_code=reason,
        occurred_at=occurred,
        maintenance_window_start=contract.maintenance_window_start,
        maintenance_window_end=contract.maintenance_window_end,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
    )
    closed = ProductionWindowLifecycleEvent(
        event_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        cutover_contract_id=contract.cutover_contract_id,
        event_type=EVENT_WINDOW_CLOSED,
        from_state=WINDOW_OPEN,
        to_state=WINDOW_CLOSED,
        actor_id=operator,
        actor_role=ACTOR_ROLE_OPERATOR,
        reason_code=reason,
        occurred_at=occurred,
        maintenance_window_start=contract.maintenance_window_start,
        maintenance_window_end=contract.maintenance_window_end,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
    )
    _append_lifecycle_event(requested, store_dir=window_store_dir)
    _append_lifecycle_event(closed, store_dir=window_store_dir)
    return evaluate_production_controlled_window(
        activation_request_id=activation_request_id,
        operator_id=operator,
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
        window_store_dir=window_store_dir,
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )


def emergency_close_production_controlled_window(
    *,
    activation_request_id: str,
    operator_id: str,
    actor_role: str,
    reason_code: str,
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
    window_store_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionControlledWindowSummary:
    """Emergency close of an open controlled window."""
    operator = (operator_id or "").strip()
    if not operator:
        raise ProductionControlledWindowError("operator_identity_invalid")
    role = (actor_role or "").strip()
    if role not in _ALLOWED_ACTOR_ROLES:
        raise ProductionControlledWindowError("actor_role_invalid")
    reason = (reason_code or "").strip()
    if reason not in _EMERGENCY_REASONS:
        raise ProductionControlledWindowError("reason_code_invalid")

    _, events = load_window_lifecycle_events(
        activation_request_id, store_dir=window_store_dir
    )
    lifecycle_state = _derive_lifecycle_state(events)
    if lifecycle_state == WINDOW_EMERGENCY_CLOSED:
        return evaluate_production_controlled_window(
            activation_request_id=activation_request_id,
            operator_id=operator,
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
            window_store_dir=window_store_dir,
            repo_root=repo_root,
            merged_config=merged_config,
            now=now,
        )
    if lifecycle_state == WINDOW_CLOSED:
        raise ProductionControlledWindowError("window_already_closed")
    if lifecycle_state != WINDOW_OPEN:
        raise ProductionControlledWindowError("window_not_open")

    contract = load_governed_cutover_contract(
        activation_request_id,
        store_dir=governed_cutover_store_dir,
    )
    if contract is None:
        raise ProductionControlledWindowError("cutover_contract_missing")

    occurred = _utc_now_iso(now)
    requested = ProductionWindowLifecycleEvent(
        event_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        cutover_contract_id=contract.cutover_contract_id,
        event_type=EVENT_EMERGENCY_CLOSE_REQUESTED,
        from_state=WINDOW_OPEN,
        to_state=WINDOW_OPEN,
        actor_id=operator,
        actor_role=role,
        reason_code=reason,
        occurred_at=occurred,
        maintenance_window_start=contract.maintenance_window_start,
        maintenance_window_end=contract.maintenance_window_end,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
    )
    closed = ProductionWindowLifecycleEvent(
        event_id=str(uuid.uuid4()),
        activation_request_id=activation_request_id,
        cutover_contract_id=contract.cutover_contract_id,
        event_type=EVENT_WINDOW_EMERGENCY_CLOSED,
        from_state=WINDOW_OPEN,
        to_state=WINDOW_EMERGENCY_CLOSED,
        actor_id=operator,
        actor_role=role,
        reason_code=reason,
        occurred_at=occurred,
        maintenance_window_start=contract.maintenance_window_start,
        maintenance_window_end=contract.maintenance_window_end,
        tested_commit_sha=contract.tested_commit_sha,
        release_tag=contract.release_tag,
    )
    _append_lifecycle_event(requested, store_dir=window_store_dir)
    _append_lifecycle_event(closed, store_dir=window_store_dir)
    result = evaluate_production_controlled_window(
        activation_request_id=activation_request_id,
        operator_id=operator,
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
        window_store_dir=window_store_dir,
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )
    return result


def build_production_controlled_window_release_summary(
    summary: ProductionControlledWindowSummary,
    *,
    governed_cutover_status: str = "",
) -> ProductionControlledWindowReleaseSummary:
    if summary.window_state == WINDOW_OPEN and not summary.expired:
        release_status = RELEASE_CONTROLLED_WINDOW_OPEN
        next_phase = _NEXT_PHASE_15C
    elif summary.window_state == WINDOW_CLOSED:
        release_status = RELEASE_CONTROLLED_WINDOW_CLOSED
        next_phase = ""
    elif summary.window_state == WINDOW_EMERGENCY_CLOSED:
        release_status = RELEASE_CONTROLLED_WINDOW_EMERGENCY_CLOSED
        next_phase = ""
    elif summary.window_state == WINDOW_EXPIRED or summary.expired:
        release_status = RELEASE_CONTROLLED_WINDOW_EXPIRED
        next_phase = ""
    elif summary.contract_valid and summary.final_signoff_valid:
        release_status = RELEASE_CONTROLLED_WINDOW_READY_TO_OPEN
        next_phase = ""
    else:
        release_status = RELEASE_CONTROLLED_WINDOW_NOT_READY
        next_phase = ""

    return ProductionControlledWindowReleaseSummary(
        activation_request_id=summary.activation_request_id,
        cutover_contract_id=summary.cutover_contract_id,
        governed_cutover_status=governed_cutover_status,
        controlled_window_state=summary.window_state,
        window_open=summary.window_open,
        window_expired=summary.expired,
        production_execution_allowed=False,
        execution_permit_created=False,
        cutover_started=False,
        production_root_hard_deny=True,
        gateway_production_enabled=False,
        discord_production_enabled=False,
        external_publish_enabled=False,
        next_phase=next_phase,
        release_status=release_status,
    )


def resolve_latest_controlled_window_dashboard_digest(
    *,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> ProductionControlledWindowDashboardDigest:
    base = (governed_cutover_store_dir or default_governed_cutover_store_dir()).resolve()
    if not base.is_dir():
        return ProductionControlledWindowDashboardDigest(
            controlled_window_state="not_configured",
            controlled_window_open=False,
            controlled_window_expired=False,
            controlled_window_contract_id="",
            controlled_window_event_count=0,
            controlled_window_blocking_count=0,
            controlled_window_warning_count=0,
            controlled_window_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )
    paths = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:500]:
        activation_id = path.stem
        try:
            summary = evaluate_production_controlled_window(
                activation_request_id=activation_id,
                governed_cutover_store_dir=governed_cutover_store_dir,
                window_store_dir=window_store_dir,
                final_signoff_store_dir=final_signoff_store_dir,
                store_dir=store_dir,
                reservation_dir=reservation_dir,
                merged_config=merged_config,
                **{
                    k: v
                    for k, v in kwargs.items()
                    if k
                    in {
                        "runtime_history_dir",
                        "evidence_dir",
                        "audit_dir",
                        "bundle_dir",
                        "confirmation_dir",
                        "transaction_dir",
                        "e2e_history_dir",
                        "signoff_store_dir",
                        "validation_store_dir",
                        "preflight_history_dir",
                        "repo_root",
                    }
                },
            )
        except ProductionControlledWindowError:
            continue
        if not summary.contract_present:
            continue
        return ProductionControlledWindowDashboardDigest(
            controlled_window_state=summary.window_state,
            controlled_window_open=summary.window_open,
            controlled_window_expired=summary.expired,
            controlled_window_contract_id=summary.cutover_contract_id,
            controlled_window_event_count=summary.event_count,
            controlled_window_blocking_count=len(summary.blocking_items),
            controlled_window_warning_count=len(summary.warning_items),
            controlled_window_recommended_action=summary.recommended_action,
        )
    return ProductionControlledWindowDashboardDigest(
        controlled_window_state="not_configured",
        controlled_window_open=False,
        controlled_window_expired=False,
        controlled_window_contract_id="",
        controlled_window_event_count=0,
        controlled_window_blocking_count=0,
        controlled_window_warning_count=0,
        controlled_window_recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
    )


def _assert_safe_output(output: str) -> None:
    sanitized = output
    for allowed in (
        "production_execution_allowed: false",
        "cutover_started: false",
        "execution_permit_created: false",
        "production_root_hard_deny: true",
        "original_repository2_execution_attempted: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
        "external_publish_enabled: false",
        "original_repository2_not_executed",
        "actor_present: true",
        "actor_present: false",
        "actor_role: operator",
        "actor_role: incident_commander",
        "actor_role: (none)",
    ):
        sanitized = sanitized.replace(allowed, "")
    lowered = sanitized.lower()
    # Avoid false positives on allowed warning/field labels.
    for label in (
        "operator_identity_valid",
        "actor_present",
        "actor_role",
        "original_repository2_not_executed",
        "original_repository2_execution_attempted",
    ):
        lowered = lowered.replace(label, "")
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionControlledWindowError(
                f"Unsafe controlled window output field: {token!r}"
            )


def format_production_controlled_window_status(
    summary: ProductionControlledWindowSummary,
) -> str:
    lines = [
        "Production Controlled Window Status",
        "",
        f"activation_request_id: {summary.activation_request_id}",
        f"cutover_contract_id: {summary.cutover_contract_id or '(none)'}",
        f"window_state: {summary.window_state}",
        f"window_open: {str(summary.window_open).lower()}",
        f"window_closed: {str(summary.window_closed).lower()}",
        f"emergency_closed: {str(summary.emergency_closed).lower()}",
        f"expired: {str(summary.expired).lower()}",
        f"contract_present: {str(summary.contract_present).lower()}",
        f"contract_valid: {str(summary.contract_valid).lower()}",
        f"contract_status: {summary.contract_status or '(none)'}",
        f"governed_cutover_ready: {str(summary.governed_cutover_ready).lower()}",
        f"maintenance_window_start: {summary.maintenance_window_start or '(none)'}",
        f"maintenance_window_end: {summary.maintenance_window_end or '(none)'}",
        "maintenance_window_duration_seconds: "
        f"{summary.maintenance_window_duration_seconds}",
        "current_time_within_window: "
        f"{str(summary.current_time_within_window).lower()}",
        f"open_event_present: {str(summary.open_event_present).lower()}",
        f"close_event_present: {str(summary.close_event_present).lower()}",
        "emergency_close_event_present: "
        f"{str(summary.emergency_close_event_present).lower()}",
        f"lifecycle_valid: {str(summary.lifecycle_valid).lower()}",
        f"operator_identity_valid: {str(summary.operator_identity_valid).lower()}",
        f"recovery_required: {str(summary.recovery_required).lower()}",
        f"repair_lock_held: {str(summary.repair_lock_held).lower()}",
        f"final_signoff_valid: {str(summary.final_signoff_valid).lower()}",
        f"rollback_ready: {str(summary.rollback_ready).lower()}",
        f"event_count: {summary.event_count}",
        f"blocking_items_count: {len(summary.blocking_items)}",
        f"warning_items_count: {len(summary.warning_items)}",
        "blocking_items: "
        f"{', '.join(summary.blocking_items) if summary.blocking_items else '(none)'}",
        "warning_items: "
        f"{', '.join(summary.warning_items) if summary.warning_items else '(none)'}",
        f"recommended_action: {summary.recommended_action}",
        f"actor_present: {str(summary.actor_present).lower()}",
        f"actor_role: {summary.actor_role or '(none)'}",
        f"tested_commit_sha: {summary.tested_commit_sha_short or '(none)'}",
        f"release_tag: {summary.release_tag or '(none)'}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
        "production_root_hard_deny: true",
        "cutover_started: false",
        "execution_permit_created: false",
        "original_repository2_execution_attempted: false",
        "gateway_production_enabled: false",
        "discord_production_enabled: false",
        "external_publish_enabled: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def format_production_controlled_window_history(
    activation_request_id: str,
    events: tuple[ProductionWindowLifecycleEvent, ...],
    *,
    cutover_contract_id: str = "",
) -> str:
    lines = [
        "Production Controlled Window History",
        "",
        f"activation_request_id: {activation_request_id}",
        f"cutover_contract_id: {cutover_contract_id or '(none)'}",
        f"event_count: {len(events)}",
        "",
    ]
    for index, event in enumerate(events, start=1):
        lines.extend(
            [
                f"event_{index}_id: {event.event_id}",
                f"event_{index}_type: {event.event_type}",
                f"event_{index}_from_state: {event.from_state}",
                f"event_{index}_to_state: {event.to_state}",
                f"event_{index}_actor_role: {event.actor_role or '(none)'}",
                f"event_{index}_reason_code: {event.reason_code or '(none)'}",
                f"event_{index}_occurred_at: {event.occurred_at}",
                f"event_{index}_actor_present: true",
                "",
            ]
        )
    lines.extend(
        [
            "[Safety]",
            "production_execution_allowed: false",
            "cutover_started: false",
            "execution_permit_created: false",
            "production_root_hard_deny: true",
        ]
    )
    output = "\n".join(lines).rstrip()
    _assert_safe_output(output)
    return output


def run_production_controlled_window_status(
    *,
    activation_request_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = evaluate_production_controlled_window(
            activation_request_id=activation_request_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionControlledWindowError:
        return "error: controlled window status unavailable", 1
    return format_production_controlled_window_status(summary), 0


def run_production_controlled_window_history(
    *,
    activation_request_id: str,
) -> tuple[str, int]:
    try:
        contract_id, events = load_window_lifecycle_events(activation_request_id)
    except ProductionControlledWindowError:
        return "error: controlled window history unavailable", 1
    return (
        format_production_controlled_window_history(
            activation_request_id,
            events,
            cutover_contract_id=contract_id,
        ),
        0,
    )


def run_production_controlled_window_open(
    *,
    activation_request_id: str,
    operator_id: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = open_production_controlled_window(
            activation_request_id=activation_request_id,
            operator_id=operator_id,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionControlledWindowError:
        try:
            summary = evaluate_production_controlled_window(
                activation_request_id=activation_request_id,
                operator_id=operator_id,
                merged_config=merged_config,
                repo_root=repo_root,
            )
            return format_production_controlled_window_status(summary), 1
        except ProductionControlledWindowError:
            return "error: controlled window open failed", 1
    exit_code = 0 if summary.window_open or summary.already_open else 1
    return format_production_controlled_window_status(summary), exit_code


def run_production_controlled_window_close(
    *,
    activation_request_id: str,
    operator_id: str,
    reason_code: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = close_production_controlled_window(
            activation_request_id=activation_request_id,
            operator_id=operator_id,
            reason_code=reason_code,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionControlledWindowError:
        try:
            summary = evaluate_production_controlled_window(
                activation_request_id=activation_request_id,
                operator_id=operator_id,
                merged_config=merged_config,
                repo_root=repo_root,
            )
            return format_production_controlled_window_status(summary), 1
        except ProductionControlledWindowError:
            return "error: controlled window close failed", 1
    exit_code = 0 if summary.window_closed or summary.already_closed else 1
    return format_production_controlled_window_status(summary), exit_code


def run_production_controlled_window_emergency_close(
    *,
    activation_request_id: str,
    operator_id: str,
    actor_role: str,
    reason_code: str,
    merged_config: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, int]:
    try:
        summary = emergency_close_production_controlled_window(
            activation_request_id=activation_request_id,
            operator_id=operator_id,
            actor_role=actor_role,
            reason_code=reason_code,
            merged_config=merged_config,
            repo_root=repo_root,
        )
    except ProductionControlledWindowError:
        try:
            summary = evaluate_production_controlled_window(
                activation_request_id=activation_request_id,
                operator_id=operator_id,
                merged_config=merged_config,
                repo_root=repo_root,
            )
            return format_production_controlled_window_status(summary), 1
        except ProductionControlledWindowError:
            return "error: controlled window emergency close failed", 1
    exit_code = (
        0 if summary.emergency_closed or summary.already_emergency_closed else 1
    )
    return format_production_controlled_window_status(summary), exit_code
