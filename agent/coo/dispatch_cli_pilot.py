"""CLI dispatch isolated operational pilot — Phase 13A / 13B.

Read-only pilot readiness and gated isolated-clone dispatch rehearsal.
Production Repository2 root execution remains hard-denied.
Gateway/Discord production dispatch paths remain disconnected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CooDispatchPilotRunOutcome:
    """Outcome of an isolated operational pilot dispatch run."""

    pilot_attempt_id: str
    exit_code: int
    history_persisted: bool
    history_persistence_failed: bool
    run_result: "CooDispatchRunResult | None" = None
    run_error: str = ""


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


def execute_pilot_dispatch_run(
    *,
    ticket_id: str,
    confirmation_id: str,
    unlock_token_id: str,
    requester_id: str,
    pipeline_root: str,
    dry_run: bool,
    pilot_summary: CooDispatchPilotReadinessSummary,
    merged_config: Mapping[str, Any] | None = None,
    subprocess_runner=None,
    node_path: str | None = None,
    use_runner_provider: bool = True,
) -> CooDispatchPilotRunOutcome:
    """Run isolated pilot dispatch and persist append-only pilot history."""
    from agent.coo.dispatch_cli_pilot_history import (
        build_pilot_history_record_from_dispatch,
    )
    from agent.coo.dispatch_cli_run import CooDispatchRunResult, execute_coo_dispatch_run
    from agent.coo.dispatch_cli_runner_injection import resolve_dispatch_run_subprocess_runner
    from agent.coo.dispatch_pilot_history import write_pilot_history_record
    from agent.coo.bounded_subprocess_runner import RUNNER_PROFILE_RESTRICTED

    pilot_attempt_id = str(uuid.uuid4())
    started_at = _utc_now_iso()
    run_result: CooDispatchRunResult | None = None
    run_error = ""
    dispatch_request_id = ""

    resolved_runner = None
    if use_runner_provider and not dry_run:
        resolved_runner = resolve_dispatch_run_subprocess_runner(
            merged_config,
            use_runner_provider=True,
            use_real_bounded_runner=False,
            dry_run=False,
            harness_profile=RUNNER_PROFILE_RESTRICTED,
        )
    elif subprocess_runner is not None:
        resolved_runner = subprocess_runner

    try:
        run_result = execute_coo_dispatch_run(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            unlock_token_id=unlock_token_id,
            requester_id=requester_id,
            pipeline_root=pipeline_root,
            dry_run=dry_run,
            subprocess_runner=resolved_runner,
            merged_config=merged_config,
            node_path=node_path,
        )
        dispatch_request_id = run_result.dispatch_request_id
    except ValueError as exc:
        run_error = str(exc)

    completed_at = _utc_now_iso()
    record = build_pilot_history_record_from_dispatch(
        pilot_attempt_id=pilot_attempt_id,
        started_at=started_at,
        completed_at=completed_at,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        dispatch_request_id=dispatch_request_id,
        dry_run=dry_run,
        run_result=run_result,
        run_error=run_error,
        pilot_summary=pilot_summary,
    )
    history_persisted = False
    try:
        write_pilot_history_record(record)
        history_persisted = True
    except ValueError:
        history_persisted = False

    exit_code = _pilot_run_exit_code(
        run_result=run_result,
        run_error=run_error,
        history_persisted=history_persisted,
    )
    return CooDispatchPilotRunOutcome(
        pilot_attempt_id=pilot_attempt_id,
        exit_code=exit_code,
        history_persisted=history_persisted,
        history_persistence_failed=not history_persisted,
        run_result=run_result,
        run_error=run_error,
    )


def _pilot_run_exit_code(
    *,
    run_result: "CooDispatchRunResult | None",
    run_error: str,
    history_persisted: bool,
) -> int:
    if not history_persisted:
        return 1
    if run_error:
        return 1
    if run_result is None:
        return 1
    if run_result.dry_run_only:
        return 0 if run_result.status == "preflight_passed" else 1
    if run_result.status == "completed" and run_result.consumed:
        return 0
    return 1


def format_dispatch_pilot_run_outcome(outcome: CooDispatchPilotRunOutcome) -> str:
    """Format safe pilot run outcome markers."""
    lines = [
        "",
        "Pilot Run",
        "---------",
        f"pilot_attempt_id: {outcome.pilot_attempt_id}",
        f"history_persisted: {str(outcome.history_persisted).lower()}",
    ]
    if outcome.history_persistence_failed:
        lines.append("history_persistence_failed: true")
    return "\n".join(lines)
