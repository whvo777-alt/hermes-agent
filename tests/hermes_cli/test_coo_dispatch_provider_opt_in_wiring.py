"""Phase 12E-3 tests — provider opt-in CLI wiring (mock-only)."""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_runner_injection import (
    DISPATCH_RUNNER_NOT_CONFIGURED,
    REASON_AMBIGUOUS_RUNNER_INJECTION,
    resolve_dispatch_run_subprocess_runner,
)
from agent.coo.dispatch_runner_provider import (
    REASON_RUNNER_PROVIDER_INJECTED_RUNNER_REQUIRED,
    REASON_RUNNER_PROVIDER_MODE_NOT_BOUNDED,
    DispatchRunnerProviderResolutionError,
)
from agent.coo.production_executor_factory import build_pipeline_dispatch_executor
from hermes_cli.coo_dispatch import run_coo_dispatch_from_args
from tests.hermes_cli.test_coo_dispatch_run import (
    _CooDispatchRunFixture,
    _CooDispatchRunTestBase,
    _mock_runner_failure,
    _mock_runner_success,
    _mock_runner_timeout,
)


def _bounded_enabled_config(pipeline_root: Path) -> dict:
    return {
        "coo": {
            "dispatch": {
                "executor": {
                    "enabled": True,
                    "allowed_pipeline_roots": [str(pipeline_root)],
                },
                "runner_provider": {"mode": "bounded"},
            }
        }
    }


