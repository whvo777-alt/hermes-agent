"""Dispatch unlock token foundation tests (Phase 10B)."""

from __future__ import annotations

import inspect
import subprocess
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionMode,
    DispatchExecutionRequestStore,
    DispatchUnlockToken,
    DispatchUnlockTokenStore,
    assert_dispatch_generation_matches,
    consume_dispatch_unlock_token,
    create_dispatch_execution_request,
    create_dispatch_unlock_token,
    get_default_dispatch_execution_request_store,
    get_default_dispatch_unlock_token_store,
    is_dispatch_unlock_token_expired,
)
from agent.coo.execution_dispatcher import DispatchPlanStatus, ExecutionDispatchPlan
from agent.coo.execution_execute import (
    ExecuteGate,
    ExecuteGateStatus,
    ExecuteGateStore,
    ExecuteRequest,
    ExecuteRequestStore,
    approve_execute_gate,
    create_execute_gate,
    create_execute_request_from_dry_run,
)
from agent.coo.execution_runtime import (
    ExecutionRequest,
    ExecutionRun,
    ExecutionRunMode,
    ExecutionRunStatus,
    ExecutionRunStore,
    create_execution_request_from_plan,
    start_dry_run,
)
from agent.coo.execution_ticket import ExecutionTicket, ExecutionTicketStatus
from agent.coo.skills_catalog import get_skill


def _manual_ticket(
    *,
    status: ExecutionTicketStatus = ExecutionTicketStatus.DISPATCH_PENDING,
    requester_id: str = "discord-user-1",
    approval_session_id: str | None = None,
    execution_dispatched: bool = False,
    publish_dispatched: bool = False,
    repository2_touched: bool = False,
) -> ExecutionTicket:
    skills = ["create_content", "approval_review", "publish_content"]
    entrypoints = [
        get_skill(skill_id).entrypoint_hint if get_skill(skill_id) else ""
        for skill_id in skills
    ]
    session_id = approval_session_id or str(uuid.uuid4())
    return ExecutionTicket(
        ticket_id=str(uuid.uuid4()),
        approval_session_id=session_id,
        status=status,
        task_kind="create_content",
        run_date="2026-07-07",
        selected_skills=skills,
        entrypoints=entrypoints,
        requester_id=requester_id,
        execution_dispatched=execution_dispatched,
        publish_dispatched=publish_dispatched,
        repository2_touched=repository2_touched,
    )


def _manual_plan(
    ticket: ExecutionTicket,
    *,
    dispatchable_skills: list[str] | None = None,
    preview_only_skills: list[str] | None = None,
    excluded_skills: list[str] | None = None,
    status: DispatchPlanStatus = DispatchPlanStatus.PLANNED,
    executed: bool = False,
    repository2_touched: bool = False,
) -> ExecutionDispatchPlan:
    dispatchable = dispatchable_skills if dispatchable_skills is not None else ["create_content"]
    preview = preview_only_skills if preview_only_skills is not None else ["approval_review"]
    excluded = excluded_skills if excluded_skills is not None else ["publish_content"]
    return ExecutionDispatchPlan(
        plan_id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        status=status,
        run_date=ticket.run_date,
        requested_by=ticket.requester_id,
        requested_at="2026-07-07T00:00:00+00:00",
        reason="manual test plan",
        dispatchable_skills=dispatchable,
        preview_only_skills=preview,
        excluded_skills=excluded,
        exclusion_reasons={"publish_content": "Publish risk skills require separate approval."},
        entrypoints_metadata=list(ticket.entrypoints),
        executed=executed,
        repository2_touched=repository2_touched,
    )


def _dry_run_context() -> tuple[ExecutionRequest, ExecutionRun, ExecutionTicket, ExecutionDispatchPlan]:
    ticket = _manual_ticket()
    plan = _manual_plan(ticket)
    request = create_execution_request_from_plan(
        plan,
        ticket,
        requested_by=ticket.requester_id,
    )
    with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
        run = start_dry_run(request, ticket, plan, run_store=ExecutionRunStore())
    return request, run, ticket, plan


