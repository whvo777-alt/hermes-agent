"""Phase 15A tests — governed production cutover contract."""

from __future__ import annotations

import io
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
)
from agent.coo.dispatch_gateway_operator_dashboard import (
    build_operator_dashboard_summary,
)
from agent.coo.production_activation_execution_reservation import (
    load_execution_reservation,
)
from agent.coo.production_final_signoff import (
    record_production_final_signoff,
)
from agent.coo.production_governed_cutover import (
    ACTION_DEFINE_MAINTENANCE_WINDOW,
    ACTION_GOVERNED_CUTOVER_READY_PREPARE_CONTRACT,
    ACTION_PREPARE_PHASE_15B_CONTROLLED_PRODUCTION_WINDOW,
    ACTION_RUN_CONSUME_RECOVERY,
    BLOCK_ACTIVATION_NOT_REVOKED,
    BLOCK_ARTIFACT_CORRUPTED,
    BLOCK_CONSUME_NOT_COMMITTED,
    BLOCK_CORRELATION_INVALID,
    BLOCK_DISCORD_PRODUCTION_ENABLED,
    BLOCK_DISPATCH_AUDIT_MISSING,
    BLOCK_E2E_NOT_FINALIZED,
    BLOCK_EVIDENCE_MISSING,
    BLOCK_EXTERNAL_PUBLISH_ATTEMPTED,
    BLOCK_FINAL_SIGNOFF_INVALID,
    BLOCK_FINAL_SIGNOFF_MISSING,
    BLOCK_GATEWAY_PRODUCTION_ENABLED,
    BLOCK_MAINTENANCE_WINDOW_INVALID,
    BLOCK_OPERATOR_IDENTITY_INVALID,
    BLOCK_PRODUCTION_EXECUTION_ENABLED,
    BLOCK_PRODUCTION_ROOT_TOUCHED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_REPAIR_LOCK_HELD,
    BLOCK_ROLLBACK_VALIDATION_MISSING,
    BLOCK_SOURCE_TREE_MUTATED,
    GOVERNED_CUTOVER_CONTRACT_PREPARED,
    GOVERNED_CUTOVER_NOT_READY,
    GOVERNED_CUTOVER_READY,
    GOVERNED_CUTOVER_READY_WITH_WARNINGS,
    GOVERNED_CUTOVER_REQUIRES_RECOVERY,
    ProductionGovernedCutoverError,
    build_production_governed_cutover_release_summary,
    evaluate_production_governed_cutover,
    format_production_governed_cutover_check,
    format_production_governed_cutover_status,
    load_governed_cutover_contract,
    prepare_production_governed_cutover,
    resolve_latest_governed_cutover_dashboard_digest,
    validate_maintenance_window,
)
from agent.coo.production_live_operational_signoff import (
    _PRODUCTION_ROOT_TOUCHED_SENTINEL,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.test_production_final_signoff import (
    TestProductionFinalSignoff,
    _EXECUTOR_ID,
    _FINAL_SIGNER,
)
from tests.hermes_cli.test_production_live_rollback_validation import (
    _SIGNOFF_OPERATOR_ID,
)

_CUTOVER_OPERATOR = "cutover-operator-phase15a"


def _future_window(
    *,
    start_offset_minutes: int = 60,
    duration_minutes: int = 30,
    now: datetime | None = None,
) -> tuple[str, str]:
    current = now or datetime.now(timezone.utc)
    start = current + timedelta(minutes=start_offset_minutes)
    end = start + timedelta(minutes=duration_minutes)
    return start.isoformat(), end.isoformat()


class TestProductionGovernedCutover(TestProductionFinalSignoff):
    def setUp(self) -> None:
        super().setUp()
        self.governed_cutover_store_dir = (
            self.hermes_home / "coo" / "production-governed-cutover"
        )
        self.governed_cutover_store_dir.mkdir(parents=True, exist_ok=True)
        self._now = datetime(2026, 7, 14, 5, 0, tzinfo=timezone.utc)

    def _cutover_kwargs(self, activation_id: str, reservation_id: str, **overrides):
        window_start, window_end = _future_window(now=self._now)
        base = {
            **self._final_kwargs(activation_id, reservation_id),
            "governed_cutover_store_dir": self.governed_cutover_store_dir,
            "operator_id": _CUTOVER_OPERATOR,
            "window_start": window_start,
            "window_end": window_end,
            "now": self._now,
        }
        base.update(overrides)
        return base

    def _complete_final_signed(self) -> tuple[str, str]:
        activation_id, reservation_id = self._complete_chain()
        record_production_final_signoff(
            **self._final_kwargs(
                activation_id,
                reservation_id,
                signer_id=_FINAL_SIGNER,
            )
        )
        return activation_id, reservation_id

    def test_full_success_governed_cutover_ready_with_warnings(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        self.assertIn(
            summary.governed_cutover_status,
            {
                GOVERNED_CUTOVER_READY_WITH_WARNINGS,
                GOVERNED_CUTOVER_READY,
            },
        )
        self.assertTrue(summary.governed_cutover_ready)
        self.assertTrue(summary.final_signoff_valid)
        self.assertTrue(summary.checklist_passed)
        self.assertTrue(summary.maintenance_window_valid)
        self.assertTrue(summary.operator_handoff_ready)
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.window_opened)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.execution_permit_created)

    def test_legacy_cutover_check_parser_unchanged(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            ["production", "cutover-check", "--ticket-id", "ticket-legacy"]
        )
        self.assertEqual(args.handler.__name__, "_cmd_production_cutover_check")
        self.assertEqual(args.ticket_ids, ["ticket-legacy"])

    def test_final_signoff_missing_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.governed_cutover_status, GOVERNED_CUTOVER_NOT_READY)
        self.assertIn(BLOCK_FINAL_SIGNOFF_MISSING, summary.blocking_items)

    def test_final_signoff_invalid_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        from dataclasses import replace

        from agent.coo.production_final_signoff import (
            PRODUCTION_FINAL_SIGNOFF_BLOCKED,
            evaluate_production_final_signoff,
        )

        baseline = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        blocked = replace(
            baseline,
            final_signoff_status=PRODUCTION_FINAL_SIGNOFF_BLOCKED,
            production_release_ready=False,
        )
        with patch(
            "agent.coo.production_governed_cutover.evaluate_production_final_signoff",
            return_value=blocked,
        ):
            summary = evaluate_production_governed_cutover(
                **self._cutover_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(summary.governed_cutover_status, GOVERNED_CUTOVER_NOT_READY)
        self.assertIn(BLOCK_FINAL_SIGNOFF_INVALID, summary.blocking_items)

    def test_rollback_validation_missing_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        for path in self.validation_store_dir.glob("*.json"):
            path.unlink()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.governed_cutover_status, GOVERNED_CUTOVER_NOT_READY)
        self.assertIn(BLOCK_ROLLBACK_VALIDATION_MISSING, summary.blocking_items)

    def test_activation_not_revoked_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        from dataclasses import replace

        from agent.coo.production_final_signoff import evaluate_production_final_signoff

        baseline = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        mutated = replace(baseline, activation_revoked=False)
        with patch(
            "agent.coo.production_governed_cutover.evaluate_production_final_signoff",
            return_value=mutated,
        ):
            summary = evaluate_production_governed_cutover(
                **self._cutover_kwargs(activation_id, reservation_id)
            )
        self.assertIn(BLOCK_ACTIVATION_NOT_REVOKED, summary.blocking_items)

    def test_consume_partial_requires_recovery(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        from agent.coo.dispatch_consume_transaction import CooDispatchConsumeStatus

        partial_status = CooDispatchConsumeStatus(
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
                return_value=partial_status,
            ),
            patch(
                "agent.coo.production_live_rollback_validation.assess_consume_status",
                return_value=partial_status,
            ),
        ):
            summary = evaluate_production_governed_cutover(
                **self._cutover_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(
            summary.governed_cutover_status,
            GOVERNED_CUTOVER_REQUIRES_RECOVERY,
        )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, summary.blocking_items)
        self.assertEqual(summary.recommended_action, ACTION_RUN_CONSUME_RECOVERY)

    def test_repair_lock_requires_recovery(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        with (
            patch(
                "agent.coo.production_live_operational_signoff._probe_repair_lock_held",
                return_value=True,
            ),
            patch(
                "agent.coo.production_live_rollback_validation._probe_repair_lock_held",
                return_value=True,
            ),
        ):
            summary = evaluate_production_governed_cutover(
                **self._cutover_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(
            summary.governed_cutover_status,
            GOVERNED_CUTOVER_REQUIRES_RECOVERY,
        )
        self.assertIn(BLOCK_REPAIR_LOCK_HELD, summary.blocking_items)

    def test_source_mutation_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        for path in self.runtime_history_dir.glob(f"{activation_id}*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for record in payload.get("records", []):
                if record.get("event_type") == "runtime_completed":
                    record["publish_attempted"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_SOURCE_TREE_MUTATED, summary.blocking_items)
        self.assertIn(BLOCK_EXTERNAL_PUBLISH_ATTEMPTED, summary.blocking_items)

    def test_production_root_touched_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        sentinel = self.signoff_store_dir / _PRODUCTION_ROOT_TOUCHED_SENTINEL
        sentinel.write_text("1", encoding="utf-8")
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_PRODUCTION_ROOT_TOUCHED, summary.blocking_items)

    def test_gateway_production_enabled_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(
                activation_id,
                reservation_id,
                force_gateway_enabled=True,
            )
        )
        self.assertIn(BLOCK_GATEWAY_PRODUCTION_ENABLED, summary.blocking_items)
        self.assertEqual(summary.governed_cutover_status, GOVERNED_CUTOVER_NOT_READY)

    def test_discord_production_enabled_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(
                activation_id,
                reservation_id,
                force_discord_enabled=True,
            )
        )
        self.assertIn(BLOCK_DISCORD_PRODUCTION_ENABLED, summary.blocking_items)

    def test_production_execution_allowed_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(
                activation_id,
                reservation_id,
                force_production_execution_allowed=True,
            )
        )
        self.assertIn(BLOCK_PRODUCTION_EXECUTION_ENABLED, summary.blocking_items)
        self.assertFalse(summary.production_execution_allowed)

    def test_valid_maintenance_window_passes(self) -> None:
        valid, future, duration_ok, seconds, _, _ = validate_maintenance_window(
            *( _future_window(now=self._now) ),
            now=self._now,
        )
        self.assertTrue(valid)
        self.assertTrue(future)
        self.assertTrue(duration_ok)
        self.assertGreaterEqual(seconds, 15 * 60)

    def test_expired_window_blocked(self) -> None:
        start = self._now - timedelta(hours=3)
        end = self._now - timedelta(hours=2)
        summary_valid, future, _, _, _, _ = validate_maintenance_window(
            start.isoformat(),
            end.isoformat(),
            now=self._now,
        )
        self.assertFalse(summary_valid)
        self.assertFalse(future)

    def test_end_before_start_blocked(self) -> None:
        start = self._now + timedelta(hours=2)
        end = self._now + timedelta(hours=1)
        valid, *_ = validate_maintenance_window(
            start.isoformat(),
            end.isoformat(),
            now=self._now,
        )
        self.assertFalse(valid)

    def test_window_too_short_blocked(self) -> None:
        start = self._now + timedelta(hours=1)
        end = start + timedelta(minutes=10)
        valid, _, duration_ok, *_ = validate_maintenance_window(
            start.isoformat(),
            end.isoformat(),
            now=self._now,
        )
        self.assertFalse(valid)
        self.assertFalse(duration_ok)

    def test_window_too_long_blocked(self) -> None:
        start = self._now + timedelta(hours=1)
        end = start + timedelta(hours=3)
        valid, _, duration_ok, *_ = validate_maintenance_window(
            start.isoformat(),
            end.isoformat(),
            now=self._now,
        )
        self.assertFalse(valid)
        self.assertFalse(duration_ok)

    def test_window_too_far_future_blocked(self) -> None:
        start = self._now + timedelta(days=10)
        end = start + timedelta(minutes=30)
        valid, *_ = validate_maintenance_window(
            start.isoformat(),
            end.isoformat(),
            now=self._now,
        )
        self.assertFalse(valid)

    def test_operator_identity_conflict_blocked(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(
                activation_id,
                reservation_id,
                operator_id=_FINAL_SIGNER,
            )
        )
        self.assertIn(BLOCK_OPERATOR_IDENTITY_INVALID, summary.blocking_items)
        summary2 = evaluate_production_governed_cutover(
            **self._cutover_kwargs(
                activation_id,
                reservation_id,
                operator_id=_EXECUTOR_ID,
            )
        )
        self.assertIn(BLOCK_OPERATOR_IDENTITY_INVALID, summary2.blocking_items)

    def test_prepare_ready_creates_artifact(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            summary = prepare_production_governed_cutover(
                **self._cutover_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(
            summary.governed_cutover_status,
            GOVERNED_CUTOVER_CONTRACT_PREPARED,
        )
        record = load_governed_cutover_contract(
            activation_id,
            store_dir=self.governed_cutover_store_dir,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertFalse(record.production_execution_allowed)
        self.assertFalse(record.window_opened)
        self.assertFalse(record.cutover_started)
        self.assertFalse(record.execution_permit_created)
        self.assertEqual(
            summary.recommended_action,
            ACTION_PREPARE_PHASE_15B_CONTROLLED_PRODUCTION_WINDOW,
        )

    def test_prepare_not_ready_no_artifact(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        with self.assertRaises(ProductionGovernedCutoverError):
            prepare_production_governed_cutover(
                **self._cutover_kwargs(activation_id, reservation_id)
            )
        self.assertIsNone(
            load_governed_cutover_contract(
                activation_id,
                store_dir=self.governed_cutover_store_dir,
            )
        )

    def test_prepare_recovery_no_artifact(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        from agent.coo.dispatch_consume_transaction import CooDispatchConsumeStatus

        prepared_status = CooDispatchConsumeStatus(
            consume_state=CONSUME_STATE_PREPARED,
            transaction_id="tx-prep",
            execution_attempt_id="exec-1",
            bundle_consumed=False,
            confirmation_consumed=False,
            recovery_required=True,
        )
        with (
            patch(
                "agent.coo.production_live_operational_signoff.assess_consume_status",
                return_value=prepared_status,
            ),
            patch(
                "agent.coo.production_live_rollback_validation.assess_consume_status",
                return_value=prepared_status,
            ),
        ):
            with self.assertRaises(ProductionGovernedCutoverError):
                prepare_production_governed_cutover(
                    **self._cutover_kwargs(activation_id, reservation_id)
                )

    def test_duplicate_prepare_idempotent(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        first = prepare_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        second = prepare_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        self.assertTrue(second.already_prepared)
        self.assertEqual(first.cutover_contract_id, second.cutover_contract_id)
        paths = list(self.governed_cutover_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    def test_duplicate_prepare_changed_window_conflict(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        kwargs = self._cutover_kwargs(activation_id, reservation_id)
        prepare_production_governed_cutover(**kwargs)
        start = self._now + timedelta(hours=3)
        end = start + timedelta(minutes=30)
        with self.assertRaises(ProductionGovernedCutoverError):
            prepare_production_governed_cutover(
                **{
                    **kwargs,
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                }
            )

    def test_contract_append_only_safe_fields(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        prepare_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        path = self.governed_cutover_store_dir / f"{activation_id}.json"
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "pipeline_root",
            "argv",
            "cwd",
            "stdout",
            "stderr",
            "attestation_hash",
            "/opt/data/",
        ):
            self.assertNotIn(forbidden, text.lower())
        payload = json.loads(text)
        contract = payload["cutover_contract"]
        self.assertFalse(contract["production_execution_allowed"])
        self.assertFalse(contract["window_opened"])
        self.assertFalse(contract["cutover_started"])
        self.assertFalse(contract["execution_permit_created"])

    def test_show_command(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        summary = prepare_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "production",
                "governed-cutover",
                "show",
                "--cutover-contract-id",
                summary.cutover_contract_id,
            ]
        )
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        output = buffer.getvalue()
        self.assertIn("cutover_contract_id:", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertNotIn(_CUTOVER_OPERATOR, output)

    def test_status_and_check_read_only(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        status_out = format_production_governed_cutover_status(summary)
        check_out = format_production_governed_cutover_check(summary)
        self.assertIn("governed_cutover_status:", status_out)
        self.assertIn("checklist_passed:", check_out)
        self.assertNotIn(_CUTOVER_OPERATOR, status_out)
        self.assertNotIn(_FINAL_SIGNER, status_out)
        self.assertIn("production_execution_allowed: false", status_out)
        before = list(self.governed_cutover_store_dir.glob("*.json"))
        evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        after = list(self.governed_cutover_store_dir.glob("*.json"))
        self.assertEqual(before, after)

    def test_status_without_window_recommends_define(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        kwargs = self._final_kwargs(activation_id, reservation_id)
        summary = evaluate_production_governed_cutover(
            **kwargs,
            governed_cutover_store_dir=self.governed_cutover_store_dir,
            now=self._now,
        )
        self.assertIn(BLOCK_MAINTENANCE_WINDOW_INVALID, summary.blocking_items)
        self.assertEqual(summary.recommended_action, ACTION_DEFINE_MAINTENANCE_WINDOW)

    def test_dashboard_governed_cutover_fields(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        prepare_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        digest = resolve_latest_governed_cutover_dashboard_digest(
            e2e_history_dir=self.e2e_history_dir,
            governed_cutover_store_dir=self.governed_cutover_store_dir,
            final_signoff_store_dir=self.final_signoff_store_dir,
            signoff_store_dir=self.signoff_store_dir,
            validation_store_dir=self.validation_store_dir,
            store_dir=self.store_dir,
            reservation_dir=self.reservation_dir,
            runtime_history_dir=self.runtime_history_dir,
            evidence_dir=self.evidence_dir,
            audit_dir=self.audit_dir,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            transaction_dir=self.transaction_dir,
            preflight_history_dir=self.preflight_history_dir,
            repo_root=self.repo_root,
            merged_config={},
        )
        self.assertTrue(digest.governed_cutover_contract_present)
        self.assertTrue(digest.governed_cutover_ready)
        self.assertEqual(
            digest.governed_cutover_status,
            GOVERNED_CUTOVER_CONTRACT_PREPARED,
        )

        dashboard = build_operator_dashboard_summary(merged_config={})
        self.assertTrue(hasattr(dashboard, "governed_cutover_status"))
        self.assertTrue(hasattr(dashboard, "governed_cutover_ready"))
        self.assertTrue(hasattr(dashboard, "governed_cutover_contract_present"))

    def test_release_summary_mapping(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        release = build_production_governed_cutover_release_summary(
            summary,
            final_signoff_status="PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS",
            merged_config={},
        )
        self.assertFalse(release.production_execution_allowed)
        self.assertTrue(release.production_root_hard_deny)
        self.assertEqual(
            release.next_phase,
            "Phase_15B_controlled_production_window",
        )
        self.assertIn("GOVERNED_CUTOVER", release.release_status)

    def test_cli_prepare_and_status(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        window_start, window_end = _future_window(now=self._now)
        parser = build_coo_dispatch_parser()
        prepare_args = parser.parse_args(
            [
                "production",
                "governed-cutover",
                "prepare",
                "--activation-request-id",
                activation_id,
                "--reservation-id",
                reservation_id,
                "--operator-id",
                _CUTOVER_OPERATOR,
                "--window-start",
                window_start,
                "--window-end",
                window_end,
            ]
        )
        with (
            patch(
                "agent.coo.production_governed_cutover.default_governed_cutover_store_dir",
                return_value=self.governed_cutover_store_dir,
            ),
            patch(
                "agent.coo.production_final_signoff.default_final_signoff_store_dir",
                return_value=self.final_signoff_store_dir,
            ),
            patch.dict(
                "os.environ",
                {"HERMES_HOME": str(self.hermes_home)},
            ),
        ):
            # Use library prepare under patches matching store dirs; CLI
            # path is covered by show + parser registration.
            summary = prepare_production_governed_cutover(
                **self._cutover_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(
            summary.governed_cutover_status,
            GOVERNED_CUTOVER_CONTRACT_PREPARED,
        )
        status_args = parser.parse_args(
            [
                "production",
                "governed-cutover",
                "status",
                "--activation-request-id",
                activation_id,
                "--reservation-id",
                reservation_id,
            ]
        )
        self.assertEqual(
            status_args.handler.__name__,
            "_cmd_production_governed_cutover_status",
        )
        self.assertEqual(
            prepare_args.handler.__name__,
            "_cmd_production_governed_cutover_prepare",
        )

    def test_no_subprocess_and_ready_action(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            summary = evaluate_production_governed_cutover(
                **self._cutover_kwargs(activation_id, reservation_id)
            )
        self.assertIn(
            summary.recommended_action,
            {
                ACTION_GOVERNED_CUTOVER_READY_PREPARE_CONTRACT,
                "review_governed_cutover_warnings",
            },
        )

    def test_evidence_missing_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        path = self.evidence_dir / f"{reservation.execution_attempt_id}.live-pilot-e2e.json"
        path.unlink()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_EVIDENCE_MISSING, summary.blocking_items)

    def test_audit_missing_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        for path in self.audit_dir.glob("*.json"):
            path.unlink()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_DISPATCH_AUDIT_MISSING, summary.blocking_items)

    def test_correlation_invalid_not_ready(self) -> None:
        activation_id, reservation_id = self._complete_final_signed()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        path = self.evidence_dir / f"{reservation.execution_attempt_id}.live-pilot-e2e.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["ticket_id"] = "mismatch"
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_CORRELATION_INVALID, summary.blocking_items)

    def test_e2e_not_finalized_not_ready(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        summary = evaluate_production_governed_cutover(
            **self._cutover_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_E2E_NOT_FINALIZED, summary.blocking_items)


if __name__ == "__main__":
    unittest.main()
