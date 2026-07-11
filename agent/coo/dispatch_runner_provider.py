"""Bounded subprocess runner provider — Phase 12E-1 / 12E-2 / 12E-5.

Defines a read-only provider boundary and opt-in resolution for bounded subprocess
runner injection. Callable return requires explicit ``injected_runner`` (mock path)
or ``use_real_bounded_runner=True`` (real harness path). Never auto-wires into the
default CLI run path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.production_executor_factory import SubprocessRunner

RUNNER_PROVIDER_MODE_SCAFFOLD = "scaffold"
RUNNER_PROVIDER_MODE_BOUNDED = "bounded"
RUNNER_PROVIDER_MODE_UNCONFIGURED = ""

REASON_RUNNER_PROVIDER_INVALID = "runner_provider_invalid"
REASON_RUNNER_PROVIDER_MODE_NOT_BOUNDED = "runner_provider_mode_not_bounded"
REASON_RUNNER_PROVIDER_INJECTED_RUNNER_REQUIRED = "runner_provider_injected_runner_required"
REASON_RUNNER_PROVIDER_INJECTED_RUNNER_INVALID = "runner_provider_injected_runner_invalid"
REASON_RUNNER_PROVIDER_REAL_HARNESS_AMBIGUOUS = "runner_provider_real_harness_ambiguous"
REASON_RUNNER_BINDING_UNBOUND = "runner_binding_unbound"
REASON_RUNNER_BINDING_STAGED = "runner_binding_staged"
REASON_RUNNER_BINDING_STATE_INVALID = "runner_binding_state_invalid"

DEFAULT_REAL_HARNESS_MAX_OUTPUT_BYTES = 64_000
DEFAULT_REAL_HARNESS_MAX_TIMEOUT_SECONDS = 300

_KNOWN_RUNNER_PROVIDER_CONFIG_KEYS = frozenset({"mode"})
_KNOWN_RUNNER_PROVIDER_MODES = frozenset(
    {RUNNER_PROVIDER_MODE_SCAFFOLD, RUNNER_PROVIDER_MODE_BOUNDED}
)


class DispatchRunnerProviderResolutionError(ValueError):
    """Raised when bounded runner provider resolution is rejected."""

    def __init__(
        self,
        reason: str,
        *,
        blocked_reasons: tuple[str, ...] = (),
    ) -> None:
        self.reason = reason
        self.blocked_reasons = blocked_reasons or (reason,)
        super().__init__(
            format_dispatch_runner_provider_resolution_failure(self.blocked_reasons)
        )


@dataclass(frozen=True)
class CooDispatchRunnerProviderSummary:
    """Safe read-only runner provider assessment without paths or secrets."""

    runner_provider_configured: bool
    runner_provider_available: bool
    runner_provider_mode: str
    provider_valid: bool = True


def _runner_provider_config_section(
    merged_config: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if merged_config is None:
        return None
    if not isinstance(merged_config, Mapping):
        raise ValueError("Hermes config must be a mapping.")
    coo = merged_config.get("coo")
    if coo is None:
        return None
    if not isinstance(coo, dict):
        raise ValueError("config coo section must be a mapping.")
    dispatch = coo.get("dispatch")
    if dispatch is None:
        return None
    if not isinstance(dispatch, dict):
        raise ValueError("config coo.dispatch section must be a mapping.")
    runner_provider = dispatch.get("runner_provider")
    if runner_provider is None:
        return None
    if not isinstance(runner_provider, dict):
        raise ValueError("config coo.dispatch.runner_provider section must be a mapping.")
    return runner_provider


def assess_dispatch_runner_provider(
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchRunnerProviderSummary:
    """Assess runner provider state without returning a callable."""
    try:
        section = _runner_provider_config_section(merged_config)
    except ValueError:
        return CooDispatchRunnerProviderSummary(
            runner_provider_configured=False,
            runner_provider_available=False,
            runner_provider_mode=RUNNER_PROVIDER_MODE_UNCONFIGURED,
            provider_valid=False,
        )

    if section is None:
        return CooDispatchRunnerProviderSummary(
            runner_provider_configured=False,
            runner_provider_available=False,
            runner_provider_mode=RUNNER_PROVIDER_MODE_UNCONFIGURED,
            provider_valid=True,
        )

    unknown_keys = set(section) - _KNOWN_RUNNER_PROVIDER_CONFIG_KEYS
    if unknown_keys:
        return CooDispatchRunnerProviderSummary(
            runner_provider_configured=False,
            runner_provider_available=False,
            runner_provider_mode=RUNNER_PROVIDER_MODE_UNCONFIGURED,
            provider_valid=False,
        )

    mode_raw = section.get("mode")
    if mode_raw is None:
        return CooDispatchRunnerProviderSummary(
            runner_provider_configured=False,
            runner_provider_available=False,
            runner_provider_mode=RUNNER_PROVIDER_MODE_UNCONFIGURED,
            provider_valid=False,
        )
    if not isinstance(mode_raw, str) or not mode_raw.strip():
        return CooDispatchRunnerProviderSummary(
            runner_provider_configured=False,
            runner_provider_available=False,
            runner_provider_mode=RUNNER_PROVIDER_MODE_UNCONFIGURED,
            provider_valid=False,
        )

    mode = mode_raw.strip().lower()
    if mode not in _KNOWN_RUNNER_PROVIDER_MODES:
        return CooDispatchRunnerProviderSummary(
            runner_provider_configured=False,
            runner_provider_available=False,
            runner_provider_mode=RUNNER_PROVIDER_MODE_UNCONFIGURED,
            provider_valid=False,
        )

    if mode in (RUNNER_PROVIDER_MODE_SCAFFOLD, RUNNER_PROVIDER_MODE_BOUNDED):
        return CooDispatchRunnerProviderSummary(
            runner_provider_configured=True,
            runner_provider_available=False,
            runner_provider_mode=mode,
            provider_valid=True,
        )

    return CooDispatchRunnerProviderSummary(
        runner_provider_configured=False,
        runner_provider_available=False,
        runner_provider_mode=RUNNER_PROVIDER_MODE_UNCONFIGURED,
        provider_valid=False,
    )


def _raise_resolution_failure(
    reason: str,
    *,
    blocked_reasons: tuple[str, ...] = (),
) -> None:
    raise DispatchRunnerProviderResolutionError(
        reason,
        blocked_reasons=blocked_reasons or (reason,),
    )


def _resolve_binding_state(
    binding_state: Any | None,
) -> Any:
    from agent.coo.dispatch_runner_binding_state import (
        RUNNER_BINDING_STATE_BOUND,
        RUNNER_BINDING_STATE_STAGED,
        DispatchRunnerBindingStateError,
        load_dispatch_runner_binding_state,
    )

    try:
        binding = (
            binding_state
            if binding_state is not None
            else load_dispatch_runner_binding_state()
        )
    except DispatchRunnerBindingStateError as exc:
        raise DispatchRunnerProviderResolutionError(
            REASON_RUNNER_BINDING_STATE_INVALID,
        ) from exc

    if binding.state == RUNNER_BINDING_STATE_BOUND:
        return binding
    if binding.state == RUNNER_BINDING_STATE_STAGED:
        _raise_resolution_failure(REASON_RUNNER_BINDING_STAGED)
    _raise_resolution_failure(REASON_RUNNER_BINDING_UNBOUND)
    return binding


def _assert_runtime_enablement_ready(
    merged_config: Mapping[str, Any] | None,
) -> None:
    from agent.coo.dispatch_cli_enablement import evaluate_dispatch_runtime_enablement

    runtime_enablement = evaluate_dispatch_runtime_enablement(merged_config)
    if not runtime_enablement.enablement_ready:
        _raise_resolution_failure(
            runtime_enablement.blocked_reasons[0]
            if runtime_enablement.blocked_reasons
            else REASON_RUNNER_PROVIDER_INVALID,
            blocked_reasons=runtime_enablement.blocked_reasons,
        )


def _load_validated_executor_allowlist(
    merged_config: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    from agent.coo.dispatch_executor_config import load_dispatch_executor_policy

    policy = load_dispatch_executor_policy(merged_config)
    if not policy.enabled:
        _raise_resolution_failure("executor_disabled")
    if not policy.allowed_pipeline_roots:
        _raise_resolution_failure("executor_allowlist_empty")
    return tuple(policy.allowed_pipeline_roots)


def resolve_bounded_subprocess_runner(
    merged_config: Mapping[str, Any] | None = None,
    *,
    injected_runner: SubprocessRunner | None = None,
    binding_state: Any | None = None,
    use_real_bounded_runner: bool = False,
    harness_max_output_bytes: int = DEFAULT_REAL_HARNESS_MAX_OUTPUT_BYTES,
    harness_max_timeout_seconds: int = DEFAULT_REAL_HARNESS_MAX_TIMEOUT_SECONDS,
) -> SubprocessRunner:
    """Resolve a bounded subprocess runner via explicit mock or real-harness opt-in."""
    assessment = assess_dispatch_runner_provider(merged_config)
    if not assessment.provider_valid:
        _raise_resolution_failure(REASON_RUNNER_PROVIDER_INVALID)

    if assessment.runner_provider_mode != RUNNER_PROVIDER_MODE_BOUNDED:
        _raise_resolution_failure(REASON_RUNNER_PROVIDER_MODE_NOT_BOUNDED)

    _resolve_binding_state(binding_state)
    _assert_runtime_enablement_ready(merged_config)

    if injected_runner is not None:
        if use_real_bounded_runner:
            _raise_resolution_failure(REASON_RUNNER_PROVIDER_REAL_HARNESS_AMBIGUOUS)
        if not callable(injected_runner):
            _raise_resolution_failure(REASON_RUNNER_PROVIDER_INJECTED_RUNNER_INVALID)
        return injected_runner

    if not use_real_bounded_runner:
        _raise_resolution_failure(REASON_RUNNER_PROVIDER_INJECTED_RUNNER_REQUIRED)

    if harness_max_output_bytes <= 0 or harness_max_timeout_seconds <= 0:
        _raise_resolution_failure(REASON_RUNNER_PROVIDER_INVALID)

    from agent.coo.bounded_subprocess_runner import create_bounded_subprocess_runner

    allowlist = _load_validated_executor_allowlist(merged_config)
    return create_bounded_subprocess_runner(
        allowlist,
        max_output_bytes=harness_max_output_bytes,
        max_timeout_seconds=harness_max_timeout_seconds,
    )


def format_dispatch_runner_provider_resolution_failure(
    blocked_reasons: tuple[str, ...],
) -> str:
    """Render a safe provider resolution failure without paths or secrets."""
    if not blocked_reasons:
        return "runner provider resolution failed"
    return f"runner provider resolution failed: {','.join(blocked_reasons)}"


def format_dispatch_runner_provider_summary(
    summary: CooDispatchRunnerProviderSummary,
) -> str:
    """Render a safe provider summary without paths, commands, or secrets."""
    mode = summary.runner_provider_mode or RUNNER_PROVIDER_MODE_UNCONFIGURED
    return "\n".join(
        (
            f"runner_provider_configured: {str(summary.runner_provider_configured).lower()}",
            f"runner_provider_available: {str(summary.runner_provider_available).lower()}",
            f"runner_provider_mode: {mode}",
        )
    )
