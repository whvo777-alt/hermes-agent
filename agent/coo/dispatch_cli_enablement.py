"""CLI dispatch enablement gate — Phase 11H.

Read-only assessment of whether production runner binding may proceed without
mutating config or invoking subprocess, factory, or runner execution.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.dispatch_executor_config import load_dispatch_executor_policy
from agent.coo.dispatch_pipeline_root_trust import assert_pipeline_root_allowed
from agent.coo.dispatch_runner_binding_state import (
    CooDispatchRunnerBindingState,
    DispatchRunnerBindingStateError,
    RUNNER_BINDING_STATE_BOUND,
    RUNNER_BINDING_STATE_STAGED,
    load_dispatch_runner_binding_state,
    runner_binding_state_is_bound,
)
from agent.coo.production_executor_policy import ProductionExecutorPolicy

REASON_EXECUTOR_CONFIG_INVALID = "executor_config_invalid"
REASON_EXECUTOR_DISABLED = "executor_disabled"
REASON_EXECUTOR_ALLOWLIST_EMPTY = "executor_allowlist_empty"
REASON_PRODUCTION_ROOT_IN_ALLOWLIST = "production_root_in_allowlist"
REASON_RUNNER_ALREADY_BOUND = "runner_already_bound"
REASON_RUNNER_BINDING_STAGED = "runner_binding_staged"
REASON_RUNNER_BINDING_STATE_INVALID = "runner_binding_state_invalid"
REASON_RUNNER_BINDING_UNBOUND = "runner_binding_unbound"
REASON_READINESS_PREFLIGHT_UNAVAILABLE = "readiness_preflight_unavailable"

RUNNER_BINDING_STATE_INVALID = "invalid"


@dataclass(frozen=True)
class CooDispatchEnablementSummary:
    """Safe read-only enablement assessment for operator review."""

    enablement_ready: bool
    runner_bound: bool
    runner_binding_state: str
    blocked_reasons: tuple[str, ...] = ()


def _readiness_preflight_system_available() -> bool:
    """Verify readiness and preflight modules are importable and callable."""
    try:
        from agent.coo.dispatch_cli_preflight import run_dispatch_policy_preflight
        from agent.coo.dispatch_cli_readiness import evaluate_dispatch_operator_readiness
        from agent.coo.dispatch_cli_validation_core import validate_dispatch_pre_run
    except ImportError:
        return False
    return all(
        callable(item)
        for item in (
            validate_dispatch_pre_run,
            run_dispatch_policy_preflight,
            evaluate_dispatch_operator_readiness,
        )
    )


def _production_root_in_allowlist(policy: ProductionExecutorPolicy) -> bool:
    for path in policy.allowed_pipeline_roots:
        resolved = os.path.realpath(os.path.expanduser(path))
        try:
            assert_pipeline_root_allowed(resolved)
        except ValueError:
            return True
    return False


def _resolve_runner_binding_state(
    binding_state: CooDispatchRunnerBindingState | None,
) -> tuple[CooDispatchRunnerBindingState | None, str | None]:
    if binding_state is not None:
        return binding_state, None
    try:
        return load_dispatch_runner_binding_state(), None
    except DispatchRunnerBindingStateError:
        return None, REASON_RUNNER_BINDING_STATE_INVALID


def evaluate_dispatch_enablement(
    merged_config: Mapping[str, Any] | None = None,
    *,
    binding_state: CooDispatchRunnerBindingState | None = None,
) -> CooDispatchEnablementSummary:
    """Assess enablement readiness without binding a runner or mutating config."""
    blocked: list[str] = []

    try:
        policy = load_dispatch_executor_policy(merged_config)
    except ValueError:
        policy = None
        blocked.append(REASON_EXECUTOR_CONFIG_INVALID)

    if policy is not None:
        if not policy.enabled:
            blocked.append(REASON_EXECUTOR_DISABLED)
        if not policy.allowed_pipeline_roots:
            blocked.append(REASON_EXECUTOR_ALLOWLIST_EMPTY)
        elif _production_root_in_allowlist(policy):
            blocked.append(REASON_PRODUCTION_ROOT_IN_ALLOWLIST)

    binding, binding_error = _resolve_runner_binding_state(binding_state)
    if binding_error is not None:
        blocked.append(binding_error)
        rendered_binding_state = RUNNER_BINDING_STATE_INVALID
        resolved_runner_bound = False
    elif binding is not None:
        rendered_binding_state = binding.state
        resolved_runner_bound = runner_binding_state_is_bound(binding)
        if binding.state == RUNNER_BINDING_STATE_BOUND:
            blocked.append(REASON_RUNNER_ALREADY_BOUND)
        elif binding.state == RUNNER_BINDING_STATE_STAGED:
            blocked.append(REASON_RUNNER_BINDING_STAGED)
    else:
        rendered_binding_state = RUNNER_BINDING_STATE_INVALID
        resolved_runner_bound = False

    if not _readiness_preflight_system_available():
        blocked.append(REASON_READINESS_PREFLIGHT_UNAVAILABLE)

    return CooDispatchEnablementSummary(
        enablement_ready=not blocked,
        runner_bound=resolved_runner_bound,
        runner_binding_state=rendered_binding_state,
        blocked_reasons=tuple(blocked),
    )


def evaluate_dispatch_runtime_enablement(
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchEnablementSummary:
    """Config-only enablement gate for pre-run validation (binding checked separately)."""
    blocked: list[str] = []

    try:
        policy = load_dispatch_executor_policy(merged_config)
    except ValueError:
        policy = None
        blocked.append(REASON_EXECUTOR_CONFIG_INVALID)

    if policy is not None:
        if not policy.enabled:
            blocked.append(REASON_EXECUTOR_DISABLED)
        if not policy.allowed_pipeline_roots:
            blocked.append(REASON_EXECUTOR_ALLOWLIST_EMPTY)
        elif _production_root_in_allowlist(policy):
            blocked.append(REASON_PRODUCTION_ROOT_IN_ALLOWLIST)

    if not _readiness_preflight_system_available():
        blocked.append(REASON_READINESS_PREFLIGHT_UNAVAILABLE)

    return CooDispatchEnablementSummary(
        enablement_ready=not blocked,
        runner_bound=False,
        runner_binding_state="",
        blocked_reasons=tuple(blocked),
    )


def format_dispatch_runtime_enablement_failure(blocked_reasons: tuple[str, ...]) -> str:
    """Render a safe runtime enablement failure without paths or secrets."""
    if not blocked_reasons:
        return "dispatch enablement gate failed"
    return f"dispatch enablement gate failed: {','.join(blocked_reasons)}"


def format_dispatch_enablement_summary(summary: CooDispatchEnablementSummary) -> str:
    """Render a safe enablement summary without paths, secrets, or policy details."""
    lines = [
        f"enablement_ready: {str(summary.enablement_ready).lower()}",
        f"runner_bound: {str(summary.runner_bound).lower()}",
        f"runner_binding_state: {summary.runner_binding_state}",
    ]
    if summary.blocked_reasons:
        lines.append(f"blocked_reasons: {','.join(summary.blocked_reasons)}")
    return "\n".join(lines)
