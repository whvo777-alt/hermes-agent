"""Phase 13N tests — gateway correlation explorer CLI."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_gateway_correlation import (
    run_gateway_correlation_show,
    show_gateway_correlation_chain,
)
from agent.coo.dispatch_gateway_correlation_explorer import (
    GatewayCorrelationExplorerError,
    QUERY_TYPE_EXECUTION_ATTEMPT,
    QUERY_TYPE_GATEWAY_REQUEST,
    RECOMMENDED_ACTION_RESOLVE_CORRELATION_MISMATCH,
    correlation_chain_exit_code,
    normalize_gateway_correlation_query,
)
from agent.coo.dispatch_gateway_execution_facade import (
    RESULT_STATUS_COMPLETED,
    RESULT_STATUS_FAILED,
    execute_gateway_dispatch,
)
from agent.coo.dispatch_gateway_request_store import (
    CooDispatchGatewayRequestRecord,
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_FAILED,
    reserve_gateway_request,
)
from agent.coo.dispatch_pilot_history import (
    EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
    PILOT_STATUS_DRY_RUN,
    PILOT_STATUS_FAILURE,
    PILOT_STATUS_SUCCESS,
    CooDispatchPilotHistoryRecord,
    write_pilot_history_record,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.test_coo_dispatch_gateway_execution_facade import (
    _GatewayMockDispatchFixture,
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
    consumed: bool = True,
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
        consumed=consumed,
        failure_reason_code="none",
        production_execution_allowed=False,
        production_root_hard_deny=True,
        gateway_enabled=False,
        gateway_request_id=gateway_request_id,
    )


class _GatewayCorrelationFixture(_GatewayMockDispatchFixture):
    def pilot_history_dir(self) -> Path:
        return self.hermes_home / "coo" / "pilot-history"

    def correlation_kwargs(self) -> dict:
        return {
            "request_dir": self.gateway_request_dir(),
            "history_dir": self.pilot_history_dir(),
            "bundle_dir": self.bundle_dir,
            "confirmation_dir": self.confirmation_dir,
        }

    def _write_request(
        self,
        *,
        gateway_request_id: str,
        ticket_id: str,
        confirmation_id: str,
        status: str,
        dry_run: bool = False,
        pilot_attempt_id: str = "",
        execution_attempt_id: str = "",
        dispatch_run_id: str = "",
        updated_at: str = "2026-07-13T01:00:00+00:00",
    ) -> None:
        reserve_gateway_request(
            CooDispatchGatewayRequestRecord(
                gateway_request_id=gateway_request_id,
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                execution_attempt_id=execution_attempt_id,
                dispatch_run_id=dispatch_run_id,
                status=REQUEST_STATUS_COMPLETED,
                dry_run=dry_run,
                failure_reason_code="none",
                production_execution_allowed=False,
                gateway_state="staged",
                pilot_attempt_id=pilot_attempt_id,
            ),
            request_dir=self.gateway_request_dir(),
        )
        path = self.gateway_request_dir() / f"{gateway_request_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["updated_at"] = updated_at
        payload["status"] = status
        payload["dry_run"] = dry_run
        if execution_attempt_id:
            payload["execution_attempt_id"] = execution_attempt_id
        if dispatch_run_id:
            payload["dispatch_run_id"] = dispatch_run_id
        if pilot_attempt_id:
            payload["pilot_attempt_id"] = pilot_attempt_id
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class TestGatewayCorrelationExplorer(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _GatewayCorrelationFixture()
        self.fixture.start()
        self.fixture.write_binding_state("bound")
        self.fixture.pipeline_root.mkdir(parents=True, exist_ok=True)
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.kwargs = self.fixture.correlation_kwargs()
        self.pilot_history_home_patch = patch(
            "agent.coo.dispatch_pilot_history.get_hermes_home",
            return_value=self.fixture.hermes_home,
        )
        self.pilot_history_home_patch.start()

    def tearDown(self) -> None:
        self.pilot_history_home_patch.stop()
        self.fixture.stop()

    def _run_cli(self, *extra_args: str) -> tuple[int, str, str]:
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
            patch(
                "agent.coo.dispatch_gateway_correlation_explorer.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
        ):
            args = parser.parse_args(["gateway", "correlation", "show", *extra_args])
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                exit_code = args.handler(args)
            return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_show_by_gateway_request_id(self) -> None:
        self.fixture._write_request(
            gateway_request_id="gw-corr-1",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            pilot_attempt_id="pilot-1",
            execution_attempt_id="exec-1",
            dispatch_run_id="run-1",
        )
        exit_code, output, _stderr = self._run_cli("--gateway-request-id", "gw-corr-1")
        self.assertEqual(exit_code, 0)
        self.assertIn("query_type: gateway_request_id", output)
        self.assertIn("gateway_request_id: gw-corr-1", output)

    def test_show_by_pilot_attempt_id(self) -> None:
        self.fixture._write_request(
            gateway_request_id="gw-corr-pilot",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            pilot_attempt_id="pilot-corr",
        )
        write_pilot_history_record(
            _history_record(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                gateway_request_id="gw-corr-pilot",
                pilot_attempt_id="pilot-corr",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        exit_code, output, _stderr = self._run_cli("--pilot-attempt-id", "pilot-corr")
        self.assertEqual(exit_code, 0)
        self.assertIn("query_type: pilot_attempt_id", output)
        self.assertIn("gateway_request_id: gw-corr-pilot", output)

    def test_show_by_execution_attempt_id(self) -> None:
        self.fixture._write_request(
            gateway_request_id="gw-corr-exec",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            execution_attempt_id="exec-corr",
        )
        exit_code, output, _stderr = self._run_cli("--execution-attempt-id", "exec-corr")
        self.assertEqual(exit_code, 0)
        self.assertIn("execution_attempt_id: exec-corr", output)

    def test_show_by_dispatch_run_id(self) -> None:
        self.fixture._write_request(
            gateway_request_id="gw-corr-run",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            dispatch_run_id="run-corr",
        )
        exit_code, output, _stderr = self._run_cli("--dispatch-run-id", "run-corr")
        self.assertEqual(exit_code, 0)
        self.assertIn("dispatch_run_id: run-corr", output)

    def test_show_by_ticket_id_newest_request(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-old",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            updated_at="2026-07-13T00:00:00+00:00",
        )
        self.fixture._write_request(
            gateway_request_id="gw-new",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            updated_at="2026-07-13T02:00:00+00:00",
        )
        exit_code, output, _stderr = self._run_cli("--ticket-id", ticket_id)
        self.assertEqual(exit_code, 0)
        self.assertIn("gateway_request_id: gw-new", output)

    def test_no_id_fail_closed(self) -> None:
        exit_code, _output, stderr = self._run_cli()
        self.assertEqual(exit_code, 1)
        self.assertIn("error:", stderr)

    def test_multiple_ids_fail_closed(self) -> None:
        with self.assertRaises(GatewayCorrelationExplorerError):
            normalize_gateway_correlation_query(
                gateway_request_id="gw-1",
                ticket_id="ticket-1",
            )
        exit_code, _output, stderr = self._run_cli(
            "--gateway-request-id",
            "gw-1",
            "--ticket-id",
            self.seeded["ticket"].ticket_id,
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("error:", stderr)

    def test_malformed_id_fail_closed(self) -> None:
        exit_code, _output, stderr = self._run_cli("--gateway-request-id", "../bad")
        self.assertEqual(exit_code, 1)
        self.assertIn("error:", stderr)

    def test_missing_id_target_fail_closed(self) -> None:
        exit_code, _output, stderr = self._run_cli("--gateway-request-id", "missing-gw")
        self.assertEqual(exit_code, 1)
        self.assertIn("error:", stderr)

    def test_duplicate_execution_correlation_fail_closed(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-dup-a",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            execution_attempt_id="exec-dup",
        )
        self.fixture._write_request(
            gateway_request_id="gw-dup-b",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_FAILED,
            execution_attempt_id="exec-dup",
        )
        exit_code, _output, stderr = self._run_cli("--execution-attempt-id", "exec-dup")
        self.assertEqual(exit_code, 1)
        self.assertIn("Ambiguous", stderr)

    def test_ambiguous_ticket_same_timestamp_fail_closed(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        same_ts = "2026-07-13T03:00:00+00:00"
        self.fixture._write_request(
            gateway_request_id="gw-amb-a",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            updated_at=same_ts,
        )
        self.fixture._write_request(
            gateway_request_id="gw-amb-b",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            updated_at=same_ts,
        )
        exit_code, output, _stderr = self._run_cli("--ticket-id", ticket_id)
        self.assertEqual(exit_code, 1)
        self.assertIn("ambiguity_detected: true", output)

    def test_correlation_mismatch_summary_exit_one(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-mismatch",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            pilot_attempt_id="pilot-record",
            execution_attempt_id="exec-mismatch",
            dispatch_run_id="run-mismatch",
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
        chain = show_gateway_correlation_chain(
            gateway_request_id="gw-mismatch",
            **self.kwargs,
        )
        self.assertFalse(chain.correlation_valid)
        self.assertEqual(
            chain.recommended_action,
            RECOMMENDED_ACTION_RESOLVE_CORRELATION_MISMATCH,
        )
        self.assertEqual(correlation_chain_exit_code(chain), 1)

    def test_failed_request_chain(self) -> None:
        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            execute_gateway_dispatch(
                **{
                    **self.fixture._dispatch_kwargs(gateway_request_id="gw-failed-chain"),
                    "injected_runner": _mock_runner_failure,
                }
            )
        exit_code, output, _stderr = self._run_cli(
            "--gateway-request-id",
            "gw-failed-chain",
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("request_status: failed", output)

    def test_dry_run_chain(self) -> None:
        with (
            _successful_attestation(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            kwargs = self.fixture._dispatch_kwargs(
                gateway_request_id="gw-dry-chain",
                dry_run=True,
            )
            kwargs["injected_runner"] = _mock_runner_success
            execute_gateway_dispatch(**kwargs)
        exit_code, output, _stderr = self._run_cli("--gateway-request-id", "gw-dry-chain")
        self.assertEqual(exit_code, 0)
        self.assertIn("request_status: completed", output)

    def test_completed_consumed_chain(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-complete",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            pilot_attempt_id="pilot-complete",
            execution_attempt_id="exec-complete",
            dispatch_run_id="run-complete",
        )
        write_pilot_history_record(
            _history_record(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                gateway_request_id="gw-complete",
                pilot_attempt_id="pilot-complete",
                execution_attempt_id="exec-complete",
                dispatch_run_id="run-complete",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        chain = show_gateway_correlation_chain(
            gateway_request_id="gw-complete",
            **self.kwargs,
        )
        self.assertTrue(chain.correlation_valid)

    def test_recovery_required_chain(self) -> None:
        self.fixture._write_request(
            gateway_request_id="gw-recovery",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
        )
        with patch(
            "agent.coo.dispatch_gateway_request_audit.assess_consume_status",
        ) as assess:
            assess.return_value = type(
                "ConsumeStatus",
                (),
                {"consume_state": "recovery_required", "recovery_required": True},
            )()
            chain = show_gateway_correlation_chain(
                gateway_request_id="gw-recovery",
                **self.kwargs,
            )
        self.assertTrue(chain.recovery_required)
        self.assertEqual(chain.recommended_action, "resolve_recovery_required")

    def test_repair_audit_chain(self) -> None:
        self.fixture._write_request(
            gateway_request_id="gw-repair",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            execution_attempt_id="exec-repair",
        )
        with patch(
            "agent.coo.dispatch_gateway_request_audit._resolve_repair_audit",
            return_value=("repair-1", True),
        ):
            chain = show_gateway_correlation_chain(
                gateway_request_id="gw-repair",
                **self.kwargs,
            )
        self.assertTrue(chain.repair_audit_present)
        self.assertEqual(chain.repair_attempt_id, "repair-1")

    def test_missing_evidence_chain(self) -> None:
        self.fixture._write_request(
            gateway_request_id="gw-no-evidence",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            execution_attempt_id="exec-missing-evidence",
        )
        chain = show_gateway_correlation_chain(
            gateway_request_id="gw-no-evidence",
            **self.kwargs,
        )
        self.assertFalse(chain.evidence_present)
        self.assertEqual(chain.recommended_action, "inspect_missing_evidence")

    def test_safe_output_forbidden_fields_absent(self) -> None:
        self.fixture._write_request(
            gateway_request_id="gw-safe",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
        )
        _exit_code, output, _stderr = self._run_cli("--gateway-request-id", "gw-safe")
        lowered = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_read_only_digest_unchanged(self) -> None:
        self.fixture._write_request(
            gateway_request_id="gw-readonly",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
        )
        digest_before = _hermes_digest(self.fixture.hermes_home)
        exit_code, _output, _stderr = self._run_cli("--gateway-request-id", "gw-readonly")
        digest_after = _hermes_digest(self.fixture.hermes_home)
        self.assertEqual(exit_code, 0)
        self.assertEqual(digest_before, digest_after)

    def test_direct_helper_exit_code(self) -> None:
        self.fixture._write_request(
            gateway_request_id="gw-helper",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
        )
        output, exit_code = run_gateway_correlation_show(
            gateway_request_id="gw-helper",
            **self.kwargs,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Gateway Correlation Chain", output)

    def test_query_type_constants(self) -> None:
        query = normalize_gateway_correlation_query(gateway_request_id="gw-const")
        self.assertEqual(query.query_type, QUERY_TYPE_GATEWAY_REQUEST)


if __name__ == "__main__":
    unittest.main()
