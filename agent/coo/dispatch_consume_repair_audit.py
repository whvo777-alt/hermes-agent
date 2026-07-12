"""Append-only consume repair audit records — Phase 12O / read-only access — Phase 12Q."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from hermes_constants import get_hermes_home


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_consume_repair_audit_dir() -> Path:
    return get_hermes_home() / "coo" / "consume-repair-audit"


def _assert_path_within_hermes_home(
    resolved: Path,
    hermes_root: Path,
    *,
    label: str,
) -> None:
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ValueError(
            f"Consume repair audit {label} must remain under Hermes home."
        ) from exc


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    hermes_root = get_hermes_home().resolve()
    resolved_path = path.resolve()
    _assert_path_within_hermes_home(resolved_path, hermes_root, label="path")
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = resolved_path.with_name(f".{resolved_path.name}.{uuid.uuid4().hex}.tmp")
    _assert_path_within_hermes_home(tmp_path.resolve(), hermes_root, label="temp file")
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, resolved_path)


@dataclass(frozen=True)
class CooDispatchConsumeRepairAuditRecord:
    """Append-only consume repair audit summary."""

    repair_attempt_id: str
    repair_action: str
    ticket_id: str
    confirmation_id: str
    transaction_id: str
    execution_attempt_id: str
    consume_state_before: str
    consume_state_after: str
    operator_id: str
    operator_name: str
    reason: str
    phrase_verified: bool
    applied_at: str
    outcome: str = "applied"
    correlation_valid: bool = False
    evidence_success: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repair_attempt_id": self.repair_attempt_id,
            "repair_action": self.repair_action,
            "ticket_id": self.ticket_id,
            "confirmation_id": self.confirmation_id,
            "transaction_id": self.transaction_id,
            "execution_attempt_id": self.execution_attempt_id,
            "consume_state_before": self.consume_state_before,
            "consume_state_after": self.consume_state_after,
            "operator_id": self.operator_id,
            "operator_name": self.operator_name,
            "reason": self.reason,
            "phrase_verified": self.phrase_verified,
            "applied_at": self.applied_at,
            "outcome": self.outcome,
            "correlation_valid": self.correlation_valid,
            "evidence_success": self.evidence_success,
        }


def append_consume_repair_audit(
    record: CooDispatchConsumeRepairAuditRecord,
    *,
    audit_dir: Path | None = None,
) -> None:
    """Persist an append-only consume repair audit record."""
    base_dir = audit_dir or default_consume_repair_audit_dir()
    path = base_dir / f"{record.repair_attempt_id}.json"
    if path.exists():
        raise ValueError("Consume repair audit record already exists.")
    _atomic_write_json(path, record.to_dict())


def _normalize_repair_attempt_id(repair_attempt_id: str) -> str:
    normalized = (repair_attempt_id or "").strip()
    if not normalized:
        raise ValueError("repair_attempt_id is required")
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise ValueError("repair_attempt_id must not contain path separators.")
    return normalized


def _audit_record_path(repair_attempt_id: str, *, audit_dir: Path) -> Path:
    normalized = _normalize_repair_attempt_id(repair_attempt_id)
    hermes_root = get_hermes_home().resolve()
    base_dir = audit_dir.resolve()
    _assert_path_within_hermes_home(base_dir, hermes_root, label="directory")
    path = (base_dir / f"{normalized}.json").resolve()
    _assert_path_within_hermes_home(path, hermes_root, label="path")
    return path


def _parse_repair_audit_payload(payload: Mapping[str, Any]) -> CooDispatchConsumeRepairAuditRecord:
    if not isinstance(payload, dict):
        raise ValueError("Consume repair audit payload must be a JSON object.")
    required = (
        "repair_attempt_id",
        "repair_action",
        "ticket_id",
        "confirmation_id",
        "transaction_id",
        "execution_attempt_id",
        "consume_state_before",
        "consume_state_after",
        "operator_id",
        "operator_name",
        "reason",
        "phrase_verified",
        "applied_at",
    )
    for key in required:
        if key not in payload:
            raise ValueError(f"Consume repair audit payload missing {key!r}.")
    return CooDispatchConsumeRepairAuditRecord(
        repair_attempt_id=str(payload["repair_attempt_id"]),
        repair_action=str(payload["repair_action"]),
        ticket_id=str(payload["ticket_id"]),
        confirmation_id=str(payload["confirmation_id"]),
        transaction_id=str(payload["transaction_id"]),
        execution_attempt_id=str(payload["execution_attempt_id"]),
        consume_state_before=str(payload["consume_state_before"]),
        consume_state_after=str(payload["consume_state_after"]),
        operator_id=str(payload["operator_id"]),
        operator_name=str(payload["operator_name"]),
        reason=str(payload["reason"]),
        phrase_verified=bool(payload["phrase_verified"]),
        applied_at=str(payload["applied_at"]),
        outcome=str(payload.get("outcome") or "applied"),
        correlation_valid=bool(payload.get("correlation_valid")),
        evidence_success=bool(payload.get("evidence_success")),
    )


def read_consume_repair_audit(
    repair_attempt_id: str,
    *,
    audit_dir: Path | None = None,
) -> CooDispatchConsumeRepairAuditRecord:
    """Load a persisted consume repair audit record by id."""
    base_dir = audit_dir or default_consume_repair_audit_dir()
    path = _audit_record_path(repair_attempt_id, audit_dir=base_dir)
    if not path.is_file():
        raise KeyError(f"Consume repair audit record not found: {repair_attempt_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Consume repair audit JSON is corrupted for id {repair_attempt_id}."
        ) from exc
    record = _parse_repair_audit_payload(payload)
    if record.repair_attempt_id != _normalize_repair_attempt_id(repair_attempt_id):
        raise ValueError("Consume repair audit repair_attempt_id does not match path.")
    return record


def list_consume_repair_audits(
    *,
    audit_dir: Path | None = None,
    ticket_id: str | None = None,
) -> list[CooDispatchConsumeRepairAuditRecord]:
    """List consume repair audit records newest-first."""
    base_dir = audit_dir or default_consume_repair_audit_dir()
    hermes_root = get_hermes_home().resolve()
    resolved_dir = base_dir.resolve()
    _assert_path_within_hermes_home(resolved_dir, hermes_root, label="directory")
    if not resolved_dir.is_dir():
        return []
    normalized_ticket_id = (ticket_id or "").strip() or None
    records: list[CooDispatchConsumeRepairAuditRecord] = []
    for path in sorted(resolved_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = _parse_repair_audit_payload(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if normalized_ticket_id is not None and record.ticket_id != normalized_ticket_id:
            continue
        records.append(record)
    records.sort(key=lambda item: item.applied_at, reverse=True)
    return records


def find_latest_failed_partial_forward_complete_audit(
    *,
    ticket_id: str,
    confirmation_id: str,
    execution_attempt_id: str,
    transaction_id: str,
    audit_dir: Path | None = None,
) -> CooDispatchConsumeRepairAuditRecord | None:
    """Return the newest failed partial forward-complete audit for a consume pair."""
    candidates = list_consume_repair_audits(audit_dir=audit_dir, ticket_id=ticket_id)
    for record in candidates:
        if record.confirmation_id != confirmation_id:
            continue
        if record.execution_attempt_id != execution_attempt_id:
            continue
        if record.transaction_id != transaction_id:
            continue
        if record.repair_action != "repair_action_partial_forward_complete":
            continue
        if record.outcome != "failed":
            continue
        if record.consume_state_after != "recovery_required":
            continue
        return record
    return None
