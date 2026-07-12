"""Phase 13J tests — gateway pilot dispatch correlation and operator CLI."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent.coo.approval_session import (
    CEOApprovalSession,
    CEOApprovalSessionStatus,
    CEOApprovalSessionStore,
)
from agent.coo.dispatch_cli_gateway_pilot import (
    FAILURE_ATTESTATION_MISMATCH,
    FAILURE_BUNDLE_MISMATCH,
    FAILURE_CONFIRMATION_MISMATCH,
    FAILURE_REQUESTER_MISMATCH,
    FAILURE_SESSION_MISSING,
    FAILURE_SESSION_TICKET_MISMATCH,
    evaluate_gateway_pilot_readiness,
    format_gateway_pilot_readiness,
    validate_gateway_pilot_correlation,
)
from agent.coo.dispatch_gateway_pilot_service import (
    FAILURE_ENABLED_NOT_SUPPORTED,
    FAILURE_GATEWAY_DISABLED,
    FAILURE_HISTORY_PERSISTENCE_FAILED,
    FAILURE_MOCK_RUNNER_NOT_CONFIGURED,
    FAILURE_READINESS_FAILED,
    execute_gateway_pilot_dispatch,
    format_gateway_pilot_result,
)
from agent.coo.dispatch_gateway_request_store import (
    REQUEST_STATUS_COMPLETED,
    read_gateway_request,
)
from agent.coo.dispatch_pilot_history import (
    EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
    read_pilot_history_record,
)
from agent.coo.production_executor_confirmation import read_confirmation
from hermes_cli.coo_dispatch import (
    build_coo_dispatch_parser,
    run_gateway_pilot_dispatch_from_args,
)
from tests.hermes_cli.test_coo_dispatch_gateway_execution_facade import (
    _GatewayMockDispatchFixture,
    _staged_config,
)
from tests.hermes_cli.test_coo_dispatch_gateway_readiness import (
    _gateway_config,
    _ready_cutover_summary,
)
from tests.hermes_cli.test_coo_dispatch_production_signoff import (
    _successful_attestation,
)
from tests.hermes_cli.test_coo_dispatch_run import (
    _mock_runner_failure,
    _mock_runner_success,
    _mock_runner_timeout,
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
    "channel_id",
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


class _GatewayPilotFixture(_GatewayMockDispatchFixture):
    def __init__(self) -> None:
        super().__init__()
        self.session_store = CEOApprovalSessionStore()
        self.pilot_store_home_patch = patch(
            "agent.coo.dispatch_pilot_history.get_hermes_home",
            return_value=self.hermes_home,
        )

    def start(self) -> None:
        super().start()
        self.pilot_store_home_patch.start()

    def stop(self) -> None:
        self.pilot_store_home_patch.stop()
        super().stop()

    def pilot_history_dir(self) -> Path:
        return self.hermes_home / "coo" / "pilot-history"

    def seed_pilot_context(self) -> dict:
        seeded = self.seed_bundle_and_confirmation()
        ticket = seeded["ticket"]
        session_id = str(uuid.uuid4())
        session = CEOApprovalSession(
            session_id=session_id,
            status=CEOApprovalSessionStatus.APPROVED,
            task_kind=ticket.task_kind,
            run_date=ticket.run_date,
            report_status="approved",
            runtime_status="ready",
            requester_id=ticket.requester_id,
            execution_ticket_id=ticket.ticket_id,
        )
        self.session_store.save(session)
        prepare = seeded["prepare"]
        return {
            **seeded,
            "session_id": session_id,
            "session": session,
            "unlock_token_id": prepare["unlock_token"]["token_id"],
        }

    def pilot_kwargs(self, *, gateway_request_id: str, dry_run: bool = False) -> dict:
        ctx = self.ctx
        return {
            "session_id": ctx["session_id"],
            "ticket_id": ctx["ticket"].ticket_id,
            "confirmation_id": ctx["confirmation"].confirmation_id,
            "unlock_token_id": ctx["unlock_token_id"],
            "requester_id": ctx["ticket"].requester_id,
            "pipeline_root": str(self.pipeline_root),
            "gateway_request_id": gateway_request_id,
            "merged_config": _staged_config(self.pipeline_root),
            "dry_run": dry_run,
            "allow_mock_gateway_dispatch": True,
            "injected_runner": _mock_runner_success,
            "session_store": self.session_store,
            "bundle_dir": self.bundle_dir,
            "confirmation_dir": self.confirmation_dir,
            "request_dir": self.gateway_request_dir(),
            "history_dir": self.pilot_history_dir(),
        }


class TestGatewayPilotDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _GatewayPilotFixture()
        self.fixture.start()
        self.fixture.write_binding_state("bound")
        self.fixture.pipeline_root.mkdir(parents=True, exist_ok=True)
        self.fixture.ctx = self.fixture.seed_pilot_context()
        self._attestation = _successful_attestation()
        self._attestation.__enter__()

    def tearDown(self) -> None:
        self._attestation.__exit__(None, None, None)
        self.fixture.stop()

    def _run_pilot(self, **overrides):
        kwargs = self.fixture.pilot_kwargs(gateway_request_id="gw-pilot-001")
        kwargs.update(overrides)
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_gateway_pilot.evaluate_production_cutover_checklist",
                return_value=_ready_cutover_summary(),
            ),
            patch(
                "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
                side_effect=AssertionError("no bounded runner"),
            ),
        ):
            return execute_gateway_pilot_dispatch(**kwargs)

    def test_staged_dry_run_success_no_runner(self) -> None:
        runner_calls = {"count": 0}

        def counting_runner(*_a, **_k):
            runner_calls["count"] += 1
            return 0, "", ""

        result = self._run_pilot(dry_run=True, injected_runner=counting_runner)
        self.assertTrue(result.accepted)
        self.assertTrue(result.dry_run)
        self.assertFalse(result.consumed)
        self.assertEqual(runner_calls["count"], 0)
        record = read_pilot_history_record(
            result.pilot_attempt_id,
            history_dir=self.fixture.pilot_history_dir(),
        )
        self.assertEqual(record.execution_scope, EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK)
        self.assertEqual(record.gateway_request_id, "gw-pilot-001")
        self.assertEqual(record.session_id, self.fixture.ctx["session_id"])

    def test_staged_mock_runner_live_success(self) -> None:
        result = self._run_pilot(dry_run=False)
        self.assertTrue(result.accepted)
        self.assertTrue(result.consumed)
        self.assertNotEqual(result.pilot_attempt_id, result.gateway_request_id)
        req = read_gateway_request(
            "gw-pilot-001",
            request_dir=self.fixture.gateway_request_dir(),
        )
        self.assertIsNotNone(req)
        assert req is not None
        self.assertEqual(req.session_id, self.fixture.ctx["session_id"])
        self.assertEqual(req.pilot_attempt_id, result.pilot_attempt_id)

    def test_non_zero_failure_no_consume(self) -> None:
        result = self._run_pilot(injected_runner=_mock_runner_failure)
        self.assertFalse(result.accepted)
        self.assertFalse(result.consumed)

    def test_timeout_failure_no_consume(self) -> None:
        result = self._run_pilot(injected_runner=_mock_runner_timeout)
        self.assertFalse(result.accepted)
        self.assertFalse(result.consumed)

    def test_disabled_blocked(self) -> None:
        kwargs = self.fixture.pilot_kwargs(gateway_request_id="gw-disabled")
        kwargs["merged_config"] = _gateway_config("disabled")
        result = execute_gateway_pilot_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_GATEWAY_DISABLED)

    def test_enabled_blocked(self) -> None:
        kwargs = self.fixture.pilot_kwargs(gateway_request_id="gw-enabled")
        kwargs["merged_config"] = _gateway_config("enabled")
        result = execute_gateway_pilot_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_ENABLED_NOT_SUPPORTED)

    def test_missing_session_blocked(self) -> None:
        kwargs = self.fixture.pilot_kwargs(gateway_request_id="gw-nosess")
        kwargs["session_id"] = "missing-session"
        result = execute_gateway_pilot_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_SESSION_MISSING)

    def test_requester_mismatch_blocked(self) -> None:
        kwargs = self.fixture.pilot_kwargs(gateway_request_id="gw-badreq")
        kwargs["requester_id"] = "wrong-requester"
        result = execute_gateway_pilot_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_REQUESTER_MISMATCH)

    def test_session_ticket_mismatch_blocked(self) -> None:
        kwargs = self.fixture.pilot_kwargs(gateway_request_id="gw-badticket")
        kwargs["ticket_id"] = "wrong-ticket"
        result = execute_gateway_pilot_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_SESSION_TICKET_MISMATCH)

    def test_confirmation_mismatch_blocked(self) -> None:
        kwargs = self.fixture.pilot_kwargs(gateway_request_id="gw-badconfirm")
        kwargs["confirmation_id"] = "wrong-confirmation"
        result = execute_gateway_pilot_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_CONFIRMATION_MISMATCH)

    def test_attestation_mismatch_blocked(self) -> None:
        kwargs = self.fixture.pilot_kwargs(gateway_request_id="gw-badroot")
        kwargs["pipeline_root"] = str(self.fixture.pipeline_root / "other-root")
        (self.fixture.pipeline_root / "other-root").mkdir(parents=True, exist_ok=True)
        result = execute_gateway_pilot_dispatch(**kwargs)
        self.assertEqual(result.failure_reason_code, FAILURE_ATTESTATION_MISMATCH)

    def test_live_cli_without_runner_blocked(self) -> None:
        args = argparse.Namespace(
            session_id=self.fixture.ctx["session_id"],
            ticket_id=self.fixture.ctx["ticket"].ticket_id,
            confirmation_id=self.fixture.ctx["confirmation"].confirmation_id,
            unlock_token_id=self.fixture.ctx["unlock_token_id"],
            requester_id=self.fixture.ctx["ticket"].requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            gateway_request_id="gw-cli-live",
            dry_run=False,
        )
        with (
            patch(
                "agent.coo.dispatch_cli_gateway_pilot.evaluate_production_cutover_checklist",
                return_value=_ready_cutover_summary(),
            ),
            patch("hermes_cli.config.load_config", return_value=_staged_config(self.fixture.pipeline_root)),
        ):
            code = run_gateway_pilot_dispatch_from_args(
                args,
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                request_dir=self.fixture.gateway_request_dir(),
                history_dir=self.fixture.pilot_history_dir(),
            )
        self.assertEqual(code, 1)

    def test_dry_run_cli_allowed(self) -> None:
        args = argparse.Namespace(
            session_id=self.fixture.ctx["session_id"],
            ticket_id=self.fixture.ctx["ticket"].ticket_id,
            confirmation_id=self.fixture.ctx["confirmation"].confirmation_id,
            unlock_token_id=self.fixture.ctx["unlock_token_id"],
            requester_id=self.fixture.ctx["ticket"].requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            gateway_request_id="gw-cli-dry",
            dry_run=True,
        )
        stdout = io.StringIO()
        with (
            patch("sys.stdout", stdout),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_gateway_pilot.evaluate_production_cutover_checklist",
                return_value=_ready_cutover_summary(),
            ),
            patch("hermes_cli.config.load_config", return_value=_staged_config(self.fixture.pipeline_root)),
        ):
            code = run_gateway_pilot_dispatch_from_args(
                args,
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                request_dir=self.fixture.gateway_request_dir(),
                history_dir=self.fixture.pilot_history_dir(),
            )
        self.assertEqual(code, 0)
        output = stdout.getvalue().lower()
        self.assertIn("dry_run: true", output)

    def test_duplicate_gateway_request_blocked(self) -> None:
        path = self.fixture.gateway_request_dir() / "gw-dup.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "gateway_request_id": "gw-dup",
                    "ticket_id": "t1",
                    "confirmation_id": "c1",
                    "execution_attempt_id": "",
                    "dispatch_run_id": "",
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
        result = self._run_pilot(gateway_request_id="gw-dup")
        self.assertEqual(result.status, "already_completed")

    def test_pilot_history_persistence_failure(self) -> None:
        with patch(
            "agent.coo.dispatch_pilot_history.write_pilot_history_record",
            side_effect=ValueError("write failed"),
        ):
            result = self._run_pilot(gateway_request_id="gw-histfail")
        self.assertFalse(result.accepted)
        self.assertEqual(result.failure_reason_code, FAILURE_HISTORY_PERSISTENCE_FAILED)

    def test_safe_output(self) -> None:
        result = self._run_pilot(gateway_request_id="gw-safe")
        output = format_gateway_pilot_result(result).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)

    def test_readiness_staged_ready(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_gateway_pilot.evaluate_production_cutover_checklist",
            return_value=_ready_cutover_summary(),
        ):
            summary = evaluate_gateway_pilot_readiness(
                session_id=self.fixture.ctx["session_id"],
                ticket_id=self.fixture.ctx["ticket"].ticket_id,
                confirmation_id=self.fixture.ctx["confirmation"].confirmation_id,
                pipeline_root=str(self.fixture.pipeline_root),
                merged_config=_staged_config(self.fixture.pipeline_root),
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
            )
        self.assertTrue(summary.pilot_ready)
        output = format_gateway_pilot_readiness(summary)
        self.assertIn("execution_scope: isolated_gateway_mock", output)

    def test_cli_parser_registers_gateway_pilot(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "gateway",
                "pilot",
                "readiness",
                "--session-id",
                "sess-1",
                "--ticket-id",
                "ticket-1",
                "--confirmation-id",
                "confirm-1",
                "--pipeline-root",
                "/tmp/fake",
            ],
        )
        self.assertEqual(args.coo_dispatch_gateway_pilot_command, "readiness")


class TestGatewayPilotCorrelation(unittest.TestCase):
    def test_validate_correlation_happy_path(self) -> None:
        fixture = _GatewayPilotFixture()
        fixture.start()
        fixture.write_binding_state("bound")
        fixture.pipeline_root.mkdir(parents=True, exist_ok=True)
        ctx = fixture.seed_pilot_context()
        failure = validate_gateway_pilot_correlation(
            session_id=ctx["session_id"],
            ticket_id=ctx["ticket"].ticket_id,
            confirmation_id=ctx["confirmation"].confirmation_id,
            unlock_token_id=ctx["unlock_token_id"],
            requester_id=ctx["ticket"].requester_id,
            pipeline_root=str(fixture.pipeline_root),
            session_store=fixture.session_store,
            bundle_dir=fixture.bundle_dir,
            confirmation_dir=fixture.confirmation_dir,
        )
        self.assertIsNone(failure)
        fixture.stop()

    def test_bundle_mismatch_detected(self) -> None:
        fixture = _GatewayPilotFixture()
        fixture.start()
        fixture.pipeline_root.mkdir(parents=True, exist_ok=True)
        ctx = fixture.seed_pilot_context()
        failure = validate_gateway_pilot_correlation(
            session_id=ctx["session_id"],
            ticket_id=ctx["ticket"].ticket_id,
            confirmation_id=ctx["confirmation"].confirmation_id,
            unlock_token_id="wrong-token",
            requester_id=ctx["ticket"].requester_id,
            pipeline_root=str(fixture.pipeline_root),
            session_store=fixture.session_store,
            bundle_dir=fixture.bundle_dir,
            confirmation_dir=fixture.confirmation_dir,
        )
        self.assertEqual(failure, FAILURE_BUNDLE_MISMATCH)
        fixture.stop()

    def test_legacy_pilot_history_backward_compatible(self) -> None:
        fixture = _GatewayPilotFixture()
        fixture.start()
        path = fixture.pilot_history_dir()
        path.mkdir(parents=True, exist_ok=True)
        legacy = {
            "version": 1,
            "pilot_attempt_id": "legacy-pilot-1",
            "execution_attempt_id": "",
            "ticket_id": "ticket-1",
            "confirmation_id": "confirm-1",
            "dispatch_run_id": "",
            "execution_scope": "isolated_clone",
            "status": "dry_run",
            "exit_code": 0,
            "dry_run": True,
            "started_at": "2026-07-11T00:00:00+00:00",
            "completed_at": "2026-07-11T00:00:00+00:00",
            "evidence_present": False,
            "audit_present": False,
            "consumed": False,
            "failure_reason_code": "none",
            "production_execution_allowed": False,
            "production_root_hard_deny": True,
            "gateway_enabled": False,
        }
        (path / "legacy-pilot-1.json").write_text(json.dumps(legacy), encoding="utf-8")
        record = read_pilot_history_record(
            "legacy-pilot-1",
            history_dir=path,
        )
        self.assertEqual(record.gateway_request_id, "")
        self.assertEqual(record.session_id, "")
        fixture.stop()

    def test_repository2_unchanged(self) -> None:
        fixture = _GatewayPilotFixture()
        fixture.start()
        before = _hermes_digest(Path("/opt/data/multi-content-pipeline"))
        fixture.stop()
        fixture2 = _GatewayPilotFixture()
        fixture2.start()
        fixture2.write_binding_state("bound")
        fixture2.pipeline_root.mkdir(parents=True, exist_ok=True)
        fixture2.ctx = fixture2.seed_pilot_context()
        with _successful_attestation():
            self._run_pilot_on(fixture2, gateway_request_id="gw-r2check")
        after = _hermes_digest(Path("/opt/data/multi-content-pipeline"))
        self.assertEqual(before, after)
        fixture2.stop()

    def _run_pilot_on(self, fixture, **kwargs):
        base = fixture.pilot_kwargs(gateway_request_id=kwargs.pop("gateway_request_id"))
        base.update(kwargs)
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_gateway_pilot.evaluate_production_cutover_checklist",
                return_value=_ready_cutover_summary(),
            ),
        ):
            return execute_gateway_pilot_dispatch(**base)


if __name__ == "__main__":
    unittest.main()
