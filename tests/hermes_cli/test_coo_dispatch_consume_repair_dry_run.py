"""Phase 12N tests — dispatch consume repair dry-run eligibility."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_consume_recovery import (
    CooDispatchConsumeRecoveryAssessment,
)
from agent.coo.dispatch_cli_consume_repair import (
    format_dispatch_consume_repair_eligibility,
    run_dispatch_consume_repair_dry_run,
)
from agent.coo.dispatch_consume_repair import (
    BLOCKED_ALREADY_COMMITTED,
    BLOCKED_EVIDENCE_NOT_SUCCESSFUL,
    BLOCKED_LEGACY_ALREADY_COMMITTED,
    BLOCKED_LEGACY_PARTIAL_MANUAL_ONLY,
    BLOCKED_MISSING_AUDIT_FOR_PARTIAL,
    BLOCKED_MISSING_EVIDENCE_FOR_PARTIAL,
    BLOCKED_PREPARED_ARTIFACT_MISMATCH,
    BLOCKED_RETRY_DISPATCH_INSTEAD,
    REPAIR_ACTION_BLOCKED,
    REPAIR_ACTION_NOT_ALLOWED,
    REPAIR_ACTION_NOT_REQUIRED,
    REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE,
    REPAIR_ACTION_PREPARED_CLEANUP,
    _eligibility_from_recovery,
    evaluate_consume_repair_eligibility,
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

_OPERATOR = {
    "operator_id": "op-repair",
    "operator_name": "Repair Operator",
    "reason": "dry-run eligibility check",
}


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


def _dir_listing(root: Path) -> tuple[str, ...]:
    if not root.exists():
        return ()
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        )
    )


def _watched_roots(fixture: _CooDispatchRunFixture) -> tuple[Path, ...]:
    return (
        fixture.bundle_dir,
        fixture.confirmation_dir,
        fixture.transaction_dir,
        fixture.hermes_home / "coo" / "audit",
        fixture.hermes_home / "coo" / "execution-evidence",
    )


class _RepairDryRunFixture(_CooDispatchRunFixture):
    pass


class _RepairDryRunBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RepairDryRunFixture()
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

    def _evaluate(self, **overrides):
        ticket_id, confirmation_id = self._pair_ids()
        base = dict(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
            audit_dir=self.audit_dir,
            evidence_dir=self.evidence_dir,
            **_OPERATOR,
        )
        base.update(overrides)
        return evaluate_consume_repair_eligibility(**base)

    def _assert_safe_output(self, output: str) -> None:
        combined = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), combined)
        self.assertNotIn(str(self.fixture.pipeline_root), output)
        self.assertNotIn(str(self.fixture.hermes_home), output)


class TestRepairDryRunEligibility(_RepairDryRunBase):
    def test_unconsumed_retry_dispatch_instead(self) -> None:
        eligibility = self._evaluate()
        self.assertFalse(eligibility.repair_eligible)
        self.assertEqual(eligibility.repair_action, REPAIR_ACTION_NOT_REQUIRED)
        self.assertEqual(eligibility.blocked_reason, BLOCKED_RETRY_DISPATCH_INSTEAD)

    def test_prepared_clean_pair_eligible_cleanup(self) -> None:
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
        eligibility = self._evaluate()
        self.assertEqual(eligibility.consume_state, CONSUME_STATE_PREPARED)
        self.assertTrue(eligibility.repair_eligible)
        self.assertEqual(eligibility.repair_action, REPAIR_ACTION_PREPARED_CLEANUP)
        self.assertFalse(eligibility.mutation_planned)

    def test_prepared_with_consumed_artifact_blocked(self) -> None:
        assessment = CooDispatchConsumeRecoveryAssessment(
            consume_state=CONSUME_STATE_PREPARED,
            recovery_required=True,
            recommended_action="inspect_stale_transaction",
            transaction_id="txn-1",
            execution_attempt_id="attempt-1",
            bundle_consumed=True,
            confirmation_consumed=False,
            audit_present=False,
            evidence_present=False,
            correlation_valid=False,
            retry_allowed=False,
            recovery_risk=True,
            evidence_success=False,
        )
        eligibility = _eligibility_from_recovery(assessment, operator_valid=True)
        self.assertFalse(eligibility.repair_eligible)
        self.assertEqual(eligibility.repair_action, REPAIR_ACTION_BLOCKED)
        self.assertEqual(eligibility.blocked_reason, BLOCKED_PREPARED_ARTIFACT_MISMATCH)

    def test_partial_valid_success_evidence_eligible(self) -> None:
        clone = CooDispatchIsolatedCloneFixture()
        clone.start()
        try:
            with patch(
                "agent.coo.dispatch_cli_evidence.get_hermes_home",
                return_value=clone.hermes_home,
            ):
                seeded = clone.seed_bundle_and_confirmation()
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
                            ticket_id=seeded["ticket"].ticket_id,
                            confirmation_id=seeded["confirmation"].confirmation_id,
                            unlock_token_id=seeded["prepare"]["unlock_token"]["token_id"],
                            requester_id=seeded["ticket"].requester_id,
                            pipeline_root=str(clone.pipeline_root),
                            bundle_dir=clone.bundle_dir,
                            confirmation_dir=clone.confirmation_dir,
                            consume_transaction_dir=clone.transaction_dir,
                            merged_config=_enabled_executor_config(clone.pipeline_root),
                            subprocess_runner=_mock_runner_success,
                        )
                eligibility = evaluate_consume_repair_eligibility(
                    ticket_id=seeded["ticket"].ticket_id,
                    confirmation_id=seeded["confirmation"].confirmation_id,
                    bundle_dir=clone.bundle_dir,
                    confirmation_dir=clone.confirmation_dir,
                    transaction_dir=clone.transaction_dir,
                    audit_dir=clone.hermes_home / "coo" / "audit",
                    evidence_dir=clone.hermes_home / "coo" / "execution-evidence",
                    **_OPERATOR,
                )
        finally:
            clone.stop()
        self.assertEqual(eligibility.consume_state, CONSUME_STATE_PARTIAL)
        self.assertTrue(eligibility.repair_eligible)
        self.assertEqual(
            eligibility.repair_action,
            REPAIR_ACTION_PARTIAL_FORWARD_COMPLETE,
        )
        self.assertTrue(eligibility.evidence_success)

    def test_partial_missing_audit_blocked(self) -> None:
        clone = CooDispatchIsolatedCloneFixture()
        clone.start()
        try:
            with patch(
                "agent.coo.dispatch_cli_evidence.get_hermes_home",
                return_value=clone.hermes_home,
            ):
                seeded = clone.seed_bundle_and_confirmation()
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
                            ticket_id=seeded["ticket"].ticket_id,
                            confirmation_id=seeded["confirmation"].confirmation_id,
                            unlock_token_id=seeded["prepare"]["unlock_token"]["token_id"],
                            requester_id=seeded["ticket"].requester_id,
                            pipeline_root=str(clone.pipeline_root),
                            bundle_dir=clone.bundle_dir,
                            confirmation_dir=clone.confirmation_dir,
                            consume_transaction_dir=clone.transaction_dir,
                            merged_config=_enabled_executor_config(clone.pipeline_root),
                            subprocess_runner=_mock_runner_success,
                        )
                audit_dir = clone.hermes_home / "coo" / "audit"
                for path in list(audit_dir.glob("*.json")):
                    path.unlink()
                eligibility = evaluate_consume_repair_eligibility(
                    ticket_id=seeded["ticket"].ticket_id,
                    confirmation_id=seeded["confirmation"].confirmation_id,
                    bundle_dir=clone.bundle_dir,
                    confirmation_dir=clone.confirmation_dir,
                    transaction_dir=clone.transaction_dir,
                    audit_dir=audit_dir,
                    evidence_dir=clone.hermes_home / "coo" / "execution-evidence",
                    **_OPERATOR,
                )
        finally:
            clone.stop()
        self.assertEqual(eligibility.consume_state, CONSUME_STATE_PARTIAL)
        self.assertFalse(eligibility.repair_eligible)
        self.assertEqual(eligibility.repair_action, REPAIR_ACTION_BLOCKED)
        self.assertEqual(eligibility.blocked_reason, BLOCKED_MISSING_AUDIT_FOR_PARTIAL)

    def test_partial_missing_evidence_blocked(self) -> None:
        clone = CooDispatchIsolatedCloneFixture()
        clone.start()
        try:
            with patch(
                "agent.coo.dispatch_cli_evidence.get_hermes_home",
                return_value=clone.hermes_home,
            ):
                seeded = clone.seed_bundle_and_confirmation()
                with (
                    patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
                    patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
                    patch(
                        "agent.coo.dispatch_consume_transaction.mark_confirmation_consumed_file",
                        side_effect=ValueError("confirmation consume failed"),
                    ),
                ):
                    from agent.coo.dispatch_cli_run import execute_coo_dispatch_run
                    from agent.coo.dispatch_consume_transaction import read_consume_transaction

                    with self.assertRaises(ValueError):
                        execute_coo_dispatch_run(
                            ticket_id=seeded["ticket"].ticket_id,
                            confirmation_id=seeded["confirmation"].confirmation_id,
                            unlock_token_id=seeded["prepare"]["unlock_token"]["token_id"],
                            requester_id=seeded["ticket"].requester_id,
                            pipeline_root=str(clone.pipeline_root),
                            bundle_dir=clone.bundle_dir,
                            confirmation_dir=clone.confirmation_dir,
                            consume_transaction_dir=clone.transaction_dir,
                            merged_config=_enabled_executor_config(clone.pipeline_root),
                            subprocess_runner=_mock_runner_success,
                        )
                txn = read_consume_transaction(
                    seeded["ticket"].ticket_id,
                    seeded["confirmation"].confirmation_id,
                    transaction_dir=clone.transaction_dir,
                )
                assert txn is not None
                evidence_dir = clone.hermes_home / "coo" / "execution-evidence"
                (evidence_dir / f"{txn.execution_attempt_id}.meta.json").unlink()
                eligibility = evaluate_consume_repair_eligibility(
                    ticket_id=seeded["ticket"].ticket_id,
                    confirmation_id=seeded["confirmation"].confirmation_id,
                    bundle_dir=clone.bundle_dir,
                    confirmation_dir=clone.confirmation_dir,
                    transaction_dir=clone.transaction_dir,
                    audit_dir=clone.hermes_home / "coo" / "audit",
                    evidence_dir=evidence_dir,
                    **_OPERATOR,
                )
        finally:
            clone.stop()
        self.assertEqual(eligibility.consume_state, CONSUME_STATE_PARTIAL)
        self.assertFalse(eligibility.repair_eligible)
        self.assertEqual(eligibility.blocked_reason, BLOCKED_MISSING_EVIDENCE_FOR_PARTIAL)

    def test_partial_non_zero_evidence_blocked(self) -> None:
        clone = CooDispatchIsolatedCloneFixture()
        clone.start()
        try:
            with patch(
                "agent.coo.dispatch_cli_evidence.get_hermes_home",
                return_value=clone.hermes_home,
            ):
                seeded = clone.seed_bundle_and_confirmation()
                with (
                    patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
                    patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
                    patch(
                        "agent.coo.dispatch_consume_transaction.mark_confirmation_consumed_file",
                        side_effect=ValueError("confirmation consume failed"),
                    ),
                ):
                    from agent.coo.dispatch_cli_run import execute_coo_dispatch_run
                    from agent.coo.dispatch_consume_transaction import read_consume_transaction

                    with self.assertRaises(ValueError):
                        execute_coo_dispatch_run(
                            ticket_id=seeded["ticket"].ticket_id,
                            confirmation_id=seeded["confirmation"].confirmation_id,
                            unlock_token_id=seeded["prepare"]["unlock_token"]["token_id"],
                            requester_id=seeded["ticket"].requester_id,
                            pipeline_root=str(clone.pipeline_root),
                            bundle_dir=clone.bundle_dir,
                            confirmation_dir=clone.confirmation_dir,
                            consume_transaction_dir=clone.transaction_dir,
                            merged_config=_enabled_executor_config(clone.pipeline_root),
                            subprocess_runner=_mock_runner_success,
                        )
                txn = read_consume_transaction(
                    seeded["ticket"].ticket_id,
                    seeded["confirmation"].confirmation_id,
                    transaction_dir=clone.transaction_dir,
                )
                assert txn is not None
                evidence_dir = clone.hermes_home / "coo" / "execution-evidence"
                meta_path = evidence_dir / f"{txn.execution_attempt_id}.meta.json"
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
                payload["exit_code"] = 1
                meta_path.write_text(json.dumps(payload), encoding="utf-8")
                eligibility = evaluate_consume_repair_eligibility(
                    ticket_id=seeded["ticket"].ticket_id,
                    confirmation_id=seeded["confirmation"].confirmation_id,
                    bundle_dir=clone.bundle_dir,
                    confirmation_dir=clone.confirmation_dir,
                    transaction_dir=clone.transaction_dir,
                    audit_dir=clone.hermes_home / "coo" / "audit",
                    evidence_dir=evidence_dir,
                    **_OPERATOR,
                )
        finally:
            clone.stop()
        self.assertEqual(eligibility.consume_state, CONSUME_STATE_PARTIAL)
        self.assertFalse(eligibility.repair_eligible)
        self.assertEqual(eligibility.blocked_reason, BLOCKED_EVIDENCE_NOT_SUCCESSFUL)

    def test_legacy_partial_blocked(self) -> None:
        from agent.coo.dispatch_bundle_store import mark_bundle_consumed

        ticket_id, confirmation_id = self._pair_ids()
        mark_bundle_consumed(ticket_id, bundle_dir=self.fixture.bundle_dir)
        eligibility = self._evaluate()
        self.assertEqual(eligibility.consume_state, CONSUME_STATE_LEGACY_PARTIAL)
        self.assertFalse(eligibility.repair_eligible)
        self.assertEqual(eligibility.repair_action, REPAIR_ACTION_BLOCKED)
        self.assertEqual(eligibility.blocked_reason, BLOCKED_LEGACY_PARTIAL_MANUAL_ONLY)

    def test_committed_not_allowed(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        execute_consume_transaction(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            execution_attempt_id="attempt-committed",
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            transaction_dir=self.fixture.transaction_dir,
        )
        eligibility = self._evaluate()
        self.assertEqual(eligibility.consume_state, CONSUME_STATE_COMMITTED)
        self.assertFalse(eligibility.repair_eligible)
        self.assertEqual(eligibility.repair_action, REPAIR_ACTION_NOT_ALLOWED)
        self.assertEqual(eligibility.blocked_reason, BLOCKED_ALREADY_COMMITTED)

    def test_legacy_committed_not_allowed(self) -> None:
        from agent.coo.dispatch_bundle_store import mark_bundle_consumed

        ticket_id, confirmation_id = self._pair_ids()
        mark_bundle_consumed(ticket_id, bundle_dir=self.fixture.bundle_dir)
        mark_confirmation_consumed_file(
            confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        eligibility = self._evaluate()
        self.assertEqual(eligibility.consume_state, CONSUME_STATE_LEGACY_COMMITTED)
        self.assertFalse(eligibility.repair_eligible)
        self.assertEqual(eligibility.blocked_reason, BLOCKED_LEGACY_ALREADY_COMMITTED)


def _mock_runner_failure(argv, cwd, env, timeout):
    return 1, "", "failed"


class TestRepairDryRunSafety(_RepairDryRunBase):
    def test_invalid_operator_fields_rejected(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        with self.assertRaises(ValueError):
            evaluate_consume_repair_eligibility(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                operator_id="",
                operator_name="Name",
                reason="reason",
            )

    def test_corrupted_transaction_rejected(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        path = self.fixture.transaction_dir / f"{ticket_id}__{confirmation_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            self._evaluate()
        self.assertIn("corrupt", str(exc.exception).lower())

    def test_path_traversal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._evaluate(ticket_id="../escape")

    def test_symlink_escape_rejected(self) -> None:
        ticket_id, confirmation_id = self._pair_ids()
        outside = Path(self.fixture.tmp.name) / "outside-repair"
        outside.mkdir()
        link_dir = self.fixture.hermes_home / "coo" / "repair-link"
        link_dir.parent.mkdir(parents=True, exist_ok=True)
        link_dir.symlink_to(outside)
        with self.assertRaises(ValueError):
            self._evaluate(transaction_dir=link_dir)

    def test_correlation_mismatch_rejected(self) -> None:
        clone = CooDispatchIsolatedCloneFixture()
        clone.start()
        try:
            with patch(
                "agent.coo.dispatch_cli_evidence.get_hermes_home",
                return_value=clone.hermes_home,
            ):
                seeded = clone.seed_bundle_and_confirmation()
                result = run_clone_full_path_execute(clone, seeded)
                audit_dir = clone.hermes_home / "coo" / "audit"
                for path in audit_dir.glob("*.json"):
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if payload.get("execution_attempt_id") == result.execution_attempt_id:
                        payload["confirmation_id"] = "wrong-id"
                        path.write_text(json.dumps(payload), encoding="utf-8")
                        break
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
                            ticket_id=seeded["ticket"].ticket_id,
                            confirmation_id=seeded["confirmation"].confirmation_id,
                            unlock_token_id=seeded["prepare"]["unlock_token"]["token_id"],
                            requester_id=seeded["ticket"].requester_id,
                            pipeline_root=str(clone.pipeline_root),
                            bundle_dir=clone.bundle_dir,
                            confirmation_dir=clone.confirmation_dir,
                            consume_transaction_dir=clone.transaction_dir,
                            merged_config=_enabled_executor_config(clone.pipeline_root),
                            subprocess_runner=_mock_runner_success,
                        )
                with self.assertRaises(ValueError) as exc:
                    evaluate_consume_repair_eligibility(
                        ticket_id=seeded["ticket"].ticket_id,
                        confirmation_id=seeded["confirmation"].confirmation_id,
                        bundle_dir=clone.bundle_dir,
                        confirmation_dir=clone.confirmation_dir,
                        transaction_dir=clone.transaction_dir,
                        audit_dir=audit_dir,
                        evidence_dir=clone.hermes_home / "coo" / "execution-evidence",
                        **_OPERATOR,
                    )
        finally:
            clone.stop()
        self.assertIn("mismatch", str(exc.exception).lower())

    def test_files_unchanged_after_dry_run(self) -> None:
        before_digest = {str(root): _dir_digest(root) for root in _watched_roots(self.fixture)}
        before_listing = {str(root): _dir_listing(root) for root in _watched_roots(self.fixture)}
        self._evaluate()
        after_digest = {str(root): _dir_digest(root) for root in _watched_roots(self.fixture)}
        after_listing = {str(root): _dir_listing(root) for root in _watched_roots(self.fixture)}
        self.assertEqual(before_digest, after_digest)
        self.assertEqual(before_listing, after_listing)

    def test_cli_output_safe_and_exit_codes(self) -> None:
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
        self.assertEqual(exit_code, 1)
        output = format_dispatch_consume_repair_eligibility(eligibility)
        self._assert_safe_output(output)
        self.assertIn("mutation_planned: false", output)

        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "consume",
                "repair",
                "dry-run",
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
            ]
        )
        stderr = io.StringIO()
        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            cli_exit = args.handler(args)
        self.assertEqual(cli_exit, 1)
        self._assert_safe_output(stdout.getvalue())

    def test_existing_recovery_and_status_unchanged(self) -> None:
        from agent.coo.dispatch_cli_consume_recovery import (
            assess_dispatch_consume_recovery,
            format_dispatch_consume_recovery_assessment,
        )
        from agent.coo.dispatch_cli_consume_status import (
            format_dispatch_consume_status_summary,
            summarize_dispatch_consume_status,
        )

        ticket_id, confirmation_id = self._pair_ids()
        status_output = format_dispatch_consume_status_summary(
            summarize_dispatch_consume_status(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                transaction_dir=self.fixture.transaction_dir,
            )
        )
        recovery_output = format_dispatch_consume_recovery_assessment(
            assess_dispatch_consume_recovery(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                transaction_dir=self.fixture.transaction_dir,
            )
        )
        self.assertIn("consume_state: unconsumed", status_output)
        self.assertIn("recommended_action: retry_allowed", recovery_output)
        self.assertNotIn("repair_eligible", recovery_output)


if __name__ == "__main__":
    unittest.main()
