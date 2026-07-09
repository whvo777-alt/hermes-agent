"""Dispatch unlock context tests (Phase 10D)."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from agent.coo.dispatch_unlock_context import (
    build_dispatch_unlock_context,
    dispatch_unlock_scope,
    get_active_dispatch_unlock_context,
)
from agent.coo.execution_dispatch_runtime import DispatchUnlockToken
from agent.coo.execution_dispatcher import DispatchPlanStatus, ExecutionDispatchPlan
from agent.coo.execution_execute import ExecuteGate, ExecuteGateStatus
from agent.coo.execution_ticket import ExecutionTicket, ExecutionTicketStatus


def _future_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _context_fixtures() -> tuple[ExecutionTicket, ExecutionDispatchPlan, ExecuteGate, DispatchUnlockToken]:
    ticket = ExecutionTicket(
        ticket_id=str(uuid.uuid4()),
        approval_session_id=str(uuid.uuid4()),
        status=ExecutionTicketStatus.DISPATCH_PENDING,
        task_kind="create_content",
        run_date="2026-07-07",
        requester_id="discord-user-1",
        dispatch_generation=1,
    )
    plan = ExecutionDispatchPlan(
        plan_id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        status=DispatchPlanStatus.PLANNED,
        run_date=ticket.run_date,
    )
    execute_request_id = str(uuid.uuid4())
    dry_run_run_id = str(uuid.uuid4())
    gate = ExecuteGate(
        gate_id=str(uuid.uuid4()),
        execute_request_id=execute_request_id,
        ticket_id=ticket.ticket_id,
        dry_run_run_id=dry_run_run_id,
        status=ExecuteGateStatus.APPROVED,
        created_at="2026-07-07T00:00:00+00:00",
        decided_by=ticket.requester_id,
        decided_at="2026-07-07T00:00:01+00:00",
    )
    token = DispatchUnlockToken(
        token_id=str(uuid.uuid4()),
        ticket_id=ticket.ticket_id,
        plan_id=plan.plan_id,
        execute_request_id=execute_request_id,
        gate_id=gate.gate_id,
        dry_run_run_id=dry_run_run_id,
        target_skills=("create_content",),
        requested_by=ticket.requester_id,
        approved_by=ticket.requester_id,
        minted_at="2026-07-07T00:00:00+00:00",
        expires_at=_future_expiry(),
        dispatch_generation=1,
    )
    return ticket, plan, gate, token


class TestDispatchUnlockContext(unittest.TestCase):
    def test_context_set_inside_scope(self) -> None:
        ticket, plan, gate, token = _context_fixtures()
        self.assertIsNone(get_active_dispatch_unlock_context())

        with dispatch_unlock_scope(
            token,
            ticket=ticket,
            plan=plan,
            gate=gate,
            pipeline_root="/opt/data/multi-content-pipeline",
        ) as context:
            active = get_active_dispatch_unlock_context()
            self.assertIsNotNone(active)
            assert active is not None
            self.assertEqual(active.token_id, context.token_id)
            self.assertEqual(active.ticket_id, ticket.ticket_id)
            self.assertEqual(active.plan_id, plan.plan_id)
            self.assertEqual(active.execute_request_id, token.execute_request_id)
            self.assertEqual(active.gate_id, gate.gate_id)
            self.assertEqual(active.dry_run_run_id, token.dry_run_run_id)
            self.assertEqual(active.target_skills, ("create_content",))
            self.assertEqual(active.dispatch_generation, 1)
            self.assertEqual(active.allowed_entrypoints, ("node pipeline.js",))

        self.assertIsNone(get_active_dispatch_unlock_context())

    def test_context_reset_after_exception(self) -> None:
        ticket, plan, gate, token = _context_fixtures()

        with self.assertRaises(RuntimeError):
            with dispatch_unlock_scope(token, ticket=ticket, plan=plan, gate=gate):
                raise RuntimeError("boom")

        self.assertIsNone(get_active_dispatch_unlock_context())

    def test_nested_scope_restores_outer_context(self) -> None:
        ticket, plan, gate, token = _context_fixtures()
        other_token = DispatchUnlockToken(
            token_id=str(uuid.uuid4()),
            ticket_id=ticket.ticket_id,
            plan_id=plan.plan_id,
            execute_request_id=token.execute_request_id,
            gate_id=gate.gate_id,
            dry_run_run_id=token.dry_run_run_id,
            target_skills=("create_content",),
            requested_by=ticket.requester_id,
            approved_by=ticket.requester_id,
            minted_at="2026-07-07T00:00:00+00:00",
            expires_at=_future_expiry(),
            dispatch_generation=1,
        )

        with dispatch_unlock_scope(token, ticket=ticket, plan=plan, gate=gate) as outer:
            with dispatch_unlock_scope(
                other_token,
                ticket=ticket,
                plan=plan,
                gate=gate,
            ) as inner:
                self.assertEqual(get_active_dispatch_unlock_context().token_id, inner.token_id)
            self.assertEqual(get_active_dispatch_unlock_context().token_id, outer.token_id)

        self.assertIsNone(get_active_dispatch_unlock_context())

    def test_build_context_rejects_generation_mismatch(self) -> None:
        ticket, plan, gate, token = _context_fixtures()
        ticket.dispatch_generation = 2

        with self.assertRaises(ValueError) as ctx:
            build_dispatch_unlock_context(token, ticket=ticket, plan=plan, gate=gate)
        self.assertIn("does not match ticket dispatch generation", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
