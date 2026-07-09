"""Dispatch unlock context — scoped ContextVar for terminal guard (Phase 10D).

Binds an active DispatchUnlockToken to the current execution context so the
Repository 2 terminal guard can allow a narrowly scoped command. No subprocess,
no adapter dispatch, and no Repository 2 mutation in this module.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator, Optional

from agent.coo.execution_dispatch_runtime import (
    DispatchUnlockToken,
    assert_dispatch_generation_matches,
    is_dispatch_unlock_token_expired,
)
from agent.coo.execution_dispatcher import ExecutionDispatchPlan
from agent.coo.execution_execute import ExecuteGate, ExecuteGateStatus
from agent.coo.execution_ticket import ExecutionTicket

_DEFAULT_PIPELINE_ROOT = "/opt/data/multi-content-pipeline"
_CREATE_CONTENT_ENTRYPOINT = "node pipeline.js"

_ACTIVE_UNLOCK: ContextVar[Optional["DispatchUnlockContext"]] = ContextVar(
    "HERMES_DISPATCH_UNLOCK",
    default=None,
)


@dataclass(frozen=True)
class DispatchUnlockContext:
    """Immutable active unlock snapshot for guard and adapter consumers."""

    token_id: str
    ticket_id: str
    plan_id: str
    execute_request_id: str
    gate_id: str
    dry_run_run_id: str
    target_skills: tuple[str, ...]
    dispatch_generation: int
    pipeline_root: str
    allowed_entrypoints: tuple[str, ...] = (_CREATE_CONTENT_ENTRYPOINT,)


def get_active_dispatch_unlock_context() -> Optional[DispatchUnlockContext]:
    """Return the active dispatch unlock context for the current task, if any."""
    return _ACTIVE_UNLOCK.get()


def _assert_gate_matches_token(gate: ExecuteGate, token: DispatchUnlockToken) -> None:
    if gate.status is not ExecuteGateStatus.APPROVED:
        raise ValueError(
            f"Cannot activate dispatch unlock context for gate {gate.gate_id} "
            f"in status {gate.status.value}"
        )
    if gate.gate_id != token.gate_id:
        raise ValueError(
            f"Gate {gate.gate_id} does not match token gate {token.gate_id}"
        )
    if gate.execute_request_id != token.execute_request_id:
        raise ValueError(
            f"Gate execute_request_id does not match token {token.execute_request_id}"
        )
    if gate.dry_run_run_id != token.dry_run_run_id:
        raise ValueError(
            f"Gate dry_run_run_id does not match token {token.dry_run_run_id}"
        )


def _assert_token_active(token: DispatchUnlockToken) -> None:
    if token.superseded:
        raise ValueError(
            f"Dispatch unlock token {token.token_id} has been superseded."
        )
    if token.consumed:
        raise ValueError(
            f"Dispatch unlock token {token.token_id} has already been consumed."
        )
    if is_dispatch_unlock_token_expired(token):
        raise ValueError(f"Dispatch unlock token {token.token_id} has expired.")


def build_dispatch_unlock_context(
    token: DispatchUnlockToken,
    *,
    ticket: ExecutionTicket,
    plan: ExecutionDispatchPlan,
    gate: ExecuteGate,
    pipeline_root: str = _DEFAULT_PIPELINE_ROOT,
) -> DispatchUnlockContext:
    """Validate token alignment and build an immutable unlock context."""
    _assert_token_active(token)
    assert_dispatch_generation_matches(token, ticket)
    _assert_gate_matches_token(gate, token)

    if token.ticket_id != ticket.ticket_id:
        raise ValueError(
            f"Token ticket_id {token.ticket_id!r} does not match ticket {ticket.ticket_id!r}"
        )
    if token.plan_id != plan.plan_id:
        raise ValueError(
            f"Token plan_id {token.plan_id!r} does not match plan {plan.plan_id!r}"
        )

    return DispatchUnlockContext(
        token_id=token.token_id,
        ticket_id=token.ticket_id,
        plan_id=token.plan_id,
        execute_request_id=token.execute_request_id,
        gate_id=token.gate_id,
        dry_run_run_id=token.dry_run_run_id,
        target_skills=token.target_skills,
        dispatch_generation=token.dispatch_generation,
        pipeline_root=pipeline_root,
        allowed_entrypoints=(_CREATE_CONTENT_ENTRYPOINT,),
    )


@contextmanager
def dispatch_unlock_scope(
    token: DispatchUnlockToken,
    *,
    ticket: ExecutionTicket,
    plan: ExecutionDispatchPlan,
    gate: ExecuteGate,
    pipeline_root: str = _DEFAULT_PIPELINE_ROOT,
) -> Iterator[DispatchUnlockContext]:
    """Bind an active dispatch unlock context for the duration of the block."""
    context = build_dispatch_unlock_context(
        token,
        ticket=ticket,
        plan=plan,
        gate=gate,
        pipeline_root=pipeline_root,
    )
    reset_token: Token = _ACTIVE_UNLOCK.set(context)
    try:
        yield context
    finally:
        _ACTIVE_UNLOCK.reset(reset_token)
