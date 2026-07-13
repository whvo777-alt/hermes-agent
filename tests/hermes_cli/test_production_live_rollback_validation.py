"""Phase 14I tests — production live rollback validation."""

from __future__ import annotations

import json
import subprocess
import unittest
from dataclasses import replace
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
from agent.coo.production_activation_live_e2e import finalize_production_live_pilot
from agent.coo.production_activation_state import ACTIVATION_STATE_SUSPENDED
from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_live_operational_signoff import (
    _PRODUCTION_ROOT_TOUCHED_SENTINEL,
    record_production_live_operational_signoff,
)
from agent.coo.production_live_rollback_validation import (
    BLOCK_ACTIVATION_NOT_REVOKED,
    BLOCK_ARTIFACT_CORRUPTED,
    BLOCK_CONSUME_NOT_COMMITTED,
    BLOCK_CORRELATION_MISMATCH,
    BLOCK_E2E_FINALIZATION_MISSING,
    BLOCK_EXTERNAL_PUBLISH_ATTEMPTED,
    BLOCK_PRODUCTION_ROOT_TOUCHED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_RELEASE_TAG_MISMATCH,
    BLOCK_RELEASE_TAG_MISSING,
    BLOCK_REPAIR_LOCK_HELD,
    BLOCK_ROLLBACK_COMMIT_EQUALS_TESTED_COMMIT,
    BLOCK_ROLLBACK_COMMIT_INVALID,
    BLOCK_ROLLBACK_COMMIT_MISSING,
    BLOCK_SIGNOFF_MISSING,
    BLOCK_SOURCE_TREE_MUTATED,
    BLOCK_TESTED_COMMIT_MISMATCH,
    BLOCK_UNEXPECTED_ARTIFACTS,
    ROLLBACK_NOT_READY,
    ROLLBACK_READY,
    ROLLBACK_READY_WITH_WARNINGS,
    ROLLBACK_REQUIRES_RECOVERY,
    ProductionLiveRollbackValidationError,
    default_rollback_validation_store_dir,
    evaluate_production_live_rollback_validation,
    format_production_live_rollback_check,
    load_rollback_validation_record,
    record_production_live_rollback_validation,
    resolve_latest_rollback_dashboard_digest,
)
from tests.hermes_cli.test_production_activation_live_runtime import (
    _ROLLBACK_SHA,
    _TESTED_SHA,
)
from tests.hermes_cli.test_production_live_operational_signoff import (
    TestProductionLiveOperationalSignoff,
    _SIGNOFF_OPERATOR,
)

_SIGNOFF_OPERATOR_ID = _SIGNOFF_OPERATOR


def _seed_git_refs(repo_root: Path) -> None:
    tags_dir = repo_root / ".git" / "refs" / "tags"
    tags_dir.mkdir(parents=True, exist_ok=True)
    (tags_dir / "v1.0.0-rc.1").write_text(f"{_TESTED_SHA}\n", encoding="utf-8")
    heads_dir = repo_root / ".git" / "refs" / "heads"
    heads_dir.mkdir(parents=True, exist_ok=True)
    (heads_dir / "rollback").write_text(f"{_ROLLBACK_SHA}\n", encoding="utf-8")


