"""Phase 12H tests — isolated fixture full-path drill with real bounded harness."""

from __future__ import annotations

import io
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
from tests.hermes_cli.coo_dispatch_isolated_fixture import (
    NODE_BEHAVIOR_ENV_PROBE,
    NODE_BEHAVIOR_FAILURE,
    NODE_BEHAVIOR_SUCCESS,
    NODE_BEHAVIOR_TIMEOUT,
    NODE_BEHAVIOR_VERBOSE,
    _DEFAULT_RUN_DATE,
    assert_subprocess_argv_contract,
    bounded_dispatch_config,
    build_run_args,
    expected_factory_argv,
    evidence_stdout_paths,
    list_audit_records,
    list_evidence_meta,
    resolve_isolated_dispatch_runner,
    run_isolated_full_path_execute,
    run_isolated_full_path_from_args,
    run_kwargs,
    write_fake_node_executable,
    write_fake_pipeline_js,
)
from tests.hermes_cli.test_coo_dispatch_run import (
    _CooDispatchRunFixture,
    _CooDispatchRunTestBase,
    _DEFAULT_DISABLED_EXECUTOR_CONFIG,
)


class TestIsolatedFullPathDrillSuccess(_CooDispatchRunTestBase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.workspace = self.fixture.pipeline_root.parent
        self.audit_dir = self.fixture.hermes_home / "coo" / "audit"
        self.evidence_dir = self.fixture.hermes_home / "coo" / "execution-evidence"

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_full_path_success_completed_audit_evidence_consume(self) -> None:
        write_fake_pipeline_js(self.fixture.pipeline_root)
        fake_node = write_fake_node_executable(self.workspace, NODE_BEHAVIOR_SUCCESS)
        subprocess_calls: list[list[str]] = []
        real_run = subprocess.run

        def spy_run(argv, **kwargs):
            subprocess_calls.append(list(argv))
            return real_run(argv, **kwargs)

        with patch.object(subprocess, "run", side_effect=spy_run):
            result = run_isolated_full_path_execute(
                self.fixture,
                self.seeded,
                fake_node=fake_node,
            )

        self.assertTrue(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.COMPLETED.value)

        audits = list_audit_records(self.audit_dir)
        self.assertGreaterEqual(len(audits), 1)
        self.assertEqual(audits[0]["pipeline_root"], os.path.realpath(str(self.fixture.pipeline_root)))

        evidence_meta = list_evidence_meta(self.evidence_dir)
        self.assertEqual(len(evidence_meta), 1)
        assert_subprocess_argv_contract(
            evidence_meta[0],
            fake_node=fake_node,
            pipeline_root=self.fixture.pipeline_root,
        )
        self.assertEqual(evidence_meta[0]["exit_code"], 0)

        stdout_path = Path(evidence_meta[0]["stdout_log"])
        self.assertTrue(stdout_path.is_file())
        self.assertIn("fixture-ok", stdout_path.read_text(encoding="utf-8"))

        self.assertEqual(len(subprocess_calls), 1)
        self.assertEqual(
            os.path.basename(subprocess_calls[0][0]).lower(),
            "node",
        )
        self.assertNotEqual(subprocess_calls[0][0], "/usr/bin/node")

        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        with self.assertRaises(ValueError):
            read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        with self.assertRaises(ValueError):
            read_confirmation(
                confirmation.confirmation_id,
                confirmation_dir=self.fixture.confirmation_dir,
            )

    def test_service_from_args_full_path_success(self) -> None:
        exit_code = run_isolated_full_path_from_args(self.fixture, self.seeded)
        self.assertEqual(exit_code, 0)
        self.assertGreaterEqual(len(list_audit_records(self.audit_dir)), 1)
        self.assertEqual(len(list_evidence_meta(self.evidence_dir)), 1)


class TestIsolatedFullPathDrillFailurePaths(_CooDispatchRunTestBase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.workspace = self.fixture.pipeline_root.parent
        self.evidence_dir = self.fixture.hermes_home / "coo" / "execution-evidence"

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_full_path_non_zero_failed_evidence_no_consume(self) -> None:
        result = run_isolated_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=NODE_BEHAVIOR_FAILURE,
        )
        self.assertFalse(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.FAILED.value)

        evidence_meta = list_evidence_meta(self.evidence_dir)
        self.assertEqual(len(evidence_meta), 1)
        self.assertEqual(evidence_meta[0]["exit_code"], 3)

        ticket = self.seeded["ticket"]
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        self.assertEqual(bundle.consumed_at, "")

    def test_full_path_timeout_failed_no_consume(self) -> None:
        result = run_isolated_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=NODE_BEHAVIOR_TIMEOUT,
            policy_max_runtime_seconds=1,
        )
        self.assertFalse(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.FAILED.value)

        evidence_meta = list_evidence_meta(self.evidence_dir)
        self.assertEqual(len(evidence_meta), 1)
        self.assertEqual(evidence_meta[0]["exit_code"], _TIMEOUT_EXIT_CODE)


