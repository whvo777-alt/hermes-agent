"""Phase 13C tests — pilot drill runbook and regression gate."""

from __future__ import annotations

import hashlib
import io
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_pilot import (
    CooDispatchPilotReadinessSummary,
    execute_pilot_dispatch_run,
)
from agent.coo.dispatch_cli_run import CooDispatchRunResult
from agent.coo.dispatch_cli_pilot_regression import (
    REGRESSION_STATUS_FAIL,
    REGRESSION_STATUS_PASS,
    REGRESSION_STATUS_WARN,
)
from agent.coo.dispatch_cli_pilot_regression_gate import (
    REGRESSION_GATE_BLOCKED_FOR_LIVE,
    REGRESSION_GATE_CLEAR,
    REGRESSION_GATE_WARN_LIVE_ALLOWED,
    evaluate_pilot_regression_gate,
)
from agent.coo.dispatch_cli_pilot_runbook import (
    RECOMMENDED_ACTION_COLLECT_INITIAL_PILOT_HISTORY,
    RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE,
    RECOMMENDED_ACTION_MAINTAIN_PRODUCTION_BLOCK,
    RECOMMENDED_ACTION_RESOLVE_REGRESSION_FAILURE,
    RECOMMENDED_ACTION_RUN_ISOLATED_PILOT,
    RECOMMENDED_ACTION_RUN_PILOT_DRY_RUN,
    TREND_STATUS_DEGRADED,
    TREND_STATUS_INSUFFICIENT_DATA,
    TREND_STATUS_STABLE,
    evaluate_pilot_trend,
    format_pilot_drill_runbook,
    summarize_pilot_drill_runbook,
)
from agent.coo.dispatch_cli_repository_attestation import (
    CooDispatchRepositoryAttestationSummary,
)
from agent.coo.dispatch_pilot_history import (
    FAILURE_REASON_POLICY_BLOCKED,
    FAILURE_REASON_RUNNER_FAILED,
    PILOT_STATUS_DRY_RUN,
    PILOT_STATUS_FAILURE,
    PILOT_STATUS_SUCCESS,
    PILOT_STATUS_TIMEOUT,
    default_pilot_history_dir,
    read_pilot_history_record,
    write_pilot_history_record,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.coo_dispatch_isolated_clone_fixture import (
    CooDispatchIsolatedCloneFixture,
    bounded_dispatch_config,
)

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
        recommended_next_phase="Phase 13A Isolated Operational Dispatch Pilot",
    )


def _pilot_summary() -> CooDispatchPilotReadinessSummary:
    return CooDispatchPilotReadinessSummary(
        pilot_ready=True,
        signoff_ready=True,
        runner_bound=True,
        runtime_enablement_ready=True,
        operator_ready="true",
        pipeline_root_trusted="true",
        production_execution_allowed=False,
        gateway_enabled=False,
        production_root_hard_deny=True,
        execution_scope="isolated_clone",
        failed_checks="(none)",
        operator_action="approve_isolated_operational_drill",
    )


def _sample_record(
    *,
    pilot_attempt_id: str,
    status: str = PILOT_STATUS_SUCCESS,
    dry_run: bool = False,
    consumed: bool = True,
    evidence_present: bool = True,
    audit_present: bool = True,
    failure_reason_code: str = "none",
    completed_at: str = "2026-07-13T00:00:02+00:00",
    ticket_id: str = "ticket-1",
    production_execution_allowed: bool = False,
    production_root_hard_deny: bool = True,
    gateway_enabled: bool = False,
):
    from agent.coo.dispatch_pilot_history import CooDispatchPilotHistoryRecord

    return CooDispatchPilotHistoryRecord(
        version=1,
        pilot_attempt_id=pilot_attempt_id,
        execution_attempt_id="exec-1",
        ticket_id=ticket_id,
        confirmation_id="confirm-1",
        dispatch_run_id="dispatch-run-1",
        execution_scope="isolated_clone",
        status=status,
        exit_code=0 if status == PILOT_STATUS_SUCCESS else 1,
        dry_run=dry_run,
        started_at="2026-07-13T00:00:01+00:00",
        completed_at=completed_at,
        evidence_present=evidence_present,
        audit_present=audit_present,
        consumed=consumed,
        failure_reason_code=failure_reason_code,
        production_execution_allowed=production_execution_allowed,
        production_root_hard_deny=production_root_hard_deny,
        gateway_enabled=gateway_enabled,
    )


