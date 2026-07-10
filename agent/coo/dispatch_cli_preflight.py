"""CLI dispatch run policy preflight — Phase 10U dry-run only.

Evaluates production executor policy against hydrated bundle evidence. No
subprocess, factory, runner, dispatch execution, or persistence writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.dispatch_bundle_store import DispatchExecutionBundle
from agent.coo.dispatch_cli_run import hydrate_dispatch_evidence_from_bundle
from agent.coo.dispatch_executor_config import load_dispatch_executor_policy
from agent.coo.production_executor_confirmation import ProductionExecutorConfirmation
from agent.coo.production_executor_factory import _ALLOWED_FACTORY_ENTRYPOINT
from agent.coo.production_executor_policy import assert_production_executor_allowed


@dataclass(frozen=True)
class CooDispatchPreflightSummary:
    """Safe pass/fail summary for CLI dry-run preflight."""

    all_passed: bool
    passed_check_names: tuple[str, ...]
    failed_check_names: tuple[str, ...]


def run_dispatch_policy_preflight(
    *,
    bundle: DispatchExecutionBundle,
    confirmation: ProductionExecutorConfirmation,
    pipeline_root: str,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchPreflightSummary:
    """Run policy checklist against bundle evidence without executing dispatch."""
    policy = load_dispatch_executor_policy(merged_config)
    evidence = hydrate_dispatch_evidence_from_bundle(bundle)
    checklist = assert_production_executor_allowed(
        policy,
        ticket=evidence["ticket"],
        plan=evidence["plan"],
        dry_run=evidence["dry_run"],
        gate=evidence["gate"],
        token=evidence["token"],
        dispatch_request=evidence["dispatch_request"],
        pipeline_root=pipeline_root,
        entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
        target_skills=list(evidence["token"].target_skills),
        confirmation=confirmation,
    )
    passed = tuple(
        str(item["name"])
        for item in checklist["checks"]
        if item.get("passed")
    )
    failed = tuple(
        str(item["name"])
        for item in checklist["checks"]
        if not item.get("passed")
    )
    return CooDispatchPreflightSummary(
        all_passed=bool(checklist["all_passed"]),
        passed_check_names=passed,
        failed_check_names=failed,
    )


def format_dispatch_preflight_summary(summary: CooDispatchPreflightSummary) -> str:
    """Render a safe preflight summary without policy paths, tokens, or reasons."""
    lines = [
        f"preflight: {'passed' if summary.all_passed else 'failed'}",
        f"checks_passed_count: {len(summary.passed_check_names)}",
        f"checks_failed_count: {len(summary.failed_check_names)}",
    ]
    if summary.failed_check_names:
        lines.append(f"failed_checks: {','.join(summary.failed_check_names)}")
    return "\n".join(lines)