class TestProductionLiveRollbackValidation(TestProductionLiveOperationalSignoff):
    def setUp(self) -> None:
        super().setUp()
        self.validation_store_dir = (
            self.hermes_home / "coo" / "production-live-rollback-validation"
        )
        self.validation_store_dir.mkdir(parents=True, exist_ok=True)
        _seed_git_refs(self.repo_root)

    def _rollback_kwargs(self, activation_id: str, reservation_id: str, **overrides):
        base = {
            **self._signoff_kwargs(activation_id, reservation_id),
            "validation_store_dir": self.validation_store_dir,
            "repo_root": self.repo_root,
        }
        base.update(overrides)
        return base

    def _full_success_chain(self) -> tuple[str, str]:
        activation_id, reservation_id = self._finalize_success()
        record_production_live_operational_signoff(
            **self._signoff_kwargs(
                activation_id,
                reservation_id,
                operator_id=_SIGNOFF_OPERATOR_ID,
            )
        )
        return activation_id, reservation_id

    def test_full_success_rollback_ready_with_warnings(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertIn(
            summary.validation_status,
            {ROLLBACK_READY_WITH_WARNINGS, ROLLBACK_READY},
        )
        self.assertTrue(summary.rollback_ready)
        self.assertTrue(summary.chain_complete)
        self.assertTrue(summary.signoff_valid)
        self.assertTrue(summary.tested_commit_matches)
        self.assertTrue(summary.release_tag_matches_tested_commit)
        self.assertTrue(summary.rollback_commit_valid)
        self.assertTrue(summary.rollback_commit_distinct)
        self.assertGreaterEqual(summary.output_artifact_count, 1)
        self.assertTrue(summary.cleanup_required)

    def test_correlation_mismatch_not_ready(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        path = self.evidence_dir / f"{reservation.execution_attempt_id}.live-pilot-e2e.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["ticket_id"] = "mismatched-ticket"
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertIn(BLOCK_CORRELATION_MISMATCH, summary.blocking_items)

    def test_tested_commit_missing_not_ready(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        with patch(
            "agent.coo.production_live_rollback_validation.load_activation_request",
            return_value=replace(request, tested_commit_sha=""),
        ):
            summary = evaluate_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertFalse(summary.tested_commit_present)

    def test_tested_commit_mismatch_not_ready(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        path = self.store_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["tested_commit_sha"] = "f" * 40
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertIn(BLOCK_TESTED_COMMIT_MISMATCH, summary.blocking_items)

    def test_release_tag_missing_not_ready(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        with patch(
            "agent.coo.production_live_rollback_validation.load_activation_request",
            return_value=replace(request, release_tag=""),
        ):
            summary = evaluate_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertIn(BLOCK_RELEASE_TAG_MISSING, summary.blocking_items)

    def test_release_tag_mismatch_not_ready(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        tags_dir = self.repo_root / ".git" / "refs" / "tags"
        (tags_dir / "v1.0.0-rc.1").write_text(f"{'a' * 40}\n", encoding="utf-8")
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertIn(BLOCK_RELEASE_TAG_MISMATCH, summary.blocking_items)

    def test_rollback_commit_missing_not_ready(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        with patch(
            "agent.coo.production_live_rollback_validation.load_activation_request",
            return_value=replace(request, rollback_commit=""),
        ):
            summary = evaluate_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertIn(BLOCK_ROLLBACK_COMMIT_MISSING, summary.blocking_items)

    def test_rollback_commit_invalid_not_ready(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        with patch(
            "agent.coo.production_live_rollback_validation.load_activation_request",
            return_value=replace(request, rollback_commit="not-a-sha"),
        ):
            summary = evaluate_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertIn(BLOCK_ROLLBACK_COMMIT_INVALID, summary.blocking_items)

    def test_rollback_equals_tested_commit_blocked(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        path = self.store_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["rollback_commit"] = payload["tested_commit_sha"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertIn(BLOCK_ROLLBACK_COMMIT_EQUALS_TESTED_COMMIT, summary.blocking_items)

    def test_signoff_missing_blocked(self) -> None:
        activation_id, reservation_id = self._finalize_success()
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertIn(BLOCK_SIGNOFF_MISSING, summary.blocking_items)

    def test_e2e_not_finalized_blocked(self) -> None:
        activation_id, _, reservation_id = self._run_runtime_success()
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertIn(BLOCK_E2E_FINALIZATION_MISSING, summary.blocking_items)

    def test_consume_partial_requires_recovery(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        with patch(
            "agent.coo.production_live_rollback_validation.assess_consume_status"
        ) as mock_assess:
            from agent.coo.dispatch_consume_transaction import CooDispatchConsumeStatus

            mock_assess.return_value = CooDispatchConsumeStatus(
                consume_state=CONSUME_STATE_PARTIAL,
                transaction_id="tx-partial",
                execution_attempt_id=reservation.execution_attempt_id,
                bundle_consumed=True,
                confirmation_consumed=False,
                recovery_required=True,
            )
            summary = evaluate_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(summary.validation_status, ROLLBACK_REQUIRES_RECOVERY)
        self.assertIn(BLOCK_RECOVERY_REQUIRED, summary.blocking_items)

    def test_repair_lock_requires_recovery(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        with patch(
            "agent.coo.production_live_rollback_validation._probe_repair_lock_held",
            return_value=True,
        ):
            summary = evaluate_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(summary.validation_status, ROLLBACK_REQUIRES_RECOVERY)
        self.assertIn(BLOCK_REPAIR_LOCK_HELD, summary.blocking_items)

    def test_activation_not_revoked_blocked(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        with (
            patch(
                "agent.coo.production_live_rollback_validation.load_activation_request",
                return_value=replace(request, state=ACTIVATION_STATE_SUSPENDED),
            ),
            patch(
                "agent.coo.production_live_rollback_validation.assess_consume_status"
            ) as mock_assess,
        ):
            from agent.coo.dispatch_consume_transaction import (
                CONSUME_STATE_COMMITTED,
                CooDispatchConsumeStatus,
            )

            mock_assess.return_value = CooDispatchConsumeStatus(
                consume_state=CONSUME_STATE_COMMITTED,
                transaction_id="tx-1",
                execution_attempt_id=reservation.execution_attempt_id,
                bundle_consumed=True,
                confirmation_consumed=True,
                recovery_required=False,
            )
            summary = evaluate_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )
        self.assertIn(BLOCK_ACTIVATION_NOT_REVOKED, summary.blocking_items)

    def test_production_root_touched_blocked(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        sentinel = self.signoff_store_dir / _PRODUCTION_ROOT_TOUCHED_SENTINEL
        sentinel.write_text("1", encoding="utf-8")
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertEqual(summary.validation_status, ROLLBACK_NOT_READY)
        self.assertIn(BLOCK_PRODUCTION_ROOT_TOUCHED, summary.blocking_items)

    def test_unexpected_artifacts_blocked(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        (self.mirror_root / "stray-artifact.txt").write_text("x", encoding="utf-8")
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_UNEXPECTED_ARTIFACTS, summary.blocking_items)
        self.assertGreater(summary.unexpected_artifact_count, 0)

    def test_validation_report_append_only(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            summary = record_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )
        record = load_rollback_validation_record(
            activation_id,
            store_dir=self.validation_store_dir,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.validation_status, summary.validation_status)
        self.assertFalse(record.production_execution_allowed)

    def test_duplicate_validation_idempotent(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        record_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        second = record_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertTrue(second.already_validated)

    def test_mismatch_duplicate_corruption(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        record_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        path = self.validation_store_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["validation"]["reservation_id"] = "other-reservation"
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_ARTIFACT_CORRUPTED, summary.blocking_items)
        with self.assertRaises(ProductionLiveRollbackValidationError):
            record_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )

    def test_recovery_validation_report_allowed(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        with patch(
            "agent.coo.production_live_rollback_validation.assess_consume_status"
        ) as mock_assess:
            from agent.coo.dispatch_consume_transaction import CooDispatchConsumeStatus

            mock_assess.return_value = CooDispatchConsumeStatus(
                consume_state=CONSUME_STATE_PREPARED,
                transaction_id="tx-partial",
                execution_attempt_id=reservation.execution_attempt_id,
                bundle_consumed=False,
                confirmation_consumed=False,
                recovery_required=True,
            )
            summary = record_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )
        self.assertEqual(summary.validation_status, ROLLBACK_REQUIRES_RECOVERY)
        self.assertFalse(summary.rollback_ready)
        record = load_rollback_validation_record(
            activation_id,
            store_dir=self.validation_store_dir,
        )
        self.assertIsNotNone(record)

    def test_dashboard_rollback_fields(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        record_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        digest = resolve_latest_rollback_dashboard_digest(
            e2e_history_dir=self.e2e_history_dir,
            validation_store_dir=self.validation_store_dir,
            signoff_store_dir=self.signoff_store_dir,
            store_dir=self.store_dir,
            reservation_dir=self.reservation_dir,
            runtime_history_dir=self.runtime_history_dir,
            evidence_dir=self.evidence_dir,
            audit_dir=self.audit_dir,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            transaction_dir=self.transaction_dir,
            merged_config=self.merged_config,
            repo_root=self.repo_root,
        )
        self.assertIn(
            digest.rollback_validation_status,
            {ROLLBACK_READY_WITH_WARNINGS, ROLLBACK_READY},
        )
        self.assertTrue(digest.rollback_ready)

        summary = build_operator_dashboard_summary(merged_config=self.merged_config)
        self.assertTrue(hasattr(summary, "rollback_validation_status"))
        self.assertTrue(hasattr(summary, "rollback_ready"))

    def test_rollback_check_cli_read_only(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        from hermes_cli.coo_dispatch import build_coo_dispatch_parser

        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "production",
                "activation",
                "rollback-check",
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
        activation_id, reservation_id = self._full_success_chain()
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        output = format_production_live_rollback_check(summary)
        for token in (
            "pipeline_root",
            "argv",
            "rollback_commit: ",
            "/opt/data/",
        ):
            self.assertNotIn(token, output.lower())

    def test_no_git_subprocess(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            summary = evaluate_production_live_rollback_validation(
                **self._rollback_kwargs(activation_id, reservation_id)
            )
        self.assertTrue(summary.rollback_ready)

    def test_source_mutation_blocked(self) -> None:
        activation_id, reservation_id = self._full_success_chain()
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
        summary = evaluate_production_live_rollback_validation(
            **self._rollback_kwargs(activation_id, reservation_id)
        )
        self.assertIn(BLOCK_EXTERNAL_PUBLISH_ATTEMPTED, summary.blocking_items)
        self.assertIn(BLOCK_SOURCE_TREE_MUTATED, summary.blocking_items)


if __name__ == "__main__":
    unittest.main()
