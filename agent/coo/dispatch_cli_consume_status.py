"""CLI dispatch consume status read — Phase 12K.

Read-only diagnosis of bundle + confirmation consume state.
No writes, subprocess, repair, or path/secret disclosure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.coo.dispatch_consume_transaction import (
    CooDispatchConsumeStatus,
    DispatchConsumeTransactionError,
    assess_consume_status,
)


@dataclass(frozen=True)
class CooDispatchConsumeStatusSummary:
    """Safe read-only consume status summary."""

    consume_state: str
    transaction_id: str
    execution_attempt_id: str
    bundle_consumed: bool
    confirmation_consumed: bool
    recovery_required: bool


def summarize_dispatch_consume_status(
    *,
    ticket_id: str,
    confirmation_id: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
) -> CooDispatchConsumeStatusSummary:
    """Build safe consume status summary for CLI output."""
    try:
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            transaction_dir=transaction_dir,
        )
    except (DispatchConsumeTransactionError, KeyError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    return _to_summary(status)


def _to_summary(status: CooDispatchConsumeStatus) -> CooDispatchConsumeStatusSummary:
    return CooDispatchConsumeStatusSummary(
        consume_state=status.consume_state,
        transaction_id=status.transaction_id,
        execution_attempt_id=status.execution_attempt_id,
        bundle_consumed=status.bundle_consumed,
        confirmation_consumed=status.confirmation_consumed,
        recovery_required=status.recovery_required,
    )


def format_dispatch_consume_status_summary(
    summary: CooDispatchConsumeStatusSummary,
) -> str:
    """Format safe consume status fields for CLI stdout."""
    lines = (
        f"consume_state: {summary.consume_state}",
        f"transaction_id: {summary.transaction_id or '(none)'}",
        f"execution_attempt_id: {summary.execution_attempt_id or '(none)'}",
        f"bundle_consumed: {str(summary.bundle_consumed).lower()}",
        f"confirmation_consumed: {str(summary.confirmation_consumed).lower()}",
        f"recovery_required: {str(summary.recovery_required).lower()}",
    )
    return "\n".join(lines)
