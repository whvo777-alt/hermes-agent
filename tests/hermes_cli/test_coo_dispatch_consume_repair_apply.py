"""Phase 12O tests — dispatch consume repair prepared cleanup apply."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_consume_repair import (
    REQUIRED_CONSUME_REPAIR_PHRASE,
    CooDispatchConsumeRepairEligibility,
    apply_prepared_transaction_cleanup,
    evaluate_consume_repair_eligibility,
)
from agent.coo.dispatch_consume_repair_lock import consume_repair_pair_lock
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_LEGACY_COMMITTED,
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_UNCONSUMED,
    assert_consume_replay_allowed,
    assess_consume_status,
    read_consume_transaction,
)
from agent.coo.dispatch_bundle_store import mark_bundle_consumed
from agent.coo.production_executor_confirmation import mark_confirmation_consumed_file
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.test_coo_dispatch_run import (
    _CooDispatchRunFixture,
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
    "phrase",
    "SECRET",
    "PASSWORD",
    "pipeline.js",
    "/opt/data/multi-content-pipeline",
)

_OPERATOR = {
    "operator_id": "op-repair-apply",
    "operator_name": "Repair Apply Operator",
    "reason": "prepared cleanup apply",
}

_VALID_PHRASE = REQUIRED_CONSUME_REPAIR_PHRASE


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dir_digest(root: Path) -> str:
    if not root.exists():
        return ""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            parts.append(f"{rel}:{digest}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


class _RepairApplyFixture(_CooDispatchRunFixture):
    def start(self) -> None:
        super().start()
        self.repair_audit_home_patch = patch(
            "agent.coo.dispatch_consume_repair_audit.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.repair_audit_home_patch.start()
        self.repair_lock_home_patch = patch(
            "agent.coo.dispatch_consume_repair_lock.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.repair_lock_home_patch.start()
        self.repair_audit_dir = self.hermes_home / "coo" / "consume-repair-audit"

    def stop(self) -> None:
        self.repair_lock_home_patch.stop()
        self.repair_audit_home_patch.stop()
        super().stop()

    @property
    def bundle_path(self) -> Path:
        ticket_id = self.seeded["ticket"].ticket_id
        return self.bundle_dir / f"{ticket_id}.json"

    @property
    def confirmation_path(self) -> Path:
        confirmation_id = self.seeded["confirmation"].confirmation_id
        return self.confirmation_dir / f"{confirmation_id}.json"

    def seed(self) -> None:
        self.seeded = self.seed_bundle_and_confirmation()

    def _apply_kwargs(self, **overrides):
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        base = dict(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            transaction_dir=self.transaction_dir,
            repair_audit_dir=self.repair_audit_dir,
            phrase=_VALID_PHRASE,
            **_OPERATOR,
        )
        base.update(overrides)
        return base

    def create_prepared_state(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_consume_transaction.mark_bundle_consumed",
                side_effect=ValueError("bundle consume failed"),
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
                raise AssertionError("expected dispatch run to fail after prepared transaction")


def _assert_safe_output_text(output: str) -> None:
    combined = output.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token == "phrase":
            if "phrase:" in combined:
                raise AssertionError("unsafe phrase field in output")
            if _VALID_PHRASE.lower() in combined:
                raise AssertionError("repair phrase leaked to output")
            continue
        if token.lower() in combined:
            raise AssertionError(f"unsafe token {token!r} in output")
    if _VALID_PHRASE in output:
        raise AssertionError("repair phrase leaked to output")


class _RepairApplyBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RepairApplyFixture()
        self.fixture.start()
        self.fixture.seed()
        self.evidence_home_patch = patch(
            "agent.coo.dispatch_cli_evidence.get_hermes_home",
            return_value=self.fixture.hermes_home,
        )
        self.evidence_home_patch.start()

    def tearDown(self) -> None:
        self.evidence_home_patch.stop()
        self.fixture.stop()

    def _assert_safe_output(self, output: str) -> None:
        _assert_safe_output_text(output)
        self.assertNotIn(str(self.fixture.pipeline_root), output)
        self.assertNotIn(str(self.fixture.hermes_home), output)


class TestPreparedCleanupApplySuccess(_RepairApplyBase):
    def test_prepared_clean_pair_apply_success(self) -> None:
        self.fixture.create_prepared_state()
        bundle_digest_before = _file_digest(self.fixture.bundle_path)
        confirmation_digest_before = _file_digest(self.fixture.confirmation_path)
        txn_before = read_consume_transaction(
            self.fixture.seeded["ticket"].ticket_id,
            self.fixture.seeded["confirmation"].confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        assert txn_before is not None
        self.assertEqual(txn_before.state, CONSUME_STATE_PREPARED)

        result = apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())
        self.assertTrue(result.applied)
        self.assertEqual(result.consume_state_before, CONSUME_STATE_PREPARED)
        self.assertEqual(result.consume_state_after, CONSUME_STATE_UNCONSUMED)
        self.assertEqual(result.repair_action, "repair_action_prepared_cleanup")
        self.assertFalse(result.bundle_consumed)
        self.assertFalse(result.confirmation_consumed)
        self.assertFalse(result.recovery_required)
        self.assertTrue(result.phrase_verified)

        status = assess_consume_status(
            ticket_id=self.fixture.seeded["ticket"].ticket_id,
            confirmation_id=self.fixture.seeded["confirmation"].confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_UNCONSUMED)
        replay = assert_consume_replay_allowed(
            ticket_id=self.fixture.seeded["ticket"].ticket_id,
            confirmation_id=self.fixture.seeded["confirmation"].confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(replay.consume_state, CONSUME_STATE_UNCONSUMED)

        self.assertEqual(_file_digest(self.fixture.bundle_path), bundle_digest_before)
        self.assertEqual(
            _file_digest(self.fixture.confirmation_path),
            confirmation_digest_before,
        )

        txn_after = read_consume_transaction(
            self.fixture.seeded["ticket"].ticket_id,
            self.fixture.seeded["confirmation"].confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        assert txn_after is not None
        self.assertEqual(txn_after.state, "aborted")
        self.assertEqual(txn_after.transaction_id, txn_before.transaction_id)
        self.assertEqual(txn_after.repair_attempt_id, result.repair_attempt_id)
        self.assertTrue(txn_after.phrase_verified)
        self.assertNotIn("confirmation_phrase", txn_after.reason)

        audit_path = self.fixture.repair_audit_dir / f"{result.repair_attempt_id}.json"
        self.assertTrue(audit_path.is_file())
        audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit_payload["consume_state_before"], CONSUME_STATE_PREPARED)
        self.assertEqual(audit_payload["consume_state_after"], CONSUME_STATE_UNCONSUMED)
        self.assertTrue(audit_payload["phrase_verified"])
        self.assertNotIn("phrase", audit_payload)

    def test_cli_apply_success_exit_zero(self) -> None:
        self.fixture.create_prepared_state()
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "consume",
                "repair",
                "apply",
                "--ticket-id",
                self.fixture.seeded["ticket"].ticket_id,
                "--confirmation-id",
                self.fixture.seeded["confirmation"].confirmation_id,
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
                "agent.coo.dispatch_consume_repair_lock.get_hermes_home",
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
        ):
            with patch("sys.stdout", buf):
                exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        self._assert_safe_output(buf.getvalue())


class TestPreparedCleanupApplyRejections(_RepairApplyBase):
    def test_wrong_phrase_zero_mutation(self) -> None:
        self.fixture.create_prepared_state()
        txn_digest_before = _file_digest(
            self.fixture.transaction_dir
            / f"{self.fixture.seeded['ticket'].ticket_id}__"
            f"{self.fixture.seeded['confirmation'].confirmation_id}.json"
        )
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(
                **self.fixture._apply_kwargs(phrase="WRONG-PHRASE"),
            )
        txn_digest_after = _file_digest(
            self.fixture.transaction_dir
            / f"{self.fixture.seeded['ticket'].ticket_id}__"
            f"{self.fixture.seeded['confirmation'].confirmation_id}.json"
        )
        self.assertEqual(txn_digest_before, txn_digest_after)
        self.assertFalse(any(self.fixture.repair_audit_dir.glob("*.json")))

    def test_missing_operator_fields_zero_mutation(self) -> None:
        self.fixture.create_prepared_state()
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs(operator_id="  "))

    def test_unconsumed_repair_not_required(self) -> None:
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())

    def test_prepared_bundle_consumed_rejected(self) -> None:
        self.fixture.create_prepared_state()
        mark_bundle_consumed(
            self.fixture.seeded["ticket"].ticket_id,
            bundle_dir=self.fixture.bundle_dir,
        )
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())

    def test_partial_rejected(self) -> None:
        ticket_id = self.fixture.seeded["ticket"].ticket_id
        confirmation_id = self.fixture.seeded["confirmation"].confirmation_id
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
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())

    def test_legacy_partial_rejected(self) -> None:
        mark_bundle_consumed(
            self.fixture.seeded["ticket"].ticket_id,
            bundle_dir=self.fixture.bundle_dir,
        )
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())

    def test_committed_rejected(self) -> None:
        ticket_id = self.fixture.seeded["ticket"].ticket_id
        confirmation_id = self.fixture.seeded["confirmation"].confirmation_id
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
        status = assess_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_COMMITTED)
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())

    def test_legacy_committed_rejected(self) -> None:
        mark_bundle_consumed(
            self.fixture.seeded["ticket"].ticket_id,
            bundle_dir=self.fixture.bundle_dir,
        )
        mark_confirmation_consumed_file(
            self.fixture.seeded["confirmation"].confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        status = assess_consume_status(
            ticket_id=self.fixture.seeded["ticket"].ticket_id,
            confirmation_id=self.fixture.seeded["confirmation"].confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_LEGACY_COMMITTED)
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())

    def test_corrupted_transaction_rejected(self) -> None:
        ticket_id = self.fixture.seeded["ticket"].ticket_id
        confirmation_id = self.fixture.seeded["confirmation"].confirmation_id
        path = self.fixture.transaction_dir / f"{ticket_id}__{confirmation_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())

    def test_transaction_id_mismatch_rejected(self) -> None:
        self.fixture.create_prepared_state()
        ticket_id = self.fixture.seeded["ticket"].ticket_id
        confirmation_id = self.fixture.seeded["confirmation"].confirmation_id
        eligibility = evaluate_consume_repair_eligibility(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
            **_OPERATOR,
        )
        tampered = CooDispatchConsumeRepairEligibility(
            consume_state=eligibility.consume_state,
            repair_eligible=eligibility.repair_eligible,
            repair_action=eligibility.repair_action,
            blocked_reason=eligibility.blocked_reason,
            transaction_id="tampered-transaction-id",
            execution_attempt_id=eligibility.execution_attempt_id,
            bundle_consumed=eligibility.bundle_consumed,
            confirmation_consumed=eligibility.confirmation_consumed,
            audit_present=eligibility.audit_present,
            evidence_present=eligibility.evidence_present,
            correlation_valid=eligibility.correlation_valid,
            evidence_success=eligibility.evidence_success,
            operator_valid=eligibility.operator_valid,
            mutation_planned=eligibility.mutation_planned,
        )
        with patch(
            "agent.coo.dispatch_consume_repair.evaluate_consume_repair_eligibility",
            return_value=tampered,
        ):
            with self.assertRaises(ValueError):
                apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())

    def test_path_traversal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            apply_prepared_transaction_cleanup(
                **self.fixture._apply_kwargs(ticket_id="../escape"),
            )


class TestPreparedCleanupApplySafety(_RepairApplyBase):
    def test_atomic_write_failure_preserves_prepared(self) -> None:
        self.fixture.create_prepared_state()
        ticket_id = self.fixture.seeded["ticket"].ticket_id
        confirmation_id = self.fixture.seeded["confirmation"].confirmation_id
        path = self.fixture.transaction_dir / f"{ticket_id}__{confirmation_id}.json"
        prepared_digest = _file_digest(path)
        with patch(
            "agent.coo.dispatch_consume_transaction._atomic_write_transaction",
            side_effect=OSError("write failed"),
        ):
            with self.assertRaises(ValueError):
                apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())
        self.assertEqual(_file_digest(path), prepared_digest)
        txn = read_consume_transaction(
            ticket_id,
            confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        assert txn is not None
        self.assertEqual(txn.state, CONSUME_STATE_PREPARED)
        self.assertFalse(any(self.fixture.repair_audit_dir.glob("*.json")))

    def test_concurrent_apply_fail_closed(self) -> None:
        self.fixture.create_prepared_state()
        ticket_id = self.fixture.seeded["ticket"].ticket_id
        confirmation_id = self.fixture.seeded["confirmation"].confirmation_id
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
            apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())
        release.set()
        thread.join(timeout=5)
        self.assertFalse(errors)

    def test_apply_preserves_bundle_confirmation_listing(self) -> None:
        self.fixture.create_prepared_state()
        bundle_listing_before = tuple(sorted(self.fixture.bundle_dir.glob("*.json")))
        confirmation_listing_before = tuple(
            sorted(self.fixture.confirmation_dir.glob("*.json"))
        )
        apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())
        self.assertEqual(
            tuple(sorted(self.fixture.bundle_dir.glob("*.json"))),
            bundle_listing_before,
        )
        self.assertEqual(
            tuple(sorted(self.fixture.confirmation_dir.glob("*.json"))),
            confirmation_listing_before,
        )

    def test_no_repository2_subprocess(self) -> None:
        self.fixture.create_prepared_state()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            apply_prepared_transaction_cleanup(**self.fixture._apply_kwargs())

    def test_dry_run_recovery_status_regression(self) -> None:
        from agent.coo.dispatch_cli_consume_repair import run_dispatch_consume_repair_dry_run
        from agent.coo.dispatch_cli_consume_recovery import assess_dispatch_consume_recovery
        from agent.coo.dispatch_cli_consume_status import summarize_dispatch_consume_status

        self.fixture.create_prepared_state()
        ticket_id = self.fixture.seeded["ticket"].ticket_id
        confirmation_id = self.fixture.seeded["confirmation"].confirmation_id
        eligibility, _ = run_dispatch_consume_repair_dry_run(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
            **_OPERATOR,
        )
        self.assertTrue(eligibility.repair_eligible)
        recovery = assess_dispatch_consume_recovery(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(recovery.consume_state, CONSUME_STATE_PREPARED)
        status = summarize_dispatch_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
        )
        self.assertEqual(status.consume_state, CONSUME_STATE_PREPARED)
