"""CLI dispatch consume repair lock status — Phase 12Q."""

from __future__ import annotations

from pathlib import Path

from agent.coo.dispatch_consume_repair_lock import (
    CooDispatchConsumeRepairLockStatus,
    probe_consume_repair_pair_lock,
)


def summarize_consume_repair_lock_status(
    *,
    ticket_id: str,
    confirmation_id: str,
    transaction_dir: Path | None = None,
) -> CooDispatchConsumeRepairLockStatus:
    """Probe consume repair lock state without mutating persisted files."""
    if transaction_dir is None:
        from agent.coo.dispatch_consume_transaction import default_consume_transaction_dir

        transaction_dir = default_consume_transaction_dir()
    return probe_consume_repair_pair_lock(
        ticket_id,
        confirmation_id,
        transaction_dir=transaction_dir,
    )


def format_dispatch_consume_repair_lock_status(
    status: CooDispatchConsumeRepairLockStatus,
) -> str:
    """Format safe repair lock status fields for CLI stdout."""
    return "\n".join(
        (
            f"lock_present: {str(status.lock_present).lower()}",
            f"lock_acquirable: {str(status.lock_acquirable).lower()}",
            f"repair_in_progress: {str(status.repair_in_progress).lower()}",
            f"stale_unknown: {str(status.stale_unknown).lower()}",
        )
    )
