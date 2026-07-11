"""Phase 12K tests — dispatch consume transaction hardening."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import mark_bundle_consumed, read_bundle
from agent.coo.dispatch_cli_consume_status import (
    format_dispatch_consume_status_summary,
    summarize_dispatch_consume_status,
)
from agent.coo.dispatch_cli_run import execute_coo_dispatch_run
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_LEGACY_COMMITTED,
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_UNCONSUMED,
    assess_consume_status,
    assert_consume_replay_allowed,
    execute_consume_transaction,
    read_consume_transaction,
)
from agent.coo.production_executor_confirmation import (
    mark_confirmation_consumed_file,
    read_confirmation,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser, run_coo_dispatch_from_args
from tests.hermes_cli.coo_dispatch_isolated_clone_fixture import (
    CooDispatchIsolatedCloneFixture,
    run_clone_full_path_execute,
)
from tests.hermes_cli.test_coo_dispatch_run import (
    _CooDispatchRunFixture,
    _enabled_executor_config,
    _mock_runner_success,
)


class _ConsumeTransactionFixture(_CooDispatchRunFixture):
    pass


class _ConsumeTransactionBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _ConsumeTransactionFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def _run_kwargs(self, **overrides):
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        base = dict(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            consume_transaction_dir=self.fixture.transaction_dir,
            merged_config=_enabled_executor_config(self.fixture.pipeline_root),
        )
        base.update(overrides)
        return base

    def _pair_ids(self) -> tuple[str, str]:
        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        return ticket.ticket_id, confirmation.confirmation_id


class TestConsumeTransactionHappyPath(_ConsumeTransactionBase):
    def test_success_prepared_committed_both_consumed(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=_mock_runner_success),
            )
        self.assertTrue(result.consumed)
        ticket_id, confirmation_id = self._pair_ids()
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_COMMITTED)
        self.assertTrue(status.bundle_consumed)
        self.assertTrue(status.confirmation_consumed)
        self.assertFalse(status.recovery_required)
        transaction = read_consume_transaction(
            ticket_id,
            confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(transaction.state, "committed")
        self.assertEqual(transaction.execution_attempt_id, result.execution_attempt_id)

    def test_execution_attempt_id_correlates_with_transaction(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=_mock_runner_success),
            )
        ticket_id, confirmation_id = self._pair_ids()
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.execution_attempt_id, result.execution_attempt_id)


class TestConsumeTransactionFailures(_ConsumeTransactionBase):
    def test_bundle_consume_failure_leaves_artifacts_unconsumed(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_consume_transaction.mark_bundle_consumed",
                side_effect=ValueError("bundle consume failed"),
            ),
        ):
            with self.assertRaises(ValueError):
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=_mock_runner_success),
                )
        bundle = read_bundle(ticket_id, bundle_dir=self.fixture.bundle_dir, reject_consumed=False)
        confirmation = read_confirmation(
            confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
            reject_consumed=False,
        )
        self.assertEqual(bundle.consumed_at, "")
        self.assertFalse(confirmation.consumed)
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_PREPARED)

    def test_confirmation_consume_failure_records_partial(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_consume_transaction.mark_confirmation_consumed_file",
                side_effect=ValueError("confirmation consume failed"),
            ),
        ):
            with self.assertRaises(ValueError):
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=_mock_runner_success),
                )
        bundle = read_bundle(ticket_id, bundle_dir=self.fixture.bundle_dir, reject_consumed=False)
        confirmation = read_confirmation(
            confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
            reject_consumed=False,
        )
        self.assertNotEqual(bundle.consumed_at, "")
        self.assertFalse(confirmation.consumed)
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_PARTIAL)
        self.assertTrue(status.recovery_required)

    def test_failure_run_no_transaction_no_consume(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()

        def failing_runner(*args, **kwargs):
            return 1, "", "failed"

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=failing_runner),
            )
        self.assertFalse(result.consumed)
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_UNCONSUMED)
        self.assertIsNone(
            read_consume_transaction(
                ticket_id,
                confirmation_id,
                transaction_dir=self.fixture.transaction_dir,
            )
        )


class TestConsumeReplayPolicy(_ConsumeTransactionBase):
    def test_committed_replay_rejected(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=_mock_runner_success),
            )
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=_mock_runner_success),
                )
        self.assertIn("consumed", str(exc.exception).lower())

    def test_partial_replay_rejected(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_consume_transaction.mark_confirmation_consumed_file",
                side_effect=ValueError("confirmation consume failed"),
            ),
        ):
            with self.assertRaises(ValueError):
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=_mock_runner_success),
                )
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=_mock_runner_success),
                )
        self.assertIn("partial", str(exc.exception).lower())

    def test_stale_prepared_replay_rejected(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_consume_transaction.mark_bundle_consumed",
                side_effect=ValueError("bundle consume failed"),
            ),
        ):
            with self.assertRaises(ValueError):
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=_mock_runner_success),
                )
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=_mock_runner_success),
                )
        self.assertIn("prepared", str(exc.exception).lower())


class TestConsumeLegacyStates(_ConsumeTransactionBase):
    def test_legacy_both_consumed_committed_summary(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        mark_bundle_consumed(ticket_id, bundle_dir=self.fixture.bundle_dir)
        mark_confirmation_consumed_file(
            confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_LEGACY_COMMITTED)
        self.assertFalse(status.recovery_required)

    def test_legacy_one_side_partial_fail_closed(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        mark_bundle_consumed(ticket_id, bundle_dir=self.fixture.bundle_dir)
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_LEGACY_PARTIAL)
        self.assertTrue(status.recovery_required)
        with self.assertRaises(ValueError) as exc:
            assert_consume_replay_allowed(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                transaction_dir=self.fixture.transaction_dir,
            )
        self.assertIn("partial", str(exc.exception).lower())


class TestConsumeTransactionSafety(_ConsumeTransactionBase):
    def test_corrupted_transaction_fail_closed(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        path = self.fixture.transaction_dir / f"{ticket_id}__{confirmation_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            assess_consume_status(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                transaction_dir=self.fixture.transaction_dir,
            )
        self.assertIn("corrupt", str(exc.exception).lower())

    def test_path_traversal_ticket_id_rejected(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        with self.assertRaises(ValueError):
            assess_consume_status(
                ticket_id="../escape",
                confirmation_id=confirmation_id,
                transaction_dir=self.fixture.transaction_dir,
            )

    def test_symlink_escape_transaction_path_rejected(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        outside = Path(self.fixture.tmp.name) / "outside"
        outside.mkdir()
        link_dir = self.fixture.hermes_home / "coo" / "consume-link"
        link_dir.parent.mkdir(parents=True, exist_ok=True)
        link_dir.symlink_to(outside)
        with self.assertRaises(ValueError):
            assess_consume_status(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                transaction_dir=link_dir,
            )

    def test_atomic_write_uses_replace(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        with patch(
            "agent.coo.dispatch_consume_transaction.os.replace",
            wraps=os.replace,
        ) as replace_mock:
            execute_consume_transaction(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                execution_attempt_id="attempt-123",
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                transaction_dir=self.fixture.transaction_dir,
            )
        self.assertGreaterEqual(replace_mock.call_count, 2)


class TestConsumeStatusCli(_ConsumeTransactionBase):
    def test_consume_status_cli_safe_output(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=_mock_runner_success),
            )
        summary = summarize_dispatch_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        output = format_dispatch_consume_status_summary(summary)
        self.assertIn("consume_state: committed", output)
        self.assertIn("bundle_consumed: true", output)
        self.assertIn("confirmation_consumed: true", output)
        self.assertIn("recovery_required: false", output)
        self.assertNotIn(str(self.fixture.pipeline_root), output)

        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "consume",
                "status",
                "--ticket-id",
                ticket_id,
                "--confirmation-id",
                confirmation_id,
            ]
        )
        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        cli_output = stdout.getvalue()
        self.assertIn("consume_state: committed", cli_output)
        self.assertNotIn("snapshot", cli_output.lower())


class TestConsumeCloneRegression(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CooDispatchIsolatedCloneFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_clone_full_path_success_still_commits_consume(self) -> None:
        result = run_clone_full_path_execute(self.fixture, self.seeded)
        self.assertTrue(result.consumed)
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_COMMITTED)
        self.assertEqual(status.execution_attempt_id, result.execution_attempt_id)


if __name__ == "__main__":
    unittest.main()
