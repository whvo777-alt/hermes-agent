"""Tests for dispatch CLI runner injection boundary (Phase 12D)."""

from __future__ import annotations

import unittest

from agent.coo.dispatch_cli_runner_injection import (
    DISPATCH_RUNNER_NOT_CONFIGURED,
    require_dispatch_subprocess_runner,
)


def _mock_runner(argv, cwd, env, timeout):
    return 0, "", ""


class TestDispatchCliRunnerInjection(unittest.TestCase):
    def test_dry_run_does_not_require_runner(self) -> None:
        self.assertIsNone(
            require_dispatch_subprocess_runner(None, dry_run=True),
        )

    def test_non_dry_run_requires_runner(self) -> None:
        with self.assertRaises(ValueError) as exc:
            require_dispatch_subprocess_runner(None, dry_run=False)
        self.assertEqual(str(exc.exception), DISPATCH_RUNNER_NOT_CONFIGURED)

    def test_non_dry_run_returns_injected_runner(self) -> None:
        runner = require_dispatch_subprocess_runner(_mock_runner, dry_run=False)
        self.assertIs(runner, _mock_runner)


if __name__ == "__main__":
    unittest.main()
