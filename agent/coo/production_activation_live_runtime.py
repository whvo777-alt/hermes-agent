"""Production activation isolated mirror bounded runtime — Phase 14H-3C-2.

Bounded subprocess execution inside ephemeral permit context only.
No consume, evidence, or original Repository2 production root execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agent.coo.bounded_subprocess_runner import (
    RUNNER_PROFILE_DISPATCH,
    BoundedSubprocessRunnerError,
    create_bounded_subprocess_runner,
)
from agent.coo.dispatch_executor_config import load_dispatch_executor_policy
from agent.coo.dispatch_pipeline_root_trust import (
    assert_pipeline_root_allowed,
    resolve_pipeline_root,
)
from agent.coo.production_activation_dry_run import _probe_publish_intent
from agent.coo.production_activation_execution_permit import (
    ActivationExecutionPermitError,
    require_active_execution_permit,
)
from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_COMPLETED,
    RESERVATION_STATE_FAILED,
    RESERVATION_STATE_RESERVED,
    RESERVATION_STATE_STARTED,
    ProductionActivationExecutionReservation,
    ProductionActivationExecutionReservationError,
    transition_execution_reservation_to_completed,
    transition_execution_reservation_to_failed,
    transition_execution_reservation_to_started,
)
from agent.coo.production_activation_kill_switch import (
    REASON_RUNTIME_COMPLETED_WAITING_E2E,
    REASON_RUNTIME_EXCEPTION,
    REASON_RUNTIME_NONZERO,
    REASON_RUNTIME_PUBLISH_ATTEMPT,
    REASON_RUNTIME_SOURCE_MUTATION,
    REASON_RUNTIME_TIMEOUT,
    is_kill_switch_available,
    suspend_production_activation,
)
from agent.coo.production_activation_live_harness import (
    ProductionActivationLiveHarnessPlan,
    ProductionActivationLiveHarnessRequest,
    ProductionLiveRuntimeInvoker,
    _DEFAULT_TIMEOUT_SECONDS,
    _ALLOWED_ENV_KEYS,
    build_live_harness_request,
    evaluate_live_harness_plan,
)
from agent.coo.production_activation_state import (
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_SUSPENDED,
    ActivationRequest,
    ROLE_OPERATOR,
)
from agent.coo.production_executor_factory import _TIMEOUT_EXIT_CODE
from agent.coo.dispatch_cli_repository_attestation import (
    REQUIRED_DIRECTORIES,
    REQUIRED_ENTRYPOINT,
    REQUIRED_MANIFEST,
)
from hermes_constants import get_hermes_home

FAIL_ISOLATED_RUNTIME_NOT_ENABLED = "isolated_runtime_not_enabled"
FAIL_PERMIT_NOT_ACTIVE = "permit_not_active"
FAIL_RESERVATION_NOT_RESERVED = "reservation_not_reserved"
FAIL_RESERVATION_START_FAILED = "reservation_start_failed"
FAIL_RUNNER_FACTORY_FAILED = "runner_factory_failed"
FAIL_RUNNER_INVOCATION_FAILED = "runner_invocation_failed"
FAIL_RUNTIME_NONZERO = "runtime_nonzero"
FAIL_RUNTIME_TIMEOUT = "runtime_timeout"
FAIL_RUNTIME_EXCEPTION = "runtime_exception"
FAIL_RUNTIME_SOURCE_MUTATION = "runtime_source_mutation"
FAIL_RUNTIME_PUBLISH_ATTEMPT = "runtime_publish_attempt_detected"
FAIL_ACTIVE_EXPIRED = "active_expired"
FAIL_KILL_SWITCH_TRIGGERED = "kill_switch_triggered"
FAIL_RUNTIME_AUDIT_FAILED = "runtime_audit_failed"
FAIL_RESERVATION_COMPLETION_FAILED = "reservation_completion_failed"
FAIL_RESERVATION_FAILURE_WRITE_FAILED = "reservation_failure_write_failed"
FAIL_EXECUTION_ALREADY_COMPLETED = "execution_already_completed"
FAIL_EXECUTION_IN_PROGRESS = "execution_in_progress"
FAIL_NEW_ACTIVATION_REQUIRED = "new_activation_required"

ACTION_CONTINUE_TO_PHASE_14H_3D = "continue_to_phase_14h_3d"
ACTION_INSPECT_RUNTIME_FAILURE = "inspect_runtime_failure"
ACTION_INSPECT_TIMEOUT = "inspect_timeout"
ACTION_INSPECT_SOURCE_MUTATION = "inspect_source_mutation"
ACTION_INSPECT_PUBLISH_VIOLATION = "inspect_publish_violation"
ACTION_SUSPEND_ACTIVE_ACTIVATION = "suspend_active_activation"
ACTION_CREATE_NEW_ACTIVATION_PROPOSAL = "create_new_activation_proposal"
ACTION_RECOVER_STARTED_RESERVATION = "recover_started_reservation"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"

_RUNTIME_STORE_DIR = "production-live-runtime"
_RUNTIME_STORE_VERSION = 1
_MAX_OUTPUT_BYTES = 64_000
_ALLOWED_OUTPUT_DIRS = frozenset({"outputs", "reports"})
_PUBLISH_OUTPUT_FRAGMENTS = frozenset(
    {
        "publish",
        "wordpress",
        "upload_to_live",
        "external_api",
        "discord_webhook",
    }
)

_EVENT_RUNTIME_INVOCATION_REQUESTED = "runtime_invocation_requested"
_EVENT_RESERVATION_STARTED = "reservation_started"
_EVENT_RUNTIME_STARTED = "runtime_started"
_EVENT_RUNTIME_COMPLETED = "runtime_completed"
_EVENT_RUNTIME_FAILED = "runtime_failed"
_EVENT_RUNTIME_TIMED_OUT = "runtime_timed_out"
_EVENT_RUNTIME_BLOCKED = "runtime_blocked"
_EVENT_RESERVATION_COMPLETED = "reservation_completed"
_EVENT_RESERVATION_FAILED = "reservation_failed"

_RUNTIME_ACTOR_ID = "live-pilot-runtime"


class ProductionActivationLiveRuntimeError(ValueError):
    """Raised when isolated mirror runtime cannot complete safely."""


@dataclass(frozen=True)
class MirrorSourceTreeSnapshot:
    pipeline_js_sha256: str
    package_json_sha256: str
    required_dir_signatures: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True)
class ProductionActivationLiveRuntimeResult:
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    runtime_invoked: bool
    started: bool
    completed: bool
    failed: bool
    exit_code: int
    timed_out: bool
    duration_ms: int
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_size_bytes: int
    stderr_size_bytes: int
    draft_artifacts_detected: bool
    source_tree_unchanged: bool
    publish_attempted: bool
    production_execution_allowed: bool = False
    original_repository2_execution_attempted: bool = False
    isolated_mirror_runtime_invoked: bool = False
    repository2_execution_attempted: bool = False
    reservation_state: str = ""
    activation_state_before: str = ""
    activation_state_after: str = ""
    consume_attempted: bool = False
    evidence_written: bool = False
    dispatch_audit_written: bool = False
    failure_reason_code: str = ""
    recommended_action: str = ""


@dataclass(frozen=True)
class ProductionActivationLiveRuntimeRecord:
    event_id: str
    event_type: str
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    ticket_id: str
    confirmation_id: str
    gate_event_id: str
    dry_run_event_id: str
    exit_code: int
    timed_out: bool
    duration_ms: int
    result: str
    failure_reason_code: str
    timestamp: str
    production_execution_allowed: bool = False
    original_repository2_execution_attempted: bool = False
    isolated_mirror_runtime_invoked: bool = False
    publish_attempted: bool = False


def default_runtime_history_dir() -> Path:
    return get_hermes_home() / "coo" / _RUNTIME_STORE_DIR


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dir_signature(root: Path, name: str) -> tuple[str, int, int]:
    dir_path = root / name
    file_count = 0
    byte_total = 0
    if dir_path.is_dir() and not dir_path.is_symlink():
        for child in dir_path.rglob("*"):
            if child.is_file() and not child.is_symlink():
                file_count += 1
                try:
                    byte_total += child.stat().st_size
                except OSError:
                    continue
    return name, file_count, byte_total


def capture_mirror_source_snapshot(resolved_mirror: str) -> MirrorSourceTreeSnapshot:
    root = Path(resolved_mirror)
    pipeline = root / REQUIRED_ENTRYPOINT
    package = root / REQUIRED_MANIFEST
    if not pipeline.is_file() or not package.is_file():
        raise ProductionActivationLiveRuntimeError("mirror source snapshot failed")
    signatures = tuple(
        _dir_signature(root, name) for name in REQUIRED_DIRECTORIES
    )
    return MirrorSourceTreeSnapshot(
        pipeline_js_sha256=_sha256_file(pipeline),
        package_json_sha256=_sha256_file(package),
        required_dir_signatures=signatures,
    )


def verify_mirror_source_unchanged(
    before: MirrorSourceTreeSnapshot,
    after: MirrorSourceTreeSnapshot,
) -> bool:
    return (
        before.pipeline_js_sha256 == after.pipeline_js_sha256
        and before.package_json_sha256 == after.package_json_sha256
        and before.required_dir_signatures == after.required_dir_signatures
    )


def _minimal_allowed_env() -> dict[str, str]:
    filtered: dict[str, str] = {}
    for key in _ALLOWED_ENV_KEYS:
        value = os.environ.get(key)
        if isinstance(value, str) and value:
            filtered[key] = value
    if "PATH" not in filtered:
        filtered["PATH"] = os.defpath
    return filtered


def _output_byte_size(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _detect_publish_attempt(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return any(fragment in combined for fragment in _PUBLISH_OUTPUT_FRAGMENTS)


def _detect_draft_artifacts(resolved_mirror: str) -> bool:
    root = Path(resolved_mirror)
    for name in _ALLOWED_OUTPUT_DIRS:
        candidate = root / name
        if candidate.is_dir() and any(candidate.iterdir()):
            return True
    return False


def _recommended_action_for_failure(code: str) -> str:
    mapping = {
        FAIL_RUNTIME_NONZERO: ACTION_INSPECT_RUNTIME_FAILURE,
        FAIL_RUNTIME_TIMEOUT: ACTION_INSPECT_TIMEOUT,
        FAIL_RUNTIME_SOURCE_MUTATION: ACTION_INSPECT_SOURCE_MUTATION,
        FAIL_RUNTIME_PUBLISH_ATTEMPT: ACTION_INSPECT_PUBLISH_VIOLATION,
        FAIL_EXECUTION_ALREADY_COMPLETED: ACTION_CREATE_NEW_ACTIVATION_PROPOSAL,
        FAIL_NEW_ACTIVATION_REQUIRED: ACTION_CREATE_NEW_ACTIVATION_PROPOSAL,
        FAIL_EXECUTION_IN_PROGRESS: ACTION_RECOVER_STARTED_RESERVATION,
        FAIL_KILL_SWITCH_TRIGGERED: ACTION_SUSPEND_ACTIVE_ACTIVATION,
    }
    return mapping.get(code, ACTION_MAINTAIN_PRODUCTION_BLOCK)


def _runtime_history_path(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionActivationLiveRuntimeError("activation_request_id is required")
    base = (history_dir or default_runtime_history_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationLiveRuntimeError(
            "Runtime history dir must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_runtime_audit_store_available(*, history_dir: Path | None = None) -> bool:
    try:
        base = (history_dir or default_runtime_history_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _load_runtime_records(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> list[ProductionActivationLiveRuntimeRecord]:
    path = _runtime_history_path(activation_request_id, history_dir=history_dir)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionActivationLiveRuntimeError(
            "Runtime audit store is corrupted."
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ProductionActivationLiveRuntimeError("Runtime audit store is corrupted.")
    records: list[ProductionActivationLiveRuntimeRecord] = []
    for item in payload["records"]:
        if not isinstance(item, dict):
            raise ProductionActivationLiveRuntimeError("Runtime audit store is corrupted.")
        records.append(
            ProductionActivationLiveRuntimeRecord(
                event_id=str(item.get("event_id", "")),
                event_type=str(item.get("event_type", "")),
                activation_request_id=str(item.get("activation_request_id", "")),
                reservation_id=str(item.get("reservation_id", "")),
                execution_attempt_id=str(item.get("execution_attempt_id", "")),
                ticket_id=str(item.get("ticket_id", "")),
                confirmation_id=str(item.get("confirmation_id", "")),
                gate_event_id=str(item.get("gate_event_id", "")),
                dry_run_event_id=str(item.get("dry_run_event_id", "")),
                exit_code=int(item.get("exit_code", 0)),
                timed_out=bool(item.get("timed_out", False)),
                duration_ms=int(item.get("duration_ms", 0)),
                result=str(item.get("result", "")),
                failure_reason_code=str(item.get("failure_reason_code", "")),
                timestamp=str(item.get("timestamp", "")),
                production_execution_allowed=False,
                original_repository2_execution_attempted=False,
                isolated_mirror_runtime_invoked=bool(
                    item.get("isolated_mirror_runtime_invoked", False)
                ),
                publish_attempted=bool(item.get("publish_attempted", False)),
            )
        )
    return records


def _atomic_append_runtime_record(
    record: ProductionActivationLiveRuntimeRecord,
    *,
    history_dir: Path | None = None,
) -> None:
    path = _runtime_history_path(record.activation_request_id, history_dir=history_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductionActivationLiveRuntimeError(
                "Runtime audit store is corrupted."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ProductionActivationLiveRuntimeError("Runtime audit store is corrupted.")
        for prior in payload["records"]:
            if (
                isinstance(prior, dict)
                and prior.get("event_type") == record.event_type
                and prior.get("execution_attempt_id") == record.execution_attempt_id
            ):
                return
        existing = payload["records"]
    entry = {
        "event_id": record.event_id,
        "event_type": record.event_type,
        "activation_request_id": record.activation_request_id,
        "reservation_id": record.reservation_id,
        "execution_attempt_id": record.execution_attempt_id,
        "ticket_id": record.ticket_id,
        "confirmation_id": record.confirmation_id,
        "gate_event_id": record.gate_event_id,
        "dry_run_event_id": record.dry_run_event_id,
        "exit_code": record.exit_code,
        "timed_out": record.timed_out,
        "duration_ms": record.duration_ms,
        "result": record.result,
        "failure_reason_code": record.failure_reason_code,
        "timestamp": record.timestamp,
        "production_execution_allowed": False,
        "original_repository2_execution_attempted": False,
        "isolated_mirror_runtime_invoked": record.isolated_mirror_runtime_invoked,
        "publish_attempted": record.publish_attempted,
    }
    payload = {
        "version": _RUNTIME_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "records": [*existing, entry],
    }
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise ProductionActivationLiveRuntimeError("runtime_audit_failed") from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _append_runtime_event(
    *,
    event_type: str,
    reservation: ProductionActivationExecutionReservation,
    result: str,
    failure_reason_code: str = "",
    exit_code: int = 0,
    timed_out: bool = False,
    duration_ms: int = 0,
    isolated_mirror_runtime_invoked: bool = False,
    publish_attempted: bool = False,
    history_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    record = ProductionActivationLiveRuntimeRecord(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        activation_request_id=reservation.activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        ticket_id=reservation.ticket_id,
        confirmation_id=reservation.confirmation_id,
        gate_event_id=reservation.execution_gate_event_id,
        dry_run_event_id=reservation.dry_run_event_id,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=duration_ms,
        result=result,
        failure_reason_code=failure_reason_code,
        timestamp=_utc_now_iso(now),
        production_execution_allowed=False,
        original_repository2_execution_attempted=False,
        isolated_mirror_runtime_invoked=isolated_mirror_runtime_invoked,
        publish_attempted=publish_attempted,
    )
    _atomic_append_runtime_record(record, history_dir=history_dir)


def _suspend_activation_after_runtime(
    request: ActivationRequest,
    *,
    reason_code: str,
    store_dir: Path | None,
    now: datetime | None,
) -> str:
    before = request.state
    if before == ACTIVATION_STATE_SUSPENDED:
        return before
    suspend_production_activation(
        activation_request_id=request.activation_request_id,
        actor_id=_RUNTIME_ACTOR_ID,
        actor_role=ROLE_OPERATOR,
        reason_code=reason_code,
        store_dir=store_dir,
        now=now,
    )
    return ACTIVATION_STATE_SUSPENDED


def _resolve_node_executable(merged_config: Mapping[str, Any] | None) -> str:
    if merged_config is None or not isinstance(merged_config, Mapping):
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


class BoundedProductionLiveRuntimeInvoker:
    """Invoke bounded dispatch runner inside an active ephemeral permit."""

    def __init__(
        self,
        *,
        runner_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._runner_factory = runner_factory or create_bounded_subprocess_runner

    def is_enabled(self) -> bool:
        return True

    def invoke(
        self,
        *,
        harness_request: ProductionActivationLiveHarnessRequest,
        reservation: ProductionActivationExecutionReservation,
    ) -> ProductionActivationLiveRuntimeResult:
        raise RuntimeError(
            "invoke requires run_isolated_mirror_live_runtime(); "
            "use orchestration entrypoint with full context"
        )


def run_isolated_mirror_live_runtime(
    *,
    request: ActivationRequest,
    reservation: ProductionActivationExecutionReservation,
    harness_request: ProductionActivationLiveHarnessRequest,
    harness_plan: ProductionActivationLiveHarnessPlan,
    pipeline_root: str,
    merged_config: Mapping[str, Any] | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    runtime_history_dir: Path | None = None,
    gate_history_dir: Path | None = None,
    dry_run_history_dir: Path | None = None,
    runner_factory: Callable[..., Any] | None = None,
    now: datetime | None = None,
) -> ProductionActivationLiveRuntimeResult:
    """Execute one bounded isolated-mirror runtime inside active permit context."""
    if not probe_runtime_audit_store_available(history_dir=runtime_history_dir):
        raise ProductionActivationLiveRuntimeError("runtime_audit_failed")

    activation_before = request.state
    base_result = ProductionActivationLiveRuntimeResult(
        activation_request_id=reservation.activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        runtime_invoked=False,
        started=False,
        completed=False,
        failed=False,
        exit_code=0,
        timed_out=False,
        duration_ms=0,
        stdout_truncated=False,
        stderr_truncated=False,
        stdout_size_bytes=0,
        stderr_size_bytes=0,
        draft_artifacts_detected=False,
        source_tree_unchanged=True,
        publish_attempted=False,
        reservation_state=reservation.state,
        activation_state_before=activation_before,
        activation_state_after=activation_before,
    )

    try:
        permit = require_active_execution_permit()
    except ActivationExecutionPermitError as exc:
        return replace(
            base_result,
            failure_reason_code=FAIL_PERMIT_NOT_ACTIVE,
            recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )

    if (
        permit.activation_request_id != reservation.activation_request_id
        or permit.reservation_id != reservation.reservation_id
        or permit.execution_attempt_id != reservation.execution_attempt_id
    ):
        return replace(
            base_result,
            failure_reason_code=FAIL_PERMIT_NOT_ACTIVE,
            recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )

    if reservation.state != RESERVATION_STATE_RESERVED:
        failure = (
            FAIL_EXECUTION_IN_PROGRESS
            if reservation.state == RESERVATION_STATE_STARTED
            else (
                FAIL_EXECUTION_ALREADY_COMPLETED
                if reservation.state == RESERVATION_STATE_COMPLETED
                else FAIL_NEW_ACTIVATION_REQUIRED
            )
        )
        return replace(
            base_result,
            failure_reason_code=failure,
            recommended_action=_recommended_action_for_failure(failure),
        )

    if not harness_plan.runtime_invocation_planned or harness_plan.blocking_reasons:
        return replace(
            base_result,
            failure_reason_code=FAIL_ISOLATED_RUNTIME_NOT_ENABLED,
            recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )

    if request.state != ACTIVATION_STATE_ACTIVE:
        return replace(
            base_result,
            failure_reason_code=FAIL_ACTIVE_EXPIRED,
            recommended_action=ACTION_SUSPEND_ACTIVE_ACTIVATION,
        )

    if not is_kill_switch_available(request, store_dir=store_dir):
        return replace(
            base_result,
            failure_reason_code=FAIL_KILL_SWITCH_TRIGGERED,
            recommended_action=ACTION_SUSPEND_ACTIVE_ACTIVATION,
        )

    if _probe_publish_intent(reservation.ticket_id):
        return replace(
            base_result,
            failure_reason_code=FAIL_RUNTIME_PUBLISH_ATTEMPT,
            recommended_action=ACTION_INSPECT_PUBLISH_VIOLATION,
        )

    try:
        resolved_mirror = resolve_pipeline_root(pipeline_root)
        assert_pipeline_root_allowed(resolved_mirror)
    except ValueError:
        return replace(
            base_result,
            failure_reason_code=FAIL_RUNNER_FACTORY_FAILED,
            recommended_action=ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )

    _append_runtime_event(
        event_type=_EVENT_RUNTIME_INVOCATION_REQUESTED,
        reservation=reservation,
        result="requested",
        history_dir=runtime_history_dir,
        now=now,
    )

    try:
        started_reservation = transition_execution_reservation_to_started(
            reservation,
            store_dir=reservation_dir,
            now=now,
        )
    except ProductionActivationExecutionReservationError:
        return replace(
            base_result,
            failure_reason_code=FAIL_RESERVATION_START_FAILED,
            recommended_action=ACTION_RECOVER_STARTED_RESERVATION,
        )

    _append_runtime_event(
        event_type=_EVENT_RESERVATION_STARTED,
        reservation=started_reservation,
        result="started",
        history_dir=runtime_history_dir,
        now=now,
    )

    before_snapshot = capture_mirror_source_snapshot(resolved_mirror)
    node_executable = _resolve_node_executable(merged_config)
    policy = load_dispatch_executor_policy(merged_config=merged_config)
    argv = [
        node_executable,
        "pipeline.js",
        "--run-date",
        harness_request.run_date,
    ]
    cwd = resolved_mirror
    env = _minimal_allowed_env()
    timeout_seconds = harness_request.timeout_seconds

    factory = runner_factory or create_bounded_subprocess_runner
    try:
        runner = factory(
            tuple(policy.allowed_pipeline_roots),
            profile=RUNNER_PROFILE_DISPATCH,
            node_executable=node_executable,
            max_output_bytes=_MAX_OUTPUT_BYTES,
            max_timeout_seconds=harness_request.timeout_seconds,
        )
    except BoundedSubprocessRunnerError:
        _fail_runtime(
            started_reservation,
            failure_code=FAIL_RUNNER_FACTORY_FAILED,
            reservation_dir=reservation_dir,
            runtime_history_dir=runtime_history_dir,
            now=now,
        )
        return _build_failed_result(
            started_reservation,
            activation_before=activation_before,
            failure_code=FAIL_RUNNER_FACTORY_FAILED,
            store_dir=store_dir,
            now=now,
        )

    _append_runtime_event(
        event_type=_EVENT_RUNTIME_STARTED,
        reservation=started_reservation,
        result="started",
        isolated_mirror_runtime_invoked=True,
        history_dir=runtime_history_dir,
        now=now,
    )

    start_time = time.monotonic()
    try:
        exit_code, stdout_text, stderr_text = runner(argv, cwd, env, timeout_seconds)
    except BoundedSubprocessRunnerError:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        _append_runtime_event(
            event_type=_EVENT_RUNTIME_FAILED,
            reservation=started_reservation,
            result="failed",
            failure_reason_code=FAIL_RUNNER_INVOCATION_FAILED,
            duration_ms=duration_ms,
            isolated_mirror_runtime_invoked=True,
            history_dir=runtime_history_dir,
            now=now,
        )
        _fail_runtime(
            started_reservation,
            failure_code=FAIL_RUNNER_INVOCATION_FAILED,
            reservation_dir=reservation_dir,
            runtime_history_dir=runtime_history_dir,
            now=now,
        )
        return _build_failed_result(
            started_reservation,
            activation_before=activation_before,
            failure_code=FAIL_RUNNER_INVOCATION_FAILED,
            runtime_invoked=True,
            duration_ms=duration_ms,
            store_dir=store_dir,
            now=now,
        )
    except Exception:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        _append_runtime_event(
            event_type=_EVENT_RUNTIME_FAILED,
            reservation=started_reservation,
            result="failed",
            failure_reason_code=FAIL_RUNTIME_EXCEPTION,
            duration_ms=duration_ms,
            isolated_mirror_runtime_invoked=True,
            history_dir=runtime_history_dir,
            now=now,
        )
        _fail_runtime(
            started_reservation,
            failure_code=FAIL_RUNTIME_EXCEPTION,
            reservation_dir=reservation_dir,
            runtime_history_dir=runtime_history_dir,
            now=now,
        )
        return _build_failed_result(
            started_reservation,
            activation_before=activation_before,
            failure_code=FAIL_RUNTIME_EXCEPTION,
            runtime_invoked=True,
            duration_ms=duration_ms,
            store_dir=store_dir,
            now=now,
        )

    duration_ms = int((time.monotonic() - start_time) * 1000)
    stdout_size = _output_byte_size(stdout_text)
    stderr_size = _output_byte_size(stderr_text)
    timed_out = exit_code == _TIMEOUT_EXIT_CODE
    publish_attempted = _detect_publish_attempt(stdout_text, stderr_text)

    try:
        after_snapshot = capture_mirror_source_snapshot(resolved_mirror)
    except ProductionActivationLiveRuntimeError:
        after_snapshot = before_snapshot

    source_unchanged = verify_mirror_source_unchanged(before_snapshot, after_snapshot)
    draft_detected = _detect_draft_artifacts(resolved_mirror)

    if publish_attempted:
        failure = FAIL_RUNTIME_PUBLISH_ATTEMPT
        _record_runtime_failure(
            started_reservation,
            failure=failure,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            reservation_dir=reservation_dir,
            runtime_history_dir=runtime_history_dir,
            now=now,
        )
        return _build_failed_result(
            started_reservation,
            activation_before=activation_before,
            failure_code=failure,
            runtime_invoked=True,
            timed_out=timed_out,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_truncated=stdout_size >= _MAX_OUTPUT_BYTES,
            stderr_truncated=stderr_size >= _MAX_OUTPUT_BYTES,
            stdout_size_bytes=stdout_size,
            stderr_size_bytes=stderr_size,
            source_tree_unchanged=source_unchanged,
            publish_attempted=True,
            draft_artifacts_detected=draft_detected,
            store_dir=store_dir,
            now=now,
        )

    if not source_unchanged:
        failure = FAIL_RUNTIME_SOURCE_MUTATION
        _record_runtime_failure(
            started_reservation,
            failure=failure,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            reservation_dir=reservation_dir,
            runtime_history_dir=runtime_history_dir,
            now=now,
        )
        return _build_failed_result(
            started_reservation,
            activation_before=activation_before,
            failure_code=failure,
            runtime_invoked=True,
            timed_out=timed_out,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_truncated=stdout_size >= _MAX_OUTPUT_BYTES,
            stderr_truncated=stderr_size >= _MAX_OUTPUT_BYTES,
            stdout_size_bytes=stdout_size,
            stderr_size_bytes=stderr_size,
            source_tree_unchanged=False,
            draft_artifacts_detected=draft_detected,
            store_dir=store_dir,
            now=now,
        )

    if timed_out:
        failure = FAIL_RUNTIME_TIMEOUT
        _record_runtime_failure(
            started_reservation,
            failure=failure,
            exit_code=exit_code,
            timed_out=True,
            duration_ms=duration_ms,
            reservation_dir=reservation_dir,
            runtime_history_dir=runtime_history_dir,
            now=now,
            timed_out_event=True,
        )
        return _build_failed_result(
            started_reservation,
            activation_before=activation_before,
            failure_code=failure,
            runtime_invoked=True,
            timed_out=True,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_truncated=stdout_size >= _MAX_OUTPUT_BYTES,
            stderr_truncated=stderr_size >= _MAX_OUTPUT_BYTES,
            stdout_size_bytes=stdout_size,
            stderr_size_bytes=stderr_size,
            source_tree_unchanged=source_unchanged,
            draft_artifacts_detected=draft_detected,
            store_dir=store_dir,
            now=now,
        )

    if exit_code != 0:
        failure = FAIL_RUNTIME_NONZERO
        _record_runtime_failure(
            started_reservation,
            failure=failure,
            exit_code=exit_code,
            timed_out=False,
            duration_ms=duration_ms,
            reservation_dir=reservation_dir,
            runtime_history_dir=runtime_history_dir,
            now=now,
        )
        return _build_failed_result(
            started_reservation,
            activation_before=activation_before,
            failure_code=failure,
            runtime_invoked=True,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_truncated=stdout_size >= _MAX_OUTPUT_BYTES,
            stderr_truncated=stderr_size >= _MAX_OUTPUT_BYTES,
            stdout_size_bytes=stdout_size,
            stderr_size_bytes=stderr_size,
            source_tree_unchanged=source_unchanged,
            draft_artifacts_detected=draft_detected,
            store_dir=store_dir,
            now=now,
        )

    try:
        completed_reservation = transition_execution_reservation_to_completed(
            started_reservation,
            store_dir=reservation_dir,
            now=now,
        )
    except ProductionActivationExecutionReservationError:
        return replace(
            base_result,
            runtime_invoked=True,
            started=True,
            isolated_mirror_runtime_invoked=True,
            failure_reason_code=FAIL_RESERVATION_COMPLETION_FAILED,
            recommended_action=ACTION_RECOVER_STARTED_RESERVATION,
            reservation_state=started_reservation.state,
        )

    _append_runtime_event(
        event_type=_EVENT_RUNTIME_COMPLETED,
        reservation=completed_reservation,
        result="completed",
        exit_code=0,
        duration_ms=duration_ms,
        isolated_mirror_runtime_invoked=True,
        history_dir=runtime_history_dir,
        now=now,
    )
    _append_runtime_event(
        event_type=_EVENT_RESERVATION_COMPLETED,
        reservation=completed_reservation,
        result="completed",
        exit_code=0,
        duration_ms=duration_ms,
        isolated_mirror_runtime_invoked=True,
        history_dir=runtime_history_dir,
        now=now,
    )

    activation_after = _suspend_activation_after_runtime(
        request,
        reason_code=REASON_RUNTIME_COMPLETED_WAITING_E2E,
        store_dir=store_dir,
        now=now,
    )

    return ProductionActivationLiveRuntimeResult(
        activation_request_id=completed_reservation.activation_request_id,
        reservation_id=completed_reservation.reservation_id,
        execution_attempt_id=completed_reservation.execution_attempt_id,
        runtime_invoked=True,
        started=True,
        completed=True,
        failed=False,
        exit_code=0,
        timed_out=False,
        duration_ms=duration_ms,
        stdout_truncated=stdout_size >= _MAX_OUTPUT_BYTES,
        stderr_truncated=stderr_size >= _MAX_OUTPUT_BYTES,
        stdout_size_bytes=stdout_size,
        stderr_size_bytes=stderr_size,
        draft_artifacts_detected=draft_detected,
        source_tree_unchanged=True,
        publish_attempted=False,
        isolated_mirror_runtime_invoked=True,
        reservation_state=completed_reservation.state,
        activation_state_before=activation_before,
        activation_state_after=activation_after,
        failure_reason_code="",
        recommended_action=ACTION_CONTINUE_TO_PHASE_14H_3D,
    )


def _record_runtime_failure(
    reservation: ProductionActivationExecutionReservation,
    *,
    failure: str,
    exit_code: int,
    timed_out: bool,
    duration_ms: int,
    reservation_dir: Path | None,
    runtime_history_dir: Path | None,
    now: datetime | None,
    timed_out_event: bool = False,
) -> None:
    _append_runtime_event(
        event_type=_EVENT_RUNTIME_TIMED_OUT if timed_out_event else _EVENT_RUNTIME_FAILED,
        reservation=reservation,
        result="failed",
        failure_reason_code=failure,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=duration_ms,
        isolated_mirror_runtime_invoked=True,
        publish_attempted=failure == FAIL_RUNTIME_PUBLISH_ATTEMPT,
        history_dir=runtime_history_dir,
        now=now,
    )
    _fail_runtime(
        reservation,
        failure_code=failure,
        reservation_dir=reservation_dir,
        runtime_history_dir=runtime_history_dir,
        now=now,
    )


def _fail_runtime(
    reservation: ProductionActivationExecutionReservation,
    *,
    failure_code: str,
    reservation_dir: Path | None,
    runtime_history_dir: Path | None,
    now: datetime | None,
) -> None:
    try:
        failed_reservation = transition_execution_reservation_to_failed(
            reservation,
            failure_reason_code=failure_code,
            store_dir=reservation_dir,
            now=now,
        )
    except ProductionActivationExecutionReservationError:
        return
    _append_runtime_event(
        event_type=_EVENT_RESERVATION_FAILED,
        reservation=failed_reservation,
        result="failed",
        failure_reason_code=failure_code,
        isolated_mirror_runtime_invoked=True,
        history_dir=runtime_history_dir,
        now=now,
    )


def _build_failed_result(
    reservation: ProductionActivationExecutionReservation,
    *,
    activation_before: str,
    failure_code: str,
    runtime_invoked: bool = False,
    timed_out: bool = False,
    exit_code: int = 1,
    duration_ms: int = 0,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    stdout_size_bytes: int = 0,
    stderr_size_bytes: int = 0,
    source_tree_unchanged: bool = True,
    publish_attempted: bool = False,
    draft_artifacts_detected: bool = False,
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> ProductionActivationLiveRuntimeResult:
    reason_map = {
        FAIL_RUNTIME_NONZERO: REASON_RUNTIME_NONZERO,
        FAIL_RUNTIME_TIMEOUT: REASON_RUNTIME_TIMEOUT,
        FAIL_RUNTIME_EXCEPTION: REASON_RUNTIME_EXCEPTION,
        FAIL_RUNTIME_SOURCE_MUTATION: REASON_RUNTIME_SOURCE_MUTATION,
        FAIL_RUNTIME_PUBLISH_ATTEMPT: REASON_RUNTIME_PUBLISH_ATTEMPT,
    }
    suspend_reason = reason_map.get(failure_code, REASON_RUNTIME_EXCEPTION)
    try:
        activation_after = _suspend_activation_after_runtime(
            _load_request_for_suspend(reservation.activation_request_id, store_dir),
            reason_code=suspend_reason,
            store_dir=store_dir,
            now=now,
        )
    except Exception:
        activation_after = activation_before

    return ProductionActivationLiveRuntimeResult(
        activation_request_id=reservation.activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        runtime_invoked=runtime_invoked,
        started=True,
        completed=False,
        failed=True,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=duration_ms,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        stdout_size_bytes=stdout_size_bytes,
        stderr_size_bytes=stderr_size_bytes,
        draft_artifacts_detected=draft_artifacts_detected,
        source_tree_unchanged=source_tree_unchanged,
        publish_attempted=publish_attempted,
        isolated_mirror_runtime_invoked=runtime_invoked,
        reservation_state=RESERVATION_STATE_FAILED,
        activation_state_before=activation_before,
        activation_state_after=activation_after,
        failure_reason_code=failure_code,
        recommended_action=_recommended_action_for_failure(failure_code),
    )


def _load_request_for_suspend(
    activation_request_id: str,
    store_dir: Path | None,
) -> ActivationRequest:
    from agent.coo.production_activation_store import load_activation_request

    return load_activation_request(activation_request_id, store_dir=store_dir)


def load_runtime_records(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> list[ProductionActivationLiveRuntimeRecord]:
    return _load_runtime_records(activation_request_id, history_dir=history_dir)
