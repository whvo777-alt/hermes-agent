"""Tests for bounded subprocess runner safety harness (Phase 12E-4)."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.bounded_subprocess_runner import (
    BoundedSubprocessRunnerError,
    create_bounded_subprocess_runner,
)
from agent.coo.dispatch_cli_runner_injection import (
    DISPATCH_RUNNER_NOT_CONFIGURED,
    create_bounded_subprocess_runner as exported_create_runner,
    require_dispatch_subprocess_runner,
    resolve_bounded_subprocess_runner,
    resolve_dispatch_run_subprocess_runner,
)
from agent.coo.dispatch_runner_binding_state import (
    RUNNER_BINDING_STATE_BOUND,
    CooDispatchRunnerBindingState,
)
from agent.coo.dispatch_runner_provider import DispatchRunnerProviderResolutionError
from agent.coo.production_executor_factory import _TIMEOUT_EXIT_CODE


def _python_argv(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class TestBoundedSubprocessRunnerHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-bounded-runner-")
        self.workspace = Path(self.tmp.name)
        self.allowed_root = self.workspace / "pipeline"
        self.allowed_root.mkdir()
        self.runner = create_bounded_subprocess_runner((str(self.allowed_root),))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "SECRET_TOKEN": "must-not-forward",
            "API_KEY": "must-not-forward",
        }

    def test_isolated_python_success(self) -> None:
        exit_code, stdout, stderr = self.runner(
            _python_argv("print('ok')"),
            str(self.allowed_root),
            self._env(),
            30,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("ok", stdout)
        self.assertEqual(stderr, "")

    def test_non_zero_exit(self) -> None:
        exit_code, stdout, stderr = self.runner(
            _python_argv("import sys; sys.exit(2)"),
            str(self.allowed_root),
            self._env(),
            30,
        )
        self.assertEqual(exit_code, 2)

    def test_timeout(self) -> None:
        exit_code, stdout, stderr = self.runner(
            _python_argv("import time; time.sleep(3)"),
            str(self.allowed_root),
            self._env(),
            1,
        )
        self.assertEqual(exit_code, _TIMEOUT_EXIT_CODE)
        self.assertIn("timeout after 1s", stderr)

    def test_rejects_string_command(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                "python -c print('bad')",  # type: ignore[arg-type]
                str(self.allowed_root),
                self._env(),
                30,
            )

    def test_rejects_empty_argv(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner([], str(self.allowed_root), self._env(), 30)

    def test_rejects_node_executable(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                ["node", "pipeline.js"],
                str(self.allowed_root),
                self._env(),
                30,
            )

    def test_rejects_cwd_outside_allowlist(self) -> None:
        outside = self.workspace / "outside"
        outside.mkdir()
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                _python_argv("print('ok')"),
                str(outside),
                self._env(),
                30,
            )

    def test_rejects_production_root_cwd(self) -> None:
        with self.assertRaises(ValueError):
            self.runner(
                _python_argv("print('ok')"),
                "/opt/data/multi-content-pipeline",
                self._env(),
                30,
            )

    def test_rejects_symlink_escape(self) -> None:
        outside = self.workspace / "outside"
        outside.mkdir()
        link = self.allowed_root / "escape-link"
        link.symlink_to(outside)
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                _python_argv("print('ok')"),
                str(link),
                self._env(),
                30,
            )

    def test_env_secret_keys_not_forwarded(self) -> None:
        exit_code, stdout, stderr = self.runner(
            _python_argv(
                "import os; print('SECRET_TOKEN' in os.environ, 'API_KEY' in os.environ)"
            ),
            str(self.allowed_root),
            self._env(),
            30,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("False False", stdout.replace("\n", " "))

    def test_stdout_stderr_truncated(self) -> None:
        runner = create_bounded_subprocess_runner(
            (str(self.allowed_root),),
            max_output_bytes=128,
        )
        exit_code, stdout, stderr = runner(
            _python_argv("print('x' * 1000)"),
            str(self.allowed_root),
            self._env(),
            30,
        )
        self.assertEqual(exit_code, 0)
        self.assertLessEqual(len(stdout.encode("utf-8")), 200)
        self.assertIn("...[truncated]", stdout)

    def test_fixture_executable_success(self) -> None:
        fixture = self.allowed_root / "ok_fixture.py"
        fixture.write_text(
            textwrap.dedent(
                """
                import sys
                print("fixture-ok")
                sys.exit(0)
                """
            ),
            encoding="utf-8",
        )
        exit_code, stdout, stderr = self.runner(
            [sys.executable, str(fixture)],
            str(self.allowed_root),
            self._env(),
            30,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("fixture-ok", stdout)

    def test_timeout_upper_bound_enforced(self) -> None:
        runner = create_bounded_subprocess_runner(
            (str(self.allowed_root),),
            max_timeout_seconds=5,
        )
        with self.assertRaises(BoundedSubprocessRunnerError):
            runner(
                _python_argv("print('ok')"),
                str(self.allowed_root),
                self._env(),
                30,
            )


class TestBoundedRunnerNotAutoWired(unittest.TestCase):
    def test_provider_still_requires_injected_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            config = {
                "coo": {
                    "dispatch": {
                        "executor": {
                            "enabled": True,
                            "allowed_pipeline_roots": [isolated_root],
                        },
                        "runner_provider": {"mode": "bounded"},
                    }
                }
            }
            with self.assertRaises(DispatchRunnerProviderResolutionError):
                resolve_bounded_subprocess_runner(config)

    def test_default_cli_resolution_still_fail_closed(self) -> None:
        with self.assertRaises(ValueError) as exc:
            require_dispatch_subprocess_runner(None, dry_run=False)
        self.assertEqual(str(exc.exception), DISPATCH_RUNNER_NOT_CONFIGURED)

    def test_provider_opt_in_does_not_create_real_runner(self) -> None:
        with (
            patch(
                "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
                side_effect=AssertionError("must not auto-create real runner"),
            ),
            patch(
                "agent.coo.dispatch_cli_runner_injection.create_bounded_subprocess_runner",
                side_effect=AssertionError("must not auto-create real runner"),
            ),
        ):
            with self.assertRaises(Exception):
                resolve_dispatch_run_subprocess_runner(
                    injected_runner=None,
                    use_runner_provider=True,
                    merged_config={
                        "coo": {
                            "dispatch": {
                                "executor": {"enabled": True, "allowed_pipeline_roots": ["/tmp/x"]},
                                "runner_provider": {"mode": "bounded"},
                            }
                        }
                    },
                    binding_state=CooDispatchRunnerBindingState(
                        state=RUNNER_BINDING_STATE_BOUND
                    ),
                )

    def test_create_helper_exported_for_explicit_use_only(self) -> None:
        self.assertIs(exported_create_runner, create_bounded_subprocess_runner)


if __name__ == "__main__":
    unittest.main()