class TestIsolatedFullPathDrillFailClosed(_CooDispatchRunTestBase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.workspace = self.fixture.pipeline_root.parent

    def tearDown(self) -> None:
        self.fixture.stop()

    def _assert_subprocess_not_called(self, test_fn) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")):
                test_fn()

    def test_attestation_mismatch_blocks_subprocess(self) -> None:
        write_fake_pipeline_js(self.fixture.pipeline_root)
        fake_node = write_fake_node_executable(self.workspace)
        runner = resolve_isolated_dispatch_runner(
            pipeline_root=self.fixture.pipeline_root,
            fake_node=fake_node,
        )

        def run_mismatch() -> None:
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **run_kwargs(
                        self.fixture,
                        self.seeded,
                        pipeline_root=str(self.workspace / "other-root"),
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
                run_isolated_full_path_execute(self.fixture, self.seeded)
            self.assertIn("runner_binding_unbound", str(exc.exception))

        self._assert_subprocess_not_called(run_unbound)

    def test_binding_staged_blocks_subprocess(self) -> None:
        self.fixture.write_binding_state("staged")

        def run_staged() -> None:
            with self.assertRaises(ValueError) as exc:
                run_isolated_full_path_execute(self.fixture, self.seeded)
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

    def test_cwd_outside_allowlist_blocks_subprocess(self) -> None:
        other_root = self.workspace / "other-allowed"
        other_root.mkdir()
        config = bounded_dispatch_config(other_root)

        def run_outside() -> None:
            with self.assertRaises(ValueError) as exc:
                run_isolated_full_path_execute(
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
        outside = self.workspace / "outside"
        outside.mkdir()
        link = self.fixture.pipeline_root / "escape-link"
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

    def test_default_cli_run_still_fail_closed(self) -> None:
        stderr = io.StringIO()
        args = build_run_args(self.seeded, self.fixture.pipeline_root)
        with patch.object(sys, "stderr", stderr):
            exit_code = run_coo_dispatch_from_args(args)
        self.assertEqual(exit_code, 1)
        self.assertIn(DISPATCH_RUNNER_NOT_CONFIGURED, stderr.getvalue())


class TestIsolatedFullPathDrillHarnessContracts(_CooDispatchRunTestBase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.workspace = self.fixture.pipeline_root.parent
        self.evidence_dir = self.fixture.hermes_home / "coo" / "execution-evidence"

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_secret_env_not_forwarded(self) -> None:
        write_fake_pipeline_js(self.fixture.pipeline_root)
        fake_node = write_fake_node_executable(self.workspace, NODE_BEHAVIOR_ENV_PROBE)
        runner = resolve_isolated_dispatch_runner(
            pipeline_root=self.fixture.pipeline_root,
            fake_node=fake_node,
        )

        import os as real_os

        secret_env = {
            "PATH": real_os.environ.get("PATH", ""),
            "HOME": real_os.environ.get("HOME", ""),
            "SECRET_TOKEN": "must-not-forward",
            "API_KEY": "must-not-forward",
        }
        exit_code, stdout, stderr = runner(
            expected_factory_argv(fake_node),
            str(self.fixture.pipeline_root),
            secret_env,
            30,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("False False", stdout.replace("\n", " "))

    def test_output_truncated_in_evidence(self) -> None:
        run_isolated_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=NODE_BEHAVIOR_VERBOSE,
            harness_max_output_bytes=128,
        )
        stdout_paths = evidence_stdout_paths(self.evidence_dir)
        self.assertEqual(len(stdout_paths), 1)
        content = stdout_paths[0].read_text(encoding="utf-8")
        self.assertLessEqual(len(content.encode("utf-8")), 200)
        self.assertIn("...[truncated]", content)

    def test_fake_node_mismatch_rejected_before_real_node(self) -> None:
        write_fake_pipeline_js(self.fixture.pipeline_root)
        configured_node = write_fake_node_executable(self.workspace, NODE_BEHAVIOR_SUCCESS)
        other_node = write_fake_node_executable(self.workspace / "other-bin", NODE_BEHAVIOR_SUCCESS)
        runner = resolve_isolated_dispatch_runner(
            pipeline_root=self.fixture.pipeline_root,
            fake_node=configured_node,
        )

        with self.assertRaises(BoundedSubprocessRunnerError):
            runner(
                expected_factory_argv(other_node),
                str(self.fixture.pipeline_root),
                {"PATH": os.environ.get("PATH", "")},
                30,
            )

    def test_factory_argv_contract_matches_dispatch_profile(self) -> None:
        write_fake_pipeline_js(self.fixture.pipeline_root)
        fake_node = write_fake_node_executable(self.workspace, NODE_BEHAVIOR_SUCCESS)
        run_isolated_full_path_execute(
            self.fixture,
            self.seeded,
            fake_node=fake_node,
        )
        evidence_meta = list_evidence_meta(self.evidence_dir)
        self.assertEqual(len(evidence_meta), 1)
        assert_subprocess_argv_contract(
            evidence_meta[0],
            fake_node=fake_node,
            pipeline_root=self.fixture.pipeline_root,
        )
        self.assertEqual(evidence_meta[0]["argv"][0], str(fake_node))
        self.assertNotEqual(evidence_meta[0]["argv"][0], "/usr/bin/node")


class TestIsolatedFullPathDrillProviderResolution(unittest.TestCase):
    def test_restricted_profile_blocks_node_argv(self) -> None:
        fixture = _CooDispatchRunFixture()
        fixture.start()
        try:
            workspace = fixture.pipeline_root.parent
            write_fake_pipeline_js(fixture.pipeline_root)
            fake_node = write_fake_node_executable(workspace)
            from agent.coo.bounded_subprocess_runner import (
                RUNNER_PROFILE_RESTRICTED,
                create_bounded_subprocess_runner,
            )

            restricted = create_bounded_subprocess_runner(
                (str(fixture.pipeline_root),),
                profile=RUNNER_PROFILE_RESTRICTED,
            )
            with self.assertRaises(BoundedSubprocessRunnerError):
                restricted(
                    expected_factory_argv(fake_node),
                    str(fixture.pipeline_root),
                    {"PATH": os.environ.get("PATH", "")},
                    30,
                )
        finally:
            fixture.stop()

    def test_dispatch_profile_real_subprocess_fixture_only(self) -> None:
        fixture = _CooDispatchRunFixture()
        fixture.start()
        try:
            seeded = fixture.seed_bundle_and_confirmation()
            exit_code = run_isolated_full_path_from_args(fixture, seeded)
            self.assertEqual(exit_code, 0)
            evidence_dir = fixture.hermes_home / "coo" / "execution-evidence"
            meta = list_evidence_meta(evidence_dir)
            self.assertEqual(len(meta), 1)
            self.assertEqual(os.path.basename(meta[0]["argv"][0]), "node")
            self.assertNotIn("/opt/data/multi-content-pipeline", meta[0]["cwd"])
        finally:
            fixture.stop()


if __name__ == "__main__":
    unittest.main()
