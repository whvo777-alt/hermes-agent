"""Gateway execute bridge tests (Phase 9C)."""

from __future__ import annotations

import inspect
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent.coo.execution_dispatcher import (
    DispatchPlanStatus,
    ExecutionDispatchPlan,
    ExecutionDispatchPlanStore,
    get_default_dispatch_plan_store,
)
from agent.coo.execution_execute import (
    ExecuteGateStatus,
    ExecuteGateStore,
    ExecuteRequestStore,
    get_default_execute_gate_store,
    get_default_execute_request_store,
)
from agent.coo.execution_runtime import (
    ExecutionRequest,
    ExecutionRequestStore,
    ExecutionRun,
    ExecutionRunMode,
    ExecutionRunStatus,
    ExecutionRunStore,
    create_execution_request_from_plan,
    get_default_execution_request_store,
    get_default_execution_run_store,
    start_dry_run,
)
from agent.coo.execution_ticket import (
    ExecutionTicket,
    ExecutionTicketStatus,
    ExecutionTicketStore,
    get_default_ticket_store,
)
from agent.coo.gateway_execution_execute import (
    approve_execute_gate_for_gateway_request,
    create_execute_request_for_gateway_session,
    create_execute_request_for_gateway_ticket,
    get_latest_execute_request_for_gateway_ticket,
    reject_execute_gate_for_gateway_request,
)
from agent.coo.gateway_execution_runtime import start_dry_run_for_gateway_ticket
from agent.coo.skills_catalog import get_skill


def _manual_ticket(
    *,
    status: ExecutionTicketStatus = ExecutionTicketStatus.DISPATCH_PENDING,
    selected_skills: list[str] | None = None,
    requester_id: str = "discord-user-1",
    approval_session_id: str | None = None,
) -> ExecutionTicket:
    skills = selected_skills or ["create_content", "approval_review", "publish_content"]
    entrypoints = [
        get_skill(skill_id).entrypoint_hint if get_skill(skill_id) else ""
        for skill_id in skills
    ]
    return ExecutionTicket(
        ticket_id=str(uuid.uuid4()),
        approval_session_id=approval_session_id or str(uuid.uuid4()),
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
    status: DispatchPlanStatus = DispatchPlanStatus.PLANNED,
    dispatchable_skills: list[str] | None = None,
    preview_only_skills: list[str] | None = None,
    excluded_skills: list[str] | None = None,
) -> ExecutionDispatchPlan:
    return ExecutionDispatchPlan(
        plan_id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        ticket_id=ticket.ticket_id,
        approval_session_id=ticket.approval_session_id,
        status=status,
        run_date=ticket.run_date,
        requested_by=ticket.requester_id,
        requested_at="2026-07-07T00:00:00+00:00",
        reason="manual gateway execute plan",
        dispatchable_skills=dispatchable_skills
        if dispatchable_skills is not None
        else ["create_content"],
        preview_only_skills=preview_only_skills
        if preview_only_skills is not None
        else ["approval_review"],
        excluded_skills=excluded_skills if excluded_skills is not None else ["publish_content"],
        exclusion_reasons={"publish_content": "Publish risk skills require separate approval."},
        entrypoints_metadata=list(ticket.entrypoints),
        executed=False,
        repository2_touched=False,
    )


def _seed_ticket_and_plan(
    *,
    ticket_store: ExecutionTicketStore,
    plan_store: ExecutionDispatchPlanStore,
    dispatchable_skills: list[str] | None = None,
) -> tuple[ExecutionTicket, ExecutionDispatchPlan]:
    ticket = _manual_ticket()
    plan = _manual_plan(ticket, dispatchable_skills=dispatchable_skills)
    ticket_store.save(ticket)
    plan_store.save(plan)
    return ticket, plan


def _start_gateway_dry_run(
    ticket: ExecutionTicket,
    *,
    ticket_store: ExecutionTicketStore,
    plan_store: ExecutionDispatchPlanStore,
    run_store: ExecutionRunStore,
    request_store: ExecutionRequestStore,
) -> dict:
    with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
        return start_dry_run_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=request_store,
        )


def _manual_run(
    request: ExecutionRequest,
    ticket: ExecutionTicket,
    plan: ExecutionDispatchPlan,
    *,
    status: ExecutionRunStatus = ExecutionRunStatus.COMPLETED,
    dry_run: bool = True,
    started_at: str = "2026-07-07T00:00:00+00:00",
    dispatchable_results: list[dict] | None = None,
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
        started_at=started_at,
        finished_at="2026-07-07T00:00:01+00:00",
        dispatchable_results=dispatchable,
        preview_results=[],
        blocked_skills=list(request.excluded_skills),
        repository2_touched=False,
    )