def _manual_run(
    request: ExecutionRequest,
    ticket: ExecutionTicket,
    plan: ExecutionDispatchPlan,
    *,
    status: ExecutionRunStatus = ExecutionRunStatus.COMPLETED,
    dry_run: bool = True,
    dispatchable_results: list[dict] | None = None,
    preview_results: list[dict] | None = None,
    repository2_touched: bool = False,
) -> ExecutionRun:
    dispatchable = dispatchable_results
    if dispatchable is None:
        dispatchable = [
            {
                "skill_id": skill_id,
                "dry_run": True,
                "status": "planned",
                "source": "pipeline_adapter_dry_run",
            }
            for skill_id in request.dispatchable_skills
        ]
    preview = preview_results
    if preview is None:
        preview = [
            {
                "skill_id": skill_id,
                "dry_run": True,
                "status": "preview_planned",
                "source": "pipeline_adapter_dry_run",
            }
            for skill_id in request.preview_only_skills
        ]
    return ExecutionRun(
        run_id=str(uuid.uuid4()),
        request_id=request.request_id,
        plan_id=plan.plan_id,
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        status=status,
        mode=ExecutionRunMode.DRY_RUN,
        dry_run=dry_run,
        run_date=plan.run_date,
        dispatchable_results=dispatchable,
        preview_results=preview,
        blocked_skills=list(request.excluded_skills),
        finished_at="2026-07-07T00:00:01+00:00",
        repository2_touched=repository2_touched,
    )


def _manual_dry_run_request(
    plan: ExecutionDispatchPlan,
    ticket: ExecutionTicket,
) -> ExecutionRequest:
    return ExecutionRequest(
        request_id=str(uuid.uuid4()),
        plan_id=plan.plan_id,
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        requested_by=ticket.requester_id,
        requested_at="2026-07-07T00:00:00+00:00",
        reason="manual dry-run request",
        mode=ExecutionRunMode.DRY_RUN,
        dispatchable_skills=list(plan.dispatchable_skills),
        preview_only_skills=list(plan.preview_only_skills),
        excluded_skills=list(plan.excluded_skills),
    )


def _approved_unlock_context(
    *,
    requester_id: str = "discord-user-1",
    target_skills: list[str] | None = None,
) -> tuple[
    ExecutionTicket,
    ExecutionDispatchPlan,
    ExecutionRun,
    ExecutionRequest,
    ExecuteRequest,
    ExecuteGate,
]:
    ticket = _manual_ticket(requester_id=requester_id)
    plan = _manual_plan(ticket)
    dry_run_request = create_execution_request_from_plan(
        plan,
        ticket,
        requested_by=ticket.requester_id,
    )
    with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
        dry_run = start_dry_run(
            dry_run_request,
            ticket,
            plan,
            run_store=ExecutionRunStore(),
        )
    execute_request = create_execute_request_from_dry_run(
        dry_run,
        dry_run_request,
        plan,
        ticket,
        requested_by=ticket.requester_id,
        request_store=ExecuteRequestStore(),
    )
    if target_skills is not None:
        execute_request = ExecuteRequest(
            execute_request_id=execute_request.execute_request_id,
            dry_run_request_id=execute_request.dry_run_request_id,
            dry_run_run_id=execute_request.dry_run_run_id,
            plan_id=execute_request.plan_id,
            ticket_id=execute_request.ticket_id,
            approval_session_id=execute_request.approval_session_id,
            requested_by=execute_request.requested_by,
            requested_at=execute_request.requested_at,
            reason=execute_request.reason,
            mode=execute_request.mode,
            target_skills=target_skills,
            auto_apply=execute_request.auto_apply,
            review_required=execute_request.review_required,
        )
    gate_store = ExecuteGateStore()
    gate = create_execute_gate(execute_request, gate_store=gate_store)
    approved_gate = approve_execute_gate(
        gate.gate_id,
        ticket.requester_id,
        gate_store=gate_store,
    )
    return ticket, plan, dry_run, dry_run_request, execute_request, approved_gate


