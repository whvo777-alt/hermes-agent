"""Phase 12P tests — dispatch consume repair partial forward-complete apply."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_consume_repair import (
    REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE,
    REQUIRED_CONSUME_REPAIR_PHRASE,
    apply_consume_repair,
    apply_partial_forward_complete,
    apply_prepared_transaction_cleanup,
)
from agent.coo.dispatch_consume_repair_lock import consume_repair_pair_lock
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    assert_consume_replay_allowed,
    assess_consume_status,
    read_consume_transaction,
)
from agent.coo.dispatch_bundle_store import mark_bundle_consumed
from agent.coo.production_executor_confirmation import read_confirmation
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.test_coo_dispatch_consume_repair_apply import (
    _OPERATOR,
    _RepairApplyFixture,
    _VALID_PHRASE,
    _file_digest,
)
from tests.hermes_cli.test_coo_dispatch_run import (
    _enabled_executor_config,
    _mock_runner_success,
)

_FORBIDDEN_OUTPUT_TOKENS = (
    "argv",
    "cwd",
    "env",
    "stdout",
    "stderr",
    "snapshot",
    "token",
    "SECRET",
    "PASSWORD",
    "pipeline.js",
    "/opt/data/multi-content-pipeline",
)


class _ForwardCompleteBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RepairApplyFixture()
        self.fixture.start()
        self.fixture.seed()
        self.evidence_home_patch = patch(
            "agent.coo.dispatch_cli_evidence.get_hermes_home",
            return_value=self.fixture.hermes_home,
        )
        self.evidence_home_patch.start()
        self.audit_dir = self.fixture.hermes_home / "coo" / "audit"
        self.evidence_dir = self.fixture.hermes_home / "coo" / "execution-evidence"

    def tearDown(self) -> None:
        self.evidence_home_patch.stop()
        self.fixture.stop()

    def _pair_ids(self) -> tuple[str, str]:
        return (
            self.fixture.seeded["ticket"].ticket_id,
            self.fixture.seeded["confirmation"].confirmation_id,
        )

    def _apply_kwargs(self, **overrides):
        base = self.fixture._apply_kwargs(
            audit_dir=self.audit_dir,
            evidence_dir=self.evidence_dir,
        )
        base.update(overrides)
        return base

    @property
    def transaction_path(self) -> Path:
        ticket_id, confirmation_id = self._pair_ids()
        return self.fixture.transaction_dir / f"{ticket_id}__{confirmation_id}.json"

    def create_partial_state(self) -> None:
        """Create a real partial: run succeeds, confirmation consume fails."""
        ticket_id, confirmation_id = self._pair_ids()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_consume_transaction.mark_confirmation_consumed_file",
                side_effect=ValueError("confirmation consume failed"),
            ),
        ):
            from agent.coo.dispatch_cli_run import execute_coo_dispatch_run

            with self.assertRaises(ValueError):
                execute_coo_dispatch_run(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    unlock_token_id=self.fixture.seeded["prepare"]["unlock_token"]["token_id"],
                    requester_id=self.fixture.seeded["ticket"].requester_id,
                    pipeline_root=str(self.fixture.pipeline_root),
                    bundle_dir=self.fixture.bundle_dir,
                    confirmation_dir=self.fixture.confirmation_dir,
                    consume_transaction_dir=self.fixture.transaction_dir,
                    merged_config=_enabled_executor_config(self.fixture.pipeline_root),
                    subprocess_runner=_mock_runner_success,
                )

    def _snapshot_digests(self) -> dict:
        return {
            "bundle": _file_digest(self.fixture.bundle_path),
            "confirmation": _file_digest(self.fixture.confirmation_path),
            "transaction": (
                _file_digest(self.transaction_path)
                if self.transaction_path.is_file()
                else ""
            ),
        }

    def _assert_zero_mutation(self, before: dict) -> None:
        self.assertEqual(self._snapshot_digests(), before)
        self.assertFalse(any(self.fixture.repair_audit_dir.glob("*.json")))

    def _assert_safe_output(self, output: str) -> None:
        combined = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), combined)
        self.assertNotIn("phrase:", combined.replace("phrase_verified:", ""))
        self.assertNotIn(_VALID_PHRASE.lower(), combined)
        self.assertNotIn(str(self.fixture.pipeline_root), output)
        self.assertNotIn(str(self.fixture.hermes_home), output)


class TestPartialForwardCompleteSuccess(_ForwardCompleteBase):
    def test_valid_partial_forward_complete_success(self) -> None:
        self.create_partial_state()
        ticket_id, confirmation_id = self._pair_ids()
        bundle_digest_before = _file_digest(self.fixture.bundle_path)
        txn_before = read_consume_transaction(
            ticket_id,
            confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        assert txn_before is not None
        self.assertEqual(txn_before.state, CONSUME_STATE_PARTIAL)

        result = apply_consume_repair(**self._apply_kwargs())

        self.assertTrue(result.applied)
        self.assertEqual(result.repair_action, REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE)
        self.assertEqual(result.consume_state_before, CONSUME_STATE_PARTIAL)
        self.assertEqual(result.consume_state_after, CONSUME_STATE_COMMITTED)
        self.assertTrue(result.bundle_consumed)
        self.assertTrue(result.confirmation_consumed)
        self.assertFalse(result.recovery_required)
        self.assertTrue(result.correlation_valid)
        self.assertTrue(result.evidence_success)
        self.assertTrue(result.phrase_verified)
        self.assertEqual(result.execution_attempt_id, txn_before.execution_attempt_id)

        # Bundle byte-for-byte unchanged; confirmation consumed.
        self.assertEqual(_file_digest(self.fixture.bundle_path), bundle_digest_before)
        confirmation = read_confirmation(
            confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
            reject_consumed=False,
        )
        self.assertTrue(confirmation.consumed)
        self.assertTrue(confirmation.consumed_at)

        # Transaction partial -> committed with repair metadata preserved.
        txn_after = read_consume_transaction(
            ticket_id,
            confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        assert txn_after is not None
        self.assertEqual(txn_after.state, "committed")
        self.assertEqual(txn_after.transaction_id, txn_before.transaction_id)
        self.assertEqual(txn_after.execution_attempt_id, txn_before.execution_attempt_id)
        self.assertTrue(txn_after.committed_at)
        self.assertTrue(txn_after.bundle_consumed)
        self.assertTrue(txn_after.confirmation_consumed)
        self.assertEqual(txn_after.failure_reason, "")
        self.assertEqual(txn_after.repair_attempt_id, result.repair_attempt_id)
        self.assertEqual(txn_after.repair_action, REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE)
        self.assertTrue(txn_after.phrase_verified)

        # Repair audit applied record.
        audit_path = self.fixture.repair_audit_dir / f"{result.repair_attempt_id}.json"
        self.assertTrue(audit_path.is_file())
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["outcome"], "applied")
        self.assertEqual(payload["consume_state_before"], CONSUME_STATE_PARTIAL)
        self.assertEqual(payload["consume_state_after"], CONSUME_STATE_COMMITTED)
        self.assertTrue(payload["correlation_valid"])
        self.assertTrue(payload["evidence_success"])
        self.assertEqual(
            payload["execution_attempt_id"],
            txn_before.execution_attempt_id,
        )
        self.assertNotIn("phrase", payload)

        # Final consume status committed.
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_COMMITTED)
        self.assertFalse(status.recovery_required)

    def test_replay_and_reapply_rejected_after_forward_complete(self) -> None:
        self.create_partial_state()
        ticket_id, confirmation_id = self._pair_ids()
        apply_consume_repair(**self._apply_kwargs())

        with self.assertRaises(ValueError):
            assert_consume_replay_allowed(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                transaction_dir=self.fixture.transaction_dir,
            )
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs())

    def test_cli_apply_forward_complete_exit_zero(self) -> None:
        self.create_partial_state()
        ticket_id, confirmation_id = self._pair_ids()
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "consume",
                "repair",
                "apply",
                "--ticket-id",
                ticket_id,
                "--confirmation-id",
                confirmation_id,
                "--operator-id",
                _OPERATOR["operator_id"],
                "--operator-name",
                _OPERATOR["operator_name"],
                "--reason",
                _OPERATOR["reason"],
                "--phrase",
                _VALID_PHRASE,
            ]
        )
        buf = io.StringIO()
        with (
            patch.dict("os.environ", {"HERMES_HOME": str(self.fixture.hermes_home)}),
            patch(
                "agent.coo.dispatch_consume_transaction.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_consume_repair_audit.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_bundle_store.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.production_executor_confirmation.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_execution_audit.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.production_executor_factory.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
        ):
            with patch("sys.stdout", buf):
                exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        output = buf.getvalue()
        self._assert_safe_output(output)
        self.assertIn("repair_action: repair_action_partial_forward_complete", output)
        self.assertIn("consume_state_after: committed", output)


class TestPartialForwardCompleteRejections(_ForwardCompleteBase):
    def test_missing_audit_zero_mutation(self) -> None:
        self.create_partial_state()
        for path in list(self.audit_dir.glob("*.json")):
            path.unlink()
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs())
        self._assert_zero_mutation(before)

    def test_missing_evidence_zero_mutation(self) -> None:
        self.create_partial_state()
        txn = read_consume_transaction(
            *self._pair_ids(),
            transaction_dir=self.fixture.transaction_dir,
        )
        assert txn is not None
        (self.evidence_dir / f"{txn.execution_attempt_id}.meta.json").unlink()
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs())
        self._assert_zero_mutation(before)

    def test_non_zero_evidence_zero_mutation(self) -> None:
        self.create_partial_state()
        txn = read_consume_transaction(
            *self._pair_ids(),
            transaction_dir=self.fixture.transaction_dir,
        )
        assert txn is not None
        meta_path = self.evidence_dir / f"{txn.execution_attempt_id}.meta.json"
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["exit_code"] = 1
        meta_path.write_text(json.dumps(payload), encoding="utf-8")
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs())
        self._assert_zero_mutation(before)

    def test_correlation_mismatch_zero_mutation(self) -> None:
        self.create_partial_state()
        audit_paths = list(self.audit_dir.glob("*.json"))
        self.assertTrue(audit_paths)
        for path in audit_paths:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["confirmation_id"] = "someone-elses-confirmation"
            path.write_text(json.dumps(payload), encoding="utf-8")
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs())
        self._assert_zero_mutation(before)

    def test_execution_attempt_id_mismatch_zero_mutation(self) -> None:
        self.create_partial_state()
        payload = json.loads(self.transaction_path.read_text(encoding="utf-8"))
        payload["execution_attempt_id"] = "tampered-attempt-id"
        self.transaction_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs())
        self._assert_zero_mutation(before)

    def test_wrong_phrase_zero_mutation(self) -> None:
        self.create_partial_state()
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs(phrase="WRONG-PHRASE"))
        self._assert_zero_mutation(before)

    def test_invalid_operator_zero_mutation(self) -> None:
        self.create_partial_state()
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs(operator_id="   "))
        self._assert_zero_mutation(before)

    def test_lock_held_zero_mutation(self) -> None:
        self.create_partial_state()
        ticket_id, confirmation_id = self._pair_ids()
        before = self._snapshot_digests()
        holder = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        def hold_lock() -> None:
            try:
                with consume_repair_pair_lock(
                    ticket_id,
                    confirmation_id,
                    transaction_dir=self.fixture.transaction_dir,
                ):
                    holder.set()
                    release.wait(timeout=5)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=hold_lock, daemon=True)
        thread.start()
        self.assertTrue(holder.wait(timeout=5))
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs())
        release.set()
        thread.join(timeout=5)
        self.assertFalse(errors)
        self._assert_zero_mutation(before)

    def test_legacy_partial_rejected_zero_mutation(self) -> None:
        ticket_id, _ = self._pair_ids()
        mark_bundle_consumed(ticket_id, bundle_dir=self.fixture.bundle_dir)
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs())
        self._assert_zero_mutation(before)

    def test_prepared_state_partial_action_rejected(self) -> None:
        self.fixture.create_prepared_state()
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_partial_forward_complete(**self._apply_kwargs())
        self._assert_zero_mutation(before)

    def test_partial_state_prepared_cleanup_rejected(self) -> None:
        self.create_partial_state()
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(**self._apply_kwargs())
        self._assert_zero_mutation(before)

    def test_committed_rejected(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            from agent.coo.dispatch_cli_run import execute_coo_dispatch_run

            execute_coo_dispatch_run(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                unlock_token_id=self.fixture.seeded["prepare"]["unlock_token"]["token_id"],
                requester_id=self.fixture.seeded["ticket"].requester_id,
                pipeline_root=str(self.fixture.pipeline_root),
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                consume_transaction_dir=self.fixture.transaction_dir,
                merged_config=_enabled_executor_config(self.fixture.pipeline_root),
                subprocess_runner=_mock_runner_success,
            )
        before = self._snapshot_digests()
        with self.assertRaises(ValueError):
            apply_consume_repair(**self._apply_kwargs())
        self._assert_zero_mutation(before)


class TestPartialForwardCompleteFailureHandling(_ForwardCompleteBase):
    def test_confirmation_consume_failure_keeps_partial(self) -> None:
        self.create_partial_state()
        ticket_id, confirmation_id = self._pair_ids()
        confirmation_digest_before = _file_digest(self.fixture.confirmation_path)
        transaction_digest_before = _file_digest(self.transaction_path)
        with patch(
            "agent.coo.dispatch_consume_repair.mark_confirmation_consumed_file",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(ValueError):
                apply_consume_repair(**self._apply_kwargs())
        self.assertEqual(
            _file_digest(self.fixture.confirmation_path),
            confirmation_digest_before,
        )
        self.assertEqual(_file_digest(self.transaction_path), transaction_digest_before)
        txn = read_consume_transaction(
            ticket_id,
            confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        assert txn is not None
        self.assertEqual(txn.state, CONSUME_STATE_PARTIAL)
        self.assertFalse(any(self.fixture.repair_audit_dir.glob("*.json")))

    def test_commit_failure_after_confirmation_consume_fail_closed(self) -> None:
        self.create_partial_state()
        ticket_id, confirmation_id = self._pair_ids()
        with patch(
            "agent.coo.dispatch_consume_repair.complete_partial_consume_transaction",
            side_effect=OSError("write failed"),
        ):
            with self.assertRaises(ValueError):
                apply_consume_repair(**self._apply_kwargs())

        # No rollback: confirmation stays consumed, transaction record stays partial.
        confirmation = read_confirmation(
            confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
            reject_consumed=False,
        )
        self.assertTrue(confirmation.consumed)
        payload = json.loads(self.transaction_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], CONSUME_STATE_PARTIAL)

        # Failed repair audit is recorded explicitly.
        audit_paths = list(self.fixture.repair_audit_dir.glob("*.json"))
        self.assertEqual(len(audit_paths), 1)
        audit_payload = json.loads(audit_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(audit_payload["outcome"], "failed")
        self.assertEqual(audit_payload["consume_state_after"], "recovery_required")

        # Known recovery-required inconsistency is surfaced explicitly.
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
            repair_audit_dir=self.fixture.repair_audit_dir,
        )
        self.assertEqual(status.consume_state, "recovery_required")
        self.assertTrue(status.recovery_required)
        self.assertTrue(status.repair_attempt_id)


class TestPartialForwardCompleteSafety(_ForwardCompleteBase):
    def test_no_repository2_subprocess(self) -> None:
        self.create_partial_state()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = apply_consume_repair(**self._apply_kwargs())
        self.assertTrue(result.applied)

    def test_prepared_cleanup_dispatch_regression(self) -> None:
        """apply_consume_repair still routes prepared clean pairs to cleanup."""
        self.fixture.create_prepared_state()
        result = apply_consume_repair(**self._apply_kwargs())
        self.assertTrue(result.applied)
        self.assertEqual(result.repair_action, "repair_action_prepared_cleanup")
        self.assertEqual(result.consume_state_after, "unconsumed")

    def test_dry_run_recovery_regression_on_partial(self) -> None:
        from agent.coo.dispatch_cli_consume_repair import (
            run_dispatch_consume_repair_dry_run,
        )

        self.create_partial_state()
        ticket_id, confirmation_id = self._pair_ids()
        eligibility, exit_code = run_dispatch_consume_repair_dry_run(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
            audit_dir=self.audit_dir,
            evidence_dir=self.evidence_dir,
            **_OPERATOR,
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(eligibility.repair_eligible)
        self.assertEqual(
            eligibility.repair_action,
            REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE,
        )
        self.assertFalse(eligibility.mutation_planned)