class TestProviderOptInCliWiring(_CooDispatchRunTestBase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def _run_args(self, *, dry_run: bool = False) -> argparse.Namespace:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        return argparse.Namespace(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            dry_run=dry_run,
        )

    def _bounded_config(self) -> dict:
        return _bounded_enabled_config(self.fixture.pipeline_root)

    def _enabled_config(self) -> dict:
        return {
            "coo": {
                "dispatch": {
                    "executor": {
                        "enabled": True,
                        "allowed_pipeline_roots": [str(self.fixture.pipeline_root)],
                    }
                }
            }
        }

    def test_default_cli_run_still_fail_closed(self) -> None:
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            exit_code = run_coo_dispatch_from_args(self._run_args())
        self.assertEqual(exit_code, 1)
        self.assertIn(DISPATCH_RUNNER_NOT_CONFIGURED, stderr.getvalue())

    def test_use_runner_provider_false_without_runner_fail_closed(self) -> None:
        with self.assertRaises(ValueError) as exc:
            resolve_dispatch_run_subprocess_runner(
                use_runner_provider=False,
                dry_run=False,
            )
        self.assertEqual(str(exc.exception), DISPATCH_RUNNER_NOT_CONFIGURED)

    def test_provider_opt_in_mock_success_consumes(self) -> None:
        args = self._run_args()
        with (
            patch("hermes_cli.config.load_config", return_value=self._bounded_config()),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = run_coo_dispatch_from_args(
                args,
                injected_runner=_mock_runner_success,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 0)

    def test_provider_opt_in_without_injected_runner_fail_closed(self) -> None:
        stderr = io.StringIO()
        with (
            patch("hermes_cli.config.load_config", return_value=self._bounded_config()),
            patch.object(sys, "stderr", stderr),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 1)
        self.assertIn(REASON_RUNNER_PROVIDER_INJECTED_RUNNER_REQUIRED, stderr.getvalue())

    def test_scaffold_provider_fail_closed(self) -> None:
        config = self._bounded_config()
        config["coo"]["dispatch"]["runner_provider"] = {"mode": "scaffold"}
        stderr = io.StringIO()
        with (
            patch("hermes_cli.config.load_config", return_value=config),
            patch.object(sys, "stderr", stderr),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                injected_runner=_mock_runner_success,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 1)
        self.assertIn(REASON_RUNNER_PROVIDER_MODE_NOT_BOUNDED, stderr.getvalue())

    def test_binding_staged_fail_closed(self) -> None:
        self.fixture.write_binding_state("staged")
        stderr = io.StringIO()
        with (
            patch("hermes_cli.config.load_config", return_value=self._bounded_config()),
            patch.object(sys, "stderr", stderr),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                injected_runner=_mock_runner_success,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("runner_binding_staged", stderr.getvalue())

    def test_binding_unbound_fail_closed(self) -> None:
        binding_path = self.fixture.hermes_home / "coo" / "dispatch-runner-binding.json"
        binding_path.unlink()
        stderr = io.StringIO()
        with (
            patch("hermes_cli.config.load_config", return_value=self._bounded_config()),
            patch.object(sys, "stderr", stderr),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                injected_runner=_mock_runner_success,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("runner_binding_unbound", stderr.getvalue())

    def test_disabled_executor_fail_closed(self) -> None:
        config = self._bounded_config()
        config["coo"]["dispatch"]["executor"]["enabled"] = False
        stderr = io.StringIO()
        with (
            patch("hermes_cli.config.load_config", return_value=config),
            patch.object(sys, "stderr", stderr),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                injected_runner=_mock_runner_success,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("executor_disabled", stderr.getvalue())

    def test_ambiguous_direct_and_provider_injection_rejected(self) -> None:
        stderr = io.StringIO()
        with (
            patch("hermes_cli.config.load_config", return_value=self._bounded_config()),
            patch.object(sys, "stderr", stderr),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                subprocess_runner=_mock_runner_success,
                injected_runner=_mock_runner_success,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 1)
        self.assertIn(REASON_AMBIGUOUS_RUNNER_INJECTION, stderr.getvalue())

    def test_dry_run_skips_provider_resolution(self) -> None:
        with (
            patch("hermes_cli.config.load_config", return_value=self._enabled_config()),
            patch(
                "agent.coo.dispatch_cli_runner_injection.resolve_bounded_subprocess_runner",
                side_effect=AssertionError("provider must not resolve on dry-run"),
            ),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(dry_run=True),
                injected_runner=_mock_runner_success,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 0)

    def test_provider_passes_same_callable_to_factory(self) -> None:
        runner_calls = {"count": 0}

        def counting_runner(argv, cwd, env, timeout):
            runner_calls["count"] += 1
            return _mock_runner_success(argv, cwd, env, timeout)

        with (
            patch("hermes_cli.config.load_config", return_value=self._bounded_config()),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.build_pipeline_dispatch_executor",
                wraps=build_pipeline_dispatch_executor,
            ) as build_mock,
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                injected_runner=counting_runner,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 0)
        build_mock.assert_called_once()
        self.assertIs(build_mock.call_args.kwargs["subprocess_runner"], counting_runner)
        self.assertEqual(runner_calls["count"], 1)

    def test_provider_failure_does_not_consume(self) -> None:
        from agent.coo.dispatch_bundle_store import read_bundle
        from agent.coo.production_executor_confirmation import read_confirmation

        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        with (
            patch("hermes_cli.config.load_config", return_value=self._bounded_config()),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                injected_runner=_mock_runner_failure,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 0)
        bundle = read_bundle(
            ticket.ticket_id,
            bundle_dir=self.fixture.bundle_dir,
            reject_consumed=False,
        )
        loaded = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
            reject_consumed=False,
        )
        self.assertEqual(bundle.consumed_at, "")
        self.assertFalse(loaded.consumed)

    def test_provider_timeout_does_not_consume(self) -> None:
        from agent.coo.dispatch_bundle_store import read_bundle
        from agent.coo.production_executor_confirmation import read_confirmation

        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        with (
            patch("hermes_cli.config.load_config", return_value=self._bounded_config()),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                injected_runner=_mock_runner_timeout,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 0)
        bundle = read_bundle(
            ticket.ticket_id,
            bundle_dir=self.fixture.bundle_dir,
            reject_consumed=False,
        )
        loaded = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
            reject_consumed=False,
        )
        self.assertEqual(bundle.consumed_at, "")
        self.assertFalse(loaded.consumed)

    def test_direct_subprocess_runner_path_still_works(self) -> None:
        with (
            patch("hermes_cli.config.load_config", return_value=self._enabled_config()),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                subprocess_runner=_mock_runner_success,
            )
        self.assertEqual(exit_code, 0)

    def test_production_root_config_fail_closed(self) -> None:
        config = {
            "coo": {
                "dispatch": {
                    "executor": {
                        "enabled": True,
                        "allowed_pipeline_roots": ["/opt/data/multi-content-pipeline"],
                    },
                    "runner_provider": {"mode": "bounded"},
                }
            }
        }
        stderr = io.StringIO()
        with (
            patch("hermes_cli.config.load_config", return_value=config),
            patch.object(sys, "stderr", stderr),
        ):
            exit_code = run_coo_dispatch_from_args(
                self._run_args(),
                injected_runner=_mock_runner_success,
                use_runner_provider=True,
            )
        self.assertEqual(exit_code, 1)


class TestProviderOptInResolutionUnit(unittest.TestCase):
    def test_provider_resolution_returns_same_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            config = _bounded_enabled_config(Path(isolated_root))

            def mock_runner(argv, cwd, env, timeout):
                return 0, "", ""

            from agent.coo.dispatch_runner_binding_state import (
                CooDispatchRunnerBindingState,
                RUNNER_BINDING_STATE_BOUND,
            )

            resolved = resolve_dispatch_run_subprocess_runner(
                injected_runner=mock_runner,
                use_runner_provider=True,
                merged_config=config,
                binding_state=CooDispatchRunnerBindingState(
                    state=RUNNER_BINDING_STATE_BOUND
                ),
            )
        self.assertIs(resolved, mock_runner)

    def test_unconfigured_provider_fail_closed(self) -> None:
        from agent.coo.dispatch_runner_binding_state import (
            CooDispatchRunnerBindingState,
            RUNNER_BINDING_STATE_BOUND,
        )

        with self.assertRaises(DispatchRunnerProviderResolutionError):
            resolve_dispatch_run_subprocess_runner(
                injected_runner=_mock_runner_success,
                use_runner_provider=True,
                merged_config={},
                binding_state=CooDispatchRunnerBindingState(
                    state=RUNNER_BINDING_STATE_BOUND
                ),
            )


if __name__ == "__main__":
    unittest.main()
