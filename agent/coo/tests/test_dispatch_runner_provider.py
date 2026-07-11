"""Tests for bounded subprocess runner provider (Phase 12E-1 / 12E-2)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_enablement import (
    REASON_EXECUTOR_DISABLED,
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
    RUNNER_BINDING_STATE_STAGED,
    CooDispatchRunnerBindingState,
)
from agent.coo.dispatch_runner_provider import (
    REASON_RUNNER_BINDING_STAGED,
    REASON_RUNNER_BINDING_UNBOUND,
    REASON_RUNNER_PROVIDER_INJECTED_RUNNER_INVALID,
    REASON_RUNNER_PROVIDER_INJECTED_RUNNER_REQUIRED,
    REASON_RUNNER_PROVIDER_MODE_NOT_BOUNDED,
    RUNNER_PROVIDER_MODE_BOUNDED,
    RUNNER_PROVIDER_MODE_SCAFFOLD,
    DispatchRunnerProviderResolutionError,
    assess_dispatch_runner_provider,
    format_dispatch_runner_provider_summary,
)
from agent.coo.production_executor_factory import _run_bounded_subprocess


def _mock_runner(argv, cwd, env, timeout):
    return 0, "", ""


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
        output = format_dispatch_runner_provider_summary(summary)
        self.assertIn("runner_provider_mode: scaffold", output)
        self.assertNotIn("command", output)

    def test_bounded_mode_configured_but_unavailable_without_injection(self) -> None:
        summary = assess_dispatch_runner_provider(
            {"coo": {"dispatch": {"runner_provider": {"mode": "bounded"}}}}
        )
        self.assertTrue(summary.runner_provider_configured)
        self.assertFalse(summary.runner_provider_available)
        self.assertEqual(summary.runner_provider_mode, RUNNER_PROVIDER_MODE_BOUNDED)

    def test_unknown_provider_mode_rejected(self) -> None:
        summary = assess_dispatch_runner_provider(
            {"coo": {"dispatch": {"runner_provider": {"mode": "live"}}}}
        )
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
                            "mode": "bounded",
                            "command": "/usr/bin/node",
                        }
                    }
                }
            }
        )
        self.assertFalse(summary.provider_valid)

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
        self.assertIn("runner_provider_mode: scaffold", output)

    def test_invalid_provider_blocks_enablement(self) -> None:
        summary = evaluate_dispatch_enablement(
            {"coo": {"dispatch": {"runner_provider": {"mode": "live"}}}}
        )
        self.assertFalse(summary.enablement_ready)
        self.assertIn(REASON_RUNNER_PROVIDER_INVALID, summary.blocked_reasons)

    def test_explicit_injection_boundary_unchanged(self) -> None:
        resolved = require_dispatch_subprocess_runner(_mock_runner, dry_run=False)
        self.assertIs(resolved, _mock_runner)
        with self.assertRaises(ValueError) as exc:
            require_dispatch_subprocess_runner(None, dry_run=False)
        self.assertEqual(str(exc.exception), DISPATCH_RUNNER_NOT_CONFIGURED)

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
        with self.assertRaises(DispatchRunnerProviderResolutionError):
            resolve_bounded_subprocess_runner({})


class TestDispatchRunnerProviderBoundedOptIn(unittest.TestCase):
    def _bounded_ready_config(self) -> tuple[dict, CooDispatchRunnerBindingState]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        isolated_root = str(Path(tmp.name) / "fake-pipeline")
        Path(isolated_root).mkdir()
        config = _enabled_config(isolated_root, provider_mode="bounded")
        binding = CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_BOUND)
        return config, binding

    def test_scaffold_with_injected_runner_rejected(self) -> None:
        config, binding = self._bounded_ready_config()
        config["coo"]["dispatch"]["runner_provider"] = {"mode": "scaffold"}
        with self.assertRaises(DispatchRunnerProviderResolutionError) as exc:
            resolve_bounded_subprocess_runner(
                config,
                injected_runner=_mock_runner,
                binding_state=binding,
            )
        self.assertIn(REASON_RUNNER_PROVIDER_MODE_NOT_BOUNDED, str(exc.exception))

    def test_bounded_without_injected_runner_rejected(self) -> None:
        config, binding = self._bounded_ready_config()
        with self.assertRaises(DispatchRunnerProviderResolutionError) as exc:
            resolve_bounded_subprocess_runner(
                config,
                binding_state=binding,
            )
        self.assertIn(REASON_RUNNER_PROVIDER_INJECTED_RUNNER_REQUIRED, str(exc.exception))

    def test_bounded_with_non_callable_rejected(self) -> None:
        config, binding = self._bounded_ready_config()
        with self.assertRaises(DispatchRunnerProviderResolutionError) as exc:
            resolve_bounded_subprocess_runner(
                config,
                injected_runner="not-a-runner",
                binding_state=binding,
            )
        self.assertIn(REASON_RUNNER_PROVIDER_INJECTED_RUNNER_INVALID, str(exc.exception))

    def test_bounded_binding_unbound_rejected(self) -> None:
        config, _binding = self._bounded_ready_config()
        with self.assertRaises(DispatchRunnerProviderResolutionError) as exc:
            resolve_bounded_subprocess_runner(
                config,
                injected_runner=_mock_runner,
                binding_state=CooDispatchRunnerBindingState(state="unbound"),
            )
        self.assertIn(REASON_RUNNER_BINDING_UNBOUND, str(exc.exception))

    def test_bounded_binding_staged_rejected(self) -> None:
        config, _binding = self._bounded_ready_config()
        with self.assertRaises(DispatchRunnerProviderResolutionError) as exc:
            resolve_bounded_subprocess_runner(
                config,
                injected_runner=_mock_runner,
                binding_state=CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_STAGED),
            )
        self.assertIn(REASON_RUNNER_BINDING_STAGED, str(exc.exception))

    def test_bounded_ready_returns_same_injected_callable(self) -> None:
        config, binding = self._bounded_ready_config()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            resolved = resolve_bounded_subprocess_runner(
                config,
                injected_runner=_mock_runner,
                binding_state=binding,
            )
        self.assertIs(resolved, _mock_runner)

    def test_bounded_disabled_executor_rejected(self) -> None:
        config, binding = self._bounded_ready_config()
        config["coo"]["dispatch"]["executor"]["enabled"] = False
        with self.assertRaises(DispatchRunnerProviderResolutionError) as exc:
            resolve_bounded_subprocess_runner(
                config,
                injected_runner=_mock_runner,
                binding_state=binding,
            )
        self.assertIn(REASON_EXECUTOR_DISABLED, str(exc.exception))

    def test_bounded_empty_allowlist_rejected(self) -> None:
        config, binding = self._bounded_ready_config()
        config["coo"]["dispatch"]["executor"]["allowed_pipeline_roots"] = []
        with self.assertRaises(DispatchRunnerProviderResolutionError):
            resolve_bounded_subprocess_runner(
                config,
                injected_runner=_mock_runner,
                binding_state=binding,
            )

    def test_bounded_production_root_rejected(self) -> None:
        config, binding = self._bounded_ready_config()
        config["coo"]["dispatch"]["executor"]["allowed_pipeline_roots"] = [
            "/opt/data/multi-content-pipeline",
        ]
        with self.assertRaises(DispatchRunnerProviderResolutionError):
            resolve_bounded_subprocess_runner(
                config,
                injected_runner=_mock_runner,
                binding_state=binding,
            )

    def test_unknown_mode_resolution_rejected(self) -> None:
        with self.assertRaises(DispatchRunnerProviderResolutionError) as exc:
            resolve_bounded_subprocess_runner(
                {"coo": {"dispatch": {"runner_provider": {"mode": "live"}}}},
                injected_runner=_mock_runner,
                binding_state=CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_BOUND),
            )
        self.assertIn(REASON_RUNNER_PROVIDER_INVALID, str(exc.exception))


if __name__ == "__main__":
    unittest.main()