@contextmanager
def _successful_signoff():
    with patch(
        "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
        return_value=_attestation_success_summary(),
    ):
        yield


@contextmanager
def _ready_pilot_env():
    from agent.coo.dispatch_cli_enablement import CooDispatchEnablementSummary

    with (
        _successful_signoff(),
        patch(
            "agent.coo.dispatch_cli_pilot.load_dispatch_runner_binding_state",
        ) as mock_binding,
        patch(
            "agent.coo.dispatch_cli_pilot.runner_binding_state_is_bound",
            return_value=True,
        ),
        patch(
            "agent.coo.dispatch_cli_pilot.evaluate_dispatch_runtime_enablement",
        ) as mock_runtime,
        patch(
            "agent.coo.dispatch_cli_pilot_runbook.load_dispatch_runner_binding_state",
        ) as mock_runbook_binding,
        patch(
            "agent.coo.dispatch_cli_pilot_runbook.evaluate_dispatch_enablement",
        ) as mock_enablement,
    ):
        mock_binding.return_value.state = "bound"
        mock_binding.return_value.state_valid = True
        mock_runbook_binding.return_value.state = "bound"
        mock_runbook_binding.return_value.state_valid = True
        mock_runtime.return_value = CooDispatchEnablementSummary(
            enablement_ready=True,
            runner_bound=True,
            runner_binding_state="bound",
        )
        mock_enablement.return_value = CooDispatchEnablementSummary(
            enablement_ready=True,
            runner_bound=True,
            runner_binding_state="bound",
            runner_provider_mode="configured",
            runner_provider_configured=True,
        )
        yield


class _PilotRunbookFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CooDispatchIsolatedCloneFixture()
        self.fixture.start()
        self.history_home_patch = patch(
            "agent.coo.dispatch_pilot_history.get_hermes_home",
            return_value=self.fixture.hermes_home,
        )
        self.history_cli_home_patch = patch(
            "agent.coo.dispatch_cli_pilot_history.get_hermes_home",
            return_value=self.fixture.hermes_home,
        )
        self.evidence_home_patch = patch(
            "agent.coo.dispatch_cli_evidence.get_hermes_home",
            return_value=self.fixture.hermes_home,
        )
        self.history_home_patch.start()
        self.history_cli_home_patch.start()
        self.evidence_home_patch.start()
        self.history_dir = default_pilot_history_dir()
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.config = bounded_dispatch_config(self.fixture.pipeline_root)
        self.ticket_id = self.seeded["ticket"].ticket_id

    def _record(self, **kwargs):
        kwargs.setdefault("ticket_id", self.ticket_id)
        return _sample_record(**kwargs)

    def tearDown(self) -> None:
        self.evidence_home_patch.stop()
        self.history_cli_home_patch.stop()
        self.history_home_patch.stop()
        self.fixture.stop()


