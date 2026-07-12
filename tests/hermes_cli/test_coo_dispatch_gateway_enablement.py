"""Phase 13F tests — dispatch gateway enablement state model."""

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
    evaluate_dispatch_production_readiness,
)
from agent.coo.dispatch_cli_production_signoff import (
    evaluate_dispatch_production_signoff,
)
from agent.coo.dispatch_cli_production_cutover import (
    evaluate_production_cutover_checklist,
)
from agent.coo.dispatch_cli_gateway_status import (
    format_dispatch_gateway_status_summary,
    summarize_dispatch_gateway_status,
)
from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_DISABLED,
    GATEWAY_STATE_ENABLED,
    GATEWAY_STATE_STAGED,
    RECOMMENDED_NEXT_PHASE_DISABLED,
    RECOMMENDED_NEXT_PHASE_ENABLED_NO_FACADE,
    RECOMMENDED_NEXT_PHASE_STAGED,
    CooDispatchGatewayEnablement,
    DispatchGatewayEnablementError,
    load_dispatch_gateway_enablement,
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
    "config.yaml",
    "HERMES_",
    "channel",
    "user_id",
)


def _gateway_config(state: str) -> dict:
    return {
        "coo": {
            "dispatch": {
                "gateway": {
                    "enablement": state,
                },
            },
        },
    }


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


class TestGatewayEnablementLoader(unittest.TestCase):
    def test_missing_config_defaults_disabled(self) -> None:
        enablement = load_dispatch_gateway_enablement(merged_config={})
        self.assertEqual(enablement.gateway_state, GATEWAY_STATE_DISABLED)
        self.assertFalse(enablement.gateway_enabled)
        self.assertFalse(enablement.gateway_staged)
        self.assertFalse(enablement.gateway_execution_configured)
        self.assertFalse(enablement.production_execution_allowed)
        self.assertTrue(enablement.production_root_hard_deny)
        self.assertTrue(enablement.valid)

    def test_disabled_valid(self) -> None:
        enablement = load_dispatch_gateway_enablement(
            merged_config=_gateway_config("disabled"),
        )
        self.assertEqual(enablement.gateway_state, GATEWAY_STATE_DISABLED)
        self.assertFalse(enablement.gateway_enabled)
        self.assertFalse(enablement.gateway_staged)
        self.assertTrue(enablement.valid)

    def test_staged_valid(self) -> None:
        enablement = load_dispatch_gateway_enablement(
            merged_config=_gateway_config("staged"),
        )
        self.assertEqual(enablement.gateway_state, GATEWAY_STATE_STAGED)
        self.assertFalse(enablement.gateway_enabled)
        self.assertTrue(enablement.gateway_staged)
        self.assertTrue(enablement.valid)

    def test_enabled_state_model_valid(self) -> None:
        enablement = load_dispatch_gateway_enablement(
            merged_config=_gateway_config("enabled"),
        )
        self.assertEqual(enablement.gateway_state, GATEWAY_STATE_ENABLED)
        self.assertTrue(enablement.gateway_enabled)
        self.assertFalse(enablement.gateway_staged)
        self.assertFalse(enablement.gateway_execution_configured)
        self.assertFalse(enablement.production_execution_allowed)
        self.assertTrue(enablement.production_root_hard_deny)
        self.assertTrue(enablement.valid)

    def test_invalid_state_rejected(self) -> None:
        enablement = load_dispatch_gateway_enablement(
            merged_config=_gateway_config("active"),
        )
        self.assertFalse(enablement.valid)
        self.assertEqual(enablement.gateway_state, GATEWAY_STATE_DISABLED)

    def test_empty_state_rejected(self) -> None:
        config = {
            "coo": {
                "dispatch": {
                    "gateway": {
                        "enablement": "   ",
                    },
                },
            },
        }
        enablement = load_dispatch_gateway_enablement(merged_config=config)
        self.assertFalse(enablement.valid)

    def test_wrong_gateway_section_type_rejected(self) -> None:
        config = {"coo": {"dispatch": {"gateway": "disabled"}}}
        enablement = load_dispatch_gateway_enablement(merged_config=config)
        self.assertFalse(enablement.valid)

    def test_unknown_key_rejected(self) -> None:
        config = {
            "coo": {
                "dispatch": {
                    "gateway": {
                        "enablement": "disabled",
                        "auto_enable": True,
                    },
                },
            },
        }
        enablement = load_dispatch_gateway_enablement(merged_config=config)
        self.assertFalse(enablement.valid)

    def test_strict_schema_raises_on_parse(self) -> None:
        from agent.coo.dispatch_gateway_enablement import _parse_gateway_enablement_state

        with self.assertRaises(DispatchGatewayEnablementError):
            _parse_gateway_enablement_state({"enablement": "bogus"})

    def test_environment_variable_does_not_auto_enable(self) -> None:
        with patch.dict("os.environ", {"HERMES_COO_GATEWAY_ENABLEMENT": "enabled"}, clear=False):
            enablement = load_dispatch_gateway_enablement(merged_config={})
        self.assertEqual(enablement.gateway_state, GATEWAY_STATE_DISABLED)
        self.assertFalse(enablement.gateway_enabled)

    def test_production_execution_allowed_false_all_states(self) -> None:
        for state in ("disabled", "staged", "enabled"):
            enablement = load_dispatch_gateway_enablement(
                merged_config=_gateway_config(state),
            )
            self.assertFalse(enablement.production_execution_allowed)

    def test_production_root_hard_deny_true_all_states(self) -> None:
        for state in ("disabled", "staged", "enabled"):
            enablement = load_dispatch_gateway_enablement(
                merged_config=_gateway_config(state),
            )
            self.assertTrue(enablement.production_root_hard_deny)


