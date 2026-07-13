"""CLI operator guidance — Phase 13Q."""

from __future__ import annotations

from agent.coo.dispatch_operator_guidance import (
    CooDispatchOperatorGuidance,
    OperatorGuidanceError,
    format_operator_guidance,
    resolve_operator_guidance,
)


def show_operator_guidance(recommended_action: str) -> CooDispatchOperatorGuidance:
    """Resolve guidance for one recommended_action code."""
    return resolve_operator_guidance(recommended_action)


def run_operator_guidance_show(recommended_action: str) -> tuple[str, int]:
    """Return formatted guidance output and CLI exit code."""
    try:
        guidance = show_operator_guidance(recommended_action)
    except OperatorGuidanceError:
        raise
    return format_operator_guidance(guidance), 0
