"""Phase 10Y tests — dispatch executor config validate CLI."""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_config_validate import (
    format_dispatch_executor_config_validation_summary,
    validate_dispatch_executor_config,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser, main
from hermes_cli.config import DEFAULT_CONFIG


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


class TestDispatchExecutorConfigValidate(unittest.TestCase):
    def test_default_disabled_config_is_valid(self) -> None:
        summary = validate_dispatch_executor_config(_DEFAULT_DISABLED_CONFIG)
        output = format_dispatch_executor_config_validation_summary(summary)
        self.assertFalse(summary.executor_enabled)
        self.assertEqual(summary.executor_allowlist_count, 0)
        self.assertTrue(summary.config_valid)
        self.assertIn("executor_enabled: false", output)
        self.assertIn("executor_allowlist_count: 0", output)
        self.assertIn("config_valid: true", output)

    def test_enabled_with_isolated_allowlist_is_valid(self) -> None:
        isolated_root = "/tmp/hermes-config-validate-stub"
        summary = validate_dispatch_executor_config(
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
        self.assertTrue(summary.executor_enabled)
        self.assertEqual(summary.executor_allowlist_count, 1)
        output = format_dispatch_executor_config_validation_summary(summary)
        self.assertIn("executor_enabled: true", output)
        self.assertIn("executor_allowlist_count: 1", output)
        self.assertNotIn(isolated_root, output)

    def test_enabled_with_empty_allowlist_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            validate_dispatch_executor_config(
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
        self.assertIn("non-empty", str(exc.exception))

    def test_unknown_key_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            validate_dispatch_executor_config(
                {
                    "coo": {
                        "dispatch": {
                            "executor": {
                                "enabled": False,
                                "extra": True,
                            }
                        }
                    }
                }
            )
        self.assertIn("Unknown", str(exc.exception))

    def test_invalid_enabled_type_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            validate_dispatch_executor_config(
                {
                    "coo": {
                        "dispatch": {
                            "executor": {
                                "enabled": "yes",
                            }
                        }
                    }
                }
            )
        self.assertIn("boolean", str(exc.exception))

    def test_production_repository2_root_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            validate_dispatch_executor_config(
                {
                    "coo": {
                        "dispatch": {
                            "executor": {
                                "enabled": False,
                                "allowed_pipeline_roots": [
                                    "/opt/data/multi-content-pipeline"
                                ],
                            }
                        }
                    }
                }
            )
        self.assertIn("production Repository2", str(exc.exception))
        self.assertNotIn("/opt/data/multi-content-pipeline", str(exc.exception))

    def test_output_excludes_allowlist_paths(self) -> None:
        isolated_root = str(Path(tempfile.gettempdir()) / "hermes-secret-allowlist-path")
        summary = validate_dispatch_executor_config(
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
        output = format_dispatch_executor_config_validation_summary(summary)
        self.assertNotIn(isolated_root, output)
        self.assertNotIn("/opt/data/multi-content-pipeline", output)

    def test_cli_validate_success_output(self) -> None:
        stdout = io.StringIO()
        with (
            patch("hermes_cli.config.load_config", return_value=_DEFAULT_DISABLED_CONFIG),
            patch.object(sys, "stdout", stdout),
        ):
            exit_code = main(["config", "validate"])
        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("config_valid: true", output)
        self.assertIn("executor_enabled: false", output)

    def test_cli_validate_failure_exit_code(self) -> None:
        stderr = io.StringIO()
        bad_config = {
            "coo": {
                "dispatch": {
                    "executor": {
                        "enabled": True,
                        "allowed_pipeline_roots": [],
                    }
                }
            }
        }
        with (
            patch("hermes_cli.config.load_config", return_value=bad_config),
            patch.object(sys, "stderr", stderr),
        ):
            exit_code = main(["config", "validate"])
        self.assertEqual(exit_code, 1)
        combined = stderr.getvalue()
        self.assertIn("error:", combined)
        self.assertNotIn("/opt/data/multi-content-pipeline", combined)

    def test_cli_validate_does_not_print_allowlist_paths(self) -> None:
        isolated_root = "/tmp/hermes-config-validate-cli-path"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch(
                "hermes_cli.config.load_config",
                return_value={
                    "coo": {
                        "dispatch": {
                            "executor": {
                                "enabled": True,
                                "allowed_pipeline_roots": [isolated_root],
                            }
                        }
                    }
                },
            ),
            patch.object(sys, "stdout", stdout),
            patch.object(sys, "stderr", stderr),
        ):
            exit_code = main(["config", "validate"])
        self.assertEqual(exit_code, 0)
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(isolated_root, combined)

    def test_subprocess_not_used(self) -> None:
        with (
            patch("hermes_cli.config.load_config", return_value=_DEFAULT_DISABLED_CONFIG),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = main(["config", "validate"])
        self.assertEqual(exit_code, 0)

    def test_parser_accepts_config_validate(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["config", "validate"])
        self.assertEqual(args.coo_dispatch_command, "config")
        self.assertEqual(args.coo_dispatch_config_command, "validate")

    def test_load_default_config_section_is_valid(self) -> None:
        summary = validate_dispatch_executor_config(DEFAULT_CONFIG)
        self.assertFalse(summary.executor_enabled)
        self.assertEqual(summary.executor_allowlist_count, 0)


if __name__ == "__main__":
    unittest.main()
