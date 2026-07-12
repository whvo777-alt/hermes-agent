"""Phase 13D tests — multi-ticket pilot fleet and production cutover checklist."""

from __future__ import annotations

import hashlib
import io
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_pilot_fleet import (
    FLEET_STATUS_NOT_READY,
    FLEET_STATUS_READY,
    FLEET_STATUS_WARN,
    MAX_FLEET_LIMIT,
    MAX_FLEET_TICKETS,
    TICKET_DISPOSITION_FAILED,
    TICKET_DISPOSITION_READY,
    format_pilot_fleet_summary,
    resolve_pilot_fleet_ticket_ids,
    summarize_pilot_fleet,
)
from agent.coo.dispatch_cli_pilot_regression import REGRESSION_STATUS_FAIL, REGRESSION_STATUS_PASS
from agent.coo.dispatch_cli_production_cutover import (
    RECOMMENDED_ACTION_COLLECT_MORE_PILOT_HISTORY,
    RECOMMENDED_ACTION_RESOLVE_PILOT_REGRESSIONS,
    RECOMMENDED_ACTION_RESOLVE_RECOVERY_ISSUES,
    evaluate_production_cutover_checklist,
    format_production_cutover_checklist,
)
from agent.coo.dispatch_cli_repository_attestation import (
    CooDispatchRepositoryAttestationSummary,
)
from agent.coo.dispatch_pilot_history import (
    FAILURE_REASON_RUNNER_FAILED,
    PILOT_STATUS_DRY_RUN,
    PILOT_STATUS_FAILURE,
    PILOT_STATUS_SUCCESS,
    default_pilot_history_dir,
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


def _sample_record(
    *,
    pilot_attempt_id: str,
    ticket_id: str,
    status: str = PILOT_STATUS_SUCCESS,
    dry_run: bool = False,
    consumed: bool = True,
    evidence_present: bool = True,
    audit_present: bool = True,
    failure_reason_code: str = "none",
    completed_at: str = "2026-07-13T00:00:02+00:00",
    confirmation_id: str = "confirm-1",
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
        confirmation_id=confirmation_id,
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
def _ready_env():
    from agent.coo.dispatch_cli_enablement import CooDispatchEnablementSummary

    with (
        patch(
            "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
            return_value=_attestation_success_summary(),
        ),
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
        for binding in (mock_binding, mock_runbook_binding):
            binding.return_value.state = "bound"
            binding.return_value.state_valid = True
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


class _FleetFixture(unittest.TestCase):
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
        self.config = bounded_dispatch_config(self.fixture.pipeline_root)

    def tearDown(self) -> None:
        self.evidence_home_patch.stop()
        self.history_cli_home_patch.stop()
        self.history_home_patch.stop()
        self.fixture.stop()

    def _write(self, **kwargs) -> None:
        write_pilot_history_record(
            _sample_record(**kwargs),
            history_dir=self.history_dir,
        )


class TestPilotFleetSummary(_FleetFixture):
    def test_single_ticket_pass(self) -> None:
        ticket_id = "ticket-pass-1"
        self._write(pilot_attempt_id="pass-1", ticket_id=ticket_id)
        with _ready_env():
            summary = summarize_pilot_fleet(
                ticket_ids=[ticket_id],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.ticket_count, 1)
        self.assertEqual(summary.tickets[0].regression_status, REGRESSION_STATUS_PASS)
        self.assertEqual(summary.tickets[0].ticket_disposition, TICKET_DISPOSITION_READY)

    def test_multiple_tickets_all_pass_fleet_ready(self) -> None:
        self._write(pilot_attempt_id="pass-a", ticket_id="ticket-a")
        self._write(pilot_attempt_id="pass-b", ticket_id="ticket-b")
        with _ready_env():
            summary = summarize_pilot_fleet(
                ticket_ids=["ticket-a", "ticket-b"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.fleet_status, FLEET_STATUS_READY)
        self.assertEqual(summary.ready_ticket_count, 2)

    def test_pass_plus_insufficient_data_fleet_warn(self) -> None:
        self._write(pilot_attempt_id="pass-1", ticket_id="ticket-pass")
        self._write(
            pilot_attempt_id="dry-1",
            ticket_id="ticket-dry",
            status=PILOT_STATUS_DRY_RUN,
            dry_run=True,
            consumed=False,
        )
        with _ready_env():
            summary = summarize_pilot_fleet(
                ticket_ids=["ticket-pass", "ticket-dry"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.fleet_status, FLEET_STATUS_WARN)
        self.assertGreater(summary.warn_ticket_count, 0)

    def test_one_ticket_fail_fleet_not_ready(self) -> None:
        self._write(pilot_attempt_id="pass-1", ticket_id="ticket-pass")
        self._write(
            pilot_attempt_id="fail-2",
            ticket_id="ticket-fail",
            status=PILOT_STATUS_FAILURE,
            consumed=False,
            evidence_present=False,
            audit_present=False,
            failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
            completed_at="2026-07-13T00:00:04+00:00",
        )
        self._write(
            pilot_attempt_id="fail-1",
            ticket_id="ticket-fail",
            status=PILOT_STATUS_FAILURE,
            consumed=False,
            evidence_present=False,
            audit_present=False,
            failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
            completed_at="2026-07-13T00:00:03+00:00",
        )
        with _ready_env():
            summary = summarize_pilot_fleet(
                ticket_ids=["ticket-pass", "ticket-fail"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.fleet_status, FLEET_STATUS_NOT_READY)
        self.assertEqual(summary.failed_ticket_count, 1)

    def test_policy_violation_not_ready(self) -> None:
        self._write(
            pilot_attempt_id="policy-bad",
            ticket_id="ticket-policy",
            production_execution_allowed=True,
        )
        with _ready_env():
            summary = summarize_pilot_fleet(
                ticket_ids=["ticket-policy"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.fleet_status, FLEET_STATUS_NOT_READY)
        self.assertEqual(summary.production_policy_violation_count, 1)

    def test_evidence_audit_integrity_not_ready(self) -> None:
        self._write(
            pilot_attempt_id="integrity-bad",
            ticket_id="ticket-integrity",
            evidence_present=False,
            audit_present=False,
        )
        with _ready_env():
            summary = summarize_pilot_fleet(
                ticket_ids=["ticket-integrity"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.fleet_status, FLEET_STATUS_NOT_READY)
        self.assertFalse(summary.tickets[0].evidence_integrity)
        self.assertFalse(summary.tickets[0].audit_integrity)

    def test_no_history_fleet_warn(self) -> None:
        with _ready_env():
            summary = summarize_pilot_fleet(
                ticket_ids=["ticket-empty"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.fleet_status, FLEET_STATUS_WARN)
        self.assertEqual(summary.tickets[0].total_attempts, 0)

    def test_duplicate_ticket_input_normalized(self) -> None:
        self._write(pilot_attempt_id="dup-1", ticket_id="ticket-dup")
        with _ready_env():
            summary = summarize_pilot_fleet(
                ticket_ids=["ticket-dup", "ticket-dup"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.ticket_count, 1)

    def test_max_ticket_validation(self) -> None:
        ticket_ids = [f"ticket-{index}" for index in range(MAX_FLEET_TICKETS + 1)]
        with _ready_env():
            with self.assertRaises(ValueError):
                summarize_pilot_fleet(
                    ticket_ids=ticket_ids,
                    merged_config=self.config,
                    history_dir=self.history_dir,
                )

    def test_max_limit_validation(self) -> None:
        with _ready_env():
            with self.assertRaises(ValueError):
                summarize_pilot_fleet(
                    ticket_ids=["ticket-1"],
                    limit=MAX_FLEET_LIMIT + 1,
                    merged_config=self.config,
                    history_dir=self.history_dir,
                )

    def test_corrupted_history_fail_closed(self) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        (self.history_dir / "bad.json").write_text("{bad", encoding="utf-8")
        with _ready_env():
            with self.assertRaises(ValueError):
                resolve_pilot_fleet_ticket_ids(history_dir=self.history_dir)

    def test_fleet_newest_first_from_history(self) -> None:
        self._write(
            pilot_attempt_id="older",
            ticket_id="ticket-older",
            completed_at="2026-07-13T00:00:01+00:00",
        )
        self._write(
            pilot_attempt_id="newer",
            ticket_id="ticket-newer",
            completed_at="2026-07-13T00:00:05+00:00",
        )
        with _ready_env():
            ticket_ids = resolve_pilot_fleet_ticket_ids(history_dir=self.history_dir)
            summary = summarize_pilot_fleet(
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(ticket_ids[0], "ticket-newer")
        self.assertEqual(summary.tickets[0].ticket_id, "ticket-newer")

    def test_fleet_safe_output(self) -> None:
        self._write(pilot_attempt_id="safe-1", ticket_id="ticket-safe")
        with _ready_env():
            output = format_pilot_fleet_summary(
                summarize_pilot_fleet(
                    ticket_ids=["ticket-safe"],
                    merged_config=self.config,
                    history_dir=self.history_dir,
                )
            )
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token, output)


class TestProductionCutoverChecklist(_FleetFixture):
    def test_cutover_ready_false_execution_allowed_false(self) -> None:
        self._write(pilot_attempt_id="cutover-pass", ticket_id="ticket-cutover")
        with _ready_env():
            summary = evaluate_production_cutover_checklist(
                ticket_ids=["ticket-cutover"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
            output = format_production_cutover_checklist(summary)
        self.assertFalse(summary.production_execution_allowed)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("gateway_enabled: false", output)
        self.assertTrue(summary.production_root_hard_deny)

    def test_cutover_pass_blocked_fail_counts(self) -> None:
        self._write(pilot_attempt_id="counts-pass", ticket_id="ticket-counts")
        with _ready_env():
            summary = evaluate_production_cutover_checklist(
                ticket_ids=["ticket-counts"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertGreater(summary.checks_passed_count, 0)
        self.assertGreater(summary.checks_blocked_count, 0)
        self.assertEqual(summary.checks_failed_count, 0)
        self.assertIn("execution_disabled", summary.blocked_checks)
        self.assertIn("production_root_hard_deny", summary.blocked_checks)

    def test_recovery_required_cutover_fail(self) -> None:
        ticket_id = "ticket-recovery"
        self._write(
            pilot_attempt_id="recovery-pass",
            ticket_id=ticket_id,
            confirmation_id="confirm-recovery",
        )
        with (
            _ready_env(),
            patch(
                "agent.coo.dispatch_cli_production_cutover._recovery_status_for_ticket",
                return_value=("recovery_required", True),
            ),
        ):
            summary = evaluate_production_cutover_checklist(
                ticket_ids=[ticket_id],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertFalse(summary.cutover_ready)
        self.assertIn("consume_recovery_clear", summary.failed_checks)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_RESOLVE_RECOVERY_ISSUES,
        )

    def test_stale_prepared_cutover_fail(self) -> None:
        ticket_id = "ticket-prepared"
        self._write(pilot_attempt_id="prepared-pass", ticket_id=ticket_id)
        with (
            _ready_env(),
            patch(
                "agent.coo.dispatch_cli_production_cutover._recovery_status_for_ticket",
                return_value=("prepared", True),
            ),
        ):
            summary = evaluate_production_cutover_checklist(
                ticket_ids=[ticket_id],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertIn("consume_recovery_clear", summary.failed_checks)

    def test_partial_legacy_partial_cutover_fail(self) -> None:
        ticket_id = "ticket-partial"
        self._write(pilot_attempt_id="partial-pass", ticket_id=ticket_id)
        for state in ("partial", "legacy_partial"):
            with (
                _ready_env(),
                patch(
                    "agent.coo.dispatch_cli_production_cutover._recovery_status_for_ticket",
                    return_value=(state, True),
                ),
            ):
                summary = evaluate_production_cutover_checklist(
                    ticket_ids=[ticket_id],
                    merged_config=self.config,
                    history_dir=self.history_dir,
                )
            self.assertIn("consume_recovery_clear", summary.failed_checks)

    def test_no_history_cutover_collect_more_history(self) -> None:
        with _ready_env():
            summary = evaluate_production_cutover_checklist(
                ticket_ids=["ticket-missing"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(summary.fleet_status, FLEET_STATUS_WARN)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_COLLECT_MORE_PILOT_HISTORY,
        )

    def test_regression_fail_cutover_not_ready(self) -> None:
        ticket_id = "ticket-regression-fail"
        self._write(
            pilot_attempt_id="fail-2",
            ticket_id=ticket_id,
            status=PILOT_STATUS_FAILURE,
            consumed=False,
            evidence_present=False,
            audit_present=False,
            failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
            completed_at="2026-07-13T00:00:04+00:00",
        )
        self._write(
            pilot_attempt_id="fail-1",
            ticket_id=ticket_id,
            status=PILOT_STATUS_FAILURE,
            consumed=False,
            evidence_present=False,
            audit_present=False,
            failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
            completed_at="2026-07-13T00:00:03+00:00",
        )
        with _ready_env():
            summary = evaluate_production_cutover_checklist(
                ticket_ids=[ticket_id],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertFalse(summary.cutover_ready)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_RESOLVE_PILOT_REGRESSIONS,
        )

    def test_cutover_safe_output(self) -> None:
        self._write(pilot_attempt_id="cutover-safe", ticket_id="ticket-safe-cutover")
        with _ready_env():
            output = format_production_cutover_checklist(
                evaluate_production_cutover_checklist(
                    ticket_ids=["ticket-safe-cutover"],
                    merged_config=self.config,
                    history_dir=self.history_dir,
                )
            )
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token, output)


class TestPilotFleetCutoverReadOnly(_FleetFixture):
    def test_read_only_no_writes(self) -> None:
        self._write(pilot_attempt_id="readonly-1", ticket_id="ticket-readonly")
        before = _hermes_digest(self.fixture.hermes_home)
        with _ready_env():
            summarize_pilot_fleet(
                ticket_ids=["ticket-readonly"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
            evaluate_production_cutover_checklist(
                ticket_ids=["ticket-readonly"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
        self.assertEqual(_hermes_digest(self.fixture.hermes_home), before)

    def test_subprocess_not_called(self) -> None:
        self._write(pilot_attempt_id="nosub-1", ticket_id="ticket-nosub")
        with (
            _ready_env(),
            patch.object(subprocess, "run", side_effect=AssertionError("subprocess.run")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess.Popen")),
        ):
            summarize_pilot_fleet(
                ticket_ids=["ticket-nosub"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )
            evaluate_production_cutover_checklist(
                ticket_ids=["ticket-nosub"],
                merged_config=self.config,
                history_dir=self.history_dir,
            )


class TestPilotFleetCutoverCli(_FleetFixture):
    def test_cli_fleet(self) -> None:
        self._write(pilot_attempt_id="cli-fleet", ticket_id="ticket-cli")
        parser = build_coo_dispatch_parser()
        with (
            _ready_env(),
            patch("hermes_cli.config.load_config", return_value=self.config),
        ):
            args = parser.parse_args(
                ["pilot", "fleet", "--ticket-id", "ticket-cli"],
            )
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        self.assertIn("fleet_status:", buffer.getvalue())

    def test_cli_cutover_check(self) -> None:
        self._write(pilot_attempt_id="cli-cutover", ticket_id="ticket-cutover-cli")
        parser = build_coo_dispatch_parser()
        with (
            _ready_env(),
            patch("hermes_cli.config.load_config", return_value=self.config),
        ):
            args = parser.parse_args(
                ["production", "cutover-check", "--ticket-id", "ticket-cutover-cli"],
            )
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                exit_code = args.handler(args)
        output = buffer.getvalue()
        self.assertIn("cutover_ready:", output)
        self.assertIn("production_execution_allowed: false", output)
