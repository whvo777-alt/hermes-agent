"""Phase 14J tests — production final sign-off gate."""

from __future__ import annotations

import json
import subprocess
import unittest
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
from agent.coo.production_activation_state import ACTIVATION_STATE_SUSPENDED
from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_final_signoff import (
    BLOCK_ACTIVATION_NOT_REVOKED,
    BLOCK_ARTIFACT_CORRUPTED,
    BLOCK_AUDIT_CHAIN_INCOMPLETE,
    BLOCK_CONSUME_NOT_COMMITTED,
    BLOCK_CORRELATION_INVALID,
    BLOCK_DISPATCH_AUDIT_MISSING,
    BLOCK_E2E_NOT_FINALIZED,
    BLOCK_EVIDENCE_MISSING,
    BLOCK_EXTERNAL_PUBLISH_ATTEMPTED,
    BLOCK_OPERATIONAL_SIGNOFF_INVALID,
    BLOCK_OPERATIONAL_SIGNOFF_MISSING,
    BLOCK_PRODUCTION_ROOT_TOUCHED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_REPAIR_LOCK_HELD,
    BLOCK_RESERVATION_NOT_COMPLETED,
    BLOCK_ROLLBACK_VALIDATION_INVALID,
    BLOCK_ROLLBACK_VALIDATION_MISSING,
    BLOCK_RUNTIME_FAILED,
    BLOCK_RUNTIME_NOT_COMPLETED,
    BLOCK_SIGNER_IDENTITY_CONFLICT,
    BLOCK_SOURCE_TREE_MUTATED,
    PRODUCTION_FINAL_SIGNOFF_BLOCKED,
    PRODUCTION_FINAL_SIGNOFF_READY,
    PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
    PRODUCTION_FINAL_SIGNOFF_REQUIRES_RECOVERY,
    RELEASE_GOVERNED_CANDIDATE_READY,
    RELEASE_GOVERNED_CANDIDATE_READY_WITH_WARNINGS,
    ProductionFinalSignoffError,
    build_production_final_release_summary,
    default_final_signoff_store_dir,
    evaluate_production_final_signoff,
    format_production_final_signoff_status,
    load_final_signoff_record,
    record_production_final_signoff,
    resolve_latest_final_signoff_dashboard_digest,
)
from agent.coo.production_live_operational_signoff import (
    _PRODUCTION_ROOT_TOUCHED_SENTINEL,
)
from agent.coo.production_live_rollback_validation import (
    record_production_live_rollback_validation,
)
from tests.hermes_cli.test_production_live_rollback_validation import (
    TestProductionLiveRollbackValidation,
    _SIGNOFF_OPERATOR_ID,
)

_FINAL_SIGNER = "release-approver-final"
_EXECUTOR_ID = "executor-e"


