"""Phase 12D tests — mock-only runner factory E2E integration."""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from agent.coo.dispatch_cli_run import execute_coo_dispatch_run
from agent.coo.dispatch_cli_runner_injection import DISPATCH_RUNNER_NOT_CONFIGURED
from agent.coo.execution_dispatch_runtime import DispatchExecutionRunStatus
from agent.coo.production_executor_factory import (
    _ALLOWED_FACTORY_ENTRYPOINT,
    _TIMEOUT_EXIT_CODE,
    build_pipeline_dispatch_executor,
)
from tests.hermes_cli.test_coo_dispatch_run import (
    _CooDispatchRunFixture,
    _CooDispatchRunTestBase,
    _DEFAULT_DISABLED_EXECUTOR_CONFIG,
    _mock_runner_failure,
    _mock_runner_success,
    _mock_runner_timeout,
)


class TestDispatchMockRunnerE2EIntegration(_CooDispatchRunTestBase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_bound_enabled_mock_success_consumes(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=_mock_runner_success),
            )
        self.assertTrue(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.COMPLETED.value)

    def test_factory_receives_same_runner_callable(self) -> None:
        runner_calls = {"count": 0}

        def counting_runner(argv, cwd, env, timeout):
            runner_calls["count"] += 1
            return _mock_runner_success(argv, cwd, env, timeout)

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.build_pipeline_dispatch_executor",
                wraps=build_pipeline_dispatch_executor,
            ) as build_mock,
        ):
            execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=counting_runner),
            )
        build_mock.assert_called_once()
        self.assertIs(build_mock.call_args.kwargs["subprocess_runner"], counting_runner)
        self.assertEqual(runner_calls["count"], 1)

    def test_mock_runner_captures_argv_cwd_env_without_execution(self) -> None:
        captured: dict[str, object] = {}

        def capturing_runner(argv, cwd, env, timeout):
            captured["argv"] = list(argv)
            captured["cwd"] = cwd
            captured["env"] = dict(env)
            captured["timeout_seconds"] = timeout
            return 0, "captured", ""

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=capturing_runner),
            )

        self.assertEqual(
            captured["argv"],
            ["/usr/bin/node", "pipeline.js", "--run-date", "2026-07-07"],
        )
        self.assertEqual(
            captured["cwd"],
            os.path.realpath(str(self.fixture.pipeline_root)),
        )
        self.assertIsInstance(captured["env"], dict)
        self.assertGreater(captured["timeout_seconds"], 0)
        self.assertNotIn("/opt/data/multi-content-pipeline", str(captured["cwd"]))

    def test_non_zero_failure_does_not_consume(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=_mock_runner_failure),
            )
        self.assertFalse(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.FAILED.value)

    def test_timeout_does_not_consume(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=_mock_runner_timeout),
            )
        self.assertFalse(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.FAILED.value)
        self.assertEqual(_mock_runner_timeout("", "", "", 0)[0], _TIMEOUT_EXIT_CODE)

    def test_no_runner_injection_fail_closed(self) -> None:
        with self.assertRaises(ValueError) as exc:
            execute_coo_dispatch_run(**self._run_kwargs(subprocess_runner=None))
        self.assertEqual(str(exc.exception), DISPATCH_RUNNER_NOT_CONFIGURED)

    def test_binding_unbound_blocks_mock_runner(self) -> None:
        binding_path = self.fixture.hermes_home / "coo" / "dispatch-runner-binding.json"
        binding_path.unlink()
        runner_calls = {"count": 0}

        def counting_runner(*args, **kwargs):
            runner_calls["count"] += 1
            return 0, "ok", ""

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.build_pipeline_dispatch_executor",
                side_effect=AssertionError("no factory"),
            ),
            patch(
                "agent.coo.dispatch_cli_run.run_approved_dispatch",
                side_effect=AssertionError("no runner"),
            ),
        ):
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=counting_runner),
                )
        self.assertIn("runner_binding_unbound", str(exc.exception))
        self.assertEqual(runner_calls["count"], 0)

    def test_binding_staged_blocks_mock_runner(self) -> None:
        self.fixture.write_binding_state("staged")
        runner_calls = {"count": 0}

        def counting_runner(*args, **kwargs):
            runner_calls["count"] += 1
            return 0, "ok", ""

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.build_pipeline_dispatch_executor",
                side_effect=AssertionError("no factory"),
            ),
            patch(
                "agent.coo.dispatch_cli_run.run_approved_dispatch",
                side_effect=AssertionError("no runner"),
            ),
        ):
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=counting_runner),
                )
        self.assertIn("runner_binding_staged", str(exc.exception))
        self.assertEqual(runner_calls["count"], 0)

    def test_invalid_enablement_blocks_mock_runner(self) -> None:
        runner_calls = {"count": 0}

        def counting_runner(*args, **kwargs):
            runner_calls["count"] += 1
            return 0, "ok", ""

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.build_pipeline_dispatch_executor",
                side_effect=AssertionError("no factory"),
            ),
            patch(
                "agent.coo.dispatch_cli_run.run_approved_dispatch",
                side_effect=AssertionError("no runner"),
            ),
        ):
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **self._run_kwargs(
                        subprocess_runner=counting_runner,
                        merged_config=_DEFAULT_DISABLED_EXECUTOR_CONFIG,
                    ),
                )
        self.assertIn("executor_disabled", str(exc.exception))
        self.assertEqual(runner_calls["count"], 0)

    def test_subprocess_never_called_on_success_path(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=_mock_runner_success),
            )

    def test_repository2_path_hard_denied_without_access(self) -> None:
        runner_calls = {"count": 0}

        def counting_runner(*args, **kwargs):
            runner_calls["count"] += 1
            return 0, "ok", ""

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **self._run_kwargs(
                        pipeline_root="/opt/data/multi-content-pipeline/evil",
                        subprocess_runner=counting_runner,
                    ),
                )
        self.assertIn("hard-denied", str(exc.exception))
        self.assertEqual(runner_calls["count"], 0)


class TestDispatchMockRunnerFactoryContract(unittest.TestCase):
    def test_injected_runner_used_instead_of_bounded_subprocess(self) -> None:
        import tempfile
        from pathlib import Path

        from agent.coo.production_executor_policy import ProductionExecutorPolicy

        with tempfile.TemporaryDirectory() as pipeline_root:
            policy = ProductionExecutorPolicy(
                enabled=True,
                allowed_pipeline_roots=(pipeline_root,),
            )

            def runner(argv, cwd, env, timeout):
                return 0, "mock-only", ""

            with (
                patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
                patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
                patch(
                    "agent.coo.production_executor_factory.get_hermes_home",
                ) as mock_home,
            ):
                hermes_home = Path(pipeline_root) / ".hermes"
                hermes_home.mkdir()
                mock_home.return_value = hermes_home
                executor = build_pipeline_dispatch_executor(
                    policy,
                    pipeline_root=pipeline_root,
                    entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                    subprocess_runner=runner,
                    node_path="/usr/bin/fake-node",
                    evidence_run_id="phase-12d",
                )
                exit_code, stdout, stderr = executor(
                    _ALLOWED_FACTORY_ENTRYPOINT,
                    pipeline_root,
                    "2026-07-07",
                )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "mock-only")
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
