"""CLI dispatch isolated operational pilot — Phase 13A.

Read-only pilot readiness and gated isolated-clone dispatch rehearsal.
Production Repository2 root execution remains hard-denied.
Gateway/Discord production dispatch paths remain disconnected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.dispatch_cli_enablement import evaluate_dispatch_runtime_enablement
from agent.coo.dispatch_cli_production_signoff import (
    evaluate_dispatch_production_signoff,
)
from agent.coo.dispatch_cli_readiness import evaluate_dispatch_operator_readiness
from agent.coo.dispatch_executor_config import load_dispatch_executor_policy
from agent.coo.dispatch_pipeline_root_trust import assert_cli_pipeline_root_trusted
from agent.coo.dispatch_runner_binding_state import (
    DispatchRunnerBindingStateError,
    load_dispatch_runner_binding_state,
    runner_binding_state_is_bound,
    validate_dispatch_runner_binding_for_run,
)
from agent.coo.production_executor_policy import _root_allowed

EXECUTION_SCOPE_ISOLATED_CLONE = "isolated_clone"
OPERATOR_READY_NOT_EVALUATED = "not_evaluated"
PIPELINE_ROOT_NOT_EVALUATED = "not_evaluated"

OPERATOR_ACTION_APPROVE_ISOLATED_DRILL = "approve_isolated_operational_drill"
OPERATOR_ACTION_RESOLVE_FAILED = "resolve_failed_checks"
OPERATOR_ACTION_MAINTAIN_EXECUTION_BLOCK = "maintain_execution_block"

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class CooDispatchPilotReadinessSummary:
    """Safe read-only isolated operational pilot readiness summary."""

    pilot_ready: bool
    signoff_ready: bool
    runner_bound: bool
    runtime_enablement_ready: bool
    operator_ready: str
    pipeline_root_trusted: str
    production_execution_allowed: bool
    gateway_enabled: bool
    production_root_hard_deny: bool
    execution_scope: str
    failed_checks: str
    operator_action: str


def _join_names(names: tuple[str, ...]) -> str:
    return ",".join(names) if names else _NONE_LABEL


def _evaluate_pipeline_root_trusted(
    *,
    pipeline_root: str | None,
    merged_config: Mapping[str, Any] | None,
) -> tuple[str, tuple[str, ...]]:
    if not pipeline_root or not str(pipeline_root).strip():
        return PIPELINE_ROOT_NOT_EVALUATED, ()
    try:
        trusted = assert_cli_pipeline_root_trusted(pipeline_root)
    except ValueError:
        return "false", ("pipeline_root_trust",)
    try:
        policy = load_dispatch_executor_policy(merged_config)
    except ValueError:
        return "false", ("executor_config",)
    root_ok, _reason = _root_allowed(trusted, policy.allowed_pipeline_roots)
    if not root_ok:
        return "false", ("pipeline_root_allowlist",)
    return "true", ()


def _evaluate_operator_ready(
    *,
    ticket_id: str | None,
    confirmation_id: str | None,
    pipeline_root: str | None,
    merged_config: Mapping[str, Any] | None,
) -> tuple[str, tuple[str, ...]]:
    if not ticket_id or not confirmation_id or not pipeline_root:
        return OPERATOR_READY_NOT_EVALUATED, ()
    readiness = evaluate_dispatch_operator_readiness(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
        merged_config=merged_config,
    )
    if readiness.ready:
        return "true", ()
    failed = readiness.failed_steps or ("operator_readiness",)
    return "false", tuple(failed)


def _resolve_operator_action(*, pilot_ready: bool, failed_checks: tuple[str, ...]) -> str:
    if failed_checks:
        return OPERATOR_ACTION_RESOLVE_FAILED
    if pilot_ready:
        return OPERATOR_ACTION_APPROVE_ISOLATED_DRILL
    return OPERATOR_ACTION_MAINTAIN_EXECUTION_BLOCK


def evaluate_pilot_readiness(
    *,
    ticket_id: str | None = None,
    confirmation_id: str | None = None,
    pipeline_root: str | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchPilotReadinessSummary:
    """Evaluate read-only isolated operational pilot readiness."""
    signoff = evaluate_dispatch_production_signoff(merged_config=merged_config)
    runtime_enablement = evaluate_dispatch_runtime_enablement(merged_config)
    try:
        binding = load_dispatch_runner_binding_state()
        runner_bound = runner_binding_state_is_bound(binding)
    except DispatchRunnerBindingStateError:
        runner_bound = False

    operator_ready, operator_failed = _evaluate_operator_ready(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
        merged_config=merged_config,
    )
    pipeline_root_trusted, pipeline_failed = _evaluate_pipeline_root_trusted(
        pipeline_root=pipeline_root,
        merged_config=merged_config,
    )

    failed: list[str] = []
    if not signoff.signoff_ready:
        failed.append("production_signoff")
    if not signoff.production_root_hard_deny:
        failed.append("production_root_hard_deny")
    if signoff.gateway_enabled:
        failed.append("gateway_enabled")
    if signoff.execution_allowed:
        failed.append("production_execution_allowed")
    if not runtime_enablement.enablement_ready:
        failed.append("runtime_enablement")
    if not runner_bound:
        failed.append("runner_binding")
    if operator_ready == "false":
        failed.extend(operator_failed)
    if pipeline_root_trusted == "false":
        failed.extend(pipeline_failed)

    failed_checks = tuple(dict.fromkeys(failed))
    pilot_ready = not failed_checks
    operator_action = _resolve_operator_action(
        pilot_ready=pilot_ready,
        failed_checks=failed_checks,
    )

    return CooDispatchPilotReadinessSummary(
        pilot_ready=pilot_ready,
        signoff_ready=signoff.signoff_ready,
        runner_bound=runner_bound,
        runtime_enablement_ready=runtime_enablement.enablement_ready,
        operator_ready=operator_ready,
        pipeline_root_trusted=pipeline_root_trusted,
        production_execution_allowed=False,
        gateway_enabled=signoff.gateway_enabled,
        production_root_hard_deny=signoff.production_root_hard_deny,
        execution_scope=EXECUTION_SCOPE_ISOLATED_CLONE,
        failed_checks=_join_names(failed_checks),
        operator_action=operator_action,
    )


def assert_pilot_dispatch_allowed(
    *,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root: str,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchPilotReadinessSummary:
    """Fail-closed gate before an isolated operational pilot dispatch run."""
    readiness = evaluate_pilot_readiness(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
        merged_config=merged_config,
    )
    if not readiness.pilot_ready:
        raise ValueError("isolated operational pilot is not ready")
    if readiness.production_execution_allowed:
        raise ValueError("production execution is not allowed for pilot")
    if readiness.gateway_enabled:
        raise ValueError("gateway production execution must remain disabled for pilot")
    if not readiness.production_root_hard_deny:
        raise ValueError("production root hard-deny must remain active for pilot")
    assert_cli_pipeline_root_trusted(pipeline_root)
    validate_dispatch_runner_binding_for_run()
    return readiness


def format_dispatch_pilot_readiness(
    summary: CooDispatchPilotReadinessSummary,
) -> str:
    """Format safe pilot readiness fields for CLI stdout."""
    lines = [
        "Isolated Operational Dispatch Pilot",
        "",
        f"pilot_ready: {str(summary.pilot_ready).lower()}",
        f"signoff_ready: {str(summary.signoff_ready).lower()}",
        f"runner_bound: {str(summary.runner_bound).lower()}",
        (
            "runtime_enablement_ready: "
            f"{str(summary.runtime_enablement_ready).lower()}"
        ),
        f"operator_ready: {summary.operator_ready}",
        f"pipeline_root_trusted: {summary.pipeline_root_trusted}",
        (
            "production_execution_allowed: "
            f"{str(summary.production_execution_allowed).lower()}"
        ),
        f"gateway_enabled: {str(summary.gateway_enabled).lower()}",
        (
            "production_root_hard_deny: "
            f"{str(summary.production_root_hard_deny).lower()}"
        ),
        f"execution_scope: {summary.execution_scope}",
        f"failed_checks: {summary.failed_checks}",
        f"operator_action: {summary.operator_action}",
    ]
    return "\n".join(lines)


def format_dispatch_pilot_run_footer(
    *,
    pilot_ready_summary: CooDispatchPilotReadinessSummary,
) -> str:
    """Format safe pilot scope markers after a dispatch run."""
    return "\n".join(
        (
            "",
            "Pilot Scope",
            "-----------",
            f"execution_scope: {pilot_ready_summary.execution_scope}",
            (
                "production_execution_allowed: "
                f"{str(pilot_ready_summary.production_execution_allowed).lower()}"
            ),
            (
                "production_root_hard_deny: "
                f"{str(pilot_ready_summary.production_root_hard_deny).lower()}"
            ),
            f"gateway_enabled: {str(pilot_ready_summary.gateway_enabled).lower()}",
        )
    )
