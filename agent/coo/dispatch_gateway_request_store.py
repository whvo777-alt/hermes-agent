"""Gateway request idempotency store — Phase 13I.

Persists mock gateway dispatch request records under Hermes home.
No pipeline roots, tokens, argv, cwd, env, stdout, stderr, or secrets.
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

GATEWAY_REQUEST_STORE_VERSION = 1

REQUEST_STATUS_PREPARED = "prepared"
REQUEST_STATUS_COMPLETED = "completed"
REQUEST_STATUS_FAILED = "failed"
REQUEST_STATUS_BLOCKED = "blocked"

_KNOWN_REQUEST_STATUSES = frozenset(
    {
        REQUEST_STATUS_PREPARED,
        REQUEST_STATUS_COMPLETED,
        REQUEST_STATUS_FAILED,
        REQUEST_STATUS_BLOCKED,
    }
)
_KNOWN_RECORD_KEYS = frozenset(
    {
        "version",
        "gateway_request_id",
        "ticket_id",
        "confirmation_id",
        "execution_attempt_id",
        "dispatch_run_id",
        "status",
        "dry_run",
        "failure_reason_code",
        "production_execution_allowed",
        "gateway_state",
        "updated_at",
    }
)
_FORBIDDEN_RECORD_KEYS = frozenset(
    {
        "pipeline_root",
        "unlock_token",
        "unlock_token_id",
        "cwd",
        "argv",
        "env",
        "token",
        "phrase",
        "stdout",
        "stderr",
        "secret",
        "snapshot",
        "requester_metadata",
    }
)


class DispatchGatewayRequestStoreError(ValueError):
    """Raised when gateway request persistence is invalid or unsafe."""


@dataclass(frozen=True)
class CooDispatchGatewayRequestRecord:
    """Safe persisted gateway request record."""

    gateway_request_id: str
    ticket_id: str
    confirmation_id: str
    execution_attempt_id: str
    dispatch_run_id: str
    status: str
    dry_run: bool
    failure_reason_code: str
    production_execution_allowed: bool
    gateway_state: str
    version: int = GATEWAY_REQUEST_STORE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "gateway_request_id": self.gateway_request_id,
            "ticket_id": self.ticket_id,
            "confirmation_id": self.confirmation_id,
            "execution_attempt_id": self.execution_attempt_id,
            "dispatch_run_id": self.dispatch_run_id,
            "status": self.status,
            "dry_run": self.dry_run,
            "failure_reason_code": self.failure_reason_code,
            "production_execution_allowed": False,
            "gateway_state": self.gateway_state,
            "updated_at": _utc_now_iso(),
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_gateway_request_dir() -> Path:
    return get_hermes_home() / "coo" / "gateway-requests"


def _assert_within_hermes_home(resolved: Path, *, label: str) -> None:
    hermes_root = get_hermes_home().resolve()
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise DispatchGatewayRequestStoreError(
            f"Gateway request {label} must remain under Hermes home."
        ) from exc


def normalize_gateway_request_id(gateway_request_id: str) -> str:
    normalized = (gateway_request_id or "").strip()
    if not normalized:
        raise DispatchGatewayRequestStoreError("gateway_request_id is required")
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise DispatchGatewayRequestStoreError(
            "gateway_request_id must not contain path separators."
        )
    return normalized


def _request_path(
    gateway_request_id: str,
    *,
    request_dir: Path | None = None,
) -> Path:
    base_dir = request_dir or default_gateway_request_dir()
    resolved_base = base_dir.resolve()
    _assert_within_hermes_home(resolved_base, label="directory")
    path = (resolved_base / f"{gateway_request_id}.json").resolve()
    _assert_within_hermes_home(path, label="path")
    if path.is_symlink():
        raise DispatchGatewayRequestStoreError(
            "Gateway request path must not be a symlink."
        )
    return path


def _validate_safe_payload(payload: Mapping[str, Any]) -> None:
    unknown = set(payload) - _KNOWN_RECORD_KEYS
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise DispatchGatewayRequestStoreError(
            f"Unknown gateway request record keys: {joined}"
        )
    for key in payload:
        if key in _FORBIDDEN_RECORD_KEYS:
            raise DispatchGatewayRequestStoreError(
                f"Gateway request record must not include {key!r}."
            )
    status = payload.get("status")
    if status not in _KNOWN_REQUEST_STATUSES:
        raise DispatchGatewayRequestStoreError(
            "Gateway request record status is invalid."
        )


def _record_from_payload(payload: Mapping[str, Any]) -> CooDispatchGatewayRequestRecord:
    _validate_safe_payload(payload)
    return CooDispatchGatewayRequestRecord(
        gateway_request_id=str(payload["gateway_request_id"]),
        ticket_id=str(payload["ticket_id"]),
        confirmation_id=str(payload["confirmation_id"]),
        execution_attempt_id=str(payload.get("execution_attempt_id", "")),
        dispatch_run_id=str(payload.get("dispatch_run_id", "")),
        status=str(payload["status"]),
        dry_run=bool(payload.get("dry_run", False)),
        failure_reason_code=str(payload.get("failure_reason_code", "")),
        production_execution_allowed=False,
        gateway_state=str(payload.get("gateway_state", "")),
        version=int(payload.get("version", GATEWAY_REQUEST_STORE_VERSION)),
    )


def read_gateway_request(
    gateway_request_id: str,
    *,
    request_dir: Path | None = None,
) -> CooDispatchGatewayRequestRecord | None:
    """Read one gateway request record, or None when missing."""
    normalized_id = normalize_gateway_request_id(gateway_request_id)
    path = _request_path(normalized_id, request_dir=request_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DispatchGatewayRequestStoreError(
            f"Gateway request record is corrupted: {normalized_id}"
        ) from exc
    if not isinstance(payload, dict):
        raise DispatchGatewayRequestStoreError(
            f"Gateway request record is corrupted: {normalized_id}"
        )
    return _record_from_payload(payload)


def reserve_gateway_request(
    record: CooDispatchGatewayRequestRecord,
    *,
    request_dir: Path | None = None,
) -> CooDispatchGatewayRequestRecord:
    """Atomically create a prepared gateway request record."""
    normalized_id = normalize_gateway_request_id(record.gateway_request_id)
    path = _request_path(normalized_id, request_dir=request_dir)
    if path.exists():
        existing = read_gateway_request(normalized_id, request_dir=request_dir)
        if existing is None:
            raise DispatchGatewayRequestStoreError(
                "Gateway request record exists but cannot be read."
            )
        raise DispatchGatewayRequestStoreError(
            f"Gateway request already exists with status: {existing.status}"
        )
    payload = record.to_dict()
    payload["status"] = REQUEST_STATUS_PREPARED
    _validate_safe_payload(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _assert_within_hermes_home(tmp_path.resolve(), label="temp file")
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    loaded = read_gateway_request(normalized_id, request_dir=request_dir)
    if loaded is None:
        raise DispatchGatewayRequestStoreError(
            "Gateway request record write failed to persist."
        )
    return loaded


def transition_gateway_request(
    gateway_request_id: str,
    *,
    status: str,
    execution_attempt_id: str = "",
    dispatch_run_id: str = "",
    failure_reason_code: str = "",
    request_dir: Path | None = None,
) -> CooDispatchGatewayRequestRecord:
    """Transition an existing gateway request from prepared to a terminal state."""
    if status not in _KNOWN_REQUEST_STATUSES:
        raise DispatchGatewayRequestStoreError("Invalid gateway request transition.")
    normalized_id = normalize_gateway_request_id(gateway_request_id)
    existing = read_gateway_request(normalized_id, request_dir=request_dir)
    if existing is None:
        raise DispatchGatewayRequestStoreError("Gateway request record not found.")
    if existing.status != REQUEST_STATUS_PREPARED:
        raise DispatchGatewayRequestStoreError(
            f"Gateway request transition not allowed from status: {existing.status}"
        )
    updated = CooDispatchGatewayRequestRecord(
        gateway_request_id=existing.gateway_request_id,
        ticket_id=existing.ticket_id,
        confirmation_id=existing.confirmation_id,
        execution_attempt_id=execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        status=status,
        dry_run=existing.dry_run,
        failure_reason_code=failure_reason_code,
        production_execution_allowed=False,
        gateway_state=existing.gateway_state,
    )
    path = _request_path(normalized_id, request_dir=request_dir)
    payload = updated.to_dict()
    _validate_safe_payload(payload)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _assert_within_hermes_home(tmp_path.resolve(), label="temp file")
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    loaded = read_gateway_request(normalized_id, request_dir=request_dir)
    if loaded is None or loaded.status != status:
        raise DispatchGatewayRequestStoreError(
            "Gateway request transition failed to persist."
        )
    return loaded