class TestDispatchUnlockTokenFoundation(unittest.TestCase):
    def setUp(self) -> None:
        get_default_dispatch_unlock_token_store().clear()
        get_default_dispatch_execution_request_store().clear()

    def test_happy_path_token_minted(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        token_store = DispatchUnlockTokenStore()

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
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

        self.assertIsInstance(token, DispatchUnlockToken)
        self.assertEqual(token.ticket_id, ticket.ticket_id)
        self.assertEqual(token.plan_id, plan.plan_id)
        self.assertEqual(token.execute_request_id, execute_request.execute_request_id)
        self.assertEqual(token.gate_id, gate.gate_id)
        self.assertEqual(token.dry_run_run_id, dry_run.run_id)
        self.assertEqual(token.target_skills, ("create_content",))
        self.assertEqual(token.requested_by, ticket.requester_id)
        self.assertEqual(token.approved_by, ticket.requester_id)
        self.assertFalse(token.consumed)
        self.assertTrue(token.expires_at)

    def test_token_contains_dispatch_generation(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        token = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            dispatch_generation=3,
            token_store=DispatchUnlockTokenStore(),
        )
        self.assertEqual(token.dispatch_generation, 3)

    def test_idempotent_same_execute_request_returns_same_token(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        token_store = DispatchUnlockTokenStore()

        first = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )
        second = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )

        self.assertEqual(first.token_id, second.token_id)

    def test_gate_not_approved_raises(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        pending_gate = ExecuteGate(
            gate_id=gate.gate_id,
            execute_request_id=gate.execute_request_id,
            ticket_id=gate.ticket_id,
            dry_run_run_id=gate.dry_run_run_id,
            status=ExecuteGateStatus.PENDING,
            created_at=gate.created_at,
            decided_by="",
            decided_at="",
        )

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                pending_gate,
                requested_by=ticket.requester_id,
                token_store=DispatchUnlockTokenStore(),
            )
        self.assertIn("pending", str(ctx.exception))

    def test_wrong_requester_raises(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by="other-user",
                token_store=DispatchUnlockTokenStore(),
            )
        self.assertIn("not authorized", str(ctx.exception))

    def test_gate_decided_by_not_owner_raises(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        wrong_owner_gate = ExecuteGate(
            gate_id=gate.gate_id,
            execute_request_id=gate.execute_request_id,
            ticket_id=gate.ticket_id,
            dry_run_run_id=gate.dry_run_run_id,
            status=ExecuteGateStatus.APPROVED,
            created_at=gate.created_at,
            decided_by="other-user",
            decided_at="2026-07-07T00:00:00+00:00",
        )

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                wrong_owner_gate,
                requested_by=ticket.requester_id,
                token_store=DispatchUnlockTokenStore(),
            )
        self.assertIn("decided_by", str(ctx.exception))

    def test_empty_target_skills_raises(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context(
            target_skills=[],
        )

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by=ticket.requester_id,
                token_store=DispatchUnlockTokenStore(),
            )
        self.assertIn("empty target_skills", str(ctx.exception))

    def test_publish_content_target_raises(self) -> None:
        ticket = _manual_ticket()
        plan = _manual_plan(ticket)
        dry_run_request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        dry_run = _manual_run(dry_run_request, ticket, plan)
        execute_request = ExecuteRequest(
            execute_request_id=str(uuid.uuid4()),
            dry_run_request_id=dry_run_request.request_id,
            dry_run_run_id=dry_run.run_id,
            plan_id=plan.plan_id,
            ticket_id=ticket.ticket_id,
            approval_session_id=ticket.approval_session_id,
            requested_by=ticket.requester_id,
            requested_at="2026-07-07T00:00:00+00:00",
            target_skills=["publish_content"],
        )
        gate_store = ExecuteGateStore()
        gate = approve_execute_gate(
            create_execute_gate(execute_request, gate_store=gate_store).gate_id,
            ticket.requester_id,
            gate_store=gate_store,
        )

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by=ticket.requester_id,
                token_store=DispatchUnlockTokenStore(),
            )
        self.assertIn("publish_content", str(ctx.exception))

    def test_approval_review_read_only_target_raises(self) -> None:
        ticket = _manual_ticket()
        plan = _manual_plan(
            ticket,
            dispatchable_skills=["create_content", "approval_review"],
            preview_only_skills=[],
        )
        dry_run_request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        dry_run = _manual_run(dry_run_request, ticket, plan)
        execute_request = ExecuteRequest(
            execute_request_id=str(uuid.uuid4()),
            dry_run_request_id=dry_run_request.request_id,
            dry_run_run_id=dry_run.run_id,
            plan_id=plan.plan_id,
            ticket_id=ticket.ticket_id,
            approval_session_id=ticket.approval_session_id,
            requested_by=ticket.requester_id,
            requested_at="2026-07-07T00:00:00+00:00",
            target_skills=["approval_review"],
        )
        gate_store = ExecuteGateStore()
        gate = approve_execute_gate(
            create_execute_gate(execute_request, gate_store=gate_store).gate_id,
            ticket.requester_id,
            gate_store=gate_store,
        )

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by=ticket.requester_id,
                token_store=DispatchUnlockTokenStore(),
            )
        self.assertIn("Read-only skill", str(ctx.exception))

    def test_dry_run_not_completed_raises(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        running_run = _manual_run(
            dry_run_request,
            ticket,
            plan,
            status=ExecutionRunStatus.RUNNING,
        )

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                running_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by=ticket.requester_id,
                token_store=DispatchUnlockTokenStore(),
            )
        self.assertIn("running", str(ctx.exception))

    def test_dry_run_repository2_touched_raises(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        touched_run = _manual_run(
            dry_run_request,
            ticket,
            plan,
            repository2_touched=True,
        )

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                touched_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by=ticket.requester_id,
                token_store=DispatchUnlockTokenStore(),
            )
        self.assertIn("repository2_touched", str(ctx.exception))

    def test_ticket_execution_dispatched_raises(self) -> None:
        ticket = _manual_ticket(execution_dispatched=True)
        plan = _manual_plan(ticket)
        dry_run_request = _manual_dry_run_request(plan, ticket)
        dry_run = _manual_run(dry_run_request, ticket, plan)
        execute_request = ExecuteRequest(
            execute_request_id=str(uuid.uuid4()),
            dry_run_request_id=dry_run_request.request_id,
            dry_run_run_id=dry_run.run_id,
            plan_id=plan.plan_id,
            ticket_id=ticket.ticket_id,
            approval_session_id=ticket.approval_session_id,
            requested_by=ticket.requester_id,
            requested_at="2026-07-07T00:00:00+00:00",
            target_skills=["create_content"],
        )
        gate_store = ExecuteGateStore()
        gate = approve_execute_gate(
            create_execute_gate(execute_request, gate_store=gate_store).gate_id,
            ticket.requester_id,
            gate_store=gate_store,
        )

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by=ticket.requester_id,
                token_store=DispatchUnlockTokenStore(),
            )
        self.assertIn("execution_dispatched", str(ctx.exception))

    def test_plan_executed_raises(self) -> None:
        ticket = _manual_ticket()
        plan = _manual_plan(ticket, executed=True)
        dry_run_request = _manual_dry_run_request(plan, ticket)
        dry_run = _manual_run(dry_run_request, ticket, plan)
        execute_request = ExecuteRequest(
            execute_request_id=str(uuid.uuid4()),
            dry_run_request_id=dry_run_request.request_id,
            dry_run_run_id=dry_run.run_id,
            plan_id=plan.plan_id,
            ticket_id=ticket.ticket_id,
            approval_session_id=ticket.approval_session_id,
            requested_by=ticket.requester_id,
            requested_at="2026-07-07T00:00:00+00:00",
            target_skills=["create_content"],
        )
        gate_store = ExecuteGateStore()
        gate = approve_execute_gate(
            create_execute_gate(execute_request, gate_store=gate_store).gate_id,
            ticket.requester_id,
            gate_store=gate_store,
        )

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by=ticket.requester_id,
                token_store=DispatchUnlockTokenStore(),
            )
        self.assertIn("executed must remain false", str(ctx.exception))

    def test_create_dispatch_execution_request_happy_path(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        token = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=DispatchUnlockTokenStore(),
        )
        request_store = DispatchExecutionRequestStore()

        dispatch_request = create_dispatch_execution_request(
            token,
            reason="ready for future dispatch",
            request_store=request_store,
        )

        self.assertEqual(dispatch_request.mode, DispatchExecutionMode.EXECUTE)
        self.assertEqual(dispatch_request.execute_request_id, execute_request.execute_request_id)
        self.assertEqual(dispatch_request.unlock_token_id, token.token_id)
        self.assertEqual(dispatch_request.target_skills, ["create_content"])
        self.assertEqual(dispatch_request.reason, "ready for future dispatch")

    def test_consumed_token_blocks_dispatch_request(self) -> None:
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
        consume_dispatch_unlock_token(token.token_id, token_store=token_store)

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_execution_request(token, request_store=DispatchExecutionRequestStore())
        self.assertIn("consumed", str(ctx.exception))

    def test_expired_token_blocks_dispatch_request(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        token = DispatchUnlockToken(
            token_id=str(uuid.uuid4()),
            ticket_id=ticket.ticket_id,
            plan_id=plan.plan_id,
            execute_request_id=execute_request.execute_request_id,
            gate_id=gate.gate_id,
            dry_run_run_id=dry_run.run_id,
            target_skills=("create_content",),
            requested_by=ticket.requester_id,
            approved_by=ticket.requester_id,
            minted_at=past,
            expires_at=past,
            consumed=False,
            dispatch_generation=0,
        )
        self.assertTrue(is_dispatch_unlock_token_expired(token))

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_execution_request(token, request_store=DispatchExecutionRequestStore())
        self.assertIn("expired", str(ctx.exception))

    def test_consume_token_once_ok_second_raises(self) -> None:
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

        consumed = consume_dispatch_unlock_token(token.token_id, token_store=token_store)
        self.assertTrue(consumed.consumed)

        with self.assertRaises(ValueError) as ctx:
            consume_dispatch_unlock_token(token.token_id, token_store=token_store)
        self.assertIn("consumed", str(ctx.exception))

    def test_ticket_and_plan_flags_remain_unchanged(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        ticket_before = (
            ticket.status,
            ticket.execution_dispatched,
            ticket.publish_dispatched,
            ticket.repository2_touched,
        )
        plan_before = (plan.status, plan.executed, plan.repository2_touched)

        token = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=DispatchUnlockTokenStore(),
        )
        create_dispatch_execution_request(
            token,
            request_store=DispatchExecutionRequestStore(),
        )

        ticket_after = (
            ticket.status,
            ticket.execution_dispatched,
            ticket.publish_dispatched,
            ticket.repository2_touched,
        )
        plan_after = (plan.status, plan.executed, plan.repository2_touched)

        self.assertEqual(ticket_before, ticket_after)
        self.assertEqual(plan_before, plan_after)
        self.assertIs(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)

    def test_subprocess_run_not_called(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            token = create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by=ticket.requester_id,
                token_store=DispatchUnlockTokenStore(),
            )
            create_dispatch_execution_request(
                token,
                request_store=DispatchExecutionRequestStore(),
            )

    def test_module_has_no_dispatch_calls(self) -> None:
        import agent.coo.execution_dispatch_runtime as module

        source = inspect.getsource(module)
        self.assertNotIn("adapter.dispatch", source)
        self.assertNotIn("dispatch_now", source)
        self.assertNotIn("run_real", source)
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("subprocess.run", source)

    def test_execution_run_mode_has_no_execute(self) -> None:
        members = {member.value for member in ExecutionRunMode}
        self.assertNotIn("execute", members)
        self.assertEqual(members, {"dry_run"})


class TestDispatchUnlockTokenRemint(unittest.TestCase):
    def setUp(self) -> None:
        get_default_dispatch_unlock_token_store().clear()

    def test_expired_unconsumed_token_remint_creates_new_token(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        token_store = DispatchUnlockTokenStore()
        first = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )
        first.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        token_store.save(first)

        second = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )

        self.assertNotEqual(first.token_id, second.token_id)
        old = token_store.get(first.token_id)
        self.assertIsNotNone(old)
        assert old is not None
        self.assertTrue(old.superseded)
        self.assertEqual(old.superseded_by, second.token_id)
        self.assertEqual(old.invalid_reason, "expired_remint")
        self.assertFalse(second.superseded)

    def test_remint_validation_failure_leaves_old_token_untouched(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        token_store = DispatchUnlockTokenStore()
        first = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )
        first.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        token_store.save(first)

        # Make post-remint validation fail so the attempt never reaches the
        # point where a new token is minted and saved.
        plan.executed = True

        with self.assertRaises(ValueError):
            create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by=ticket.requester_id,
                token_store=token_store,
            )

        old = token_store.get(first.token_id)
        self.assertIsNotNone(old)
        assert old is not None
        self.assertFalse(old.superseded)
        self.assertEqual(old.superseded_by, "")
        self.assertEqual(old.invalid_reason, "")

    def test_list_by_execute_request_returns_history(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        token_store = DispatchUnlockTokenStore()
        first = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )
        first.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        token_store.save(first)
        second = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )

        history = token_store.list_by_execute_request(execute_request.execute_request_id)
        self.assertEqual(len(history), 2)
        self.assertEqual({item.token_id for item in history}, {first.token_id, second.token_id})

    def test_consumed_token_remint_raises(self) -> None:
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
        consume_dispatch_unlock_token(token.token_id, token_store=token_store)

        with self.assertRaises(ValueError) as ctx:
            create_dispatch_unlock_token(
                ticket,
                plan,
                dry_run,
                dry_run_request,
                execute_request,
                gate,
                requested_by=ticket.requester_id,
                token_store=token_store,
            )
        self.assertIn("consumed", str(ctx.exception))

    def test_usable_token_returns_same_token(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        token_store = DispatchUnlockTokenStore()
        first = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )
        second = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )
        self.assertEqual(first.token_id, second.token_id)

    def test_generation_mismatch_raises(self) -> None:
        ticket = _manual_ticket()
        token = DispatchUnlockToken(
            token_id=str(uuid.uuid4()),
            ticket_id=ticket.ticket_id,
            plan_id=str(uuid.uuid4()),
            execute_request_id=str(uuid.uuid4()),
            gate_id=str(uuid.uuid4()),
            dry_run_run_id=str(uuid.uuid4()),
            target_skills=("create_content",),
            requested_by=ticket.requester_id,
            approved_by=ticket.requester_id,
            minted_at="2026-07-07T00:00:00+00:00",
            expires_at="2026-07-08T00:00:00+00:00",
            dispatch_generation=0,
        )
        ticket.dispatch_generation = 1

        with self.assertRaises(ValueError) as ctx:
            assert_dispatch_generation_matches(token, ticket)
        self.assertIn("generation", str(ctx.exception))

    def test_generation_match_passes(self) -> None:
        ticket = _manual_ticket()
        ticket.dispatch_generation = 2
        token = DispatchUnlockToken(
            token_id=str(uuid.uuid4()),
            ticket_id=ticket.ticket_id,
            plan_id=str(uuid.uuid4()),
            execute_request_id=str(uuid.uuid4()),
            gate_id=str(uuid.uuid4()),
            dry_run_run_id=str(uuid.uuid4()),
            target_skills=("create_content",),
            requested_by=ticket.requester_id,
            approved_by=ticket.requester_id,
            minted_at="2026-07-07T00:00:00+00:00",
            expires_at="2026-07-08T00:00:00+00:00",
            dispatch_generation=2,
        )
        assert_dispatch_generation_matches(token, ticket)


if __name__ == "__main__":
    unittest.main()
