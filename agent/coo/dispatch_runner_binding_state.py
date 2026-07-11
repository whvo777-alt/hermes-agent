"""Dispatch runner binding state — Phase 11I read-only state model.

Persists runner binding lifecycle under Hermes home without subprocess, runner
injection, or automatic state transitions. Missing state file means unbound.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home

RUNNER_BINDING_STATE_UNBOUND = "unbound"
RUNNER_BINDING_STATE_STAGED = "staged"
RUNNER_BINDING_STATE_BOUND = "bound"

_KNOWN_BINDING_STATES = frozenset(
    {
        RUNNER_BINDING_STATE_UNBOUND,
        RUNNER_BINDING_STATE_STAGED,
        RUNNER_BINDING_STATE_BOUND,
    }
)
_STATE_FILE_VERSION = 1
_STATE_FILENAME = "dispatch-runner-binding.json"
_KNOWN_STATE_FILE_KEYS = frozenset({"version", "state"})


class DispatchRunnerBindingStateError(ValueError):
    """Raised when runner binding state cannot be read safely."""


@dataclass(frozen=True)
class CooDispatchRunnerBindingState:
    """Safe read-only runner binding state snapshot."""

    state: str
    state_valid: bool = True


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

    return CooDispatchRunnerBindingState(state=normalized, state_valid=True)


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


def runner_binding_state_is_bound(binding: CooDispatchRunnerBindingState) -> bool:
    return binding.state_valid and binding.state == RUNNER_BINDING_STATE_BOUND


def format_runner_binding_state_summary(binding: CooDispatchRunnerBindingState) -> str:
    """Render a safe binding-state summary without paths, secrets, or commands."""
    if not binding.state_valid:
        rendered_state = "invalid"
    else:
        rendered_state = binding.state
    return f"runner_binding_state: {rendered_state}"
