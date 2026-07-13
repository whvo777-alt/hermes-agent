"""Phase 13O tests — gateway operator dashboard and correlation diff CLI."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_gateway_dashboard import (
    run_gateway_correlation_diff,
    run_operator_dashboard,
    show_gateway_correlation_diff,
    show_operator_dashboard,
)
from agent.coo.dispatch_consume_transaction import CONSUME_STATE_RECOVERY_REQUIRED
from agent.coo.dispatch_gateway_enablement import GATEWAY_STATE_DISABLED
from agent.coo.dispatch_gateway_operator_dashboard import (
    DASHBOARD_ACTION_COLLECT_MORE_HISTORY,
    DASHBOARD_ACTION_INSPECT_LATEST_FAILURE,
    DASHBOARD_ACTION_NO_ACTION_REQUIRED,
    DASHBOARD_ACTION_RESOLVE_CORRELATION_MISMATCH,
    DASHBOARD_ACTION_RESOLVE_RECOVERY_REQUIRED,
    DASHBOARD_ACTION_RUN_GATEWAY_PILOT_DRY_RUN,
    DASHBOARD_HEALTH_BLOCKED,
    DASHBOARD_HEALTH_DEGRADED,
    DASHBOARD_HEALTH_HEALTHY,
    DASHBOARD_HEALTH_NOT_CONFIGURED,
    DIFF_ACTION_INSPECT_REGRESSION,
    DIFF_ACTION_NO_ACTION_REQUIRED,
    DIFF_ACTION_PROVIDE_SAME_TICKET_REQUESTS,
    GatewayOperatorDashboardError,
    correlation_diff_exit_code,
    dashboard_exit_code,
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
from tests.hermes_cli.test_coo_dispatch_gateway_pilot import _GatewayPilotFixture
from tests.hermes_cli.test_coo_dispatch_gateway_readiness import (
    _gateway_config,
    _ready_cutover_summary,
)
from tests.hermes_cli.test_coo_dispatch_production_signoff import (
    _successful_attestation,
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
    "channel_id",
    "requester_metadata",
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
    session_id: str = "",
    execution_attempt_id: str = "exec-1",
    dispatch_run_id: str = "run-1",
    status: str = PILOT_STATUS_SUCCESS,
    dry_run: bool = False,
    consumed: bool = True,
    evidence_present: bool = True,
    audit_present: bool = True,
    completed_at: str = "2026-07-13T01:00:00+00:00",
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
        completed_at=completed_at,
        evidence_present=evidence_present,
        audit_present=audit_present,
        consumed=consumed,
        failure_reason_code="none",
        production_execution_allowed=False,
        production_root_hard_deny=True,
        gateway_enabled=False,
        gateway_request_id=gateway_request_id,
        session_id=session_id,
    )


@contextmanager
def _ready_env():
    with (
        _successful_attestation(),
        patch(
            "agent.coo.dispatch_cli_gateway_readiness.evaluate_production_cutover_checklist",
            return_value=_ready_cutover_summary(),
        ),
        patch(
            "agent.coo.dispatch_gateway_operator_dashboard.evaluate_production_cutover_checklist",
            return_value=_ready_cutover_summary(),
        ),
        patch(
            "agent.coo.dispatch_cli_gateway_pilot.evaluate_production_cutover_checklist",
            return_value=_ready_cutover_summary(),
        ),
    ):
        yield


class _DashboardFixture(_GatewayPilotFixture):
    def __init__(self) -> None:
        super().__init__()
        self.dashboard_home_patch = patch(
            "agent.coo.dispatch_gateway_operator_dashboard.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.correlation_home_patch = patch(
            "agent.coo.dispatch_gateway_correlation_explorer.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.consume_home_patch = patch(
            "agent.coo.dispatch_consume_transaction.get_hermes_home",
            return_value=self.hermes_home,
        )

    def start(self) -> None:
        super().start()
        self.dashboard_home_patch.start()
        self.correlation_home_patch.start()
        self.consume_home_patch.start()

    def stop(self) -> None:
        self.consume_home_patch.stop()
        self.correlation_home_patch.stop()
        self.dashboard_home_patch.stop()
        super().stop()

    def dashboard_kwargs(self) -> dict:
        return {
            "merged_config": _gateway_config("staged"),
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
        session_id: str = "",
        status: str = REQUEST_STATUS_COMPLETED,
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
                session_id=session_id,
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
        if session_id:
            payload["session_id"] = session_id
        if execution_attempt_id:
            payload["execution_attempt_id"] = execution_attempt_id
        if dispatch_run_id:
            payload["dispatch_run_id"] = dispatch_run_id
        if pilot_attempt_id:
            payload["pilot_attempt_id"] = pilot_attempt_id
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class TestGatewayOperatorDashboard(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _DashboardFixture()
        self.fixture.start()
        self.fixture.write_binding_state("bound")
        self.fixture.pipeline_root.mkdir(parents=True, exist_ok=True)
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.kwargs = self.fixture.dashboard_kwargs()

    def tearDown(self) -> None:
        self.fixture.stop()

    def _run_dashboard_cli(self, *extra: str) -> tuple[int, str, str]:
        parser = build_coo_dispatch_parser()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "hermes_cli.config.load_config",
                return_value=_gateway_config("staged"),
            ),
            patch(
                "agent.coo.dispatch_gateway_operator_dashboard.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_gateway_correlation_explorer.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            _ready_env(),
        ):
            args = parser.parse_args(["gateway", "dashboard", *extra])
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                exit_code = args.handler(args)
            return exit_code, buffer.getvalue(), ""

    def _run_diff_cli(self, left: str, right: str) -> tuple[int, str, str]:
        parser = build_coo_dispatch_parser()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_gateway_operator_dashboard.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_gateway_correlation_explorer.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
        ):
            args = parser.parse_args(
                [
                    "gateway",
                    "correlation",
                    "diff",
                    "--left-gateway-request-id",
                    left,
                    "--right-gateway-request-id",
                    right,
                ]
            )
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                exit_code = args.handler(args)
            return exit_code, buffer.getvalue(), ""

    def test_dashboard_no_history(self) -> None:
        with _ready_env():
            summary = show_operator_dashboard(**self.kwargs)
        self.assertIn(
            summary.dashboard_health,
            {DASHBOARD_HEALTH_DEGRADED, DASHBOARD_HEALTH_BLOCKED},
        )
        self.assertEqual(summary.recommended_action, DASHBOARD_ACTION_COLLECT_MORE_HISTORY)
        self.assertEqual(dashboard_exit_code(summary), 0)

    def test_dashboard_staged_healthy(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        session_id = "session-healthy"
        self.fixture._write_request(
            gateway_request_id="gw-healthy",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            session_id=session_id,
            pilot_attempt_id="pilot-healthy",
            execution_attempt_id="exec-healthy",
            dispatch_run_id="run-healthy",
        )
        write_pilot_history_record(
            _history_record(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                gateway_request_id="gw-healthy",
                pilot_attempt_id="pilot-healthy",
                session_id=session_id,
                execution_attempt_id="exec-healthy",
                dispatch_run_id="run-healthy",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )

        def _evidence_present(execution_attempt_id: str, **kwargs):
            return type(
                "Evidence",
                (),
                {
                    "status": "completed",
                    "evidence_files_present": True,
                    "audit_present": True,
                    "dispatch_run_id": "run-healthy",
                },
            )()

        with (
            _ready_env(),
            patch(
                "agent.coo.dispatch_cli_evidence.summarize_dispatch_evidence_attempt",
                side_effect=_evidence_present,
            ),
        ):
            summary = show_operator_dashboard(**self.kwargs)
        self.assertEqual(summary.dashboard_health, DASHBOARD_HEALTH_HEALTHY)
        self.assertEqual(summary.recommended_action, DASHBOARD_ACTION_NO_ACTION_REQUIRED)
        self.assertEqual(summary.latest_gateway_request_id, "gw-healthy")

    def test_dashboard_dry_run_only_degraded(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        write_pilot_history_record(
            _history_record(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                gateway_request_id="gw-dry",
                pilot_attempt_id="pilot-dry",
                status=PILOT_STATUS_DRY_RUN,
                dry_run=True,
                consumed=False,
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        self.fixture._write_request(
            gateway_request_id="gw-dry",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            dry_run=True,
            pilot_attempt_id="pilot-dry",
        )
        with _ready_env():
            summary = show_operator_dashboard(**self.kwargs)
        self.assertEqual(summary.dashboard_health, DASHBOARD_HEALTH_DEGRADED)
        self.assertEqual(
            summary.recommended_action,
            DASHBOARD_ACTION_RUN_GATEWAY_PILOT_DRY_RUN,
        )

    def test_dashboard_latest_failure_degraded(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-fail",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_FAILED,
            pilot_attempt_id="pilot-fail",
            execution_attempt_id="exec-fail",
            dispatch_run_id="run-fail",
        )
        write_pilot_history_record(
            _history_record(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                gateway_request_id="gw-fail",
                pilot_attempt_id="pilot-fail",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                execution_attempt_id="exec-fail",
                dispatch_run_id="run-fail",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        with _ready_env():
            summary = show_operator_dashboard(**self.kwargs)
        self.assertEqual(summary.dashboard_health, DASHBOARD_HEALTH_DEGRADED)
        self.assertEqual(
            summary.recommended_action,
            DASHBOARD_ACTION_INSPECT_LATEST_FAILURE,
        )

    def test_dashboard_regression_fail_blocked(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        for idx in range(2):
            write_pilot_history_record(
                _history_record(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    gateway_request_id=f"gw-reg-{idx}",
                    pilot_attempt_id=f"pilot-reg-{idx}",
                    status=PILOT_STATUS_FAILURE,
                    consumed=False,
                    completed_at=f"2026-07-13T0{idx}:00:00+00:00",
                ),
                history_dir=self.fixture.pilot_history_dir(),
            )
        with _ready_env():
            summary = show_operator_dashboard(**self.kwargs)
        self.assertEqual(summary.dashboard_health, DASHBOARD_HEALTH_BLOCKED)
        self.assertEqual(dashboard_exit_code(summary), 1)

    def test_dashboard_recovery_required_blocked(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-recovery",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        with (
            _ready_env(),
            patch(
                "agent.coo.dispatch_gateway_request_audit.assess_consume_status",
            ) as assess,
        ):
            assess.return_value = type(
                "ConsumeStatus",
                (),
                {
                    "consume_state": CONSUME_STATE_RECOVERY_REQUIRED,
                    "recovery_required": True,
                },
            )()
            summary = show_operator_dashboard(**self.kwargs)
        self.assertEqual(summary.dashboard_health, DASHBOARD_HEALTH_BLOCKED)
        self.assertTrue(summary.recovery_required)
        self.assertEqual(
            summary.recommended_action,
            DASHBOARD_ACTION_RESOLVE_RECOVERY_REQUIRED,
        )

    def test_dashboard_repair_lock_blocked(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-lock",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        with (
            _ready_env(),
            patch(
                "agent.coo.dispatch_cli_consume_repair_lock.summarize_consume_repair_lock_status",
                return_value=type("Lock", (), {"repair_in_progress": True})(),
            ),
        ):
            summary = show_operator_dashboard(**self.kwargs)
        self.assertEqual(summary.dashboard_health, DASHBOARD_HEALTH_BLOCKED)
        self.assertTrue(summary.repair_lock_held)

    def test_dashboard_correlation_mismatch_blocked(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-mismatch",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            pilot_attempt_id="pilot-req",
            execution_attempt_id="exec-req",
            dispatch_run_id="run-req",
        )
        write_pilot_history_record(
            _history_record(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                gateway_request_id="gw-mismatch",
                pilot_attempt_id="pilot-other",
                execution_attempt_id="exec-other",
                dispatch_run_id="run-other",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        with _ready_env():
            summary = show_operator_dashboard(**self.kwargs)
        self.assertEqual(summary.dashboard_health, DASHBOARD_HEALTH_BLOCKED)
        self.assertFalse(summary.correlation_valid)
        self.assertEqual(
            summary.recommended_action,
            DASHBOARD_ACTION_RESOLVE_CORRELATION_MISMATCH,
        )

    def test_dashboard_ticket_filter(self) -> None:
        ticket_a = self.seeded["ticket"].ticket_id
        ticket_b = "ticket-other"
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-a",
            ticket_id=ticket_a,
            confirmation_id=confirmation_id,
            updated_at="2026-07-13T02:00:00+00:00",
        )
        self.fixture._write_request(
            gateway_request_id="gw-b",
            ticket_id=ticket_b,
            confirmation_id=confirmation_id,
            updated_at="2026-07-13T03:00:00+00:00",
        )
        with _ready_env():
            summary = show_operator_dashboard(ticket_id=ticket_a, **self.kwargs)
        self.assertEqual(summary.latest_gateway_request_id, "gw-a")
        self.assertEqual(summary.total_recent_requests, 1)

    def test_dashboard_session_filter(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-s1",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            session_id="session-1",
            updated_at="2026-07-13T02:00:00+00:00",
        )
        self.fixture._write_request(
            gateway_request_id="gw-s2",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            session_id="session-2",
            updated_at="2026-07-13T03:00:00+00:00",
        )
        with _ready_env():
            summary = show_operator_dashboard(session_id="session-1", **self.kwargs)
        self.assertEqual(summary.latest_gateway_request_id, "gw-s1")

    def test_dashboard_ticket_session_and_filter(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-match",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            session_id="session-and",
        )
        self.fixture._write_request(
            gateway_request_id="gw-wrong-session",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            session_id="session-other",
        )
        with _ready_env():
            summary = show_operator_dashboard(
                ticket_id=ticket_id,
                session_id="session-and",
                **self.kwargs,
            )
        self.assertEqual(summary.latest_gateway_request_id, "gw-match")
        self.assertEqual(summary.total_recent_requests, 1)

    def test_dashboard_safe_output(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-safe",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        with _ready_env():
            output, _exit_code = run_operator_dashboard(**self.kwargs)
        lowered = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_dashboard_read_only_digest_unchanged(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-ro",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        digest_before = _hermes_digest(self.fixture.hermes_home)
        with _ready_env():
            _output, exit_code = run_operator_dashboard(**self.kwargs)
        digest_after = _hermes_digest(self.fixture.hermes_home)
        self.assertEqual(digest_before, digest_after)
        self.assertEqual(exit_code, 0)

    def test_dashboard_disabled_not_configured(self) -> None:
        kwargs = {
            **self.kwargs,
            "merged_config": _gateway_config(GATEWAY_STATE_DISABLED),
        }
        with _ready_env():
            summary = show_operator_dashboard(**kwargs)
        self.assertEqual(summary.dashboard_health, DASHBOARD_HEALTH_NOT_CONFIGURED)

    def test_diff_healthy_to_healthy_no_regression(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        for gw_id, pilot_id in (("gw-left", "pilot-left"), ("gw-right", "pilot-right")):
            self.fixture._write_request(
                gateway_request_id=gw_id,
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                pilot_attempt_id=pilot_id,
                execution_attempt_id=f"exec-{pilot_id}",
                dispatch_run_id=f"run-{pilot_id}",
            )
            write_pilot_history_record(
                _history_record(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    gateway_request_id=gw_id,
                    pilot_attempt_id=pilot_id,
                    execution_attempt_id=f"exec-{pilot_id}",
                    dispatch_run_id=f"run-{pilot_id}",
                ),
                history_dir=self.fixture.pilot_history_dir(),
            )
        diff = show_gateway_correlation_diff(
            left_gateway_request_id="gw-left",
            right_gateway_request_id="gw-right",
            **{k: v for k, v in self.kwargs.items() if k != "merged_config"},
        )
        self.assertTrue(diff.same_ticket)
        self.assertFalse(diff.regression_detected)
        self.assertEqual(diff.recommended_action, DIFF_ACTION_NO_ACTION_REQUIRED)
        self.assertEqual(correlation_diff_exit_code(diff), 0)

    def test_diff_completed_to_failed_regression(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-ok",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
            pilot_attempt_id="pilot-ok",
            execution_attempt_id="exec-ok",
            dispatch_run_id="run-ok",
        )
        self.fixture._write_request(
            gateway_request_id="gw-bad",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_FAILED,
            pilot_attempt_id="pilot-bad",
            execution_attempt_id="exec-bad",
            dispatch_run_id="run-bad",
        )
        write_pilot_history_record(
            _history_record(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                gateway_request_id="gw-ok",
                pilot_attempt_id="pilot-ok",
                execution_attempt_id="exec-ok",
                dispatch_run_id="run-ok",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        write_pilot_history_record(
            _history_record(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                gateway_request_id="gw-bad",
                pilot_attempt_id="pilot-bad",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                execution_attempt_id="exec-bad",
                dispatch_run_id="run-bad",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        diff = show_gateway_correlation_diff(
            left_gateway_request_id="gw-ok",
            right_gateway_request_id="gw-bad",
            **{k: v for k, v in self.kwargs.items() if k != "merged_config"},
        )
        self.assertTrue(diff.regression_detected)
        self.assertEqual(diff.recommended_action, DIFF_ACTION_INSPECT_REGRESSION)
        self.assertEqual(correlation_diff_exit_code(diff), 1)

    def test_diff_evidence_present_to_missing_regression(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-ev-left",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            pilot_attempt_id="pilot-ev-left",
            execution_attempt_id="exec-ev-left",
            dispatch_run_id="run-ev-left",
        )
        self.fixture._write_request(
            gateway_request_id="gw-ev-right",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            pilot_attempt_id="pilot-ev-right",
            execution_attempt_id="exec-ev-right",
            dispatch_run_id="run-ev-right",
        )

        def _evidence_side_effect(execution_attempt_id: str, **kwargs):
            present = execution_attempt_id == "exec-ev-left"
            return type(
                "Evidence",
                (),
                {
                    "status": "completed",
                    "evidence_files_present": present,
                    "audit_present": present,
                    "dispatch_run_id": "run-1",
                },
            )()

        with patch(
            "agent.coo.dispatch_cli_evidence.summarize_dispatch_evidence_attempt",
            side_effect=_evidence_side_effect,
        ):
            diff = show_gateway_correlation_diff(
                left_gateway_request_id="gw-ev-left",
                right_gateway_request_id="gw-ev-right",
                **{k: v for k, v in self.kwargs.items() if k != "merged_config"},
            )
        self.assertTrue(diff.regression_detected)
        self.assertIn("evidence_present", diff.changed_fields)

    def test_diff_committed_to_recovery_required_regression(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-c-left",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        self.fixture._write_request(
            gateway_request_id="gw-c-right",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
        )
        with patch(
            "agent.coo.dispatch_gateway_request_audit.assess_consume_status",
            side_effect=[
                type("S", (), {"consume_state": "committed", "recovery_required": False})(),
                type(
                    "S",
                    (),
                    {
                        "consume_state": CONSUME_STATE_RECOVERY_REQUIRED,
                        "recovery_required": True,
                    },
                )(),
            ],
        ):
            diff = show_gateway_correlation_diff(
                left_gateway_request_id="gw-c-left",
                right_gateway_request_id="gw-c-right",
                **{k: v for k, v in self.kwargs.items() if k != "merged_config"},
            )
        self.assertTrue(diff.regression_detected)

    def test_diff_correlation_true_to_false_regression(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-corr-left",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            pilot_attempt_id="pilot-corr",
            execution_attempt_id="exec-corr",
            dispatch_run_id="run-corr",
        )
        self.fixture._write_request(
            gateway_request_id="gw-corr-right",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            pilot_attempt_id="pilot-mismatch",
            execution_attempt_id="exec-mismatch",
            dispatch_run_id="run-mismatch",
        )
        write_pilot_history_record(
            _history_record(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                gateway_request_id="gw-corr-left",
                pilot_attempt_id="pilot-corr",
                execution_attempt_id="exec-corr",
                dispatch_run_id="run-corr",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        write_pilot_history_record(
            _history_record(
                ticket_id=ticket_id,
                confirmation_id=confirmation_id,
                gateway_request_id="gw-corr-right",
                pilot_attempt_id="pilot-other",
                execution_attempt_id="exec-other",
                dispatch_run_id="run-other",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        diff = show_gateway_correlation_diff(
            left_gateway_request_id="gw-corr-left",
            right_gateway_request_id="gw-corr-right",
            **{k: v for k, v in self.kwargs.items() if k != "merged_config"},
        )
        self.assertTrue(diff.regression_detected)
        self.assertIn("correlation_valid", diff.changed_fields)

    def test_diff_same_request_id_fail(self) -> None:
        with self.assertRaises(GatewayOperatorDashboardError):
            show_gateway_correlation_diff(
                left_gateway_request_id="gw-same",
                right_gateway_request_id="gw-same",
                **{k: v for k, v in self.kwargs.items() if k != "merged_config"},
            )

    def test_diff_different_ticket_fail(self) -> None:
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-t1",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=confirmation_id,
        )
        reserve_gateway_request(
            CooDispatchGatewayRequestRecord(
                gateway_request_id="gw-t2",
                ticket_id="ticket-other",
                confirmation_id=confirmation_id,
                execution_attempt_id="",
                dispatch_run_id="",
                status=REQUEST_STATUS_COMPLETED,
                dry_run=False,
                failure_reason_code="none",
                production_execution_allowed=False,
                gateway_state="staged",
            ),
            request_dir=self.fixture.gateway_request_dir(),
        )
        path = self.fixture.gateway_request_dir() / "gw-t2.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = REQUEST_STATUS_COMPLETED
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with patch(
            "agent.coo.dispatch_gateway_request_audit.assess_consume_status",
            return_value=type(
                "ConsumeStatus",
                (),
                {"consume_state": "unconsumed", "recovery_required": False},
            )(),
        ):
            diff = show_gateway_correlation_diff(
                left_gateway_request_id="gw-t1",
                right_gateway_request_id="gw-t2",
                **{k: v for k, v in self.kwargs.items() if k != "merged_config"},
            )
        self.assertFalse(diff.same_ticket)
        self.assertEqual(
            diff.recommended_action,
            DIFF_ACTION_PROVIDE_SAME_TICKET_REQUESTS,
        )
        self.assertEqual(correlation_diff_exit_code(diff), 1)

    def test_malformed_id_fail(self) -> None:
        with self.assertRaises(GatewayOperatorDashboardError):
            show_operator_dashboard(ticket_id="../bad", **self.kwargs)

    def test_corrupted_record_fail(self) -> None:
        bad_dir = self.fixture.gateway_request_dir()
        bad_dir.mkdir(parents=True, exist_ok=True)
        bad_path = bad_dir / "gw-bad.json"
        bad_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(GatewayOperatorDashboardError):
            show_operator_dashboard(**self.kwargs)

    def test_safe_changed_fields_only(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-sf-left",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
        )
        self.fixture._write_request(
            gateway_request_id="gw-sf-right",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_FAILED,
        )
        output, _exit_code = run_gateway_correlation_diff(
            left_gateway_request_id="gw-sf-left",
            right_gateway_request_id="gw-sf-right",
            **{k: v for k, v in self.kwargs.items() if k != "merged_config"},
        )
        for field in ("request_status", "changed_fields"):
            self.assertIn(field, output)
        lowered = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_dashboard_cli_blocked_exit_code(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        for idx in range(2):
            write_pilot_history_record(
                _history_record(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    gateway_request_id=f"gw-cli-{idx}",
                    pilot_attempt_id=f"pilot-cli-{idx}",
                    status=PILOT_STATUS_FAILURE,
                    consumed=False,
                ),
                history_dir=self.fixture.pilot_history_dir(),
            )
        with _ready_env():
            exit_code, output, _stderr = self._run_dashboard_cli()
        self.assertEqual(exit_code, 1)
        self.assertIn("dashboard_health: BLOCKED", output)

    def test_diff_cli_regression_exit_code(self) -> None:
        ticket_id = self.seeded["ticket"].ticket_id
        confirmation_id = self.seeded["confirmation"].confirmation_id
        self.fixture._write_request(
            gateway_request_id="gw-cli-left",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_COMPLETED,
        )
        self.fixture._write_request(
            gateway_request_id="gw-cli-right",
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            status=REQUEST_STATUS_FAILED,
        )
        exit_code, output, _stderr = self._run_diff_cli("gw-cli-left", "gw-cli-right")
        self.assertEqual(exit_code, 1)
        self.assertIn("regression_detected: true", output)


if __name__ == "__main__":
    unittest.main()
