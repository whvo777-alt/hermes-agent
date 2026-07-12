"""Phase 13H/13I tests — gateway execution facade and mock dispatch."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_gateway_readiness import (
    READINESS_LEVEL_READY_FOR_MOCK_DISPATCH,
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
    FAILURE_BINDING_NOT_BOUND,
    FAILURE_EXECUTOR_DISABLED,
    FAILURE_GATEWAY_DISABLED,
    FAILURE_GATEWAY_ENABLED_NOT_SUPPORTED,
    FAILURE_INJECTED_RUNNER_MISSING,
    FAILURE_INJECTED_RUNNER_NOT_CALLABLE,
    FAILURE_MOCK_DISPATCH_NOT_ALLOWED,
    FAILURE_OPERATOR_READINESS_FAILED,
    FAILURE_PRODUCTION_ROOT_DENIED,
    FAILURE_REGRESSION_BLOCKED,
    FAILURE_REQUEST_PERSISTENCE_FAILED,
    FAILURE_SIGNOFF_NOT_READY,
    GATEWAY_EXECUTION_FACADE_CONNECTED,
    GATEWAY_EXECUTION_FACADE_VERSION,
    RESULT_STATUS_ALREADY_COMPLETED,
    RESULT_STATUS_BLOCKED,
    RESULT_STATUS_COMPLETED,
    RESULT_STATUS_FAILED,
    RESULT_STATUS_IN_PROGRESS,
    CooDispatchGatewayExecutionFacade,
    evaluate_gateway_execution_facade,
    execute_gateway_dispatch,
    format_gateway_dispatch_result,
    format_gateway_execution_facade,
    load_gateway_execution_facade,
)
from agent.coo.dispatch_gateway_request_store import (
    CooDispatchGatewayRequestRecord,
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_PREPARED,
    read_gateway_request,
    reserve_gateway_request,
)
from agent.coo.execution_dispatch_runtime import DispatchExecutionRunStatus
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.test_coo_dispatch_gateway_readiness import (
    _gateway_config,
    _ready_cutover_summary,
)
from tests.hermes_cli.test_coo_dispatch_production_signoff import (
    _successful_attestation,
)
from tests.hermes_cli.test_coo_dispatch_run import (
    _CooDispatchRunFixture,
    _mock_runner_failure,
    _mock_runner_success,
    _mock_runner_timeout,
)


def _staged_config(pipeline_root: Path) -> dict:
    return {
        "coo": {
            "dispatch": {
                "executor": {
                    "enabled": True,
                    "allowed_pipeline_roots": [str(pipeline_root)],
                },
                "gateway": {"enablement": "staged"},
            },
        },
    }


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


class _GatewayMockDispatchFixture(_CooDispatchRunFixture):
    def __init__(self) -> None:
        super().__init__()
        self.gateway_request_home_patch = patch(
            "agent.coo.dispatch_gateway_request_store.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.evidence_home_patch = patch(
            "agent.coo.dispatch_cli_evidence.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.pilot_history_home_patch = patch(
            "agent.coo.dispatch_cli_pilot_history.get_hermes_home",
            return_value=self.hermes_home,
        )

    def start(self) -> None:
        super().start()
        self.gateway_request_home_patch.start()
        self.evidence_home_patch.start()
        self.pilot_history_home_patch.start()

    def stop(self) -> None:
        self.pilot_history_home_patch.stop()
        self.evidence_home_patch.stop()
        self.gateway_request_home_patch.stop()
        super().stop()

    def gateway_request_dir(self) -> Path:
        return self.hermes_home / "coo" / "gateway-requests"

    def _dispatch_kwargs(self, *, gateway_request_id: str, dry_run: bool = False):
        seeded = self.seed_bundle_and_confirmation()
        prepare = seeded["prepare"]
        return {
            "ticket_id": seeded["ticket"].ticket_id,
            "confirmation_id": seeded["confirmation"].confirmation_id,
            "unlock_token_id": prepare["unlock_token"]["token_id"],
            "requester_id": seeded["ticket"].requester_id,
            "pipeline_root": str(self.pipeline_root),
            "gateway_request_id": gateway_request_id,
            "merged_config": _staged_config(self.pipeline_root),
            "request_dir": self.gateway_request_dir(),
            "dry_run": dry_run,
            "allow_mock_gateway_dispatch": True,
            "injected_runner": _mock_runner_success,
        }


class TestGatewayExecutionFacadeLoad(unittest.TestCase):
    def test_facade_module_loads(self) -> None:
        facade = load_gateway_execution_facade(merged_config={})
        self.assertTrue(facade.valid)
        self.assertTrue(facade.facade_connected)
        self.assertEqual(facade.version, GATEWAY_EXECUTION_FACADE_VERSION)

    def test_marker_exists(self) -> None:
        self.assertTrue(GATEWAY_EXECUTION_FACADE_CONNECTED)

    def test_disabled_execution_not_enabled(self) -> None:
        facade = evaluate_gateway_execution_facade(merged_config={})
        self.assertFalse(facade.execution_enabled)
        self.assertTrue(facade.isolated_execution_supported)

    def test_staged_mock_capable(self) -> None:
        facade = evaluate_gateway_execution_facade(
            merged_config=_gateway_config("staged"),
        )
        self.assertTrue(facade.execution_enabled)
        self.assertTrue(facade.isolated_execution_supported)
        self.assertFalse(facade.production_execution_allowed)

    def test_marker_missing_fail_closed(self) -> None:
        with patch(
            "agent.coo.dispatch_gateway_execution_facade._read_facade_connected_marker",
            return_value=False,
        ):
            facade = load_gateway_execution_facade(merged_config={})
        self.assertFalse(facade.valid)
        self.assertFalse(facade.facade_connected)

    def test_execution_enabled_with_production_allowed_fail_closed(self) -> None:
        facade = CooDispatchGatewayExecutionFacade(
            facade_connected=True,
            execution_enabled=True,
            production_execution_allowed=True,
            isolated_execution_supported=True,
            gateway_state="staged",
            version=GATEWAY_EXECUTION_FACADE_VERSION,
            valid=True,
        )
        from agent.coo.dispatch_gateway_execution_facade import _validate_facade_policy

        validated = _validate_facade_policy(facade)
        self.assertFalse(validated.valid)


class TestGatewayFacadeIntegrations(unittest.TestCase):
    def test_gateway_status_includes_facade_fields(self) -> None:
        summary = summarize_dispatch_gateway_status(
            merged_config=_gateway_config("staged"),
        )
        output = format_dispatch_gateway_status_summary(summary)
        self.assertIn("facade_connected: true", output)
        self.assertIn("execution_enabled: true", output)
        self.assertIn("isolated_execution_supported: true", output)

    def test_gateway_readiness_staged_ready_for_mock_dispatch(self) -> None:
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
        self.assertEqual(summary.readiness_level, READINESS_LEVEL_READY_FOR_MOCK_DISPATCH)
        self.assertTrue(summary.gateway_execution_facade_connected)
        statuses = {check.name: check.status for check in summary.checks}
        self.assertEqual(statuses["gateway_execution_facade_connected"], "PASS")

    def test_enabled_facade_connected_blocked_not_fail(self) -> None:
        summary = evaluate_dispatch_production_readiness(
            merged_config=_gateway_config("enabled"),
        )
        gateway = next(check for check in summary.checks if check.name == "gateway")
        self.assertEqual(gateway.status, CHECK_BLOCKED)

    def test_production_signoff_enabled_facade_blocked(self) -> None:
        from agent.coo.dispatch_cli_production_signoff import (
            evaluate_dispatch_production_signoff,
        )

        with _successful_attestation():
            summary = evaluate_dispatch_production_signoff(
                merged_config=_gateway_config("enabled"),
            )
        self.assertFalse(summary.signoff_ready)
        self.assertIn("gateway_disabled", summary.blocked_checks)


class TestGatewayMockDispatchExecution(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _GatewayMockDispatchFixture()
        self.fixture.start()
        self.fixture.write_binding_state("bound")
        self.fixture.pipeline_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_staged_opt_in_mock_runner_success(self) -> None:
        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
                side_effect=AssertionError("no bounded runner"),
            ),
        ):
            result = execute_gateway_dispatch(
                **self.fixture._dispatch_kwargs(gateway_request_id="gw-req-001"),
            )
        self.assertTrue(result.accepted)
        self.assertEqual(result.status, RESULT_STATUS_COMPLETED)
        self.assertTrue(result.consumed)
        record = read_gateway_request(
            "gw-req-001",
            request_dir=self.fixture.gateway_request_dir(),
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.status, REQUEST_STATUS_COMPLETED)
        self.assertNotIn("pipeline_root", record.to_dict())

    def test_non_zero_failure_no_consume(self) -> None:
        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-fail")
            kwargs["injected_runner"] = _mock_runner_failure
            result = execute_gateway_dispatch(**kwargs)
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, RESULT_STATUS_FAILED)
        self.assertFalse(result.consumed)

    def test_timeout_failure_no_consume(self) -> None:
        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-timeout")
            kwargs["injected_runner"] = _mock_runner_timeout
            result = execute_gateway_dispatch(**kwargs)
        self.assertFalse(result.accepted)
        self.assertFalse(result.consumed)

    def test_dry_run_no_runner_no_consume(self) -> None:
        runner_calls = {"count": 0}

        def counting_runner(*_args, **_kwargs):
            runner_calls["count"] += 1
            return 0, "", ""

        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            kwargs = self.fixture._dispatch_kwargs(
                gateway_request_id="gw-req-dry",
                dry_run=True,
            )
            kwargs["injected_runner"] = counting_runner
            result = execute_gateway_dispatch(**kwargs)
        self.assertTrue(result.accepted)
        self.assertTrue(result.dry_run)
        self.assertFalse(result.consumed)
        self.assertEqual(runner_calls["count"], 0)

    def test_disabled_blocked(self) -> None:
        kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-disabled")
        kwargs["merged_config"] = _gateway_config("disabled")
        result = execute_gateway_dispatch(**kwargs)
        self.assertEqual(result.status, RESULT_STATUS_BLOCKED)
        self.assertEqual(result.failure_reason_code, FAILURE_GATEWAY_DISABLED)

    def test_enabled_blocked(self) -> None:
        kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-enabled")
        kwargs["merged_config"] = _gateway_config("enabled")
        result = execute_gateway_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_GATEWAY_ENABLED_NOT_SUPPORTED)

    def test_opt_in_false_blocked(self) -> None:
        kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-noopt")
        kwargs["allow_mock_gateway_dispatch"] = False
        result = execute_gateway_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_MOCK_DISPATCH_NOT_ALLOWED)

    def test_live_without_runner_blocked(self) -> None:
        kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-norunner")
        kwargs["injected_runner"] = None
        result = execute_gateway_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_INJECTED_RUNNER_MISSING)

    def test_non_callable_runner_blocked(self) -> None:
        kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-badrunner")
        kwargs["injected_runner"] = "not-callable"
        result = execute_gateway_dispatch(**kwargs)
        self.assertEqual(
            result.failure_reason_code,
            FAILURE_INJECTED_RUNNER_NOT_CALLABLE,
        )

    def test_production_root_blocked(self) -> None:
        kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-prodroot")
        kwargs["pipeline_root"] = "/opt/data/multi-content-pipeline"
        result = execute_gateway_dispatch(**kwargs)
        self.assertEqual(result.status, RESULT_STATUS_BLOCKED)

    def test_binding_unbound_blocked(self) -> None:
        kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-unbound")
        kwargs["binding_state"] = {"state": "unbound"}
        result = execute_gateway_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_BINDING_NOT_BOUND)

    def test_executor_disabled_blocked(self) -> None:
        kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-execoff")
        kwargs["merged_config"] = _gateway_config("staged")
        result = execute_gateway_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_EXECUTOR_DISABLED)

    def test_operator_readiness_failure_blocked(self) -> None:
        kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-badticket")
        kwargs["ticket_id"] = "missing-ticket"
        result = execute_gateway_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_OPERATOR_READINESS_FAILED)

    def test_signoff_not_ready_blocked(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
            side_effect=ValueError("attestation failed"),
        ):
            result = execute_gateway_dispatch(
                **self.fixture._dispatch_kwargs(gateway_request_id="gw-req-signoff"),
            )
        self.assertEqual(result.failure_reason_code, FAILURE_SIGNOFF_NOT_READY)

    def test_regression_blocked(self) -> None:
        with (
            _successful_attestation(),
            patch(
                "agent.coo.dispatch_cli_pilot_regression_gate.evaluate_pilot_regression_gate",
            ) as mock_gate,
        ):
            from agent.coo.dispatch_cli_pilot_regression_gate import (
                CooDispatchPilotRegressionGateSummary,
            )

            mock_gate.return_value = CooDispatchPilotRegressionGateSummary(
                regression_status="FAIL",
                regression_gate="blocked_for_live",
                live_pilot_allowed=False,
                dry_run_allowed=True,
                consecutive_failures=2,
                total_attempts=2,
                latest_status="failure",
                latest_pilot_attempt_id="pilot-1",
                production_policy_violations=0,
            )
            result = execute_gateway_dispatch(
                **self.fixture._dispatch_kwargs(gateway_request_id="gw-req-regress"),
            )
        self.assertEqual(result.failure_reason_code, FAILURE_REGRESSION_BLOCKED)

    def test_duplicate_completed_replay(self) -> None:
        path = self.fixture.gateway_request_dir() / "gw-req-dup.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "gateway_request_id": "gw-req-dup",
                    "ticket_id": "ticket-1",
                    "confirmation_id": "confirm-1",
                    "execution_attempt_id": "attempt-1",
                    "dispatch_run_id": "run-1",
                    "status": REQUEST_STATUS_COMPLETED,
                    "dry_run": False,
                    "failure_reason_code": "none",
                    "production_execution_allowed": False,
                    "gateway_state": "staged",
                    "updated_at": "2026-07-11T00:00:00+00:00",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        result = execute_gateway_dispatch(
            **self.fixture._dispatch_kwargs(gateway_request_id="gw-req-dup"),
        )
        self.assertEqual(result.status, RESULT_STATUS_ALREADY_COMPLETED)

    def test_concurrent_prepared_in_progress(self) -> None:
        reserve_gateway_request(
            CooDispatchGatewayRequestRecord(
                gateway_request_id="gw-req-busy",
                ticket_id="ticket-1",
                confirmation_id="confirm-1",
                execution_attempt_id="",
                dispatch_run_id="",
                status=REQUEST_STATUS_PREPARED,
                dry_run=False,
                failure_reason_code="none",
                production_execution_allowed=False,
                gateway_state="staged",
            ),
            request_dir=self.fixture.gateway_request_dir(),
        )
        result = execute_gateway_dispatch(
            **self.fixture._dispatch_kwargs(gateway_request_id="gw-req-busy"),
        )
        self.assertEqual(result.status, RESULT_STATUS_IN_PROGRESS)

    def test_persistence_failure_after_dispatch_fail_closed(self) -> None:
        from agent.coo.dispatch_gateway_request_store import (
            DispatchGatewayRequestStoreError,
        )

        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_gateway_execution_facade.transition_gateway_request",
                side_effect=DispatchGatewayRequestStoreError("write failed"),
            ),
        ):
            result = execute_gateway_dispatch(
                **self.fixture._dispatch_kwargs(gateway_request_id="gw-req-persist"),
            )
        self.assertFalse(result.accepted)
        self.assertEqual(result.failure_reason_code, FAILURE_REQUEST_PERSISTENCE_FAILED)

    def test_forbidden_runner_kwargs_rejected(self) -> None:
        from agent.coo.dispatch_gateway_execution_facade import GatewayExecutionFacadeError

        kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-req-forbidden")
        with self.assertRaises(GatewayExecutionFacadeError):
            execute_gateway_dispatch(**kwargs, node_path="/usr/bin/node")

    def test_safe_output(self) -> None:
        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_gateway_dispatch(
                **self.fixture._dispatch_kwargs(gateway_request_id="gw-req-safe"),
            )
        output = format_gateway_dispatch_result(result).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)


class TestGatewayFacadeCli(unittest.TestCase):
    def test_gateway_facade_cli_output(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["gateway", "facade"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        output = buf.getvalue()
        self.assertIn("facade_connected: true", output)
        self.assertIn("isolated_execution_supported: true", output)

    def test_safe_output_has_no_forbidden_tokens(self) -> None:
        facade = evaluate_gateway_execution_facade(
            merged_config=_gateway_config("staged"),
        )
        output = format_gateway_execution_facade(facade).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)


class TestGatewayFacadeReadOnly(unittest.TestCase):
    def test_repository2_digest_unchanged(self) -> None:
        repo_root = Path("/opt/data/multi-content-pipeline")
        before = _hermes_digest(repo_root) if repo_root.exists() else ""
        evaluate_gateway_execution_facade(merged_config=_gateway_config("staged"))
        if repo_root.exists():
            self.assertEqual(before, _hermes_digest(repo_root))

    def test_gateway_states_production_execution_denied(self) -> None:
        for state in ("disabled", "staged", "enabled"):
            facade = load_gateway_execution_facade(merged_config=_gateway_config(state))
            self.assertFalse(facade.production_execution_allowed)


if __name__ == "__main__":
    unittest.main()
