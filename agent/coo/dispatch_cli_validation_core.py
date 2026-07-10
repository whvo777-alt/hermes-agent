"""Shared dispatch pre-run validation — Phase 11B.

Single ordered validation path for readiness, run, and status preflight.
No writes, consume, subprocess, factory, or runner invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.coo.dispatch_bundle_store import (
    DispatchExecutionBundle,
    read_bundle,
    validate_bundle_for_cli_execution,
)
from agent.coo.dispatch_cli_config_validate import validate_dispatch_executor_config
from agent.coo.dispatch_cli_preflight import (
    CooDispatchPreflightSummary,
    run_dispatch_policy_preflight,
)
from agent.coo.dispatch_pipeline_root_trust import assert_pipeline_root_matches_attestation
from agent.coo.production_executor_confirmation import (
    ProductionExecutorConfirmation,
    read_confirmation,
    validate_confirmation_for_cli_execution,
)

STEP_CLI_ARGS = "cli_args"
STEP_PIPELINE_ROOT_TRUST = "pipeline_root_trust"
STEP_EXECUTOR_CONFIG = "executor_config"
STEP_BUNDLE_PERSISTENCE = "bundle_persistence"
STEP_CONFIRMATION_PERSISTENCE = "confirmation_persistence"
STEP_PIPELINE_ROOT_ATTESTATION = "pipeline_root_attestation"
STEP_POLICY_PREFLIGHT = "policy_preflight"


@dataclass(frozen=True)
class CooDispatchPreRunValidationResult:
    """Validated dispatch evidence and safe preflight summary."""

    trusted_pipeline_root: str
    bundle: DispatchExecutionBundle
    confirmation: ProductionExecutorConfirmation
    preflight: CooDispatchPreflightSummary


@dataclass(frozen=True)
class DispatchPreRunValidationFailure(Exception):
    """Structured pre-run validation failure for readiness soft mapping."""

    step: str
    config_valid: bool = False
    persistence_valid: bool = False
    preflight: Optional[CooDispatchPreflightSummary] = None
    cause_exc: Optional[BaseException] = None

    def __str__(self) -> str:
        return f"dispatch pre-run validation failed at step: {self.step}"


def raise_dispatch_pre_run_failure(
    step: str,
    exc: BaseException,
    *,
    config_valid: bool = False,
    persistence_valid: bool = False,
    preflight: Optional[CooDispatchPreflightSummary] = None,
) -> None:
    """Raise a structured validation failure preserving the original cause."""
    raise DispatchPreRunValidationFailure(
        step=step,
        config_valid=config_valid,
        persistence_valid=persistence_valid,
        preflight=preflight,
        cause_exc=exc,
    ) from exc


def re_raise_dispatch_pre_run_failure(exc: DispatchPreRunValidationFailure) -> None:
    """Re-raise the original CLI-facing error for run/status callers."""
    if exc.cause_exc is not None:
        raise exc.cause_exc
    raise ValueError(str(exc)) from exc


def validate_dispatch_pre_run(
    *,
    ticket_id: str,
    confirmation_id: str,
    pipeline_root: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchPreRunValidationResult:
    """Run ordered pre-run validation without mutating persisted dispatch state."""
    normalized_ticket_id = (ticket_id or "").strip()
    normalized_confirmation_id = (confirmation_id or "").strip()
    normalized_pipeline_root = (pipeline_root or "").strip()
    if not normalized_ticket_id or not normalized_confirmation_id or not normalized_pipeline_root:
        raise_dispatch_pre_run_failure(
            STEP_CLI_ARGS,
            ValueError("ticket_id, confirmation_id, and pipeline_root are required"),
        )

    from hermes_cli.coo_dispatch import assert_cli_pipeline_root_trusted

    try:
        trusted_pipeline_root = assert_cli_pipeline_root_trusted(normalized_pipeline_root)
    except ValueError as exc:
        raise_dispatch_pre_run_failure(STEP_PIPELINE_ROOT_TRUST, exc)

    try:
        validate_dispatch_executor_config(merged_config)
    except ValueError as exc:
        raise_dispatch_pre_run_failure(STEP_EXECUTOR_CONFIG, exc)

    try:
        bundle = read_bundle(
            normalized_ticket_id,
            bundle_dir=bundle_dir,
            reject_consumed=True,
        )
        if bundle.ticket_id != normalized_ticket_id:
            raise ValueError("ticket_id mismatch")
        validate_bundle_for_cli_execution(bundle)
    except KeyError:
        raise
    except (ValueError, KeyError) as exc:
        raise_dispatch_pre_run_failure(
            STEP_BUNDLE_PERSISTENCE,
            exc,
            config_valid=True,
        )

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
    except KeyError as exc:
        raise_dispatch_pre_run_failure(
            STEP_CONFIRMATION_PERSISTENCE,
            exc,
            config_valid=True,
        )
    except ValueError as exc:
        raise_dispatch_pre_run_failure(
            STEP_CONFIRMATION_PERSISTENCE,
            exc,
            config_valid=True,
        )

    try:
        assert_pipeline_root_matches_attestation(
            cli_pipeline_root=normalized_pipeline_root,
            attested_pipeline_root=confirmation.attested_pipeline_root,
        )
    except ValueError as exc:
        raise_dispatch_pre_run_failure(
            STEP_PIPELINE_ROOT_ATTESTATION,
            exc,
            config_valid=True,
            persistence_valid=True,
        )

    preflight_summary = run_dispatch_policy_preflight(
        bundle=bundle,
        confirmation=confirmation,
        pipeline_root=trusted_pipeline_root,
        merged_config=merged_config,
    )

    return CooDispatchPreRunValidationResult(
        trusted_pipeline_root=trusted_pipeline_root,
        bundle=bundle,
        confirmation=confirmation,
        preflight=preflight_summary,
    )
