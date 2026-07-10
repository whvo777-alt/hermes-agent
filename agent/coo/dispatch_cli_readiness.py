"""CLI dispatch operator readiness — Phase 10Z.

Read-only orchestration of executor config, bundle/confirmation persistence,
and policy preflight before a real dispatch run. No writes, consume, subprocess,
factory, or runner invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.coo.dispatch_bundle_store import (
    read_bundle,
    validate_bundle_for_cli_execution,
)
from agent.coo.dispatch_cli_config_validate import validate_dispatch_executor_config
from agent.coo.dispatch_cli_preflight import run_dispatch_policy_preflight
from agent.coo.production_executor_confirmation import (
    read_confirmation,
    validate_confirmation_for_cli_execution,
)

STEP_CLI_ARGS = "cli_args"
STEP_PIPELINE_ROOT_TRUST = "pipeline_root_trust"
STEP_EXECUTOR_CONFIG = "executor_config"
STEP_BUNDLE_PERSISTENCE = "bundle_persistence"
STEP_CONFIRMATION_PERSISTENCE = "confirmation_persistence"
STEP_POLICY_PREFLIGHT = "policy_preflight"


@dataclass(frozen=True)
class CooDispatchReadinessSummary:
    """Safe read-only readiness summary for operator pre-run checks."""

    ready: bool
    config_valid: bool = False
    persistence_valid: bool = False
    preflight: str = "not_run"
    checks_passed_count: Optional[int] = None
    checks_failed_count: Optional[int] = None
    failed_steps: tuple[str, ...] = ()


def _fail(
    *,
    failed_step: str,
    config_valid: bool = False,
    persistence_valid: bool = False,
    preflight: str = "not_run",
) -> CooDispatchReadinessSummary:
    return CooDispatchReadinessSummary(
        ready=False,
        config_valid=config_valid,
        persistence_valid=persistence_valid,
        preflight=preflight,
        failed_steps=(failed_step,),
    )


def evaluate_dispatch_operator_readiness(
    *,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchReadinessSummary:
    """Run ordered readiness checks without mutating persisted dispatch state."""
    normalized_ticket_id = (ticket_id or "").strip()
    normalized_confirmation_id = (confirmation_id or "").strip()
    normalized_pipeline_root = (pipeline_root or "").strip()
    if not normalized_ticket_id or not normalized_confirmation_id or not normalized_pipeline_root:
        return _fail(failed_step=STEP_CLI_ARGS)

    from hermes_cli.coo_dispatch import assert_cli_pipeline_root_trusted

    try:
        trusted_pipeline_root = assert_cli_pipeline_root_trusted(normalized_pipeline_root)
    except ValueError:
        return _fail(failed_step=STEP_PIPELINE_ROOT_TRUST)

    try:
        config_summary = validate_dispatch_executor_config(merged_config)
    except ValueError:
        return _fail(failed_step=STEP_EXECUTOR_CONFIG)

    try:
        bundle = read_bundle(
            normalized_ticket_id,
            bundle_dir=bundle_dir,
            reject_consumed=True,
        )
        if bundle.ticket_id != normalized_ticket_id:
            raise ValueError("ticket_id mismatch")
        validate_bundle_for_cli_execution(bundle)
    except (ValueError, KeyError):
        return _fail(failed_step=STEP_BUNDLE_PERSISTENCE, config_valid=True)

    try:
        confirmation = read_confirmation(
            normalized_confirmation_id,
            confirmation_dir=confirmation_dir,
            reject_consumed=True,
        )
        validate_confirmation_for_cli_execution(
            confirmation,
            bundle=bundle,
            expected_confirmation_id=normalized_confirmation_id,
        )
    except (ValueError, KeyError):
        return _fail(
            failed_step=STEP_CONFIRMATION_PERSISTENCE,
            config_valid=True,
        )

    preflight_summary = run_dispatch_policy_preflight(
        bundle=bundle,
        confirmation=confirmation,
        pipeline_root=trusted_pipeline_root,
        merged_config=merged_config,
    )
    if not preflight_summary.all_passed:
        return CooDispatchReadinessSummary(
            ready=False,
            config_valid=True,
            persistence_valid=True,
            preflight="failed",
            checks_passed_count=len(preflight_summary.passed_check_names),
            checks_failed_count=len(preflight_summary.failed_check_names),
            failed_steps=(STEP_POLICY_PREFLIGHT,),
        )

    return CooDispatchReadinessSummary(
        ready=True,
        config_valid=True,
        persistence_valid=True,
        preflight="passed",
        checks_passed_count=len(preflight_summary.passed_check_names),
        checks_failed_count=0,
        failed_steps=(),
    )


def format_dispatch_readiness_summary(summary: CooDispatchReadinessSummary) -> str:
    """Render a safe readiness summary without paths, tokens, or policy reasons."""
    if summary.ready:
        return "\n".join(
            [
                "readiness: ready",
                f"config_valid: {str(summary.config_valid).lower()}",
                f"persistence_valid: {str(summary.persistence_valid).lower()}",
                f"preflight: {summary.preflight}",
                f"checks_passed_count: {summary.checks_passed_count}",
                f"checks_failed_count: {summary.checks_failed_count}",
            ]
        )

    lines = ["readiness: not_ready"]
    if summary.failed_steps:
        lines.append(f"failed_steps: {','.join(summary.failed_steps)}")
    return "\n".join(lines)
