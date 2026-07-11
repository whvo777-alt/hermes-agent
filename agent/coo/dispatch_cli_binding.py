"""CLI dispatch runner binding commands — Phase 11J."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.dispatch_runner_binding_state import (
    CooDispatchRunnerBindingState,
    bind_dispatch_runner_binding,
    format_runner_binding_state_summary,
    load_dispatch_runner_binding_state,
    reset_dispatch_runner_binding,
    stage_dispatch_runner_binding,
)


@dataclass(frozen=True)
class CooDispatchBindingTransitionSummary:
    """Safe summary for a binding transition result."""

    binding: CooDispatchRunnerBindingState
    transition: str
    changed: bool


def summarize_dispatch_runner_binding() -> CooDispatchRunnerBindingState:
    return load_dispatch_runner_binding_state()


def execute_dispatch_binding_stage(
    *,
    operator_id: str,
    reason: str,
) -> CooDispatchBindingTransitionSummary:
    before = load_dispatch_runner_binding_state()
    after = stage_dispatch_runner_binding(operator_id=operator_id, reason=reason)
    changed = before.state != after.state
    transition = "unbound_to_staged" if changed else "already_staged"
    return CooDispatchBindingTransitionSummary(
        binding=after,
        transition=transition,
        changed=changed,
    )


def execute_dispatch_binding_reset(
    *,
    operator_id: str,
    reason: str,
) -> CooDispatchBindingTransitionSummary:
    before = load_dispatch_runner_binding_state()
    after = reset_dispatch_runner_binding(operator_id=operator_id, reason=reason)
    changed = before.state != after.state
    transition = "staged_to_unbound" if changed else "already_unbound"
    return CooDispatchBindingTransitionSummary(
        binding=after,
        transition=transition,
        changed=changed,
    )


def execute_dispatch_binding_bind(
    *,
    operator_id: str,
    reason: str,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchBindingTransitionSummary:
    before = load_dispatch_runner_binding_state()
    after = bind_dispatch_runner_binding(
        operator_id=operator_id,
        reason=reason,
        merged_config=merged_config,
    )
    changed = before.state != after.state
    transition = "staged_to_bound" if changed else "already_bound"
    return CooDispatchBindingTransitionSummary(
        binding=after,
        transition=transition,
        changed=changed,
    )


def format_dispatch_binding_transition_summary(
    summary: CooDispatchBindingTransitionSummary,
) -> str:
    lines = [
        format_runner_binding_state_summary(summary.binding),
        f"transition: {summary.transition}",
        f"changed: {str(summary.changed).lower()}",
    ]
    return "\n".join(lines)