class TestPilotRunbookSummary(_PilotRunbookFixture):
    def test_no_history_insufficient_data_and_collect_initial(self) -> None:
        with _ready_pilot_env():
            summary = summarize_pilot_drill_runbook(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.trend_status, TREND_STATUS_INSUFFICIENT_DATA)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_COLLECT_INITIAL_PILOT_HISTORY,
        )
        self.assertEqual(summary.total_attempts, 0)

    def test_dry_run_only_recommends_dry_run(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="dry-1",
                status=PILOT_STATUS_DRY_RUN,
                dry_run=True,
                consumed=False,
            ),
            history_dir=self.history_dir,
        )
        with _ready_pilot_env():
            summary = summarize_pilot_drill_runbook(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.regression_status, REGRESSION_STATUS_WARN)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_RUN_PILOT_DRY_RUN,
        )
        self.assertEqual(summary.trend_status, TREND_STATUS_INSUFFICIENT_DATA)

    def test_pass_regression_allows_live_pilot(self) -> None:
        write_pilot_history_record(
            self._record(pilot_attempt_id="pass-1"),
            history_dir=self.history_dir,
        )
        with _ready_pilot_env():
            summary = summarize_pilot_drill_runbook(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.regression_status, REGRESSION_STATUS_PASS)
        self.assertTrue(summary.pilot_execution_allowed)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_RUN_ISOLATED_PILOT,
        )
        self.assertEqual(summary.regression_gate, REGRESSION_GATE_CLEAR)

    def test_warn_single_failure_before_success_allows_with_warning(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="success-1",
                completed_at="2026-07-13T00:00:04+00:00",
            ),
            history_dir=self.history_dir,
        )
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="fail-1",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:03+00:00",
            ),
            history_dir=self.history_dir,
        )
        with _ready_pilot_env():
            summary = summarize_pilot_drill_runbook(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.regression_status, REGRESSION_STATUS_WARN)
        self.assertEqual(summary.regression_gate, REGRESSION_GATE_WARN_LIVE_ALLOWED)
        self.assertTrue(summary.pilot_execution_allowed)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_RUN_ISOLATED_PILOT,
        )

    def test_production_policy_violation_blocks(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="policy-bad",
                production_execution_allowed=True,
            ),
            history_dir=self.history_dir,
        )
        with _ready_pilot_env():
            summary = summarize_pilot_drill_runbook(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertFalse(summary.production_policy_valid)
        self.assertFalse(summary.pilot_execution_allowed)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_MAINTAIN_PRODUCTION_BLOCK,
        )

    def test_signoff_not_ready_blocks_runbook(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
            side_effect=ValueError("attestation failed"),
        ):
            summary = summarize_pilot_drill_runbook(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertFalse(summary.signoff_ready)
        self.assertFalse(summary.pilot_runbook_ready)

    def test_binding_invalid_blocks_runbook(self) -> None:
        from agent.coo.dispatch_runner_binding_state import (
            DispatchRunnerBindingStateError,
        )

        with (
            _successful_signoff(),
            patch(
                "agent.coo.dispatch_cli_pilot_runbook.load_dispatch_runner_binding_state",
                side_effect=DispatchRunnerBindingStateError("binding invalid"),
            ),
            patch(
                "agent.coo.dispatch_cli_pilot.load_dispatch_runner_binding_state",
                side_effect=DispatchRunnerBindingStateError("binding invalid"),
            ),
            patch(
                "agent.coo.dispatch_cli_pilot.runner_binding_state_is_bound",
                return_value=False,
            ),
        ):
            summary = summarize_pilot_drill_runbook(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.binding_state, "invalid")
        self.assertFalse(summary.pilot_runbook_ready)

    def test_runbook_safe_output(self) -> None:
        write_pilot_history_record(
            self._record(pilot_attempt_id="safe-1"),
            history_dir=self.history_dir,
        )
        with _ready_pilot_env():
            summary = summarize_pilot_drill_runbook(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
            output = format_pilot_drill_runbook(summary)
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token, output)
        self.assertIn("pilot_runbook_ready:", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("gateway_enabled: false", output)


class TestPilotTrend(_PilotRunbookFixture):
    def test_trend_stable(self) -> None:
        write_pilot_history_record(
            self._record(pilot_attempt_id="stable-1"),
            history_dir=self.history_dir,
        )
        trend = evaluate_pilot_trend(history_dir=self.history_dir)
        self.assertEqual(trend.trend_status, TREND_STATUS_STABLE)
        self.assertEqual(trend.pass_count, 1)
        self.assertEqual(trend.consecutive_failures, 0)
        self.assertEqual(trend.success_rate_percent, 100)

    def test_trend_degraded_consecutive_failures(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="fail-2",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:04+00:00",
            ),
            history_dir=self.history_dir,
        )
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="fail-1",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:03+00:00",
            ),
            history_dir=self.history_dir,
        )
        trend = evaluate_pilot_trend(history_dir=self.history_dir)
        self.assertEqual(trend.trend_status, TREND_STATUS_DEGRADED)
        self.assertEqual(trend.consecutive_failures, 2)
        self.assertIn("runner_failed=2", trend.failure_reason_counts)

    def test_trend_insufficient_data(self) -> None:
        trend = evaluate_pilot_trend(history_dir=self.history_dir)
        self.assertEqual(trend.trend_status, TREND_STATUS_INSUFFICIENT_DATA)

    def test_trend_degraded_timeout_repeat(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="timeout-2",
                status=PILOT_STATUS_TIMEOUT,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code="timeout",
                completed_at="2026-07-13T00:00:04+00:00",
            ),
            history_dir=self.history_dir,
        )
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="timeout-1",
                status=PILOT_STATUS_TIMEOUT,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code="timeout",
                completed_at="2026-07-13T00:00:03+00:00",
            ),
            history_dir=self.history_dir,
        )
        trend = evaluate_pilot_trend(history_dir=self.history_dir)
        self.assertEqual(trend.trend_status, TREND_STATUS_DEGRADED)
        self.assertEqual(trend.timeout_count, 2)

    def test_failure_reason_distribution(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="fail-dist",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
            ),
            history_dir=self.history_dir,
        )
        trend = evaluate_pilot_trend(history_dir=self.history_dir)
        self.assertIn("runner_failed=1", trend.failure_reason_counts)
        self.assertIn("none=0", trend.failure_reason_counts)


