"""CLI dispatch operator readiness — Phase 10Z / 11B validation core.

Read-only orchestration of executor config, bundle/confirmation persistence,
and policy preflight before a real dispatch run. No writes, consume, subprocess,
factory, or runner invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.coo.dispatch_cli_validation_core import (
    STEP_BUNDLE_PERSISTENCE,
    STEP_CLI_ARGS,
    STEP_CONFIRMATION_PERSISTENCE,
    STEP_EXECUTOR_CONFIG,
    STEP_PIPELINE_ROOT_ATTESTATION,
    STEP_PIPELINE_ROOT_TRUST,
    STEP_POLICY_PREFLIGHT,
    DispatchPreRunValidationFailure,
    validate_dispatch_pre_run,
)


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
    checks_passed_count: Optional[int] = None,
    checks_failed_count: Optional[int] = None,
) -> CooDispatchReadinessSummary:
    return CooDispatchReadinessSummary(
        ready=False,
        config_valid=config_valid,
        persistence_valid=persistence_valid,
        preflight=preflight,
        checks_passed_count=checks_passed_count,
        checks_failed_count=checks_failed_count,
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
    try:
        validated = validate_dispatch_pre_run(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            pipeline_root=pipeline_root,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            merged_config=merged_config,
        )
    except DispatchPreRunValidationFailure as exc:
        preflight = "failed" if exc.step == STEP_POLICY_PREFLIGHT else "not_run"
        return _fail(
            failed_step=exc.step,
            config_valid=exc.config_valid,
            persistence_valid=exc.persistence_valid,
            preflight=preflight,
            checks_passed_count=(
                len(exc.preflight.passed_check_names) if exc.preflight else None
            ),
            checks_failed_count=(
                len(exc.preflight.failed_check_names) if exc.preflight else None
            ),
        )
    except KeyError:
        return _fail(failed_step=STEP_BUNDLE_PERSISTENCE, config_valid=True)

    preflight_summary = validated.preflight
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
