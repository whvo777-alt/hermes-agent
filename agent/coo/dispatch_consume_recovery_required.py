"""Known recovery-required consume inconsistency detection — Phase 12Q.

Read-only classification for partial forward-complete failures where the
confirmation artifact was consumed but the transaction record remains partial.
Unknown mismatches remain fail-closed exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent.coo.dispatch_consume_repair_audit import (
    CooDispatchConsumeRepairAuditRecord,
    find_latest_failed_partial_forward_complete_audit,
)
from agent.coo.dispatch_consume_transaction import (
    DispatchConsumeTransaction,
    DispatchConsumeTransactionError,
    _TRANSACTION_STATE_PARTIAL,
)

FAILURE_REASON_TRANSACTION_COMMIT_FAILED = "transaction_commit_failed"


@dataclass(frozen=True)
class KnownRecoveryRequiredContext:
    """Correlated recovery-required inconsistency summary."""

    repair_attempt_id: str
    repair_action: str
    outcome: str
    failure_reason_code: str


def _validate_audit_correlation(
    audit: CooDispatchConsumeRepairAuditRecord,
    *,
    ticket_id: str,
    confirmation_id: str,
    transaction: DispatchConsumeTransaction,
) -> None:
    if audit.ticket_id != ticket_id:
        raise DispatchConsumeTransactionError(
            "Failed repair audit ticket_id does not match consume pair."
        )
    if audit.confirmation_id != confirmation_id:
        raise DispatchConsumeTransactionError(
            "Failed repair audit confirmation_id does not match consume pair."
        )
    if audit.transaction_id != transaction.transaction_id:
        raise DispatchConsumeTransactionError(
            "Failed repair audit transaction_id does not match consume transaction."
        )
    if audit.execution_attempt_id != transaction.execution_attempt_id:
        raise DispatchConsumeTransactionError(
            "Failed repair audit execution_attempt_id does not match consume transaction."
        )
    if audit.repair_action != "repair_action_partial_forward_complete":
        raise DispatchConsumeTransactionError(
            "Failed repair audit repair_action does not match partial forward-complete."
        )
    if audit.outcome != "failed":
        raise DispatchConsumeTransactionError(
            "Failed repair audit outcome is not failed."
        )
    if audit.consume_state_after != "recovery_required":
        raise DispatchConsumeTransactionError(
            "Failed repair audit consume_state_after is not recovery_required."
        )


def try_resolve_known_recovery_required(
    *,
    ticket_id: str,
    confirmation_id: str,
    bundle_consumed: bool,
    confirmation_consumed: bool,
    transaction: DispatchConsumeTransaction,
    repair_audit_dir: Path | None = None,
) -> Optional[KnownRecoveryRequiredContext]:
    """Return correlated recovery context or None when the pattern does not apply."""
    if transaction.state != _TRANSACTION_STATE_PARTIAL:
        return None
    if not bundle_consumed or not confirmation_consumed:
        return None
    if transaction.confirmation_consumed:
        raise DispatchConsumeTransactionError(
            "Partial consume transaction conflicts with consumed confirmation artifact."
        )

    audit = find_latest_failed_partial_forward_complete_audit(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        execution_attempt_id=transaction.execution_attempt_id,
        transaction_id=transaction.transaction_id,
        audit_dir=repair_audit_dir,
    )
    if audit is None:
        raise DispatchConsumeTransactionError(
            "Consume artifact and transaction state are inconsistent; "
            "no correlated failed repair audit was found."
        )
    _validate_audit_correlation(
        audit,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        transaction=transaction,
    )
    return KnownRecoveryRequiredContext(
        repair_attempt_id=audit.repair_attempt_id,
        repair_action=audit.repair_action,
        outcome=audit.outcome,
        failure_reason_code=FAILURE_REASON_TRANSACTION_COMMIT_FAILED,
    )
