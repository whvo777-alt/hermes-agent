"""CLI dispatch execution evidence read — Phase 12J.

Read-only correlation of dispatch audit JSON and execution evidence meta files.
No writes, subprocess, factory, runner, stdout/stderr body output, or path/argv/env
disclosure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.coo.dispatch_bundle_store import default_bundle_dir, read_bundle
from agent.coo.dispatch_cli_audit import _normalize_ticket_id
from agent.coo.dispatch_execution_audit import (
    DispatchExecutionAudit,
    audit_from_dict,
    default_audit_dir,
)
from agent.coo.production_executor_confirmation import (
    default_confirmation_dir,
    read_confirmation,
)
from agent.coo.production_executor_factory import default_evidence_dir
from hermes_constants import get_hermes_home

_TRUNCATION_MARKER = "...[truncated]"
_TIMEOUT_EXIT_CODE = 124


@dataclass(frozen=True)
class CooDispatchEvidenceSummary:
    """Safe read-only summary for one dispatch execution attempt."""

    execution_attempt_id: str
    dispatch_run_id: str
    ticket_id: str
    status: str
    exit_code: int
    started_at: str
    completed_at: str
    stdout_truncated: bool
    stderr_truncated: bool
    evidence_files_present: bool
    audit_present: bool
    consumed: str
    failure_reason: str


def _assert_path_within_hermes_home(
    resolved: Path,
    *,
    label: str,
) -> None:
    hermes_root = get_hermes_home().resolve()
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ValueError(f"Dispatch evidence {label} must remain under Hermes home.") from exc


def _normalize_execution_attempt_id(execution_attempt_id: str) -> str:
    normalized = (execution_attempt_id or "").strip()
    if not normalized:
        raise ValueError("execution_attempt_id is required")
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise ValueError("execution_attempt_id must not contain path separators.")
    return normalized


def _validate_dir(path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    _assert_path_within_hermes_home(resolved, label=label)
    return resolved


def _validate_evidence_meta_path(
    execution_attempt_id: str,
    evidence_dir: Path,
) -> Path:
    _validate_dir(evidence_dir, label="directory")
    path = evidence_dir / f"{execution_attempt_id}.meta.json"
    resolved = path.resolve()
    _assert_path_within_hermes_home(resolved, label="meta path")
    return path


def _load_json_file(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise KeyError(f"{label} not found.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON is corrupted.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} JSON must be an object.")
    return payload


def _load_evidence_meta(
    execution_attempt_id: str,
    *,
    evidence_dir: Path,
) -> dict[str, Any]:
    path = _validate_evidence_meta_path(execution_attempt_id, evidence_dir)
    meta = _load_json_file(path, label="Dispatch evidence meta")
    meta_attempt_id = str(meta.get("execution_attempt_id") or "")
    if not meta_attempt_id:
        raise ValueError("Dispatch evidence meta is legacy and lacks execution_attempt_id.")
    if meta_attempt_id != execution_attempt_id:
        raise ValueError("Dispatch evidence meta execution_attempt_id mismatch.")
    return meta


def _safe_log_truncated(meta: dict[str, Any], key: str) -> tuple[bool, bool]:
    raw_path = meta.get(key)
    if not isinstance(raw_path, str) or not raw_path.strip():
        return False, False
    path = Path(raw_path)
    resolved = path.resolve()
    _assert_path_within_hermes_home(resolved, label=f"{key} path")
    if not path.is_file():
        return False, False
    text = path.read_text(encoding="utf-8", errors="replace")
    return True, _TRUNCATION_MARKER in text


def _load_audits(audit_dir: Path) -> tuple[DispatchExecutionAudit, ...]:
    _validate_dir(audit_dir, label="audit directory")
    if not audit_dir.is_dir():
        return ()
    audits: list[DispatchExecutionAudit] = []
    for path in sorted(audit_dir.glob("*.json")):
        resolved = path.resolve()
        _assert_path_within_hermes_home(resolved, label="audit path")
        if "/" in path.stem or "\\" in path.stem or not path.stem:
            raise ValueError("audit directory contains an invalid dispatch run id.")
        payload = _load_json_file(path, label="Dispatch execution audit")
        audits.append(audit_from_dict(payload))
    return tuple(audits)


def _audit_ticket_id(audit: DispatchExecutionAudit) -> str:
    snapshot = audit.snapshot if isinstance(audit.snapshot, dict) else {}
    ticket = snapshot.get("ticket") if isinstance(snapshot, dict) else {}
    if isinstance(ticket, dict):
        return str(ticket.get("ticket_id") or "")
    return ""


def _find_audit_for_attempt(
    execution_attempt_id: str,
    *,
    audit_dir: Path,
) -> DispatchExecutionAudit | None:
    matches = [
        audit
        for audit in _load_audits(audit_dir)
        if audit.execution_attempt_id == execution_attempt_id
    ]
    if len(matches) > 1:
        raise ValueError("Multiple audits share execution_attempt_id.")
    return matches[0] if matches else None


def _consumed_summary(
    *,
    ticket_id: str,
    confirmation_id: str,
    bundle_dir: Path,
    confirmation_dir: Path,
) -> str:
    bundle_consumed = "unknown"
    confirmation_consumed = "unknown"
    if ticket_id:
        try:
            bundle = read_bundle(
                ticket_id,
                bundle_dir=bundle_dir,
                reject_consumed=False,
            )
            bundle_consumed = str(bool(bundle.consumed_at)).lower()
        except (KeyError, ValueError):
            bundle_consumed = "unavailable"
    if confirmation_id:
        try:
            confirmation = read_confirmation(
                confirmation_id,
                confirmation_dir=confirmation_dir,
                reject_consumed=False,
            )
            confirmation_consumed = str(bool(confirmation.consumed)).lower()
        except (KeyError, ValueError):
            confirmation_consumed = "unavailable"
    return f"bundle={bundle_consumed},confirmation={confirmation_consumed}"


def _status_and_reason(exit_code: int) -> tuple[str, str]:
    if exit_code == 0:
        return "completed", "none"
    if exit_code == _TIMEOUT_EXIT_CODE:
        return "failed", "timeout"
    return "failed", "exit_non_zero"


def _summary_from_meta_and_audit(
    execution_attempt_id: str,
    meta: dict[str, Any],
    audit: DispatchExecutionAudit | None,
    *,
    bundle_dir: Path,
    confirmation_dir: Path,
) -> CooDispatchEvidenceSummary:
    exit_code = int(meta.get("exit_code"))
    status, failure_reason = _status_and_reason(exit_code)
    stdout_present, stdout_truncated = _safe_log_truncated(meta, "stdout_log")
    stderr_present, stderr_truncated = _safe_log_truncated(meta, "stderr_log")

    ticket_id = _audit_ticket_id(audit) if audit is not None else ""
    dispatch_run_id = audit.dispatch_run_id if audit is not None else ""
    confirmation_id = audit.confirmation_id if audit is not None else ""
    consumed = _consumed_summary(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
    )

    return CooDispatchEvidenceSummary(
        execution_attempt_id=execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        ticket_id=ticket_id,
        status=status,
        exit_code=exit_code,
        started_at=str(meta.get("started_at") or ""),
        completed_at=str(meta.get("finished_at") or ""),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        evidence_files_present=stdout_present and stderr_present,
        audit_present=audit is not None,
        consumed=consumed,
        failure_reason=failure_reason,
    )


def summarize_dispatch_evidence_attempt(
    execution_attempt_id: str,
    *,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> CooDispatchEvidenceSummary:
    """Load and safely summarize one execution attempt by id."""
    normalized = _normalize_execution_attempt_id(execution_attempt_id)
    resolved_evidence_dir = evidence_dir or default_evidence_dir()
    resolved_audit_dir = audit_dir or default_audit_dir()
    meta = _load_evidence_meta(normalized, evidence_dir=resolved_evidence_dir)
    audit = _find_audit_for_attempt(normalized, audit_dir=resolved_audit_dir)
    if audit is not None and audit.execution_attempt_id != normalized:
        raise ValueError("Dispatch audit/evidence execution_attempt_id mismatch.")
    return _summary_from_meta_and_audit(
        normalized,
        meta,
        audit,
        bundle_dir=bundle_dir or default_bundle_dir(),
        confirmation_dir=confirmation_dir or default_confirmation_dir(),
    )


def find_dispatch_evidence_attempts_for_ticket(
    ticket_id: str,
    *,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> tuple[CooDispatchEvidenceSummary, ...]:
    """Find execution attempts for a ticket in newest-first order."""
    normalized_ticket_id = _normalize_ticket_id(ticket_id)
    resolved_evidence_dir = evidence_dir or default_evidence_dir()
    resolved_audit_dir = audit_dir or default_audit_dir()
    entries: list[CooDispatchEvidenceSummary] = []
    for audit in _load_audits(resolved_audit_dir):
        if _audit_ticket_id(audit) != normalized_ticket_id:
            continue
        if not audit.execution_attempt_id:
            raise ValueError("Dispatch audit is legacy and lacks execution_attempt_id.")
        summary = summarize_dispatch_evidence_attempt(
            audit.execution_attempt_id,
            evidence_dir=resolved_evidence_dir,
            audit_dir=resolved_audit_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
        entries.append(summary)
    entries.sort(key=lambda item: item.started_at, reverse=True)
    return tuple(entries)


def format_dispatch_evidence_summary(summary: CooDispatchEvidenceSummary) -> str:
    """Render a safe summary without cwd, argv, env, paths, or log bodies."""
    return "\n".join(
        [
            f"execution_attempt_id: {summary.execution_attempt_id}",
            f"dispatch_run_id: {summary.dispatch_run_id}",
            f"ticket_id: {summary.ticket_id}",
            f"status: {summary.status}",
            f"exit_code: {summary.exit_code}",
            f"started_at: {summary.started_at}",
            f"completed_at: {summary.completed_at}",
            f"stdout_truncated: {str(summary.stdout_truncated).lower()}",
            f"stderr_truncated: {str(summary.stderr_truncated).lower()}",
            f"evidence_files_present: {str(summary.evidence_files_present).lower()}",
            f"audit_present: {str(summary.audit_present).lower()}",
            f"consumed: {summary.consumed}",
            f"failure_reason: {summary.failure_reason}",
        ]
    )


def format_dispatch_evidence_find(
    ticket_id: str,
    entries: tuple[CooDispatchEvidenceSummary, ...],
) -> str:
    lines = [
        f"ticket_id: {ticket_id}",
        f"attempt_count: {len(entries)}",
    ]
    for entry in entries:
        lines.append("")
        lines.extend(
            [
                f"execution_attempt_id: {entry.execution_attempt_id}",
                f"dispatch_run_id: {entry.dispatch_run_id}",
                f"status: {entry.status}",
                f"exit_code: {entry.exit_code}",
                f"started_at: {entry.started_at}",
                f"completed_at: {entry.completed_at}",
                f"audit_present: {str(entry.audit_present).lower()}",
                f"evidence_files_present: {str(entry.evidence_files_present).lower()}",
                f"consumed: {entry.consumed}",
                f"failure_reason: {entry.failure_reason}",
            ]
        )
    return "\n".join(lines)

