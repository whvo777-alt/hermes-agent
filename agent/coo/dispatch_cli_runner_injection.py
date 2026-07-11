"""Dispatch CLI runner injection boundary — Phase 12D / 12E-1 / 12E-2 / 12E-3 / 12E-5.

Production dispatch never invokes subprocess by default. Non-dry execution
requires an explicitly injected mock runner, provider opt-in with mock injection,
or internal ``use_real_bounded_runner=True`` harness opt-in. The Hermes CLI default
path does not inject a runner and remains fail-closed.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.coo.bounded_subprocess_runner import (
    RUNNER_PROFILE_DISPATCH,
    RUNNER_PROFILE_RESTRICTED,
    create_bounded_subprocess_runner,
)
from agent.coo.dispatch_runner_provider import (
    DEFAULT_REAL_HARNESS_MAX_OUTPUT_BYTES,
    DEFAULT_REAL_HARNESS_MAX_TIMEOUT_SECONDS,
    resolve_bounded_subprocess_runner,
)
from agent.coo.production_executor_factory import SubprocessRunner

DISPATCH_RUNNER_NOT_CONFIGURED = "production runner is not configured"
REASON_AMBIGUOUS_RUNNER_INJECTION = "ambiguous_runner_injection"

__all__ = (
    "DEFAULT_REAL_HARNESS_MAX_OUTPUT_BYTES",
    "DEFAULT_REAL_HARNESS_MAX_TIMEOUT_SECONDS",
    "DISPATCH_RUNNER_NOT_CONFIGURED",
    "REASON_AMBIGUOUS_RUNNER_INJECTION",
    "RUNNER_PROFILE_DISPATCH",
    "RUNNER_PROFILE_RESTRICTED",
    "SubprocessRunner",
    "create_bounded_subprocess_runner",
    "require_dispatch_subprocess_runner",
    "resolve_bounded_subprocess_runner",
    "resolve_dispatch_run_subprocess_runner",
)


def require_dispatch_subprocess_runner(
    subprocess_runner: SubprocessRunner | None,
    *,
    dry_run: bool,
) -> SubprocessRunner | None:
    """Return the injected runner or fail-closed when non-dry execution lacks one."""
    if dry_run:
        return subprocess_runner
    if subprocess_runner is None:
        raise ValueError(DISPATCH_RUNNER_NOT_CONFIGURED)
    return subprocess_runner


def format_ambiguous_runner_injection_failure() -> str:
    """Render a safe ambiguous injection failure without paths or secrets."""
    return f"runner injection failed: {REASON_AMBIGUOUS_RUNNER_INJECTION}"


def resolve_dispatch_run_subprocess_runner(
    *,
    subprocess_runner: SubprocessRunner | None = None,
    injected_runner: SubprocessRunner | None = None,
    use_runner_provider: bool = False,
    use_real_bounded_runner: bool = False,
    dry_run: bool = False,
    merged_config: Mapping[str, Any] | None = None,
    binding_state: Any | None = None,
    harness_profile: str = RUNNER_PROFILE_RESTRICTED,
    node_executable: str | None = None,
    harness_max_output_bytes: int = DEFAULT_REAL_HARNESS_MAX_OUTPUT_BYTES,
    harness_max_timeout_seconds: int = DEFAULT_REAL_HARNESS_MAX_TIMEOUT_SECONDS,
) -> SubprocessRunner | None:
    """Resolve the subprocess runner for a dispatch run service entry."""
    if dry_run:
        return subprocess_runner

    if use_runner_provider and subprocess_runner is not None:
        raise ValueError(format_ambiguous_runner_injection_failure())

    if use_runner_provider:
        config = merged_config
        if config is None:
            from hermes_cli.config import load_config

            config = load_config()
        binding = binding_state
        if binding is None:
            from agent.coo.dispatch_runner_binding_state import (
                load_dispatch_runner_binding_state,
            )

            binding = load_dispatch_runner_binding_state()
        return resolve_bounded_subprocess_runner(
            config,
            injected_runner=injected_runner,
            binding_state=binding,
            use_real_bounded_runner=use_real_bounded_runner,
            harness_profile=harness_profile,
            node_executable=node_executable,
            harness_max_output_bytes=harness_max_output_bytes,
            harness_max_timeout_seconds=harness_max_timeout_seconds,
        )

    return require_dispatch_subprocess_runner(subprocess_runner, dry_run=False)