class TestProductionFinalSignoff(TestProductionLiveRollbackValidation):
    def setUp(self) -> None:
        super().setUp()
        self.final_signoff_store_dir = (
            self.hermes_home / "coo" / "production-final-signoff"
        )
        self.final_signoff_store_dir.mkdir(parents=True, exist_ok=True)
        self.preflight_history_dir = (
            self.hermes_home / "coo" / "production-activation-execution-preflight"
        )

    def _final_kwargs(self, activation_id: str, reservation_id: str, **overrides):
        base = {
            **self._rollback_kwargs(activation_id, reservation_id),
            "final_signoff_store_dir": self.final_signoff_store_dir,
            "preflight_history_dir": self.preflight_history_dir,
        }
        base.update(overrides)
        return base

    def _complete_chain(self) -> tuple[str, str]:
        activation_id, reservation_id = self._full_success_chain()
        record_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        return activation_id, reservation_id

    def test_full_success_final_signoff_ready_with_warnings(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertIn(
            summary.final_signoff_status,
            {
                PRODUCTION_FINAL_SIGNOFF_READY_WITH_WARNINGS,
                PRODUCTION_FINAL_SIGNOFF_READY,
            },
        )
        self.assertTrue(summary.production_release_ready)
        self.assertTrue(summary.operational_signoff_valid)
        self.assertTrue(summary.rollback_validation_valid)
        self.assertTrue(summary.audit_chain_complete)
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.gateway_production_enabled)

    def test_operational_signoff_missing_blocked(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        record_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.final_signoff_status, PRODUCTION_FINAL_SIGNOFF_BLOCKED)
        self.assertIn(BLOCK_OPERATIONAL_SIGNOFF_MISSING, summary.blocking_items)

    def test_rollback_validation_missing_blocked(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.final_signoff_status, PRODUCTION_FINAL_SIGNOFF_BLOCKED)
        self.assertIn(BLOCK_ROLLBACK_VALIDATION_MISSING, summary.blocking_items)

    def test_rollback_validation_invalid_blocked(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        from agent.coo.production_live_rollback_validation import (
            ROLLBACK_NOT_READY,
            ProductionLiveRollbackValidationSummary,
        )

        rb_summary = ProductionLiveRollbackValidationSummary(
            activation_request_id=activation_id,
            reservation_id=reservation_id,
            execution_attempt_id="exec-placeholder",
            dispatch_run_id="run-placeholder",
            validation_status=ROLLBACK_NOT_READY,
            chain_complete=False,
            activation_valid=True,
            reservation_valid=True,
            runtime_valid=True,
            evidence_valid=True,
            dispatch_audit_valid=True,
            consume_valid=True,
            signoff_valid=True,
            tested_commit_present=True,
            tested_commit_matches=True,
            release_tag_present=True,
            release_tag_matches_tested_commit=True,
            rollback_commit_present=True,
            rollback_commit_valid=False,
            rollback_commit_distinct=True,
            rollback_path_available=False,
            production_root_untouched=True,
            isolated_mirror_only=True,
            source_tree_unchanged=True,
            output_artifacts_identifiable=True,
            external_publish_attempted=False,
            recovery_required=False,
            repair_lock_held=False,
            rollback_ready=False,
            blocking_items=(BLOCK_ROLLBACK_VALIDATION_INVALID,),
            warning_items=(),
            recommended_action="maintain_production_block",
        )
        with patch(
            "agent.coo.production_final_signoff.evaluate_production_live_rollback_validation",
            return_value=rb_summary,
        ):
            summary = evaluate_production_final_signoff(
                **self._final_kwargs(activation_id, reservation_id)
            )
        self.assertIn(BLOCK_ROLLBACK_VALIDATION_INVALID, summary.blocking_items)
        self.assertEqual(summary.final_signoff_status, PRODUCTION_FINAL_SIGNOFF_BLOCKED)

    def test_activation_not_revoked_blocked(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        from dataclasses import replace

        from agent.coo.production_live_operational_signoff import (
            ProductionLiveOperationalSignoffSummary,
        )

        baseline = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        op_summary = ProductionLiveOperationalSignoffSummary(
            activation_request_id=baseline.activation_request_id,
            reservation_id=baseline.reservation_id,
            execution_attempt_id=baseline.execution_attempt_id,
            dispatch_run_id=baseline.dispatch_run_id,
            signoff_status="SIGNOFF_READY_WITH_WARNINGS",
            first_run_detected=True,
            runtime_completed=True,
            runtime_exit_code=0,
            runtime_timed_out=False,
            source_tree_unchanged=True,
            publish_attempted=False,
            evidence_present=True,
            dispatch_audit_present=True,
            evidence_audit_correlation_valid=True,
            consume_state="committed",
            consume_committed=True,
            activation_state=ACTIVATION_STATE_SUSPENDED,
            activation_revoked=False,
            reservation_state="completed",
            e2e_finalized=True,
            recovery_required=False,
            repair_lock_held=False,
            production_root_untouched=True,
            isolated_mirror_only=True,
            draft_only=True,
            external_publish_attempted=False,
            rollback_ready=True,
            operator_checklist_passed=True,
            blocking_items=(),
            warning_items=(),
            recommended_action="maintain_production_block",
        )
        with patch(
            "agent.coo.production_final_signoff.evaluate_production_live_operational_signoff",
            return_value=op_summary,
        ):
            summary = evaluate_production_final_signoff(
                **self._final_kwargs(activation_id, reservation_id)
            )
        self.assertIn(BLOCK_ACTIVATION_NOT_REVOKED, summary.blocking_items)

    def test_runtime_failed_blocked(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        for path in self.runtime_history_dir.glob(f"{activation_id}*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for record in payload.get("records", []):
                if record.get("event_type") == "runtime_completed":
                    record["exit_code"] = 2
            path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_RUNTIME_FAILED, summary.blocking_items)

    def test_evidence_missing_blocked(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        path = self.evidence_dir / f"{reservation.execution_attempt_id}.live-pilot-e2e.json"
        path.unlink()
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_EVIDENCE_MISSING, summary.blocking_items)

    def test_audit_missing_blocked(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        for path in self.audit_dir.glob("*.json"):
            path.unlink()
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_DISPATCH_AUDIT_MISSING, summary.blocking_items)

    def test_correlation_invalid_blocked(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        path = self.evidence_dir / f"{reservation.execution_attempt_id}.live-pilot-e2e.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["ticket_id"] = "mismatch"
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_CORRELATION_INVALID, summary.blocking_items)

    def test_consume_partial_requires_recovery(self) -> None:
        activation_id, reservation_id = self._complete_chain()
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
            summary = evaluate_production_final_signoff(
                **self._final_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(
            summary.final_signoff_status,
            PRODUCTION_FINAL_SIGNOFF_REQUIRES_RECOVERY,
        )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, summary.blocking_items)

    def test_repair_lock_requires_recovery(self) -> None:
        activation_id, reservation_id = self._complete_chain()
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
            summary = evaluate_production_final_signoff(
                **self._final_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(
            summary.final_signoff_status,
            PRODUCTION_FINAL_SIGNOFF_REQUIRES_RECOVERY,
        )
        self.assertIn(BLOCK_REPAIR_LOCK_HELD, summary.blocking_items)

    def test_e2e_not_finalized_blocked(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_E2E_NOT_FINALIZED, summary.blocking_items)

    def test_source_mutation_blocked(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        for path in self.runtime_history_dir.glob(f"{activation_id}*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for record in payload.get("records", []):
                if record.get("event_type") == "runtime_completed":
                    record["publish_attempted"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_SOURCE_TREE_MUTATED, summary.blocking_items)
        self.assertIn(BLOCK_EXTERNAL_PUBLISH_ATTEMPTED, summary.blocking_items)

    def test_production_root_touched_blocked(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        sentinel = self.signoff_store_dir / _PRODUCTION_ROOT_TOUCHED_SENTINEL
        sentinel.write_text("1", encoding="utf-8")
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_PRODUCTION_ROOT_TOUCHED, summary.blocking_items)

    def test_audit_chain_incomplete_blocked(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        e2e_path = self.e2e_history_dir / f"{activation_id}.json"
        payload = json.loads(e2e_path.read_text(encoding="utf-8"))
        payload["records"] = [
            record
            for record in payload.get("records", [])
            if record.get("event_type") != "e2e_completed"
        ]
        e2e_path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_AUDIT_CHAIN_INCOMPLETE, summary.blocking_items)

    def test_final_signoff_artifact_append_only(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            summary = record_production_final_signoff(
                **self._final_kwargs(
                    activation_id,
                    reservation_id,
                    signer_id=_FINAL_SIGNER,
                )
            )
        record = load_final_signoff_record(
            activation_id,
            store_dir=self.final_signoff_store_dir,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.final_signoff_status, summary.final_signoff_status)
        self.assertFalse(record.production_execution_allowed)
        self.assertFalse(record.gateway_production_enabled)

    def test_duplicate_final_signoff_idempotent(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        record_production_final_signoff(
            **self._final_kwargs(
                activation_id,
                reservation_id,
                signer_id=_FINAL_SIGNER,
            )
        )
        second = record_production_final_signoff(
            **self._final_kwargs(
                activation_id,
                reservation_id,
                signer_id=_FINAL_SIGNER,
            )
        )
        self.assertTrue(second.already_final_signed)

    def test_mismatched_duplicate_corruption(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        record_production_final_signoff(
            **self._final_kwargs(
                activation_id,
                reservation_id,
                signer_id=_FINAL_SIGNER,
            )
        )
        path = self.final_signoff_store_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["final_signoff"]["reservation_id"] = "other"
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_ARTIFACT_CORRUPTED, summary.blocking_items)
        with self.assertRaises(ProductionFinalSignoffError):
            record_production_final_signoff(
                **self._final_kwargs(
                    activation_id,
                    reservation_id,
                    signer_id=_FINAL_SIGNER,
                )
            )

    def test_recovery_does_not_write_final_signoff(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        from agent.coo.dispatch_consume_transaction import CooDispatchConsumeStatus

        prepared_status = CooDispatchConsumeStatus(
            consume_state=CONSUME_STATE_PREPARED,
            transaction_id="tx-partial",
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
            with self.assertRaises(ProductionFinalSignoffError):
                record_production_final_signoff(
                    **self._final_kwargs(
                        activation_id,
                        reservation_id,
                        signer_id=_FINAL_SIGNER,
                    )
                )

    def test_signer_identity_conflict_blocked(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        with self.assertRaises(ProductionFinalSignoffError):
            record_production_final_signoff(
                **self._final_kwargs(
                    activation_id,
                    reservation_id,
                    signer_id=_SIGNOFF_OPERATOR_ID,
                )
            )
        with self.assertRaises(ProductionFinalSignoffError):
            record_production_final_signoff(
                **self._final_kwargs(
                    activation_id,
                    reservation_id,
                    signer_id=_EXECUTOR_ID,
                )
            )

    def test_final_signoff_does_not_enable_execution(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        summary = record_production_final_signoff(
            **self._final_kwargs(
                activation_id,
                reservation_id,
                signer_id=_FINAL_SIGNER,
            )
        )
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.gateway_production_enabled)
        self.assertFalse(summary.discord_production_enabled)

    def test_dashboard_final_signoff_fields(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        record_production_final_signoff(
            **self._final_kwargs(
                activation_id,
                reservation_id,
                signer_id=_FINAL_SIGNER,
            )
        )
        digest = resolve_latest_final_signoff_dashboard_digest(
            e2e_history_dir=self.e2e_history_dir,
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
            merged_config=self.merged_config,
        )
        self.assertTrue(digest.production_final_signoff_present)
        dashboard = build_operator_dashboard_summary(merged_config=self.merged_config)
        self.assertTrue(hasattr(dashboard, "production_final_signoff_status"))

    def test_release_summary_status_mapping(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        release = build_production_final_release_summary(
            summary,
            request=request,
            merged_config=self.merged_config,
        )
        self.assertIn(
            release.release_status,
            {
                RELEASE_GOVERNED_CANDIDATE_READY,
                RELEASE_GOVERNED_CANDIDATE_READY_WITH_WARNINGS,
            },
        )
        self.assertFalse(release.gateway_production_enabled)
        self.assertTrue(release.production_root_hard_deny)

    def test_status_cli_read_only(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        from hermes_cli.coo_dispatch import build_coo_dispatch_parser

        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "production",
                "final-signoff-status",
                "--activation-request-id",
                activation_id,
                "--reservation-id",
                reservation_id,
            ]
        )
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            exit_code = args.handler(args)
        self.assertIn(exit_code, {0, 1})

    def test_safe_output_forbidden_fields(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        summary = evaluate_production_final_signoff(
            **self._final_kwargs(activation_id, reservation_id)
        )
        output = format_production_final_signoff_status(summary)
        for token in (
            "pipeline_root",
            "argv",
            "signed_by",
            "/opt/data/",
        ):
            self.assertNotIn(token, output.lower())

    def test_no_subprocess(self) -> None:
        activation_id, reservation_id = self._complete_chain()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            summary = evaluate_production_final_signoff(
                **self._final_kwargs(activation_id, reservation_id)
            )
        self.assertTrue(summary.production_release_ready)


if __name__ == "__main__":
    unittest.main()
