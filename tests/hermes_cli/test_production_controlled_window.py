"""Phase 15B tests — controlled production window lifecycle."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_PARTIAL,
    CooDispatchConsumeStatus,
)
from agent.coo.dispatch_gateway_operator_dashboard import (
    build_operator_dashboard_summary,
)
from agent.coo.production_activation_execution_reservation import (
    load_execution_reservation,
)
from agent.coo.production_controlled_window import (
    ACTION_PREPARE_PHASE_15C_RUNTIME_PERMISSION,
    BLOCK_OPERATOR_IDENTITY_INVALID,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_WINDOW_NOT_OPEN,
    BLOCK_WINDOW_NOT_STARTED,
    REASON_INCIDENT_DETECTED,
    REASON_OPERATOR_CLOSE,
    WINDOW_CLOSED,
    WINDOW_EMERGENCY_CLOSED,
    WINDOW_EXPIRED,
    WINDOW_OPEN,
    ProductionControlledWindowError,
    build_production_controlled_window_release_summary,
    close_production_controlled_window,
    emergency_close_production_controlled_window,
    evaluate_production_controlled_window,
    format_production_controlled_window_status,
    load_window_lifecycle_events,
    open_production_controlled_window,
    resolve_latest_controlled_window_dashboard_digest,
)
from agent.coo.production_final_signoff import record_production_final_signoff
from agent.coo.production_governed_cutover import (
    load_governed_cutover_contract,
    prepare_production_governed_cutover,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.test_production_final_signoff import _EXECUTOR_ID, _FINAL_SIGNER
from tests.hermes_cli.test_production_governed_cutover import (
    TestProductionGovernedCutover,
    _CUTOVER_OPERATOR,
)

_WINDOW_OPERATOR = "window-operator-phase15b"


class TestProductionControlledWindow(TestProductionGovernedCutover):
    def setUp(self) -> None:
        super().setUp()
        self.window_store_dir = (
            self.hermes_home / "coo" / "production-controlled-window"
        )
        self.window_store_dir.mkdir(parents=True, exist_ok=True)
        # Keep parent-chain clock (Phase 15A) to avoid activation history skew.

    def _active_window(self) -> tuple[str, str]:
        start = self._now - timedelta(minutes=10)
        end = self._now + timedelta(minutes=50)
        return start.isoformat(), end.isoformat()

    def _future_window_bounds(self) -> tuple[str, str]:
        start = self._now + timedelta(minutes=30)
        end = start + timedelta(minutes=30)
        return start.isoformat(), end.isoformat()

    def _window_kwargs(self, activation_id: str, **overrides):
        base = {
            **self._final_kwargs(activation_id, "unused"),
            "governed_cutover_store_dir": self.governed_cutover_store_dir,
            "window_store_dir": self.window_store_dir,
            "now": self._now,
        }
        # Drop bogus reservation placeholder; callers pass real reservation via kwargs path.
        del base["reservation_id"]
        base.update(overrides)
        return base

    def _prepare_active_contract(self) -> tuple[str, str]:
        activation_id, reservation_id = self._complete_final_signed()
        window_start, window_end = self._active_window()
        prepare_production_governed_cutover(
            **self._cutover_kwargs(
                activation_id,
                reservation_id,
                operator_id=_CUTOVER_OPERATOR,
                window_start=window_start,
                window_end=window_end,
                now=self._now,
            )
        )
        return activation_id, reservation_id

    def _eval_kwargs(self, activation_id: str, **overrides):
        base = {
            "activation_request_id": activation_id,
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
            "validation_store_dir": self.validation_store_dir,
            "final_signoff_store_dir": self.final_signoff_store_dir,
            "preflight_history_dir": self.preflight_history_dir,
            "governed_cutover_store_dir": self.governed_cutover_store_dir,
            "window_store_dir": self.window_store_dir,
            "repo_root": self.repo_root,
            "merged_config": {},
            "now": self._now,
        }
        base.update(overrides)
        return base

    def test_open_prepared_contract_within_window(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        contract_path = self.governed_cutover_store_dir / f"{activation_id}.json"
        before = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            summary = open_production_controlled_window(
                **self._eval_kwargs(
                    activation_id,
                    operator_id=_WINDOW_OPERATOR,
                )
            )
        after = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(summary.window_state, WINDOW_OPEN)
        self.assertTrue(summary.window_open)
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.execution_permit_created)
        self.assertEqual(
            summary.recommended_action,
            ACTION_PREPARE_PHASE_15C_RUNTIME_PERMISSION,
        )
        contract = load_governed_cutover_contract(
            activation_id,
            store_dir=self.governed_cutover_store_dir,
        )
        assert contract is not None
        self.assertFalse(contract.window_opened)
        self.assertFalse(contract.cutover_started)
        self.assertFalse(contract.execution_permit_created)

    def test_open_event_append_only(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        _, events = load_window_lifecycle_events(
            activation_id, store_dir=self.window_store_dir
        )
        types = [event.event_type for event in events]
        self.assertIn("window_open_requested", types)
        self.assertIn("window_opened", types)
        self.assertEqual(len(events), 2)

    def test_before_start_open_blocked(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        window_start, window_end = self._future_window_bounds()
        prepare_production_governed_cutover(
            **self._cutover_kwargs(
                activation_id,
                reservation_id,
                window_start=window_start,
                window_end=window_end,
                now=self._now,
            )
        )
        with self.assertRaises(ProductionControlledWindowError) as ctx:
            open_production_controlled_window(
                **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
            )
        self.assertIn("window_not_started", str(ctx.exception))
        summary = evaluate_production_controlled_window(
            **self._eval_kwargs(activation_id)
        )
        self.assertIn(BLOCK_WINDOW_NOT_STARTED, summary.blocking_items)

    def test_after_end_expired_open_blocked(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        window_start, window_end = self._active_window()
        prepare_production_governed_cutover(
            **self._cutover_kwargs(
                activation_id,
                reservation_id,
                window_start=window_start,
                window_end=window_end,
                now=self._now,
            )
        )
        later = self._now + timedelta(hours=2)
        with self.assertRaises(ProductionControlledWindowError):
            open_production_controlled_window(
                **self._eval_kwargs(
                    activation_id,
                    operator_id=_WINDOW_OPERATOR,
                    now=later,
                )
            )
        summary = evaluate_production_controlled_window(
            **self._eval_kwargs(activation_id, now=later)
        )
        self.assertTrue(summary.expired)
        self.assertEqual(summary.window_state, WINDOW_EXPIRED)

    def test_recovery_blocks_open(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        reservation = load_execution_reservation(
            activation_id, store_dir=self.reservation_dir
        )
        assert reservation is not None
        partial = CooDispatchConsumeStatus(
            consume_state=CONSUME_STATE_PARTIAL,
            transaction_id="tx-partial",
            execution_attempt_id=reservation.execution_attempt_id,
            bundle_consumed=True,
            confirmation_consumed=False,
            recovery_required=True,
        )
        with (
            patch(
                "agent.coo.production_live_operational_signoff.assess_consume_status",
                return_value=partial,
            ),
            patch(
                "agent.coo.production_live_rollback_validation.assess_consume_status",
                return_value=partial,
            ),
        ):
            with self.assertRaises(ProductionControlledWindowError):
                open_production_controlled_window(
                    **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
                )
            summary = evaluate_production_controlled_window(
                **self._eval_kwargs(activation_id)
            )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, summary.blocking_items)

    def test_executor_operator_identity_conflict(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        summary = evaluate_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_EXECUTOR_ID)
        )
        self.assertIn(BLOCK_OPERATOR_IDENTITY_INVALID, summary.blocking_items)
        with self.assertRaises(ProductionControlledWindowError):
            open_production_controlled_window(
                **self._eval_kwargs(activation_id, operator_id=_EXECUTOR_ID)
            )

    def test_duplicate_open_idempotent(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        first = open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        second = open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        self.assertTrue(first.window_open)
        self.assertTrue(second.window_open)
        _, events = load_window_lifecycle_events(
            activation_id, store_dir=self.window_store_dir
        )
        self.assertEqual(len([e for e in events if e.event_type == "window_opened"]), 1)

    def test_normal_close_and_idempotent(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        closed = close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                reason_code=REASON_OPERATOR_CLOSE,
            )
        )
        self.assertEqual(closed.window_state, WINDOW_CLOSED)
        self.assertTrue(closed.window_closed)
        again = close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                reason_code=REASON_OPERATOR_CLOSE,
            )
        )
        self.assertTrue(again.already_closed)
        _, events = load_window_lifecycle_events(
            activation_id, store_dir=self.window_store_dir
        )
        self.assertEqual(len([e for e in events if e.event_type == "window_closed"]), 1)

    def test_close_when_not_open_blocked(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        with self.assertRaises(ProductionControlledWindowError) as ctx:
            close_production_controlled_window(
                **self._eval_kwargs(
                    activation_id,
                    operator_id=_WINDOW_OPERATOR,
                    reason_code=REASON_OPERATOR_CLOSE,
                )
            )
        self.assertIn("window_not_open", str(ctx.exception))

    def test_emergency_close(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        summary = emergency_close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                actor_role="incident_commander",
                reason_code=REASON_INCIDENT_DETECTED,
            )
        )
        self.assertEqual(summary.window_state, WINDOW_EMERGENCY_CLOSED)
        self.assertTrue(summary.emergency_closed)
        again = emergency_close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                actor_role="incident_commander",
                reason_code=REASON_INCIDENT_DETECTED,
            )
        )
        self.assertTrue(again.already_emergency_closed)

    def test_invalid_emergency_role_and_reason(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        with self.assertRaises(ProductionControlledWindowError):
            emergency_close_production_controlled_window(
                **self._eval_kwargs(
                    activation_id,
                    operator_id=_WINDOW_OPERATOR,
                    actor_role="approver",
                    reason_code=REASON_INCIDENT_DETECTED,
                )
            )
        with self.assertRaises(ProductionControlledWindowError):
            emergency_close_production_controlled_window(
                **self._eval_kwargs(
                    activation_id,
                    operator_id=_WINDOW_OPERATOR,
                    actor_role="operator",
                    reason_code="not_a_reason",
                )
            )

    def test_reopen_after_close_blocked(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                reason_code=REASON_OPERATOR_CLOSE,
            )
        )
        with self.assertRaises(ProductionControlledWindowError) as ctx:
            open_production_controlled_window(
                **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
            )
        self.assertIn("reopen_not_allowed", str(ctx.exception))

    def test_reopen_after_emergency_blocked(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        emergency_close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                actor_role="operator",
                reason_code=REASON_INCIDENT_DETECTED,
            )
        )
        with self.assertRaises(ProductionControlledWindowError):
            open_production_controlled_window(
                **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
            )

    def test_status_read_only_and_safe_output(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        before = list(self.window_store_dir.glob("*.json"))
        summary = evaluate_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        output = format_production_controlled_window_status(summary)
        after = list(self.window_store_dir.glob("*.json"))
        self.assertEqual(before, after)
        self.assertIn("window_state:", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertNotIn(_WINDOW_OPERATOR, output)
        self.assertNotIn(_CUTOVER_OPERATOR, output)

    def test_history_and_cli_parsers(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        parser = build_coo_dispatch_parser()
        status_args = parser.parse_args(
            [
                "production",
                "governed-cutover",
                "window",
                "status",
                "--activation-request-id",
                activation_id,
            ]
        )
        history_args = parser.parse_args(
            [
                "production",
                "governed-cutover",
                "window",
                "history",
                "--activation-request-id",
                activation_id,
            ]
        )
        self.assertEqual(
            status_args.handler.__name__,
            "_cmd_production_controlled_window_status",
        )
        self.assertEqual(
            history_args.handler.__name__,
            "_cmd_production_controlled_window_history",
        )
        # Legacy cutover-check still registered unchanged.
        legacy = parser.parse_args(
            ["production", "cutover-check", "--ticket-id", "ticket-x"]
        )
        self.assertEqual(legacy.handler.__name__, "_cmd_production_cutover_check")

    def test_corrupted_lifecycle_fail_closed(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        path = self.window_store_dir / f"{activation_id}.json"
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ProductionControlledWindowError):
            evaluate_production_controlled_window(**self._eval_kwargs(activation_id))

    def test_contract_id_mismatch_fail_closed(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        path = self.window_store_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cutover_contract_id"] = "other-contract"
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_controlled_window(
            **self._eval_kwargs(activation_id)
        )
        self.assertIn("contract_correlation_mismatch", summary.blocking_items)

    def test_dashboard_and_release_summary(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        digest = resolve_latest_controlled_window_dashboard_digest(
            governed_cutover_store_dir=self.governed_cutover_store_dir,
            window_store_dir=self.window_store_dir,
            final_signoff_store_dir=self.final_signoff_store_dir,
            store_dir=self.store_dir,
            reservation_dir=self.reservation_dir,
            runtime_history_dir=self.runtime_history_dir,
            evidence_dir=self.evidence_dir,
            audit_dir=self.audit_dir,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            transaction_dir=self.transaction_dir,
            e2e_history_dir=self.e2e_history_dir,
            signoff_store_dir=self.signoff_store_dir,
            validation_store_dir=self.validation_store_dir,
            preflight_history_dir=self.preflight_history_dir,
            repo_root=self.repo_root,
            merged_config={},
        )
        self.assertTrue(digest.controlled_window_open)
        self.assertEqual(digest.controlled_window_state, WINDOW_OPEN)
        dashboard = build_operator_dashboard_summary(merged_config={})
        self.assertTrue(hasattr(dashboard, "controlled_window_state"))
        summary = evaluate_production_controlled_window(
            **self._eval_kwargs(activation_id)
        )
        release = build_production_controlled_window_release_summary(summary)
        self.assertEqual(release.release_status, "CONTROLLED_WINDOW_OPEN")
        self.assertEqual(
            release.next_phase,
            "Phase_15C_production_runtime_permission",
        )
        self.assertFalse(release.production_execution_allowed)
        self.assertFalse(release.cutover_started)
        self.assertFalse(release.execution_permit_created)

    def test_force_flags_keep_output_false(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        summary = evaluate_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                force_production_execution_allowed=True,
                force_cutover_started=True,
                force_execution_permit_created=True,
            )
        )
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.execution_permit_created)
        self.assertIn("production_execution_enabled", summary.blocking_items)

    def test_expired_open_window_requires_manual_close(self) -> None:
        activation_id, _ = self._prepare_active_contract()
        open_production_controlled_window(
            **self._eval_kwargs(activation_id, operator_id=_WINDOW_OPERATOR)
        )
        later = self._now + timedelta(hours=2)
        summary = evaluate_production_controlled_window(
            **self._eval_kwargs(activation_id, now=later)
        )
        self.assertTrue(summary.expired)
        self.assertEqual(summary.window_state, WINDOW_EXPIRED)
        # Explicit close still allowed on lifecycle OPEN under the covers.
        closed = close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                reason_code="maintenance_window_expired",
                now=later,
            )
        )
        self.assertTrue(closed.window_closed)


if __name__ == "__main__":
    unittest.main()
