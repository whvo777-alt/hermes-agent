"""Phase 13H tests — gateway execution facade scaffold."""

from __future__ import annotations

import hashlib
import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_gateway_readiness import (
    READINESS_LEVEL_READY_FOR_MOCK_WIRING,
    evaluate_dispatch_gateway_readiness,
    format_dispatch_gateway_readiness_summary,
)
from agent.coo.dispatch_cli_gateway_status import (
    format_dispatch_gateway_status_summary,
    summarize_dispatch_gateway_status,
)
from agent.coo.dispatch_cli_production_readiness import (
    CHECK_BLOCKED,
    CHECK_FAIL,
    evaluate_dispatch_production_readiness,
)
from agent.coo.dispatch_gateway_enablement import load_dispatch_gateway_enablement
from agent.coo.dispatch_gateway_execution_facade import (
    GATEWAY_EXECUTION_FACADE_CONNECTED,
    GATEWAY_EXECUTION_FACADE_VERSION,
    GatewayExecutionNotEnabled,
    CooDispatchGatewayExecutionFacade,
    evaluate_gateway_execution_facade,
    execute_gateway_dispatch,
    format_gateway_execution_facade,
    load_gateway_execution_facade,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.test_coo_dispatch_gateway_readiness import (
    _gateway_config,
    _ready_cutover_summary,
)
from tests.hermes_cli.test_coo_dispatch_production_signoff import (
    _successful_attestation,
)

_FORBIDDEN_OUTPUT_TOKENS = (
    "argv",
    "cwd",
    "env",
    "stdout",
    "stderr",
    "snapshot",
    "unlock",
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
    "ticket",
    "confirmation",
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


class TestGatewayExecutionFacadeLoad(unittest.TestCase):
    def test_facade_module_loads(self) -> None:
        facade = load_gateway_execution_facade(merged_config={})
        self.assertTrue(facade.valid)
        self.assertTrue(facade.facade_connected)
        self.assertEqual(facade.version, GATEWAY_EXECUTION_FACADE_VERSION)

    def test_marker_exists(self) -> None:
        self.assertTrue(GATEWAY_EXECUTION_FACADE_CONNECTED)

    def test_execution_disabled_scaffold(self) -> None:
        facade = evaluate_gateway_execution_facade(merged_config={})
        self.assertFalse(facade.execution_enabled)
        self.assertFalse(facade.production_execution_allowed)
        self.assertFalse(facade.isolated_execution_supported)

    def test_marker_missing_fail_closed(self) -> None:
        with patch(
            "agent.coo.dispatch_gateway_execution_facade._read_facade_connected_marker",
            return_value=False,
        ):
            facade = load_gateway_execution_facade(merged_config={})
        self.assertFalse(facade.valid)
        self.assertFalse(facade.facade_connected)

    def test_marker_invalid_type_fail_closed(self) -> None:
        with patch(
            "agent.coo.dispatch_gateway_execution_facade.GATEWAY_EXECUTION_FACADE_CONNECTED",
            "yes",
        ):
            facade = load_gateway_execution_facade(merged_config={})
        self.assertFalse(facade.valid)
        self.assertFalse(facade.facade_connected)

    def test_execution_enabled_with_production_denied_stays_valid(self) -> None:
        with patch(
            "agent.coo.dispatch_gateway_execution_facade.GATEWAY_EXECUTION_ENABLED",
            True,
        ):
            facade = evaluate_gateway_execution_facade(
                merged_config=_gateway_config("staged"),
            )
        self.assertTrue(facade.valid)
        self.assertTrue(facade.execution_enabled)
        self.assertFalse(facade.production_execution_allowed)

    def test_execution_enabled_with_production_allowed_fail_closed(self) -> None:
        facade = CooDispatchGatewayExecutionFacade(
            facade_connected=True,
            execution_enabled=True,
            production_execution_allowed=True,
            isolated_execution_supported=False,
            gateway_state="staged",
            version=GATEWAY_EXECUTION_FACADE_VERSION,
            valid=True,
        )
        from agent.coo.dispatch_gateway_execution_facade import (
            _validate_facade_policy,
        )

        validated = _validate_facade_policy(facade)
        self.assertFalse(validated.valid)
        self.assertFalse(validated.production_execution_allowed)

    def test_invalid_gateway_state_fail_closed(self) -> None:
        config = _gateway_config("enabled")
        config["coo"]["dispatch"]["gateway"]["enablement"] = "bogus"
        facade = load_gateway_execution_facade(merged_config=config)
        self.assertFalse(facade.valid)


class TestGatewayExecutionFacadeDispatch(unittest.TestCase):
    def test_execute_gateway_dispatch_raises_not_enabled(self) -> None:
        with self.assertRaises(GatewayExecutionNotEnabled):
            execute_gateway_dispatch(unlock_token_id="token-1", requester_id="op")


class TestGatewayFacadeIntegrations(unittest.TestCase):
    def test_gateway_status_includes_facade_fields(self) -> None:
        summary = summarize_dispatch_gateway_status(merged_config={})
        output = format_dispatch_gateway_status_summary(summary)
        self.assertIn("facade_connected: true", output)
        self.assertIn("execution_enabled: false", output)
        self.assertIn("isolated_execution_supported: false", output)

    def test_gateway_readiness_staged_ready_for_mock_wiring(self) -> None:
        with (
            _successful_attestation(),
            patch(
                "agent.coo.dispatch_cli_gateway_readiness.evaluate_production_cutover_checklist",
                return_value=_ready_cutover_summary(),
            ),
        ):
            summary = evaluate_dispatch_gateway_readiness(
                merged_config=_gateway_config("staged"),
            )
        self.assertEqual(summary.readiness_level, READINESS_LEVEL_READY_FOR_MOCK_WIRING)
        self.assertTrue(summary.gateway_execution_facade_connected)
        self.assertIn("gateway_execution_facade_connected", summary.blocked_checks)

    def test_enabled_facade_connected_blocked_not_fail(self) -> None:
        summary = evaluate_dispatch_production_readiness(
            merged_config=_gateway_config("enabled"),
        )
        gateway = next(check for check in summary.checks if check.name == "gateway")
        self.assertEqual(gateway.status, CHECK_BLOCKED)

    def test_enabled_facade_disconnected_fails(self) -> None:
        with patch(
            "agent.coo.dispatch_gateway_enablement._gateway_execution_facade_connected",
            return_value=False,
        ):
            enablement = load_dispatch_gateway_enablement(
                merged_config=_gateway_config("enabled"),
            )
            summary = evaluate_dispatch_production_readiness(
                merged_config=_gateway_config("enabled"),
            )
        self.assertFalse(enablement.gateway_execution_configured)
        gateway = next(check for check in summary.checks if check.name == "gateway")
        self.assertEqual(gateway.status, CHECK_FAIL)

    def test_production_signoff_enabled_facade_blocked(self) -> None:
        from agent.coo.dispatch_cli_production_signoff import (
            evaluate_dispatch_production_signoff,
        )

        with _successful_attestation():
            summary = evaluate_dispatch_production_signoff(
                merged_config=_gateway_config("enabled"),
            )
        self.assertFalse(summary.signoff_ready)
        self.assertTrue(summary.gateway_enabled)
        self.assertIn("gateway_disabled", summary.blocked_checks)
        self.assertNotIn("gateway_disabled", summary.failed_checks)


class TestGatewayFacadeCli(unittest.TestCase):
    def test_gateway_facade_cli_output(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["gateway", "facade"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        output = buf.getvalue()
        self.assertIn("Gateway Execution Facade", output)
        self.assertIn("facade_version:", output)
        self.assertIn("facade_connected: true", output)
        self.assertIn("execution_enabled: false", output)
        self.assertIn("isolated_execution_supported: false", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("gateway_state:", output)
        self.assertIn("recommended_next_phase:", output)

    def test_safe_output_has_no_forbidden_tokens(self) -> None:
        facade = evaluate_gateway_execution_facade(merged_config={})
        output = format_gateway_execution_facade(facade).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)

    def test_readiness_safe_output_still_clean(self) -> None:
        summary = evaluate_dispatch_gateway_readiness(merged_config={})
        output = format_dispatch_gateway_readiness_summary(summary).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)


class TestGatewayFacadeReadOnly(unittest.TestCase):
    def test_no_subprocess_invocation(self) -> None:
        with (
            patch("subprocess.run", side_effect=AssertionError("subprocess.run called")),
            patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen called")),
        ):
            load_gateway_execution_facade(merged_config={})
            evaluate_gateway_execution_facade(merged_config=_gateway_config("staged"))
            summarize_dispatch_gateway_status(merged_config={})

    def test_repository2_digest_unchanged(self) -> None:
        repo_root = Path("/opt/data/multi-content-pipeline")
        before = _hermes_digest(repo_root) if repo_root.exists() else ""
        evaluate_gateway_execution_facade(merged_config={})
        if repo_root.exists():
            self.assertEqual(before, _hermes_digest(repo_root))

    def test_gateway_states_regression(self) -> None:
        for state in ("disabled", "staged", "enabled"):
            facade = load_gateway_execution_facade(merged_config=_gateway_config(state))
            self.assertFalse(facade.production_execution_allowed)
            self.assertFalse(facade.execution_enabled)


if __name__ == "__main__":
    unittest.main()