class TestGatewayProductionIntegration(unittest.TestCase):
    def test_disabled_readiness_gateway_blocked(self) -> None:
        summary = evaluate_dispatch_production_readiness(merged_config={})
        gateway = next(check for check in summary.checks if check.name == "gateway")
        self.assertEqual(gateway.status, CHECK_BLOCKED)
        self.assertEqual(summary.overall, OVERALL_READY)
        self.assertEqual(summary.repository2_policy.gateway_disabled, "enabled")

    def test_staged_readiness_gateway_blocked(self) -> None:
        summary = evaluate_dispatch_production_readiness(
            merged_config=_gateway_config("staged"),
        )
        gateway = next(check for check in summary.checks if check.name == "gateway")
        self.assertEqual(gateway.status, CHECK_BLOCKED)

    def test_enabled_readiness_gateway_fail(self) -> None:
        summary = evaluate_dispatch_production_readiness(
            merged_config=_gateway_config("enabled"),
        )
        gateway = next(check for check in summary.checks if check.name == "gateway")
        self.assertEqual(gateway.status, CHECK_FAIL)
        self.assertEqual(summary.overall, OVERALL_NOT_READY)

    def test_invalid_config_readiness_gateway_fail(self) -> None:
        config = _gateway_config("enabled")
        config["coo"]["dispatch"]["gateway"]["enablement"] = "bogus"
        summary = evaluate_dispatch_production_readiness(merged_config=config)
        gateway = next(check for check in summary.checks if check.name == "gateway")
        self.assertEqual(gateway.status, CHECK_FAIL)

    def test_disabled_signoff_gateway_blocked(self) -> None:
        from tests.hermes_cli.test_coo_dispatch_production_signoff import (
            _successful_attestation,
        )

        with _successful_attestation():
            summary = evaluate_dispatch_production_signoff(merged_config={})
        self.assertIn("gateway_disabled", summary.blocked_checks)
        self.assertFalse(summary.gateway_enabled)

    def test_staged_signoff_gateway_blocked(self) -> None:
        from tests.hermes_cli.test_coo_dispatch_production_signoff import (
            _successful_attestation,
        )

        with _successful_attestation():
            summary = evaluate_dispatch_production_signoff(
                merged_config=_gateway_config("staged"),
            )
        self.assertIn("gateway_disabled", summary.blocked_checks)
        self.assertFalse(summary.gateway_enabled)

    def test_enabled_signoff_not_ready(self) -> None:
        from tests.hermes_cli.test_coo_dispatch_production_signoff import (
            _successful_attestation,
        )

        with _successful_attestation():
            summary = evaluate_dispatch_production_signoff(
                merged_config=_gateway_config("enabled"),
            )
        self.assertFalse(summary.signoff_ready)
        self.assertTrue(summary.gateway_enabled)
        self.assertIn("gateway_disabled", summary.failed_checks)

    def test_enabled_cutover_gateway_fail(self) -> None:
        from tests.hermes_cli.test_coo_dispatch_production_signoff import (
            _successful_attestation,
        )

        with _successful_attestation():
            summary = evaluate_production_cutover_checklist(
                merged_config=_gateway_config("enabled"),
            )
        self.assertIn("gateway_disabled", summary.failed_checks)
        self.assertTrue(summary.gateway_enabled)


