"""Read-only dispatch persistence status — Phase 10S.

Loads bundle and optional confirmation files under Hermes home and returns a
safe summary. No dispatch execution, no writes, no subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.coo.dispatch_bundle_store import read_bundle
from agent.coo.production_executor_confirmation import read_confirmation


@dataclass(frozen=True)
class CooDispatchStatusSummary:
    """Safe, read-only dispatch persistence summary for CLI output."""

    ticket_id: str
    bundle_id: str
    dispatch_request_id: str
    dispatch_generation: int
    bundle_consumed: bool
    remint_pending_prepare: bool
    gate_status: str
    ticket_status: str
    confirmation_id: str = ""
    confirmation_consumed: Optional[bool] = None
    confirmation_expired: Optional[bool] = None


def _confirmation_is_expired(expires_at: str) -> bool:
    expires = datetime.fromisoformat(expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires


def summarize_dispatch_persistence_status(
    *,
    ticket_id: str,
    confirmation_id: str | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> CooDispatchStatusSummary:
    """Load persisted bundle/confirmation files and build a safe status summary."""
    normalized_ticket_id = ticket_id.strip()
    if not normalized_ticket_id:
        raise ValueError("ticket_id is required")

    bundle = read_bundle(
        normalized_ticket_id,
        bundle_dir=bundle_dir,
        reject_consumed=False,
    )
    if bundle.ticket_id != normalized_ticket_id:
        raise ValueError("Dispatch bundle ticket_id does not match CLI input.")

    snapshot = bundle.snapshot
    gate = snapshot.get("gate") if isinstance(snapshot, dict) else {}
    ticket = snapshot.get("ticket") if isinstance(snapshot, dict) else {}
    if not isinstance(gate, dict) or not isinstance(ticket, dict):
        raise ValueError("Dispatch bundle snapshot is missing gate or ticket blocks.")

    confirmation_consumed: bool | None = None
    confirmation_expired: bool | None = None
    resolved_confirmation_id = ""

    if confirmation_id is not None:
        normalized_confirmation_id = confirmation_id.strip()
        if not normalized_confirmation_id:
            raise ValueError("confirmation_id must not be empty when provided.")
        confirmation = read_confirmation(
            normalized_confirmation_id,
            confirmation_dir=confirmation_dir,
            reject_consumed=False,
        )
        if confirmation.confirmation_id != normalized_confirmation_id:
            raise ValueError("Confirmation file confirmation_id does not match CLI input.")
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
        resolved_confirmation_id = confirmation.confirmation_id
        confirmation_consumed = bool(confirmation.consumed)
        confirmation_expired = _confirmation_is_expired(confirmation.expires_at)

    return CooDispatchStatusSummary(
        ticket_id=bundle.ticket_id,
        bundle_id=bundle.bundle_id,
        dispatch_request_id=bundle.dispatch_request_id,
        dispatch_generation=bundle.dispatch_generation,
        bundle_consumed=bool(bundle.consumed_at),
        remint_pending_prepare=bool(snapshot.get("_remint_pending_prepare")),
        gate_status=str(gate.get("status") or ""),
        ticket_status=str(ticket.get("status") or ""),
        confirmation_id=resolved_confirmation_id,
        confirmation_consumed=confirmation_consumed,
        confirmation_expired=confirmation_expired,
    )


def format_dispatch_status_summary(summary: CooDispatchStatusSummary) -> str:
    """Render a safe text summary without secrets or snapshot dumps."""
    lines = [
        f"ticket_id: {summary.ticket_id}",
        f"bundle_id: {summary.bundle_id}",
        f"dispatch_request_id: {summary.dispatch_request_id}",
        f"dispatch_generation: {summary.dispatch_generation}",
        f"bundle_consumed: {str(summary.bundle_consumed).lower()}",
        f"remint_pending_prepare: {str(summary.remint_pending_prepare).lower()}",
        f"gate_status: {summary.gate_status}",
        f"ticket_status: {summary.ticket_status}",
    ]
    if summary.confirmation_id:
        lines.extend(
            [
                f"confirmation_id: {summary.confirmation_id}",
                f"confirmation_consumed: {str(summary.confirmation_consumed).lower()}",
                f"confirmation_expired: {str(summary.confirmation_expired).lower()}",
            ]
        )
    return "\n".join(lines)
