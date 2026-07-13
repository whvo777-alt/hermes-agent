"""Phase 13M Step 2 tests — gateway request audit CLI."""

from __future__ import annotations

import hashlib
import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_gateway_request_audit import (
    normalize_gateway_request_audit_id,
    run_gateway_request_audit_show,
    show_gateway_request_audit,
)
from agent.coo.dispatch_gateway_execution_facade import (
    RESULT_STATUS_COMPLETED,
    RESULT_STATUS_FAILED,
    execute_gateway_dispatch,
)
from agent.coo.dispatch_gateway_request_store import (
    CooDispatchGatewayRequestRecord,
    REQUEST_STATUS_COMPLETED,
    reserve_gateway_request,
)
from agent.coo.dispatch_pilot_history import (
    EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
    PILOT_STATUS_SUCCESS,
    write_pilot_history_record,
)
from agent.coo.dispatch_pilot_history import CooDispatchPilotHistoryRecord
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.test_coo_dispatch_gateway_execution_facade import (
    _GatewayMockDispatchFixture,
    _staged_config,
)
from tests.hermes_cli.test_coo_dispatch_production_signoff import (
    _successful_attestation,
)
from tests.hermes_cli.test_coo_dispatch_run import (
    _mock_runner_failure,
    _mock_runner_success,
)


