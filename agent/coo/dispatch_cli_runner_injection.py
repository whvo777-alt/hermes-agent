"""Dispatch CLI runner injection boundary — Phase 12D / 12E-1 / 12E-2.

Production dispatch never invokes subprocess by default. Non-dry execution
requires an explicitly injected ``subprocess_runner`` callable (tests only).
The Hermes CLI default path does not inject a runner and remains fail-closed.

Bounded provider resolution (``resolve_bounded_subprocess_runner``) supports
opt-in return of an explicitly injected callable when ``mode=bounded`` and all
gates pass. It is not wired into the default CLI run path.
"""

from __future__ import annotations

from agent.coo.dispatch_runner_provider import resolve_bounded_subprocess_runner
from agent.coo.production_executor_factory import SubprocessRunner

DISPATCH_RUNNER_NOT_CONFIGURED = "production runner is not configured"

__all__ = (
    "DISPATCH_RUNNER_NOT_CONFIGURED",
    "SubprocessRunner",
    "require_dispatch_subprocess_runner",
    "resolve_bounded_subprocess_runner",
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
