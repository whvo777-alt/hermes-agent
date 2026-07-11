"""Phase 12E-5 tests — provider real harness explicit opt-in wiring."""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.bounded_subprocess_runner import BoundedSubprocessRunnerError
from agent.coo.dispatch_cli_runner_injection import (
    DISPATCH_RUNNER_NOT_CONFIGURED,
    resolve_bounded_subprocess_runner,
    resolve_dispatch_run_subprocess_runner,
)
from agent.coo.dispatch_runner_binding_state import (
    RUNNER_BINDING_STATE_BOUND,
    RUNNER_BINDING_STATE_STAGED,
    CooDispatchRunnerBindingState,
)
from agent.coo.dispatch_runner_provider import (
    REASON_RUNNER_PROVIDER_INJECTED_RUNNER_REQUIRED,
    REASON_RUNNER_PROVIDER_MODE_NOT_BOUNDED,
    REASON_RUNNER_PROVIDER_REAL_HARNESS_AMBIGUOUS,
    DispatchRunnerProviderResolutionError,
)
from agent.coo.production_executor_factory import _TIMEOUT_EXIT_CODE
from hermes_cli.coo_dispatch import run_coo_dispatch_from_args
from tests.hermes_cli.test_coo_dispatch_run import (
    _CooDispatchRunFixture,
    _CooDispatchRunTestBase,
    _mock_runner_success,
)


def _bounded_config(pipeline_root: str) -> dict:
    return {
        "coo": {
            "dispatch": {
                "executor": {
                    "enabled": True,
                    "allowed_pipeline_roots": [pipeline_root],
                },
                "runner_provider": {"mode": "bounded"},
            }
        }
    }


