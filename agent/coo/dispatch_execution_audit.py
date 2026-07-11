"""Dispatch execution audit — Phase 10L pre-execution snapshot persistence.

Writes immutable audit JSON under Hermes home only. No Repository2 writes.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

from hermes_constants import get_hermes_home

if TYPE_CHECKING:
    from agent.coo.execution_dispatch_runtime import (
        DispatchExecutionRequest,
        DispatchExecutionRun,
        DispatchUnlockToken,
    )
    from agent.coo.execution_dispatcher import ExecutionDispatchPlan
    from agent.coo.execution_execute import ExecuteGate, ExecuteRequest
    from agent.coo.execution_runtime import ExecutionRun
    from agent.coo.execution_ticket import ExecutionTicket
    from agent.coo.production_executor_policy import ProductionExecutorPolicy


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_audit_dir() -> Path:
    return get_hermes_home() / "coo" / "audit"


@dataclass
class DispatchExecutionAudit:
    """Immutable pre-execution audit record for a dispatch run."""

    audit_id: str
    dispatch_run_id: str
    dispatch_generation: int
    requested_by: str
    approved_by: str
    operator_id: str
    operator_name: str
    confirmation_id: str
    timestamp: str
    executor_policy: Dict[str, Any]
    pipeline_root: str
    command: str
    pre_execution_checklist: Dict[str, Any]
    snapshot: Dict[str, Any] = field(default_factory=dict)
    execution_attempt_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "execution_attempt_id": self.execution_attempt_id,
            "dispatch_run_id": self.dispatch_run_id,
            "dispatch_generation": self.dispatch_generation,
            "requested_by": self.requested_by,
            "approved_by": self.approved_by,
            "operator_id": self.operator_id,
            "operator_name": self.operator_name,
            "confirmation_id": self.confirmation_id,
            "timestamp": self.timestamp,
            "executor_policy": dict(self.executor_policy),
            "pipeline_root": self.pipeline_root,
            "command": self.command,
            "pre_execution_checklist": dict(self.pre_execution_checklist),
            "snapshot": dict(self.snapshot),
        }


def build_dispatch_execution_audit(
    *,
    dispatch_run: "DispatchExecutionRun",
    ticket: "ExecutionTicket",
    plan: "ExecutionDispatchPlan",
    dry_run: "ExecutionRun",
    execute_request: Optional["ExecuteRequest"] = None,
    gate: "ExecuteGate",
    token: "DispatchUnlockToken",
    dispatch_request: "DispatchExecutionRequest",
    executor_policy: ProductionExecutorPolicy,
    pipeline_root: str,
    entrypoint: str,
    run_date: str,
    pre_execution_checklist: Dict[str, Any],
    requested_by: str,
    operator_id: str = "",
    operator_name: str = "",
    confirmation_id: str = "",
    execution_attempt_id: str = "",
) -> DispatchExecutionAudit:
    """Build an audit record from dispatch context snapshots."""
    command = f"{entrypoint} (run_date={run_date})"
    snapshot: Dict[str, Any] = {
        "ticket": ticket.to_dict(),
        "plan": plan.to_dict(),
        "dry_run": dry_run.to_dict(),
        "gate": gate.to_dict(),
        "unlock_token": token.to_dict(),
        "dispatch_request": dispatch_request.to_dict(),
    }
    if execute_request is not None:
        snapshot["execute_request"] = execute_request.to_dict()

    return DispatchExecutionAudit(
        audit_id=str(uuid.uuid4()),
        dispatch_run_id=dispatch_run.dispatch_run_id,
        dispatch_generation=token.dispatch_generation,
        requested_by=requested_by,
        approved_by=gate.decided_by or "",
        operator_id=operator_id,
        operator_name=operator_name,
        confirmation_id=confirmation_id,
        timestamp=_utc_now_iso(),
        executor_policy=executor_policy.to_dict(),
        pipeline_root=pipeline_root,
        command=command,
        pre_execution_checklist=dict(pre_execution_checklist),
        snapshot=snapshot,
        execution_attempt_id=execution_attempt_id,
    )


def audit_to_json(audit: DispatchExecutionAudit) -> str:
    return json.dumps(audit.to_dict(), indent=2, sort_keys=True)


def audit_from_dict(payload: Dict[str, Any]) -> DispatchExecutionAudit:
    return DispatchExecutionAudit(
        audit_id=str(payload["audit_id"]),
        dispatch_run_id=str(payload["dispatch_run_id"]),
        dispatch_generation=int(payload["dispatch_generation"]),
        requested_by=str(payload["requested_by"]),
        approved_by=str(payload.get("approved_by") or ""),
        operator_id=str(payload.get("operator_id") or ""),
        operator_name=str(payload.get("operator_name") or ""),
        confirmation_id=str(payload.get("confirmation_id") or ""),
        timestamp=str(payload["timestamp"]),
        executor_policy=dict(payload.get("executor_policy") or {}),
        pipeline_root=str(payload["pipeline_root"]),
        command=str(payload["command"]),
        pre_execution_checklist=dict(payload.get("pre_execution_checklist") or {}),
        snapshot=dict(payload.get("snapshot") or {}),
        execution_attempt_id=str(payload.get("execution_attempt_id") or ""),
    )


def read_dispatch_execution_audit(
    dispatch_run_id: str,
    *,
    audit_dir: Optional[Path] = None,
) -> Optional[DispatchExecutionAudit]:
    """Read a persisted audit record by dispatch run id."""
    base = audit_dir or default_audit_dir()
    path = base / f"{dispatch_run_id}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return audit_from_dict(payload)


def _assert_audit_path_within_hermes_home(
    resolved: Path,
    hermes_root: Path,
    *,
    label: str,
) -> None:
    """Fail closed when an audit path would escape Hermes home."""
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ValueError(
            f"Audit {label} {resolved} must remain under Hermes home {hermes_root}"
        ) from exc


def write_dispatch_execution_audit(
    audit: DispatchExecutionAudit,
    audit_dir: Optional[Path] = None,
) -> Path:
    """Persist audit JSON under Hermes home — never under Repository2."""
    base = audit_dir or default_audit_dir()
    path = base / f"{audit.dispatch_run_id}.json"
    hermes_root = get_hermes_home().resolve()
    resolved_base = base.resolve()
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
    base.mkdir(parents=True, exist_ok=True)
    path.write_text(audit_to_json(audit), encoding="utf-8")
    return path
