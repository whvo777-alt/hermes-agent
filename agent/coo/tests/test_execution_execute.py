"""Execute bridge readiness foundation tests (Phase 9B)."""

from __future__ import annotations

import inspect
import subprocess
import unittest
import uuid
from unittest.mock import patch

from agent.coo.execution_dispatcher import DispatchPlanStatus, ExecutionDispatchPlan
from agent.coo.execution_execute import (
    ExecuteGate,
    ExecuteGateStatus,
    ExecuteGateStore,
    ExecuteRequest,
    ExecuteRequestMode,
    ExecuteRequestStore,
    approve_execute_gate,
    create_execute_gate,
    create_execute_request_from_dry_run,
    get_default_execute_gate_store,
    get_default_execute_request_store,
    reject_execute_gate,
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
    )


def _manual_plan(
    ticket: ExecutionTicket,
    *,
    dispatchable_skills: list[str] | None = None,
    preview_only_skills: list[str] | None = None,
    excluded_skills: list[str] | None = None,
    status: DispatchPlanStatus = DispatchPlanStatus.PLANNED,
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
        executed=False,
        repository2_touched=False,
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
        repository2_touched=False,
    )


class TestExecuteRequestCreation(unittest.TestCase):
    def setUp(self) -> None:
        get_default_execute_request_store().clear()
        get_default_execute_gate_store().clear()

    def test_create_execute_request_from_completed_dry_run(self) -> None:
        request, run, ticket, plan = _dry_run_context()
        store = ExecuteRequestStore()

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            execute_request = create_execute_request_from_dry_run(
                run,
                request,
                plan,
                ticket,
                requested_by=ticket.requester_id,
                reason="readiness",
                request_store=store,
            )

        self.assertIsInstance(execute_request, ExecuteRequest)
        self.assertEqual(execute_request.mode, ExecuteRequestMode.READINESS_CHECK)
        self.assertEqual(execute_request.dry_run_request_id, request.request_id)
        self.assertEqual(execute_request.dry_run_run_id, run.run_id)
        self.assertEqual(execute_request.target_skills, ["create_content"])
        self.assertFalse(execute_request.auto_apply)
        self.assertTrue(execute_request.review_required)
        payload = execute_request.to_dict()
        self.assertIn("execute_request_id", payload)
        self.assertNotIn("gate_id", payload)
        self.assertNotIn("gate_status", payload)

    def test_same_dry_run_run_id_returns_same_request(self) -> None:
        request, run, ticket, plan = _dry_run_context()
        store = ExecuteRequestStore()

        first = create_execute_request_from_dry_run(
            run,
            request,
            plan,
            ticket,
            requested_by=ticket.requester_id,
            request_store=store,
        )
        second = create_execute_request_from_dry_run(
            run,
            request,
            plan,
            ticket,
            requested_by=ticket.requester_id,
            request_store=store,
        )

        self.assertIs(first, second)
        self.assertEqual(len(store.list_requests()), 1)

    def test_wrong_requester_raises(self) -> None:
        request, run, ticket, plan = _dry_run_context()
        store = ExecuteRequestStore()

        create_execute_request_from_dry_run(
            run,
            request,
            plan,
            ticket,
            requested_by=ticket.requester_id,
            request_store=store,
        )

        with self.assertRaises(ValueError) as ctx:
            create_execute_request_from_dry_run(
                run,
                request,
                plan,
                ticket,
                requested_by="other-user",
                request_store=store,
            )

        self.assertIn("not authorized", str(ctx.exception))

    def test_run_not_completed_raises(self) -> None:
        request, run, ticket, plan = _dry_run_context()
        run.status = ExecutionRunStatus.RUNNING

        with self.assertRaises(ValueError) as ctx:
            create_execute_request_from_dry_run(
                run,
                request,
                plan,
                ticket,
                requested_by=ticket.requester_id,
                request_store=ExecuteRequestStore(),
            )

        self.assertIn("running", str(ctx.exception).lower())

    def test_run_not_dry_run_raises(self) -> None:
        request, run, ticket, plan = _dry_run_context()
        run.dry_run = False

        with self.assertRaises(ValueError) as ctx:
            create_execute_request_from_dry_run(
                run,
                request,
                plan,
                ticket,
                requested_by=ticket.requester_id,
                request_store=ExecuteRequestStore(),
            )

        self.assertIn("dry_run", str(ctx.exception))

    def test_request_run_mismatch_raises(self) -> None:
        request, run, ticket, plan = _dry_run_context()
        run.request_id = "mismatched-request-id"

        with self.assertRaises(ValueError) as ctx:
            create_execute_request_from_dry_run(
                run,
                request,
                plan,
                ticket,
                requested_by=ticket.requester_id,
                request_store=ExecuteRequestStore(),
            )

        self.assertIn("does not match", str(ctx.exception))

    def test_ticket_not_dispatch_pending_raises(self) -> None:
        request, run, ticket, plan = _dry_run_context()
        ticket.status = ExecutionTicketStatus.CREATED

        with self.assertRaises(ValueError) as ctx:
            create_execute_request_from_dry_run(
                run,
                request,
                plan,
                ticket,
                requested_by=ticket.requester_id,
                request_store=ExecuteRequestStore(),
            )

        self.assertIn("created", str(ctx.exception).lower())

    def test_plan_not_planned_raises(self) -> None:
        request, run, ticket, plan = _dry_run_context()
        plan.status = DispatchPlanStatus.BLOCKED

        with self.assertRaises(ValueError) as ctx:
            create_execute_request_from_dry_run(
                run,
                request,
                plan,
                ticket,
                requested_by=ticket.requester_id,
                request_store=ExecuteRequestStore(),
            )

        self.assertIn("blocked", str(ctx.exception).lower())

    def test_failed_skill_not_target(self) -> None:
        ticket = _manual_ticket()
        plan = _manual_plan(ticket)
        request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        run = _manual_run(
            request,
            ticket,
            plan,
            dispatchable_results=[
                {
                    "skill_id": "create_content",
                    "dry_run": True,
                    "status": "failed",
                    "source": "pipeline_adapter_dry_run",
                }
            ],
        )

        execute_request = create_execute_request_from_dry_run(
            run,
            request,
            plan,
            ticket,
            requested_by=ticket.requester_id,
            request_store=ExecuteRequestStore(),
        )

        self.assertEqual(execute_request.target_skills, [])

    def test_preview_skill_not_target(self) -> None:
        ticket = _manual_ticket()
        plan = _manual_plan(ticket)
        request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        run = _manual_run(
            request,
            ticket,
            plan,
            dispatchable_results=[
                {
                    "skill_id": "approval_review",
                    "dry_run": True,
                    "status": "planned",
                    "source": "pipeline_adapter_dry_run",
                }
            ],
            preview_results=[
                {
                    "skill_id": "approval_review",
                    "dry_run": True,
                    "status": "preview_planned",
                    "source": "pipeline_adapter_dry_run",
                }
            ],
        )

        with self.assertRaises(ValueError) as ctx:
            create_execute_request_from_dry_run(
                run,
                request,
                plan,
                ticket,
                requested_by=ticket.requester_id,
                request_store=ExecuteRequestStore(),
            )

        self.assertIn("preview", str(ctx.exception).lower())

    def test_publish_content_not_target(self) -> None:
        ticket = _manual_ticket()
        plan = _manual_plan(ticket)
        request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        run = _manual_run(
            request,
            ticket,
            plan,
            dispatchable_results=[
                {
                    "skill_id": "publish_content",
                    "dry_run": True,
                    "status": "planned",
                    "source": "pipeline_adapter_dry_run",
                }
            ],
        )

        with self.assertRaises(ValueError) as ctx:
            create_execute_request_from_dry_run(
                run,
                request,
                plan,
                ticket,
                requested_by=ticket.requester_id,
                request_store=ExecuteRequestStore(),
            )

        self.assertIn("publish", str(ctx.exception).lower())

    def test_request_store_rejects_duplicate_dry_run_run_id(self) -> None:
        store = ExecuteRequestStore()
        execute_request = ExecuteRequest(
            execute_request_id="exec-req-1",
            dry_run_request_id="dry-req-1",
            dry_run_run_id="dry-run-1",
            plan_id="plan-1",
            ticket_id="ticket-1",
            approval_session_id="session-1",
            requested_by="discord-user-1",
            requested_at="2026-07-07T00:00:00+00:00",
            target_skills=["create_content"],
        )
        store.save(execute_request)

        duplicate = ExecuteRequest(
            execute_request_id="exec-req-2",
            dry_run_request_id="dry-req-1",
            dry_run_run_id="dry-run-1",
            plan_id="plan-1",
            ticket_id="ticket-1",
            approval_session_id="session-1",
            requested_by="discord-user-1",
            requested_at="2026-07-07T00:00:00+00:00",
            target_skills=["create_content"],
        )

        with self.assertRaises(ValueError):
            store.save(duplicate)