class TestGatewayStatusCli(unittest.TestCase):
    def test_gateway_status_cli_output(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["gateway", "status"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        output = buf.getvalue()
        self.assertIn("gateway_state: disabled", output)
        self.assertIn("gateway_enabled: false", output)
        self.assertIn("gateway_staged: false", output)
        self.assertIn("gateway_execution_configured: false", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("production_root_hard_deny: true", output)
        self.assertIn("recommended_next_phase:", output)

    def test_safe_output_has_no_forbidden_tokens(self) -> None:
        summary = summarize_dispatch_gateway_status(merged_config={})
        output = format_dispatch_gateway_status_summary(summary).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)

    def test_recommended_next_phase_by_state(self) -> None:
        from agent.coo.dispatch_gateway_enablement import (
            resolve_gateway_recommended_next_phase,
        )

        disabled = load_dispatch_gateway_enablement(merged_config={})
        staged = load_dispatch_gateway_enablement(
            merged_config=_gateway_config("staged"),
        )
        enabled = load_dispatch_gateway_enablement(
            merged_config=_gateway_config("enabled"),
        )
        self.assertEqual(
            resolve_gateway_recommended_next_phase(disabled),
            RECOMMENDED_NEXT_PHASE_DISABLED,
        )
        self.assertEqual(
            resolve_gateway_recommended_next_phase(staged),
            RECOMMENDED_NEXT_PHASE_STAGED,
        )
        self.assertEqual(
            resolve_gateway_recommended_next_phase(enabled),
            RECOMMENDED_NEXT_PHASE_ENABLED_NO_FACADE,
        )


class TestGatewayReadOnlyGuarantees(unittest.TestCase):
    def test_no_subprocess_invocation(self) -> None:
        with (
            patch("subprocess.run", side_effect=AssertionError("subprocess.run called")),
            patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen called")),
        ):
            load_dispatch_gateway_enablement(merged_config=_gateway_config("enabled"))
            summarize_dispatch_gateway_status(merged_config={})
            evaluate_dispatch_production_readiness(merged_config={})

    def test_repository2_path_not_accessed(self) -> None:
        repo_root = Path("/opt/data/multi-content-pipeline")
        before = _hermes_digest(repo_root) if repo_root.exists() else ""
        load_dispatch_gateway_enablement(merged_config={})
        summarize_dispatch_gateway_status(merged_config={})
        if repo_root.exists():
            after = _hermes_digest(repo_root)
            self.assertEqual(before, after)

    def test_gateway_status_cli_no_subprocess(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["gateway", "status"])
        with patch("subprocess.run", side_effect=AssertionError("subprocess.run called")):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