class TestRegressionGate(_PilotRunbookFixture):
    def test_fail_consecutive_blocks_live_pilot(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="gate-fail-2",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:04+00:00",
            ),
            history_dir=self.history_dir,
        )
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="gate-fail-1",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:03+00:00",
            ),
            history_dir=self.history_dir,
        )
        gate = evaluate_pilot_regression_gate(history_dir=self.history_dir, dry_run=False)
        self.assertEqual(gate.regression_status, REGRESSION_STATUS_FAIL)
        self.assertFalse(gate.live_pilot_allowed)
        self.assertEqual(gate.regression_gate, REGRESSION_GATE_BLOCKED_FOR_LIVE)

    def test_fail_blocks_subprocess_and_writes_policy_blocked_history(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="live-fail-2",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:04+00:00",
            ),
            history_dir=self.history_dir,
        )
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="live-fail-1",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:03+00:00",
            ),
            history_dir=self.history_dir,
        )
        with patch(
            "agent.coo.dispatch_cli_run.execute_coo_dispatch_run",
        ) as mock_run:
            outcome = execute_pilot_dispatch_run(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                unlock_token_id=self.seeded["prepare"]["unlock_token"]["token_id"],
                requester_id=self.seeded["ticket"].requester_id,
                pipeline_root=str(self.fixture.pipeline_root),
                dry_run=False,
                pilot_summary=_pilot_summary(),
                merged_config=self.config,
                use_runner_provider=False,
            )
        mock_run.assert_not_called()
        self.assertTrue(outcome.regression_gate_blocked)
        self.assertEqual(outcome.regression_gate, REGRESSION_GATE_BLOCKED_FOR_LIVE)
        loaded = read_pilot_history_record(
            outcome.pilot_attempt_id,
            history_dir=self.history_dir,
        )
        self.assertEqual(loaded.failure_reason_code, FAILURE_REASON_POLICY_BLOCKED)
        self.assertFalse(loaded.consumed)
        self.assertEqual(loaded.status, PILOT_STATUS_FAILURE)

    def test_fail_allows_dry_run_diagnostic(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="dry-fail-2",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:04+00:00",
            ),
            history_dir=self.history_dir,
        )
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="dry-fail-1",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:03+00:00",
            ),
            history_dir=self.history_dir,
        )
        gate = evaluate_pilot_regression_gate(history_dir=self.history_dir, dry_run=True)
        self.assertTrue(gate.dry_run_allowed)
        self.assertEqual(gate.regression_gate, REGRESSION_GATE_BLOCKED_FOR_LIVE)

        with patch(
            "agent.coo.dispatch_cli_run.execute_coo_dispatch_run",
            return_value=CooDispatchRunResult(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                dispatch_request_id="req-1",
                status="preflight_passed",
                consumed=False,
                dry_run_only=True,
                preflight=None,
            ),
        ) as mock_run:
            outcome = execute_pilot_dispatch_run(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                unlock_token_id=self.seeded["prepare"]["unlock_token"]["token_id"],
                requester_id=self.seeded["ticket"].requester_id,
                pipeline_root=str(self.fixture.pipeline_root),
                dry_run=True,
                pilot_summary=_pilot_summary(),
                merged_config=self.config,
                use_runner_provider=False,
            )
        mock_run.assert_called_once()
        self.assertFalse(outcome.regression_gate_blocked)
        self.assertEqual(outcome.regression_gate, REGRESSION_GATE_BLOCKED_FOR_LIVE)

    def test_pass_gate_allows_live(self) -> None:
        write_pilot_history_record(
            self._record(pilot_attempt_id="gate-pass"),
            history_dir=self.history_dir,
        )
        gate = evaluate_pilot_regression_gate(history_dir=self.history_dir, dry_run=False)
        self.assertTrue(gate.live_pilot_allowed)
        self.assertEqual(gate.regression_gate, REGRESSION_GATE_CLEAR)


