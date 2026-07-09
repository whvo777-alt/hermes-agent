"""PipelineAdapter token-gated dispatch tests (Phase 10F)."""

from __future__ import annotations

import subprocess
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agent.coo.dispatch_unlock_context import dispatch_unlock_scope
from agent.coo.execution_contract import (
    SkillExecutionMode,
    SkillExecutionRequest,
    SkillExecutionStatus,
)
from agent.coo.execution_dispatch_runtime import DispatchUnlockToken
from agent.coo.execution_dispatcher import DispatchPlanStatus, ExecutionDispatchPlan
from agent.coo.execution_execute import ExecuteGate, ExecuteGateStatus
from agent.coo.execution_ticket import ExecutionTicket, ExecutionTicketStatus
from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig


def _future_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _fixtures() -> tuple[ExecutionTicket, ExecutionDispatchPlan, ExecuteGate, DispatchUnlockToken]:
    ticket = ExecutionTicket(
        ticket_id=str(uuid.uuid4()),
        approval_session_id=str(uuid.uuid4()),
        status=ExecutionTicketStatus.DISPATCH_PENDING,
        task_kind="create_content",
        run_date="2026-07-07",
        requester_id="discord-user-1",
        dispatch_generation=0,
    )
    plan = ExecutionDispatchPlan(
        plan_id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        status=DispatchPlanStatus.PLANNED,
        run_date=ticket.run_date,
        dispatchable_skills=["create_content"],
    )
    execute_request_id = str(uuid.uuid4())
    gate = ExecuteGate(
        gate_id=str(uuid.uuid4()),
        execute_request_id=execute_request_id,
        ticket_id=ticket.ticket_id,
        dry_run_run_id=str(uuid.uuid4()),
        status=ExecuteGateStatus.APPROVED,
        created_at="2026-07-07T00:00:00+00:00",
        decided_by=ticket.requester_id,
        decided_at="2026-07-07T00:00:01+00:00",
    )
    token = DispatchUnlockToken(
        token_id=str(uuid.uuid4()),
        ticket_id=ticket.ticket_id,
        plan_id=plan.plan_id,
        execute_request_id=execute_request_id,
        gate_id=gate.gate_id,
        dry_run_run_id=gate.dry_run_run_id,
        target_skills=("create_content",),
        requested_by=ticket.requester_id,
        approved_by=ticket.requester_id,
        minted_at="2026-07-07T00:00:00+00:00",
        expires_at=_future_expiry(),
        dispatch_generation=0,
    )
    return ticket, plan, gate, token


def _execute_request(
    *,
    skill_id: str = "create_content",
    assignment_id: str = "ticket-1",
) -> SkillExecutionRequest:
    return SkillExecutionRequest(
        skill_id=skill_id,
        worker_id="test_worker",
        assignment_id=assignment_id,
        run_date="2026-07-07",
        mode=SkillExecutionMode.EXECUTE,
        worker_mode="execute",
        auto_apply=False,
        review_required=True,
    )


