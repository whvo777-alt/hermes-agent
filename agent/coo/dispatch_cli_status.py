"""Read-only dispatch persistence status — Phase 10S / 10W / 11E preflight.

Loads bundle and optional confirmation files under Hermes home and returns a
safe summary. Optional read-only policy preflight when both confirmation id and
pipeline root are supplied. No dispatch execution, no writes, no subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.coo.dispatch_bundle_store import read_bundle
from agent.coo.dispatch_executor_config import load_dispatch_executor_policy
from agent.coo.dispatch_cli_validation_core import (
    DispatchPreRunValidationFailure,
    re_raise_dispatch_pre_run_failure,
    validate_dispatch_pre_run,
)


@dataclass(frozen=True)
class CooDispatchStatusSummary:
    """Safe, read-only dispatch persistence summary for CLI output."""

    ticket_id: str
    bundle_id: str
    dispatch_request_id: str
    dispatch_generation: int
    bundle_consumed: bool
    remint_pending_prepare: bool
    gate_status: str
    ticket_status: str
    confirmation_id: str = ""
    confirmation_consumed: Optional[bool] = None
    confirmation_expired: Optional[bool] = None
    executor_enabled: bool = False
    executor_allowlist_count: int = 0
    preflight: str = "not_requested"
    checks_passed_count: Optional[int] = None
    checks_failed_count: Optional[int] = None
    failed_checks: tuple[str, ...] = ()
    pipeline_root_attested: Optional[bool] = None
    pipeline_root_matches: Optional[bool] = None


def _confirmation_is_expired(expires_at: str) -> bool:
    expires = datetime.fromisoformat(expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires


def _resolve_status_preflight_inputs(
    confirmation_id: str | None,
    pipeline_root: str | None,
) -> tuple[bool, str | None, str | None]:
    """Require confirmation id and pipeline root together for preflight."""
    confirmation_supplied = confirmation_id is not None
    pipeline_root_supplied = pipeline_root is not None
    if confirmation_supplied != pipeline_root_supplied:
        raise ValueError(
            "status preflight requires both --confirmation-id and --pipeline-root."
        )
    if not confirmation_supplied:
        return False, None, None

    normalized_confirmation_id = confirmation_id.strip()
    normalized_pipeline_root = pipeline_root.strip()
    if not normalized_confirmation_id:
        raise ValueError("confirmation_id must not be empty when provided.")
    if not normalized_pipeline_root:
        raise ValueError("pipeline_root must not be empty when provided.")
    return True, normalized_confirmation_id, normalized_pipeline_root


def summarize_dispatch_persistence_status(
    *,
    ticket_id: str,
    confirmation_id: str | None = None,
    pipeline_root: str | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchStatusSummary:
    """Load persisted bundle/confirmation files and build a safe status summary."""
    policy = load_dispatch_executor_policy(merged_config)
    normalized_ticket_id = ticket_id.strip()
    if not normalized_ticket_id:
        raise ValueError("ticket_id is required")

    preflight_requested, normalized_confirmation_id, normalized_pipeline_root = (
        _resolve_status_preflight_inputs(confirmation_id, pipeline_root)
    )

    confirmation_consumed: bool | None = None
    confirmation_expired: bool | None = None
    resolved_confirmation_id = ""
    pipeline_root_attested: bool | None = None
    pipeline_root_matches: bool | None = None

    preflight_status = "not_requested"
    checks_passed_count: int | None = None
    checks_failed_count: int | None = None
    failed_checks: tuple[str, ...] = ()

    if preflight_requested:
        assert normalized_confirmation_id is not None
        assert normalized_pipeline_root is not None
        try:
            validated = validate_dispatch_pre_run(
                ticket_id=normalized_ticket_id,
                confirmation_id=normalized_confirmation_id,
                pipeline_root=normalized_pipeline_root,
                bundle_dir=bundle_dir,
                confirmation_dir=confirmation_dir,
                merged_config=merged_config,
            )
        except DispatchPreRunValidationFailure as exc:
            re_raise_dispatch_pre_run_failure(exc)

        bundle = validated.bundle
        confirmation = validated.confirmation
        resolved_confirmation_id = confirmation.confirmation_id
        confirmation_consumed = bool(confirmation.consumed)
        confirmation_expired = _confirmation_is_expired(confirmation.expires_at)
        pipeline_root_attested = True
        pipeline_root_matches = True
        preflight_summary = validated.preflight
        preflight_status = "passed" if preflight_summary.all_passed else "failed"
        checks_passed_count = len(preflight_summary.passed_check_names)
        checks_failed_count = len(preflight_summary.failed_check_names)
        failed_checks = preflight_summary.failed_check_names
    else:
        bundle = read_bundle(
            normalized_ticket_id,
            bundle_dir=bundle_dir,
            reject_consumed=False,
        )
        if bundle.ticket_id != normalized_ticket_id:
            raise ValueError("Dispatch bundle ticket_id does not match CLI input.")

    snapshot = bundle.snapshot
    gate = snapshot.get("gate") if isinstance(snapshot, dict) else {}
    ticket = snapshot.get("ticket") if isinstance(snapshot, dict) else {}
    if not isinstance(gate, dict) or not isinstance(ticket, dict):
        raise ValueError("Dispatch bundle snapshot is missing gate or ticket blocks.")

    return CooDispatchStatusSummary(
        ticket_id=bundle.ticket_id,
        bundle_id=bundle.bundle_id,
        dispatch_request_id=bundle.dispatch_request_id,
        dispatch_generation=bundle.dispatch_generation,
        bundle_consumed=bool(bundle.consumed_at),
        remint_pending_prepare=bool(snapshot.get("_remint_pending_prepare")),
        gate_status=str(gate.get("status") or ""),
        ticket_status=str(ticket.get("status") or ""),
        confirmation_id=resolved_confirmation_id,
        confirmation_consumed=confirmation_consumed,
        confirmation_expired=confirmation_expired,
        executor_enabled=policy.enabled,
        executor_allowlist_count=len(policy.allowed_pipeline_roots),
        preflight=preflight_status,
        checks_passed_count=checks_passed_count,
        checks_failed_count=checks_failed_count,
        failed_checks=failed_checks,
        pipeline_root_attested=pipeline_root_attested,
        pipeline_root_matches=pipeline_root_matches,
    )


def format_dispatch_status_summary(summary: CooDispatchStatusSummary) -> str:
    """Render a safe text summary without secrets or snapshot dumps."""
    lines = [
        f"ticket_id: {summary.ticket_id}",
        f"bundle_id: {summary.bundle_id}",
        f"dispatch_request_id: {summary.dispatch_request_id}",
        f"dispatch_generation: {summary.dispatch_generation}",
        f"bundle_consumed: {str(summary.bundle_consumed).lower()}",
        f"remint_pending_prepare: {str(summary.remint_pending_prepare).lower()}",
        f"gate_status: {summary.gate_status}",
        f"ticket_status: {summary.ticket_status}",
    ]
    if summary.confirmation_id:
        lines.extend(
            [
                f"confirmation_id: {summary.confirmation_id}",
                f"confirmation_consumed: {str(summary.confirmation_consumed).lower()}",
                f"confirmation_expired: {str(summary.confirmation_expired).lower()}",
            ]
        )
        if summary.pipeline_root_attested is not None:
            lines.append(
                f"pipeline_root_attested: {str(summary.pipeline_root_attested).lower()}"
            )
        if summary.pipeline_root_matches is not None:
            lines.append(
                f"pipeline_root_matches: {str(summary.pipeline_root_matches).lower()}"
            )
    lines.extend(
        [
            f"executor_enabled: {str(summary.executor_enabled).lower()}",
            f"executor_allowlist_count: {summary.executor_allowlist_count}",
            f"preflight: {summary.preflight}",
        ]
    )
    if summary.preflight != "not_requested":
        lines.append(f"checks_passed_count: {summary.checks_passed_count}")
        lines.append(f"checks_failed_count: {summary.checks_failed_count}")
        if summary.failed_checks:
            lines.append(f"failed_checks: {','.join(summary.failed_checks)}")
    return "\n".join(lines)
