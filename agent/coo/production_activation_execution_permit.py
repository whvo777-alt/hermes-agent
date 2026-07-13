"""Production activation ephemeral execution permit — Phase 14H-3B.

Function-scoped permit context manager without config persistence or subprocess.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_RESERVED,
    ProductionActivationExecutionReservation,
    load_execution_reservation,
)
from agent.coo.production_activation_execution_gate import (
    evaluate_production_execution_gate,
    find_ready_execution_gate_record,
)
from agent.coo.production_activation_state import (
    ACTIVATION_STATE_ACTIVE,
    ActivationRequest,
)
from agent.coo.production_activation_store import load_activation_request

_PERMIT_SCOPE = "activation_one_shot_live_pilot"
_thread_state = threading.local()


class ActivationExecutionPermitError(ValueError):
    """Raised when an ephemeral execution permit cannot be granted."""


@dataclass
class ActivationExecutionPermit:
    """Ephemeral in-process execution permit for a single live pilot attempt."""

    activation_request_id: str
    reservation_id: str
    execution_attempt_id: str
    granted: bool = False
    entered_at: str = ""
    expires_at: str = ""
    scope: str = _PERMIT_SCOPE
    consumed: bool = False
    _store_dir: Path | None = None
    _reservation_dir: Path | None = None
    _gate_history_dir: Path | None = None
    _dry_run_history_dir: Path | None = None
    _bundle_dir: Path | None = None
    _confirmation_dir: Path | None = None
    _merged_config: Mapping[str, Any] | None = None
    _pipeline_root: str = ""
    _ticket_id: str = ""
    _confirmation_id: str = ""
    _gate_key: str = ""
    _now: datetime | None = None

    def __enter__(self) -> ActivationExecutionPermit:
        if self.consumed:
            raise ActivationExecutionPermitError("permit_not_ready")
        if getattr(_thread_state, "active_permit", None) is not None:
            raise ActivationExecutionPermitError("nested permit blocked")
        request = self._load_request()
        if request.state != ACTIVATION_STATE_ACTIVE:
            raise ActivationExecutionPermitError("activation_not_active")
        if (request.active_expires_at or "").strip():
            expires = datetime.fromisoformat(
                request.active_expires_at.replace("Z", "+00:00")
            )
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            current = self._now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if current >= expires:
                raise ActivationExecutionPermitError("active_expired")
            self.expires_at = request.active_expires_at
        reservation = load_execution_reservation(
            self.activation_request_id,
            store_dir=self._reservation_dir,
        )
        if reservation is None:
            raise ActivationExecutionPermitError("permit_not_ready")
        if reservation.reservation_id != self.reservation_id:
            raise ActivationExecutionPermitError("permit_not_ready")
        if reservation.execution_attempt_id != self.execution_attempt_id:
            raise ActivationExecutionPermitError("permit_not_ready")
        if reservation.state != RESERVATION_STATE_RESERVED:
            raise ActivationExecutionPermitError("permit_not_ready")
        gate_record = find_ready_execution_gate_record(
            self.activation_request_id,
            gate_key=self._gate_key,
            history_dir=self._gate_history_dir,
        )
        if gate_record is None or gate_record.event_id != reservation.execution_gate_event_id:
            raise ActivationExecutionPermitError("execution_gate_correlation_mismatch")
        assessment = evaluate_production_execution_gate(
            request,
            ticket_id=self._ticket_id,
            confirmation_id=self._confirmation_id,
            pipeline_root=self._pipeline_root,
            store_dir=self._store_dir,
            history_dir=self._gate_history_dir,
            dry_run_history_dir=self._dry_run_history_dir,
            bundle_dir=self._bundle_dir,
            confirmation_dir=self._confirmation_dir,
            merged_config=self._merged_config,
            now=self._now,
        )
        if not assessment.execution_gate_ready:
            raise ActivationExecutionPermitError("permit_not_ready")
        self.granted = True
        self.entered_at = (self._now or datetime.now(timezone.utc)).isoformat()
        _thread_state.active_permit = self
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.granted = False
        self.consumed = True
        _thread_state.active_permit = None
        return False

    def __reduce__(self):
        raise TypeError("ActivationExecutionPermit cannot be serialized")

    def _load_request(self) -> ActivationRequest:
        return load_activation_request(
            self.activation_request_id,
            store_dir=self._store_dir,
        )


def build_activation_execution_permit(
    reservation: ProductionActivationExecutionReservation,
    *,
    pipeline_root: str,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    gate_history_dir: Path | None = None,
    dry_run_history_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ActivationExecutionPermit:
    return ActivationExecutionPermit(
        activation_request_id=reservation.activation_request_id,
        reservation_id=reservation.reservation_id,
        execution_attempt_id=reservation.execution_attempt_id,
        _store_dir=store_dir,
        _reservation_dir=reservation_dir,
        _gate_history_dir=gate_history_dir,
        _dry_run_history_dir=dry_run_history_dir,
        _bundle_dir=bundle_dir,
        _confirmation_dir=confirmation_dir,
        _merged_config=merged_config,
        _pipeline_root=pipeline_root,
        _ticket_id=reservation.ticket_id,
        _confirmation_id=reservation.confirmation_id,
        _gate_key=reservation.gate_key,
        _now=now,
    )


def get_active_execution_permit() -> ActivationExecutionPermit | None:
    """Return the in-process permit when inside its context manager."""
    return getattr(_thread_state, "active_permit", None)


def require_active_execution_permit() -> ActivationExecutionPermit:
    """Fail closed when runtime is invoked outside an active permit context."""
    permit = get_active_execution_permit()
    if permit is None or not permit.granted or permit.consumed:
        raise ActivationExecutionPermitError("permit_not_active")
    return permit


def evaluate_permit_ready(
    reservation: ProductionActivationExecutionReservation,
    *,
    pipeline_root: str,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    gate_history_dir: Path | None = None,
    dry_run_history_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    """Acquire and release a permit once to verify it can be granted."""
    permit = build_activation_execution_permit(
        reservation,
        pipeline_root=pipeline_root,
        store_dir=store_dir,
        reservation_dir=reservation_dir,
        gate_history_dir=gate_history_dir,
        dry_run_history_dir=dry_run_history_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        merged_config=merged_config,
        now=now,
    )
    try:
        with permit:
            return permit.granted
    except ActivationExecutionPermitError:
        return False