def _python_argv(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class TestProviderRealHarnessOptIn(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-real-harness-")
        self.isolated_root = Path(self.tmp.name) / "pipeline"
        self.isolated_root.mkdir()
        self.config = _bounded_config(str(self.isolated_root))
        self.binding = CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_BOUND)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_creates_bounded_runner_callable(self) -> None:
        with patch(
            "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
            wraps=__import__(
                "agent.coo.bounded_subprocess_runner",
                fromlist=["create_bounded_subprocess_runner"],
            ).create_bounded_subprocess_runner,
        ) as create_mock:
            runner = resolve_bounded_subprocess_runner(
                self.config,
                binding_state=self.binding,
                use_real_bounded_runner=True,
            )
        create_mock.assert_called_once()
        _, kwargs = create_mock.call_args
        self.assertEqual(kwargs.get("profile"), "restricted")
        self.assertIsNone(kwargs.get("node_executable"))
        self.assertTrue(callable(runner))

    def test_real_harness_python_fixture_success(self) -> None:
        runner = resolve_bounded_subprocess_runner(
            self.config,
            binding_state=self.binding,
            use_real_bounded_runner=True,
        )
        exit_code, stdout, stderr = runner(
            _python_argv("print('harness-ok')"),
            str(self.isolated_root),
            {"PATH": __import__("os").environ.get("PATH", "")},
            30,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("harness-ok", stdout)

    def test_real_harness_non_zero_exit(self) -> None:
        runner = resolve_bounded_subprocess_runner(
            self.config,
            binding_state=self.binding,
            use_real_bounded_runner=True,
        )
        exit_code, stdout, stderr = runner(
            _python_argv("import sys; sys.exit(3)"),
            str(self.isolated_root),
            {"PATH": __import__("os").environ.get("PATH", "")},
            30,
        )
        self.assertEqual(exit_code, 3)

    def test_real_harness_timeout(self) -> None:
        runner = resolve_bounded_subprocess_runner(
            self.config,
            binding_state=self.binding,
            use_real_bounded_runner=True,
            harness_max_timeout_seconds=5,
        )
        exit_code, stdout, stderr = runner(
            _python_argv("import time; time.sleep(3)"),
            str(self.isolated_root),
            {"PATH": __import__("os").environ.get("PATH", "")},
            1,
        )
        self.assertEqual(exit_code, _TIMEOUT_EXIT_CODE)

    def test_scaffold_mode_rejected(self) -> None:
        config = dict(self.config)
        config["coo"]["dispatch"]["runner_provider"] = {"mode": "scaffold"}
        with self.assertRaises(DispatchRunnerProviderResolutionError) as exc:
            resolve_bounded_subprocess_runner(
                config,
                binding_state=self.binding,
                use_real_bounded_runner=True,
            )
        self.assertIn(REASON_RUNNER_PROVIDER_MODE_NOT_BOUNDED, str(exc.exception))

    def test_unbound_binding_rejected(self) -> None:
        with self.assertRaises(DispatchRunnerProviderResolutionError):
            resolve_bounded_subprocess_runner(
                self.config,
                binding_state=CooDispatchRunnerBindingState(state="unbound"),
                use_real_bounded_runner=True,
            )

    def test_staged_binding_rejected(self) -> None:
        with self.assertRaises(DispatchRunnerProviderResolutionError):
            resolve_bounded_subprocess_runner(
                self.config,
                binding_state=CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_STAGED),
                use_real_bounded_runner=True,
            )

    def test_disabled_executor_rejected(self) -> None:
        config = dict(self.config)
        config["coo"]["dispatch"]["executor"]["enabled"] = False
        with self.assertRaises(DispatchRunnerProviderResolutionError):
            resolve_bounded_subprocess_runner(
                config,
                binding_state=self.binding,
                use_real_bounded_runner=True,
            )

    def test_production_root_rejected(self) -> None:
        config = _bounded_config("/opt/data/multi-content-pipeline")
        with self.assertRaises(DispatchRunnerProviderResolutionError):
            resolve_bounded_subprocess_runner(
                config,
                binding_state=self.binding,
                use_real_bounded_runner=True,
            )

    def test_mock_and_real_harness_ambiguous(self) -> None:
        with self.assertRaises(DispatchRunnerProviderResolutionError) as exc:
            resolve_bounded_subprocess_runner(
                self.config,
                injected_runner=_mock_runner_success,
                binding_state=self.binding,
                use_real_bounded_runner=True,
            )
        self.assertIn(REASON_RUNNER_PROVIDER_REAL_HARNESS_AMBIGUOUS, str(exc.exception))

    def test_without_opt_in_fail_closed(self) -> None:
        with self.assertRaises(DispatchRunnerProviderResolutionError) as exc:
            resolve_bounded_subprocess_runner(
                self.config,
                binding_state=self.binding,
            )
        self.assertIn(REASON_RUNNER_PROVIDER_INJECTED_RUNNER_REQUIRED, str(exc.exception))

    def test_node_argv_rejected_by_harness(self) -> None:
        runner = resolve_bounded_subprocess_runner(
            self.config,
            binding_state=self.binding,
            use_real_bounded_runner=True,
        )
        with self.assertRaises(BoundedSubprocessRunnerError):
            runner(
                ["node", "pipeline.js"],
                str(self.isolated_root),
                {"PATH": __import__("os").environ.get("PATH", "")},
                30,
            )

    def test_dry_run_skips_harness_creation(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_runner_injection.resolve_bounded_subprocess_runner",
            side_effect=AssertionError("provider must not resolve on dry-run"),
        ):
            resolved = resolve_dispatch_run_subprocess_runner(
                use_runner_provider=True,
                use_real_bounded_runner=True,
                dry_run=True,
                merged_config=self.config,
                binding_state=self.binding,
            )
        self.assertIsNone(resolved)


class TestProviderRealHarnessCliWiring(_CooDispatchRunTestBase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def _run_args(self) -> argparse.Namespace:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        return argparse.Namespace(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            dry_run=False,
        )

    def test_default_cli_run_still_fail_closed(self) -> None:
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            exit_code = run_coo_dispatch_from_args(self._run_args())
        self.assertEqual(exit_code, 1)
        self.assertIn(DISPATCH_RUNNER_NOT_CONFIGURED, stderr.getvalue())

    def test_service_real_harness_opt_in_resolves_runner(self) -> None:
        config = _bounded_config(str(self.fixture.pipeline_root))
        with patch(
            "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
            wraps=__import__(
                "agent.coo.bounded_subprocess_runner",
                fromlist=["create_bounded_subprocess_runner"],
            ).create_bounded_subprocess_runner,
        ) as create_mock:
            runner = resolve_dispatch_run_subprocess_runner(
                use_runner_provider=True,
                use_real_bounded_runner=True,
                merged_config=config,
                binding_state=CooDispatchRunnerBindingState(
                    state=RUNNER_BINDING_STATE_BOUND
                ),
            )
        create_mock.assert_called_once()
        _, kwargs = create_mock.call_args
        self.assertEqual(kwargs.get("profile"), "restricted")
        self.assertIsNone(kwargs.get("node_executable"))
        self.assertTrue(callable(runner))


if __name__ == "__main__":
    unittest.main()
