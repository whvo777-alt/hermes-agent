"""Dispatch execution bundle persistence — Phase 10P cross-process CLI bridge.

Stores ticket/token/request/gate snapshots as JSON under Hermes home only.
No Repository 2 execution or mutation.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

_BUNDLE_VERSION = 1
_REQUIRED_BUNDLE_IDS = (
    "bundle_id",
    "ticket_id",
    "plan_id",
    "dry_run_run_id",
    "execute_request_id",
    "gate_id",
    "unlock_token_id",
    "dispatch_request_id",
    "dispatch_generation",
    "requester_id",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_bundle_dir() -> Path:
    return get_hermes_home() / "coo" / "dispatch-bundles"


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
            f"Bundle {label} {resolved} must remain under Hermes home {hermes_root}"
        ) from exc


def _bundle_path(ticket_id: str, bundle_dir: Path) -> Path:
    return bundle_dir / f"{ticket_id}.json"


def _validate_bundle_paths(ticket_id: str, bundle_dir: Path) -> tuple[Path, Path]:
    hermes_root = get_hermes_home().resolve()
    resolved_base = bundle_dir.resolve()
    path = _bundle_path(ticket_id, bundle_dir)
    resolved_path = path.resolve()
    _assert_path_within_hermes_home(resolved_base, hermes_root, label="directory")
    _assert_path_within_hermes_home(resolved_path, hermes_root, label="path")
    return resolved_base, resolved_path


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


@dataclass(frozen=True)
class DispatchExecutionBundle:
    """Frozen dispatch snapshot bundle for cross-process CLI reads."""

    bundle_id: str
    ticket_id: str
    plan_id: str
    dry_run_run_id: str
    execute_request_id: str
    gate_id: str
    unlock_token_id: str
    dispatch_request_id: str
    dispatch_generation: int
    requester_id: str
    created_at: str
    updated_at: str
    snapshot: Dict[str, Any]
    consumed_at: str = ""
    version: int = _BUNDLE_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "ticket_id": self.ticket_id,
            "plan_id": self.plan_id,
            "dry_run_run_id": self.dry_run_run_id,
            "execute_request_id": self.execute_request_id,
            "gate_id": self.gate_id,
            "unlock_token_id": self.unlock_token_id,
            "dispatch_request_id": self.dispatch_request_id,
            "dispatch_generation": self.dispatch_generation,
            "requester_id": self.requester_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "consumed_at": self.consumed_at,
            "version": self.version,
            "snapshot": dict(self.snapshot),
        }


def build_dispatch_execution_bundle(
    *,
    ticket,
    plan,
    dry_run,
    dry_run_request,
    execute_request,
    gate,
    token,
    dispatch_request,
    bundle_id: str | None = None,
    created_at: str | None = None,
    consumed_at: str = "",
) -> DispatchExecutionBundle:
    """Build a bundle from live dispatch records."""
    now = _utc_now_iso()
    snapshot = {
        "ticket": ticket.to_dict(),
        "plan": plan.to_dict(),
        "dry_run": dry_run.to_dict(),
        "dry_run_request": dry_run_request.to_dict(),
        "execute_request": execute_request.to_dict(),
        "gate": gate.to_dict(),
        "unlock_token": token.to_dict(),
        "dispatch_request": dispatch_request.to_dict(),
    }
    return DispatchExecutionBundle(
        bundle_id=bundle_id or str(uuid.uuid4()),
        ticket_id=ticket.ticket_id,
        plan_id=plan.plan_id,
        dry_run_run_id=dry_run.run_id,
        execute_request_id=execute_request.execute_request_id,
        gate_id=gate.gate_id,
        unlock_token_id=token.token_id,
        dispatch_request_id=dispatch_request.dispatch_request_id,
        dispatch_generation=token.dispatch_generation,
        requester_id=ticket.requester_id,
        created_at=created_at or now,
        updated_at=now,
        consumed_at=consumed_at,
        snapshot=snapshot,
    )


def _validate_bundle_payload(payload: Dict[str, Any]) -> DispatchExecutionBundle:
    if not isinstance(payload, dict):
        raise ValueError("Dispatch bundle payload must be a JSON object.")
    if payload.get("version") != _BUNDLE_VERSION:
        raise ValueError(
            f"Unsupported dispatch bundle version: {payload.get('version')!r}"
        )
    for key in _REQUIRED_BUNDLE_IDS:
        if key == "dispatch_request_id":
            continue
        if key == "dispatch_generation":
            if payload.get(key) is None:
                raise ValueError("Dispatch bundle missing required field: dispatch_generation")
            continue
        if not str(payload.get(key) or "").strip():
            raise ValueError(f"Dispatch bundle missing required field: {key}")
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("Dispatch bundle snapshot must be a JSON object.")
    remint_pending = bool(snapshot.get("_remint_pending_prepare"))
    if not remint_pending and not str(payload.get("dispatch_request_id") or "").strip():
        raise ValueError("Dispatch bundle missing required field: dispatch_request_id")

    bundle = DispatchExecutionBundle(
        bundle_id=str(payload["bundle_id"]),
        ticket_id=str(payload["ticket_id"]),
        plan_id=str(payload["plan_id"]),
        dry_run_run_id=str(payload["dry_run_run_id"]),
        execute_request_id=str(payload["execute_request_id"]),
        gate_id=str(payload["gate_id"]),
        unlock_token_id=str(payload["unlock_token_id"]),
        dispatch_request_id=str(payload["dispatch_request_id"]),
        dispatch_generation=int(payload["dispatch_generation"]),
        requester_id=str(payload["requester_id"]),
        created_at=str(payload["created_at"]),
        updated_at=str(payload.get("updated_at") or payload["created_at"]),
        consumed_at=str(payload.get("consumed_at") or ""),
        snapshot=dict(snapshot),
    )
    _assert_bundle_alignment(bundle)
    return bundle


def _assert_bundle_alignment(bundle: DispatchExecutionBundle) -> None:
    snapshot = bundle.snapshot
    if snapshot.get("_remint_pending_prepare"):
        token_block = snapshot.get("unlock_token")
        if not isinstance(token_block, dict):
            raise ValueError("Dispatch bundle snapshot missing 'unlock_token'.")
        if str(token_block.get("token_id") or "") != bundle.unlock_token_id:
            raise ValueError(
                "Dispatch bundle unlock_token_id does not match snapshot token_id."
            )
        return

    checks = {
        "ticket": bundle.ticket_id,
        "plan": bundle.plan_id,
        "dry_run": bundle.dry_run_run_id,
        "execute_request": bundle.execute_request_id,
        "gate": bundle.gate_id,
        "unlock_token": bundle.unlock_token_id,
        "dispatch_request": bundle.dispatch_request_id,
    }
    for snapshot_key, expected_id in checks.items():
        block = snapshot.get(snapshot_key)
        if not isinstance(block, dict):
            raise ValueError(f"Dispatch bundle snapshot missing {snapshot_key!r}.")
        id_key = {
            "ticket": "ticket_id",
            "plan": "plan_id",
            "dry_run": "run_id",
            "execute_request": "execute_request_id",
            "gate": "gate_id",
            "unlock_token": "token_id",
            "dispatch_request": "dispatch_request_id",
        }[snapshot_key]
        actual_id = str(block.get(id_key) or "")
        if actual_id != expected_id:
            raise ValueError(
                f"Dispatch bundle ID mismatch for {snapshot_key}: "
                f"{actual_id!r} != {expected_id!r}"
            )

    token_block = snapshot["unlock_token"]
    request_block = snapshot["dispatch_request"]
    if str(request_block.get("unlock_token_id") or "") != str(token_block.get("token_id") or ""):
        raise ValueError(
            "Dispatch bundle unlock_token_id does not match dispatch_request unlock_token_id."
        )


_REQUIRED_CLI_SNAPSHOT_KEYS = (
    "ticket",
    "plan",
    "dry_run",
    "dry_run_request",
    "execute_request",
    "gate",
    "unlock_token",
    "dispatch_request",
)


def validate_bundle_for_cli_execution(bundle: DispatchExecutionBundle) -> None:
    """Fail-closed validation before CLI dispatch run."""
    if bundle.version != _BUNDLE_VERSION:
        raise ValueError(f"Unsupported dispatch bundle version: {bundle.version!r}")
    if not str(bundle.bundle_id or "").strip():
        raise ValueError("Dispatch bundle bundle_id is required.")
    if not str(bundle.ticket_id or "").strip():
        raise ValueError("Dispatch bundle ticket_id is required.")
    if bundle.consumed_at:
        raise ValueError(
            f"Dispatch bundle for ticket {bundle.ticket_id} has already been consumed."
        )
    snapshot = bundle.snapshot
    if not isinstance(snapshot, dict):
        raise ValueError("Dispatch bundle snapshot must be a JSON object.")
    if snapshot.get("_remint_pending_prepare"):
        raise ValueError(
            "Dispatch bundle is pending prepare after token remint; run is not allowed."
        )
    for key in _REQUIRED_CLI_SNAPSHOT_KEYS:
        if key not in snapshot or not isinstance(snapshot[key], dict):
            raise ValueError(f"Dispatch bundle snapshot missing required block: {key!r}")

    _assert_bundle_alignment(bundle)
    _assert_bundle_cross_references(bundle)
    _assert_bundle_execution_state(bundle)


def _assert_bundle_cross_references(bundle: DispatchExecutionBundle) -> None:
    snap = bundle.snapshot
    ticket = snap["ticket"]
    plan = snap["plan"]
    dry_run = snap["dry_run"]
    dry_run_request = snap["dry_run_request"]
    execute_request = snap["execute_request"]
    gate = snap["gate"]
    token = snap["unlock_token"]
    dispatch_request = snap["dispatch_request"]

    ticket_id = bundle.ticket_id
    refs = (
        (str(ticket.get("ticket_id") or ""), ticket_id, "ticket.ticket_id"),
        (str(plan.get("ticket_id") or ""), ticket_id, "plan.ticket_id"),
        (str(dry_run.get("ticket_id") or ""), ticket_id, "dry_run.ticket_id"),
        (str(dry_run_request.get("ticket_id") or ""), ticket_id, "dry_run_request.ticket_id"),
        (str(execute_request.get("ticket_id") or ""), ticket_id, "execute_request.ticket_id"),
        (str(gate.get("ticket_id") or ""), ticket_id, "gate.ticket_id"),
        (str(token.get("ticket_id") or ""), ticket_id, "unlock_token.ticket_id"),
        (str(dispatch_request.get("ticket_id") or ""), ticket_id, "dispatch_request.ticket_id"),
        (str(plan.get("plan_id") or ""), bundle.plan_id, "plan.plan_id"),
        (str(dry_run.get("run_id") or ""), bundle.dry_run_run_id, "dry_run.run_id"),
        (
            str(execute_request.get("execute_request_id") or ""),
            bundle.execute_request_id,
            "execute_request.execute_request_id",
        ),
        (str(gate.get("gate_id") or ""), bundle.gate_id, "gate.gate_id"),
        (str(gate.get("execute_request_id") or ""), bundle.execute_request_id, "gate.execute_request_id"),
        (str(token.get("token_id") or ""), bundle.unlock_token_id, "unlock_token.token_id"),
        (
            str(dispatch_request.get("dispatch_request_id") or ""),
            bundle.dispatch_request_id,
            "dispatch_request.dispatch_request_id",
        ),
        (
            str(dispatch_request.get("unlock_token_id") or ""),
            bundle.unlock_token_id,
            "dispatch_request.unlock_token_id",
        ),
        (
            str(dispatch_request.get("execute_request_id") or ""),
            bundle.execute_request_id,
            "dispatch_request.execute_request_id",
        ),
        (str(token.get("execute_request_id") or ""), bundle.execute_request_id, "unlock_token.execute_request_id"),
        (str(token.get("gate_id") or ""), bundle.gate_id, "unlock_token.gate_id"),
        (str(dry_run.get("request_id") or ""), str(dry_run_request.get("request_id") or ""), "dry_run.request_id"),
        (
            str(execute_request.get("dry_run_run_id") or ""),
            bundle.dry_run_run_id,
            "execute_request.dry_run_run_id",
        ),
        (str(gate.get("dry_run_run_id") or ""), bundle.dry_run_run_id, "gate.dry_run_run_id"),
    )
    for actual, expected, label in refs:
        if actual != expected:
            raise ValueError(
                f"Dispatch bundle cross-reference mismatch for {label}: "
                f"{actual!r} != {expected!r}"
            )

    token_generation = int(token.get("dispatch_generation") or 0)
    if token_generation != bundle.dispatch_generation:
        raise ValueError(
            "Dispatch bundle dispatch_generation does not match unlock_token dispatch_generation."
        )
    ticket_generation = int(ticket.get("dispatch_generation") or 0)
    if ticket_generation != bundle.dispatch_generation:
        raise ValueError(
            "Dispatch bundle dispatch_generation does not match ticket dispatch_generation."
        )


def _assert_bundle_execution_state(bundle: DispatchExecutionBundle) -> None:
    from agent.coo.execution_dispatch_runtime import (
        DispatchUnlockToken,
        is_dispatch_unlock_token_expired,
    )
    from agent.coo.execution_execute import ExecuteGateStatus
    from agent.coo.execution_runtime import ExecutionRunStatus
    from agent.coo.execution_ticket import ExecutionTicketStatus

    snap = bundle.snapshot
    gate = snap["gate"]
    token_payload = snap["unlock_token"]
    ticket = snap["ticket"]
    dry_run = snap["dry_run"]

    gate_status = str(gate.get("status") or "")
    if gate_status != ExecuteGateStatus.APPROVED.value:
        raise ValueError(
            f"Execute gate must be approved for CLI dispatch run, got {gate_status!r}"
        )

    if bool(token_payload.get("consumed")):
        raise ValueError("Dispatch unlock token has already been consumed.")
    if bool(token_payload.get("superseded")):
        raise ValueError("Dispatch unlock token has been superseded.")

    token = DispatchUnlockToken(
        token_id=str(token_payload["token_id"]),
        ticket_id=str(token_payload["ticket_id"]),
        plan_id=str(token_payload["plan_id"]),
        execute_request_id=str(token_payload["execute_request_id"]),
        gate_id=str(token_payload["gate_id"]),
        dry_run_run_id=str(token_payload["dry_run_run_id"]),
        target_skills=tuple(token_payload.get("target_skills") or ()),
        requested_by=str(token_payload.get("requested_by") or ""),
        approved_by=str(token_payload.get("approved_by") or ""),
        minted_at=str(token_payload.get("minted_at") or ""),
        expires_at=str(token_payload.get("expires_at") or ""),
        consumed=bool(token_payload.get("consumed")),
        dispatch_generation=int(token_payload.get("dispatch_generation") or 0),
        superseded=bool(token_payload.get("superseded")),
        superseded_by=str(token_payload.get("superseded_by") or ""),
        superseded_at=str(token_payload.get("superseded_at") or ""),
        invalid_reason=str(token_payload.get("invalid_reason") or ""),
    )
    if is_dispatch_unlock_token_expired(token):
        raise ValueError("Dispatch unlock token has expired.")

    ticket_status = str(ticket.get("status") or "")
    if ticket_status != ExecutionTicketStatus.DISPATCH_PENDING.value:
        raise ValueError(
            f"Ticket must be dispatch_pending for CLI dispatch run, got {ticket_status!r}"
        )
    if bool(ticket.get("execution_dispatched")):
        raise ValueError("Ticket execution_dispatched must be false before CLI run.")
    if bool(ticket.get("repository2_touched")):
        raise ValueError("Ticket repository2_touched must be false before CLI run.")

    dry_run_status = str(dry_run.get("status") or "")
    if dry_run_status != ExecutionRunStatus.COMPLETED.value:
        raise ValueError(
            f"Dry-run must be completed for CLI dispatch run, got {dry_run_status!r}"
        )
    if not bool(dry_run.get("dry_run")):
        raise ValueError("Dry-run record must have dry_run=true.")
    if bool(dry_run.get("repository2_touched")):
        raise ValueError("Dry-run repository2_touched must be false before CLI run.")


def write_bundle(
    bundle: DispatchExecutionBundle,
    bundle_dir: Optional[Path] = None,
) -> Path:
    """Persist a dispatch bundle under Hermes home."""
    if bundle.consumed_at:
        raise ValueError(
            f"Cannot write consumed dispatch bundle for ticket {bundle.ticket_id}."
        )
    base_dir = bundle_dir or default_bundle_dir()
    _validate_bundle_paths(bundle.ticket_id, base_dir)
    path = _bundle_path(bundle.ticket_id, base_dir)
    _atomic_write_json(path, bundle.to_dict())
    return path


def read_bundle(
    ticket_id: str,
    *,
    bundle_dir: Optional[Path] = None,
    reject_consumed: bool = True,
) -> DispatchExecutionBundle:
    """Load a dispatch bundle by ticket id."""
    base_dir = bundle_dir or default_bundle_dir()
    _validate_bundle_paths(ticket_id, base_dir)
    path = _bundle_path(ticket_id, base_dir)
    if not path.is_file():
        raise KeyError(f"Dispatch bundle not found for ticket: {ticket_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Dispatch bundle JSON is corrupted for ticket {ticket_id}."
        ) from exc
    bundle = _validate_bundle_payload(payload)
    if reject_consumed and bundle.consumed_at:
        raise ValueError(
            f"Dispatch bundle for ticket {ticket_id} has already been consumed."
        )
    return bundle


def get_by_unlock_token(
    unlock_token_id: str,
    *,
    bundle_dir: Optional[Path] = None,
    reject_consumed: bool = True,
) -> Optional[DispatchExecutionBundle]:
    """Find a bundle by unlock token id."""
    base_dir = bundle_dir or default_bundle_dir()
    if not base_dir.is_dir():
        return None
    for path in base_dir.glob("*.json"):
        ticket_id = path.stem
        try:
            bundle = read_bundle(
                ticket_id,
                bundle_dir=base_dir,
                reject_consumed=reject_consumed,
            )
        except (KeyError, ValueError):
            continue
        if bundle.unlock_token_id == unlock_token_id:
            return bundle
    return None


def mark_bundle_consumed(
    ticket_id: str,
    *,
    consumed_at: str | None = None,
    bundle_dir: Optional[Path] = None,
) -> DispatchExecutionBundle:
    """Mark a bundle consumed and persist the update."""
    bundle = read_bundle(ticket_id, bundle_dir=bundle_dir, reject_consumed=True)
    consumed = DispatchExecutionBundle(
        bundle_id=bundle.bundle_id,
        ticket_id=bundle.ticket_id,
        plan_id=bundle.plan_id,
        dry_run_run_id=bundle.dry_run_run_id,
        execute_request_id=bundle.execute_request_id,
        gate_id=bundle.gate_id,
        unlock_token_id=bundle.unlock_token_id,
        dispatch_request_id=bundle.dispatch_request_id,
        dispatch_generation=bundle.dispatch_generation,
        requester_id=bundle.requester_id,
        created_at=bundle.created_at,
        updated_at=_utc_now_iso(),
        consumed_at=consumed_at or _utc_now_iso(),
        snapshot=bundle.snapshot,
    )
    base_dir = bundle_dir or default_bundle_dir()
    _validate_bundle_paths(ticket_id, base_dir)
    path = _bundle_path(ticket_id, base_dir)
    _atomic_write_json(path, consumed.to_dict())
    return consumed


def upsert_bundle_preserving_identity(
    bundle: DispatchExecutionBundle,
    *,
    bundle_dir: Optional[Path] = None,
) -> Path:
    """Write bundle, preserving bundle_id/created_at when file already exists."""
    base_dir = bundle_dir or default_bundle_dir()
    try:
        existing = read_bundle(
            bundle.ticket_id,
            bundle_dir=base_dir,
            reject_consumed=False,
        )
    except (KeyError, ValueError):
        existing = None
    if existing is not None:
        if existing.consumed_at:
            raise ValueError(
                f"Cannot update consumed dispatch bundle for ticket {bundle.ticket_id}."
            )
        merged = DispatchExecutionBundle(
            bundle_id=existing.bundle_id,
            ticket_id=bundle.ticket_id,
            plan_id=bundle.plan_id,
            dry_run_run_id=bundle.dry_run_run_id,
            execute_request_id=bundle.execute_request_id,
            gate_id=bundle.gate_id,
            unlock_token_id=bundle.unlock_token_id,
            dispatch_request_id=bundle.dispatch_request_id,
            dispatch_generation=bundle.dispatch_generation,
            requester_id=bundle.requester_id,
            created_at=existing.created_at,
            updated_at=_utc_now_iso(),
            consumed_at=existing.consumed_at,
            snapshot=bundle.snapshot,
        )
        return write_bundle(merged, bundle_dir=base_dir)
    return write_bundle(bundle, bundle_dir=base_dir)


def upsert_bundle_after_remint(
    ticket_id: str,
    token,
    *,
    ticket,
    plan,
    dry_run,
    dry_run_request,
    execute_request,
    gate,
    bundle_dir: Optional[Path] = None,
) -> Path:
    """Update bundle token after remint; dispatch_request realigns on next prepare."""
    base_dir = bundle_dir or default_bundle_dir()
    snapshot = {
        "ticket": ticket.to_dict(),
        "plan": plan.to_dict(),
        "dry_run": dry_run.to_dict(),
        "dry_run_request": dry_run_request.to_dict(),
        "execute_request": execute_request.to_dict(),
        "gate": gate.to_dict(),
        "unlock_token": token.to_dict(),
        "_remint_pending_prepare": True,
    }
    try:
        existing = read_bundle(
            ticket_id,
            bundle_dir=base_dir,
            reject_consumed=False,
        )
        bundle = DispatchExecutionBundle(
            bundle_id=existing.bundle_id,
            ticket_id=ticket_id,
            plan_id=plan.plan_id,
            dry_run_run_id=dry_run.run_id,
            execute_request_id=execute_request.execute_request_id,
            gate_id=gate.gate_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=existing.dispatch_request_id,
            dispatch_generation=token.dispatch_generation,
            requester_id=ticket.requester_id,
            created_at=existing.created_at,
            updated_at=_utc_now_iso(),
            consumed_at=existing.consumed_at,
            snapshot=snapshot,
        )
    except (KeyError, ValueError):
        bundle = DispatchExecutionBundle(
            bundle_id=str(uuid.uuid4()),
            ticket_id=ticket_id,
            plan_id=plan.plan_id,
            dry_run_run_id=dry_run.run_id,
            execute_request_id=execute_request.execute_request_id,
            gate_id=gate.gate_id,
            unlock_token_id=token.token_id,
            dispatch_request_id="",
            dispatch_generation=token.dispatch_generation,
            requester_id=ticket.requester_id,
            created_at=_utc_now_iso(),
            updated_at=_utc_now_iso(),
            snapshot=snapshot,
        )
    if bundle.consumed_at:
        raise ValueError(
            f"Cannot update consumed dispatch bundle for ticket {ticket_id}."
        )
    base_dir = bundle_dir or default_bundle_dir()
    _validate_bundle_paths(ticket_id, base_dir)
    path = _bundle_path(ticket_id, base_dir)
    _atomic_write_json(path, bundle.to_dict())
    return path
