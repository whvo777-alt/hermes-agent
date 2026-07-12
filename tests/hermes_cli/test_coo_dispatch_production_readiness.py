"""Phase 12S tests — dispatch production readiness review CLI."""

from __future__ import annotations

import hashlib
import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_production_readiness import (
    CHECK_BLOCKED,
    CHECK_FAIL,
    CHECK_PASS,
    OVERALL_NOT_READY,
    OVERALL_READY,
    RECOMMENDED_NEXT_PHASE_READY,
    evaluate_dispatch_production_readiness,
    format_dispatch_production_readiness,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser

_FORBIDDEN_OUTPUT_TOKENS = (
    "argv",
    "cwd",
    "env",
    "stdout",
    "stderr",
    "snapshot",
    "token",
    "SECRET",
    "PASSWORD",
    "phrase",
    "pipeline.js",
    "/opt/data/multi-content-pipeline",
)


def _hermes_digest(root: Path) -> str:
    if not root.exists():
        return ""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            parts.append(f"{rel}:{digest}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


class TestProductionReadinessChecks(unittest.TestCase):
    def test_all_checks_pass_by_default(self) -> None:
        summary = evaluate_dispatch_production_readiness()
        self.assertEqual(summary.overall, OVERALL_READY)
        self.assertEqual(summary.blocking_items, ())
        self.assertEqual(summary.recommended_next_phase, RECOMMENDED_NEXT_PHASE_READY)
        statuses = {check.name: check.status for check in summary.checks}
        self.assertEqual(statuses["binding"], CHECK_PASS)
        self.assertEqual(statuses["provider"], CHECK_PASS)
        self.assertEqual(statuses["runner_harness"], CHECK_PASS)
        self.assertEqual(statuses["dispatch_profile"], CHECK_PASS)
        self.assertEqual(statuses["runtime_gates"], CHECK_PASS)
        self.assertEqual(statuses["enablement"], CHECK_PASS)
        self.assertEqual(statuses["evidence"], CHECK_PASS)
        self.assertEqual(statuses["audit"], CHECK_PASS)
        self.assertEqual(statuses["consume"], CHECK_PASS)
        self.assertEqual(statuses["recovery"], CHECK_PASS)
        self.assertEqual(statuses["repair"], CHECK_PASS)
        self.assertEqual(statuses["operator"], CHECK_PASS)
        self.assertEqual(statuses["repository2_policy"], CHECK_PASS)
        self.assertEqual(statuses["gateway"], CHECK_BLOCKED)

    def test_gateway_blocked_is_intentional_safety(self) -> None:
        summary = evaluate_dispatch_production_readiness()
        gateway = next(check for check in summary.checks if check.name == "gateway")
        self.assertEqual(gateway.status, CHECK_BLOCKED)
        self.assertEqual(summary.overall, OVERALL_READY)

    def test_repository2_policy_disabled_for_execution(self) -> None:
        summary = evaluate_dispatch_production_readiness()
        policy = summary.repository2_policy
        self.assertEqual(policy.production_root_hard_deny, "enabled")
        self.assertEqual(policy.read_only_only, "enabled")
        self.assertEqual(policy.execution_disabled, "enabled")
        self.assertEqual(policy.gateway_disabled, "enabled")

    def test_missing_capability_marks_fail(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_production_readiness._evaluate_consume_check",
            return_value=CHECK_FAIL,
        ):
            summary = evaluate_dispatch_production_readiness()
        self.assertEqual(summary.overall, OVERALL_NOT_READY)
        self.assertIn("consume", summary.blocking_items)

    def test_format_includes_required_sections(self) -> None:
        summary = evaluate_dispatch_production_readiness()
        output = format_dispatch_production_readiness(summary)
        self.assertIn("Production Readiness", output)
        self.assertIn("overall: READY", output)
        self.assertIn("Checks", output)
        self.assertIn("binding: PASS", output)
        self.assertIn("gateway: BLOCKED", output)
        self.assertIn("Repository2 Policy", output)
        self.assertIn("production_root_hard_deny: enabled", output)
        self.assertIn("execution_disabled: enabled", output)
        self.assertIn("gateway_disabled: enabled", output)
        self.assertIn("blocking_items: (none)", output)
        self.assertIn("recommended_next_phase:", output)

    def test_safe_output_has_no_secrets_or_paths(self) -> None:
        summary = evaluate_dispatch_production_readiness()
        output = format_dispatch_production_readiness(summary).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)
        self.assertNotIn("reason:", output.replace("blocking_items:", ""))


class TestProductionReadinessCli(unittest.TestCase):
    def test_cli_production_readiness_exit_zero(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["production", "readiness"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        output = buf.getvalue()
        self.assertIn("overall: READY", output)
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output.lower())

    def test_cli_not_ready_exit_one(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["production", "readiness"])
        with (
            patch(
                "agent.coo.dispatch_cli_production_readiness._evaluate_repair_check",
                return_value=CHECK_FAIL,
            ),
            patch("sys.stdout", io.StringIO()),
        ):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 1)

    def test_read_only_no_subprocess_or_writes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            before = _hermes_digest(hermes_home)
            with (
                patch.dict("os.environ", {"HERMES_HOME": str(hermes_home)}),
                patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
                patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            ):
                evaluate_dispatch_production_readiness()
                format_dispatch_production_readiness(evaluate_dispatch_production_readiness())
            self.assertEqual(_hermes_digest(hermes_home), before)

    def test_production_root_hard_deny_rejects_repository2_path(self) -> None:
        from agent.coo.dispatch_pipeline_root_trust import assert_pipeline_root_allowed

        with self.assertRaises(ValueError):
            assert_pipeline_root_allowed("/opt/data/multi-content-pipeline")
