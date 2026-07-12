"""Phase 12R tests — dispatch operator runbook CLI."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import mark_bundle_consumed
from agent.coo.dispatch_cli_operator_runbook import (
    RUNBOOK_ACTION_ALREADY_COMPLETED,
    RUNBOOK_ACTION_INSPECT_STALE_TRANSACTION,
    RUNBOOK_ACTION_MANUAL_RECOVERY_REQUIRED,
    RUNBOOK_ACTION_RETRY_DISPATCH,
    format_dispatch_operator_runbook,
    summarize_dispatch_operator_runbook,
)
from agent.coo.dispatch_consume_transaction import execute_consume_transaction
from agent.coo.production_executor_confirmation import mark_confirmation_consumed_file
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.test_coo_dispatch_consume_repair_apply import _RepairApplyFixture
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


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_digests(fixture: _RepairApplyFixture) -> dict[str, str]:
    ticket_id = fixture.seeded["ticket"].ticket_id
    confirmation_id = fixture.seeded["confirmation"].confirmation_id
    txn_path = fixture.transaction_dir / f"{ticket_id}__{confirmation_id}.json"
    digests = {
        "bundle": _file_digest(fixture.bundle_dir / f"{ticket_id}.json"),
        "confirmation": _file_digest(
            fixture.confirmation_dir / f"{confirmation_id}.json"
        ),
        "transaction": _file_digest(txn_path) if txn_path.is_file() else "",
    }
    repair_audit_dir = fixture.hermes_home / "coo" / "consume-repair-audit"
    if repair_audit_dir.exists():
        for path in sorted(repair_audit_dir.glob("*.json")):
            digests[f"audit:{path.name}"] = _file_digest(path)
    return digests


class _RunbookBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RepairApplyFixture()
        self.fixture.start()
        self.fixture.seed()
        self.fixture.write_binding_state("bound")
        self.evidence_home_patch = patch(
            "agent.coo.dispatch_cli_evidence.get_hermes_home",
            return_value=self.fixture.hermes_home,
        )
        self.evidence_home_patch.start()
        self.audit_dir = self.fixture.hermes_home / "coo" / "audit"
        self.evidence_dir = self.fixture.hermes_home / "coo" / "execution-evidence"
        self.repair_audit_dir = self.fixture.hermes_home / "coo" / "consume-repair-audit"
        self.config = _enabled_executor_config(self.fixture.pipeline_root)

    def tearDown(self) -> None:
        self.evidence_home_patch.stop()
        self.fixture.stop()

    def _pair_ids(self) -> tuple[str, str]:
        return (
            self.fixture.seeded["ticket"].ticket_id,
            self.fixture.seeded["confirmation"].confirmation_id,
        )

    def _runbook_kwargs(self) -> dict:
        ticket_id, confirmation_id = self._pair_ids()
        return {
            "ticket_id": ticket_id,
            "confirmation_id": confirmation_id,
            "bundle_dir": self.fixture.bundle_dir,
            "confirmation_dir": self.fixture.confirmation_dir,
            "transaction_dir": self.fixture.transaction_dir,
            "audit_dir": self.audit_dir,
            "evidence_dir": self.evidence_dir,
            "repair_audit_dir": self.repair_audit_dir,
            "merged_config": self.config,
        }

    def _assert_safe_output(self, output: str) -> None:
        combined = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), combined)
        self.assertNotIn("reason:", combined.replace("description:", ""))
        self.assertNotIn(str(self.fixture.pipeline_root), output)
        self.assertNotIn(str(self.fixture.hermes_home), output)

    def _assert_read_only(self, before: dict[str, str]) -> None:
        self.assertEqual(_snapshot_digests(self.fixture), before)


class TestOperatorRunbookStates(_RunbookBase):
    def test_unconsumed_recommends_retry_dispatch(self) -> None:
        before = _snapshot_digests(self.fixture)
        summary = summarize_dispatch_operator_runbook(**self._runbook_kwargs())
        output = format_dispatch_operator_runbook(summary)
        self._assert_read_only(before)
        self._assert_safe_output(output)
        self.assertEqual(summary.recommended_action, RUNBOOK_ACTION_RETRY_DISPATCH)
        self.assertTrue(summary.retry_allowed)
        self.assertTrue(summary.replay_allowed)
        self.assertEqual(summary.dispatch_status, "ready_for_dispatch")
        self.assertIn("recommended_action: retry_dispatch", output)

    def test_prepared_recommends_inspect_stale_transaction(self) -> None:
        self.fixture.create_prepared_state()
        summary = summarize_dispatch_operator_runbook(**self._runbook_kwargs())
        self.assertEqual(summary.consume_state, "prepared")
        self.assertEqual(
            summary.recommended_action,
            RUNBOOK_ACTION_INSPECT_STALE_TRANSACTION,
        )
        self.assertFalse(summary.replay_allowed)
        self.assertEqual(summary.dispatch_status, "dispatch_blocked")

    def test_partial_recommends_manual_recovery(self) -> None:
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
                    merged_config=self.config,
                    subprocess_runner=_mock_runner_success,
                )
        summary = summarize_dispatch_operator_runbook(**self._runbook_kwargs())
        self.assertEqual(summary.consume_state, "partial")
        self.assertEqual(
            summary.recommended_action,
            RUNBOOK_ACTION_MANUAL_RECOVERY_REQUIRED,
        )
        self.assertFalse(summary.replay_allowed)

    def test_committed_recommends_already_completed(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        execute_consume_transaction(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            execution_attempt_id="attempt-committed",
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
        )
        summary = summarize_dispatch_operator_runbook(**self._runbook_kwargs())
        self.assertEqual(summary.consume_state, "committed")
        self.assertEqual(summary.recommended_action, RUNBOOK_ACTION_ALREADY_COMPLETED)
        self.assertFalse(summary.replay_allowed)
        self.assertEqual(summary.dispatch_status, "already_completed")

    def test_legacy_partial_recommends_manual_recovery(self) -> None:
        ticket_id, _ = self._pair_ids()
        mark_bundle_consumed(ticket_id, bundle_dir=self.fixture.bundle_dir)
        summary = summarize_dispatch_operator_runbook(**self._runbook_kwargs())
        self.assertEqual(summary.consume_state, "legacy_partial")
        self.assertEqual(
            summary.recommended_action,
            RUNBOOK_ACTION_MANUAL_RECOVERY_REQUIRED,
        )

    def test_legacy_committed_recommends_already_completed(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        mark_bundle_consumed(ticket_id, bundle_dir=self.fixture.bundle_dir)
        mark_confirmation_consumed_file(
            confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        summary = summarize_dispatch_operator_runbook(**self._runbook_kwargs())
        self.assertEqual(summary.consume_state, "legacy_committed")
        self.assertEqual(summary.recommended_action, RUNBOOK_ACTION_ALREADY_COMPLETED)

    def test_recovery_required_recommends_manual_recovery(self) -> None:
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
                    merged_config=self.config,
                    subprocess_runner=_mock_runner_success,
                )
        with patch(
            "agent.coo.dispatch_consume_repair.complete_partial_consume_transaction",
            side_effect=OSError("write failed"),
        ):
            from agent.coo.dispatch_consume_repair import apply_consume_repair
            from tests.hermes_cli.test_coo_dispatch_consume_repair_apply import (
                _OPERATOR,
                _VALID_PHRASE,
            )

            with self.assertRaises(ValueError):
                apply_consume_repair(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    bundle_dir=self.fixture.bundle_dir,
                    confirmation_dir=self.fixture.confirmation_dir,
                    transaction_dir=self.fixture.transaction_dir,
                    audit_dir=self.audit_dir,
                    evidence_dir=self.evidence_dir,
                    repair_audit_dir=self.repair_audit_dir,
                    phrase=_VALID_PHRASE,
                    **_OPERATOR,
                )
        summary = summarize_dispatch_operator_runbook(**self._runbook_kwargs())
        self.assertEqual(summary.consume_state, "recovery_required")
        self.assertEqual(
            summary.recommended_action,
            RUNBOOK_ACTION_MANUAL_RECOVERY_REQUIRED,
        )
        self.assertTrue(summary.repair_attempt_id)
        self.assertFalse(summary.replay_allowed)


class TestOperatorRunbookSafety(_RunbookBase):
    def test_cli_runbook_success(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "operator",
                "runbook",
                "--ticket-id",
                ticket_id,
                "--confirmation-id",
                confirmation_id,
            ]
        )
        buf = io.StringIO()
        before = _snapshot_digests(self.fixture)
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
            patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
        ):
            with patch("sys.stdout", buf):
                exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        output = buf.getvalue()
        self._assert_read_only(before)
        self._assert_safe_output(output)
        self.assertIn("Dispatch Status", output)
        self.assertIn("Operator Action", output)
        self.assertIn("recommended_action:", output)

    def test_no_repository2_subprocess(self) -> None:
        before = _snapshot_digests(self.fixture)
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            summarize_dispatch_operator_runbook(**self._runbook_kwargs())
        self._assert_read_only(before)

    def test_cross_reference_fields_present(self) -> None:
        summary = summarize_dispatch_operator_runbook(**self._runbook_kwargs())
        self.assertTrue(summary.binding_state)
        self.assertTrue(summary.runner_provider)
        self.assertTrue(summary.repair_state)
        self.assertTrue(summary.action_description)