class TestPipelineAdapterDispatch(unittest.TestCase):
    def test_allow_execute_false_with_context_raises(self) -> None:
        ticket, plan, gate, token = _fixtures()
        adapter = PipelineAdapter(PipelineAdapterConfig(allow_execute=False))
        request = _execute_request(assignment_id=ticket.ticket_id)

        with dispatch_unlock_scope(token, ticket=ticket, plan=plan, gate=gate):
            with self.assertRaises(RuntimeError) as ctx:
                adapter.dispatch(request, unlock_token_id=token.token_id)
        self.assertIn("allow_execute", str(ctx.exception))

    def test_allow_execute_true_without_token_raises(self) -> None:
        adapter = PipelineAdapter(
            PipelineAdapterConfig(allow_execute=True),
            executor=lambda *_args: (0, "", ""),
        )
        request = _execute_request()

        with self.assertRaises(RuntimeError) as ctx:
            adapter.dispatch(request)
        self.assertIn("unlock token", str(ctx.exception).lower())

    def test_allow_execute_true_without_context_raises(self) -> None:
        ticket, plan, gate, token = _fixtures()
        adapter = PipelineAdapter(
            PipelineAdapterConfig(allow_execute=True),
            executor=lambda *_args: (0, "", ""),
        )
        request = _execute_request(assignment_id=ticket.ticket_id)

        with self.assertRaises(RuntimeError) as ctx:
            adapter.dispatch(request, unlock_token_id=token.token_id)
        self.assertIn("context", str(ctx.exception).lower())

    def test_token_context_mismatch_raises(self) -> None:
        ticket, plan, gate, token = _fixtures()
        adapter = PipelineAdapter(
            PipelineAdapterConfig(allow_execute=True),
            executor=lambda *_args: (0, "", ""),
        )
        request = _execute_request(assignment_id=ticket.ticket_id)

        with dispatch_unlock_scope(token, ticket=ticket, plan=plan, gate=gate):
            with self.assertRaises(RuntimeError) as ctx:
                adapter.dispatch(request, unlock_token_id="other-token")
        self.assertIn("does not match", str(ctx.exception))

    def test_publish_content_blocked_by_contract(self) -> None:
        ticket, plan, gate, token = _fixtures()
        adapter = PipelineAdapter(
            PipelineAdapterConfig(allow_execute=True),
            executor=lambda *_args: (0, "", ""),
        )
        request = _execute_request(skill_id="publish_content", assignment_id=ticket.ticket_id)

        with dispatch_unlock_scope(token, ticket=ticket, plan=plan, gate=gate):
            result = adapter.dispatch(request, unlock_token_id=token.token_id)

        self.assertEqual(result.status, SkillExecutionStatus.BLOCKED)

    def test_approval_review_blocked_by_runtime_error(self) -> None:
        ticket, plan, gate, token = _fixtures()
        adapter = PipelineAdapter(
            PipelineAdapterConfig(allow_execute=True),
            executor=lambda *_args: (0, "", ""),
        )
        request = _execute_request(skill_id="approval_review", assignment_id=ticket.ticket_id)

        with dispatch_unlock_scope(token, ticket=ticket, plan=plan, gate=gate):
            with self.assertRaises(RuntimeError) as ctx:
                adapter.dispatch(request, unlock_token_id=token.token_id)
        self.assertIn("create_content", str(ctx.exception))

    def test_create_content_success_with_fake_executor(self) -> None:
        ticket, plan, gate, token = _fixtures()
        calls: list[tuple[str, str, str]] = []

        def fake_executor(command: str, pipeline_root: str, run_date: str) -> tuple[int, str, str]:
            calls.append((command, pipeline_root, run_date))
            return 0, "fake output", ""

        adapter = PipelineAdapter(
            PipelineAdapterConfig(
                allow_execute=True,
                pipeline_root="/tmp/fake-pipeline-root",
            ),
            executor=fake_executor,
        )
        request = _execute_request(assignment_id=ticket.ticket_id)

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch.object(PipelineAdapter, "validate_root", return_value=(True, "")),
        ):
            with dispatch_unlock_scope(
                token,
                ticket=ticket,
                plan=plan,
                gate=gate,
                pipeline_root="/tmp/fake-pipeline-root",
            ):
                result = adapter.dispatch(request, unlock_token_id=token.token_id)

        self.assertEqual(result.status, SkillExecutionStatus.COMPLETED)
        self.assertFalse(result.dry_run)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "node pipeline.js")
        self.assertEqual(calls[0][1], "/tmp/fake-pipeline-root")

    def test_fake_executor_failure_returns_failed_result(self) -> None:
        ticket, plan, gate, token = _fixtures()

        def failing_executor(*_args):
            return 1, "", "fake failure"

        adapter = PipelineAdapter(
            PipelineAdapterConfig(allow_execute=True),
            executor=failing_executor,
        )
        request = _execute_request(assignment_id=ticket.ticket_id)

        with patch.object(PipelineAdapter, "validate_root", return_value=(True, "")):
            with dispatch_unlock_scope(token, ticket=ticket, plan=plan, gate=gate):
                result = adapter.dispatch(request, unlock_token_id=token.token_id)

        self.assertEqual(result.status, SkillExecutionStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