class TestGatewayExecuteBridge(unittest.TestCase):
    def setUp(self) -> None:
        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()
        get_default_execution_run_store().clear()
        get_default_execution_request_store().clear()
        get_default_execute_request_store().clear()
        get_default_execute_gate_store().clear()

    def test_happy_path_creates_execute_request_and_pending_gate(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        execute_request_store = ExecuteRequestStore()
        gate_store = ExecuteGateStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
        _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )

        result = create_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            reason="readiness",
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        self.assertIn("execute_request_id", result["execute_request"])
        self.assertEqual(result["execute_request"]["target_skills"], ["create_content"])
        self.assertEqual(result["gate"]["status"], ExecuteGateStatus.PENDING.value)
        stored_gate = gate_store.get_by_request(result["execute_request"]["execute_request_id"])
        self.assertIsNotNone(stored_gate)
        assert stored_gate is not None
        self.assertEqual(stored_gate.status, ExecuteGateStatus.PENDING)

    def test_session_path_delegates_to_ticket_bridge(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        execute_request_store = ExecuteRequestStore()
        gate_store = ExecuteGateStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
        _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )

        result = create_execute_request_for_gateway_session(
            ticket.approval_session_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        self.assertEqual(result["gate"]["status"], ExecuteGateStatus.PENDING.value)
        self.assertEqual(
            result["execute_request"]["approval_session_id"],
            ticket.approval_session_id,
        )

    def test_missing_dry_run_raises_key_error(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        with self.assertRaises(KeyError) as ctx:
            create_execute_request_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=ExecutionRunStore(),
                dry_run_request_store=ExecutionRequestStore(),
                execute_request_store=ExecuteRequestStore(),
                gate_store=ExecuteGateStore(),
            )

        self.assertIn("dry-run", str(ctx.exception).lower())

    def test_wrong_requester_raises_value_error(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
        _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )

        with self.assertRaises(ValueError) as ctx:
            create_execute_request_for_gateway_ticket(
                ticket.ticket_id,
                requester_id="other-user",
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=run_store,
                dry_run_request_store=dry_run_request_store,
                execute_request_store=ExecuteRequestStore(),
                gate_store=ExecuteGateStore(),
            )

        self.assertIn("not authorized", str(ctx.exception))

    def test_latest_completed_dry_run_selected(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        execute_request_store = ExecuteRequestStore()
        gate_store = ExecuteGateStore()
        ticket, plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        older_request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        older_run = _manual_run(
            older_request,
            ticket,
            plan,
            started_at="2026-07-07T00:00:00+00:00",
        )
        newer_request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        newer_run = _manual_run(
            newer_request,
            ticket,
            plan,
            started_at="2026-07-07T00:00:02+00:00",
        )
        dry_run_request_store.save(older_request)
        dry_run_request_store.save(newer_request)
        run_store.save(older_run)
        run_store.save(newer_run)

        result = create_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        self.assertEqual(result["execute_request"]["dry_run_run_id"], newer_run.run_id)

    def test_running_and_failed_dry_runs_excluded(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        execute_request_store = ExecuteRequestStore()
        gate_store = ExecuteGateStore()
        ticket, plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        running_request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        failed_request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        completed_request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        running_run = _manual_run(
            running_request,
            ticket,
            plan,
            status=ExecutionRunStatus.RUNNING,
            started_at="2026-07-07T00:00:03+00:00",
        )
        failed_run = _manual_run(
            failed_request,
            ticket,
            plan,
            status=ExecutionRunStatus.FAILED,
            started_at="2026-07-07T00:00:04+00:00",
        )
        completed_run = _manual_run(
            completed_request,
            ticket,
            plan,
            started_at="2026-07-07T00:00:01+00:00",
        )
        for request in (running_request, failed_request, completed_request):
            dry_run_request_store.save(request)
        for run in (running_run, failed_run, completed_run):
            run_store.save(run)

        result = create_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        self.assertEqual(result["execute_request"]["dry_run_run_id"], completed_run.run_id)

    def test_empty_target_skills_raises_value_error(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        ticket, plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
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
        dry_run_request_store.save(request)
        run_store.save(run)

        with self.assertRaises(ValueError) as ctx:
            create_execute_request_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=run_store,
                dry_run_request_store=dry_run_request_store,
                execute_request_store=ExecuteRequestStore(),
                gate_store=ExecuteGateStore(),
            )

        self.assertIn("target skills", str(ctx.exception).lower())

    def test_repeated_create_is_idempotent(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        execute_request_store = ExecuteRequestStore()
        gate_store = ExecuteGateStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
        _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )

        first = create_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )
        second = create_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        self.assertEqual(
            first["execute_request"]["execute_request_id"],
            second["execute_request"]["execute_request_id"],
        )
        self.assertEqual(first["gate"]["gate_id"], second["gate"]["gate_id"])
        self.assertEqual(len(execute_request_store.list_requests()), 1)
        self.assertEqual(len(gate_store.list_gates()), 1)

    def test_get_latest_execute_request_includes_gate_when_present(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        execute_request_store = ExecuteRequestStore()
        gate_store = ExecuteGateStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
        _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )
        created = create_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        latest = get_latest_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        assert latest is not None
        self.assertEqual(
            latest["execute_request"]["execute_request_id"],
            created["execute_request"]["execute_request_id"],
        )
        self.assertEqual(latest["gate"]["gate_id"], created["gate"]["gate_id"])

    def test_get_latest_returns_none_when_missing(self) -> None:
        self.assertIsNone(
            get_latest_execute_request_for_gateway_ticket(
                str(uuid.uuid4()),
                execute_request_store=ExecuteRequestStore(),
                gate_store=ExecuteGateStore(),
            )
        )

    def test_approve_gate_sets_approved_without_execution(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        execute_request_store = ExecuteRequestStore()
        gate_store = ExecuteGateStore()
        ticket, plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
        _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )
        created = create_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = approve_execute_gate_for_gateway_request(
                created["execute_request"]["execute_request_id"],
                reviewer_id=ticket.requester_id,
                gate_store=gate_store,
                ticket_store=ticket_store,
            )

        self.assertEqual(result["gate"]["status"], ExecuteGateStatus.APPROVED.value)
        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)
        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(plan.executed)

    def test_reject_gate_sets_rejected_without_execution(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        execute_request_store = ExecuteRequestStore()
        gate_store = ExecuteGateStore()
        ticket, plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
        _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )
        created = create_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = reject_execute_gate_for_gateway_request(
                created["execute_request"]["execute_request_id"],
                reviewer_id=ticket.requester_id,
                reason="not ready",
                gate_store=gate_store,
                ticket_store=ticket_store,
            )

        self.assertEqual(result["gate"]["status"], ExecuteGateStatus.REJECTED.value)
        self.assertEqual(result["gate"]["reason"], "not ready")
        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)
        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(plan.executed)

    def test_approve_gate_wrong_reviewer_raises_value_error(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        execute_request_store = ExecuteRequestStore()
        gate_store = ExecuteGateStore()
        ticket, plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
        _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )
        created = create_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with self.assertRaises(ValueError) as ctx:
                approve_execute_gate_for_gateway_request(
                    created["execute_request"]["execute_request_id"],
                    reviewer_id="total-stranger-999",
                    gate_store=gate_store,
                    ticket_store=ticket_store,
                )

        self.assertIn("not authorized", str(ctx.exception))
        stored_gate = gate_store.get_by_request(created["execute_request"]["execute_request_id"])
        assert stored_gate is not None
        self.assertEqual(stored_gate.status, ExecuteGateStatus.PENDING)
        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)
        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(plan.executed)

    def test_reject_gate_wrong_reviewer_raises_value_error(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        dry_run_request_store = ExecutionRequestStore()
        execute_request_store = ExecuteRequestStore()
        gate_store = ExecuteGateStore()
        ticket, plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
        _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )
        created = create_execute_request_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            dry_run_request_store=dry_run_request_store,
            execute_request_store=execute_request_store,
            gate_store=gate_store,
        )

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with self.assertRaises(ValueError) as ctx:
                reject_execute_gate_for_gateway_request(
                    created["execute_request"]["execute_request_id"],
                    reviewer_id="total-stranger-999",
                    reason="not my call",
                    gate_store=gate_store,
                    ticket_store=ticket_store,
                )

        self.assertIn("not authorized", str(ctx.exception))
        stored_gate = gate_store.get_by_request(created["execute_request"]["execute_request_id"])
        assert stored_gate is not None
        self.assertEqual(stored_gate.status, ExecuteGateStatus.PENDING)
        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)
        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(plan.executed)

    def test_gateway_execute_surface_has_no_forbidden_functions(self) -> None:
        import agent.coo.gateway_execution_execute as gateway_execute_mod

        source = inspect.getsource(gateway_execute_mod)
        self.assertNotIn("adapter.dispatch", source)
        self.assertNotIn(".dispatch(", source)
        self.assertFalse(hasattr(gateway_execute_mod, "execute_now"))
        self.assertFalse(hasattr(gateway_execute_mod, "run_real"))
        self.assertFalse(hasattr(gateway_execute_mod, "dispatch"))

    def test_discord_lazy_imports_gateway_execution_execute_only(self) -> None:
        discord_path = (
            Path(__file__).resolve().parents[3]
            / "plugins/platforms/discord/coo_approval.py"
        )
        source = discord_path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith("from agent.coo.gateway_execution_execute import"):
                self.fail(
                    "gateway_execution_execute must be imported lazily inside functions"
                )
            if line.startswith("import agent.coo.gateway_execution_execute"):
                self.fail(
                    "gateway_execution_execute must be imported lazily inside functions"
                )


if __name__ == "__main__":
    unittest.main()
