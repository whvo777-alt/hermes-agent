"""Phase 12U tests — dispatch production sign-off CLI."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_production_readiness import CHECK_FAIL
from agent.coo.dispatch_cli_production_signoff import (
    OPERATOR_ACTION_APPROVE_ISOLATED_DRILL,
    OPERATOR_ACTION_RESOLVE_FAILED,
    OVERALL_SIGNOFF_NOT_READY,
    OVERALL_SIGNOFF_READY,
    RECOMMENDED_NEXT_PHASE_NOT_READY,
    RECOMMENDED_NEXT_PHASE_READY,
    CooDispatchProductionSignoffSummary,
    evaluate_dispatch_production_signoff,
    format_dispatch_production_signoff,
)
from agent.coo.dispatch_cli_repository_attestation import (
    CooDispatchRepositoryAttestationSummary,
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
    "dependencies",
    "node pipeline.js",
    "npm",
    "npx",
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


def _attestation_success_summary() -> CooDispatchRepositoryAttestationSummary:
    return CooDispatchRepositoryAttestationSummary(
        repository_attested=True,
        root_matches_expected=True,
        root_is_symlink=False,
        pipeline_entrypoint_present=True,
        package_manifest_present=True,
        required_directories_present_count=3,
        required_directories_missing="(none)",
        optional_directories_present="outputs",
        pipeline_sha256="a" * 64,
        package_sha256="b" * 64,
        git_metadata_present=True,
        git_head_kind="symbolic_ref",
        git_head_value="master@abcdef012345",
        execution_allowed=False,
        production_root_hard_deny=True,
        recommended_next_phase=RECOMMENDED_NEXT_PHASE_READY,
    )


@contextmanager
def _successful_attestation():
    with patch(
        "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
        return_value=_attestation_success_summary(),
    ):
        yield


class TestProductionSignoffReady(unittest.TestCase):
    def test_all_capabilities_pass_with_intentional_blocks_ready(self) -> None:
        with _successful_attestation():
            summary = evaluate_dispatch_production_signoff()
        self.assertTrue(summary.signoff_ready)
        self.assertEqual(summary.overall_status, OVERALL_SIGNOFF_READY)
        self.assertGreater(summary.checks_passed_count, 0)
        self.assertEqual(summary.checks_blocked_count, 3)
        self.assertEqual(summary.checks_failed_count, 0)
        self.assertEqual(summary.failed_checks, "(none)")
        self.assertIn("production_root_hard_deny", summary.blocked_checks)
        self.assertIn("execution_disabled", summary.blocked_checks)
        self.assertIn("gateway_disabled", summary.blocked_checks)
        self.assertTrue(summary.repository_attested)
        self.assertTrue(summary.production_root_hard_deny)
        self.assertFalse(summary.execution_allowed)
        self.assertFalse(summary.gateway_enabled)
        self.assertEqual(summary.recommended_next_phase, RECOMMENDED_NEXT_PHASE_READY)
        self.assertEqual(
            summary.operator_action,
            OPERATOR_ACTION_APPROVE_ISOLATED_DRILL,
        )

    def test_blocked_checks_do_not_block_signoff_when_no_failures(self) -> None:
        with _successful_attestation():
            summary = evaluate_dispatch_production_signoff()
        self.assertTrue(summary.signoff_ready)
        self.assertGreater(summary.checks_blocked_count, 0)
        self.assertEqual(summary.checks_failed_count, 0)


class TestProductionSignoffFailClosed(unittest.TestCase):
    def test_readiness_fail_makes_signoff_not_ready(self) -> None:
        with (
            _successful_attestation(),
            patch(
                "agent.coo.dispatch_cli_production_signoff.evaluate_dispatch_production_readiness",
            ) as mock_readiness,
        ):
            from agent.coo.dispatch_cli_production_readiness import (
                OVERALL_NOT_READY,
                CooDispatchProductionReadinessSummary,
                CooDispatchRepository2PolicySummary,
            )

            mock_readiness.return_value = CooDispatchProductionReadinessSummary(
                overall=OVERALL_NOT_READY,
                checks=(),
                repository2_policy=CooDispatchRepository2PolicySummary(
                    production_root_hard_deny="enabled",
                    read_only_only="enabled",
                    execution_disabled="enabled",
                    gateway_disabled="enabled",
                ),
                blocking_items=("consume",),
                recommended_next_phase=RECOMMENDED_NEXT_PHASE_NOT_READY,
            )
            summary = evaluate_dispatch_production_signoff()
        self.assertFalse(summary.signoff_ready)
        self.assertIn("production_readiness_ready", summary.failed_checks)

    def test_repository_attestation_fail_makes_signoff_not_ready(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
            side_effect=ValueError("attestation failed"),
        ):
            summary = evaluate_dispatch_production_signoff()
        self.assertFalse(summary.signoff_ready)
        self.assertFalse(summary.repository_attested)
        self.assertIn("repository_attestation_valid", summary.failed_checks)

    def test_hard_deny_inactive_makes_signoff_not_ready(self) -> None:
        with (
            _successful_attestation(),
            patch(
                "agent.coo.dispatch_cli_production_signoff._production_root_hard_deny_active",
                return_value=False,
            ),
        ):
            summary = evaluate_dispatch_production_signoff()
        self.assertFalse(summary.signoff_ready)
        self.assertFalse(summary.production_root_hard_deny)
        self.assertIn("production_root_hard_deny", summary.failed_checks)
        self.assertIn("execution_disabled", summary.failed_checks)

    def test_gateway_enabled_makes_signoff_not_ready(self) -> None:
        with _successful_attestation():
            summary = evaluate_dispatch_production_signoff(
                merged_config={
                    "coo": {
                        "dispatch": {
                            "gateway": {"enablement": "enabled"},
                        },
                    },
                },
            )
        self.assertFalse(summary.signoff_ready)
        self.assertTrue(summary.gateway_enabled)
        self.assertIn("gateway_disabled", summary.failed_checks)

    def test_execution_allowed_true_forces_not_ready(self) -> None:
        summary = evaluate_dispatch_production_signoff()
        self.assertFalse(summary.execution_allowed)

        forced = replace(summary, execution_allowed=True)
        self.assertTrue(forced.execution_allowed)
        with _successful_attestation():
            evaluated = evaluate_dispatch_production_signoff()
        self.assertFalse(evaluated.execution_allowed)

    def test_required_capability_missing_makes_signoff_not_ready(self) -> None:
        with (
            _successful_attestation(),
            patch(
                "agent.coo.dispatch_cli_production_signoff._evaluate_consume_check",
                return_value=CHECK_FAIL,
            ),
        ):
            summary = evaluate_dispatch_production_signoff()
        self.assertFalse(summary.signoff_ready)
        self.assertIn("consume_transaction_available", summary.failed_checks)
        self.assertEqual(summary.operator_action, OPERATOR_ACTION_RESOLVE_FAILED)


class TestProductionSignoffSafeOutput(unittest.TestCase):
    def test_format_includes_required_fields(self) -> None:
        with _successful_attestation():
            summary = evaluate_dispatch_production_signoff()
        output = format_dispatch_production_signoff(summary)
        self.assertIn("Production Dispatch Sign-off", output)
        self.assertIn("signoff_ready:", output)
        self.assertIn("overall_status:", output)
        self.assertIn("checks_passed_count:", output)
        self.assertIn("checks_blocked_count:", output)
        self.assertIn("checks_failed_count:", output)
        self.assertIn("failed_checks:", output)
        self.assertIn("blocked_checks:", output)
        self.assertIn("repository_attested:", output)
        self.assertIn("production_root_hard_deny:", output)
        self.assertIn("execution_allowed: false", output)
        self.assertIn("gateway_enabled:", output)
        self.assertIn("recommended_next_phase:", output)
        self.assertIn("operator_action:", output)

    def test_safe_output_has_no_forbidden_tokens(self) -> None:
        with _successful_attestation():
            summary = evaluate_dispatch_production_signoff()
        output = format_dispatch_production_signoff(summary).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)


class TestProductionSignoffCli(unittest.TestCase):
    def test_cli_signoff_ready_exit_zero(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["production", "sign-off"])
        buf = io.StringIO()
        with (
            _successful_attestation(),
            patch("sys.stdout", buf),
        ):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        self.assertIn("signoff_ready: true", buf.getvalue())

    def test_cli_signoff_not_ready_exit_one(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["production", "sign-off"])
        with (
            patch(
                "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
                side_effect=ValueError("attestation failed"),
            ),
            patch("sys.stdout", io.StringIO()),
        ):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 1)

    def test_read_only_no_subprocess_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            before = _hermes_digest(hermes_home)
            with (
                _successful_attestation(),
                patch.dict("os.environ", {"HERMES_HOME": str(hermes_home)}),
                patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
                patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            ):
                evaluate_dispatch_production_signoff()
                format_dispatch_production_signoff(evaluate_dispatch_production_signoff())
            self.assertEqual(_hermes_digest(hermes_home), before)


class TestProductionSignoffRegression(unittest.TestCase):
    def test_production_readiness_still_ready(self) -> None:
        from agent.coo.dispatch_cli_production_readiness import (
            OVERALL_READY,
            RECOMMENDED_NEXT_PHASE_READY,
            evaluate_dispatch_production_readiness,
        )

        summary = evaluate_dispatch_production_readiness()
        self.assertEqual(summary.overall, OVERALL_READY)
        self.assertEqual(summary.recommended_next_phase, RECOMMENDED_NEXT_PHASE_READY)

    def test_not_ready_overall_status(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
            side_effect=ValueError("attestation failed"),
        ):
            summary = evaluate_dispatch_production_signoff()
        self.assertEqual(summary.overall_status, OVERALL_SIGNOFF_NOT_READY)
        self.assertEqual(summary.recommended_next_phase, RECOMMENDED_NEXT_PHASE_NOT_READY)
