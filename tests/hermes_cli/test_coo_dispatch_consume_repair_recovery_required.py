"""Phase 12Q tests — recovery-required diagnosis and repair audit/lock read paths."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_consume_recovery import (
    RECOMMENDED_ACTION_MANUAL_RECOVERY_REQUIRED,
    assess_dispatch_consume_recovery,
)
from agent.coo.dispatch_cli_consume_repair_audit import (
    format_dispatch_consume_repair_audit_summary,
    list_consume_repair_audit_summaries,
    summarize_consume_repair_audit,
)
from agent.coo.dispatch_cli_consume_repair_lock import (
    format_dispatch_consume_repair_lock_status,
    summarize_consume_repair_lock_status,
)
from agent.coo.dispatch_cli_validation_core import (
    STEP_REPAIR_LOCK,
    DispatchPreRunValidationFailure,
    validate_dispatch_pre_run,
)
from agent.coo.dispatch_consume_repair import (
    BLOCKED_RECOVERY_REQUIRED_MANUAL_ONLY,
    evaluate_consume_repair_eligibility,
)
from agent.coo.dispatch_consume_repair_lock import consume_repair_pair_lock
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_RECOVERY_REQUIRED,
    assert_consume_replay_allowed,
    assess_consume_status,
)
from agent.coo.production_executor_confirmation import mark_confirmation_consumed_file
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
    "SECRET",
    "PASSWORD",
    "pipeline.js",
    "/opt/data/multi-content-pipeline",
)


class _RecoveryRequiredFixture(_RepairApplyFixture):
    def create_recovery_required_state(self) -> str:
        ticket_id, confirmation_id = (
            self.seeded["ticket"].ticket_id,
            self.seeded["confirmation"].confirmation_id,
        )
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_consume_transaction.mark_confirmation_consumed_file",
                side_effect=ValueError("confirmation consume failed"),
            ),
        ):
            from agent.coo.dispatch_cli_run import execute_coo_dispatch_run

            try:
                execute_coo_dispatch_run(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    unlock_token_id=self.seeded["prepare"]["unlock_token"]["token_id"],
                    requester_id=self.seeded["ticket"].requester_id,
                    pipeline_root=str(self.pipeline_root),
                    bundle_dir=self.bundle_dir,
                    confirmation_dir=self.confirmation_dir,
                    consume_transaction_dir=self.transaction_dir,
                    merged_config=_enabled_executor_config(self.pipeline_root),
                    subprocess_runner=_mock_runner_success,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("expected partial consume failure")
        with patch(
            "agent.coo.dispatch_consume_repair.complete_partial_consume_transaction",
            side_effect=OSError("write failed"),
        ):
            from agent.coo.dispatch_consume_repair import apply_consume_repair

            try:
                apply_consume_repair(**self._apply_kwargs())
            except ValueError:
                pass
            else:
                raise AssertionError("expected repair commit failure")
        audit_paths = list(self.repair_audit_dir.glob("*.json"))
        if len(audit_paths) != 1:
            raise AssertionError(f"expected one repair audit, found {len(audit_paths)}")
        return json.loads(audit_paths[0].read_text(encoding="utf-8"))["repair_attempt_id"]


class _RecoveryRequiredBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RecoveryRequiredFixture()
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

    def _snapshot_digests(self) -> dict[str, str]:
        ticket_id, confirmation_id = self._pair_ids()
        txn_path = self.fixture.transaction_dir / f"{ticket_id}__{confirmation_id}.json"
        digests = {
            "bundle": _file_digest(self.fixture.bundle_path),
            "confirmation": _file_digest(self.fixture.confirmation_path),
            "transaction": _file_digest(txn_path) if txn_path.is_file() else "",
        }
        for path in sorted(self.fixture.repair_audit_dir.glob("*.json")):
            digests[f"audit:{path.name}"] = _file_digest(path)
        lock_dir = self.fixture.transaction_dir / ".locks"
        if lock_dir.exists():
            for path in sorted(lock_dir.glob("*.lock")):
                digests[f"lock:{path.name}"] = _file_digest(path)
        return digests

    def _assert_read_only(self, before: dict[str, str]) -> None:
        self.assertEqual(self._snapshot_digests(), before)

    def _assert_safe_output(self, output: str) -> None:
        combined = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), combined)
        self.assertNotIn(_VALID_PHRASE.lower(), combined)
        self.assertNotIn(str(self.fixture.pipeline_root), output)
        self.assertNotIn(str(self.fixture.hermes_home), output)
        self.assertNotIn("reason:", combined.replace("failure_reason_code:", ""))


class TestRecoveryRequiredDiagnosis(_RecoveryRequiredBase):
    def test_commit_failure_known_recovery_required(self) -> None:
        repair_attempt_id = self.fixture.create_recovery_required_state()
        ticket_id, confirmation_id = self._pair_ids()
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
            repair_audit_dir=self.fixture.repair_audit_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_RECOVERY_REQUIRED)
        self.assertTrue(status.recovery_required)
        self.assertTrue(status.bundle_consumed)
        self.assertTrue(status.confirmation_consumed)
        self.assertEqual(status.repair_attempt_id, repair_attempt_id)

    def test_recovery_required_replay_rejected(self) -> None:
        self.fixture.create_recovery_required_state()
        ticket_id, confirmation_id = self._pair_ids()
        with self.assertRaises(ValueError):
            assert_consume_replay_allowed(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                transaction_dir=self.fixture.transaction_dir,
                repair_audit_dir=self.fixture.repair_audit_dir,
            )

    def test_recovery_required_repair_dry_run_blocked(self) -> None:
        self.fixture.create_recovery_required_state()
        ticket_id, confirmation_id = self._pair_ids()
        eligibility = evaluate_consume_repair_eligibility(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
            audit_dir=self.audit_dir,
            evidence_dir=self.evidence_dir,
            repair_audit_dir=self.fixture.repair_audit_dir,
            **_OPERATOR,
        )
        self.assertFalse(eligibility.repair_eligible)
        self.assertEqual(eligibility.repair_action, "repair_action_blocked")
        self.assertEqual(eligibility.blocked_reason, BLOCKED_RECOVERY_REQUIRED_MANUAL_ONLY)

    def test_recovery_assessment_manual_recovery_required(self) -> None:
        self.fixture.create_recovery_required_state()
        ticket_id, confirmation_id = self._pair_ids()
        assessment = assess_dispatch_consume_recovery(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
            audit_dir=self.audit_dir,
            evidence_dir=self.evidence_dir,
            repair_audit_dir=self.fixture.repair_audit_dir,
        )
        self.assertEqual(assessment.consume_state, CONSUME_STATE_RECOVERY_REQUIRED)
        self.assertTrue(assessment.recovery_required)
        self.assertEqual(
            assessment.recommended_action,
            RECOMMENDED_ACTION_MANUAL_RECOVERY_REQUIRED,
        )
        self.assertFalse(assessment.retry_allowed)
        self.assertTrue(assessment.repair_audit_present)
        self.assertTrue(assessment.repair_attempt_id)

    def test_missing_repair_audit_unknown_inconsistency_raises(self) -> None:
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
        mark_confirmation_consumed_file(
            confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        with self.assertRaises(ValueError):
            assess_consume_status(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                transaction_dir=self.fixture.transaction_dir,
                repair_audit_dir=self.fixture.repair_audit_dir,
            )

    def test_mismatched_repair_audit_raises(self) -> None:
        self.fixture.create_recovery_required_state()
        audit_path = next(self.fixture.repair_audit_dir.glob("*.json"))
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        payload["execution_attempt_id"] = "tampered-attempt-id"
        audit_path.write_text(json.dumps(payload), encoding="utf-8")
        ticket_id, confirmation_id = self._pair_ids()
        with self.assertRaises(ValueError):
            assess_consume_status(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                transaction_dir=self.fixture.transaction_dir,
                repair_audit_dir=self.fixture.repair_audit_dir,
            )


class TestRepairAuditReadOnly(_RecoveryRequiredBase):
    def test_repair_audit_show(self) -> None:
        repair_attempt_id = self.fixture.create_recovery_required_state()
        before = self._snapshot_digests()
        summary = summarize_consume_repair_audit(
            repair_attempt_id=repair_attempt_id,
            audit_dir=self.fixture.repair_audit_dir,
        )
        output = format_dispatch_consume_repair_audit_summary(summary)
        self._assert_read_only(before)
        self._assert_safe_output(output)
        self.assertEqual(summary.outcome, "failed")
        self.assertEqual(summary.consume_state_after, "recovery_required")
        self.assertEqual(summary.failure_reason_code, "transaction_commit_failed")

    def test_repair_audit_list_newest_first(self) -> None:
        repair_attempt_id = self.fixture.create_recovery_required_state()
        ticket_id, _ = self._pair_ids()
        audit_path = self.fixture.repair_audit_dir / f"{repair_attempt_id}.json"
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
        older = dict(payload)
        older["repair_attempt_id"] = "00000000-0000-4000-8000-000000000001"
        older["applied_at"] = "2020-01-01T00:00:00+00:00"
        (self.fixture.repair_audit_dir / f"{older['repair_attempt_id']}.json").write_text(
            json.dumps(older),
            encoding="utf-8",
        )
        before = self._snapshot_digests()
        summaries = list_consume_repair_audit_summaries(
            audit_dir=self.fixture.repair_audit_dir,
            ticket_id=ticket_id,
        )
        self._assert_read_only(before)
        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0].repair_attempt_id, repair_attempt_id)

    def test_corrupted_repair_audit_rejected(self) -> None:
        repair_attempt_id = self.fixture.create_recovery_required_state()
        audit_path = self.fixture.repair_audit_dir / f"{repair_attempt_id}.json"
        audit_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            summarize_consume_repair_audit(
                repair_attempt_id=repair_attempt_id,
                audit_dir=self.fixture.repair_audit_dir,
            )

    def test_repair_audit_path_traversal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            summarize_consume_repair_audit(
                repair_attempt_id="../escape",
                audit_dir=self.fixture.repair_audit_dir,
            )


class TestRepairLockStatus(_RecoveryRequiredBase):
    def test_lock_status_free(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        before = self._snapshot_digests()
        status = summarize_consume_repair_lock_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        output = format_dispatch_consume_repair_lock_status(status)
        self._assert_read_only(before)
        self._assert_safe_output(output)
        self.assertFalse(status.lock_present)
        self.assertTrue(status.lock_acquirable)
        self.assertFalse(status.repair_in_progress)
        self.assertFalse(status.stale_unknown)

    def test_lock_status_held(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        holder = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with consume_repair_pair_lock(
                ticket_id,
                confirmation_id,
                transaction_dir=self.fixture.transaction_dir,
            ):
                holder.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=hold_lock, daemon=True)
        thread.start()
        self.assertTrue(holder.wait(timeout=5))
        before = self._snapshot_digests()
        status = summarize_consume_repair_lock_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        release.set()
        thread.join(timeout=5)
        self._assert_read_only(before)
        self.assertTrue(status.lock_present)
        self.assertFalse(status.lock_acquirable)
        self.assertTrue(status.repair_in_progress)
        self.assertFalse(status.stale_unknown)

    def test_stale_lock_file_not_deleted(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        lock_dir = self.fixture.transaction_dir / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{ticket_id}__{confirmation_id}.lock"
        lock_path.write_text("orphaned", encoding="utf-8")
        digest_before = _file_digest(lock_path)
        status = summarize_consume_repair_lock_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertTrue(status.lock_present)
        self.assertTrue(status.stale_unknown)
        self.assertEqual(_file_digest(lock_path), digest_before)


class TestConcurrentRunGuard(_RecoveryRequiredBase):
    def test_pre_run_fails_when_repair_lock_held(self) -> None:
        seeded = self.fixture.seeded
        ticket_id = seeded["ticket"].ticket_id
        confirmation_id = seeded["confirmation"].confirmation_id
        self.fixture.write_binding_state("bound")
        holder = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with consume_repair_pair_lock(
                ticket_id,
                confirmation_id,
                transaction_dir=self.fixture.transaction_dir,
            ):
                holder.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=hold_lock, daemon=True)
        thread.start()
        self.assertTrue(holder.wait(timeout=5))
        with self.assertRaises(DispatchPreRunValidationFailure) as ctx:
            validate_dispatch_pre_run(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                pipeline_root=str(self.fixture.pipeline_root),
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                transaction_dir=self.fixture.transaction_dir,
                merged_config=_enabled_executor_config(self.fixture.pipeline_root),
            )
        release.set()
        thread.join(timeout=5)
        self.assertEqual(ctx.exception.step, STEP_REPAIR_LOCK)


class TestRecoveryRequiredCliAndSafety(_RecoveryRequiredBase):
    def test_cli_audit_show_and_lock_status(self) -> None:
        repair_attempt_id = self.fixture.create_recovery_required_state()
        ticket_id, confirmation_id = self._pair_ids()
        parser = build_coo_dispatch_parser()
        audit_args = parser.parse_args(
            [
                "consume",
                "repair",
                "audit",
                "show",
                "--repair-attempt-id",
                repair_attempt_id,
            ]
        )
        lock_args = parser.parse_args(
            [
                "consume",
                "repair",
                "lock",
                "status",
                "--ticket-id",
                ticket_id,
                "--confirmation-id",
                confirmation_id,
            ]
        )
        audit_buf = io.StringIO()
        lock_buf = io.StringIO()
        with (
            patch.dict("os.environ", {"HERMES_HOME": str(self.fixture.hermes_home)}),
            patch(
                "agent.coo.dispatch_consume_repair_audit.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_consume_repair_audit.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_consume_transaction.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
        ):
            with patch("sys.stdout", audit_buf):
                self.assertEqual(audit_args.handler(audit_args), 0)
            with patch("sys.stdout", lock_buf):
                self.assertEqual(lock_args.handler(lock_args), 0)
        self._assert_safe_output(audit_buf.getvalue())
        self._assert_safe_output(lock_buf.getvalue())

    def test_no_repository2_subprocess_on_read_paths(self) -> None:
        repair_attempt_id = self.fixture.create_recovery_required_state()
        ticket_id, confirmation_id = self._pair_ids()
        before = self._snapshot_digests()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            assess_dispatch_consume_recovery(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                transaction_dir=self.fixture.transaction_dir,
                audit_dir=self.audit_dir,
                evidence_dir=self.evidence_dir,
                repair_audit_dir=self.fixture.repair_audit_dir,
            )
            summarize_consume_repair_audit(
                repair_attempt_id=repair_attempt_id,
                audit_dir=self.fixture.repair_audit_dir,
            )
            summarize_consume_repair_lock_status(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                transaction_dir=self.fixture.transaction_dir,
            )
        self._assert_read_only(before)

    def test_partial_state_unchanged_by_recovery_reads(self) -> None:
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
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_PARTIAL)
