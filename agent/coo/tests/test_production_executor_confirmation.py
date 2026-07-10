"""Tests for production executor confirmation (Phase 10L)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.coo.production_executor_confirmation import (
    ProductionExecutorConfirmation,
    ProductionExecutorConfirmationStore,
    REQUIRED_CONFIRMATION_PHRASE,
    assert_confirmation_valid,
    create_production_executor_confirmation,
    mark_confirmation_consumed_file,
    read_confirmation,
    write_confirmation,
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


class TestProductionExecutorConfirmationPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.get_home_patch = patch(
            "agent.coo.production_executor_confirmation.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.get_home_patch.start()
        self.store = ProductionExecutorConfirmationStore()

    def tearDown(self) -> None:
        self.get_home_patch.stop()
        self.tmp.cleanup()

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
            dispatch_request_id="req-confirm-file",
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

    def test_create_write_read_round_trip(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-file",
            operator_name="File Operator",
            confirmation_reason="persist test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
            persist_to_file=True,
            confirmation_dir=self.confirmation_dir,
        )
        loaded = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.confirmation_dir,
        )
        self.assertEqual(loaded.operator_id, "op-file")
        self.assertEqual(loaded.operator_name, "File Operator")
        self.assertEqual(loaded.confirmation_reason, "persist test")

    def test_phrase_not_stored_in_json(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-file",
            operator_name="File Operator",
            confirmation_reason="persist test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
            persist_to_file=True,
            confirmation_dir=self.confirmation_dir,
        )
        path = self.confirmation_dir / f"{confirmation.confirmation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("confirmation_phrase", payload)
        self.assertTrue(payload["phrase_verified"])

    def test_expired_confirmation_rejected_on_read(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-file",
            operator_name="File Operator",
            confirmation_reason="persist test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
            persist_to_file=True,
            confirmation_dir=self.confirmation_dir,
        )
        path = self.confirmation_dir / f"{confirmation.confirmation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["expires_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.confirmation_dir,
        )
        with self.assertRaises(ValueError) as exc:
            assert_confirmation_valid(
                loaded,
                token=token,
                dispatch_request=dispatch_request,
                ticket=ticket,
            )
        self.assertIn("expired", str(exc.exception))

    def test_consumed_confirmation_rejected_on_read(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-file",
            operator_name="File Operator",
            confirmation_reason="persist test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
            persist_to_file=True,
            confirmation_dir=self.confirmation_dir,
        )
        mark_confirmation_consumed_file(
            confirmation.confirmation_id,
            confirmation_dir=self.confirmation_dir,
        )
        with self.assertRaises(ValueError) as exc:
            read_confirmation(
                confirmation.confirmation_id,
                confirmation_dir=self.confirmation_dir,
            )
        self.assertIn("consumed", str(exc.exception))

    def test_corrupted_json_rejected(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-file",
            operator_name="File Operator",
            confirmation_reason="persist test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
            persist_to_file=True,
            confirmation_dir=self.confirmation_dir,
        )
        path = self.confirmation_dir / f"{confirmation.confirmation_id}.json"
        path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            read_confirmation(
                confirmation.confirmation_id,
                confirmation_dir=self.confirmation_dir,
            )
        self.assertIn("corrupted", str(exc.exception))

    def test_outside_hermes_path_rejected_without_mkdir(self) -> None:
        ticket, token, dispatch_request = self._dispatch_binding()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-file",
            operator_name="File Operator",
            confirmation_reason="persist test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=self.store,
        )
        outside = Path(self.tmp.name) / "outside" / "confirmations"
        with self.assertRaises(ValueError):
            write_confirmation(confirmation, confirmation_dir=outside)
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
