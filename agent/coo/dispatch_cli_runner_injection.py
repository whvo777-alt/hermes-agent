"""Dispatch CLI runner injection boundary — Phase 12D.

Production dispatch never invokes subprocess by default. Non-dry execution
requires an explicitly injected ``subprocess_runner`` callable (tests only).
The Hermes CLI default path does not inject a runner and remains fail-closed.
"""

from __future__ import annotations

from agent.coo.production_executor_factory import SubprocessRunner

DISPATCH_RUNNER_NOT_CONFIGURED = "production runner is not configured"


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
