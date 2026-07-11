"""Phase 11H tests — dispatch enablement gate CLI."""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_enablement import (
    REASON_EXECUTOR_ALLOWLIST_EMPTY,
    REASON_EXECUTOR_CONFIG_INVALID,
    REASON_EXECUTOR_DISABLED,
    REASON_PRODUCTION_ROOT_IN_ALLOWLIST,
    REASON_READINESS_PREFLIGHT_UNAVAILABLE,
    REASON_RUNNER_ALREADY_BOUND,
    REASON_RUNNER_BINDING_STAGED,
    REASON_RUNNER_BINDING_STATE_INVALID,
    REASON_RUNNER_PROVIDER_INVALID,
    evaluate_dispatch_enablement,
    format_dispatch_enablement_summary,
)
from agent.coo.dispatch_runner_binding_state import (
    RUNNER_BINDING_STATE_BOUND,
    RUNNER_BINDING_STATE_STAGED,
    CooDispatchRunnerBindingState,
    DispatchRunnerBindingStateError,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser, main


_DEFAULT_DISABLED_CONFIG = {
    "coo": {
        "dispatch": {
            "executor": {
                "enabled": False,
                "allowed_pipeline_roots": [],
            }
        }
    }
}


class TestDispatchEnablementGate(unittest.TestCase):
    def test_default_disabled_config_not_enablement_ready(self) -> None:
        summary = evaluate_dispatch_enablement(_DEFAULT_DISABLED_CONFIG)
        output = format_dispatch_enablement_summary(summary)
        self.assertFalse(summary.enablement_ready)
        self.assertFalse(summary.runner_bound)
        self.assertIn(REASON_EXECUTOR_DISABLED, summary.blocked_reasons)
        self.assertIn(REASON_EXECUTOR_ALLOWLIST_EMPTY, summary.blocked_reasons)
        self.assertIn("enablement_ready: false", output)
        self.assertIn("runner_bound: false", output)
        self.assertIn("runner_binding_state: unbound", output)
        self.assertIn("runner_provider_configured: false", output)
        self.assertIn("runner_provider_available: false", output)
        self.assertIn(f"blocked_reasons: {REASON_EXECUTOR_DISABLED}", output)
        self.assertNotIn("/opt/data/multi-content-pipeline", output)

    def test_enabled_isolated_allowlist_enablement_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            summary = evaluate_dispatch_enablement(
                {
                    "coo": {
                        "dispatch": {
                            "executor": {
                                "enabled": True,
                                "allowed_pipeline_roots": [isolated_root],
                            }
                        }
                    }
                }
            )
        output = format_dispatch_enablement_summary(summary)
        self.assertTrue(summary.enablement_ready)
        self.assertFalse(summary.runner_bound)
        self.assertEqual(summary.runner_binding_state, "unbound")
        self.assertEqual(summary.blocked_reasons, ())
        self.assertIn("enablement_ready: true", output)
        self.assertIn("runner_binding_state: unbound", output)
        self.assertIn("runner_provider_configured: false", output)
        self.assertIn("runner_provider_available: false", output)
        self.assertNotIn("blocked_reasons:", output)
        self.assertNotIn(isolated_root, output)

    def test_production_root_in_allowlist_blocked(self) -> None:
        for enabled in (True, False):
            summary = evaluate_dispatch_enablement(
                {
                    "coo": {
                        "dispatch": {
                            "executor": {
                                "enabled": enabled,
                                "allowed_pipeline_roots": [
                                    "/opt/data/multi-content-pipeline",
                                ],
                            }
                        }
                    }
                }
            )
            self.assertFalse(summary.enablement_ready)
            self.assertIn(REASON_EXECUTOR_CONFIG_INVALID, summary.blocked_reasons)

    def test_production_root_reason_when_policy_loads(self) -> None:
        from agent.coo.production_executor_policy import ProductionExecutorPolicy

        policy = ProductionExecutorPolicy(
            enabled=True,
            allowed_pipeline_roots=("/opt/data/multi-content-pipeline",),
        )
        with patch(
            "agent.coo.dispatch_cli_enablement.load_dispatch_executor_policy",
            return_value=policy,
        ):
            summary = evaluate_dispatch_enablement({})
        self.assertFalse(summary.enablement_ready)
        self.assertIn(REASON_PRODUCTION_ROOT_IN_ALLOWLIST, summary.blocked_reasons)

    def test_enabled_empty_allowlist_config_invalid(self) -> None:
        summary = evaluate_dispatch_enablement(
            {
                "coo": {
                    "dispatch": {
                        "executor": {
                            "enabled": True,
                            "allowed_pipeline_roots": [],
                        }
                    }
                }
            }
        )
        self.assertFalse(summary.enablement_ready)
        self.assertIn(REASON_EXECUTOR_CONFIG_INVALID, summary.blocked_reasons)

    def test_runner_already_bound_blocks_enablement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            summary = evaluate_dispatch_enablement(
                {
                    "coo": {
                        "dispatch": {
                            "executor": {
                                "enabled": True,
                                "allowed_pipeline_roots": [isolated_root],
                            }
                        }
                    }
                },
                binding_state=CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_BOUND),
            )
        self.assertFalse(summary.enablement_ready)
        self.assertTrue(summary.runner_bound)
        self.assertEqual(summary.runner_binding_state, RUNNER_BINDING_STATE_BOUND)
        self.assertIn(REASON_RUNNER_ALREADY_BOUND, summary.blocked_reasons)

    def test_runner_staged_blocks_enablement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            summary = evaluate_dispatch_enablement(
                {
                    "coo": {
                        "dispatch": {
                            "executor": {
                                "enabled": True,
                                "allowed_pipeline_roots": [isolated_root],
                            }
                        }
                    }
                },
                binding_state=CooDispatchRunnerBindingState(state=RUNNER_BINDING_STATE_STAGED),
            )
        self.assertFalse(summary.enablement_ready)
        self.assertFalse(summary.runner_bound)
        self.assertEqual(summary.runner_binding_state, RUNNER_BINDING_STATE_STAGED)
        self.assertIn(REASON_RUNNER_BINDING_STAGED, summary.blocked_reasons)

    def test_invalid_binding_state_blocks_enablement(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_enablement.load_dispatch_runner_binding_state",
            side_effect=DispatchRunnerBindingStateError("invalid"),
        ):
            summary = evaluate_dispatch_enablement(_DEFAULT_DISABLED_CONFIG)
        self.assertFalse(summary.enablement_ready)
        self.assertEqual(summary.runner_binding_state, "invalid")
        self.assertIn(REASON_RUNNER_BINDING_STATE_INVALID, summary.blocked_reasons)

    def test_readiness_preflight_unavailable_blocks_enablement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            with patch(
                "agent.coo.dispatch_cli_enablement._readiness_preflight_system_available",
                return_value=False,
            ):
                summary = evaluate_dispatch_enablement(
                    {
                        "coo": {
                            "dispatch": {
                                "executor": {
                                    "enabled": True,
                                    "allowed_pipeline_roots": [isolated_root],
                                }
                            }
                        }
                    }
                )
        self.assertFalse(summary.enablement_ready)
        self.assertIn(REASON_READINESS_PREFLIGHT_UNAVAILABLE, summary.blocked_reasons)

    def test_enablement_check_cli_not_ready_exit_one(self) -> None:
        with patch(
            "hermes_cli.config.load_config",
            return_value=dict(_DEFAULT_DISABLED_CONFIG),
        ):
            stdout = io.StringIO()
            with patch.object(sys, "stdout", stdout):
                exit_code = main(["enablement", "check"])
        self.assertEqual(exit_code, 1)
        self.assertIn("enablement_ready: false", stdout.getvalue())

    def test_enablement_check_cli_ready_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            enabled_config = {
                "coo": {
                    "dispatch": {
                        "executor": {
                            "enabled": True,
                            "allowed_pipeline_roots": [isolated_root],
                        }
                    }
                }
            }
            with patch("hermes_cli.config.load_config", return_value=enabled_config):
                with patch(
                    "agent.coo.dispatch_cli_enablement.load_dispatch_runner_binding_state",
                    return_value=CooDispatchRunnerBindingState(state="unbound"),
                ):
                    stdout = io.StringIO()
                    with (
                        patch.object(sys, "stdout", stdout),
                        patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
                        patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
                    ):
                        exit_code = main(["enablement", "check"])
        self.assertEqual(exit_code, 0)
        self.assertIn("enablement_ready: true", stdout.getvalue())
        self.assertNotIn(isolated_root, stdout.getvalue())

    def test_scaffold_provider_configured_but_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            summary = evaluate_dispatch_enablement(
                {
                    "coo": {
                        "dispatch": {
                            "executor": {
                                "enabled": True,
                                "allowed_pipeline_roots": [isolated_root],
                            },
                            "runner_provider": {"mode": "scaffold"},
                        }
                    }
                }
            )
        output = format_dispatch_enablement_summary(summary)
        self.assertTrue(summary.enablement_ready)
        self.assertTrue(summary.runner_provider_configured)
        self.assertFalse(summary.runner_provider_available)
        self.assertEqual(summary.runner_provider_mode, "scaffold")
        self.assertIn("runner_provider_mode: scaffold", output)
        self.assertNotIn(isolated_root, output)

    def test_bounded_provider_configured_but_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            summary = evaluate_dispatch_enablement(
                {
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
            )
        self.assertTrue(summary.enablement_ready)
        self.assertTrue(summary.runner_provider_configured)
        self.assertFalse(summary.runner_provider_available)
        self.assertEqual(summary.runner_provider_mode, "bounded")

    def test_unknown_provider_mode_blocks_enablement(self) -> None:
        summary = evaluate_dispatch_enablement(
            {"coo": {"dispatch": {"runner_provider": {"mode": "live"}}}}
        )
        self.assertFalse(summary.enablement_ready)
        self.assertIn(REASON_RUNNER_PROVIDER_INVALID, summary.blocked_reasons)

    def test_enablement_check_parser_registered(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["enablement", "check"])
        self.assertEqual(args.coo_dispatch_command, "enablement")
        self.assertEqual(args.coo_dispatch_enablement_command, "check")


if __name__ == "__main__":
    unittest.main()
