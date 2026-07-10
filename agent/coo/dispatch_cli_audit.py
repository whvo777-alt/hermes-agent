"""CLI dispatch execution audit read — Phase 11C.

Read-only lookup of persisted dispatch execution audit records. No writes,
subprocess, factory, runner, or dispatch execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.coo.dispatch_execution_audit import (
    DispatchExecutionAudit,
    default_audit_dir,
    read_dispatch_execution_audit,
)
from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class CooDispatchAuditSummary:
    """Safe read-only summary of a dispatch execution audit record."""

    audit_id: str
    dispatch_run_id: str
    dispatch_generation: int
    confirmation_id: str
    operator_id: str
    operator_name: str
    requested_by: str
    approved_by: str
    timestamp: str
    executor_enabled: bool
    pipeline_root_recorded: bool
    pre_execution_checklist: str
    checks_passed_count: int
    checks_failed_count: int
    snapshot_blocks: tuple[str, ...]


def _assert_audit_path_within_hermes_home(
    resolved: Path,
    hermes_root: Path,
    *,
    label: str,
) -> None:
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ValueError(
            f"Audit {label} must remain under Hermes home."
        ) from exc


def _normalize_dispatch_run_id(dispatch_run_id: str) -> str:
    normalized = (dispatch_run_id or "").strip()
    if not normalized:
        raise ValueError("dispatch_run_id is required")
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise ValueError("dispatch_run_id must not contain path separators.")
    return normalized


def _validate_audit_read_path(
    dispatch_run_id: str,
    audit_dir: Path,
) -> Path:
    hermes_root = get_hermes_home().resolve()
    resolved_base = audit_dir.resolve()
    path = audit_dir / f"{dispatch_run_id}.json"
    resolved_path = path.resolve()
    _assert_audit_path_within_hermes_home(
        resolved_base,
        hermes_root,
        label="directory",
    )
    _assert_audit_path_within_hermes_home(
        resolved_path,
        hermes_root,
        label="path",
    )
    return path


def _checklist_counts(checklist: dict[str, Any]) -> tuple[str, int, int]:
    checks = checklist.get("checks")
    if not isinstance(checks, list):
        all_passed = bool(checklist.get("all_passed"))
        return ("passed" if all_passed else "failed", 0, 0)

    passed = tuple(
        str(item.get("name") or "")
        for item in checks
        if isinstance(item, dict) and item.get("passed")
    )
    failed = tuple(
        str(item.get("name") or "")
        for item in checks
        if isinstance(item, dict) and not item.get("passed")
    )
    status = "passed" if bool(checklist.get("all_passed")) and not failed else "failed"
    return (status, len(passed), len(failed))


def _audit_to_summary(audit: DispatchExecutionAudit) -> CooDispatchAuditSummary:
    executor_policy = audit.executor_policy if isinstance(audit.executor_policy, dict) else {}
    checklist = (
        audit.pre_execution_checklist
        if isinstance(audit.pre_execution_checklist, dict)
        else {}
    )
    preflight_status, checks_passed_count, checks_failed_count = _checklist_counts(
        checklist
    )
    snapshot = audit.snapshot if isinstance(audit.snapshot, dict) else {}
    snapshot_blocks = tuple(sorted(str(key) for key in snapshot.keys()))

    return CooDispatchAuditSummary(
        audit_id=audit.audit_id,
        dispatch_run_id=audit.dispatch_run_id,
        dispatch_generation=audit.dispatch_generation,
        confirmation_id=audit.confirmation_id,
        operator_id=audit.operator_id,
        operator_name=audit.operator_name,
        requested_by=audit.requested_by,
        approved_by=audit.approved_by,
        timestamp=audit.timestamp,
        executor_enabled=bool(executor_policy.get("enabled")),
        pipeline_root_recorded=bool(str(audit.pipeline_root or "").strip()),
        pre_execution_checklist=preflight_status,
        checks_passed_count=checks_passed_count,
        checks_failed_count=checks_failed_count,
        snapshot_blocks=snapshot_blocks,
    )


def summarize_dispatch_execution_audit(
    dispatch_run_id: str,
    *,
    audit_dir: Path | None = None,
) -> CooDispatchAuditSummary:
    """Load a persisted audit record and build a safe operator summary."""
    normalized_dispatch_run_id = _normalize_dispatch_run_id(dispatch_run_id)
    base_dir = audit_dir or default_audit_dir()
    _validate_audit_read_path(normalized_dispatch_run_id, base_dir)

    try:
        audit = read_dispatch_execution_audit(
            normalized_dispatch_run_id,
            audit_dir=base_dir,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Dispatch execution audit JSON is corrupted for id {normalized_dispatch_run_id}."
        ) from exc

    if audit is None:
        raise KeyError(
            f"Dispatch execution audit not found for dispatch_run_id: {normalized_dispatch_run_id}"
        )

    return _audit_to_summary(audit)


def format_dispatch_audit_summary(summary: CooDispatchAuditSummary) -> str:
    """Render a safe audit summary without paths, commands, tokens, or snapshots."""
    lines = [
        f"audit_id: {summary.audit_id}",
        f"dispatch_run_id: {summary.dispatch_run_id}",
        f"dispatch_generation: {summary.dispatch_generation}",
        f"confirmation_id: {summary.confirmation_id}",
        f"operator_id: {summary.operator_id}",
        f"operator_name: {summary.operator_name}",
        f"requested_by: {summary.requested_by}",
        f"approved_by: {summary.approved_by}",
        f"timestamp: {summary.timestamp}",
        f"executor_enabled: {str(summary.executor_enabled).lower()}",
        f"pipeline_root_recorded: {str(summary.pipeline_root_recorded).lower()}",
        f"pre_execution_checklist: {summary.pre_execution_checklist}",
        f"checks_passed_count: {summary.checks_passed_count}",
        f"checks_failed_count: {summary.checks_failed_count}",
    ]
    if summary.snapshot_blocks:
        lines.append(f"snapshot_blocks: {','.join(summary.snapshot_blocks)}")
    return "\n".join(lines)
