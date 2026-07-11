"""Dispatch bundle + confirmation consume transactions — Phase 12K.

Near-atomic consume of bundle and confirmation artifacts keyed by
execution_attempt_id. Transaction records live under Hermes home only.
No automatic repair, replay after partial/committed, or Repository 2 execution.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from agent.coo.dispatch_bundle_store import (
    mark_bundle_consumed,
    read_bundle,
)
from agent.coo.production_executor_confirmation import (
    mark_confirmation_consumed_file,
    read_confirmation,
)
from hermes_constants import get_hermes_home

_TRANSACTION_VERSION = 1
_TRANSACTION_STATE_PREPARED = "prepared"
_TRANSACTION_STATE_COMMITTED = "committed"
_TRANSACTION_STATE_PARTIAL = "partial"

CONSUME_STATE_UNCONSUMED = "unconsumed"
CONSUME_STATE_PREPARED = "prepared"
CONSUME_STATE_COMMITTED = "committed"
CONSUME_STATE_PARTIAL = "partial"
CONSUME_STATE_LEGACY_COMMITTED = "legacy_committed"
CONSUME_STATE_LEGACY_PARTIAL = "legacy_partial"

_KNOWN_TRANSACTION_STATES = frozenset(
    {
        _TRANSACTION_STATE_PREPARED,
        _TRANSACTION_STATE_COMMITTED,
        _TRANSACTION_STATE_PARTIAL,
    }
)
_KNOWN_TRANSACTION_KEYS = frozenset(
    {
        "version",
        "transaction_id",
        "execution_attempt_id",
        "ticket_id",
        "confirmation_id",
        "state",
        "prepared_at",
        "committed_at",
        "partial_at",
        "bundle_consumed",
        "confirmation_consumed",
        "failure_reason",
    }
)


class DispatchConsumeTransactionError(ValueError):
    """Raised when consume transaction state cannot be read or applied safely."""


@dataclass(frozen=True)
class DispatchConsumeTransaction:
    """Persisted consume transaction record."""

    transaction_id: str
    execution_attempt_id: str
    ticket_id: str
    confirmation_id: str
    state: str
    prepared_at: str
    committed_at: str = ""
    partial_at: str = ""
    bundle_consumed: bool = False
    confirmation_consumed: bool = False
    failure_reason: str = ""


@dataclass(frozen=True)
class CooDispatchConsumeStatus:
    """Derived consume status for bundle + confirmation pair."""

    consume_state: str
    transaction_id: str
    execution_attempt_id: str
    bundle_consumed: bool
    confirmation_consumed: bool
    recovery_required: bool


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_consume_transaction_dir() -> Path:
    return get_hermes_home() / "coo" / "consume-transactions"


def _assert_path_within_hermes_home(
    resolved: Path,
    hermes_root: Path,
    *,
    label: str,
) -> None:
    try:
        resolved.relative_to(hermes_root)
    except ValueError as exc:
        raise DispatchConsumeTransactionError(
            f"Consume transaction {label} must remain under Hermes home."
        ) from exc


def _normalize_pair_id(value: str, *, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise DispatchConsumeTransactionError(f"{field_name} is required")
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise DispatchConsumeTransactionError(
            f"{field_name} must not contain path separators."
        )
    return normalized


def _transaction_filename(ticket_id: str, confirmation_id: str) -> str:
    return f"{ticket_id}__{confirmation_id}.json"


def _validate_transaction_paths(
    ticket_id: str,
    confirmation_id: str,
    transaction_dir: Path,
) -> tuple[Path, Path]:
    hermes_root = get_hermes_home().resolve()
    resolved_base = transaction_dir.resolve()
    path = transaction_dir / _transaction_filename(ticket_id, confirmation_id)
    resolved_path = path.resolve()
    _assert_path_within_hermes_home(resolved_base, hermes_root, label="directory")
    _assert_path_within_hermes_home(resolved_path, hermes_root, label="path")
    if path.is_symlink():
        raise DispatchConsumeTransactionError(
            "Consume transaction path must not be a symlink."
        )
    return resolved_base, resolved_path


def _atomic_write_transaction(path: Path, payload: Mapping[str, Any]) -> None:
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


def _transaction_to_dict(transaction: DispatchConsumeTransaction) -> Dict[str, Any]:
    return {
        "version": _TRANSACTION_VERSION,
        "transaction_id": transaction.transaction_id,
        "execution_attempt_id": transaction.execution_attempt_id,
        "ticket_id": transaction.ticket_id,
        "confirmation_id": transaction.confirmation_id,
        "state": transaction.state,
        "prepared_at": transaction.prepared_at,
        "committed_at": transaction.committed_at,
        "partial_at": transaction.partial_at,
        "bundle_consumed": transaction.bundle_consumed,
        "confirmation_consumed": transaction.confirmation_consumed,
        "failure_reason": transaction.failure_reason,
    }


def _parse_transaction_payload(payload: Mapping[str, Any]) -> DispatchConsumeTransaction:
    if not isinstance(payload, Mapping):
        raise DispatchConsumeTransactionError(
            "Consume transaction record must be a mapping."
        )
    unknown_keys = set(payload) - _KNOWN_TRANSACTION_KEYS - {"version"}
    if unknown_keys:
        raise DispatchConsumeTransactionError(
            "Consume transaction record contains unknown fields."
        )
    version = payload.get("version")
    if version != _TRANSACTION_VERSION:
        raise DispatchConsumeTransactionError(
            "Consume transaction record version is invalid."
        )
    state = payload.get("state")
    if not isinstance(state, str) or state.strip().lower() not in _KNOWN_TRANSACTION_STATES:
        raise DispatchConsumeTransactionError(
            "Consume transaction record state is invalid."
        )
    transaction_id = payload.get("transaction_id")
    execution_attempt_id = payload.get("execution_attempt_id")
    ticket_id = payload.get("ticket_id")
    confirmation_id = payload.get("confirmation_id")
    prepared_at = payload.get("prepared_at")
    for field_name, value in (
        ("transaction_id", transaction_id),
        ("execution_attempt_id", execution_attempt_id),
        ("ticket_id", ticket_id),
        ("confirmation_id", confirmation_id),
        ("prepared_at", prepared_at),
    ):
        if not isinstance(value, str) or not value.strip():
            raise DispatchConsumeTransactionError(
                f"Consume transaction field {field_name} is required."
            )
    bundle_consumed = payload.get("bundle_consumed")
    confirmation_consumed = payload.get("confirmation_consumed")
    if not isinstance(bundle_consumed, bool) or not isinstance(confirmation_consumed, bool):
        raise DispatchConsumeTransactionError(
            "Consume transaction consumed flags must be booleans."
        )
    normalized_state = state.strip().lower()
    if normalized_state == _TRANSACTION_STATE_COMMITTED:
        if not bundle_consumed or not confirmation_consumed:
            raise DispatchConsumeTransactionError(
                "Committed consume transaction must mark both artifacts consumed."
            )
    if normalized_state == _TRANSACTION_STATE_PARTIAL:
        if not bundle_consumed or confirmation_consumed:
            raise DispatchConsumeTransactionError(
                "Partial consume transaction must have bundle consumed only."
            )
    failure_reason = payload.get("failure_reason", "")
    if failure_reason != "" and not isinstance(failure_reason, str):
        raise DispatchConsumeTransactionError(
            "Consume transaction failure_reason must be a string."
        )
    committed_at = payload.get("committed_at", "")
    partial_at = payload.get("partial_at", "")
    if committed_at != "" and not isinstance(committed_at, str):
        raise DispatchConsumeTransactionError(
            "Consume transaction committed_at must be a string."
        )
    if partial_at != "" and not isinstance(partial_at, str):
        raise DispatchConsumeTransactionError(
            "Consume transaction partial_at must be a string."
        )
    return DispatchConsumeTransaction(
        transaction_id=transaction_id.strip(),
        execution_attempt_id=execution_attempt_id.strip(),
        ticket_id=ticket_id.strip(),
        confirmation_id=confirmation_id.strip(),
        state=normalized_state,
        prepared_at=prepared_at.strip(),
        committed_at=str(committed_at).strip(),
        partial_at=str(partial_at).strip(),
        bundle_consumed=bundle_consumed,
        confirmation_consumed=confirmation_consumed,
        failure_reason=str(failure_reason).strip(),
    )


def read_consume_transaction(
    ticket_id: str,
    confirmation_id: str,
    *,
    transaction_dir: Path | None = None,
) -> Optional[DispatchConsumeTransaction]:
    """Load consume transaction for a ticket + confirmation pair, if present."""
    normalized_ticket_id = _normalize_pair_id(ticket_id, field_name="ticket_id")
    normalized_confirmation_id = _normalize_pair_id(
        confirmation_id,
        field_name="confirmation_id",
    )
    base_dir = transaction_dir or default_consume_transaction_dir()
    _, path = _validate_transaction_paths(
        normalized_ticket_id,
        normalized_confirmation_id,
        base_dir,
    )
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DispatchConsumeTransactionError(
            "Consume transaction record is corrupted."
        ) from exc
    transaction = _parse_transaction_payload(payload)
    if (
        transaction.ticket_id != normalized_ticket_id
        or transaction.confirmation_id != normalized_confirmation_id
    ):
        raise DispatchConsumeTransactionError(
            "Consume transaction record ids do not match lookup keys."
        )
    return transaction


def _artifact_consumed_flags(
    *,
    ticket_id: str,
    confirmation_id: str,
    bundle_dir: Path | None,
    confirmation_dir: Path | None,
) -> tuple[bool, bool]:
    bundle = read_bundle(
        ticket_id,
        bundle_dir=bundle_dir,
        reject_consumed=False,
    )
    confirmation = read_confirmation(
        confirmation_id,
        confirmation_dir=confirmation_dir,
        reject_consumed=False,
    )
    return bool(bundle.consumed_at), bool(confirmation.consumed)


def _derive_consume_state(
    *,
    bundle_consumed: bool,
    confirmation_consumed: bool,
    transaction: Optional[DispatchConsumeTransaction],
) -> str:
    if transaction is not None:
        if transaction.state == _TRANSACTION_STATE_COMMITTED:
            if not bundle_consumed or not confirmation_consumed:
                raise DispatchConsumeTransactionError(
                    "Committed consume transaction does not match artifact state."
                )
            return CONSUME_STATE_COMMITTED
        if transaction.state == _TRANSACTION_STATE_PARTIAL:
            if not bundle_consumed or confirmation_consumed:
                raise DispatchConsumeTransactionError(
                    "Partial consume transaction does not match artifact state."
                )
            return CONSUME_STATE_PARTIAL
        if transaction.state == _TRANSACTION_STATE_PREPARED:
            if bundle_consumed and not confirmation_consumed:
                return CONSUME_STATE_PARTIAL
            if not bundle_consumed and not confirmation_consumed:
                return CONSUME_STATE_PREPARED
            if bundle_consumed and confirmation_consumed:
                raise DispatchConsumeTransactionError(
                    "Prepared consume transaction conflicts with fully consumed artifacts."
                )

    if bundle_consumed and confirmation_consumed:
        return (
            CONSUME_STATE_LEGACY_COMMITTED
            if transaction is None
            else CONSUME_STATE_COMMITTED
        )
    if bundle_consumed != confirmation_consumed:
        return (
            CONSUME_STATE_LEGACY_PARTIAL
            if transaction is None
            else CONSUME_STATE_PARTIAL
        )
    return CONSUME_STATE_UNCONSUMED


def assess_consume_status(
    *,
    ticket_id: str,
    confirmation_id: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
) -> CooDispatchConsumeStatus:
    """Derive read-only consume status for bundle + confirmation pair."""
    normalized_ticket_id = _normalize_pair_id(ticket_id, field_name="ticket_id")
    normalized_confirmation_id = _normalize_pair_id(
        confirmation_id,
        field_name="confirmation_id",
    )
    bundle_consumed, confirmation_consumed = _artifact_consumed_flags(
        ticket_id=normalized_ticket_id,
        confirmation_id=normalized_confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
    )
    transaction = read_consume_transaction(
        normalized_ticket_id,
        normalized_confirmation_id,
        transaction_dir=transaction_dir,
    )
    consume_state = _derive_consume_state(
        bundle_consumed=bundle_consumed,
        confirmation_consumed=confirmation_consumed,
        transaction=transaction,
    )
    recovery_required = consume_state in {
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_LEGACY_PARTIAL,
    }
    return CooDispatchConsumeStatus(
        consume_state=consume_state,
        transaction_id=transaction.transaction_id if transaction else "",
        execution_attempt_id=transaction.execution_attempt_id if transaction else "",
        bundle_consumed=bundle_consumed,
        confirmation_consumed=confirmation_consumed,
        recovery_required=recovery_required,
    )


def assert_consume_replay_allowed(
    *,
    ticket_id: str,
    confirmation_id: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
) -> CooDispatchConsumeStatus:
    """Fail-closed when replay must not proceed for consume state."""
    status = assess_consume_status(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
    )
    if status.consume_state == CONSUME_STATE_UNCONSUMED:
        return status
    if status.consume_state in {
        CONSUME_STATE_COMMITTED,
        CONSUME_STATE_LEGACY_COMMITTED,
    }:
        raise ValueError(
            "Dispatch bundle and confirmation have already been consumed; replay is not permitted."
        )
    if status.consume_state in {
        CONSUME_STATE_PARTIAL,
        CONSUME_STATE_LEGACY_PARTIAL,
    }:
        raise ValueError(
            "Dispatch consume is in a partial state; manual recovery is required before replay."
        )
    if status.consume_state == CONSUME_STATE_PREPARED:
        raise ValueError(
            "Dispatch consume transaction is prepared but not committed; replay is not permitted."
        )
    raise ValueError(f"Dispatch consume state {status.consume_state!r} blocks replay.")


def _write_transaction_record(
    *,
    ticket_id: str,
    confirmation_id: str,
    transaction: DispatchConsumeTransaction,
    transaction_dir: Path,
) -> None:
    _, path = _validate_transaction_paths(ticket_id, confirmation_id, transaction_dir)
    _atomic_write_transaction(path, _transaction_to_dict(transaction))


def execute_consume_transaction(
    *,
    ticket_id: str,
    confirmation_id: str,
    execution_attempt_id: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
) -> DispatchConsumeTransaction:
    """Consume bundle then confirmation under a single transaction record."""
    normalized_ticket_id = _normalize_pair_id(ticket_id, field_name="ticket_id")
    normalized_confirmation_id = _normalize_pair_id(
        confirmation_id,
        field_name="confirmation_id",
    )
    normalized_attempt_id = _normalize_pair_id(
        execution_attempt_id,
        field_name="execution_attempt_id",
    )
    resolved_transaction_dir = transaction_dir or default_consume_transaction_dir()

    status = assert_consume_replay_allowed(
        ticket_id=normalized_ticket_id,
        confirmation_id=normalized_confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=resolved_transaction_dir,
    )
    if status.consume_state != CONSUME_STATE_UNCONSUMED:
        raise ValueError(
            f"Dispatch consume preflight failed with state {status.consume_state!r}."
        )

    read_bundle(
        normalized_ticket_id,
        bundle_dir=bundle_dir,
        reject_consumed=True,
    )
    read_confirmation(
        normalized_confirmation_id,
        confirmation_dir=confirmation_dir,
        reject_consumed=True,
    )

    transaction_id = str(uuid.uuid4())
    prepared_at = _utc_now_iso()
    prepared = DispatchConsumeTransaction(
        transaction_id=transaction_id,
        execution_attempt_id=normalized_attempt_id,
        ticket_id=normalized_ticket_id,
        confirmation_id=normalized_confirmation_id,
        state=_TRANSACTION_STATE_PREPARED,
        prepared_at=prepared_at,
    )
    _write_transaction_record(
        ticket_id=normalized_ticket_id,
        confirmation_id=normalized_confirmation_id,
        transaction=prepared,
        transaction_dir=resolved_transaction_dir,
    )

    try:
        mark_bundle_consumed(normalized_ticket_id, bundle_dir=bundle_dir)
    except (ValueError, OSError, KeyError) as exc:
        raise ValueError(
            "Dispatch run completed but bundle consume failed; no artifacts were consumed."
        ) from exc

    try:
        mark_confirmation_consumed_file(
            normalized_confirmation_id,
            confirmation_dir=confirmation_dir,
        )
    except (ValueError, OSError, KeyError) as exc:
        partial = DispatchConsumeTransaction(
            transaction_id=transaction_id,
            execution_attempt_id=normalized_attempt_id,
            ticket_id=normalized_ticket_id,
            confirmation_id=normalized_confirmation_id,
            state=_TRANSACTION_STATE_PARTIAL,
            prepared_at=prepared_at,
            partial_at=_utc_now_iso(),
            bundle_consumed=True,
            confirmation_consumed=False,
            failure_reason=str(exc),
        )
        _write_transaction_record(
            ticket_id=normalized_ticket_id,
            confirmation_id=normalized_confirmation_id,
            transaction=partial,
            transaction_dir=resolved_transaction_dir,
        )
        raise ValueError(
            "Dispatch run completed but confirmation consume failed; "
            "bundle is consumed and manual recovery is required."
        ) from exc

    committed = DispatchConsumeTransaction(
        transaction_id=transaction_id,
        execution_attempt_id=normalized_attempt_id,
        ticket_id=normalized_ticket_id,
        confirmation_id=normalized_confirmation_id,
        state=_TRANSACTION_STATE_COMMITTED,
        prepared_at=prepared_at,
        committed_at=_utc_now_iso(),
        bundle_consumed=True,
        confirmation_consumed=True,
    )
    _write_transaction_record(
        ticket_id=normalized_ticket_id,
        confirmation_id=normalized_confirmation_id,
        transaction=committed,
        transaction_dir=resolved_transaction_dir,
    )
    return committed
