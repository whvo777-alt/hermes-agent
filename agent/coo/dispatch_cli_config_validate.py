"""CLI dispatch executor config validation — Phase 10Y.

Read-only validation of ``coo.dispatch.executor`` from merged Hermes config.
No config writes, subprocess, bundle access, or dispatch execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.dispatch_executor_config import load_dispatch_executor_policy


@dataclass(frozen=True)
class CooDispatchExecutorConfigValidationSummary:
    """Safe read-only summary of validated dispatch executor config."""

    executor_enabled: bool
    executor_allowlist_count: int
    config_valid: bool = True


def validate_dispatch_executor_config(
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchExecutorConfigValidationSummary:
    """Load and validate dispatch executor policy from merged Hermes config."""
    policy = load_dispatch_executor_policy(merged_config)
    return CooDispatchExecutorConfigValidationSummary(
        executor_enabled=policy.enabled,
        executor_allowlist_count=len(policy.allowed_pipeline_roots),
        config_valid=True,
    )


def format_dispatch_executor_config_validation_summary(
    summary: CooDispatchExecutorConfigValidationSummary,
) -> str:
    """Render a safe validation summary without allowlist paths or secrets."""
    return "\n".join(
        [
            f"executor_enabled: {str(summary.executor_enabled).lower()}",
            f"executor_allowlist_count: {summary.executor_allowlist_count}",
            f"config_valid: {str(summary.config_valid).lower()}",
        ]
    )
