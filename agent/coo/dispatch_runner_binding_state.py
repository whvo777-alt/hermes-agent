"""Dispatch runner binding state — Phase 11I read-only / Phase 11J transitions.

Persists runner binding lifecycle under Hermes home without subprocess, runner
injection, or bound-state transitions. Missing state file means unbound.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home

RUNNER_BINDING_STATE_UNBOUND = "unbound"
RUNNER_BINDING_STATE_STAGED = "staged"
RUNNER_BINDING_STATE_BOUND = "bound"

REASON_RUNNER_BINDING_UNBOUND = "runner_binding_unbound"
REASON_RUNNER_BINDING_STAGED = "runner_binding_staged"
REASON_RUNNER_BINDING_STATE_INVALID = "runner_binding_state_invalid"

_KNOWN_BINDING_STATES = frozenset(
    {
        RUNNER_BINDING_STATE_UNBOUND,
        RUNNER_BINDING_STATE_STAGED,
        RUNNER_BINDING_STATE_BOUND,
    }
)
_STATE_FILE_VERSION = 1
_STATE_FILENAME = "dispatch-runner-binding.json"
_KNOWN_STATE_FILE_KEYS = frozenset(
    {"version", "state", "updated_at", "operator_id", "reason"}
)


class DispatchRunnerBindingStateError(ValueError):
    """Raised when runner binding state cannot be read safely."""


class DispatchRunnerBindingTransitionError(ValueError):
    """Raised when a runner binding transition is not permitted."""


@dataclass(frozen=True)
class CooDispatchRunnerBindingState:
    """Safe runner binding state snapshot."""

    state: str
    state_valid: bool = True
    updated_at: str = ""
    operator_id: str = ""
    reason: str = ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_runner_binding_state_path() -> Path:
    return get_hermes_home() / "coo" / _STATE_FILENAME


def _assert_path_within_hermes_home(
    resolved: Path,
    hermes_root: Path,
    *,
    label: str,
) -> None:
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise DispatchRunnerBindingStateError(
            f"Runner binding {label} must remain under Hermes home."
        ) from exc


def _validate_state_path(state_path: Path) -> Path:
    hermes_root = get_hermes_home().resolve()
    resolved = state_path.resolve()
    _assert_path_within_hermes_home(resolved, hermes_root, label="state file")
    return resolved


def _validate_parent_dir_within_hermes_home(state_path: Path) -> Path:
    hermes_root = get_hermes_home().resolve()
    parent = state_path.parent.resolve()
    _assert_path_within_hermes_home(parent, hermes_root, label="state directory")
    return parent


def _validate_operator_fields(operator_id: str, reason: str) -> None:
    if not isinstance(operator_id, str) or not operator_id.strip():
        raise ValueError("operator_id is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason is required")


def _parse_optional_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key, "")
    if value == "":
        return ""
    if not isinstance(value, str):
        raise DispatchRunnerBindingStateError(
            f"Runner binding state field {key} must be a string."
        )
    return value.strip()


def _parse_runner_binding_payload(payload: Mapping[str, Any]) -> CooDispatchRunnerBindingState:
    if not isinstance(payload, Mapping):
        raise DispatchRunnerBindingStateError("Runner binding state must be a mapping.")

    unknown_keys = set(payload) - _KNOWN_STATE_FILE_KEYS
    if unknown_keys:
        raise DispatchRunnerBindingStateError(
            "Runner binding state contains unknown fields."
        )

    version = payload.get("version")
    if version != _STATE_FILE_VERSION:
        raise DispatchRunnerBindingStateError("Runner binding state version is invalid.")

    state = payload.get("state")
    if not isinstance(state, str) or not state.strip():
        raise DispatchRunnerBindingStateError("Runner binding state value is required.")
    normalized = state.strip().lower()
    if normalized not in _KNOWN_BINDING_STATES:
        raise DispatchRunnerBindingStateError("Runner binding state value is unknown.")

    return CooDispatchRunnerBindingState(
        state=normalized,
        state_valid=True,
        updated_at=_parse_optional_string(payload, "updated_at"),
        operator_id=_parse_optional_string(payload, "operator_id"),
        reason=_parse_optional_string(payload, "reason"),
    )


def _binding_state_payload(
    *,
    state: str,
    operator_id: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "version": _STATE_FILE_VERSION,
        "state": state,
        "updated_at": _utc_now_iso(),
        "operator_id": operator_id.strip(),
        "reason": reason.strip(),
    }


def _atomic_write_binding_state(path: Path, payload: Mapping[str, Any]) -> None:
    resolved_path = _validate_state_path(path)
    _validate_parent_dir_within_hermes_home(resolved_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = resolved_path.with_name(f".{resolved_path.name}.{uuid.uuid4().hex}.tmp")
    _assert_path_within_hermes_home(
        tmp_path.resolve(),
        get_hermes_home().resolve(),
        label="temporary state file",
    )
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, resolved_path)


def _write_binding_state(
    *,
    state: str,
    operator_id: str,
    reason: str,
    state_path: Path | None = None,
) -> CooDispatchRunnerBindingState:
    path = _validate_state_path(state_path or default_runner_binding_state_path())
    payload = _binding_state_payload(
        state=state,
        operator_id=operator_id,
        reason=reason,
    )
    _atomic_write_binding_state(path, payload)
    return _parse_runner_binding_payload(payload)


def load_dispatch_runner_binding_state(
    state_path: Path | None = None,
) -> CooDispatchRunnerBindingState:
    """Load runner binding state from Hermes home. Missing file means unbound."""
    resolved_path = _validate_state_path(state_path or default_runner_binding_state_path())
    if not resolved_path.exists():
        return CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_UNBOUND)

    try:
        raw = resolved_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchRunnerBindingStateError(
            "Runner binding state file is unreadable or corrupted."
        ) from exc

    return _parse_runner_binding_payload(payload)


def stage_dispatch_runner_binding(
    *,
    operator_id: str,
    reason: str,
    state_path: Path | None = None,
) -> CooDispatchRunnerBindingState:
    """Transition unbound → staged. Idempotent when already staged."""
    _validate_operator_fields(operator_id, reason)
    current = load_dispatch_runner_binding_state(state_path)
    if current.state == RUNNER_BINDING_STATE_BOUND:
        raise DispatchRunnerBindingTransitionError(
            "Runner binding is bound; stage is not permitted."
        )
    if current.state == RUNNER_BINDING_STATE_STAGED:
        return current
    return _write_binding_state(
        state=RUNNER_BINDING_STATE_STAGED,
        operator_id=operator_id,
        reason=reason,
        state_path=state_path,
    )


def reset_dispatch_runner_binding(
    *,
    operator_id: str,
    reason: str,
    state_path: Path | None = None,
) -> CooDispatchRunnerBindingState:
    """Transition staged → unbound. Idempotent when already unbound."""
    _validate_operator_fields(operator_id, reason)
    current = load_dispatch_runner_binding_state(state_path)
    if current.state == RUNNER_BINDING_STATE_BOUND:
        raise DispatchRunnerBindingTransitionError(
            "Runner binding is bound; reset is not permitted."
        )
    if current.state == RUNNER_BINDING_STATE_UNBOUND:
        return current
    return _write_binding_state(
        state=RUNNER_BINDING_STATE_UNBOUND,
        operator_id=operator_id,
        reason=reason,
        state_path=state_path,
    )


def runner_binding_state_is_bound(binding: CooDispatchRunnerBindingState) -> bool:
    return binding.state_valid and binding.state == RUNNER_BINDING_STATE_BOUND


def validate_dispatch_runner_binding_for_run(
    binding_state: CooDispatchRunnerBindingState | None = None,
) -> CooDispatchRunnerBindingState:
    """Fail-closed unless runner binding state is bound."""
    try:
        binding = (
            binding_state
            if binding_state is not None
            else load_dispatch_runner_binding_state()
        )
    except DispatchRunnerBindingStateError as exc:
        raise ValueError(
            f"runner binding gate failed: {REASON_RUNNER_BINDING_STATE_INVALID}"
        ) from exc

    if binding.state == RUNNER_BINDING_STATE_BOUND:
        return binding
    if binding.state == RUNNER_BINDING_STATE_STAGED:
        raise ValueError(f"runner binding gate failed: {REASON_RUNNER_BINDING_STAGED}")
    raise ValueError(f"runner binding gate failed: {REASON_RUNNER_BINDING_UNBOUND}")


def format_runner_binding_gate_failure(reason: str) -> str:
    """Render a safe runner binding gate failure without paths or secrets."""
    return f"runner binding gate failed: {reason}"


def format_runner_binding_state_summary(binding: CooDispatchRunnerBindingState) -> str:
    """Render a safe binding-state summary without paths, secrets, or commands."""
    if not binding.state_valid:
        rendered_state = "invalid"
    else:
        rendered_state = binding.state
    lines = [
        f"runner_binding_state: {rendered_state}",
        f"runner_bound: {str(runner_binding_state_is_bound(binding)).lower()}",
    ]
    return "\n".join(lines)
