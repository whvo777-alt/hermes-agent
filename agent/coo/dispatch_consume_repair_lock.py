"""Pair-scoped consume repair lock — Phase 12O.

Non-blocking exclusive flock per ticket + confirmation pair. No stale-lock
auto-release; concurrent repair/apply attempts fail closed.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from agent.coo.dispatch_consume_transaction import (
    DispatchConsumeTransactionError,
    _normalize_pair_id,
    _validate_transaction_paths,
)

_IS_WINDOWS = sys.platform == "win32"


class DispatchConsumeRepairLockError(DispatchConsumeTransactionError):
    """Raised when a consume repair lock cannot be acquired."""


@dataclass(frozen=True)
class CooDispatchConsumeRepairLockStatus:
    """Read-only consume repair lock diagnosis."""

    lock_present: bool
    lock_acquirable: bool
    repair_in_progress: bool
    stale_unknown: bool


def _try_acquire_lock_handle(handle) -> bool:
    if _IS_WINDOWS:
        import msvcrt

        locking = getattr(msvcrt, "locking")
        nb_lock = getattr(msvcrt, "LK_NBLCK")
        try:
            handle.seek(0)
            locking(handle.fileno(), nb_lock, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _release_lock_handle(handle, *, acquired: bool) -> None:
    if not acquired:
        return
    if _IS_WINDOWS:
        import msvcrt

        handle.seek(0)
        locking = getattr(msvcrt, "locking")
        unlock_mode = getattr(msvcrt, "LK_UNLCK")
        locking(handle.fileno(), unlock_mode, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def probe_consume_repair_pair_lock(
    ticket_id: str,
    confirmation_id: str,
    *,
    transaction_dir: Path,
) -> CooDispatchConsumeRepairLockStatus:
    """Non-blocking read-only lock diagnosis; probe acquires and releases immediately."""
    lock_path = _repair_lock_path(
        ticket_id,
        confirmation_id,
        transaction_dir=transaction_dir,
    )
    if not lock_path.exists():
        return CooDispatchConsumeRepairLockStatus(
            lock_present=False,
            lock_acquirable=True,
            repair_in_progress=False,
            stale_unknown=False,
        )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        acquired = _try_acquire_lock_handle(handle)
        if acquired:
            return CooDispatchConsumeRepairLockStatus(
                lock_present=True,
                lock_acquirable=True,
                repair_in_progress=False,
                stale_unknown=True,
            )
        return CooDispatchConsumeRepairLockStatus(
            lock_present=True,
            lock_acquirable=False,
            repair_in_progress=True,
            stale_unknown=False,
        )
    finally:
        try:
            _release_lock_handle(handle, acquired=acquired)
        finally:
            handle.close()


def _repair_lock_path(
    ticket_id: str,
    confirmation_id: str,
    *,
    transaction_dir: Path,
) -> Path:
    normalized_ticket_id = _normalize_pair_id(ticket_id, field_name="ticket_id")
    normalized_confirmation_id = _normalize_pair_id(
        confirmation_id,
        field_name="confirmation_id",
    )
    _, active_path = _validate_transaction_paths(
        normalized_ticket_id,
        normalized_confirmation_id,
        transaction_dir,
    )
    resolved_transaction_dir = transaction_dir.resolve()
    lock_dir = active_path.parent / ".locks"
    resolved_lock_dir = lock_dir.resolve()
    try:
        resolved_lock_dir.relative_to(resolved_transaction_dir)
    except ValueError as exc:
        raise DispatchConsumeRepairLockError(
            "Consume repair lock directory must remain under consume transaction directory."
        ) from exc
    return lock_dir / f"{normalized_ticket_id}__{normalized_confirmation_id}.lock"


@contextlib.contextmanager
def consume_repair_pair_lock(
    ticket_id: str,
    confirmation_id: str,
    *,
    transaction_dir: Path,
) -> Iterator[None]:
    """Acquire an exclusive non-blocking lock for a consume repair pair."""
    lock_path = _repair_lock_path(
        ticket_id,
        confirmation_id,
        transaction_dir=transaction_dir,
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        acquired = _try_acquire_lock_handle(handle)
        if not acquired:
            raise DispatchConsumeRepairLockError(
                "Consume repair lock is already held."
            )
        yield
    finally:
        try:
            _release_lock_handle(handle, acquired=acquired)
        finally:
            handle.close()
