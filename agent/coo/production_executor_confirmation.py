"""Production executor confirmation — Phase 10L operator attestation.

Record-only confirmation minting and validation. No dispatch execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.coo.execution_dispatch_runtime import (
        DispatchExecutionRequest,
        DispatchUnlockToken,
    )
    from agent.coo.execution_ticket import ExecutionTicket

REQUIRED_CONFIRMATION_PHRASE = "CONFIRM-REPOSITORY2-EXECUTION"
DEFAULT_CONFIRMATION_TTL_SECONDS = 300


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
    consumed: bool = False

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
            "consumed": self.consumed,
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
    ttl_seconds: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
    confirmation_store: Optional[ProductionExecutorConfirmationStore] = None,
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
    )
    store = confirmation_store or get_default_production_executor_confirmation_store()
    store.save(confirmation)
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
