"""Tests for production executor confirmation (Phase 10L)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from agent.coo.production_executor_confirmation import (
    ProductionExecutorConfirmation,
    ProductionExecutorConfirmationStore,
    REQUIRED_CONFIRMATION_PHRASE,
    assert_confirmation_valid,
    create_production_executor_confirmation,
)
from agent.coo.tests.test_execution_dispatch_runtime import _approved_unlock_context


class TestProductionExecutorConfirmation(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProductionExecutorConfirmationStore()

    def _dispatch_binding(self):
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        from agent.coo.execution_dispatch_runtime import (
            DispatchExecutionRequest,
            DispatchUnlockTokenStore,
            create_dispatch_unlock_token,
        )

        token_store = DispatchUnlockTokenStore()
        token = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )
        dispatch_request = DispatchExecutionRequest(
            dispatch_request_id="req-confirm",
            execute_request_id=token.execute_request_id,
            gate_id=gate.gate_id,
            ticket_id=ticket.ticket_id,
            plan_id=plan.plan_id,
            dry_run_run_id=token.dry_run_run_id,
            unlock_token_id=token.token_id,
            target_skills=list(token.target_skills),
            requested_by=ticket.requester_id,
            requested_at="2026-07-07T00:00:00+00:00",
        )
        return ticket, token, dispatch_request

    def test_phrase_mismatch_raises(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        with self.assertRaises(ValueError) as exc:
            create_production_executor_confirmation(
                ticket_id=ticket.ticket_id,
                plan_id=token.plan_id,
                unlock_token_id=token.token_id,
                dispatch_request_id=dispatch_request.dispatch_request_id,
                operator_id="op-1",
                operator_name="Operator",
                confirmation_reason="test",
                confirmation_phrase="WRONG-PHRASE",
                confirmation_store=self.store,
            )
        self.assertIn(REQUIRED_CONFIRMATION_PHRASE, str(exc.exception))

    def test_valid_confirmation_created(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-1",
            operator_name="Operator",
            confirmation_reason="ready to run",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
        )
        self.assertEqual(confirmation.operator_id, "op-1")
        self.assertEqual(confirmation.operator_name, "Operator")
        self.assertEqual(confirmation.confirmation_reason, "ready to run")
        self.assertFalse(confirmation.consumed)

    def test_expired_confirmation_invalid(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-1",
            operator_name="Operator",
            confirmation_reason="test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
        )
        confirmation.expires_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        with self.assertRaises(ValueError) as exc:
            assert_confirmation_valid(
                confirmation,
                token=token,
                dispatch_request=dispatch_request,
                ticket=ticket,
            )
        self.assertIn("expired", str(exc.exception))

    def test_consumed_confirmation_invalid(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-1",
            operator_name="Operator",
            confirmation_reason="test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
        )
        confirmation.consumed = True
        with self.assertRaises(ValueError) as exc:
            assert_confirmation_valid(
                confirmation,
                token=token,
                dispatch_request=dispatch_request,
                ticket=ticket,
            )
        self.assertIn("consumed", str(exc.exception))

    def test_wrong_token_alignment_invalid(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id="wrong-token",
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-1",
            operator_name="Operator",
            confirmation_reason="test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
        )
        with self.assertRaises(ValueError) as exc:
            assert_confirmation_valid(
                confirmation,
                token=token,
                dispatch_request=dispatch_request,
                ticket=ticket,
            )
        self.assertIn("unlock_token_id", str(exc.exception))

    def test_consume_once_ok_second_raises(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-1",
            operator_name="Operator",
            confirmation_reason="test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
        )
        self.store.consume(confirmation.confirmation_id)
        with self.assertRaises(ValueError) as exc:
            self.store.consume(confirmation.confirmation_id)
        self.assertIn("consumed", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