_FORBIDDEN_OUTPUT_TOKENS = (
    "pipeline_root",
    "unlock",
    "token",
    "phrase",
    "argv",
    "cwd",
    "env",
    "stdout",
    "stderr",
    "snapshot",
    "secret",
    "/opt/data/multi-content-pipeline",
    "pipeline.js",
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


def _history_record(
    *,
    ticket_id: str,
    confirmation_id: str,
    gateway_request_id: str,
    pilot_attempt_id: str,
    execution_attempt_id: str = "exec-1",
    dispatch_run_id: str = "run-1",
    status: str = PILOT_STATUS_SUCCESS,
    dry_run: bool = False,
) -> CooDispatchPilotHistoryRecord:
    return CooDispatchPilotHistoryRecord(
        version=1,
        pilot_attempt_id=pilot_attempt_id,
        execution_attempt_id=execution_attempt_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        dispatch_run_id=dispatch_run_id,
        execution_scope=EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
        status=status,
        exit_code=0 if status == PILOT_STATUS_SUCCESS else 1,
        dry_run=dry_run,
        started_at="2026-07-13T00:59:00+00:00",
        completed_at="2026-07-13T01:00:00+00:00",
        evidence_present=True,
        audit_present=True,
        consumed=True,
        failure_reason_code="none",
        production_execution_allowed=False,
        production_root_hard_deny=True,
        gateway_enabled=False,
        gateway_request_id=gateway_request_id,
    )


class _GatewayAuditFixture(_GatewayMockDispatchFixture):
    def pilot_history_dir(self) -> Path:
        return self.hermes_home / "coo" / "pilot-history"

    def audit_kwargs(self) -> dict:
        return {
            "request_dir": self.gateway_request_dir(),
            "history_dir": self.pilot_history_dir(),
            "bundle_dir": self.bundle_dir,
            "confirmation_dir": self.confirmation_dir,
        }


class TestGatewayRequestAuditCli(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _GatewayAuditFixture()
        self.fixture.start()
        self.fixture.write_binding_state("bound")
        self.fixture.pipeline_root.mkdir(parents=True, exist_ok=True)
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.kwargs = self.fixture.audit_kwargs()
        self.pilot_history_home_patch = patch(
            "agent.coo.dispatch_pilot_history.get_hermes_home",
            return_value=self.fixture.hermes_home,
        )
        self.pilot_history_home_patch.start()

    def tearDown(self) -> None:
        self.pilot_history_home_patch.stop()
        self.fixture.stop()

    def _run_cli(self, gateway_request_id: str) -> tuple[int, str, str]:
        parser = build_coo_dispatch_parser()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_gateway_request_store.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_pilot_history.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_cli_evidence.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_consume_transaction.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
        ):
            args = parser.parse_args(
                [
                    "gateway",
                    "audit",
                    "show",
                    "--gateway-request-id",
                    gateway_request_id,
                ]
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                exit_code = args.handler(args)
            return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_completed_gateway_request_audit_show(self) -> None:
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
                **self.fixture._dispatch_kwargs(gateway_request_id="gw-audit-complete"),
            )
        self.assertEqual(result.status, RESULT_STATUS_COMPLETED)

        exit_code, output, _stderr = self._run_cli("gw-audit-complete")
        self.assertEqual(exit_code, 0)
        self.assertIn("gateway_request_id: gw-audit-complete", output)
        self.assertIn("request_status: completed", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("gateway_execution_scope: isolated_gateway_mock", output)

    def test_failed_request_audit_show(self) -> None:
        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            kwargs = self.fixture._dispatch_kwargs(gateway_request_id="gw-audit-failed")
            kwargs["injected_runner"] = _mock_runner_failure
            result = execute_gateway_dispatch(**kwargs)
        self.assertEqual(result.status, RESULT_STATUS_FAILED)

        exit_code, output, _stderr = self._run_cli("gw-audit-failed")
        self.assertEqual(exit_code, 0)
        self.assertIn("request_status: failed", output)

    def test_dry_run_request_audit_show(self) -> None:
        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            kwargs = self.fixture._dispatch_kwargs(
                gateway_request_id="gw-audit-dry",
                dry_run=True,
            )
            kwargs["injected_runner"] = _mock_runner_success
            result = execute_gateway_dispatch(**kwargs)
        self.assertTrue(result.dry_run)

        exit_code, output, _stderr = self._run_cli("gw-audit-dry")
        self.assertEqual(exit_code, 0)
        self.assertIn("dry_run: true", output)

    def test_missing_request_fail_closed(self) -> None:
        exit_code, _output, stderr = self._run_cli("missing-request-id")
        self.assertEqual(exit_code, 1)
        self.assertIn("error:", stderr)

    def test_corrupted_request_fail_closed(self) -> None:
        path = self.fixture.gateway_request_dir() / "gw-corrupt.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")

        exit_code, _output, stderr = self._run_cli("gw-corrupt")
        self.assertEqual(exit_code, 1)
        self.assertIn("error:", stderr)

    def test_path_traversal_fail_closed(self) -> None:
        for bad_id in ("../escape", "foo/bar", ".."):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    normalize_gateway_request_audit_id(bad_id)
                exit_code, _output, stderr = self._run_cli(bad_id)
                self.assertEqual(exit_code, 1)
                self.assertIn("error:", stderr)

    def test_correlation_mismatch_reports_invalid(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        reserve_gateway_request(
            CooDispatchGatewayRequestRecord(
                gateway_request_id="gw-mismatch",
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                execution_attempt_id="exec-mismatch",
                dispatch_run_id="run-mismatch",
                status=REQUEST_STATUS_COMPLETED,
                dry_run=False,
                failure_reason_code="none",
                production_execution_allowed=False,
                gateway_state="staged",
                pilot_attempt_id="pilot-record",
            ),
            request_dir=self.fixture.gateway_request_dir(),
        )
        write_pilot_history_record(
            _history_record(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                gateway_request_id="gw-mismatch",
                pilot_attempt_id="pilot-history",
                execution_attempt_id="exec-mismatch",
                dispatch_run_id="run-mismatch",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )

        summary = show_gateway_request_audit("gw-mismatch", **self.kwargs)
        self.assertFalse(summary.correlation_valid)

        exit_code, output, _stderr = self._run_cli("gw-mismatch")
        self.assertEqual(exit_code, 0)
        self.assertIn("correlation_valid: false", output)

    def test_safe_output_forbidden_fields_absent(self) -> None:
        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            execute_gateway_dispatch(
                **self.fixture._dispatch_kwargs(gateway_request_id="gw-safe-output"),
            )
        _exit_code, output, _stderr = self._run_cli("gw-safe-output")
        lowered = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_read_only_digest_unchanged(self) -> None:
        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            execute_gateway_dispatch(
                **self.fixture._dispatch_kwargs(gateway_request_id="gw-readonly"),
            )
        digest_before = _hermes_digest(self.fixture.hermes_home)
        exit_code, _output, _stderr = self._run_cli("gw-readonly")
        digest_after = _hermes_digest(self.fixture.hermes_home)
        self.assertEqual(exit_code, 0)
        self.assertEqual(digest_before, digest_after)

    def test_direct_show_helper(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        prepared = reserve_gateway_request(
            CooDispatchGatewayRequestRecord(
                gateway_request_id="gw-prepared-only",
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                execution_attempt_id="",
                dispatch_run_id="",
                status="prepared",
                dry_run=True,
                failure_reason_code="",
                production_execution_allowed=False,
                gateway_state="staged",
            ),
            request_dir=self.fixture.gateway_request_dir(),
        )
        self.assertEqual(prepared.status, "prepared")
        output = run_gateway_request_audit_show("gw-prepared-only", **self.kwargs)
        self.assertIn("dry_run: true", output)
        self.assertIn("pilot_history_present: false", output)


if __name__ == "__main__":
    unittest.main()