class TestPilotRunbookReadOnly(_PilotRunbookFixture):
    def test_read_only_runbook_no_writes(self) -> None:
        write_pilot_history_record(
            self._record(pilot_attempt_id="readonly-1"),
            history_dir=self.history_dir,
        )
        before = _hermes_digest(self.fixture.hermes_home)
        with _ready_pilot_env():
            summarize_pilot_drill_runbook(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
            evaluate_pilot_trend(history_dir=self.history_dir)
            evaluate_pilot_regression_gate(history_dir=self.history_dir)
        self.assertEqual(_hermes_digest(self.fixture.hermes_home), before)

    def test_corrupted_history_fail_closed(self) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        (self.history_dir / "bad.json").write_text("{not-json", encoding="utf-8")
        with _ready_pilot_env():
            with self.assertRaises(ValueError):
                summarize_pilot_drill_runbook(
                    merged_config=self.config,
                    history_dir=self.history_dir,
                )


class TestPilotRunbookCli(_PilotRunbookFixture):
    def test_cli_runbook_warn_no_history(self) -> None:
        parser = build_coo_dispatch_parser()
        with (
            _ready_pilot_env(),
            patch("hermes_cli.config.load_config", return_value=self.config),
        ):
            args = parser.parse_args(["pilot", "runbook"])
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                exit_code = args.handler(args)
        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("collect_initial_pilot_history", output)
        self.assertIn("INSUFFICIENT_DATA", output)

    def test_cli_runbook_fail_regression(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="cli-fail-2",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:04+00:00",
            ),
            history_dir=self.history_dir,
        )
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="cli-fail-1",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:03+00:00",
            ),
            history_dir=self.history_dir,
        )
        parser = build_coo_dispatch_parser()
        with (
            _ready_pilot_env(),
            patch("hermes_cli.config.load_config", return_value=self.config),
        ):
            args = parser.parse_args(["pilot", "runbook"])
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                exit_code = args.handler(args)
        output = buffer.getvalue()
        self.assertIn("resolve_regression_failure", output)
        self.assertFalse("pilot_execution_allowed: true" in output)


class TestPilotRunbookInvestigate(_PilotRunbookFixture):
    def test_investigate_recent_failure_without_success(self) -> None:
        write_pilot_history_record(
            self._record(
                pilot_attempt_id="investigate-fail",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
            ),
            history_dir=self.history_dir,
        )
        with _ready_pilot_env():
            summary = summarize_pilot_drill_runbook(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.regression_status, REGRESSION_STATUS_FAIL)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE,
        )
