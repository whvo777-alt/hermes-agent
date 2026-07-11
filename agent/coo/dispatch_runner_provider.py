"""Bounded subprocess runner provider — Phase 12E-1 scaffold.

Defines a read-only provider boundary for future bounded subprocess runner
injection. The scaffold never returns a runner callable and never invokes
subprocess, auto-discovery, or environment-variable activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.production_executor_factory import SubprocessRunner

RUNNER_PROVIDER_MODE_SCAFFOLD = "scaffold"
RUNNER_PROVIDER_MODE_UNCONFIGURED = ""

REASON_RUNNER_PROVIDER_INVALID = "runner_provider_invalid"

_KNOWN_RUNNER_PROVIDER_CONFIG_KEYS = frozenset({"mode"})
_KNOWN_RUNNER_PROVIDER_MODES = frozenset({RUNNER_PROVIDER_MODE_SCAFFOLD})


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
    """Assess runner provider scaffold state without returning a callable."""
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

    if mode == RUNNER_PROVIDER_MODE_SCAFFOLD:
        return CooDispatchRunnerProviderSummary(
            runner_provider_configured=True,
            runner_provider_available=False,
            runner_provider_mode=RUNNER_PROVIDER_MODE_SCAFFOLD,
            provider_valid=True,
        )

    return CooDispatchRunnerProviderSummary(
        runner_provider_configured=False,
        runner_provider_available=False,
        runner_provider_mode=RUNNER_PROVIDER_MODE_UNCONFIGURED,
        provider_valid=False,
    )


def resolve_bounded_subprocess_runner(
    merged_config: Mapping[str, Any] | None = None,
) -> SubprocessRunner | None:
    """Resolve a bounded subprocess runner from provider config.

    Phase 12E-1 scaffold: always returns ``None`` even when provider config,
    binding, and executor policy would otherwise be ready. Real subprocess
    wiring is deferred to a later phase.
    """
    assessment = assess_dispatch_runner_provider(merged_config)
    if not assessment.provider_valid:
        return None
    return None


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
