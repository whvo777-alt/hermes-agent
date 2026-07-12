"""Phase 13B tests — pilot operations history and regression CLI."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_pilot import (
    CooDispatchPilotReadinessSummary,
    execute_pilot_dispatch_run,
)
from agent.coo.dispatch_cli_pilot_history import (
    build_pilot_history_record_from_dispatch,
    format_pilot_history_find,
    format_pilot_history_list,
    format_pilot_history_summary,
    list_pilot_history_summaries,
    summarize_pilot_history_record,
)
from agent.coo.dispatch_cli_pilot_regression import (
    REGRESSION_STATUS_FAIL,
    REGRESSION_STATUS_PASS,
    REGRESSION_STATUS_WARN,
    evaluate_pilot_regression,
    format_pilot_regression_summary,
)
from agent.coo.dispatch_cli_repository_attestation import (
    CooDispatchRepositoryAttestationSummary,
)
from agent.coo.dispatch_cli_run import CooDispatchRunResult
from agent.coo.dispatch_pilot_history import (
    FAILURE_REASON_NONE,
    FAILURE_REASON_POLICY_BLOCKED,
    FAILURE_REASON_PREFLIGHT_FAILED,
    FAILURE_REASON_RUNNER_FAILED,
    FAILURE_REASON_TIMEOUT,
    PILOT_STATUS_DRY_RUN,
    PILOT_STATUS_FAILURE,
    PILOT_STATUS_SUCCESS,
    PILOT_STATUS_TIMEOUT,
    CooDispatchPilotHistoryRecord,
    default_pilot_history_dir,
    read_pilot_history_record,
    write_pilot_history_record,
)
from agent.coo.execution_dispatch_runtime import DispatchExecutionRunStatus
from agent.coo.production_executor_factory import _TIMEOUT_EXIT_CODE
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.coo_dispatch_isolated_clone_fixture import (
    CLONE_BEHAVIOR_FAILURE,
    CLONE_BEHAVIOR_SUCCESS,
    CLONE_BEHAVIOR_TIMEOUT,
    CooDispatchIsolatedCloneFixture,
    bounded_dispatch_config,
    run_clone_full_path_execute,
    write_clone_fake_node,
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
    pilot_attempt_id: str = "pilot-attempt-1",
    ticket_id: str = "ticket-1",
    status: str = PILOT_STATUS_SUCCESS,
    dry_run: bool = False,
    consumed: bool = True,
    evidence_present: bool = True,
    audit_present: bool = True,
    failure_reason_code: str = FAILURE_REASON_NONE,
    completed_at: str = "2026-07-13T00:00:02+00:00",
) -> CooDispatchPilotHistoryRecord:
    return CooDispatchPilotHistoryRecord(
        version=1,
        pilot_attempt_id=pilot_attempt_id,
        execution_attempt_id="exec-1",
        ticket_id=ticket_id,
        confirmation_id="confirm-1",
        dispatch_run_id="dispatch-run-1",
        execution_scope="isolated_clone",
        status=status,
        exit_code=0,
        dry_run=dry_run,
        started_at="2026-07-13T00:00:01+00:00",
        completed_at=completed_at,
        evidence_present=evidence_present,
        audit_present=audit_present,
        consumed=consumed,
        failure_reason_code=failure_reason_code,
        production_execution_allowed=False,
        production_root_hard_deny=True,
        gateway_enabled=False,
    )


@contextmanager
def _successful_signoff():
    with patch(
        "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
        return_value=_attestation_success_summary(),
    ):
        yield


class _PilotHistoryFixture(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.evidence_home_patch.stop()
        self.history_cli_home_patch.stop()
        self.history_home_patch.stop()
        self.fixture.stop()


class TestPilotHistoryPersistence(_PilotHistoryFixture):
    def test_success_history_record_fields(self) -> None:
        result = run_clone_full_path_execute(self.fixture, self.seeded)
        record = build_pilot_history_record_from_dispatch(
            pilot_attempt_id="pilot-success-1",
            started_at="2026-07-13T00:00:01+00:00",
            completed_at="2026-07-13T00:00:02+00:00",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            dispatch_request_id=result.dispatch_request_id,
            dry_run=False,
            run_result=result,
            run_error="",
            pilot_summary=_pilot_summary(),
        )
        path = write_pilot_history_record(record, history_dir=self.history_dir)
        loaded = read_pilot_history_record("pilot-success-1", history_dir=self.history_dir)
        self.assertEqual(loaded.status, PILOT_STATUS_SUCCESS)
        self.assertTrue(loaded.consumed)
        self.assertTrue(loaded.evidence_present)
        self.assertTrue(loaded.audit_present)
        self.assertFalse(loaded.production_execution_allowed)
        self.assertTrue(loaded.production_root_hard_deny)
        self.assertFalse(loaded.gateway_enabled)
        self.assertEqual(path.parent, self.history_dir)

    def test_failure_history_record(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_FAILURE,
        )
        record = build_pilot_history_record_from_dispatch(
            pilot_attempt_id="pilot-failure-1",
            started_at="2026-07-13T00:00:01+00:00",
            completed_at="2026-07-13T00:00:02+00:00",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            dispatch_request_id=result.dispatch_request_id,
            dry_run=False,
            run_result=result,
            run_error="",
            pilot_summary=_pilot_summary(),
        )
        write_pilot_history_record(record, history_dir=self.history_dir)
        loaded = read_pilot_history_record("pilot-failure-1", history_dir=self.history_dir)
        self.assertEqual(loaded.status, PILOT_STATUS_FAILURE)
        self.assertEqual(loaded.failure_reason_code, FAILURE_REASON_RUNNER_FAILED)
        self.assertFalse(loaded.consumed)

    def test_timeout_history_record(self) -> None:
        result = run_clone_full_path_execute(
            self.fixture,
            self.seeded,
            node_behavior=CLONE_BEHAVIOR_TIMEOUT,
            policy_max_runtime_seconds=1,
        )
        record = build_pilot_history_record_from_dispatch(
            pilot_attempt_id="pilot-timeout-1",
            started_at="2026-07-13T00:00:01+00:00",
            completed_at="2026-07-13T00:00:02+00:00",
            ticket_id=self.seeded["ticket"].ticket_id,
            confirmation_id=self.seeded["confirmation"].confirmation_id,
            dispatch_request_id=result.dispatch_request_id,
            dry_run=False,
            run_result=result,
            run_error="",
            pilot_summary=_pilot_summary(),
        )
        write_pilot_history_record(record, history_dir=self.history_dir)
        loaded = read_pilot_history_record("pilot-timeout-1", history_dir=self.history_dir)
        self.assertEqual(loaded.status, PILOT_STATUS_TIMEOUT)
        self.assertEqual(loaded.exit_code, _TIMEOUT_EXIT_CODE)
        self.assertEqual(loaded.failure_reason_code, FAILURE_REASON_TIMEOUT)

    def test_dry_run_history_record(self) -> None:
        with _successful_signoff():
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
        loaded = read_pilot_history_record(
            outcome.pilot_attempt_id,
            history_dir=self.history_dir,
        )
        self.assertTrue(loaded.dry_run)
        self.assertEqual(loaded.status, PILOT_STATUS_DRY_RUN)
        self.assertFalse(loaded.consumed)

    def test_retry_uses_distinct_pilot_attempt_ids(self) -> None:
        with _successful_signoff():
            first = execute_pilot_dispatch_run(
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
            second = execute_pilot_dispatch_run(
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
        self.assertNotEqual(first.pilot_attempt_id, second.pilot_attempt_id)

    def test_overwrite_rejected(self) -> None:
        record = _sample_record(pilot_attempt_id="pilot-dup")
        write_pilot_history_record(record, history_dir=self.history_dir)
        with self.assertRaises(ValueError):
            write_pilot_history_record(record, history_dir=self.history_dir)

    def test_path_traversal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            read_pilot_history_record("../escape", history_dir=self.history_dir)

    def test_symlink_escape_rejected_on_write(self) -> None:
        outside = self.fixture.pipeline_root.parent / "outside-history"
        outside.mkdir()
        evil = outside / "evil.json"
        evil.touch()
        self.history_dir.mkdir(parents=True, exist_ok=True)
        link = self.history_dir / "linked.json"
        if link.exists():
            link.unlink()
        link.symlink_to(evil)
        record = _sample_record(pilot_attempt_id="linked")
        with self.assertRaises(ValueError):
            write_pilot_history_record(record, history_dir=self.history_dir)

    def test_history_write_failure_marks_outcome(self) -> None:
        with (
            _successful_signoff(),
            patch(
                "agent.coo.dispatch_pilot_history.write_pilot_history_record",
                side_effect=ValueError("write failed"),
            ),
        ):
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
        self.assertTrue(outcome.history_persistence_failed)
        self.assertEqual(outcome.exit_code, 1)


class TestPilotHistoryReadCli(_PilotHistoryFixture):
    def setUp(self) -> None:
        super().setUp()
        write_pilot_history_record(
            _sample_record(
                pilot_attempt_id="pilot-a",
                completed_at="2026-07-13T00:00:03+00:00",
            ),
            history_dir=self.history_dir,
        )
        write_pilot_history_record(
            _sample_record(
                pilot_attempt_id="pilot-b",
                ticket_id="ticket-2",
                completed_at="2026-07-13T00:00:04+00:00",
            ),
            history_dir=self.history_dir,
        )

    def test_history_show(self) -> None:
        summary = summarize_pilot_history_record("pilot-a", history_dir=self.history_dir)
        output = format_pilot_history_summary(summary)
        self.assertIn("pilot_attempt_id: pilot-a", output)
        self.assertIn("execution_scope: isolated_clone", output)

    def test_history_list_newest_first(self) -> None:
        entries = list_pilot_history_summaries(history_dir=self.history_dir)
        self.assertEqual(entries[0].pilot_attempt_id, "pilot-b")
        self.assertEqual(entries[1].pilot_attempt_id, "pilot-a")

    def test_history_find_by_ticket(self) -> None:
        from agent.coo.dispatch_cli_pilot_history import (
            find_pilot_history_summaries_for_ticket,
        )

        entries = find_pilot_history_summaries_for_ticket(
            "ticket-1",
            history_dir=self.history_dir,
        )
        output = format_pilot_history_find(entries)
        self.assertIn("pilot-a", output)
        self.assertNotIn("pilot-b", output)

    def test_empty_list_exit_zero(self) -> None:
        empty_dir = self.fixture.hermes_home / "coo" / "empty-pilot-history"
        output = format_pilot_history_list(())
        self.assertIn("count: 0", output)
        self.assertIn("records: (none)", output)

    def test_corrupted_record_fail_closed(self) -> None:
        bad = self.history_dir / "bad-record.json"
        bad.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            list_pilot_history_summaries(history_dir=self.history_dir)

    def test_safe_output_has_no_forbidden_tokens(self) -> None:
        summary = summarize_pilot_history_record("pilot-a", history_dir=self.history_dir)
        output = format_pilot_history_summary(summary).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)
        payload = read_pilot_history_record("pilot-a", history_dir=self.history_dir).to_dict()
        self.assertNotIn("pipeline_root", payload)


class TestPilotRegression(_PilotHistoryFixture):
    def test_regression_pass(self) -> None:
        write_pilot_history_record(
            _sample_record(pilot_attempt_id="pass-1"),
            history_dir=self.history_dir,
        )
        summary = evaluate_pilot_regression(history_dir=self.history_dir)
        self.assertEqual(summary.regression_status, REGRESSION_STATUS_PASS)

    def test_regression_warn_no_history(self) -> None:
        summary = evaluate_pilot_regression(history_dir=self.history_dir)
        self.assertEqual(summary.regression_status, REGRESSION_STATUS_WARN)
        self.assertEqual(summary.total_attempts, 0)

    def test_regression_warn_dry_run_only(self) -> None:
        write_pilot_history_record(
            _sample_record(pilot_attempt_id="dry-1", dry_run=True, status=PILOT_STATUS_DRY_RUN),
            history_dir=self.history_dir,
        )
        summary = evaluate_pilot_regression(history_dir=self.history_dir)
        self.assertEqual(summary.regression_status, REGRESSION_STATUS_WARN)
        self.assertEqual(summary.dry_run_count, 1)

    def test_regression_fail_consecutive_failures(self) -> None:
        write_pilot_history_record(
            _sample_record(
                pilot_attempt_id="fail-2",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:05+00:00",
            ),
            history_dir=self.history_dir,
        )
        write_pilot_history_record(
            _sample_record(
                pilot_attempt_id="fail-1",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                evidence_present=False,
                audit_present=False,
                failure_reason_code=FAILURE_REASON_RUNNER_FAILED,
                completed_at="2026-07-13T00:00:04+00:00",
            ),
            history_dir=self.history_dir,
        )
        summary = evaluate_pilot_regression(history_dir=self.history_dir)
        self.assertEqual(summary.regression_status, REGRESSION_STATUS_FAIL)
        self.assertGreaterEqual(summary.consecutive_failures, 2)

    def test_regression_fail_missing_evidence_audit(self) -> None:
        write_pilot_history_record(
            _sample_record(
                pilot_attempt_id="bad-success",
                evidence_present=False,
                audit_present=False,
                consumed=False,
            ),
            history_dir=self.history_dir,
        )
        summary = evaluate_pilot_regression(history_dir=self.history_dir)
        self.assertEqual(summary.regression_status, REGRESSION_STATUS_FAIL)

    def test_regression_fail_production_policy_violation(self) -> None:
        write_pilot_history_record(
            CooDispatchPilotHistoryRecord(
                version=1,
                pilot_attempt_id="policy-bad",
                execution_attempt_id="exec-1",
                ticket_id="ticket-1",
                confirmation_id="confirm-1",
                dispatch_run_id="dispatch-run-1",
                execution_scope="isolated_clone",
                status=PILOT_STATUS_SUCCESS,
                exit_code=0,
                dry_run=False,
                started_at="2026-07-13T00:00:01+00:00",
                completed_at="2026-07-13T00:00:02+00:00",
                evidence_present=True,
                audit_present=True,
                consumed=True,
                failure_reason_code=FAILURE_REASON_NONE,
                production_execution_allowed=True,
                production_root_hard_deny=True,
                gateway_enabled=False,
            ),
            history_dir=self.history_dir,
        )
        summary = evaluate_pilot_regression(history_dir=self.history_dir)
        self.assertEqual(summary.regression_status, REGRESSION_STATUS_FAIL)
        self.assertEqual(summary.production_policy_violations, 1)


class TestPilotHistoryCliIntegration(_PilotHistoryFixture):
    def test_cli_history_show(self) -> None:
        write_pilot_history_record(
            _sample_record(pilot_attempt_id="cli-show"),
            history_dir=self.history_dir,
        )
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["pilot", "history", "show", "--pilot-attempt-id", "cli-show"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        self.assertIn("pilot_attempt_id: cli-show", buf.getvalue())

    def test_cli_regression_warn(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["pilot", "regression"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        self.assertIn("regression_status: WARN", buf.getvalue())

    def test_read_only_list_no_writes(self) -> None:
        before = _hermes_digest(self.fixture.hermes_home)
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            evaluate_pilot_regression(history_dir=self.history_dir)
            list_pilot_history_summaries(history_dir=self.history_dir)
            format_pilot_regression_summary(
                evaluate_pilot_regression(history_dir=self.history_dir)
            )
        self.assertEqual(_hermes_digest(self.fixture.hermes_home), before)


class TestPilotReplayHistory(_PilotHistoryFixture):
    def test_replay_rejection_records_policy_blocked_history(self) -> None:
        fake_node = write_clone_fake_node(self.fixture.pipeline_root.parent)
        run_clone_full_path_execute(self.fixture, self.seeded, fake_node=fake_node)
        with _successful_signoff():
            outcome = execute_pilot_dispatch_run(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                unlock_token_id=self.seeded["prepare"]["unlock_token"]["token_id"],
                requester_id=self.seeded["ticket"].requester_id,
                pipeline_root=str(self.fixture.pipeline_root),
                dry_run=False,
                pilot_summary=_pilot_summary(),
                merged_config=self.config,
                subprocess_runner=lambda *a, **k: (0, "ok", ""),
                use_runner_provider=False,
            )
        loaded = read_pilot_history_record(
            outcome.pilot_attempt_id,
            history_dir=self.history_dir,
        )
        self.assertEqual(loaded.failure_reason_code, FAILURE_REASON_POLICY_BLOCKED)
        self.assertEqual(loaded.status, PILOT_STATUS_FAILURE)


class TestPilotRunIntegration(_PilotHistoryFixture):
    def test_execute_pilot_dispatch_run_success_writes_history(self) -> None:
        from tests.hermes_cli.coo_dispatch_isolated_clone_fixture import (
            build_isolated_clone_tree,
            resolve_isolated_dispatch_runner,
        )

        self.fixture.clone_paths = build_isolated_clone_tree(self.fixture.pipeline_root)
        fake_node = write_clone_fake_node(
            self.fixture.pipeline_root.parent,
            CLONE_BEHAVIOR_SUCCESS,
        )
        runner = resolve_isolated_dispatch_runner(
            pipeline_root=self.fixture.pipeline_root,
            fake_node=fake_node,
            merged_config=self.config,
        )
        with _successful_signoff():
            outcome = execute_pilot_dispatch_run(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                unlock_token_id=self.seeded["prepare"]["unlock_token"]["token_id"],
                requester_id=self.seeded["ticket"].requester_id,
                pipeline_root=str(self.fixture.pipeline_root),
                dry_run=False,
                pilot_summary=_pilot_summary(),
                merged_config=self.config,
                subprocess_runner=runner,
                node_path=str(fake_node),
                use_runner_provider=False,
            )
        self.assertTrue(outcome.history_persisted)
        loaded = read_pilot_history_record(
            outcome.pilot_attempt_id,
            history_dir=self.history_dir,
        )
        self.assertEqual(loaded.status, PILOT_STATUS_SUCCESS)
        self.assertNotEqual(loaded.pilot_attempt_id, loaded.execution_attempt_id)


class TestPilotReadinessRegression(_PilotHistoryFixture):
    def test_existing_pilot_readiness_still_works(self) -> None:
        from agent.coo.dispatch_cli_pilot import evaluate_pilot_readiness

        with _successful_signoff():
            summary = evaluate_pilot_readiness(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                pipeline_root=str(self.fixture.pipeline_root),
                merged_config=self.config,
            )
        self.assertTrue(summary.pilot_ready)

    def test_preflight_failed_reason_code(self) -> None:
        record = build_pilot_history_record_from_dispatch(
            pilot_attempt_id="preflight-fail",
            started_at="2026-07-13T00:00:01+00:00",
            completed_at="2026-07-13T00:00:02+00:00",
            ticket_id="ticket-1",
            confirmation_id="confirm-1",
            dispatch_request_id="req-1",
            dry_run=True,
            run_result=CooDispatchRunResult(
                ticket_id="ticket-1",
                confirmation_id="confirm-1",
                dispatch_request_id="req-1",
                status="preflight_failed",
                consumed=False,
                dry_run_only=True,
            ),
            run_error="",
            pilot_summary=_pilot_summary(),
        )
        self.assertEqual(record.failure_reason_code, FAILURE_REASON_PREFLIGHT_FAILED)
