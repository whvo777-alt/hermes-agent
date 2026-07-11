"""Phase 12L tests — dispatch consume recovery assessment CLI."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import mark_bundle_consumed
from agent.coo.dispatch_cli_consume_recovery import (
    RECOMMENDED_ACTION_INSPECT_STALE_TRANSACTION,
    RECOMMENDED_ACTION_MANUAL_RECOVERY_REQUIRED,
    RECOMMENDED_ACTION_NONE,
    RECOMMENDED_ACTION_RETRY_ALLOWED,
    assess_dispatch_consume_recovery,
    format_dispatch_consume_recovery_assessment,
)
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_COMMITTED,
    CONSUME_STATE_LEGACY_COMMITTED,
    CONSUME_STATE_LEGACY_PARTIAL,
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_PREPARED,
    CONSUME_STATE_UNCONSUMED,
    execute_consume_transaction,
)
from agent.coo.production_executor_confirmation import mark_confirmation_consumed_file
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.coo_dispatch_isolated_clone_fixture import (
    CooDispatchIsolatedCloneFixture,
    run_clone_full_path_execute,
)
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


def _watched_roots(fixture: _CooDispatchRunFixture) -> tuple[Path, ...]:
    return (
        fixture.bundle_dir,
        fixture.confirmation_dir,
        fixture.transaction_dir,
        fixture.hermes_home / "coo" / "audit",
        fixture.hermes_home / "coo" / "execution-evidence",
    )


class _RecoveryAssessmentFixture(_CooDispatchRunFixture):
    pass


class _RecoveryAssessmentBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RecoveryAssessmentFixture()
        self.fixture.start()
        self.evidence_home_patch = patch(
            "agent.coo.dispatch_cli_evidence.get_hermes_home",
            return_value=self.fixture.hermes_home,
        )
        self.evidence_home_patch.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.audit_dir = self.fixture.hermes_home / "coo" / "audit"
        self.evidence_dir = self.fixture.hermes_home / "coo" / "execution-evidence"

    def tearDown(self) -> None:
        self.evidence_home_patch.stop()
        self.fixture.stop()

    def _pair_ids(self) -> tuple[str, str]:
        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        return ticket.ticket_id, confirmation.confirmation_id

    def _assess(self, **overrides):
        ticket_id, confirmation_id = self._pair_ids()
        base = dict(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
            audit_dir=self.audit_dir,
            evidence_dir=self.evidence_dir,
        )
        base.update(overrides)
        return assess_dispatch_consume_recovery(**base)

    def _assert_safe_output(self, output: str) -> None:
        combined = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), combined)
        self.assertNotIn(str(self.fixture.pipeline_root), output)
        self.assertNotIn(str(self.fixture.hermes_home), output)


class TestRecoveryAssessmentStates(_RecoveryAssessmentBase):
    def test_unconsumed_retry_allowed(self) -> None:
        assessment = self._assess()
        self.assertEqual(assessment.consume_state, CONSUME_STATE_UNCONSUMED)
        self.assertFalse(assessment.recovery_required)
        self.assertEqual(assessment.recommended_action, RECOMMENDED_ACTION_RETRY_ALLOWED)
        self.assertTrue(assessment.retry_allowed)
        self.assertFalse(assessment.recovery_risk)

    def test_committed_no_recovery(self) -> None:
        clone = CooDispatchIsolatedCloneFixture()
        clone.start()
        try:
            clone_evidence_patch = patch(
                "agent.coo.dispatch_cli_evidence.get_hermes_home",
                return_value=clone.hermes_home,
            )
            with clone_evidence_patch:
                seeded = clone.seed_bundle_and_confirmation()
                result = run_clone_full_path_execute(clone, seeded)
                assessment = assess_dispatch_consume_recovery(
                    ticket_id=seeded["ticket"].ticket_id,
                    confirmation_id=seeded["confirmation"].confirmation_id,
                    bundle_dir=clone.bundle_dir,
                    confirmation_dir=clone.confirmation_dir,
                    transaction_dir=clone.transaction_dir,
                    audit_dir=clone.hermes_home / "coo" / "audit",
                    evidence_dir=clone.hermes_home / "coo" / "execution-evidence",
                )
        finally:
            clone.stop()
        self.assertEqual(assessment.consume_state, CONSUME_STATE_COMMITTED)
        self.assertFalse(assessment.recovery_required)
        self.assertEqual(assessment.recommended_action, RECOMMENDED_ACTION_NONE)
        self.assertFalse(assessment.retry_allowed)
        self.assertTrue(assessment.audit_present)
        self.assertTrue(assessment.evidence_present)
        self.assertTrue(assessment.correlation_valid)
        self.assertFalse(assessment.recovery_risk)
        self.assertEqual(assessment.execution_attempt_id, result.execution_attempt_id)

    def test_legacy_committed_no_recovery(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        mark_bundle_consumed(ticket_id, bundle_dir=self.fixture.bundle_dir)
        mark_confirmation_consumed_file(
            confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        assessment = self._assess()
        self.assertEqual(assessment.consume_state, CONSUME_STATE_LEGACY_COMMITTED)
        self.assertFalse(assessment.recovery_required)
        self.assertEqual(assessment.recommended_action, RECOMMENDED_ACTION_NONE)
        self.assertTrue(assessment.recovery_risk)

    def test_prepared_stale_inspection(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_consume_transaction.mark_bundle_consumed",
                side_effect=ValueError("bundle consume failed"),
            ),
        ):
            from agent.coo.dispatch_cli_run import execute_coo_dispatch_run

            with self.assertRaises(ValueError):
                execute_coo_dispatch_run(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    unlock_token_id=self.seeded["prepare"]["unlock_token"]["token_id"],
                    requester_id=self.seeded["ticket"].requester_id,
                    pipeline_root=str(self.fixture.pipeline_root),
                    bundle_dir=self.fixture.bundle_dir,
                    confirmation_dir=self.fixture.confirmation_dir,
                    consume_transaction_dir=self.fixture.transaction_dir,
                    merged_config=_enabled_executor_config(self.fixture.pipeline_root),
                    subprocess_runner=_mock_runner_success,
                )
        assessment = self._assess()
        self.assertEqual(assessment.consume_state, CONSUME_STATE_PREPARED)
        self.assertTrue(assessment.recovery_required)
        self.assertEqual(
            assessment.recommended_action,
            RECOMMENDED_ACTION_INSPECT_STALE_TRANSACTION,
        )
        self.assertFalse(assessment.retry_allowed)

    def test_partial_manual_recovery(self) -> None:
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
                    unlock_token_id=self.seeded["prepare"]["unlock_token"]["token_id"],
                    requester_id=self.seeded["ticket"].requester_id,
                    pipeline_root=str(self.fixture.pipeline_root),
                    bundle_dir=self.fixture.bundle_dir,
                    confirmation_dir=self.fixture.confirmation_dir,
                    consume_transaction_dir=self.fixture.transaction_dir,
                    merged_config=_enabled_executor_config(self.fixture.pipeline_root),
                    subprocess_runner=_mock_runner_success,
                )
        assessment = self._assess()
        self.assertEqual(assessment.consume_state, CONSUME_STATE_PARTIAL)
        self.assertTrue(assessment.recovery_required)
        self.assertEqual(
            assessment.recommended_action,
            RECOMMENDED_ACTION_MANUAL_RECOVERY_REQUIRED,
        )

    def test_legacy_partial_manual_recovery(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        mark_bundle_consumed(ticket_id, bundle_dir=self.fixture.bundle_dir)
        assessment = self._assess()
        self.assertEqual(assessment.consume_state, CONSUME_STATE_LEGACY_PARTIAL)
        self.assertTrue(assessment.recovery_required)
        self.assertEqual(
            assessment.recommended_action,
            RECOMMENDED_ACTION_MANUAL_RECOVERY_REQUIRED,
        )
        self.assertTrue(assessment.recovery_risk)


class TestRecoveryAssessmentCorrelation(_RecoveryAssessmentBase):
    def test_success_correlation_normal(self) -> None:
        clone = CooDispatchIsolatedCloneFixture()
        clone.start()
        try:
            with (
                patch(
                    "agent.coo.dispatch_cli_evidence.get_hermes_home",
                    return_value=clone.hermes_home,
                ),
            ):
                seeded = clone.seed_bundle_and_confirmation()
                run_clone_full_path_execute(clone, seeded)
                assessment = assess_dispatch_consume_recovery(
                    ticket_id=seeded["ticket"].ticket_id,
                    confirmation_id=seeded["confirmation"].confirmation_id,
                    bundle_dir=clone.bundle_dir,
                    confirmation_dir=clone.confirmation_dir,
                    transaction_dir=clone.transaction_dir,
                    audit_dir=clone.hermes_home / "coo" / "audit",
                    evidence_dir=clone.hermes_home / "coo" / "execution-evidence",
                )
        finally:
            clone.stop()
        self.assertTrue(assessment.correlation_valid)
        self.assertFalse(assessment.recovery_risk)

    def test_audit_confirmation_mismatch_rejected(self) -> None:
        clone = CooDispatchIsolatedCloneFixture()
        clone.start()
        try:
            with (
                patch(
                    "agent.coo.dispatch_cli_evidence.get_hermes_home",
                    return_value=clone.hermes_home,
                ),
            ):
                seeded = clone.seed_bundle_and_confirmation()
                result = run_clone_full_path_execute(clone, seeded)
                audit_dir = clone.hermes_home / "coo" / "audit"
                for path in audit_dir.glob("*.json"):
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if payload.get("execution_attempt_id") == result.execution_attempt_id:
                        payload["confirmation_id"] = "wrong-confirmation-id"
                        path.write_text(json.dumps(payload), encoding="utf-8")
                        break
                with self.assertRaises(ValueError) as exc:
                    assess_dispatch_consume_recovery(
                        ticket_id=seeded["ticket"].ticket_id,
                        confirmation_id=seeded["confirmation"].confirmation_id,
                        bundle_dir=clone.bundle_dir,
                        confirmation_dir=clone.confirmation_dir,
                        transaction_dir=clone.transaction_dir,
                        audit_dir=audit_dir,
                        evidence_dir=clone.hermes_home / "coo" / "execution-evidence",
                    )
        finally:
            clone.stop()
        self.assertIn("mismatch", str(exc.exception).lower())

    def test_consumed_without_success_evidence_shows_risk(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        execute_consume_transaction(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            execution_attempt_id="orphan-attempt-id",
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
        )
        assessment = self._assess()
        self.assertEqual(assessment.consume_state, CONSUME_STATE_COMMITTED)
        self.assertFalse(assessment.correlation_valid)
        self.assertTrue(assessment.recovery_risk)


class TestRecoveryAssessmentSafety(_RecoveryAssessmentBase):
    def test_corrupted_transaction_rejected(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        path = self.fixture.transaction_dir / f"{ticket_id}__{confirmation_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            self._assess()
        self.assertIn("corrupt", str(exc.exception).lower())

    def test_path_traversal_ticket_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._assess(ticket_id="../escape")

    def test_symlink_escape_rejected(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        outside = Path(self.fixture.tmp.name) / "outside-recovery"
        outside.mkdir()
        link_dir = self.fixture.hermes_home / "coo" / "recovery-link"
        link_dir.parent.mkdir(parents=True, exist_ok=True)
        link_dir.symlink_to(outside)
        with self.assertRaises(ValueError):
            self._assess(transaction_dir=link_dir)

    def test_assessment_does_not_mutate_files(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        before = {str(root): _dir_digest(root) for root in _watched_roots(self.fixture)}
        self._assess()
        after = {str(root): _dir_digest(root) for root in _watched_roots(self.fixture)}
        self.assertEqual(before, after)

    def test_cli_output_is_safe(self) -> None:
        assessment = self._assess()
        output = format_dispatch_consume_recovery_assessment(assessment)
        self._assert_safe_output(output)

        ticket_id, confirmation_id = self._pair_ids()
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "consume",
                "recovery",
                "--ticket-id",
                ticket_id,
                "--confirmation-id",
                confirmation_id,
            ]
        )
        before = {str(root): _dir_digest(root) for root in _watched_roots(self.fixture)}
        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout):
            exit_code = args.handler(args)
        after = {str(root): _dir_digest(root) for root in _watched_roots(self.fixture)}
        self.assertEqual(exit_code, 0)
        self.assertEqual(before, after)
        self._assert_safe_output(stdout.getvalue())
        self.assertIn("recommended_action: retry_allowed", stdout.getvalue())

    def test_existing_consume_status_unchanged(self) -> None:
        from agent.coo.dispatch_cli_consume_status import (
            format_dispatch_consume_status_summary,
            summarize_dispatch_consume_status,
        )

        ticket_id, confirmation_id = self._pair_ids()
        summary = summarize_dispatch_consume_status(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            transaction_dir=self.fixture.transaction_dir,
        )
        output = format_dispatch_consume_status_summary(summary)
        self.assertIn("consume_state: unconsumed", output)
        self.assertNotIn("recommended_action", output)


if __name__ == "__main__":
    unittest.main()
