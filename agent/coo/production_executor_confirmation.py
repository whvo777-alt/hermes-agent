"""Production executor confirmation — Phase 10L operator attestation.

Record-only confirmation minting and validation. No dispatch execution.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from hermes_constants import get_hermes_home

from agent.coo.dispatch_pipeline_root_trust import validate_stored_attested_pipeline_root

if TYPE_CHECKING:
    from agent.coo.execution_dispatch_runtime import (
        DispatchExecutionRequest,
        DispatchUnlockToken,
    )
    from agent.coo.dispatch_bundle_store import DispatchExecutionBundle
    from agent.coo.execution_ticket import ExecutionTicket

REQUIRED_CONFIRMATION_PHRASE = "CONFIRM-REPOSITORY2-EXECUTION"
DEFAULT_CONFIRMATION_TTL_SECONDS = 300
_CONFIRMATION_FILE_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProductionExecutorConfirmation:
    """Single-use operator confirmation bound to a dispatch attempt."""

    confirmation_id: str
    ticket_id: str
    plan_id: str
    unlock_token_id: str
    dispatch_request_id: str
    operator_id: str
    operator_name: str
    confirmation_reason: str
    confirmation_phrase: str
    created_at: str
    expires_at: str
    attested_pipeline_root: str = ""
    consumed: bool = False
    consumed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "confirmation_id": self.confirmation_id,
            "ticket_id": self.ticket_id,
            "plan_id": self.plan_id,
            "unlock_token_id": self.unlock_token_id,
            "dispatch_request_id": self.dispatch_request_id,
            "operator_id": self.operator_id,
            "operator_name": self.operator_name,
            "confirmation_reason": self.confirmation_reason,
            "confirmation_phrase": self.confirmation_phrase,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "attested_pipeline_root": self.attested_pipeline_root,
            "consumed": self.consumed,
            "consumed_at": self.consumed_at,
        }


class ProductionExecutorConfirmationStore:
    """Process-local in-memory confirmation store."""

    def __init__(self) -> None:
        self._confirmations: Dict[str, ProductionExecutorConfirmation] = {}
        self._by_token: Dict[str, str] = {}

    def save(self, confirmation: ProductionExecutorConfirmation) -> None:
        self._confirmations[confirmation.confirmation_id] = confirmation
        if not confirmation.consumed:
            self._by_token[confirmation.unlock_token_id] = confirmation.confirmation_id

    def get(self, confirmation_id: str) -> Optional[ProductionExecutorConfirmation]:
        return self._confirmations.get(confirmation_id)

    def get_by_token(
        self,
        unlock_token_id: str,
    ) -> Optional[ProductionExecutorConfirmation]:
        confirmation_id = self._by_token.get(unlock_token_id)
        if confirmation_id is None:
            return None
        return self._confirmations.get(confirmation_id)

    def list_confirmations(self) -> List[ProductionExecutorConfirmation]:
        return list(self._confirmations.values())

    def clear(self) -> None:
        self._confirmations.clear()
        self._by_token.clear()

    def consume(self, confirmation_id: str) -> ProductionExecutorConfirmation:
        confirmation = self.get(confirmation_id)
        if confirmation is None:
            raise KeyError(f"Confirmation not found: {confirmation_id}")
        if confirmation.consumed:
            raise ValueError(
                f"Confirmation {confirmation_id} has already been consumed."
            )
        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(confirmation.expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now >= expires:
            raise ValueError(f"Confirmation {confirmation_id} has expired.")
        confirmation.consumed = True
        self._by_token.pop(confirmation.unlock_token_id, None)
        self.save(confirmation)
        return confirmation


_DEFAULT_CONFIRMATION_STORE: Optional[ProductionExecutorConfirmationStore] = None


def get_default_production_executor_confirmation_store() -> ProductionExecutorConfirmationStore:
    global _DEFAULT_CONFIRMATION_STORE
    if _DEFAULT_CONFIRMATION_STORE is None:
        _DEFAULT_CONFIRMATION_STORE = ProductionExecutorConfirmationStore()
    return _DEFAULT_CONFIRMATION_STORE


def default_confirmation_dir() -> Path:
    return get_hermes_home() / "coo" / "confirmations"


def _assert_confirmation_path_within_hermes_home(
    resolved: Path,
    hermes_root: Path,
    *,
    label: str,
) -> None:
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise ValueError(
            f"Confirmation {label} {resolved} must remain under Hermes home {hermes_root}"
        ) from exc


def _confirmation_path(confirmation_id: str, confirmation_dir: Path) -> Path:
    return confirmation_dir / f"{confirmation_id}.json"


def _validate_confirmation_paths(
    confirmation_id: str,
    confirmation_dir: Path,
) -> tuple[Path, Path]:
    hermes_root = get_hermes_home().resolve()
    resolved_base = confirmation_dir.resolve()
    path = _confirmation_path(confirmation_id, confirmation_dir)
    resolved_path = path.resolve()
    _assert_confirmation_path_within_hermes_home(
        resolved_base,
        hermes_root,
        label="directory",
    )
    _assert_confirmation_path_within_hermes_home(
        resolved_path,
        hermes_root,
        label="path",
    )
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


def confirmation_to_file_dict(confirmation: ProductionExecutorConfirmation) -> Dict[str, Any]:
    return {
        "version": _CONFIRMATION_FILE_VERSION,
        "confirmation_id": confirmation.confirmation_id,
        "ticket_id": confirmation.ticket_id,
        "plan_id": confirmation.plan_id,
        "unlock_token_id": confirmation.unlock_token_id,
        "dispatch_request_id": confirmation.dispatch_request_id,
        "operator_id": confirmation.operator_id,
        "operator_name": confirmation.operator_name,
        "confirmation_reason": confirmation.confirmation_reason,
        "phrase_verified": True,
        "attested_pipeline_root": confirmation.attested_pipeline_root,
        "created_at": confirmation.created_at,
        "expires_at": confirmation.expires_at,
        "consumed": confirmation.consumed,
        "consumed_at": confirmation.consumed_at,
    }


def confirmation_from_file_dict(payload: Dict[str, Any]) -> ProductionExecutorConfirmation:
    if not isinstance(payload, dict):
        raise ValueError("Confirmation payload must be a JSON object.")
    if payload.get("version") != _CONFIRMATION_FILE_VERSION:
        raise ValueError(
            f"Unsupported confirmation file version: {payload.get('version')!r}"
        )
    if "confirmation_phrase" in payload:
        raise ValueError("Confirmation file must not contain confirmation_phrase.")
    if not payload.get("phrase_verified"):
        raise ValueError("Confirmation file phrase_verified must be true.")
    attested_raw = payload.get("attested_pipeline_root")
    if attested_raw is None:
        raise ValueError("Confirmation file missing attested_pipeline_root.")
    attested_pipeline_root = validate_stored_attested_pipeline_root(str(attested_raw))
    confirmation_id = str(payload.get("confirmation_id") or "").strip()
    if not confirmation_id:
        raise ValueError("Confirmation file missing confirmation_id.")
    return ProductionExecutorConfirmation(
        confirmation_id=confirmation_id,
        ticket_id=str(payload["ticket_id"]),
        plan_id=str(payload["plan_id"]),
        unlock_token_id=str(payload["unlock_token_id"]),
        dispatch_request_id=str(payload["dispatch_request_id"]),
        operator_id=str(payload["operator_id"]),
        operator_name=str(payload["operator_name"]),
        confirmation_reason=str(payload["confirmation_reason"]),
        confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
        created_at=str(payload["created_at"]),
        expires_at=str(payload["expires_at"]),
        attested_pipeline_root=attested_pipeline_root,
        consumed=bool(payload.get("consumed")),
        consumed_at=str(payload.get("consumed_at") or ""),
    )


def write_confirmation(
    confirmation: ProductionExecutorConfirmation,
    confirmation_dir: Optional[Path] = None,
) -> Path:
    """Persist confirmation metadata under Hermes home without storing the phrase."""
    validate_stored_attested_pipeline_root(confirmation.attested_pipeline_root)
    base_dir = confirmation_dir or default_confirmation_dir()
    _validate_confirmation_paths(confirmation.confirmation_id, base_dir)
    path = _confirmation_path(confirmation.confirmation_id, base_dir)
    _atomic_write_json(path, confirmation_to_file_dict(confirmation))
    return path


def read_confirmation(
    confirmation_id: str,
    *,
    confirmation_dir: Optional[Path] = None,
    reject_consumed: bool = True,
) -> ProductionExecutorConfirmation:
    """Load a persisted confirmation by id."""
    base_dir = confirmation_dir or default_confirmation_dir()
    _validate_confirmation_paths(confirmation_id, base_dir)
    path = _confirmation_path(confirmation_id, base_dir)
    if not path.is_file():
        raise KeyError(f"Confirmation not found: {confirmation_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Confirmation JSON is corrupted for id {confirmation_id}."
        ) from exc
    confirmation = confirmation_from_file_dict(payload)
    if confirmation.confirmation_id != confirmation_id:
        raise ValueError("Confirmation file confirmation_id does not match path.")
    if reject_consumed and confirmation.consumed:
        raise ValueError(f"Confirmation {confirmation_id} has already been consumed.")
    return confirmation


def mark_confirmation_consumed_file(
    confirmation_id: str,
    *,
    consumed_at: str | None = None,
    confirmation_dir: Optional[Path] = None,
) -> ProductionExecutorConfirmation:
    """Mark a persisted confirmation consumed."""
    confirmation = read_confirmation(
        confirmation_id,
        confirmation_dir=confirmation_dir,
        reject_consumed=True,
    )
    consumed = ProductionExecutorConfirmation(
        confirmation_id=confirmation.confirmation_id,
        ticket_id=confirmation.ticket_id,
        plan_id=confirmation.plan_id,
        unlock_token_id=confirmation.unlock_token_id,
        dispatch_request_id=confirmation.dispatch_request_id,
        operator_id=confirmation.operator_id,
        operator_name=confirmation.operator_name,
        confirmation_reason=confirmation.confirmation_reason,
        confirmation_phrase=confirmation.confirmation_phrase,
        created_at=confirmation.created_at,
        expires_at=confirmation.expires_at,
        attested_pipeline_root=confirmation.attested_pipeline_root,
        consumed=True,
        consumed_at=consumed_at or _utc_now_iso(),
    )
    write_confirmation(consumed, confirmation_dir=confirmation_dir)
    return consumed


def create_production_executor_confirmation(
    *,
    ticket_id: str,
    plan_id: str,
    unlock_token_id: str,
    dispatch_request_id: str,
    operator_id: str,
    operator_name: str,
    confirmation_reason: str,
    confirmation_phrase: str,
    attested_pipeline_root: str = "",
    ttl_seconds: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
    confirmation_store: Optional[ProductionExecutorConfirmationStore] = None,
    persist_to_file: bool = False,
    confirmation_dir: Optional[Path] = None,
) -> ProductionExecutorConfirmation:
    """Mint a new production executor confirmation record."""
    if not operator_id.strip():
        raise ValueError("operator_id is required")
    if not operator_name.strip():
        raise ValueError("operator_name is required")
    if not confirmation_reason.strip():
        raise ValueError("confirmation_reason is required")
    if confirmation_phrase != REQUIRED_CONFIRMATION_PHRASE:
        raise ValueError(
            f"confirmation_phrase must equal {REQUIRED_CONFIRMATION_PHRASE!r}"
        )
    validated_attested = ""
    if attested_pipeline_root.strip() or persist_to_file:
        validated_attested = validate_stored_attested_pipeline_root(
            attested_pipeline_root
        )

    created_at = _utc_now_iso()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    ).isoformat()
    confirmation = ProductionExecutorConfirmation(
        confirmation_id=str(uuid.uuid4()),
        ticket_id=ticket_id,
        plan_id=plan_id,
        unlock_token_id=unlock_token_id,
        dispatch_request_id=dispatch_request_id,
        operator_id=operator_id.strip(),
        operator_name=operator_name.strip(),
        confirmation_reason=confirmation_reason.strip(),
        confirmation_phrase=confirmation_phrase,
        created_at=created_at,
        expires_at=expires_at,
        attested_pipeline_root=validated_attested,
    )
    store = confirmation_store or get_default_production_executor_confirmation_store()
    store.save(confirmation)
    if persist_to_file:
        write_confirmation(confirmation, confirmation_dir=confirmation_dir)
    return confirmation


def assert_confirmation_valid(
    confirmation: ProductionExecutorConfirmation,
    *,
    token: "DispatchUnlockToken",
    dispatch_request: "DispatchExecutionRequest",
    ticket: "ExecutionTicket",
) -> None:
    """Validate confirmation alignment, expiry, and consumption state."""
    if confirmation.consumed:
        raise ValueError(
            f"Confirmation {confirmation.confirmation_id} has already been consumed."
        )
    now = datetime.now(timezone.utc)
    expires = datetime.fromisoformat(confirmation.expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now >= expires:
        raise ValueError(f"Confirmation {confirmation.confirmation_id} has expired.")

    if confirmation.ticket_id != ticket.ticket_id:
        raise ValueError("Confirmation ticket_id does not match ticket.")
    if confirmation.plan_id != token.plan_id:
        raise ValueError("Confirmation plan_id does not match token plan.")
    if confirmation.unlock_token_id != token.token_id:
        raise ValueError("Confirmation unlock_token_id does not match token.")
    if confirmation.dispatch_request_id != dispatch_request.dispatch_request_id:
        raise ValueError(
            "Confirmation dispatch_request_id does not match dispatch request."
        )
    if confirmation.confirmation_phrase != REQUIRED_CONFIRMATION_PHRASE:
        raise ValueError("Confirmation phrase is invalid.")


def validate_confirmation_for_cli_execution(
    confirmation: ProductionExecutorConfirmation,
    *,
    bundle: "DispatchExecutionBundle",
    expected_confirmation_id: str,
) -> None:
    """Fail-closed validation for CLI dispatch run confirmation files."""
    if confirmation.confirmation_id != expected_confirmation_id:
        raise ValueError("Confirmation id does not match CLI input.")
    if confirmation.consumed:
        raise ValueError(
            f"Confirmation {confirmation.confirmation_id} has already been consumed."
        )
    if confirmation.ticket_id != bundle.ticket_id:
        raise ValueError("Confirmation ticket_id does not match bundle ticket_id.")
    if confirmation.dispatch_request_id != bundle.dispatch_request_id:
        raise ValueError(
            "Confirmation dispatch_request_id does not match bundle dispatch_request_id."
        )
    if confirmation.unlock_token_id != bundle.unlock_token_id:
        raise ValueError(
            "Confirmation unlock_token_id does not match bundle unlock_token_id."
        )
    if confirmation.plan_id != bundle.plan_id:
        raise ValueError("Confirmation plan_id does not match bundle plan_id.")

    now = datetime.now(timezone.utc)
    expires = datetime.fromisoformat(confirmation.expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now >= expires:
        raise ValueError(f"Confirmation {confirmation.confirmation_id} has expired.")
