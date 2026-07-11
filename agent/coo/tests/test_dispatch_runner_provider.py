"""Tests for bounded subprocess runner provider scaffold (Phase 12E-1)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_enablement import (
    REASON_RUNNER_PROVIDER_INVALID,
    evaluate_dispatch_enablement,
    format_dispatch_enablement_summary,
)
from agent.coo.dispatch_cli_runner_injection import (
    DISPATCH_RUNNER_NOT_CONFIGURED,
    require_dispatch_subprocess_runner,
    resolve_bounded_subprocess_runner,
)
from agent.coo.dispatch_runner_binding_state import (
    RUNNER_BINDING_STATE_BOUND,
    CooDispatchRunnerBindingState,
)
from agent.coo.dispatch_runner_provider import (
    RUNNER_PROVIDER_MODE_SCAFFOLD,
    assess_dispatch_runner_provider,
    format_dispatch_runner_provider_summary,
    resolve_bounded_subprocess_runner as provider_resolve,
)
from agent.coo.production_executor_factory import _run_bounded_subprocess


def _enabled_config(pipeline_root: str, *, provider_mode: str | None = None) -> dict:
    dispatch: dict = {
        "executor": {
            "enabled": True,
            "allowed_pipeline_roots": [pipeline_root],
        }
    }
    if provider_mode is not None:
        dispatch["runner_provider"] = {"mode": provider_mode}
    return {"coo": {"dispatch": dispatch}}


class TestDispatchRunnerProviderScaffold(unittest.TestCase):
    def test_default_provider_unconfigured_unavailable(self) -> None:
        summary = assess_dispatch_runner_provider({})
        self.assertFalse(summary.runner_provider_configured)
        self.assertFalse(summary.runner_provider_available)
        self.assertEqual(summary.runner_provider_mode, "")
        self.assertTrue(summary.provider_valid)

    def test_scaffold_mode_configured_but_unavailable(self) -> None:
        summary = assess_dispatch_runner_provider(
            {"coo": {"dispatch": {"runner_provider": {"mode": "scaffold"}}}}
        )
        self.assertTrue(summary.runner_provider_configured)
        self.assertFalse(summary.runner_provider_available)
        self.assertEqual(summary.runner_provider_mode, RUNNER_PROVIDER_MODE_SCAFFOLD)
        self.assertTrue(summary.provider_valid)
        output = format_dispatch_runner_provider_summary(summary)
        self.assertIn("runner_provider_mode: scaffold", output)
        self.assertNotIn("command", output)
        self.assertNotIn("token", output)

    def test_unknown_provider_mode_rejected(self) -> None:
        summary = assess_dispatch_runner_provider(
            {"coo": {"dispatch": {"runner_provider": {"mode": "live"}}}}
        )
        self.assertFalse(summary.runner_provider_configured)
        self.assertFalse(summary.provider_valid)

    def test_malformed_provider_section_rejected(self) -> None:
        summary = assess_dispatch_runner_provider(
            {"coo": {"dispatch": {"runner_provider": "bad"}}}
        )
        self.assertFalse(summary.provider_valid)

    def test_unknown_provider_keys_rejected(self) -> None:
        summary = assess_dispatch_runner_provider(
            {
                "coo": {
                    "dispatch": {
                        "runner_provider": {
                            "mode": "scaffold",
                            "command": "/usr/bin/node",
                        }
                    }
                }
            }
        )
        self.assertFalse(summary.provider_valid)

    def test_bound_enabled_scaffold_still_returns_no_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            config = _enabled_config(isolated_root, provider_mode="scaffold")
            with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
                with patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")):
                    runner = resolve_bounded_subprocess_runner(config)
            self.assertIsNone(runner)
            self.assertIsNone(provider_resolve(config))

    def test_provider_resolve_never_calls_bounded_subprocess(self) -> None:
        with patch(
            "agent.coo.production_executor_factory.subprocess.run",
            side_effect=AssertionError("no subprocess"),
        ):
            with self.assertRaises(AssertionError):
                _run_bounded_subprocess(
                    ["node", "pipeline.js"],
                    cwd="/tmp/fake-pipeline",
                    env={},
                    timeout_seconds=30,
                )
        self.assertIsNone(resolve_bounded_subprocess_runner({}))

    def test_enablement_includes_provider_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            summary = evaluate_dispatch_enablement(
                _enabled_config(isolated_root, provider_mode="scaffold")
            )
        output = format_dispatch_enablement_summary(summary)
        self.assertTrue(summary.runner_provider_configured)
        self.assertFalse(summary.runner_provider_available)
        self.assertEqual(summary.runner_provider_mode, RUNNER_PROVIDER_MODE_SCAFFOLD)
        self.assertIn("runner_provider_configured: true", output)
        self.assertIn("runner_provider_available: false", output)
        self.assertIn("runner_provider_mode: scaffold", output)

    def test_invalid_provider_blocks_enablement(self) -> None:
        summary = evaluate_dispatch_enablement(
            {"coo": {"dispatch": {"runner_provider": {"mode": "live"}}}}
        )
        self.assertFalse(summary.enablement_ready)
        self.assertIn(REASON_RUNNER_PROVIDER_INVALID, summary.blocked_reasons)

    def test_bound_with_scaffold_provider_still_no_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            config = _enabled_config(isolated_root, provider_mode="scaffold")
            summary = evaluate_dispatch_enablement(
                config,
                binding_state=CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_BOUND),
            )
            runner = resolve_bounded_subprocess_runner(config)
        self.assertIsNone(runner)
        self.assertTrue(summary.runner_provider_configured)
        self.assertFalse(summary.runner_provider_available)

    def test_explicit_injection_boundary_unchanged(self) -> None:
        def mock_runner(argv, cwd, env, timeout):
            return 0, "", ""

        resolved = require_dispatch_subprocess_runner(mock_runner, dry_run=False)
        self.assertIs(resolved, mock_runner)
        with self.assertRaises(ValueError) as exc:
            require_dispatch_subprocess_runner(None, dry_run=False)
        self.assertEqual(str(exc.exception), DISPATCH_RUNNER_NOT_CONFIGURED)


if __name__ == "__main__":
    unittest.main()