class TestExecuteGate(unittest.TestCase):
    def setUp(self) -> None:
        get_default_execute_request_store().clear()
        get_default_execute_gate_store().clear()

    def _execute_request(self) -> ExecuteRequest:
        request, run, ticket, plan = _dry_run_context()
        return create_execute_request_from_dry_run(
            run,
            request,
            plan,
            ticket,
            requested_by=ticket.requester_id,
            request_store=ExecuteRequestStore(),
        )

    def test_create_execute_gate_pending(self) -> None:
        execute_request = self._execute_request()
        store = ExecuteGateStore()

        gate = create_execute_gate(execute_request, gate_store=store)

        self.assertEqual(gate.status, ExecuteGateStatus.PENDING)
        self.assertEqual(gate.execute_request_id, execute_request.execute_request_id)
        self.assertEqual(gate.dry_run_run_id, execute_request.dry_run_run_id)
        self.assertIn("gate_id", gate.to_dict())

        stored_gate = store.get_by_request(execute_request.execute_request_id)
        self.assertIsNotNone(stored_gate)
        assert stored_gate is not None
        self.assertEqual(stored_gate.status, ExecuteGateStatus.PENDING)

        request_payload = execute_request.to_dict()
        self.assertNotIn("gate_id", request_payload)
        self.assertNotIn("gate_status", request_payload)

    def test_create_execute_gate_idempotent(self) -> None:
        execute_request = self._execute_request()
        store = ExecuteGateStore()

        first = create_execute_gate(execute_request, gate_store=store)
        second = create_execute_gate(execute_request, gate_store=store)

        self.assertIs(first, second)
        self.assertEqual(len(store.list_gates()), 1)

    def test_approve_execute_gate(self) -> None:
        execute_request = self._execute_request()
        store = ExecuteGateStore()
        gate = create_execute_gate(execute_request, gate_store=store)

        approved = approve_execute_gate(gate.gate_id, "ceo-reviewer", gate_store=store)

        self.assertEqual(approved.status, ExecuteGateStatus.APPROVED)
        self.assertEqual(approved.decided_by, "ceo-reviewer")
        self.assertTrue(approved.decided_at)

        stored_gate = store.get_by_request(execute_request.execute_request_id)
        self.assertIsNotNone(stored_gate)
        assert stored_gate is not None
        self.assertEqual(stored_gate.status, ExecuteGateStatus.APPROVED)

        request_payload = execute_request.to_dict()
        self.assertNotIn("gate_id", request_payload)
        self.assertNotIn("gate_status", request_payload)

    def test_reject_execute_gate(self) -> None:
        execute_request = self._execute_request()
        store = ExecuteGateStore()
        gate = create_execute_gate(execute_request, gate_store=store)

        rejected = reject_execute_gate(
            gate.gate_id,
            "ceo-reviewer",
            reason="not ready",
            gate_store=store,
        )

        self.assertEqual(rejected.status, ExecuteGateStatus.REJECTED)
        self.assertEqual(rejected.decided_by, "ceo-reviewer")
        self.assertEqual(rejected.reason, "not ready")

    def test_gate_decision_preserves_ticket_flags(self) -> None:
        request, run, ticket, plan = _dry_run_context()
        execute_request = create_execute_request_from_dry_run(
            run,
            request,
            plan,
            ticket,
            requested_by=ticket.requester_id,
            request_store=ExecuteRequestStore(),
        )
        store = ExecuteGateStore()
        gate = create_execute_gate(execute_request, gate_store=store)

        approve_execute_gate(gate.gate_id, ticket.requester_id, gate_store=store)

        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)
        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(plan.executed)

    def test_gate_store_rejects_duplicate_execute_request_id(self) -> None:
        store = ExecuteGateStore()
        gate = ExecuteGate(
            gate_id="gate-1",
            execute_request_id="exec-req-1",
            ticket_id="ticket-1",
            dry_run_run_id="dry-run-1",
            status=ExecuteGateStatus.PENDING,
            created_at="2026-07-07T00:00:00+00:00",
        )
        store.save(gate)

        duplicate = ExecuteGate(
            gate_id="gate-2",
            execute_request_id="exec-req-1",
            ticket_id="ticket-1",
            dry_run_run_id="dry-run-1",
            status=ExecuteGateStatus.PENDING,
            created_at="2026-07-07T00:00:00+00:00",
        )

        with self.assertRaises(ValueError):
            store.save(duplicate)


