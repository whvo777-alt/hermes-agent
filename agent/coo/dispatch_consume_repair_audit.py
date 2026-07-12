"""Append-only consume repair audit records — Phase 12O."""

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
