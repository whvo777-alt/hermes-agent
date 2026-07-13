"""Production activation live harness wiring contract — Phase 14H-3C-1.

Harness request/plan evaluation and runtime boundary wiring without subprocess,
bounded runner creation, or Repository2 execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from agent.coo.bounded_subprocess_runner import (
    RUNNER_PROFILE_DISPATCH,
    BoundedSubprocessRunnerError,
    validate_dispatch_runner_argv_contract,
)
from agent.coo.dispatch_cli_repository_attestation import (
    REQUIRED_DIRECTORIES,
    REQUIRED_ENTRYPOINT,
    REQUIRED_MANIFEST,
)
from agent.coo.dispatch_executor_config import load_dispatch_executor_policy
from agent.coo.dispatch_pipeline_root_trust import (
    assert_pipeline_root_allowed,
    resolve_pipeline_root,
)
from agent.coo.dispatch_runner_binding_state import (
    RUNNER_BINDING_STATE_BOUND,
    load_dispatch_runner_binding_state,
)
from agent.coo.dispatch_runner_provider import (
    RUNNER_PROVIDER_MODE_BOUNDED,
    assess_dispatch_runner_provider,
)
from agent.coo.production_activation_dry_run import (
    _probe_publish_intent,
    find_dry_run_record,
)
from agent.coo.production_activation_execution_gate import (
    find_ready_execution_gate_record,
)
from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_RESERVED,
    ProductionActivationExecutionReservation,
)
from agent.coo.production_activation_state import (
    ACTIVATION_STATE_ACTIVE,
    ActivationRequest,
)
from hermes_constants import get_hermes_home

BLOCK_ACTIVATION_NOT_ACTIVE = "activation_not_active"
BLOCK_ACTIVE_EXPIRED = "active_expired"
BLOCK_RESERVATION_MISSING = "reservation_missing"
BLOCK_RESERVATION_NOT_RESERVED = "reservation_not_reserved"
BLOCK_RESERVATION_SCOPE_MISMATCH = "reservation_scope_mismatch"
BLOCK_PERMIT_NOT_READY = "permit_not_ready"
BLOCK_EXECUTION_GATE_NOT_READY = "execution_gate_not_ready"
BLOCK_DRY_RUN_NOT_READY = "dry_run_not_ready"
BLOCK_MIRROR_ROOT_NOT_TRUSTED = "mirror_root_not_trusted"
BLOCK_PRODUCTION_ROOT_DENIED = "production_root_denied"
BLOCK_RUNNER_PROFILE_INVALID = "runner_profile_invalid"
BLOCK_RUNNER_FACTORY_UNAVAILABLE = "runner_factory_unavailable"
BLOCK_RUNTIME_INVOKER_DISABLED = "runtime_invoker_disabled"
BLOCK_ARGV_CONTRACT_INVALID = "argv_contract_invalid"
BLOCK_CWD_CONTRACT_INVALID = "cwd_contract_invalid"
BLOCK_ENV_CONTRACT_INVALID = "env_contract_invalid"
BLOCK_TIMEOUT_INVALID = "timeout_invalid"
BLOCK_PUBLISH_NOT_ALLOWED = "publish_not_allowed"
BLOCK_AUDIT_STORE_UNAVAILABLE = "audit_store_unavailable"
BLOCK_RUNTIME_BLOCKED_WAITING_PHASE_14H_3C_2 = "runtime_blocked_waiting_phase_14h_3c_2"

ACTION_CONTINUE_TO_PHASE_14H_3C_2 = "continue_to_phase_14h_3c_2"
ACTION_RESOLVE_HARNESS_CONTRACT = "resolve_harness_contract"
ACTION_RESOLVE_RESERVATION = "resolve_reservation"
ACTION_REFRESH_EXECUTION_GATE = "refresh_execution_gate"
ACTION_REFRESH_PRODUCTION_DRY_RUN = "refresh_production_dry_run"
ACTION_PREPARE_ISOLATED_PRODUCTION_MIRROR = "prepare_isolated_production_mirror"
ACTION_CONFIGURE_BOUNDED_RUNNER_CONTRACT = "configure_bounded_runner_contract"
ACTION_SUSPEND_ACTIVE_ACTIVATION = "suspend_active_activation"
ACTION_CREATE_NEW_ACTIVATION_PROPOSAL = "create_new_activation_proposal"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_ALREADY_EVALUATED = "already_evaluated"

_DEFAULT_TIMEOUT_SECONDS = 300
_MIN_TIMEOUT_SECONDS = 1
_MAX_TIMEOUT_SECONDS = 3600
_RUN_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALLOWED_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ"})
_FORBIDDEN_ENV_KEY_FRAGMENTS = frozenset(
    {"token", "secret", "api_key", "apikey", "discord", "wordpress", "credential"}
)
_BLOCKED_EXECUTABLE_BASENAMES = frozenset(
    {"npm", "npx", "bash", "sh", "dash", "zsh", "bun", "deno"}
)
_SHELL_METACHAR_PATTERN = re.compile(r"[;&|`$<>]")

_HARNESS_STORE_DIR = "production-live-harness"
_HARNESS_STORE_VERSION = 1

_EVENT_HARNESS_PLAN_EVALUATED = "harness_plan_evaluated"
_EVENT_HARNESS_PLAN_READY = "harness_plan_ready"
_EVENT_RUNTIME_BLOCKED_WAITING = "runtime_blocked_waiting_phase_14h_3c_2"


class ProductionActivationLiveHarnessError(ValueError):
    """Raised when harness wiring cannot complete safely."""


class RuntimeNotEnabledError(RuntimeError):
    """Raised when runtime invocation is attempted before Phase 14H-3C-2."""


@dataclass(frozen=True)
class ProductionActivationLiveHarnessRequest:
    """Safe harness request without pipeline root or path literals."""

    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    ticket_id: str
    confirmation_id: str
    execution_gate_event_id: str
    dry_run_event_id: str
    pipeline_root_token: str
    runner_profile: str
    timeout_seconds: int
    run_date: str
    draft_only: bool = True
    publish_allowed: bool = False
    production_execution_allowed: bool = False
    repository2_execution_attempted: bool = False


@dataclass(frozen=True)
class ProductionActivationLiveHarnessPlan:
    """Harness contract evaluation plan without runtime invocation."""

    request_valid: bool
    reservation_valid: bool
    permit_valid: bool
    active_valid: bool
    gate_valid: bool
    mirror_valid: bool
    runner_profile_valid: bool
    argv_contract_valid: bool
    cwd_contract_valid: bool
    env_contract_valid: bool
    timeout_valid: bool
    draft_only: bool = True
    publish_allowed: bool = False
    runtime_invocation_planned: bool = False
    runtime_invoked: bool = False
    production_execution_allowed: bool = False
    repository2_execution_attempted: bool = False
    blocking_reasons: tuple[str, ...] = ()
    recommended_action: str = ""


@dataclass(frozen=True)
class ProductionActivationLiveHarnessResult:
    """Safe harness wiring result."""

    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    runner_profile: str
    timeout_seconds: int
    harness_ready: bool
    already_evaluated: bool
    plan: ProductionActivationLiveHarnessPlan
    failure_reason_code: str = ""
    recommended_action: str = ""


@dataclass(frozen=True)
class ProductionActivationLiveHarnessRecord:
    event_id: str
    event_type: str
    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    harness_key: str
    result: str
    failure_reason_code: str
    timestamp: str
    production_execution_allowed: bool = False
    repository2_execution_attempted: bool = False
    runtime_invoked: bool = False


@runtime_checkable
class ProductionLiveRunnerFactory(Protocol):
    """Runner factory contract — availability check only in Phase 14H-3C-1."""

    def is_available(
        self,
        *,
        pipeline_root: str,
        runner_profile: str,
        timeout_seconds: int,
        merged_config: Mapping[str, Any] | None = None,
    ) -> bool:
        """Return whether a bounded runner could be created without invoking it."""


@runtime_checkable
class ProductionLiveRuntimeInvoker(Protocol):
    """Runtime invoker contract — disabled until Phase 14H-3C-2."""

    def is_enabled(self) -> bool:
        """Whether runtime invocation is enabled."""

    def invoke(
        self,
        *,
        harness_request: ProductionActivationLiveHarnessRequest,
        reservation: ProductionActivationExecutionReservation,
    ) -> Any:
        """Invoke bounded runtime (not enabled in Phase 14H-3C-1)."""


class ConfigBoundRunnerFactoryAvailability:
    """Check runner factory prerequisites without calling create_bounded_subprocess_runner."""

    def is_available(
        self,
        *,
        pipeline_root: str,
        runner_profile: str,
        timeout_seconds: int,
        merged_config: Mapping[str, Any] | None = None,
    ) -> bool:
        if runner_profile != RUNNER_PROFILE_DISPATCH:
            return False
        provider = assess_dispatch_runner_provider(merged_config)
        if not provider.runner_provider_configured:
            return False
        if provider.runner_provider_mode != RUNNER_PROVIDER_MODE_BOUNDED:
            return False
        if not provider.provider_valid:
            return False
        binding = load_dispatch_runner_binding_state()
        if binding.state != RUNNER_BINDING_STATE_BOUND:
            return False
        node_executable = _resolve_node_executable(merged_config)
        if not node_executable:
            return False
        try:
            resolved_root = resolve_pipeline_root(pipeline_root)
            assert_pipeline_root_allowed(resolved_root)
            policy = load_dispatch_executor_policy(merged_config=merged_config)
            candidate = os.path.realpath(resolved_root)
            if not any(
                os.path.realpath(os.path.expanduser(allowed.strip())) == candidate
                for allowed in policy.allowed_pipeline_roots
            ):
                return False
        except ValueError:
            return False
        try:
            expanded = os.path.expanduser(node_executable.strip())
            if not os.path.isabs(expanded) or not os.path.isfile(expanded):
                return False
            if os.path.basename(os.path.realpath(expanded)).lower() != "node":
                return False
        except OSError:
            return False
        return True


class DisabledProductionLiveRuntimeInvoker:
    """Default runtime invoker that refuses invocation until Phase 14H-3C-2."""

    def is_enabled(self) -> bool:
        return False

    def invoke(
        self,
        *,
        harness_request: ProductionActivationLiveHarnessRequest,
        reservation: ProductionActivationExecutionReservation,
    ) -> Any:
        raise RuntimeNotEnabledError("runtime invocation is not enabled")


def default_harness_history_dir() -> Path:
    return get_hermes_home() / "coo" / _HARNESS_STORE_DIR


def compute_pipeline_root_token(pipeline_root_resolved: str) -> str:
    normalized = (pipeline_root_resolved or "").strip()
    if not normalized:
        raise ProductionActivationLiveHarnessError("pipeline_root_token requires resolved root")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_harness_key(request: ProductionActivationLiveHarnessRequest) -> str:
    payload = "|".join(
        (
            request.activation_request_id,
            request.reservation_id,
            request.execution_gate_event_id,
            request.dry_run_event_id,
            request.pipeline_root_token,
            request.runner_profile,
            str(request.timeout_seconds),
            request.run_date,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


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


def _default_run_date(now: datetime | None = None) -> str:
    return _utc_now(now).date().isoformat()


def _assert_run_date(run_date: str) -> None:
    normalized = (run_date or "").strip()
    if not _RUN_DATE_PATTERN.match(normalized):
        raise ProductionActivationLiveHarnessError("run_date must match YYYY-MM-DD")
    datetime.strptime(normalized, "%Y-%m-%d")


def _validate_timeout_seconds(
    timeout_seconds: int,
    *,
    request: ActivationRequest,
    now: datetime | None = None,
) -> bool:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        return False
    if timeout_seconds < _MIN_TIMEOUT_SECONDS or timeout_seconds > _MAX_TIMEOUT_SECONDS:
        return False
    expires_text = (request.active_expires_at or "").strip()
    if not expires_text:
        return False
    expires = _parse_iso(expires_text)
    current = _utc_now(now)
    remaining = int((expires - current).total_seconds())
    if remaining <= 0:
        return False
    return timeout_seconds <= remaining


def validate_live_harness_env_contract(env: Mapping[str, Any] | None) -> bool:
    """Validate env keys against allowlist without creating or passing env."""
    if env is None:
        return True
    if not isinstance(env, Mapping):
        return False
    for key in env:
        if not isinstance(key, str):
            return False
        lowered = key.lower()
        if key not in _ALLOWED_ENV_KEYS:
            return False
        if any(fragment in lowered for fragment in _FORBIDDEN_ENV_KEY_FRAGMENTS):
            return False
        value = env.get(key)
        if value is not None and not isinstance(value, str):
            return False
    return True


def _argv_has_shell_metacharacters(argv: list[str]) -> bool:
    return any(_SHELL_METACHAR_PATTERN.search(item) for item in argv)


def validate_live_harness_argv_contract(
    argv: list[str],
    *,
    node_executable: str,
) -> bool:
    """Validate planned argv shape without execution."""
    if not argv or len(argv) != 4:
        return False
    executable_basename = os.path.basename(os.path.expanduser(argv[0])).lower()
    if executable_basename in _BLOCKED_EXECUTABLE_BASENAMES:
        return False
    if _argv_has_shell_metacharacters(argv):
        return False
    try:
        validate_dispatch_runner_argv_contract(argv, node_executable=node_executable)
    except BoundedSubprocessRunnerError:
        return False
    return True


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


def _symlink_escape_detected(path: str, *, allowed_real: str) -> bool:
    expanded = os.path.abspath(os.path.expanduser(path.strip()))
    current = expanded
    while True:
        if os.path.islink(current):
            link_target = os.path.realpath(current)
            try:
                if os.path.commonpath([link_target, allowed_real]) != allowed_real:
                    return True
            except ValueError:
                return True
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return False


def _mirror_structure_valid(resolved_mirror: str) -> bool:
    root = Path(resolved_mirror)
    pipeline = root / REQUIRED_ENTRYPOINT
    package = root / REQUIRED_MANIFEST
    try:
        if not pipeline.is_file() or pipeline.is_symlink():
            return False
        if not package.is_file() or package.is_symlink():
            return False
        for name in REQUIRED_DIRECTORIES:
            dir_path = root / name
            if dir_path.is_symlink() or not dir_path.is_dir():
                return False
    except OSError:
        return False
    return True


def validate_live_harness_cwd_contract(
    cwd: str,
    *,
    resolved_mirror: str,
    merged_config: Mapping[str, Any] | None = None,
) -> bool:
    """Validate cwd against isolated mirror contract without storing cwd."""
    if not isinstance(cwd, str) or not cwd.strip():
        return False
    try:
        resolved_cwd = os.path.realpath(os.path.expanduser(cwd.strip()))
        assert_pipeline_root_allowed(resolved_cwd)
        allowed_real = os.path.realpath(resolved_mirror)
        if resolved_cwd != allowed_real:
            return False
        if not _mirror_in_allowlist(resolved_mirror, merged_config=merged_config):
            return False
        if _symlink_escape_detected(cwd, allowed_real=allowed_real):
            return False
        return _mirror_structure_valid(resolved_mirror)
    except (ValueError, OSError):
        return False


def build_live_harness_request(
    *,
    activation_request_id: str,
    reservation: ProductionActivationExecutionReservation,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root_resolved: str,
    runner_profile: str = RUNNER_PROFILE_DISPATCH,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    run_date: str | None = None,
    draft_only: bool = True,
    now: datetime | None = None,
) -> ProductionActivationLiveHarnessRequest:
    """Build a safe harness request with opaque pipeline root token only."""
    normalized_activation = (activation_request_id or "").strip()
    normalized_ticket = (ticket_id or "").strip()
    normalized_confirmation = (confirmation_id or "").strip()
    if not normalized_activation:
        raise ProductionActivationLiveHarnessError("activation_request_id is required")
    if runner_profile != RUNNER_PROFILE_DISPATCH:
        raise ProductionActivationLiveHarnessError("runner_profile must be dispatch")
    if not draft_only:
        raise ProductionActivationLiveHarnessError("draft_only must be true")
    run_date_value = (run_date or _default_run_date(now)).strip()
    _assert_run_date(run_date_value)
    token = compute_pipeline_root_token(pipeline_root_resolved)
    return ProductionActivationLiveHarnessRequest(
        activation_request_id=normalized_activation,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        ticket_id=normalized_ticket,
        confirmation_id=normalized_confirmation,
        execution_gate_event_id=reservation.execution_gate_event_id,
        dry_run_event_id=reservation.dry_run_event_id,
        pipeline_root_token=token,
        runner_profile=runner_profile,
        timeout_seconds=timeout_seconds,
        run_date=run_date_value,
        draft_only=True,
        publish_allowed=False,
        production_execution_allowed=False,
        repository2_execution_attempted=False,
    )


def _harness_history_path(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise ProductionActivationLiveHarnessError("activation_request_id is required")
    base = (history_dir or default_harness_history_dir()).resolve()
    hermes_root = get_hermes_home().resolve()
    try:
        base.relative_to(hermes_root)
    except ValueError as exc:
        raise ProductionActivationLiveHarnessError(
            "Harness history dir must remain under Hermes home."
        ) from exc
    return base / f"{normalized}.json"


def probe_harness_audit_store_available(*, history_dir: Path | None = None) -> bool:
    try:
        base = (history_dir or default_harness_history_dir()).resolve()
        base.mkdir(parents=True, exist_ok=True)
        return os.access(base, os.W_OK)
    except OSError:
        return False


def _load_harness_records(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> list[ProductionActivationLiveHarnessRecord]:
    path = _harness_history_path(activation_request_id, history_dir=history_dir)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionActivationLiveHarnessError(
            "Harness audit store is corrupted."
        ) from exc
    if not isinstance(payload, dict):
        raise ProductionActivationLiveHarnessError("Harness audit store is corrupted.")
    records_raw = payload.get("records")
    if not isinstance(records_raw, list):
        raise ProductionActivationLiveHarnessError("Harness audit store is corrupted.")
    records: list[ProductionActivationLiveHarnessRecord] = []
    for item in records_raw:
        if not isinstance(item, dict):
            raise ProductionActivationLiveHarnessError("Harness audit store is corrupted.")
        records.append(
            ProductionActivationLiveHarnessRecord(
                event_id=str(item.get("event_id", "")),
                event_type=str(item.get("event_type", "")),
                activation_request_id=str(item.get("activation_request_id", "")),
                reservation_id=str(item.get("reservation_id", "")),
                execution_attempt_id=str(item.get("execution_attempt_id", "")),
                harness_key=str(item.get("harness_key", "")),
                result=str(item.get("result", "")),
                failure_reason_code=str(item.get("failure_reason_code", "")),
                timestamp=str(item.get("timestamp", "")),
                production_execution_allowed=bool(
                    item.get("production_execution_allowed", False)
                ),
                repository2_execution_attempted=bool(
                    item.get("repository2_execution_attempted", False)
                ),
                runtime_invoked=bool(item.get("runtime_invoked", False)),
            )
        )
    return records


def _atomic_append_harness_record(
    record: ProductionActivationLiveHarnessRecord,
    *,
    history_dir: Path | None = None,
) -> None:
    path = _harness_history_path(record.activation_request_id, history_dir=history_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductionActivationLiveHarnessError(
                "Harness audit store is corrupted."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ProductionActivationLiveHarnessError("Harness audit store is corrupted.")
        existing = payload["records"]
    entry = {
        "event_id": record.event_id,
        "event_type": record.event_type,
        "activation_request_id": record.activation_request_id,
        "reservation_id": record.reservation_id,
        "execution_attempt_id": record.execution_attempt_id,
        "harness_key": record.harness_key,
        "result": record.result,
        "failure_reason_code": record.failure_reason_code,
        "timestamp": record.timestamp,
        "production_execution_allowed": False,
        "repository2_execution_attempted": False,
        "runtime_invoked": False,
    }
    payload = {
        "version": _HARNESS_STORE_VERSION,
        "activation_request_id": record.activation_request_id,
        "records": [*existing, entry],
    }
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise ProductionActivationLiveHarnessError(
            "Harness audit write failed."
        ) from exc
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _find_harness_record_for_key(
    records: list[ProductionActivationLiveHarnessRecord],
    *,
    harness_key: str,
    event_type: str,
) -> ProductionActivationLiveHarnessRecord | None:
    for record in reversed(records):
        if record.harness_key == harness_key and record.event_type == event_type:
            return record
    return None


def _recommended_action_for_blocking(blocking: tuple[str, ...]) -> str:
    if not blocking:
        return ACTION_CONTINUE_TO_PHASE_14H_3C_2
    first = blocking[0]
    mapping = {
        BLOCK_RESERVATION_MISSING: ACTION_RESOLVE_RESERVATION,
        BLOCK_RESERVATION_NOT_RESERVED: ACTION_RESOLVE_RESERVATION,
        BLOCK_RESERVATION_SCOPE_MISMATCH: ACTION_RESOLVE_RESERVATION,
        BLOCK_EXECUTION_GATE_NOT_READY: ACTION_REFRESH_EXECUTION_GATE,
        BLOCK_DRY_RUN_NOT_READY: ACTION_REFRESH_PRODUCTION_DRY_RUN,
        BLOCK_MIRROR_ROOT_NOT_TRUSTED: ACTION_PREPARE_ISOLATED_PRODUCTION_MIRROR,
        BLOCK_PRODUCTION_ROOT_DENIED: ACTION_MAINTAIN_PRODUCTION_BLOCK,
        BLOCK_RUNNER_PROFILE_INVALID: ACTION_CONFIGURE_BOUNDED_RUNNER_CONTRACT,
        BLOCK_RUNNER_FACTORY_UNAVAILABLE: ACTION_CONFIGURE_BOUNDED_RUNNER_CONTRACT,
        BLOCK_ARGV_CONTRACT_INVALID: ACTION_RESOLVE_HARNESS_CONTRACT,
        BLOCK_CWD_CONTRACT_INVALID: ACTION_PREPARE_ISOLATED_PRODUCTION_MIRROR,
        BLOCK_ENV_CONTRACT_INVALID: ACTION_RESOLVE_HARNESS_CONTRACT,
        BLOCK_TIMEOUT_INVALID: ACTION_RESOLVE_HARNESS_CONTRACT,
        BLOCK_PUBLISH_NOT_ALLOWED: ACTION_MAINTAIN_PRODUCTION_BLOCK,
        BLOCK_ACTIVATION_NOT_ACTIVE: ACTION_SUSPEND_ACTIVE_ACTIVATION,
        BLOCK_ACTIVE_EXPIRED: ACTION_CREATE_NEW_ACTIVATION_PROPOSAL,
        BLOCK_PERMIT_NOT_READY: ACTION_RESOLVE_RESERVATION,
        BLOCK_RUNTIME_INVOKER_DISABLED: ACTION_MAINTAIN_PRODUCTION_BLOCK,
    }
    return mapping.get(first, ACTION_RESOLVE_HARNESS_CONTRACT)


def evaluate_live_harness_plan(
    *,
    request: ActivationRequest,
    harness_request: ProductionActivationLiveHarnessRequest,
    reservation: ProductionActivationExecutionReservation | None,
    pipeline_root: str,
    merged_config: Mapping[str, Any] | None = None,
    gate_history_dir: Path | None = None,
    dry_run_history_dir: Path | None = None,
    permit_ready: bool = True,
    runner_factory: ProductionLiveRunnerFactory | None = None,
    runtime_invoker: ProductionLiveRuntimeInvoker | None = None,
    proposed_env: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProductionActivationLiveHarnessPlan:
    """Evaluate harness contracts without runtime invocation."""
    blocking: list[str] = []
    factory = runner_factory or ConfigBoundRunnerFactoryAvailability()
    invoker = runtime_invoker or DisabledProductionLiveRuntimeInvoker()

    request_valid = bool(harness_request.activation_request_id)
    if not request_valid:
        blocking.append(BLOCK_ARGV_CONTRACT_INVALID)

    active_valid = request.state == ACTIVATION_STATE_ACTIVE
    if not active_valid:
        blocking.append(BLOCK_ACTIVATION_NOT_ACTIVE)
    else:
        expires_text = (request.active_expires_at or "").strip()
        if not expires_text or _utc_now(now) >= _parse_iso(expires_text):
            active_valid = False
            blocking.append(BLOCK_ACTIVE_EXPIRED)

    reservation_valid = reservation is not None
    if not reservation_valid:
        blocking.append(BLOCK_RESERVATION_MISSING)
    elif reservation.state != RESERVATION_STATE_RESERVED:
        reservation_valid = False
        blocking.append(BLOCK_RESERVATION_NOT_RESERVED)
    elif (
        reservation.reservation_id != harness_request.reservation_id
        or reservation.execution_attempt_id != harness_request.execution_attempt_id
        or reservation.execution_gate_event_id != harness_request.execution_gate_event_id
        or reservation.dry_run_event_id != harness_request.dry_run_event_id
    ):
        reservation_valid = False
        blocking.append(BLOCK_RESERVATION_SCOPE_MISMATCH)

    permit_valid = permit_ready
    if not permit_valid:
        blocking.append(BLOCK_PERMIT_NOT_READY)

    gate_valid = False
    mirror_valid = False
    argv_contract_valid = False
    cwd_contract_valid = False
    resolved_mirror = ""
    node_executable = _resolve_node_executable(merged_config)

    try:
        resolved_mirror = resolve_pipeline_root(pipeline_root)
        assert_pipeline_root_allowed(resolved_mirror)
        mirror_valid = _mirror_in_allowlist(resolved_mirror, merged_config=merged_config)
        if not mirror_valid:
            blocking.append(BLOCK_MIRROR_ROOT_NOT_TRUSTED)
    except ValueError as exc:
        message = str(exc).lower()
        blocking.append(
            BLOCK_PRODUCTION_ROOT_DENIED
            if "hard-denied" in message
            else BLOCK_MIRROR_ROOT_NOT_TRUSTED
        )

    gate_record = None
    if reservation_valid and resolved_mirror:
        gate_record = find_ready_execution_gate_record(
            harness_request.activation_request_id,
            gate_key=reservation.gate_key if reservation else "",
            history_dir=gate_history_dir,
        )
        gate_valid = (
            gate_record is not None
            and gate_record.event_id == harness_request.execution_gate_event_id
        )
        if not gate_valid:
            blocking.append(BLOCK_EXECUTION_GATE_NOT_READY)

    dry_run_record = find_dry_run_record(
        harness_request.activation_request_id,
        event_id=harness_request.dry_run_event_id,
        history_dir=dry_run_history_dir,
    )
    dry_run_ready = (
        dry_run_record is not None
        and dry_run_record.event_id == harness_request.dry_run_event_id
    )
    if not dry_run_ready:
        blocking.append(BLOCK_DRY_RUN_NOT_READY)

    if harness_request.publish_allowed:
        blocking.append(BLOCK_PUBLISH_NOT_ALLOWED)
    if _probe_publish_intent(harness_request.ticket_id):
        blocking.append(BLOCK_PUBLISH_NOT_ALLOWED)
    if not harness_request.draft_only:
        blocking.append(BLOCK_PUBLISH_NOT_ALLOWED)

    runner_profile_valid = harness_request.runner_profile == RUNNER_PROFILE_DISPATCH
    if not runner_profile_valid:
        blocking.append(BLOCK_RUNNER_PROFILE_INVALID)

    factory_available = False
    if runner_profile_valid and resolved_mirror:
        factory_available = factory.is_available(
            pipeline_root=resolved_mirror,
            runner_profile=harness_request.runner_profile,
            timeout_seconds=harness_request.timeout_seconds,
            merged_config=merged_config,
        )
    if not factory_available:
        blocking.append(BLOCK_RUNNER_FACTORY_UNAVAILABLE)

    if invoker.is_enabled():
        blocking.append(BLOCK_RUNTIME_INVOKER_DISABLED)

    if node_executable and resolved_mirror:
        planned_argv = [
            node_executable,
            "pipeline.js",
            "--run-date",
            harness_request.run_date,
        ]
        argv_contract_valid = validate_live_harness_argv_contract(
            planned_argv,
            node_executable=node_executable,
        )
    if not argv_contract_valid:
        blocking.append(BLOCK_ARGV_CONTRACT_INVALID)

    if resolved_mirror:
        cwd_contract_valid = validate_live_harness_cwd_contract(
            resolved_mirror,
            resolved_mirror=resolved_mirror,
            merged_config=merged_config,
        )
    if not cwd_contract_valid:
        blocking.append(BLOCK_CWD_CONTRACT_INVALID)

    env_contract_valid = validate_live_harness_env_contract(proposed_env)
    if not env_contract_valid:
        blocking.append(BLOCK_ENV_CONTRACT_INVALID)

    timeout_valid = _validate_timeout_seconds(
        harness_request.timeout_seconds,
        request=request,
        now=now,
    )
    if not timeout_valid:
        blocking.append(BLOCK_TIMEOUT_INVALID)

    deduped_blocking = tuple(dict.fromkeys(blocking))
    harness_ready = not deduped_blocking
    runtime_invocation_planned = harness_ready

    recommended = (
        ACTION_CONTINUE_TO_PHASE_14H_3C_2
        if harness_ready
        else _recommended_action_for_blocking(deduped_blocking)
    )

    return ProductionActivationLiveHarnessPlan(
        request_valid=request_valid,
        reservation_valid=reservation_valid,
        permit_valid=permit_valid,
        active_valid=active_valid,
        gate_valid=gate_valid,
        mirror_valid=mirror_valid,
        runner_profile_valid=runner_profile_valid,
        argv_contract_valid=argv_contract_valid,
        cwd_contract_valid=cwd_contract_valid,
        env_contract_valid=env_contract_valid,
        timeout_valid=timeout_valid,
        draft_only=True,
        publish_allowed=False,
        runtime_invocation_planned=runtime_invocation_planned,
        runtime_invoked=False,
        production_execution_allowed=False,
        repository2_execution_attempted=False,
        blocking_reasons=deduped_blocking,
        recommended_action=recommended,
    )


def run_live_harness_wiring(
    *,
    request: ActivationRequest,
    reservation: ProductionActivationExecutionReservation,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root: str,
    permit_ready: bool = True,
    merged_config: Mapping[str, Any] | None = None,
    gate_history_dir: Path | None = None,
    dry_run_history_dir: Path | None = None,
    harness_history_dir: Path | None = None,
    runner_factory: ProductionLiveRunnerFactory | None = None,
    runtime_invoker: ProductionLiveRuntimeInvoker | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    run_date: str | None = None,
    now: datetime | None = None,
) -> ProductionActivationLiveHarnessResult:
    """Evaluate harness wiring and stop at runtime boundary."""
    if not probe_harness_audit_store_available(history_dir=harness_history_dir):
        raise ProductionActivationLiveHarnessError("Harness audit store unavailable.")

    resolved_mirror = resolve_pipeline_root(pipeline_root)
    harness_request = build_live_harness_request(
        activation_request_id=request.activation_request_id,
        reservation=reservation,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root_resolved=resolved_mirror,
        timeout_seconds=timeout_seconds,
        run_date=run_date,
        now=now,
    )
    harness_key = compute_harness_key(harness_request)

    records = _load_harness_records(
        harness_request.activation_request_id,
        history_dir=harness_history_dir,
    )
    existing_ready = _find_harness_record_for_key(
        records,
        harness_key=harness_key,
        event_type=_EVENT_HARNESS_PLAN_READY,
    )
    if existing_ready is not None:
        plan = evaluate_live_harness_plan(
            request=request,
            harness_request=harness_request,
            reservation=reservation,
            pipeline_root=pipeline_root,
            merged_config=merged_config,
            gate_history_dir=gate_history_dir,
            dry_run_history_dir=dry_run_history_dir,
            permit_ready=permit_ready,
            runner_factory=runner_factory,
            runtime_invoker=runtime_invoker,
            now=now,
        )
        return ProductionActivationLiveHarnessResult(
            activation_request_id=harness_request.activation_request_id,
            reservation_id=harness_request.reservation_id,
            execution_attempt_id=harness_request.execution_attempt_id,
            runner_profile=harness_request.runner_profile,
            timeout_seconds=harness_request.timeout_seconds,
            harness_ready=plan.runtime_invocation_planned and not plan.blocking_reasons,
            already_evaluated=True,
            plan=plan,
            failure_reason_code=(
                BLOCK_RUNTIME_BLOCKED_WAITING_PHASE_14H_3C_2
                if plan.runtime_invocation_planned and not plan.blocking_reasons
                else (plan.blocking_reasons[0] if plan.blocking_reasons else "")
            ),
            recommended_action=ACTION_ALREADY_EVALUATED,
        )

    plan = evaluate_live_harness_plan(
        request=request,
        harness_request=harness_request,
        reservation=reservation,
        pipeline_root=pipeline_root,
        merged_config=merged_config,
        gate_history_dir=gate_history_dir,
        dry_run_history_dir=dry_run_history_dir,
        permit_ready=permit_ready,
        runner_factory=runner_factory,
        runtime_invoker=runtime_invoker,
        now=now,
    )

    _append_harness_event(
        event_type=_EVENT_HARNESS_PLAN_EVALUATED,
        harness_request=harness_request,
        harness_key=harness_key,
        result="ready" if not plan.blocking_reasons else "blocked",
        failure_reason_code=plan.blocking_reasons[0] if plan.blocking_reasons else "",
        history_dir=harness_history_dir,
        now=now,
    )

    harness_ready = not plan.blocking_reasons
    if harness_ready:
        _append_harness_event(
            event_type=_EVENT_HARNESS_PLAN_READY,
            harness_request=harness_request,
            harness_key=harness_key,
            result="ready",
            failure_reason_code="",
            history_dir=harness_history_dir,
            now=now,
        )
        _append_harness_event(
            event_type=_EVENT_RUNTIME_BLOCKED_WAITING,
            harness_request=harness_request,
            harness_key=harness_key,
            result="blocked",
            failure_reason_code=BLOCK_RUNTIME_BLOCKED_WAITING_PHASE_14H_3C_2,
            history_dir=harness_history_dir,
            now=now,
        )

    failure = (
        BLOCK_RUNTIME_BLOCKED_WAITING_PHASE_14H_3C_2
        if harness_ready
        else (plan.blocking_reasons[0] if plan.blocking_reasons else "")
    )
    recommended = (
        ACTION_CONTINUE_TO_PHASE_14H_3C_2
        if harness_ready
        else plan.recommended_action
    )

    return ProductionActivationLiveHarnessResult(
        activation_request_id=harness_request.activation_request_id,
        reservation_id=harness_request.reservation_id,
        execution_attempt_id=harness_request.execution_attempt_id,
        runner_profile=harness_request.runner_profile,
        timeout_seconds=harness_request.timeout_seconds,
        harness_ready=harness_ready,
        already_evaluated=False,
        plan=plan,
        failure_reason_code=failure,
        recommended_action=recommended,
    )


def _append_harness_event(
    *,
    event_type: str,
    harness_request: ProductionActivationLiveHarnessRequest,
    harness_key: str,
    result: str,
    failure_reason_code: str,
    history_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    record = ProductionActivationLiveHarnessRecord(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        activation_request_id=harness_request.activation_request_id,
        reservation_id=harness_request.reservation_id,
        execution_attempt_id=harness_request.execution_attempt_id,
        harness_key=harness_key,
        result=result,
        failure_reason_code=failure_reason_code,
        timestamp=_utc_now_iso(now),
        production_execution_allowed=False,
        repository2_execution_attempted=False,
        runtime_invoked=False,
    )
    _atomic_append_harness_record(record, history_dir=history_dir)


def load_harness_records(
    activation_request_id: str,
    *,
    history_dir: Path | None = None,
) -> list[ProductionActivationLiveHarnessRecord]:
    return _load_harness_records(activation_request_id, history_dir=history_dir)
