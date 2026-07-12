"""Pair-scoped consume repair lock — Phase 12O.

Non-blocking exclusive flock per ticket + confirmation pair. No stale-lock
auto-release; concurrent repair/apply attempts fail closed.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Iterator

from agent.coo.dispatch_consume_transaction import (
    DispatchConsumeTransactionError,
    _normalize_pair_id,
    _validate_transaction_paths,
)
from hermes_constants import get_hermes_home

_IS_WINDOWS = sys.platform == "win32"


class DispatchConsumeRepairLockError(DispatchConsumeTransactionError):
    """Raised when a consume repair lock cannot be acquired."""


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
    lock_dir = active_path.parent / ".locks"
    hermes_root = get_hermes_home().resolve()
    resolved_lock_dir = lock_dir.resolve()
    try:
        resolved_lock_dir.relative_to(hermes_root)
    except ValueError as exc:
        raise DispatchConsumeRepairLockError(
            "Consume repair lock directory must remain under Hermes home."
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
        if _IS_WINDOWS:
            import msvcrt

            locking = getattr(msvcrt, "locking")
            nb_lock = getattr(msvcrt, "LK_NBLCK")
            try:
                handle.seek(0)
                locking(handle.fileno(), nb_lock, 1)
                acquired = True
            except OSError as exc:
                raise DispatchConsumeRepairLockError(
                    "Consume repair lock is already held."
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError) as exc:
                raise DispatchConsumeRepairLockError(
                    "Consume repair lock is already held."
                ) from exc
        yield
    finally:
        try:
            if acquired:
                if _IS_WINDOWS:
                    import msvcrt

                    handle.seek(0)
                    locking = getattr(msvcrt, "locking")
                    unlock_mode = getattr(msvcrt, "LK_UNLCK")
                    locking(handle.fileno(), unlock_mode, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
