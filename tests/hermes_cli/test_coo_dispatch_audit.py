"""Phase 11C / 11F / 11G tests — read-only dispatch execution audit CLI."""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_audit import (
    find_dispatch_execution_audits_for_ticket,
    format_dispatch_audit_find,
    format_dispatch_audit_list,
    format_dispatch_audit_summary,
    list_dispatch_execution_audits,
    summarize_dispatch_execution_audit,
)
from agent.coo.dispatch_execution_audit import (
    build_dispatch_execution_audit,
    write_dispatch_execution_audit,
)
from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionRequest,
    DispatchExecutionRun,
    DispatchExecutionRunStatus,
    DispatchUnlockTokenStore,
    create_dispatch_unlock_token,
)
from agent.coo.production_executor_confirmation import (
    REQUIRED_CONFIRMATION_PHRASE,
    create_production_executor_confirmation,
)
from agent.coo.production_executor_policy import ProductionExecutorPolicy
from agent.coo.tests.test_execution_dispatch_runtime import _approved_unlock_context
from hermes_cli.coo_dispatch import build_coo_dispatch_parser, main


def _seed_audit_record(
    *,
    audit_dir: Path,
    dispatch_run_id: str = "run-audit-cli-1",
    checklist_all_passed: bool = True,
) -> dict:
    ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
    token_store = DispatchUnlockTokenStore()
    token = create_dispatch_unlock_token(
        ticket,
        plan,
        dry_run,
        dry_run_request,
        execute_request,
        gate,
        requested_by=ticket.requester_id,
        token_store=token_store,
    )
    dispatch_request = DispatchExecutionRequest(
        dispatch_request_id="req-audit-cli",
        execute_request_id=token.execute_request_id,
        gate_id=gate.gate_id,
        ticket_id=ticket.ticket_id,
        plan_id=plan.plan_id,
        dry_run_run_id=token.dry_run_run_id,
        unlock_token_id=token.token_id,
        target_skills=list(token.target_skills),
        requested_by=ticket.requester_id,
        requested_at="2026-07-07T00:00:00+00:00",
    )
    confirmation = create_production_executor_confirmation(
        ticket_id=ticket.ticket_id,
        plan_id=plan.plan_id,
        unlock_token_id=token.token_id,
        dispatch_request_id=dispatch_request.dispatch_request_id,
        operator_id="op-audit-cli",
        operator_name="Audit CLI Operator",
        confirmation_reason="audit cli test",
        confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
    )
    run = DispatchExecutionRun(
        dispatch_run_id=dispatch_run_id,
        dispatch_request_id=dispatch_request.dispatch_request_id,
        ticket_id=ticket.ticket_id,
        plan_id=plan.plan_id,
        status=DispatchExecutionRunStatus.RUNNING,
    )
    checklist = {
        "all_passed": checklist_all_passed,
        "checks": [
            {"name": "policy_enabled", "passed": checklist_all_passed},
            {"name": "pipeline_root_allowed", "passed": checklist_all_passed},
        ],
    }
    audit = build_dispatch_execution_audit(
        dispatch_run=run,
        ticket=ticket,
        plan=plan,
        dry_run=dry_run,
        gate=gate,
        token=token,
        dispatch_request=dispatch_request,
        executor_policy=ProductionExecutorPolicy(
            enabled=True,
            allowed_pipeline_roots=("/tmp/fake-pipeline",),
        ),
        pipeline_root="/tmp/fake-pipeline",
        entrypoint="node pipeline.js",
        run_date=plan.run_date,
        pre_execution_checklist=checklist,
        requested_by=ticket.requester_id,
        operator_id=confirmation.operator_id,
        operator_name=confirmation.operator_name,
        confirmation_id=confirmation.confirmation_id,
    )
    write_dispatch_execution_audit(audit, audit_dir)
    return {
        "audit": audit,
        "confirmation": confirmation,
        "ticket": ticket,
    }


class TestDispatchAuditCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.audit_dir = self.hermes_home / "coo" / "audit"
        self.home_patch = patch(
            "agent.coo.dispatch_execution_audit.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.cli_home_patch = patch(
            "agent.coo.dispatch_cli_audit.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.home_patch.start()
        self.cli_home_patch.start()

    def tearDown(self) -> None:
        self.cli_home_patch.stop()
        self.home_patch.stop()
        self.tmp.cleanup()

    def test_summarize_existing_audit_record(self) -> None:
        seeded = _seed_audit_record(audit_dir=self.audit_dir)
        audit = seeded["audit"]
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            summary = summarize_dispatch_execution_audit(
                audit.dispatch_run_id,
                audit_dir=self.audit_dir,
            )
        output = format_dispatch_audit_summary(summary)
        self.assertEqual(summary.audit_id, audit.audit_id)
        self.assertEqual(summary.dispatch_run_id, audit.dispatch_run_id)
        self.assertTrue(summary.executor_enabled)
        self.assertTrue(summary.pipeline_root_recorded)
        self.assertEqual(summary.pre_execution_checklist, "passed")
        self.assertEqual(summary.checks_passed_count, 2)
        self.assertEqual(summary.checks_failed_count, 0)
        self.assertIn("ticket", summary.snapshot_blocks)
        self.assertIn(f"audit_id: {audit.audit_id}", output)
        self.assertIn("pre_execution_checklist: passed", output)
        self.assertNotIn("node pipeline.js", output)
        self.assertNotIn("/tmp/fake-pipeline", output)
        self.assertNotIn('"snapshot"', output)

    def test_missing_audit_rejected(self) -> None:
        with self.assertRaises(KeyError) as exc:
            summarize_dispatch_execution_audit(
                "missing-run-id",
                audit_dir=self.audit_dir,
            )
        self.assertIn("not found", str(exc.exception))

    def test_empty_dispatch_run_id_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            summarize_dispatch_execution_audit("   ", audit_dir=self.audit_dir)
        self.assertIn("required", str(exc.exception))

    def test_path_separator_dispatch_run_id_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            summarize_dispatch_execution_audit("../escape", audit_dir=self.audit_dir)
        self.assertIn("path separators", str(exc.exception))

    def test_failed_checklist_summary(self) -> None:
        seeded = _seed_audit_record(
            audit_dir=self.audit_dir,
            dispatch_run_id="run-audit-cli-failed",
            checklist_all_passed=False,
        )
        summary = summarize_dispatch_execution_audit(
            seeded["audit"].dispatch_run_id,
            audit_dir=self.audit_dir,
        )
        self.assertEqual(summary.pre_execution_checklist, "failed")
        self.assertEqual(summary.checks_failed_count, 2)

    def test_audit_show_cli_exit_zero(self) -> None:
        seeded = _seed_audit_record(audit_dir=self.audit_dir)
        audit = seeded["audit"]
        stdout = io.StringIO()
        with (
            patch.object(sys, "stdout", stdout),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = main(
                [
                    "audit",
                    "show",
                    "--dispatch-run-id",
                    audit.dispatch_run_id,
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertIn(f"dispatch_run_id: {audit.dispatch_run_id}", stdout.getvalue())

    def test_audit_show_cli_missing_exit_one(self) -> None:
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            exit_code = main(
                [
                    "audit",
                    "show",
                    "--dispatch-run-id",
                    "missing-run-id",
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("not found", stderr.getvalue())

    def test_audit_show_parser_registered(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "audit",
                "show",
                "--dispatch-run-id",
                "run-1",
            ]
        )
        self.assertEqual(args.coo_dispatch_command, "audit")
        self.assertEqual(args.coo_dispatch_audit_command, "show")
        self.assertEqual(args.dispatch_run_id, "run-1")

    def test_list_dispatch_execution_audits(self) -> None:
        first = _seed_audit_record(
            audit_dir=self.audit_dir,
            dispatch_run_id="run-audit-cli-a",
        )
        second = _seed_audit_record(
            audit_dir=self.audit_dir,
            dispatch_run_id="run-audit-cli-b",
            checklist_all_passed=False,
        )
        entries = list_dispatch_execution_audits(audit_dir=self.audit_dir)
        output = format_dispatch_audit_list(entries)
        self.assertEqual(len(entries), 2)
        self.assertIn("audit_count: 2", output)
        self.assertIn(first["audit"].dispatch_run_id, output)
        self.assertIn(second["audit"].dispatch_run_id, output)
        self.assertNotIn("/tmp/fake-pipeline", output)
        self.assertNotIn("node pipeline.js", output)

    def test_list_empty_audit_dir(self) -> None:
        entries = list_dispatch_execution_audits(audit_dir=self.audit_dir)
        output = format_dispatch_audit_list(entries)
        self.assertEqual(entries, ())
        self.assertEqual(output, "audit_count: 0")

    def test_list_corrupted_audit_rejected(self) -> None:
        _seed_audit_record(audit_dir=self.audit_dir, dispatch_run_id="run-audit-good")
        corrupt_path = self.audit_dir / "run-audit-bad.json"
        corrupt_path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            list_dispatch_execution_audits(audit_dir=self.audit_dir)
        self.assertIn("corrupted", str(exc.exception).lower())

    def test_audit_list_cli_exit_zero(self) -> None:
        seeded = _seed_audit_record(audit_dir=self.audit_dir)
        stdout = io.StringIO()
        with (
            patch.object(sys, "stdout", stdout),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = main(["audit", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("audit_count: 1", stdout.getvalue())
        self.assertIn(seeded["audit"].dispatch_run_id, stdout.getvalue())

    def test_audit_list_parser_registered(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["audit", "list"])
        self.assertEqual(args.coo_dispatch_command, "audit")
        self.assertEqual(args.coo_dispatch_audit_command, "list")

    def test_find_dispatch_execution_audits_for_ticket(self) -> None:
        seeded = _seed_audit_record(audit_dir=self.audit_dir)
        ticket_id = seeded["ticket"].ticket_id
        entries = find_dispatch_execution_audits_for_ticket(
            ticket_id,
            audit_dir=self.audit_dir,
        )
        output = format_dispatch_audit_find(ticket_id, entries)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].ticket_id, ticket_id)
        self.assertEqual(entries[0].dispatch_run_id, seeded["audit"].dispatch_run_id)
        self.assertIn(f"ticket_id: {ticket_id}", output)
        self.assertIn("match_count: 1", output)
        self.assertNotIn("/tmp/fake-pipeline", output)

    def test_find_no_matches_returns_empty(self) -> None:
        _seed_audit_record(audit_dir=self.audit_dir)
        entries = find_dispatch_execution_audits_for_ticket(
            "missing-ticket-id",
            audit_dir=self.audit_dir,
        )
        output = format_dispatch_audit_find("missing-ticket-id", entries)
        self.assertEqual(entries, ())
        self.assertIn("match_count: 0", output)

    def test_find_empty_ticket_id_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            find_dispatch_execution_audits_for_ticket("  ", audit_dir=self.audit_dir)
        self.assertIn("required", str(exc.exception))

    def test_audit_find_cli_exit_zero(self) -> None:
        seeded = _seed_audit_record(audit_dir=self.audit_dir)
        ticket_id = seeded["ticket"].ticket_id
        stdout = io.StringIO()
        with (
            patch.object(sys, "stdout", stdout),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = main(
                [
                    "audit",
                    "find",
                    "--ticket-id",
                    ticket_id,
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("match_count: 1", stdout.getvalue())
        self.assertIn(seeded["audit"].dispatch_run_id, stdout.getvalue())

    def test_audit_find_parser_registered(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "audit",
                "find",
                "--ticket-id",
                "ticket-1",
            ]
        )
        self.assertEqual(args.coo_dispatch_command, "audit")
        self.assertEqual(args.coo_dispatch_audit_command, "find")
        self.assertEqual(args.ticket_id, "ticket-1")


if __name__ == "__main__":
    unittest.main()
