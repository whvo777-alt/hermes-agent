"""CLI dispatch production sign-off — Phase 12U.

Read-only aggregation of readiness, attestation, and capability checks.
Sign-off ready does not grant production execution permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.dispatch_cli_production_readiness import (
    CHECK_BLOCKED,
    CHECK_FAIL,
    CHECK_PASS,
    OVERALL_READY,
    _evaluate_audit_check,
    _evaluate_binding_check,
    _evaluate_consume_check,
    _evaluate_evidence_check,
    _evaluate_operator_check,
    _evaluate_provider_check,
    _evaluate_recovery_check,
    _evaluate_repair_check,
    _evaluate_runtime_gates_check,
    _production_root_hard_deny_active,
    evaluate_dispatch_production_readiness,
)
from agent.coo.dispatch_cli_repository_attestation import (
    EXPECTED_REPOSITORY2_PRODUCTION_ROOT,
    attest_repository2_production_root,
)
from agent.coo.dispatch_gateway_enablement import (
    evaluate_gateway_production_check,
    load_dispatch_gateway_enablement,
)

OVERALL_SIGNOFF_READY = "SIGNOFF_READY"
OVERALL_SIGNOFF_NOT_READY = "SIGNOFF_NOT_READY"

RECOMMENDED_NEXT_PHASE_READY = "Phase 13A Isolated Operational Dispatch Pilot"
RECOMMENDED_NEXT_PHASE_NOT_READY = "resolve_production_signoff_failures"

OPERATOR_ACTION_APPROVE_ISOLATED_DRILL = "approve_isolated_operational_drill"
OPERATOR_ACTION_RESOLVE_FAILED = "resolve_failed_checks"
OPERATOR_ACTION_MAINTAIN_EXECUTION_BLOCK = "maintain_execution_block"
OPERATOR_ACTION_REVIEW_GATEWAY_LATER = "review_gateway_integration_later"

_NONE_LABEL = "(none)"


@dataclass(frozen=True)
class CooDispatchProductionSignoffCheck:
    """One production dispatch sign-off check."""

    name: str
    status: str


@dataclass(frozen=True)
class CooDispatchProductionSignoffSummary:
    """Safe read-only production dispatch sign-off summary."""

    signoff_ready: bool
    overall_status: str
    checks_passed_count: int
    checks_blocked_count: int
    checks_failed_count: int
    failed_checks: str
    blocked_checks: str
    repository_attested: bool
    production_root_hard_deny: bool
    execution_allowed: bool
    gateway_enabled: bool
    recommended_next_phase: str
    operator_action: str


def _map_capability_status(readiness_status: str) -> str:
    if readiness_status == CHECK_PASS:
        return CHECK_PASS
    return CHECK_FAIL


def _evaluate_production_readiness_ready(
    *,
    merged_config: Mapping[str, Any] | None,
) -> str:
    readiness = evaluate_dispatch_production_readiness(merged_config=merged_config)
    return CHECK_PASS if readiness.overall == OVERALL_READY else CHECK_FAIL


def _evaluate_repository_attestation_valid() -> tuple[str, bool]:
    try:
        summary = attest_repository2_production_root(
            repository_root=EXPECTED_REPOSITORY2_PRODUCTION_ROOT,
        )
    except ValueError:
        return CHECK_FAIL, False
    if not summary.repository_attested:
        return CHECK_FAIL, False
    return CHECK_PASS, True


def _evaluate_production_root_hard_deny() -> str:
    if _production_root_hard_deny_active():
        return CHECK_BLOCKED
    return CHECK_FAIL


def _evaluate_execution_disabled() -> str:
    if _production_root_hard_deny_active():
        return CHECK_BLOCKED
    return CHECK_FAIL


def _evaluate_gateway_disabled(
    *,
    merged_config: Mapping[str, Any] | None = None,
) -> str:
    enablement = load_dispatch_gateway_enablement(merged_config=merged_config)
    return evaluate_gateway_production_check(enablement)


def _build_signoff_checks(
    *,
    merged_config: Mapping[str, Any] | None,
    repository_attestation_status: str,
) -> tuple[CooDispatchProductionSignoffCheck, ...]:
    return (
        CooDispatchProductionSignoffCheck(
            "production_readiness_ready",
            _evaluate_production_readiness_ready(merged_config=merged_config),
        ),
        CooDispatchProductionSignoffCheck(
            "repository_attestation_valid",
            repository_attestation_status,
        ),
        CooDispatchProductionSignoffCheck(
            "production_root_hard_deny",
            _evaluate_production_root_hard_deny(),
        ),
        CooDispatchProductionSignoffCheck(
            "execution_disabled",
            _evaluate_execution_disabled(),
        ),
        CooDispatchProductionSignoffCheck(
            "gateway_disabled",
            _evaluate_gateway_disabled(merged_config=merged_config),
        ),
        CooDispatchProductionSignoffCheck(
            "binding_model_available",
            _map_capability_status(_evaluate_binding_check()),
        ),
        CooDispatchProductionSignoffCheck(
            "provider_model_available",
            _map_capability_status(_evaluate_provider_check()),
        ),
        CooDispatchProductionSignoffCheck(
            "pre_run_gate_available",
            _map_capability_status(_evaluate_runtime_gates_check()),
        ),
        CooDispatchProductionSignoffCheck(
            "evidence_available",
            _map_capability_status(_evaluate_evidence_check()),
        ),
        CooDispatchProductionSignoffCheck(
            "audit_available",
            _map_capability_status(_evaluate_audit_check()),
        ),
        CooDispatchProductionSignoffCheck(
            "consume_transaction_available",
            _map_capability_status(_evaluate_consume_check()),
        ),
        CooDispatchProductionSignoffCheck(
            "recovery_available",
            _map_capability_status(_evaluate_recovery_check()),
        ),
        CooDispatchProductionSignoffCheck(
            "repair_available",
            _map_capability_status(_evaluate_repair_check()),
        ),
        CooDispatchProductionSignoffCheck(
            "operator_runbook_available",
            _map_capability_status(_evaluate_operator_check()),
        ),
    )


def _join_check_names(names: tuple[str, ...]) -> str:
    return ",".join(names) if names else _NONE_LABEL


def _resolve_operator_action(
    *,
    signoff_ready: bool,
    failed_checks: tuple[str, ...],
    blocked_checks: tuple[str, ...],
) -> str:
    if failed_checks:
        return OPERATOR_ACTION_RESOLVE_FAILED
    if signoff_ready:
        if "gateway_disabled" in blocked_checks:
            return OPERATOR_ACTION_APPROVE_ISOLATED_DRILL
        return OPERATOR_ACTION_APPROVE_ISOLATED_DRILL
    if blocked_checks and not failed_checks:
        return OPERATOR_ACTION_MAINTAIN_EXECUTION_BLOCK
    return OPERATOR_ACTION_REVIEW_GATEWAY_LATER


def evaluate_dispatch_production_signoff(
    *,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchProductionSignoffSummary:
    """Evaluate read-only production dispatch sign-off readiness."""
    attestation_status, repository_attested = _evaluate_repository_attestation_valid()
    checks = _build_signoff_checks(
        merged_config=merged_config,
        repository_attestation_status=attestation_status,
    )
    failed = tuple(check.name for check in checks if check.status == CHECK_FAIL)
    blocked = tuple(check.name for check in checks if check.status == CHECK_BLOCKED)
    passed_count = sum(1 for check in checks if check.status == CHECK_PASS)
    blocked_count = len(blocked)
    failed_count = len(failed)

    hard_deny_active = _production_root_hard_deny_active()
    enablement = load_dispatch_gateway_enablement(merged_config=merged_config)
    gateway_enabled = enablement.gateway_enabled
    execution_allowed = False

    policy_fail = (
        not hard_deny_active
        or gateway_enabled
        or execution_allowed
    )
    signoff_ready = failed_count == 0 and not policy_fail and repository_attested
    overall_status = (
        OVERALL_SIGNOFF_READY if signoff_ready else OVERALL_SIGNOFF_NOT_READY
    )
    recommended = (
        RECOMMENDED_NEXT_PHASE_READY
        if signoff_ready
        else RECOMMENDED_NEXT_PHASE_NOT_READY
    )
    operator_action = _resolve_operator_action(
        signoff_ready=signoff_ready,
        failed_checks=failed,
        blocked_checks=blocked,
    )

    return CooDispatchProductionSignoffSummary(
        signoff_ready=signoff_ready,
        overall_status=overall_status,
        checks_passed_count=passed_count,
        checks_blocked_count=blocked_count,
        checks_failed_count=failed_count,
        failed_checks=_join_check_names(failed),
        blocked_checks=_join_check_names(blocked),
        repository_attested=repository_attested,
        production_root_hard_deny=hard_deny_active,
        execution_allowed=execution_allowed,
        gateway_enabled=gateway_enabled,
        recommended_next_phase=recommended,
        operator_action=operator_action,
    )


def format_dispatch_production_signoff(
    summary: CooDispatchProductionSignoffSummary,
) -> str:
    """Format safe production sign-off fields for CLI stdout."""
    lines = [
        "Production Dispatch Sign-off",
        "",
        f"signoff_ready: {str(summary.signoff_ready).lower()}",
        f"overall_status: {summary.overall_status}",
        f"checks_passed_count: {summary.checks_passed_count}",
        f"checks_blocked_count: {summary.checks_blocked_count}",
        f"checks_failed_count: {summary.checks_failed_count}",
        f"failed_checks: {summary.failed_checks}",
        f"blocked_checks: {summary.blocked_checks}",
        f"repository_attested: {str(summary.repository_attested).lower()}",
        (
            "production_root_hard_deny: "
            f"{str(summary.production_root_hard_deny).lower()}"
        ),
        f"execution_allowed: {str(summary.execution_allowed).lower()}",
        f"gateway_enabled: {str(summary.gateway_enabled).lower()}",
        f"recommended_next_phase: {summary.recommended_next_phase}",
        f"operator_action: {summary.operator_action}",
    ]
    return "\n".join(lines)
