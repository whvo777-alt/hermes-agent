"""Phase 14H-3E tests — production live operational sign-off."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    DispatchConsumeTransaction,
    _write_transaction_record,
)
from agent.coo.production_activation_execution_reservation import (
    load_execution_reservation,
)
from agent.coo.production_activation_live_e2e import (
    finalize_production_live_pilot,
)
from agent.coo.production_activation_state import (
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
)
from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_live_operational_signoff import (
    BLOCK_ACTIVATION_NOT_REVOKED,
    BLOCK_CORRELATION_INVALID,
    BLOCK_DISPATCH_AUDIT_MISSING,
    BLOCK_E2E_NOT_FINALIZED,
    BLOCK_EVIDENCE_MISSING,
    BLOCK_PRODUCTION_ROOT_TOUCHED,
    BLOCK_PUBLISH_ATTEMPT_DETECTED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_RESERVATION_NOT_COMPLETED,
    BLOCK_ROLLBACK_NOT_READY,
    BLOCK_RUNTIME_FAILED,
    BLOCK_RUNTIME_TIMEOUT,
    BLOCK_SIGNER_IDENTITY_CONFLICT,
    BLOCK_SOURCE_TREE_MUTATED,
    SIGNOFF_BLOCKED,
    SIGNOFF_READY,
    SIGNOFF_READY_WITH_WARNINGS,
    SIGNOFF_REQUIRES_RECOVERY,
    ProductionLiveOperationalSignoffError,
    _PRODUCTION_ROOT_TOUCHED_SENTINEL,
    default_signoff_store_dir,
    evaluate_production_live_operational_signoff,
    format_production_live_operational_status,
    load_operational_signoff_record,
    record_production_live_operational_signoff,
)
from tests.hermes_cli.test_production_activation_live_e2e import (
    TestProductionActivationLiveE2E,
)
from tests.hermes_cli.test_production_activation_live_runtime import (
    _gate_patch_context,
    _write_fake_node,
)

_EXECUTOR_ID = "executor-e"
_SIGNOFF_OPERATOR = "operator-supervisor"


class TestProductionLiveOperationalSignoff(TestProductionActivationLiveE2E):
    def setUp(self) -> None:
        super().setUp()
        (self.hermes_home / "coo" / "production-live-signoff").mkdir(
            parents=True,
            exist_ok=True,
        )
        self.signoff_store_dir = self.hermes_home / "coo" / "production-live-signoff"

    def _finalize_success(self) -> tuple[str, str]:
        activation_id, _, reservation_id = self._run_runtime_success()
        finalize_production_live_pilot(
            **self._finalize_kwargs(activation_id, reservation_id)
        )
        return activation_id, reservation_id

    def _signoff_kwargs(self, activation_id: str, reservation_id: str, **overrides):
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
            "signoff_store_dir": self.signoff_store_dir,
            "merged_config": self.merged_config,
        }
        base.update(overrides)
        return base

    def test_full_success_signoff_ready_with_warnings(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        summary = evaluate_production_live_operational_signoff(
            **self._signoff_kwargs(activation_id, reservation_id)
        )
        self.assertIn(
            summary.signoff_status,
            {SIGNOFF_READY_WITH_WARNINGS, SIGNOFF_READY},
        )
        self.assertTrue(summary.first_run_detected)
        self.assertTrue(summary.runtime_completed)
        self.assertTrue(summary.evidence_present)
        self.assertTrue(summary.dispatch_audit_present)
        self.assertTrue(summary.evidence_audit_correlation_valid)
        self.assertTrue(summary.consume_committed)
        self.assertTrue(summary.activation_revoked)
        self.assertTrue(summary.e2e_finalized)
        self.assertTrue(summary.operator_checklist_passed)

    def test_signoff_record_append_only(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            summary = record_production_live_operational_signoff(
                **self._signoff_kwargs(
                    activation_id,
                    reservation_id,
                    operator_id=_SIGNOFF_OPERATOR,
                )
            )
        self.assertTrue(summary.already_signed_off)
        record = load_operational_signoff_record(
            activation_id,
            store_dir=self.signoff_store_dir,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.signoff_status, summary.signoff_status)
        self.assertFalse(record.production_execution_allowed)

    def test_duplicate_signoff_idempotent(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        record_production_live_operational_signoff(
            **self._signoff_kwargs(
                activation_id,
                reservation_id,
                operator_id=_SIGNOFF_OPERATOR,
            )
        )
        second = record_production_live_operational_signoff(
            **self._signoff_kwargs(
                activation_id,
                reservation_id,
                operator_id=_SIGNOFF_OPERATOR,
            )
        )
        self.assertTrue(second.already_signed_off)

    def test_signer_identity_conflict_blocked(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        with self.assertRaises(ProductionLiveOperationalSignoffError):
            record_production_live_operational_signoff(
                **self._signoff_kwargs(
                    activation_id,
                    reservation_id,
                    operator_id=_EXECUTOR_ID,
                )
            )

    def test_second_run_not_first_run(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        other = self.e2e_history_dir / "other-activation.json"
        other.write_text(
            json.dumps(
                {
                    "version": 1,
                    "activation_request_id": "other-id",
                    "finalization": {"e2e_finalized": True},
                    "records": [],
                }
            ),
            encoding="utf-8",
        )
        summary = evaluate_production_live_operational_signoff(
            **self._signoff_kwargs(activation_id, reservation_id)
        )
        self.assertFalse(summary.first_run_detected)

    def test_runtime_failure_blocked(self) -> None:
        self.fake_node = _write_fake_node(
            self.tmp_path,
            script_body="import sys\nsys.exit(2)\n",
        )
        from tests.hermes_cli.test_production_activation_live_runtime import _bound_config

        self.merged_config = _bound_config(self.mirror_root, self.fake_node)
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            from agent.coo.production_activation_live_pilot import (
                run_production_activation_live_pilot_preflight,
            )

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
        summary = evaluate_production_live_operational_signoff(
            **self._signoff_kwargs(activation_id, reservation.reservation_id)
        )
        self.assertEqual(summary.signoff_status, SIGNOFF_BLOCKED)
        self.assertIn(BLOCK_RUNTIME_FAILED, summary.blocking_items)

    def test_e2e_not_finalized_blocked(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        summary = evaluate_production_live_operational_signoff(
            **self._signoff_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.signoff_status, SIGNOFF_BLOCKED)
        self.assertIn(BLOCK_E2E_NOT_FINALIZED, summary.blocking_items)

    def test_consume_partial_requires_recovery(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        from agent.coo.dispatch_consume_transaction import CooDispatchConsumeStatus

        with patch(
            "agent.coo.production_live_operational_signoff.assess_consume_status",
            return_value=CooDispatchConsumeStatus(
                consume_state=CONSUME_STATE_PARTIAL,
                transaction_id="txn-partial-signoff",
                execution_attempt_id=reservation.execution_attempt_id,
                bundle_consumed=True,
                confirmation_consumed=False,
                recovery_required=True,
            ),
        ):
            summary = evaluate_production_live_operational_signoff(
                **self._signoff_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(summary.signoff_status, SIGNOFF_REQUIRES_RECOVERY)
        self.assertIn(BLOCK_RECOVERY_REQUIRED, summary.blocking_items)
        from dataclasses import replace

        blocked_summary = replace(summary, signoff_status=SIGNOFF_REQUIRES_RECOVERY)
        with patch(
            "agent.coo.production_live_operational_signoff.evaluate_production_live_operational_signoff",
            return_value=blocked_summary,
        ):
            with self.assertRaises(ProductionLiveOperationalSignoffError):
                record_production_live_operational_signoff(
                    **self._signoff_kwargs(
                        activation_id,
                        reservation_id,
                        operator_id=_SIGNOFF_OPERATOR,
                    )
                )

    def test_production_root_touched_blocked(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        sentinel = self.signoff_store_dir / _PRODUCTION_ROOT_TOUCHED_SENTINEL
        sentinel.write_text("1", encoding="utf-8")
        summary = evaluate_production_live_operational_signoff(
            **self._signoff_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.signoff_status, SIGNOFF_BLOCKED)
        self.assertIn(BLOCK_PRODUCTION_ROOT_TOUCHED, summary.blocking_items)

    def test_activation_not_revoked_blocked(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(request.state, ACTIVATION_STATE_REVOKED)
        from dataclasses import replace

        suspended_request = replace(request, state=ACTIVATION_STATE_SUSPENDED)
        with patch(
            "agent.coo.production_live_operational_signoff.load_activation_request",
            return_value=suspended_request,
        ):
            summary = evaluate_production_live_operational_signoff(
                **self._signoff_kwargs(activation_id, reservation_id)
            )
        self.assertIn(BLOCK_ACTIVATION_NOT_REVOKED, summary.blocking_items)

    def test_evidence_missing_blocked(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        for path in self.evidence_dir.glob("*.live-pilot-e2e.json"):
            path.unlink()
        summary = evaluate_production_live_operational_signoff(
            **self._signoff_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_EVIDENCE_MISSING, summary.blocking_items)

    def test_safe_output_forbidden_fields(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        summary = evaluate_production_live_operational_signoff(
            **self._signoff_kwargs(activation_id, reservation_id)
        )
        output = format_production_live_operational_status(summary)
        for token in (
            "pipeline_root",
            "argv",
            "signed_by",
            "rollback_commit",
            "/opt/data/",
        ):
            self.assertNotIn(token, output.lower())

    def test_no_subprocess_on_signoff(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
                side_effect=AssertionError("runner blocked"),
            ),
        ):
            summary = evaluate_production_live_operational_signoff(
                **self._signoff_kwargs(activation_id, reservation_id)
            )
        self.assertTrue(summary.e2e_finalized)

    def test_status_cli_read_only(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        from hermes_cli.coo_dispatch import build_coo_dispatch_parser

        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "production",
                "activation",
                "live-pilot-status",
                "--activation-request-id",
                activation_id,
                "--reservation-id",
                reservation_id,
            ]
        )
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            exit_code = args.handler(args)
        self.assertIn(exit_code, {0, 1})


if __name__ == "__main__":
    unittest.main()
