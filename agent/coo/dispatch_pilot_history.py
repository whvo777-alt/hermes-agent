"""Pilot operations history persistence — Phase 13B.

Append-only JSON records under Hermes home. No Repository2 writes.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home

PILOT_HISTORY_VERSION = 1

PILOT_STATUS_SUCCESS = "success"
PILOT_STATUS_FAILURE = "failure"
PILOT_STATUS_TIMEOUT = "timeout"
PILOT_STATUS_DRY_RUN = "dry_run"

FAILURE_REASON_NONE = "none"
FAILURE_REASON_PREFLIGHT_FAILED = "preflight_failed"
FAILURE_REASON_RUNNER_FAILED = "runner_failed"
FAILURE_REASON_TIMEOUT = "timeout"
FAILURE_REASON_CONSUME_FAILED = "consume_failed"
FAILURE_REASON_POLICY_BLOCKED = "policy_blocked"
FAILURE_REASON_UNKNOWN_FAILURE = "unknown_failure"

EXECUTION_SCOPE_ISOLATED_CLONE = "isolated_clone"

_FORBIDDEN_RECORD_KEYS = frozenset(
    {
        "pipeline_root",
        "cwd",
        "argv",
        "env",
        "token",
        "phrase",
        "stdout",
        "stderr",
        "secret",
        "snapshot",
        "reason",
        "operator_reason",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_pilot_history_dir() -> Path:
    return get_hermes_home() / "coo" / "pilot-history"


def _assert_path_within_hermes_home(
    resolved: Path,
    hermes_root: Path,
    *,
    label: str,
) -> None:
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ValueError(f"Pilot history {label} must remain under Hermes home.") from exc


def _normalize_pilot_attempt_id(pilot_attempt_id: str) -> str:
    normalized = (pilot_attempt_id or "").strip()
    if not normalized:
        raise ValueError("pilot_attempt_id is required")
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise ValueError("pilot_attempt_id must not contain path separators.")
    return normalized


def _pilot_history_record_path(
    pilot_attempt_id: str,
    *,
    history_dir: Path,
) -> Path:
    normalized = _normalize_pilot_attempt_id(pilot_attempt_id)
    hermes_root = get_hermes_home().resolve()
    base_dir = history_dir.resolve()
    _assert_path_within_hermes_home(base_dir, hermes_root, label="directory")
    if base_dir.is_symlink():
        raise ValueError("Pilot history directory must not be a symlink.")
    path = (base_dir / f"{normalized}.json").resolve()
    _assert_path_within_hermes_home(path, hermes_root, label="path")
    if path.is_symlink():
        raise ValueError("Pilot history path must not be a symlink.")
    return path


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    hermes_root = get_hermes_home().resolve()
    resolved_path = path.resolve()
    _assert_path_within_hermes_home(resolved_path, hermes_root, label="path")
    if resolved_path.exists():
        raise ValueError("Pilot history record already exists.")
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = resolved_path.with_name(f".{resolved_path.name}.{uuid.uuid4().hex}.tmp")
    _assert_path_within_hermes_home(tmp_path.resolve(), hermes_root, label="temp file")
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, resolved_path)


def _validate_safe_payload(payload: Mapping[str, Any]) -> None:
    for key in payload:
        if key in _FORBIDDEN_RECORD_KEYS:
            raise ValueError(f"Pilot history record must not include {key!r}.")


@dataclass(frozen=True)
class CooDispatchPilotHistoryRecord:
    """Append-only isolated operational pilot history record."""

    version: int
    pilot_attempt_id: str
    execution_attempt_id: str
    ticket_id: str
    confirmation_id: str
    dispatch_run_id: str
    execution_scope: str
    status: str
    exit_code: int
    dry_run: bool
    started_at: str
    completed_at: str
    evidence_present: bool
    audit_present: bool
    consumed: bool
    failure_reason_code: str
    production_execution_allowed: bool
    production_root_hard_deny: bool
    gateway_enabled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "pilot_attempt_id": self.pilot_attempt_id,
            "execution_attempt_id": self.execution_attempt_id,
            "ticket_id": self.ticket_id,
            "confirmation_id": self.confirmation_id,
            "dispatch_run_id": self.dispatch_run_id,
            "execution_scope": self.execution_scope,
            "status": self.status,
            "exit_code": self.exit_code,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "evidence_present": self.evidence_present,
            "audit_present": self.audit_present,
            "consumed": self.consumed,
            "failure_reason_code": self.failure_reason_code,
            "production_execution_allowed": self.production_execution_allowed,
            "production_root_hard_deny": self.production_root_hard_deny,
            "gateway_enabled": self.gateway_enabled,
        }


def parse_pilot_history_record(payload: Mapping[str, Any]) -> CooDispatchPilotHistoryRecord:
    if not isinstance(payload, dict):
        raise ValueError("Pilot history payload must be a JSON object.")
    _validate_safe_payload(payload)
    required = (
        "version",
        "pilot_attempt_id",
        "execution_attempt_id",
        "ticket_id",
        "confirmation_id",
        "dispatch_run_id",
        "execution_scope",
        "status",
        "exit_code",
        "dry_run",
        "started_at",
        "completed_at",
        "evidence_present",
        "audit_present",
        "consumed",
        "failure_reason_code",
        "production_execution_allowed",
        "production_root_hard_deny",
        "gateway_enabled",
    )
    for key in required:
        if key not in payload:
            raise ValueError(f"Pilot history payload missing {key!r}.")
    return CooDispatchPilotHistoryRecord(
        version=int(payload["version"]),
        pilot_attempt_id=str(payload["pilot_attempt_id"]),
        execution_attempt_id=str(payload["execution_attempt_id"]),
        ticket_id=str(payload["ticket_id"]),
        confirmation_id=str(payload["confirmation_id"]),
        dispatch_run_id=str(payload["dispatch_run_id"]),
        execution_scope=str(payload["execution_scope"]),
        status=str(payload["status"]),
        exit_code=int(payload["exit_code"]),
        dry_run=bool(payload["dry_run"]),
        started_at=str(payload["started_at"]),
        completed_at=str(payload["completed_at"]),
        evidence_present=bool(payload["evidence_present"]),
        audit_present=bool(payload["audit_present"]),
        consumed=bool(payload["consumed"]),
        failure_reason_code=str(payload["failure_reason_code"]),
        production_execution_allowed=bool(payload["production_execution_allowed"]),
        production_root_hard_deny=bool(payload["production_root_hard_deny"]),
        gateway_enabled=bool(payload["gateway_enabled"]),
    )


def write_pilot_history_record(
    record: CooDispatchPilotHistoryRecord,
    *,
    history_dir: Path | None = None,
) -> Path:
    """Persist one append-only pilot history record."""
    payload = record.to_dict()
    _validate_safe_payload(payload)
    base_dir = history_dir or default_pilot_history_dir()
    path = _pilot_history_record_path(record.pilot_attempt_id, history_dir=base_dir)
    _atomic_write_json(path, payload)
    return path


def read_pilot_history_record(
    pilot_attempt_id: str,
    *,
    history_dir: Path | None = None,
) -> CooDispatchPilotHistoryRecord:
    """Load one pilot history record by id."""
    base_dir = history_dir or default_pilot_history_dir()
    path = _pilot_history_record_path(pilot_attempt_id, history_dir=base_dir)
    if not path.is_file() or path.is_symlink():
        raise KeyError(f"Pilot history record not found: {pilot_attempt_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Pilot history JSON is corrupted for id {pilot_attempt_id}."
        ) from exc
    record = parse_pilot_history_record(payload)
    if record.pilot_attempt_id != _normalize_pilot_attempt_id(pilot_attempt_id):
        raise ValueError("Pilot history pilot_attempt_id does not match path.")
    return record


def list_pilot_history_records(
    *,
    history_dir: Path | None = None,
    ticket_id: str | None = None,
) -> tuple[CooDispatchPilotHistoryRecord, ...]:
    """List pilot history records newest-first; fail-closed on corruption."""
    base_dir = history_dir or default_pilot_history_dir()
    hermes_root = get_hermes_home().resolve()
    resolved_dir = base_dir.resolve()
    _assert_path_within_hermes_home(resolved_dir, hermes_root, label="directory")
    if not resolved_dir.is_dir():
        return ()
    normalized_ticket_id = (ticket_id or "").strip() or None
    records: list[CooDispatchPilotHistoryRecord] = []
    for path in sorted(resolved_dir.glob("*.json")):
        if not path.is_file() or path.is_symlink():
            continue
        stem = path.stem
        if not stem or "/" in stem or "\\" in stem or stem in {".", ".."}:
            raise ValueError("Pilot history directory contains an invalid record id.")
        _pilot_history_record_path(stem, history_dir=resolved_dir)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = parse_pilot_history_record(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Pilot history JSON is corrupted for id {stem}."
            ) from exc
        if record.pilot_attempt_id != stem:
            raise ValueError("Pilot history record id does not match filename.")
        if normalized_ticket_id is not None and record.ticket_id != normalized_ticket_id:
            continue
        records.append(record)
    records.sort(key=lambda item: item.completed_at, reverse=True)
    return tuple(records)


def find_pilot_history_records_for_ticket(
    ticket_id: str,
    *,
    history_dir: Path | None = None,
) -> tuple[CooDispatchPilotHistoryRecord, ...]:
    """Find pilot history records for one ticket id, newest-first."""
    return list_pilot_history_records(history_dir=history_dir, ticket_id=ticket_id)
