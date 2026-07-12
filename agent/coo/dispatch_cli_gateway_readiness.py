"""CLI dispatch gateway readiness — Phase 13G.

Read-only cross-check of Gateway UI/session/prepare surfaces against file-backed
dispatch state. No execution, config writes, subprocess, or Gateway/Discord calls.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_cli_production_cutover import (
    evaluate_production_cutover_checklist,
)
from agent.coo.dispatch_cli_production_readiness import (
    CHECK_BLOCKED,
    CHECK_FAIL,
    CHECK_NOT_APPLICABLE,
    CHECK_PASS,
    _production_root_hard_deny_active,
)
from agent.coo.dispatch_cli_production_signoff import (
    evaluate_dispatch_production_signoff,
)
from agent.coo.dispatch_cli_readiness import evaluate_dispatch_operator_readiness
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_LEGACY_COMMITTED,
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_RECOVERY_REQUIRED,
    assess_consume_status,
)
from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_DISABLED,
    GATEWAY_STATE_ENABLED,
    GATEWAY_STATE_STAGED,
    load_dispatch_gateway_enablement,
)
from agent.coo.dispatch_gateway_execution_facade import (
    evaluate_gateway_execution_facade,
)

READINESS_LEVEL_NOT_READY_FOR_EXECUTION = "NOT_READY_FOR_EXECUTION"
READINESS_LEVEL_READY_FOR_MOCK_WIRING = "READY_FOR_MOCK_WIRING"
READINESS_LEVEL_READY_FOR_MOCK_DISPATCH = "READY_FOR_MOCK_DISPATCH"
READINESS_LEVEL_NOT_READY = "NOT_READY"

RECOMMENDED_ACTION_STAGE_GATEWAY = "stage_gateway_for_mock_wiring"
RECOMMENDED_ACTION_IMPLEMENT_FACADE = "implement_gateway_execution_facade"
RECOMMENDED_ACTION_RUN_MOCK_DISPATCH = "run_mock_gateway_dispatch"
RECOMMENDED_ACTION_RESOLVE_FACADE_GAP = "resolve_gateway_facade_gap"
RECOMMENDED_ACTION_RESOLVE_FAILED_CHECKS = "resolve_failed_gateway_readiness_checks"

RECOMMENDED_NEXT_PHASE_DISABLED = "Phase 13I Mock Gateway Dispatch"
RECOMMENDED_NEXT_PHASE_STAGED_READY = "Phase 13I Mock Gateway Dispatch"
RECOMMENDED_NEXT_PHASE_ENABLED_NO_FACADE = (
    "Phase 13H Connect Gateway Execution Facade"
)
RECOMMENDED_NEXT_PHASE_NOT_READY = (
    "Resolve failing gateway readiness checks before Phase 13I."
)

EVIDENCE_CONTEXT_NONE = "none"
EVIDENCE_CONTEXT_FULL = "full"
EVIDENCE_CONTEXT_AMBIGUOUS = "ambiguous"

_NONE_LABEL = "(none)"

_STAGED_REQUIRED_CAPABILITY_CHECKS = frozenset(
    {
        "gateway_ui_surface_available",
        "gateway_session_model_available",
        "gateway_prepare_surface_available",
        "gateway_enablement_state",
        "production_root_hard_deny",
    }
)


@dataclass(frozen=True)
class CooDispatchGatewayReadinessCheck:
    """One gateway readiness subsystem check."""

    name: str
    status: str


@dataclass(frozen=True)
class CooDispatchGatewayReadinessSummary:
    """Safe read-only gateway readiness summary."""

    gateway_state: str
    readiness_level: str
    gateway_readiness_ready: bool
    checks_passed_count: int
    checks_blocked_count: int
    checks_failed_count: int
    failed_checks: str
    blocked_checks: str
    gateway_ui_surface_available: bool
    gateway_session_model_available: bool
    gateway_prepare_surface_available: bool
    gateway_execution_facade_connected: bool
    evidence_context_requested: str
    operator_readiness_status: str
    consume_state: str
    repair_in_progress: bool
    production_signoff_ready: bool
    production_cutover_ready: bool
    production_root_hard_deny: bool
    production_execution_allowed: bool
    gateway_execution_allowed: bool
    recommended_action: str
    recommended_next_phase: str
    checks: tuple[CooDispatchGatewayReadinessCheck, ...] = ()


def _join_check_names(names: tuple[str, ...]) -> str:
    return ",".join(names) if names else _NONE_LABEL


def _verify_callables(module_name: str, names: tuple[str, ...]) -> bool:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False
    return all(callable(getattr(module, name, None)) for name in names)


def _probe_gateway_ui_surface_available() -> bool:
    return _verify_callables(
        "plugins.platforms.discord.coo_approval",
        (
            "discord_ui_available",
            "build_coo_approval_session_payload",
            "build_coo_approval_embed_payload",
        ),
    )


def _probe_gateway_session_model_available() -> bool:
    return _verify_callables(
        "agent.coo.approval_session",
        ("CEOApprovalSessionStore", "create_approval_session"),
    ) and _verify_callables(
        "agent.coo.discord_approval_adapter",
        ("create_discord_approval_session", "get_discord_approval_session"),
    )


def _probe_gateway_prepare_surface_available() -> bool:
    return _verify_callables(
        "agent.coo.gateway_execution_dispatch",
        (
            "prepare_dispatch_for_gateway_session",
            "prepare_dispatch_for_gateway_ticket",
            "run_dispatch_for_gateway_request",
        ),
    ) and _verify_callables(
        "agent.coo.dispatch_cli_run",
        ("execute_coo_dispatch_run",),
    )


def _probe_unexpected_mutating_facade_marker() -> bool:
    from agent.coo.dispatch_gateway_execution_facade import (
        evaluate_gateway_execution_facade,
    )

    facade = evaluate_gateway_execution_facade()
    if not facade.valid:
        return False
    if facade.facade_connected and not facade.execution_enabled:
        return False
    if facade.execution_enabled and facade.production_execution_allowed:
        return True
    return False


def _resolve_evidence_context(
    *,
    ticket_id: str | None,
    confirmation_id: str | None,
    pipeline_root: str | None,
) -> str:
    provided = (
        ticket_id is not None,
        confirmation_id is not None,
        pipeline_root is not None,
    )
    if not any(provided):
        return EVIDENCE_CONTEXT_NONE
    if all(provided):
        return EVIDENCE_CONTEXT_FULL
    return EVIDENCE_CONTEXT_AMBIGUOUS


def _evaluate_enablement_state_check(enablement) -> str:
    if not enablement.valid:
        return CHECK_FAIL
    if enablement.gateway_state == GATEWAY_STATE_DISABLED:
        return CHECK_BLOCKED
    return CHECK_PASS


def _evaluate_facade_check(
    enablement,
    merged_config: Mapping[str, Any] | None = None,
) -> str:
    from agent.coo.dispatch_gateway_execution_facade import (
        evaluate_gateway_execution_facade,
    )

    if not enablement.valid:
        return CHECK_FAIL
    facade = evaluate_gateway_execution_facade(merged_config=merged_config)
    if not facade.valid or not facade.facade_connected:
        if enablement.gateway_state == GATEWAY_STATE_ENABLED:
            return CHECK_FAIL
        return CHECK_BLOCKED
    if facade.execution_enabled and facade.production_execution_allowed:
        return CHECK_FAIL
    if enablement.gateway_state == GATEWAY_STATE_STAGED and facade.execution_enabled:
        return CHECK_PASS
    if not facade.execution_enabled:
        return CHECK_BLOCKED
    return CHECK_PASS


def _evaluate_bundle_available(
    *,
    ticket_id: str,
    bundle_dir: Path | None,
) -> str:
    from agent.coo.dispatch_bundle_store import read_bundle

    try:
        read_bundle(ticket_id, bundle_dir=bundle_dir)
    except (KeyError, ValueError, OSError):
        return CHECK_FAIL
    return CHECK_PASS


def _evaluate_confirmation_available(
    *,
    confirmation_id: str,
    confirmation_dir: Path | None,
) -> str:
    from agent.coo.production_executor_confirmation import read_confirmation

    try:
        read_confirmation(
            confirmation_id,
            confirmation_dir=confirmation_dir,
        )
    except (KeyError, ValueError, OSError):
        return CHECK_FAIL
    return CHECK_PASS


def _evaluate_consume_state_clear(
    *,
    ticket_id: str,
    confirmation_id: str,
) -> str:
    status = assess_consume_status(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
    )
    if status.consume_state in {
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_LEGACY_PARTIAL,
        CONSUME_STATE_RECOVERY_REQUIRED,
        CONSUME_STATE_COMMITTED,
        CONSUME_STATE_LEGACY_COMMITTED,
    }:
        return CHECK_FAIL
    return CHECK_PASS


def _evaluate_repair_lock_clear(
    *,
    ticket_id: str,
    confirmation_id: str,
) -> str:
    from agent.coo.dispatch_cli_consume_repair_lock import (
        summarize_consume_repair_lock_status,
    )

    lock_status = summarize_consume_repair_lock_status(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
    )
    if lock_status.repair_in_progress:
        return CHECK_FAIL
    return CHECK_PASS


def _build_checks(
    *,
    enablement,
    evidence_context: str,
    ticket_id: str | None,
    confirmation_id: str | None,
    pipeline_root: str | None,
    merged_config: Mapping[str, Any] | None,
) -> tuple[CooDispatchGatewayReadinessCheck, ...]:
    checks: list[CooDispatchGatewayReadinessCheck] = []

    if evidence_context == EVIDENCE_CONTEXT_AMBIGUOUS:
        checks.append(
            CooDispatchGatewayReadinessCheck("evidence_context", CHECK_FAIL),
        )

    if _probe_unexpected_mutating_facade_marker():
        checks.append(
            CooDispatchGatewayReadinessCheck(
                "unexpected_mutating_facade",
                CHECK_FAIL,
            ),
        )

    ui_available = _probe_gateway_ui_surface_available()
    session_available = _probe_gateway_session_model_available()
    prepare_available = _probe_gateway_prepare_surface_available()

    checks.extend(
        (
            CooDispatchGatewayReadinessCheck(
                "gateway_enablement_state",
                _evaluate_enablement_state_check(enablement),
            ),
            CooDispatchGatewayReadinessCheck(
                "gateway_ui_surface_available",
                CHECK_PASS if ui_available else CHECK_FAIL,
            ),
            CooDispatchGatewayReadinessCheck(
                "gateway_session_model_available",
                CHECK_PASS if session_available else CHECK_FAIL,
            ),
            CooDispatchGatewayReadinessCheck(
                "gateway_prepare_surface_available",
                CHECK_PASS if prepare_available else CHECK_FAIL,
            ),
            CooDispatchGatewayReadinessCheck(
                "gateway_execution_facade_connected",
                _evaluate_facade_check(enablement, merged_config),
            ),
        )
    )

    if evidence_context == EVIDENCE_CONTEXT_FULL:
        assert ticket_id is not None
        assert confirmation_id is not None
        assert pipeline_root is not None
        checks.extend(
            (
                CooDispatchGatewayReadinessCheck(
                    "file_backed_bundle_available",
                    _evaluate_bundle_available(ticket_id=ticket_id, bundle_dir=None),
                ),
                CooDispatchGatewayReadinessCheck(
                    "file_backed_confirmation_available",
                    _evaluate_confirmation_available(
                        confirmation_id=confirmation_id,
                        confirmation_dir=None,
                    ),
                ),
                CooDispatchGatewayReadinessCheck(
                    "consume_state_clear",
                    _evaluate_consume_state_clear(
                        ticket_id=ticket_id,
                        confirmation_id=confirmation_id,
                    ),
                ),
                CooDispatchGatewayReadinessCheck(
                    "repair_lock_clear",
                    _evaluate_repair_lock_clear(
                        ticket_id=ticket_id,
                        confirmation_id=confirmation_id,
                    ),
                ),
                CooDispatchGatewayReadinessCheck(
                    "operator_readiness",
                    (
                        CHECK_PASS
                        if evaluate_dispatch_operator_readiness(
                            ticket_id=ticket_id,
                            confirmation_id=confirmation_id,
                            pipeline_root=pipeline_root,
                            merged_config=merged_config,
                        ).ready
                        else CHECK_FAIL
                    ),
                ),
            )
        )
    else:
        checks.extend(
            (
                CooDispatchGatewayReadinessCheck(
                    "file_backed_bundle_available",
                    CHECK_NOT_APPLICABLE,
                ),
                CooDispatchGatewayReadinessCheck(
                    "file_backed_confirmation_available",
                    CHECK_NOT_APPLICABLE,
                ),
                CooDispatchGatewayReadinessCheck(
                    "consume_state_clear",
                    CHECK_NOT_APPLICABLE,
                ),
                CooDispatchGatewayReadinessCheck(
                    "repair_lock_clear",
                    CHECK_NOT_APPLICABLE,
                ),
                CooDispatchGatewayReadinessCheck(
                    "operator_readiness",
                    CHECK_NOT_APPLICABLE,
                ),
            )
        )

    signoff = evaluate_dispatch_production_signoff(merged_config=merged_config)
    cutover = evaluate_production_cutover_checklist(merged_config=merged_config)
    hard_deny = _production_root_hard_deny_active()

    checks.extend(
        (
            CooDispatchGatewayReadinessCheck(
                "production_signoff_ready",
                CHECK_PASS if signoff.signoff_ready else CHECK_FAIL,
            ),
            CooDispatchGatewayReadinessCheck(
                "production_cutover_ready",
                CHECK_PASS if cutover.cutover_ready else CHECK_FAIL,
            ),
            CooDispatchGatewayReadinessCheck(
                "production_root_hard_deny",
                CHECK_PASS if hard_deny else CHECK_FAIL,
            ),
            CooDispatchGatewayReadinessCheck(
                "production_execution_allowed",
                CHECK_BLOCKED,
            ),
            CooDispatchGatewayReadinessCheck(
                "gateway_execution_allowed",
                CHECK_BLOCKED,
            ),
        )
    )
    return tuple(checks)


def _resolve_readiness_level(
    *,
    enablement,
    evidence_context: str,
    failed_checks: tuple[str, ...],
    checks: tuple[CooDispatchGatewayReadinessCheck, ...],
) -> tuple[bool, str]:
    if evidence_context == EVIDENCE_CONTEXT_AMBIGUOUS or not enablement.valid:
        return False, READINESS_LEVEL_NOT_READY

    if enablement.gateway_state == GATEWAY_STATE_DISABLED:
        return False, READINESS_LEVEL_NOT_READY_FOR_EXECUTION

    if enablement.gateway_state == GATEWAY_STATE_ENABLED:
        if not enablement.gateway_execution_configured:
            return False, READINESS_LEVEL_NOT_READY
        return False, READINESS_LEVEL_NOT_READY

    if enablement.gateway_state == GATEWAY_STATE_STAGED:
        if failed_checks:
            return False, READINESS_LEVEL_NOT_READY
        statuses = {check.name: check.status for check in checks}
        if any(statuses.get(name) != CHECK_PASS for name in _STAGED_REQUIRED_CAPABILITY_CHECKS):
            return False, READINESS_LEVEL_NOT_READY
        if statuses.get("gateway_execution_facade_connected") != CHECK_PASS:
            return False, READINESS_LEVEL_NOT_READY
        return True, READINESS_LEVEL_READY_FOR_MOCK_DISPATCH

    return False, READINESS_LEVEL_NOT_READY


def _resolve_recommended_action(
    *,
    enablement,
    readiness_level: str,
    failed_checks: tuple[str, ...],
) -> str:
    if failed_checks:
        return RECOMMENDED_ACTION_RESOLVE_FAILED_CHECKS
    if not enablement.valid:
        return RECOMMENDED_ACTION_RESOLVE_FAILED_CHECKS
    if enablement.gateway_state == GATEWAY_STATE_DISABLED:
        return RECOMMENDED_ACTION_STAGE_GATEWAY
    if enablement.gateway_state == GATEWAY_STATE_STAGED:
        if readiness_level == READINESS_LEVEL_READY_FOR_MOCK_DISPATCH:
            return RECOMMENDED_ACTION_RUN_MOCK_DISPATCH
        return RECOMMENDED_ACTION_RESOLVE_FAILED_CHECKS
    if enablement.gateway_state == GATEWAY_STATE_ENABLED:
        return RECOMMENDED_ACTION_RESOLVE_FACADE_GAP
    return RECOMMENDED_ACTION_RESOLVE_FAILED_CHECKS


def _resolve_recommended_next_phase(
    *,
    enablement,
    readiness_level: str,
    failed_checks: tuple[str, ...],
) -> str:
    if failed_checks or not enablement.valid:
        return RECOMMENDED_NEXT_PHASE_NOT_READY
    if enablement.gateway_state == GATEWAY_STATE_DISABLED:
        return RECOMMENDED_NEXT_PHASE_DISABLED
    if readiness_level == READINESS_LEVEL_READY_FOR_MOCK_DISPATCH:
        return RECOMMENDED_NEXT_PHASE_STAGED_READY
    if enablement.gateway_state == GATEWAY_STATE_ENABLED:
        return RECOMMENDED_NEXT_PHASE_ENABLED_NO_FACADE
    return RECOMMENDED_NEXT_PHASE_NOT_READY


def evaluate_dispatch_gateway_readiness(
    *,
    ticket_id: str | None = None,
    confirmation_id: str | None = None,
    pipeline_root: str | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchGatewayReadinessSummary:
    """Evaluate read-only gateway readiness without mutating state."""
    if merged_config is None:
        merged_config = {}

    enablement = load_dispatch_gateway_enablement(merged_config=merged_config)
    evidence_context = _resolve_evidence_context(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
    )

    checks = _build_checks(
        enablement=enablement,
        evidence_context=evidence_context,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
        merged_config=merged_config,
    )

    failed = tuple(check.name for check in checks if check.status == CHECK_FAIL)
    blocked = tuple(check.name for check in checks if check.status == CHECK_BLOCKED)
    passed_count = sum(1 for check in checks if check.status == CHECK_PASS)
    blocked_count = len(blocked)
    failed_count = len(failed)

    gateway_readiness_ready, readiness_level = _resolve_readiness_level(
        enablement=enablement,
        evidence_context=evidence_context,
        failed_checks=failed,
        checks=checks,
    )

    signoff = evaluate_dispatch_production_signoff(merged_config=merged_config)
    cutover = evaluate_production_cutover_checklist(merged_config=merged_config)

    operator_status = "not_evaluated"
    consume_state = "not_evaluated"
    repair_in_progress = False
    if evidence_context == EVIDENCE_CONTEXT_FULL:
        assert ticket_id is not None
        assert confirmation_id is not None
        assert pipeline_root is not None
        readiness = evaluate_dispatch_operator_readiness(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            pipeline_root=pipeline_root,
            merged_config=merged_config,
        )
        operator_status = "ready" if readiness.ready else "not_ready"
        consume = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        consume_state = consume.consume_state
        from agent.coo.dispatch_cli_consume_repair_lock import (
            summarize_consume_repair_lock_status,
        )

        lock_status = summarize_consume_repair_lock_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        repair_in_progress = lock_status.repair_in_progress

    recommended_action = _resolve_recommended_action(
        enablement=enablement,
        readiness_level=readiness_level,
        failed_checks=failed,
    )
    recommended_next_phase = _resolve_recommended_next_phase(
        enablement=enablement,
        readiness_level=readiness_level,
        failed_checks=failed,
    )

    return CooDispatchGatewayReadinessSummary(
        gateway_state=enablement.gateway_state,
        readiness_level=readiness_level,
        gateway_readiness_ready=gateway_readiness_ready,
        checks_passed_count=passed_count,
        checks_blocked_count=blocked_count,
        checks_failed_count=failed_count,
        failed_checks=_join_check_names(failed),
        blocked_checks=_join_check_names(blocked),
        gateway_ui_surface_available=_probe_gateway_ui_surface_available(),
        gateway_session_model_available=_probe_gateway_session_model_available(),
        gateway_prepare_surface_available=_probe_gateway_prepare_surface_available(),
        gateway_execution_facade_connected=evaluate_gateway_execution_facade(
            merged_config=merged_config,
        ).execution_enabled,
        evidence_context_requested=evidence_context,
        operator_readiness_status=operator_status,
        consume_state=consume_state,
        repair_in_progress=repair_in_progress,
        production_signoff_ready=signoff.signoff_ready,
        production_cutover_ready=cutover.cutover_ready,
        production_root_hard_deny=_production_root_hard_deny_active(),
        production_execution_allowed=False,
        gateway_execution_allowed=False,
        recommended_action=recommended_action,
        recommended_next_phase=recommended_next_phase,
        checks=checks,
    )


def format_dispatch_gateway_readiness_summary(
    summary: CooDispatchGatewayReadinessSummary,
) -> str:
    """Format safe gateway readiness fields for CLI stdout."""
    lines = [
        "Gateway Readiness",
        "",
        f"gateway_state: {summary.gateway_state}",
        f"readiness_level: {summary.readiness_level}",
        f"gateway_readiness_ready: {str(summary.gateway_readiness_ready).lower()}",
        f"checks_passed_count: {summary.checks_passed_count}",
        f"checks_blocked_count: {summary.checks_blocked_count}",
        f"checks_failed_count: {summary.checks_failed_count}",
        f"failed_checks: {summary.failed_checks}",
        f"blocked_checks: {summary.blocked_checks}",
        (
            "gateway_ui_surface_available: "
            f"{str(summary.gateway_ui_surface_available).lower()}"
        ),
        (
            "gateway_session_model_available: "
            f"{str(summary.gateway_session_model_available).lower()}"
        ),
        (
            "gateway_prepare_surface_available: "
            f"{str(summary.gateway_prepare_surface_available).lower()}"
        ),
        (
            "gateway_execution_facade_connected: "
            f"{str(summary.gateway_execution_facade_connected).lower()}"
        ),
        f"evidence_context_requested: {summary.evidence_context_requested}",
        f"operator_readiness_status: {summary.operator_readiness_status}",
        f"consume_state: {summary.consume_state}",
        f"repair_in_progress: {str(summary.repair_in_progress).lower()}",
        f"production_signoff_ready: {str(summary.production_signoff_ready).lower()}",
        f"production_cutover_ready: {str(summary.production_cutover_ready).lower()}",
        (
            "production_root_hard_deny: "
            f"{str(summary.production_root_hard_deny).lower()}"
        ),
        (
            "production_execution_allowed: "
            f"{str(summary.production_execution_allowed).lower()}"
        ),
        (
            "gateway_execution_allowed: "
            f"{str(summary.gateway_execution_allowed).lower()}"
        ),
        f"recommended_action: {summary.recommended_action}",
        f"recommended_next_phase: {summary.recommended_next_phase}",
    ]
    return "\n".join(lines)
