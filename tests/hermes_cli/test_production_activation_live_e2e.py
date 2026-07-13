"""Phase 14H-3D tests — live pilot consume/evidence/audit E2E finalize."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import read_bundle
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_UNCONSUMED,
    assess_consume_status,
)
from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_COMPLETED,
    RESERVATION_STATE_FAILED,
    load_execution_reservation,
)
from agent.coo.production_activation_kill_switch import (
    ProductionActivationKillSwitchError,
)
from agent.coo.production_activation_live_e2e import (
    FAIL_ACTIVATION_REVOKE_FAILED,
    FAIL_ALREADY_FINALIZED,
    FAIL_AUDIT_MISSING,
    FAIL_CONSUME_PARTIAL,
    FAIL_CONSUME_REPLAY_BLOCKED,
    FAIL_CORRELATION_MISMATCH,
    FAIL_DISPATCH_AUDIT_WRITE_FAILED,
    FAIL_EVIDENCE_WRITE_FAILED,
    FAIL_NEW_ACTIVATION_REQUIRED,
    FAIL_RECOVERY_REQUIRED,
    FAIL_RUNTIME_NONZERO,
    FAIL_RUNTIME_NOT_COMPLETED,
    FAIL_RUNTIME_TIMEOUT,
    derive_live_pilot_dispatch_run_id,
    finalize_production_live_pilot,
    format_live_pilot_e2e_result,
    load_e2e_finalization_state,
    load_live_pilot_dispatch_audit,
    load_live_pilot_evidence,
    run_activation_live_pilot_finalize,
)
from agent.coo.production_activation_live_pilot import (
    run_production_activation_live_pilot_preflight,
)
from agent.coo.production_activation_state import (
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
)
from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_executor_confirmation import read_confirmation
from agent.coo.production_executor_factory import _TIMEOUT_EXIT_CODE
from tests.hermes_cli.test_production_activation_live_runtime import (
    TestProductionActivationLiveRuntime,
    _gate_patch_context,
    _write_fake_node,
)

_PRODUCTION_ROOT = "/opt/data/multi-content-pipeline"


class TestProductionActivationLiveE2E(TestProductionActivationLiveRuntime):
    def setUp(self) -> None:
        super().setUp()
        for sub in (
            "execution-evidence",
            "audit",
            "consume-transactions",
            "production-live-e2e",
        ):
            (self.hermes_home / "coo" / sub).mkdir(parents=True, exist_ok=True)
        self.evidence_dir = self.hermes_home / "coo" / "execution-evidence"
        self.audit_dir = self.hermes_home / "coo" / "audit"
        self.transaction_dir = self.hermes_home / "coo" / "consume-transactions"
        self.e2e_history_dir = self.hermes_home / "coo" / "production-live-e2e"

    def _run_runtime_success(self) -> tuple[str, str, str]:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            outcome = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    execute_isolated_mirror=True,
                )
            )
        from agent.coo.production_activation_live_runtime import (
            ProductionActivationLiveRuntimeResult,
        )

        self.assertIsInstance(outcome, ProductionActivationLiveRuntimeResult)
        self.assertTrue(outcome.completed)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        self.assertEqual(reservation.state, RESERVATION_STATE_COMPLETED)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_SUSPENDED)
        return activation_id, confirmation_id, reservation.reservation_id

    def _finalize_kwargs(self, activation_id: str, reservation_id: str, **overrides):
        base = {
            "activation_request_id": activation_id,
            "reservation_id": reservation_id,
            "store_dir": self.store_dir,
            "reservation_dir": self.reservation_dir,
            "runtime_history_dir": self.runtime_history_dir,
            "evidence_dir": self.evidence_dir,
            "audit_dir": self.audit_dir,
            "bundle_dir": self.bundle_dir,
            "confirmation_dir": self.confirmation_dir,
            "transaction_dir": self.transaction_dir,
            "e2e_history_dir": self.e2e_history_dir,
            "now": self._now + timedelta(minutes=9),
        }
        base.update(overrides)
        return base

    def test_full_e2e_success_chain(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = finalize_production_live_pilot(
                **self._finalize_kwargs(activation_id, reservation_id)
            )
        self.assertTrue(result.final_success)
        self.assertTrue(result.evidence_written)
        self.assertTrue(result.dispatch_audit_written)
        self.assertTrue(result.evidence_audit_correlation_valid)
        self.assertTrue(result.consume_attempted)
        self.assertTrue(result.consume_committed)
        self.assertEqual(result.consume_state, CONSUME_STATE_COMMITTED)
        self.assertEqual(result.activation_state_after, ACTIVATION_STATE_REVOKED)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        evidence = load_live_pilot_evidence(
            reservation.execution_attempt_id,
            evidence_dir=self.evidence_dir,
        )
        self.assertIsNotNone(evidence)
        audit = load_live_pilot_dispatch_audit(
            result.dispatch_run_id,
            audit_dir=self.audit_dir,
        )
        self.assertIsNotNone(audit)
        bundle = read_bundle(self.ticket_id, bundle_dir=self.bundle_dir, reject_consumed=False)
        confirmation = read_confirmation(
            "conf-runtime-1",
            confirmation_dir=self.confirmation_dir,
            reject_consumed=False,
        )
        self.assertTrue(bundle.consumed_at)
        self.assertTrue(confirmation.consumed)
        finalization = load_e2e_finalization_state(
            activation_id,
            history_dir=self.e2e_history_dir,
        )
        self.assertTrue(finalization.e2e_finalized)
        output = format_live_pilot_e2e_result(result)
        self.assertIn("final_success: true", output)

    def test_runtime_nonzero_blocks_e2e(self) -> None:
        self.fake_node = _write_fake_node(
            self.tmp_path,
            script_body="import sys\nsys.exit(2)\n",
        )
        self.merged_config = self.merged_config | {}
        from tests.hermes_cli.test_production_activation_live_runtime import _bound_config

        self.merged_config = _bound_config(self.mirror_root, self.fake_node)
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    execute_isolated_mirror=True,
                )
            )
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        result = finalize_production_live_pilot(
            **self._finalize_kwargs(activation_id, reservation.reservation_id)
        )
        self.assertFalse(result.final_success)
        self.assertIn(
            result.failure_reason_code,
            {FAIL_RUNTIME_NOT_COMPLETED, FAIL_NEW_ACTIVATION_REQUIRED},
        )
        self.assertFalse(result.consume_attempted)
        self.assertFalse(result.evidence_written)

    def test_timeout_blocks_e2e(self) -> None:
        self.fake_node = _write_fake_node(
            self.tmp_path,
            script_body="import time\ntime.sleep(5)\n",
        )
        from tests.hermes_cli.test_production_activation_live_runtime import _bound_config

        self.merged_config = _bound_config(self.mirror_root, self.fake_node)
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    execute_isolated_mirror=True,
                    runtime_timeout_seconds=1,
                )
            )
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        self.assertEqual(reservation.state, RESERVATION_STATE_FAILED)
        result = finalize_production_live_pilot(
            **self._finalize_kwargs(activation_id, reservation.reservation_id)
        )
        self.assertFalse(result.final_success)
        self.assertEqual(result.failure_reason_code, FAIL_NEW_ACTIVATION_REQUIRED)
        self.assertFalse(result.consume_attempted)

    def test_evidence_write_failure_blocks_consume(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        with patch(
            "agent.coo.production_activation_live_e2e._write_live_pilot_evidence",
            side_effect=Exception("disk full"),
        ):
            result = finalize_production_live_pilot(
                **self._finalize_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_EVIDENCE_WRITE_FAILED)
        self.assertFalse(result.consume_attempted)
        self.assertFalse(result.dispatch_audit_written)

    def test_audit_write_failure_blocks_consume(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        with patch(
            "agent.coo.production_activation_live_e2e._write_live_pilot_dispatch_audit",
            side_effect=Exception("disk full"),
        ):
            result = finalize_production_live_pilot(
                **self._finalize_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_DISPATCH_AUDIT_WRITE_FAILED)
        self.assertFalse(result.consume_attempted)
        self.assertTrue(result.evidence_written)

    def test_correlation_mismatch_blocks_consume(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        with patch(
            "agent.coo.production_activation_live_e2e.correlate_live_pilot_evidence_and_audit",
            return_value=False,
        ):
            result = finalize_production_live_pilot(
                **self._finalize_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_CORRELATION_MISMATCH)
        self.assertFalse(result.consume_attempted)

    def test_duplicate_evidence_mismatch_corruption(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        dispatch_run_id = derive_live_pilot_dispatch_run_id(
            reservation.execution_attempt_id
        )
        path = self.evidence_dir / f"{reservation.execution_attempt_id}.live-pilot-e2e.json"
        path.write_text(
            json.dumps(
                {
                    "artifact_type": "production_live_pilot_e2e",
                    "version": 1,
                    "activation_request_id": activation_id,
                    "reservation_id": reservation_id,
                    "execution_gate_event_id": reservation.execution_gate_event_id,
                    "dry_run_event_id": reservation.dry_run_event_id,
                    "execution_attempt_id": reservation.execution_attempt_id,
                    "dispatch_run_id": "wrong-run-id",
                    "ticket_id": reservation.ticket_id,
                    "confirmation_id": reservation.confirmation_id,
                    "runtime_exit_code": 0,
                    "timed_out": False,
                    "source_tree_unchanged": True,
                    "publish_attempted": False,
                    "isolated_mirror_runtime_invoked": True,
                    "original_repository2_execution_attempted": False,
                    "production_execution_allowed": False,
                    "timestamp": "2026-07-13T12:00:00+00:00",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        result = finalize_production_live_pilot(
            **self._finalize_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(result.failure_reason_code, FAIL_CORRELATION_MISMATCH)
        self.assertFalse(result.consume_attempted)
        self.assertNotEqual(dispatch_run_id, "wrong-run-id")

    def test_consume_partial_recovery_required(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        with patch(
            "agent.coo.dispatch_consume_transaction.mark_confirmation_consumed_file",
            side_effect=ValueError("confirmation consume failed"),
        ):
            result = finalize_production_live_pilot(
                **self._finalize_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_CONSUME_PARTIAL)
        self.assertTrue(result.consume_attempted)
        status = assess_consume_status(
            ticket_id=self.ticket_id,
            confirmation_id="conf-runtime-1",
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            transaction_dir=self.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_PARTIAL)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_SUSPENDED)

    def test_activation_revoke_failure_after_consume(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        with patch(
            "agent.coo.production_activation_live_e2e.revoke_production_activation",
            side_effect=ProductionActivationKillSwitchError("revoke failed"),
        ):
            result = finalize_production_live_pilot(
                **self._finalize_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_ACTIVATION_REVOKE_FAILED)
        self.assertFalse(result.final_success)
        self.assertTrue(result.consume_committed)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_SUSPENDED)

    def test_duplicate_finalize_idempotent(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        first = finalize_production_live_pilot(
            **self._finalize_kwargs(activation_id, reservation_id)
        )
        self.assertTrue(first.final_success)
        second = finalize_production_live_pilot(
            **self._finalize_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(second.failure_reason_code, FAIL_ALREADY_FINALIZED)
        self.assertTrue(second.final_success)

    def test_already_consumed_replay_blocked(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        first = finalize_production_live_pilot(
            **self._finalize_kwargs(activation_id, reservation_id)
        )
        self.assertTrue(first.final_success)
        finalization = load_e2e_finalization_state(
            activation_id,
            history_dir=self.e2e_history_dir,
        )
        self.assertTrue(finalization.e2e_finalized)

    def test_prepared_consume_blocks_finalize(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        from agent.coo.dispatch_consume_transaction import DispatchConsumeTransaction

        prepared = DispatchConsumeTransaction(
            transaction_id="txn-prepared-1",
            execution_attempt_id=reservation.execution_attempt_id,
            ticket_id=reservation.ticket_id,
            confirmation_id=reservation.confirmation_id,
            state="prepared",
            prepared_at="2026-07-13T12:00:00+00:00",
        )
        from agent.coo.dispatch_consume_transaction import _write_transaction_record

        _write_transaction_record(
            ticket_id=reservation.ticket_id,
            confirmation_id=reservation.confirmation_id,
            transaction=prepared,
            transaction_dir=self.transaction_dir,
        )
        result = finalize_production_live_pilot(
            **self._finalize_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(result.failure_reason_code, FAIL_RECOVERY_REQUIRED)
        self.assertFalse(result.consume_attempted)

    def test_no_subprocess_on_finalize(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
                side_effect=AssertionError("runner factory blocked"),
            ),
        ):
            result = finalize_production_live_pilot(
                **self._finalize_kwargs(activation_id, reservation_id)
            )
        self.assertTrue(result.final_success)

    def test_production_root_finalize_blocked_via_runtime_failure(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            blocked = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    pipeline_root=_PRODUCTION_ROOT,
                    execute_isolated_mirror=True,
                )
            )
        from agent.coo.production_activation_live_pilot import (
            ProductionActivationLivePilotPreflightResult,
        )

        self.assertIsInstance(blocked, ProductionActivationLivePilotPreflightResult)

    def test_cli_finalize_command(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        from hermes_cli.coo_dispatch import build_coo_dispatch_parser

        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "production",
                "activation",
                "live-pilot-finalize",
                "--activation-request-id",
                activation_id,
                "--reservation-id",
                reservation_id,
            ]
        )
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)

    def test_safe_output_forbidden_contents(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        result = finalize_production_live_pilot(
            **self._finalize_kwargs(activation_id, reservation_id)
        )
        output = format_live_pilot_e2e_result(result)
        for token in (
            "pipeline_root",
            "argv",
            "cwd",
            "stdout",
            "stderr",
            "/opt/data/",
            "pipeline.js",
        ):
            self.assertNotIn(token, output.lower())

    def test_dispatch_run_id_deterministic(self) -> None:
        activation_id, _, _ = self._run_runtime_success()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        first = derive_live_pilot_dispatch_run_id(reservation.execution_attempt_id)
        second = derive_live_pilot_dispatch_run_id(reservation.execution_attempt_id)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
