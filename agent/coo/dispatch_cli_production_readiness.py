"""CLI dispatch production readiness review — Phase 12S.

Read-only capability and policy audit before production cutover.
No writes, subprocess, repair, dispatch execution, or secret disclosure.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.dispatch_pipeline_root_trust import PRODUCTION_ROOT_HARD_DENY

CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"
CHECK_BLOCKED = "BLOCKED"
CHECK_NOT_APPLICABLE = "NOT_APPLICABLE"

OVERALL_READY = "READY"
OVERALL_NOT_READY = "NOT_READY"

RECOMMENDED_NEXT_PHASE_READY = "Phase 12T Repository2 Read-Only Attestation"
RECOMMENDED_NEXT_PHASE_NOT_READY = (
    "Resolve failing production readiness checks before Phase 12T."
)


@dataclass(frozen=True)
class CooDispatchProductionReadinessCheck:
    """One production readiness subsystem check."""

    name: str
    status: str


@dataclass(frozen=True)
class CooDispatchRepository2PolicySummary:
    """Safe Repository2 production policy flags."""

    production_root_hard_deny: str
    read_only_only: str
    execution_disabled: str
    gateway_disabled: str


@dataclass(frozen=True)
class CooDispatchProductionReadinessSummary:
    """Safe read-only production readiness review summary."""

    overall: str
    checks: tuple[CooDispatchProductionReadinessCheck, ...]
    repository2_policy: CooDispatchRepository2PolicySummary
    blocking_items: tuple[str, ...]
    recommended_next_phase: str


def _policy_enabled(enabled: bool) -> str:
    return "enabled" if enabled else "disabled"


def _verify_callables(
    module_name: str,
    names: tuple[str, ...],
) -> bool:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False
    return all(callable(getattr(module, name, None)) for name in names)


def _verify_attribute(module_name: str, attribute: str) -> bool:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False
    return getattr(module, attribute, None) is not None


def _production_root_hard_deny_active() -> bool:
    if not PRODUCTION_ROOT_HARD_DENY:
        return False
    from agent.coo.dispatch_pipeline_root_trust import assert_pipeline_root_allowed

    for denied in PRODUCTION_ROOT_HARD_DENY:
        try:
            assert_pipeline_root_allowed(denied)
        except ValueError:
            continue
        return False
    return True


def _gateway_production_execution_disabled() -> bool:
    try:
        dispatcher = importlib.import_module("agent.coo.gateway_execution_dispatcher")
    except ImportError:
        return False
    forbidden = ("execute", "run", "dispatch_now")
    return not any(hasattr(dispatcher, name) for name in forbidden)


def _evaluate_binding_check() -> str:
    if _verify_callables(
        "agent.coo.dispatch_runner_binding_state",
        ("load_dispatch_runner_binding_state", "format_runner_binding_state_summary"),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_provider_check() -> str:
    if _verify_callables(
        "agent.coo.dispatch_runner_provider",
        ("assess_dispatch_runner_provider",),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_runner_harness_check() -> str:
    if _verify_callables(
        "agent.coo.bounded_subprocess_runner",
        ("create_bounded_subprocess_runner",),
    ) and _verify_callables(
        "agent.coo.dispatch_runner_provider",
        ("resolve_bounded_subprocess_runner",),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_dispatch_profile_check() -> str:
    if _verify_attribute("agent.coo.bounded_subprocess_runner", "RUNNER_PROFILE_DISPATCH"):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_evidence_check() -> str:
    if _verify_callables(
        "agent.coo.dispatch_cli_evidence",
        (
            "summarize_dispatch_evidence_attempt",
            "find_dispatch_evidence_attempts_for_ticket",
        ),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_audit_check() -> str:
    if _verify_callables(
        "agent.coo.dispatch_cli_audit",
        (
            "summarize_dispatch_execution_audit",
            "list_dispatch_execution_audits",
        ),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_consume_check() -> str:
    if _verify_callables(
        "agent.coo.dispatch_consume_transaction",
        ("assess_consume_status", "assert_consume_replay_allowed"),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_recovery_check() -> str:
    if _verify_callables(
        "agent.coo.dispatch_cli_consume_recovery",
        ("assess_dispatch_consume_recovery",),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_repair_check() -> str:
    if _verify_callables(
        "agent.coo.dispatch_consume_repair",
        (
            "evaluate_consume_repair_eligibility",
            "apply_consume_repair",
            "apply_prepared_transaction_cleanup",
            "apply_partial_forward_complete",
        ),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_operator_check() -> str:
    if _verify_callables(
        "agent.coo.dispatch_cli_operator_runbook",
        ("summarize_dispatch_operator_runbook", "format_dispatch_operator_runbook"),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_runtime_gates_check() -> str:
    if _verify_callables(
        "agent.coo.dispatch_cli_validation_core",
        ("validate_dispatch_pre_run",),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_enablement_check() -> str:
    if _verify_callables(
        "agent.coo.dispatch_cli_enablement",
        ("evaluate_dispatch_enablement", "evaluate_dispatch_runtime_enablement"),
    ):
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_repository2_policy_check() -> str:
    if _production_root_hard_deny_active():
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_gateway_check() -> str:
    if _gateway_production_execution_disabled():
        return CHECK_BLOCKED
    return CHECK_FAIL


def _build_checks() -> tuple[CooDispatchProductionReadinessCheck, ...]:
    return (
        CooDispatchProductionReadinessCheck("binding", _evaluate_binding_check()),
        CooDispatchProductionReadinessCheck("provider", _evaluate_provider_check()),
        CooDispatchProductionReadinessCheck(
            "runner_harness",
            _evaluate_runner_harness_check(),
        ),
        CooDispatchProductionReadinessCheck(
            "dispatch_profile",
            _evaluate_dispatch_profile_check(),
        ),
        CooDispatchProductionReadinessCheck(
            "runtime_gates",
            _evaluate_runtime_gates_check(),
        ),
        CooDispatchProductionReadinessCheck("enablement", _evaluate_enablement_check()),
        CooDispatchProductionReadinessCheck("evidence", _evaluate_evidence_check()),
        CooDispatchProductionReadinessCheck("audit", _evaluate_audit_check()),
        CooDispatchProductionReadinessCheck("consume", _evaluate_consume_check()),
        CooDispatchProductionReadinessCheck("recovery", _evaluate_recovery_check()),
        CooDispatchProductionReadinessCheck("repair", _evaluate_repair_check()),
        CooDispatchProductionReadinessCheck("operator", _evaluate_operator_check()),
        CooDispatchProductionReadinessCheck(
            "repository2_policy",
            _evaluate_repository2_policy_check(),
        ),
        CooDispatchProductionReadinessCheck("gateway", _evaluate_gateway_check()),
    )


def _repository2_policy_summary() -> CooDispatchRepository2PolicySummary:
    hard_deny = _production_root_hard_deny_active()
    gateway_disabled = _gateway_production_execution_disabled()
    return CooDispatchRepository2PolicySummary(
        production_root_hard_deny=_policy_enabled(hard_deny),
        read_only_only=_policy_enabled(True),
        execution_disabled=_policy_enabled(hard_deny),
        gateway_disabled=_policy_enabled(gateway_disabled),
    )


def evaluate_dispatch_production_readiness(
    *,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchProductionReadinessSummary:
    """Evaluate read-only dispatch production readiness."""
    _ = merged_config  # reserved for future config-aware checks; no writes
    checks = _build_checks()
    blocking = tuple(
        check.name for check in checks if check.status == CHECK_FAIL
    )
    overall = OVERALL_READY if not blocking else OVERALL_NOT_READY
    recommended = (
        RECOMMENDED_NEXT_PHASE_READY
        if overall == OVERALL_READY
        else RECOMMENDED_NEXT_PHASE_NOT_READY
    )
    return CooDispatchProductionReadinessSummary(
        overall=overall,
        checks=checks,
        repository2_policy=_repository2_policy_summary(),
        blocking_items=blocking,
        recommended_next_phase=recommended,
    )


def format_dispatch_production_readiness(
    summary: CooDispatchProductionReadinessSummary,
) -> str:
    """Format safe production readiness fields for CLI stdout."""
    lines = [
        "Production Readiness",
        "",
        f"overall: {summary.overall}",
        "",
        "Checks",
        "------",
    ]
    for check in summary.checks:
        lines.append(f"{check.name}: {check.status}")
    lines.extend(
        (
            "",
            "Repository2 Policy",
            "------------------",
            (
                "production_root_hard_deny: "
                f"{summary.repository2_policy.production_root_hard_deny}"
            ),
            f"read_only_only: {summary.repository2_policy.read_only_only}",
            (
                "execution_disabled: "
                f"{summary.repository2_policy.execution_disabled}"
            ),
            f"gateway_disabled: {summary.repository2_policy.gateway_disabled}",
            "",
            "Summary",
            "-------",
            f"overall_readiness: {summary.overall}",
        )
    )
    if summary.blocking_items:
        lines.append(f"blocking_items: {','.join(summary.blocking_items)}")
    else:
        lines.append("blocking_items: (none)")
    lines.append(f"recommended_next_phase: {summary.recommended_next_phase}")
    return "\n".join(lines)
