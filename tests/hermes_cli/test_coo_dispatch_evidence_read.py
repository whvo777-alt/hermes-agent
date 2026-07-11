"""Phase 12J tests — execution evidence read CLI and attempt correlation."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import read_bundle
from agent.coo.dispatch_cli_evidence import (
    find_dispatch_evidence_attempts_for_ticket,
    format_dispatch_evidence_find,
    format_dispatch_evidence_summary,
    summarize_dispatch_evidence_attempt,
)
from agent.coo.execution_dispatch_runtime import DispatchExecutionRunStatus
from hermes_cli.coo_dispatch import build_coo_dispatch_parser, run_coo_dispatch_from_args
from tests.hermes_cli.coo_dispatch_isolated_clone_fixture import (
    CLONE_BEHAVIOR_FAILURE,
    CLONE_BEHAVIOR_PARTIAL,
    CLONE_BEHAVIOR_SUCCESS,
    CLONE_BEHAVIOR_TIMEOUT,
    CLONE_BEHAVIOR_VERBOSE,
    CooDispatchIsolatedCloneFixture,
    clone_partial_output_path,
    run_clone_full_path_execute,
)
from tests.hermes_cli.coo_dispatch_isolated_fixture import build_run_args
from tests.hermes_cli.coo_dispatch_isolated_fixture import bounded_dispatch_config


class _EvidenceReadBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CooDispatchIsolatedCloneFixture()
        self.fixture.start()
        self.evidence_home_patch = patch(
            "agent.coo.dispatch_cli_evidence.get_hermes_home",
            return_value=self.fixture.hermes_home,
        )
        self.evidence_home_patch.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.evidence_dir = self.fixture.hermes_home / "coo" / "execution-evidence"
        self.audit_dir = self.fixture.hermes_home / "coo" / "audit"
        self.bundle_dir = self.fixture.bundle_dir
        self.confirmation_dir = self.fixture.confirmation_dir

    def tearDown(self) -> None:
        self.evidence_home_patch.stop()
        self.fixture.stop()

    def _summary(self, attempt_id: str):
        return summarize_dispatch_evidence_attempt(
            attempt_id,
            evidence_dir=self.evidence_dir,
            audit_dir=self.audit_dir,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )


class TestEvidenceReadAttempts(_EvidenceReadBase):
    def test_success_attempt_show(self) -> None:
        result = run_clone_full_path_execute(self.fixture, self.seeded)
        self.assertTrue(result.execution_attempt_id)
        summary = self._summary(result.execution_attempt_id)

        self.assertEqual(summary.execution_attempt_id, result.execution_attempt_id)
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.exit_code, 0)
        self.assertTrue(summary.audit_present)
        self.assertTrue(summary.evidence_files_present)
        self.assertIn("bundle=true", summary.consumed)
        self.assertIn("confirmation=true", summary.consumed)

        output = format_dispatch_evidence_summary(summary)
        self.assertIn(f"execution_attempt_id: {result.execution_attempt_id}", output)
        self.assertIn("status: completed", output)
        self.assertNotIn("pipeline.js", output)
        self.assertNotIn("argv", output)
        self.assertNotIn("cwd", output)
        self.assertNotIn("env", output)
        self.assertNotIn("clone-ok", output)

    def test_non_zero_attempt_show(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_FAILURE,
        )
        summary = self._summary(result.execution_attempt_id)
        self.assertFalse(result.consumed)
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.exit_code, 3)
        self.assertEqual(summary.failure_reason, "exit_non_zero")
        self.assertIn("bundle=false", summary.consumed)

    def test_timeout_attempt_show(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_TIMEOUT,
            policy_max_runtime_seconds=1,
        )
        summary = self._summary(result.execution_attempt_id)
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.exit_code, 124)
        self.assertEqual(summary.failure_reason, "timeout")

    def test_partial_output_attempt_show(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_PARTIAL,
        )
        self.assertTrue(clone_partial_output_path(self.fixture.pipeline_root).is_file())
        summary = self._summary(result.execution_attempt_id)
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.failure_reason, "exit_non_zero")
        self.assertIn("bundle=false", summary.consumed)

    def test_stdout_stderr_truncate_flags_without_body(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_VERBOSE,
            harness_max_output_bytes=128,
        )
        summary = self._summary(result.execution_attempt_id)
        self.assertTrue(summary.stdout_truncated)
        output = format_dispatch_evidence_summary(summary)
        self.assertIn("stdout_truncated: true", output)
        self.assertNotIn("xxxxxxxxxxxxxxxx", output)


class TestEvidenceReadFindRetryReplay(_EvidenceReadBase):
    def test_find_by_ticket_newest_first_and_retry_has_distinct_attempt_ids(self) -> None:
        first = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_FAILURE,
        )
        second = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_SUCCESS,
        )
        self.assertNotEqual(first.execution_attempt_id, second.execution_attempt_id)

        ticket_id = self.seeded["ticket"].ticket_id
        entries = find_dispatch_evidence_attempts_for_ticket(
            ticket_id,
            evidence_dir=self.evidence_dir,
            audit_dir=self.audit_dir,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        self.assertEqual([entry.execution_attempt_id for entry in entries], [
            second.execution_attempt_id,
            first.execution_attempt_id,
        ])
        rendered = format_dispatch_evidence_find(ticket_id, entries)
        self.assertIn("attempt_count: 2", rendered)
        self.assertNotIn("pipeline.js", rendered)
        self.assertNotIn("cwd", rendered)

    def test_replay_rejected_does_not_create_new_attempt_or_evidence(self) -> None:
        first = run_clone_full_path_execute(self.fixture, self.seeded)
        before = sorted(self.evidence_dir.glob("*.meta.json"))
        args = build_run_args(self.seeded, self.fixture.pipeline_root)
        stderr = io.StringIO()
        with (
            patch.object(sys, "stderr", stderr),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = run_coo_dispatch_from_args(
                args,
                subprocess_runner=lambda *a: (0, "", ""),
                merged_config=bounded_dispatch_config(self.fixture.pipeline_root),
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("consumed", stderr.getvalue().lower())
        after = sorted(self.evidence_dir.glob("*.meta.json"))
        self.assertEqual(before, after)
        self.assertEqual(before[0].stem, f"{first.execution_attempt_id}.meta")


class TestEvidenceReadFailClosed(_EvidenceReadBase):
    def test_audit_evidence_attempt_id_mismatch_rejected(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_FAILURE,
        )
        meta_path = self.evidence_dir / f"{result.execution_attempt_id}.meta.json"
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["execution_attempt_id"] = "00000000-0000-0000-0000-000000000000"
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(ValueError) as exc:
            self._summary(result.execution_attempt_id)
        self.assertIn("mismatch", str(exc.exception).lower())

    def test_missing_meta_rejected(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_FAILURE,
        )
        (self.evidence_dir / f"{result.execution_attempt_id}.meta.json").unlink()
        with self.assertRaises(KeyError):
            self._summary(result.execution_attempt_id)

    def test_corrupted_meta_rejected(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_FAILURE,
        )
        (self.evidence_dir / f"{result.execution_attempt_id}.meta.json").write_text(
            "{not-json",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError) as exc:
            self._summary(result.execution_attempt_id)
        self.assertIn("corrupted", str(exc.exception).lower())

    def test_path_traversal_attempt_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._summary("../evil")

    def test_symlink_escape_evidence_dir_rejected(self) -> None:
        outside = self.fixture.pipeline_root.parent / "outside-evidence"
        outside.mkdir()
        link = self.fixture.hermes_home / "coo" / "evidence-link"
        link.symlink_to(outside)
        with self.assertRaises(ValueError) as exc:
            summarize_dispatch_evidence_attempt(
                "missing",
                evidence_dir=link,
                audit_dir=self.audit_dir,
            )
        self.assertIn("Hermes home", str(exc.exception))

    def test_legacy_evidence_without_attempt_id_rejected(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_FAILURE,
        )
        meta_path = self.evidence_dir / f"{result.execution_attempt_id}.meta.json"
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload.pop("execution_attempt_id")
        meta_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            self._summary(result.execution_attempt_id)
        self.assertIn("legacy", str(exc.exception).lower())

    def test_cli_evidence_show_and_find_are_safe(self) -> None:
        result = run_clone_full_path_execute(self.fixture, self.seeded)
        parser = build_coo_dispatch_parser()
        show_args = parser.parse_args(
            [
                "evidence",
                "show",
                "--execution-attempt-id",
                result.execution_attempt_id,
            ]
        )
        find_args = parser.parse_args(
            [
                "evidence",
                "find",
                "--ticket-id",
                self.seeded["ticket"].ticket_id,
            ]
        )

        for args in (show_args, find_args):
            stdout = io.StringIO()
            with patch.object(sys, "stdout", stdout):
                exit_code = args.handler(args)
            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("execution_attempt_id:", output)
            self.assertNotIn("pipeline.js", output)
            self.assertNotIn("argv", output)
            self.assertNotIn("cwd", output)
            self.assertNotIn("env", output)


if __name__ == "__main__":
    unittest.main()

