"""Phase 12I tests — isolated clone-shaped fixture full-path drill."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.bounded_subprocess_runner import BoundedSubprocessRunnerError
from agent.coo.dispatch_bundle_store import read_bundle
from agent.coo.dispatch_cli_runner_injection import DISPATCH_RUNNER_NOT_CONFIGURED
from agent.coo.dispatch_cli_run import execute_coo_dispatch_run
from agent.coo.execution_dispatch_runtime import DispatchExecutionRunStatus
from agent.coo.production_executor_confirmation import read_confirmation
from agent.coo.production_executor_factory import _TIMEOUT_EXIT_CODE
from hermes_cli.coo_dispatch import run_coo_dispatch_from_args
from tests.hermes_cli.coo_dispatch_isolated_clone_fixture import (
    CLONE_BEHAVIOR_ENV_PROBE,
    CLONE_BEHAVIOR_FAILURE,
    CLONE_BEHAVIOR_PARTIAL,
    CLONE_BEHAVIOR_SUCCESS,
    CLONE_BEHAVIOR_TIMEOUT,
    CLONE_BEHAVIOR_VERBOSE,
    CooDispatchIsolatedCloneFixture,
    clone_output_path,
    clone_partial_output_path,
    clone_report_path,
    resolve_isolated_dispatch_runner,
    run_clone_full_path_execute,
    run_kwargs,
    write_clone_fake_node,
)
from tests.hermes_cli.coo_dispatch_isolated_fixture import (
    _DEFAULT_RUN_DATE,
    assert_subprocess_argv_contract,
    bounded_dispatch_config,
    build_run_args,
    evidence_stdout_paths,
    expected_factory_argv,
    list_audit_records,
    list_evidence_meta,
)
from tests.hermes_cli.test_coo_dispatch_run import (
    _CooDispatchRunTestBase,
    _DEFAULT_DISABLED_EXECUTOR_CONFIG,
)


class _CloneDrillBase(_CooDispatchRunTestBase):
    def setUp(self) -> None:
        self.fixture = CooDispatchIsolatedCloneFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.workspace = self.fixture.pipeline_root.parent
        self.audit_dir = self.fixture.hermes_home / "coo" / "audit"
        self.evidence_dir = self.fixture.hermes_home / "coo" / "execution-evidence"
        self.clone_root = self.fixture.pipeline_root

    def tearDown(self) -> None:
        self.fixture.stop()


class TestIsolatedCloneDrillOutcomes(_CloneDrillBase):
    def test_clone_success_outputs_reports_completed_consume(self) -> None:
        fake_node = write_clone_fake_node(self.workspace, CLONE_BEHAVIOR_SUCCESS)
        subprocess_calls: list[list[str]] = []
        real_run = subprocess.run

        def spy_run(argv, **kwargs):
            subprocess_calls.append(list(argv))
            return real_run(argv, **kwargs)

        with patch.object(subprocess, "run", side_effect=spy_run):
            result = run_clone_full_path_execute(
                self.fixture,
                self.seeded,
                fake_node=fake_node,
            )

        self.assertTrue(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.COMPLETED.value)
        self.assertTrue(clone_output_path(self.clone_root).is_file())
        self.assertTrue(clone_report_path(self.clone_root).is_file())

        output_payload = json.loads(
            clone_output_path(self.clone_root).read_text(encoding="utf-8")
        )
        self.assertEqual(output_payload["run_date"], _DEFAULT_RUN_DATE)
        self.assertEqual(output_payload["status"], "completed")

        state = json.loads(
            self.fixture.clone_paths.fixture_state.read_text(encoding="utf-8")
        )
        self.assertEqual(state["last_run_date"], _DEFAULT_RUN_DATE)
        self.assertEqual(len(state["runs"]), 1)

        self.assertGreaterEqual(len(list_audit_records(self.audit_dir)), 1)
        evidence_meta = list_evidence_meta(self.evidence_dir)
        self.assertEqual(len(evidence_meta), 1)
        assert_subprocess_argv_contract(
            evidence_meta[0],
            fake_node=fake_node,
            pipeline_root=self.clone_root,
        )
        self.assertIn("clone-ok", Path(evidence_meta[0]["stdout_log"]).read_text(encoding="utf-8"))

        self.assertEqual(len(subprocess_calls), 1)
        self.assertEqual(subprocess_calls[0], expected_factory_argv(fake_node))

        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        with self.assertRaises(ValueError):
            read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        with self.assertRaises(ValueError):
            read_confirmation(
                confirmation.confirmation_id,
                confirmation_dir=self.fixture.confirmation_dir,
            )

    def test_clone_non_zero_failed_no_consume(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_FAILURE,
        )
        self.assertFalse(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.FAILED.value)
        self.assertFalse(clone_output_path(self.clone_root).exists())
        self.assertEqual(len(list_evidence_meta(self.evidence_dir)), 1)

        ticket = self.seeded["ticket"]
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        self.assertEqual(bundle.consumed_at, "")

    def test_clone_timeout_failed_no_consume(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_TIMEOUT,
            policy_max_runtime_seconds=1,
        )
        self.assertFalse(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.FAILED.value)
        evidence_meta = list_evidence_meta(self.evidence_dir)
        self.assertEqual(evidence_meta[0]["exit_code"], _TIMEOUT_EXIT_CODE)

    def test_clone_partial_output_failure_no_consume(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_PARTIAL,
        )
        self.assertFalse(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.FAILED.value)
        self.assertTrue(clone_partial_output_path(self.clone_root).is_file())
        partial = json.loads(
            clone_partial_output_path(self.clone_root).read_text(encoding="utf-8")
        )
        self.assertTrue(partial["partial"])
        self.assertFalse(clone_output_path(self.clone_root).exists())


class TestIsolatedCloneDrillRetryReplay(_CloneDrillBase):
    def test_failure_retry_success_then_consume(self) -> None:
        workspace = self.workspace
        node = write_clone_fake_node(workspace, CLONE_BEHAVIOR_FAILURE)
        first = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            fake_node=node,
        )
        self.assertFalse(first.consumed)
        self.assertEqual(first.status, DispatchExecutionRunStatus.FAILED.value)
        self.assertEqual(len(list_evidence_meta(self.evidence_dir)), 1)

        write_clone_fake_node(workspace, CLONE_BEHAVIOR_SUCCESS)
        second = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            fake_node=node,
        )
        self.assertTrue(second.consumed)
        self.assertEqual(second.status, DispatchExecutionRunStatus.COMPLETED.value)
        self.assertTrue(clone_output_path(self.clone_root).is_file())
        self.assertEqual(len(list_evidence_meta(self.evidence_dir)), 2)

    def test_success_replay_rejected_without_subprocess(self) -> None:
        runner_calls = {"count": 0}
        real_execute = run_clone_full_path_execute

        def counting_clone_execute(*args, **kwargs):
            runner_calls["count"] += 1
            return real_execute(*args, **kwargs)

        write_clone_fake_node(self.workspace, CLONE_BEHAVIOR_SUCCESS)
        first = counting_clone_execute(self.fixture, self.seeded)
        self.assertTrue(first.consumed)
        runner_calls["count"] = 0

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **run_kwargs(
                        self.fixture,
                        self.seeded,
                        subprocess_runner=lambda *a, **k: (0, "ok", ""),
                        node_path=str(self.workspace / "bin" / "node"),
                    ),
                )
        self.assertIn("consumed", str(exc.exception).lower())
        self.assertEqual(runner_calls["count"], 0)


class TestIsolatedCloneDrillFailClosed(_CloneDrillBase):
    def _assert_subprocess_not_called(self, test_fn) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")):
                test_fn()

    def test_attestation_mismatch_blocks_subprocess(self) -> None:
        fake_node = write_clone_fake_node(self.workspace)
        runner = resolve_isolated_dispatch_runner(
            pipeline_root=self.clone_root,
            fake_node=fake_node,
        )

        def run_mismatch() -> None:
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **run_kwargs(
                        self.fixture,
                        self.seeded,
                        pipeline_root=str(self.workspace / "other-clone"),
                        subprocess_runner=runner,
                        node_path=str(fake_node),
                    ),
                )
            self.assertIn("does not match attested", str(exc.exception))

        self._assert_subprocess_not_called(run_mismatch)

    def test_binding_unbound_blocks_subprocess(self) -> None:
        self.fixture.write_binding_state("unbound")

        def run_unbound() -> None:
            with self.assertRaises(ValueError) as exc:
                run_clone_full_path_execute(self.fixture, self.seeded)
            self.assertIn("runner_binding_unbound", str(exc.exception))

        self._assert_subprocess_not_called(run_unbound)

    def test_binding_staged_blocks_subprocess(self) -> None:
        self.fixture.write_binding_state("staged")

        def run_staged() -> None:
            with self.assertRaises(ValueError) as exc:
                run_clone_full_path_execute(self.fixture, self.seeded)
            self.assertIn("runner_binding_staged", str(exc.exception))

        self._assert_subprocess_not_called(run_staged)

    def test_executor_disabled_blocks_subprocess(self) -> None:
        runner_calls = {"count": 0}

        def counting_runner(*args, **kwargs):
            runner_calls["count"] += 1
            return 0, "ok", ""

        def run_disabled() -> None:
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **run_kwargs(
                        self.fixture,
                        self.seeded,
                        merged_config=_DEFAULT_DISABLED_EXECUTOR_CONFIG,
                        subprocess_runner=counting_runner,
                    ),
                )
            self.assertIn("executor_disabled", str(exc.exception))

        self._assert_subprocess_not_called(run_disabled)
        self.assertEqual(runner_calls["count"], 0)

    def test_outside_allowlist_blocks_subprocess(self) -> None:
        other_root = self.workspace / "other-allowed-clone"
        other_root.mkdir()
        config = bounded_dispatch_config(other_root)

        def run_outside() -> None:
            with self.assertRaises(ValueError) as exc:
                run_clone_full_path_execute(
                    self.fixture,
                    self.seeded,
                    merged_config=config,
                )
            self.assertIn("outside allowed_pipeline_roots", str(exc.exception))

        self._assert_subprocess_not_called(run_outside)

    def test_production_root_blocks_subprocess(self) -> None:
        def run_production() -> None:
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **run_kwargs(
                        self.fixture,
                        self.seeded,
                        pipeline_root="/opt/data/multi-content-pipeline",
                        subprocess_runner=lambda *a, **k: (0, "", ""),
                    ),
                )
            self.assertIn("hard-denied", str(exc.exception))

        self._assert_subprocess_not_called(run_production)

    def test_symlink_escape_blocks_subprocess(self) -> None:
        outside = self.workspace / "outside-clone"
        outside.mkdir()
        link = self.clone_root / "escape-link"
        link.symlink_to(outside)

        def run_escape() -> None:
            with self.assertRaises(ValueError):
                execute_coo_dispatch_run(
                    **run_kwargs(
                        self.fixture,
                        self.seeded,
                        pipeline_root=str(link),
                        subprocess_runner=lambda *a, **k: (0, "", ""),
                    ),
                )

        self._assert_subprocess_not_called(run_escape)

    def test_default_cli_fail_closed(self) -> None:
        stderr = io.StringIO()
        args = build_run_args(self.seeded, self.clone_root)
        with patch.object(sys, "stderr", stderr):
            exit_code = run_coo_dispatch_from_args(args)
        self.assertEqual(exit_code, 1)
        self.assertIn(DISPATCH_RUNNER_NOT_CONFIGURED, stderr.getvalue())


class TestIsolatedCloneDrillHarnessContracts(_CloneDrillBase):
    def test_secret_env_not_forwarded(self) -> None:
        fake_node = write_clone_fake_node(self.workspace, CLONE_BEHAVIOR_ENV_PROBE)
        runner = resolve_isolated_dispatch_runner(
            pipeline_root=self.clone_root,
            fake_node=fake_node,
        )
        exit_code, stdout, stderr = runner(
            expected_factory_argv(fake_node),
            str(self.clone_root),
            {
                "PATH": os.environ.get("PATH", ""),
                "SECRET_TOKEN": "must-not-forward",
                "API_KEY": "must-not-forward",
            },
            30,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("False False", stdout.replace("\n", " "))

    def test_output_truncated_in_evidence(self) -> None:
        run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_VERBOSE,
            harness_max_output_bytes=128,
        )
        stdout_paths = evidence_stdout_paths(self.evidence_dir)
        self.assertEqual(len(stdout_paths), 1)
        content = stdout_paths[0].read_text(encoding="utf-8")
        self.assertLessEqual(len(content.encode("utf-8")), 200)
        self.assertIn("...[truncated]", content)

    def test_only_fake_node_executable_runs(self) -> None:
        fake_node = write_clone_fake_node(self.workspace, CLONE_BEHAVIOR_SUCCESS)
        seen: list[str] = []
        real_run = subprocess.run

        def spy_run(argv, **kwargs):
            seen.append(argv[0])
            return real_run(argv, **kwargs)

        with patch.object(subprocess, "run", side_effect=spy_run):
            run_clone_full_path_execute(
                self.fixture,
                self.seeded,
                fake_node=fake_node,
            )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], str(fake_node))
        self.assertEqual(os.path.basename(seen[0]), "node")

    def test_restricted_profile_rejects_clone_argv(self) -> None:
        fake_node = write_clone_fake_node(self.workspace)
        from agent.coo.bounded_subprocess_runner import (
            RUNNER_PROFILE_RESTRICTED,
            create_bounded_subprocess_runner,
        )

        restricted = create_bounded_subprocess_runner(
            (str(self.clone_root),),
            profile=RUNNER_PROFILE_RESTRICTED,
        )
        with self.assertRaises(BoundedSubprocessRunnerError):
            restricted(
                expected_factory_argv(fake_node),
                str(self.clone_root),
                {"PATH": os.environ.get("PATH", "")},
                30,
            )


if __name__ == "__main__":
    unittest.main()
