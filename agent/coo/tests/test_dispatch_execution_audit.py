"""Tests for dispatch execution audit (Phase 10L)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_execution_audit import (
    audit_to_json,
    build_dispatch_execution_audit,
    read_dispatch_execution_audit,
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


class TestDispatchExecutionAudit(unittest.TestCase):
    def test_build_audit_includes_snapshots_and_operator(self) -> None:
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
            dispatch_request_id="req-audit",
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
            operator_id="op-42",
            operator_name="Audit Operator",
            confirmation_reason="audit test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
        )
        run = DispatchExecutionRun(
            dispatch_run_id="run-audit-1",
            dispatch_request_id=dispatch_request.dispatch_request_id,
            ticket_id=ticket.ticket_id,
            plan_id=plan.plan_id,
            status=DispatchExecutionRunStatus.RUNNING,
        )
        policy = ProductionExecutorPolicy(
            enabled=True,
            allowed_pipeline_roots=("/tmp/fake-pipeline",),
        )
        checklist = {"all_passed": True, "checks": [], "summary": ""}
        audit = build_dispatch_execution_audit(
            dispatch_run=run,
            ticket=ticket,
            plan=plan,
            dry_run=dry_run,
            gate=gate,
            token=token,
            dispatch_request=dispatch_request,
            executor_policy=policy,
            pipeline_root="/tmp/fake-pipeline",
            entrypoint="node pipeline.js",
            run_date=plan.run_date,
            pre_execution_checklist=checklist,
            requested_by=ticket.requester_id,
            operator_id=confirmation.operator_id,
            operator_name=confirmation.operator_name,
            confirmation_id=confirmation.confirmation_id,
        )
        payload = audit.to_dict()
        self.assertEqual(payload["operator_id"], "op-42")
        self.assertEqual(payload["operator_name"], "Audit Operator")
        self.assertEqual(payload["confirmation_id"], confirmation.confirmation_id)
        self.assertIn("ticket", payload["snapshot"])
        self.assertIn("plan", payload["snapshot"])
        self.assertIn("dry_run", payload["snapshot"])
        self.assertIn("gate", payload["snapshot"])
        self.assertIn("unlock_token", payload["snapshot"])
        self.assertIn("dispatch_request", payload["snapshot"])
        json.loads(audit_to_json(audit))

    def test_write_audit_under_hermes_home_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            audit_dir = hermes_home / "coo" / "audit"
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
                dispatch_request_id="req-audit-2",
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
            run = DispatchExecutionRun(
                dispatch_run_id="run-audit-2",
                dispatch_request_id=dispatch_request.dispatch_request_id,
                ticket_id=ticket.ticket_id,
                plan_id=plan.plan_id,
                status=DispatchExecutionRunStatus.RUNNING,
            )
            audit = build_dispatch_execution_audit(
                dispatch_run=run,
                ticket=ticket,
                plan=plan,
                dry_run=dry_run,
                gate=gate,
                token=token,
                dispatch_request=dispatch_request,
                executor_policy=ProductionExecutorPolicy(enabled=True),
                pipeline_root="/tmp/fake-pipeline",
                entrypoint="node pipeline.js",
                run_date=plan.run_date,
                pre_execution_checklist={"all_passed": True},
                requested_by=ticket.requester_id,
            )
            with patch("agent.coo.dispatch_execution_audit.get_hermes_home", return_value=hermes_home):
                path = write_dispatch_execution_audit(audit, audit_dir)
            self.assertTrue(str(path).startswith(str(hermes_home)))
            self.assertFalse(str(path).startswith("/opt/data/multi-content-pipeline"))
            loaded = read_dispatch_execution_audit("run-audit-2", audit_dir=audit_dir)
            assert loaded is not None
            self.assertEqual(loaded.dispatch_run_id, "run-audit-2")

    def test_write_audit_rejects_repository2_like_path_without_creating_dirs(self) -> None:
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
            dispatch_request_id="req-audit-3",
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
        run = DispatchExecutionRun(
            dispatch_run_id="run-audit-3",
            dispatch_request_id=dispatch_request.dispatch_request_id,
            ticket_id=ticket.ticket_id,
            plan_id=plan.plan_id,
            status=DispatchExecutionRunStatus.RUNNING,
        )
        audit = build_dispatch_execution_audit(
            dispatch_run=run,
            ticket=ticket,
            plan=plan,
            dry_run=dry_run,
            gate=gate,
            token=token,
            dispatch_request=dispatch_request,
            executor_policy=ProductionExecutorPolicy(enabled=True),
            pipeline_root="/tmp/fake-pipeline",
            entrypoint="node pipeline.js",
            run_date=plan.run_date,
            pre_execution_checklist={"all_passed": True},
            requested_by=ticket.requester_id,
        )
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            fake_repository2_audit_dir = Path(tmp) / "fake-multi-content-pipeline" / "outputs" / "audit"
            self.assertFalse(fake_repository2_audit_dir.exists())
            with patch(
                "agent.coo.dispatch_execution_audit.get_hermes_home",
                return_value=hermes_home,
            ):
                with self.assertRaises(ValueError):
                    write_dispatch_execution_audit(audit, fake_repository2_audit_dir)
            self.assertFalse(fake_repository2_audit_dir.exists())
            self.assertFalse((fake_repository2_audit_dir / "run-audit-3.json").exists())


if __name__ == "__main__":
    unittest.main()
