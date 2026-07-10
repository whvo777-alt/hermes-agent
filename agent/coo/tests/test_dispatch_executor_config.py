"""Tests for COO dispatch executor config scaffold (Phase 10T)."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from agent.coo.dispatch_executor_config import (
    load_dispatch_executor_policy,
    parse_dispatch_executor_config,
)
from hermes_cli.config import DEFAULT_CONFIG


class TestDispatchExecutorConfig(unittest.TestCase):
    def test_default_enabled_false(self) -> None:
        policy = parse_dispatch_executor_config({})
        self.assertFalse(policy.enabled)

    def test_default_allowlist_empty(self) -> None:
        policy = parse_dispatch_executor_config({})
        self.assertEqual(policy.allowed_pipeline_roots, ())

    def test_default_config_section_disabled(self) -> None:
        policy = load_dispatch_executor_policy(DEFAULT_CONFIG)
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.allowed_pipeline_roots, ())

    def test_valid_disabled_config_with_allowlist(self) -> None:
        policy = parse_dispatch_executor_config(
            {
                "enabled": False,
                "allowed_pipeline_roots": ["/tmp/hermes-r2-stub"],
            }
        )
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.allowed_pipeline_roots, ("/tmp/hermes-r2-stub",))

    def test_invalid_enabled_type_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            parse_dispatch_executor_config({"enabled": "yes"})
        self.assertIn("boolean", str(exc.exception))

    def test_invalid_allowlist_type_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            parse_dispatch_executor_config({"allowed_pipeline_roots": "/tmp/x"})
        self.assertIn("list", str(exc.exception))

    def test_unknown_key_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            parse_dispatch_executor_config({"enabled": False, "extra": True})
        self.assertIn("Unknown", str(exc.exception))

    def test_enabled_true_with_empty_allowlist_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            parse_dispatch_executor_config({"enabled": True, "allowed_pipeline_roots": []})
        self.assertIn("non-empty", str(exc.exception))

    def test_production_repository2_path_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            parse_dispatch_executor_config(
                {
                    "enabled": False,
                    "allowed_pipeline_roots": ["/opt/data/multi-content-pipeline"],
                }
            )
        self.assertIn("production Repository2", str(exc.exception))

    def test_production_repository2_subdirectory_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            parse_dispatch_executor_config(
                {
                    "enabled": False,
                    "allowed_pipeline_roots": ["/opt/data/multi-content-pipeline/outputs"],
                }
            )
        self.assertIn("production Repository2", str(exc.exception))

    def test_load_dispatch_executor_policy_uses_merged_config(self) -> None:
        merged = {
            "coo": {
                "dispatch": {
                    "executor": {
                        "enabled": False,
                        "allowed_pipeline_roots": ["/tmp/isolated-root"],
                    }
                }
            }
        }
        policy = load_dispatch_executor_policy(merged)
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.allowed_pipeline_roots, ("/tmp/isolated-root",))

    def test_subprocess_not_used_by_config_loader(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")):
                policy = load_dispatch_executor_policy(DEFAULT_CONFIG)
        self.assertFalse(policy.enabled)


if __name__ == "__main__":
    unittest.main()