class TestExecuteSafetyInvariants(unittest.TestCase):
    def setUp(self) -> None:
        get_default_execute_request_store().clear()
        get_default_execute_gate_store().clear()

    def test_subprocess_not_called(self) -> None:
        request, run, ticket, plan = _dry_run_context()

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            execute_request = create_execute_request_from_dry_run(
                run,
                request,
                plan,
                ticket,
                requested_by=ticket.requester_id,
                request_store=ExecuteRequestStore(),
            )
            gate_store = ExecuteGateStore()
            gate = create_execute_gate(execute_request, gate_store=gate_store)
            approve_execute_gate(gate.gate_id, ticket.requester_id, gate_store=gate_store)

    def test_execution_execute_has_no_dispatch_or_execute_now(self) -> None:
        import agent.coo.execution_execute as execute_mod
        import agent.coo.execution_runtime as runtime_mod

        source = inspect.getsource(execute_mod)
        self.assertNotIn("adapter.dispatch", source)
        self.assertNotIn(".dispatch(", source)
        self.assertFalse(hasattr(execute_mod, "execute_now"))
        self.assertFalse(hasattr(execute_mod, "run_real"))
        self.assertFalse(hasattr(execute_mod, "dispatch"))

        run_mode_values = {member.value for member in ExecutionRunMode}
        self.assertNotIn("execute", run_mode_values)

        runtime_source = inspect.getsource(runtime_mod.ExecutionRunMode)
        self.assertNotIn("EXECUTE", runtime_source)


if __name__ == "__main__":
    unittest.main()
