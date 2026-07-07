"""Execution runtime dry-run foundation tests (Phase 8B)."""

from __future__ import annotations

import inspect
import subprocess
import unittest
import uuid
from unittest.mock import patch

from agent.coo.approval_report import build_approval_report
from agent.coo.approval_session import (
    CEOApprovalSessionStore,
    approve_session,
    create_approval_session,
)
from agent.coo.execution_dispatcher import (
    DispatchPlanStatus,
    ExecutionDispatchPlan,
    create_dispatch_plan_from_ticket,
)
from agent.coo.execution_runtime import (
    ExecutionRequest,
    ExecutionRun,
    ExecutionRunMode,
    ExecutionRunStatus,
    ExecutionRunStore,
    create_execution_request_from_plan,
    get_default_execution_run_store,
    start_dry_run,
)
from agent.coo.execution_ticket import (
    ExecutionTicket,
    ExecutionTicketStatus,
    ExecutionTicketStore,
    create_ticket_from_approval_session,
)
from agent.coo.orchestrator import COOOrchestrator
from agent.coo.skills_catalog import get_skill


def _ticket_and_plan_from_orchestrate(
    *,
    session_store: CEOApprovalSessionStore,
    ticket_store: ExecutionTicketStore,
    message: str = "오늘 블로그 글 작성해서 보고해",
    run_date: str = "2026-07-07",
    requester_id: str = "discord-user-1",
) -> tuple[ExecutionTicket, ExecutionDispatchPlan]:
    with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
        orchestrated = COOOrchestrator().orchestrate(message, run_date=run_date)
    report = build_approval_report(orchestrated)
    session = create_approval_session(
        report,
        orchestrated,
        requester_id=requester_id,
        channel_id="discord-chan-1",
        store=session_store,
    )
    approved = approve_session(
        session.session_id,
        reviewer=requester_id,
        requester_id=requester_id,
        store=session_store,
    )
    ticket = create_ticket_from_approval_session(approved, store=ticket_store)
    plan = create_dispatch_plan_from_ticket(
        ticket,
        requested_by=requester_id,
        reason="test plan",
    )
    return ticket, plan


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


class TestExecutionRuntimeRequest(unittest.TestCase):
    def setUp(self) -> None:
        get_default_execution_run_store().clear()

    def test_create_execution_request_from_plan_success(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        ticket, plan = _ticket_and_plan_from_orchestrate(
            session_store=session_store,
            ticket_store=ticket_store,
        )

        request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by="discord-user-1",
            reason="dry-run request",
        )

        self.assertIsInstance(request, ExecutionRequest)
        self.assertTrue(request.request_id)
        self.assertEqual(request.plan_id, plan.plan_id)
        self.assertEqual(request.ticket_id, ticket.ticket_id)
        self.assertEqual(request.approval_session_id, ticket.approval_session_id)
        self.assertEqual(request.requested_by, "discord-user-1")
        self.assertEqual(request.reason, "dry-run request")
        self.assertEqual(request.mode, ExecutionRunMode.DRY_RUN)
        self.assertEqual(request.dispatchable_skills, list(plan.dispatchable_skills))
        self.assertEqual(request.preview_only_skills, list(plan.preview_only_skills))
        self.assertEqual(request.excluded_skills, list(plan.excluded_skills))
        self.assertFalse(request.auto_apply)
        self.assertTrue(request.review_required)
        self.assertIn("request_id", request.to_dict())

    def test_create_request_rejects_wrong_requester(self) -> None:
        ticket = _manual_ticket()
        plan = _manual_plan(ticket)

        with self.assertRaises(ValueError) as ctx:
            create_execution_request_from_plan(plan, ticket, requested_by="other-user")

        self.assertIn("not authorized", str(ctx.exception))

    def test_create_request_rejects_ticket_not_dispatch_pending(self) -> None:
        ticket = _manual_ticket(status=ExecutionTicketStatus.CREATED)
        plan = _manual_plan(ticket)

        with self.assertRaises(ValueError) as ctx:
            create_execution_request_from_plan(plan, ticket, requested_by=ticket.requester_id)

        self.assertIn("created", str(ctx.exception).lower())

    def test_create_request_rejects_plan_not_planned(self) -> None:
        ticket = _manual_ticket()
        plan = _manual_plan(ticket, status=DispatchPlanStatus.BLOCKED)

        with self.assertRaises(ValueError) as ctx:
            create_execution_request_from_plan(plan, ticket, requested_by=ticket.requester_id)

        self.assertIn("blocked", str(ctx.exception).lower())

    def test_create_request_rejects_publish_content_in_dispatchable(self) -> None:
        ticket = _manual_ticket()
        plan = _manual_plan(
            ticket,
            dispatchable_skills=["create_content", "publish_content"],
            preview_only_skills=["approval_review"],
            excluded_skills=[],
        )

        with self.assertRaises(ValueError) as ctx:
            create_execution_request_from_plan(plan, ticket, requested_by=ticket.requester_id)

        self.assertIn("publish", str(ctx.exception).lower())

    def test_create_request_snapshots_skill_lists(self) -> None:
        ticket = _manual_ticket()
        plan = _manual_plan(ticket)
        original_dispatchable = list(plan.dispatchable_skills)
        original_preview = list(plan.preview_only_skills)
        original_excluded = list(plan.excluded_skills)

        request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )

        plan.dispatchable_skills.append("mutated")
        plan.preview_only_skills.append("mutated")
        plan.excluded_skills.append("mutated")

        self.assertEqual(request.dispatchable_skills, original_dispatchable)
        self.assertEqual(request.preview_only_skills, original_preview)
        self.assertEqual(request.excluded_skills, original_excluded)


class TestExecutionRuntimeDryRun(unittest.TestCase):
    def setUp(self) -> None:
        get_default_execution_run_store().clear()

    def _request_and_context(
        self,
    ) -> tuple[ExecutionRequest, ExecutionTicket, ExecutionDispatchPlan]:
        ticket = _manual_ticket()
        plan = _manual_plan(ticket)
        request = create_execution_request_from_plan(
            plan,
            ticket,
            requested_by=ticket.requester_id,
        )
        return request, ticket, plan

    def test_start_dry_run_creates_completed_run(self) -> None:
        request, ticket, plan = self._request_and_context()
        store = ExecutionRunStore()

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            run = start_dry_run(request, ticket, plan, run_store=store)

        self.assertIsInstance(run, ExecutionRun)
        self.assertEqual(run.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(run.mode, ExecutionRunMode.DRY_RUN)
        self.assertTrue(run.dry_run)
        self.assertEqual(run.request_id, request.request_id)
        self.assertTrue(run.finished_at)
        self.assertIs(store.get(run.run_id), run)
        self.assertIs(store.get_by_request(request.request_id), run)

    def test_dispatchable_results_are_synthetic_dry_run(self) -> None:
        request, ticket, plan = self._request_and_context()

        run = start_dry_run(request, ticket, plan, run_store=ExecutionRunStore())

        self.assertEqual(len(run.dispatchable_results), len(request.dispatchable_skills))
        for result in run.dispatchable_results:
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["status"], "planned")
            self.assertIn(result["skill_id"], request.dispatchable_skills)

    def test_preview_results_are_synthetic_dry_run(self) -> None:
        request, ticket, plan = self._request_and_context()

        run = start_dry_run(request, ticket, plan, run_store=ExecutionRunStore())

        self.assertEqual(len(run.preview_results), len(request.preview_only_skills))
        for result in run.preview_results:
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["status"], "preview_planned")
            self.assertIn(result["skill_id"], request.preview_only_skills)

    def test_blocked_skills_contains_excluded(self) -> None:
        request, ticket, plan = self._request_and_context()

        run = start_dry_run(request, ticket, plan, run_store=ExecutionRunStore())

        self.assertEqual(run.blocked_skills, list(request.excluded_skills))
        self.assertIn("publish_content", run.blocked_skills)

    def test_start_dry_run_preserves_ticket_flags(self) -> None:
        request, ticket, plan = self._request_and_context()

        start_dry_run(request, ticket, plan, run_store=ExecutionRunStore())

        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)
        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(ticket.repository2_touched)

    def test_start_dry_run_preserves_plan_flags(self) -> None:
        request, ticket, plan = self._request_and_context()

        start_dry_run(request, ticket, plan, run_store=ExecutionRunStore())

        self.assertEqual(plan.status, DispatchPlanStatus.PLANNED)
        self.assertFalse(plan.executed)
        self.assertFalse(plan.repository2_touched)

    def test_run_repository2_touched_false(self) -> None:
        request, ticket, plan = self._request_and_context()

        run = start_dry_run(request, ticket, plan, run_store=ExecutionRunStore())

        self.assertFalse(run.repository2_touched)
        self.assertIn("repository2_touched", run.to_dict())
        self.assertFalse(run.to_dict()["repository2_touched"])

    def test_start_dry_run_idempotent_by_request_id(self) -> None:
        request, ticket, plan = self._request_and_context()
        store = ExecutionRunStore()

        first = start_dry_run(request, ticket, plan, run_store=store)
        second = start_dry_run(request, ticket, plan, run_store=store)

        self.assertIs(first, second)
        self.assertEqual(len(store.list_runs()), 1)

    def test_run_store_rejects_duplicate_request_id(self) -> None:
        request, ticket, plan = self._request_and_context()
        store = ExecutionRunStore()
        first = start_dry_run(request, ticket, plan, run_store=store)

        duplicate = ExecutionRun(
            run_id=str(uuid.uuid4()),
            request_id=request.request_id,
            plan_id=plan.plan_id,
            ticket_id=ticket.ticket_id,
            approval_session_id=ticket.approval_session_id,
            status=ExecutionRunStatus.COMPLETED,
            run_date=plan.run_date,
        )

        with self.assertRaises(ValueError):
            store.save(duplicate)

        self.assertIs(store.get(first.run_id), first)

    def test_run_store_allows_same_run_id_resave(self) -> None:
        request, ticket, plan = self._request_and_context()
        store = ExecutionRunStore()
        run = start_dry_run(request, ticket, plan, run_store=store)

        store.save(run)
        self.assertIs(store.get(run.run_id), run)

    def test_subprocess_not_called(self) -> None:
        request, ticket, plan = self._request_and_context()

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            start_dry_run(request, ticket, plan, run_store=ExecutionRunStore())

    def test_no_adapter_dispatch_import_or_call(self) -> None:
        import agent.coo.execution_runtime as runtime_mod
        import agent.coo.pipeline_adapter as pipeline_adapter_mod

        source = inspect.getsource(runtime_mod)
        self.assertNotIn("adapter.dispatch", source)
        self.assertNotIn(".dispatch(", source)
        self.assertNotIn("from agent.coo.pipeline_adapter", source)
        self.assertNotIn("import agent.coo.pipeline_adapter", source)

        request, ticket, plan = self._request_and_context()
        with patch.object(
            pipeline_adapter_mod.PipelineAdapter,
            "dispatch",
            side_effect=AssertionError("dispatch must not be called"),
        ), patch.object(
            pipeline_adapter_mod.PipelineAdapter,
            "dry_run",
            side_effect=AssertionError("dry_run deferred to Phase 8C"),
        ):
            start_dry_run(request, ticket, plan, run_store=ExecutionRunStore())

    def test_execution_runtime_has_no_execute_function(self) -> None:
        import agent.coo.execution_runtime as runtime_mod

        self.assertFalse(hasattr(runtime_mod, "execute"))


class TestExecutionRuntimeGatewayDiscordUntouched(unittest.TestCase):
    def test_gateway_execution_dispatcher_unchanged_surface(self) -> None:
        import agent.coo.gateway_execution_dispatcher as bridge_mod

        self.assertTrue(hasattr(bridge_mod, "create_dispatch_plan_for_gateway_ticket"))
        self.assertFalse(hasattr(bridge_mod, "start_dry_run"))

    def test_discord_coo_approval_has_no_execution_runtime_import(self) -> None:
        from pathlib import Path

        discord_path = (
            Path(__file__).resolve().parents[3]
            / "plugins/platforms/discord/coo_approval.py"
        )
        source = discord_path.read_text(encoding="utf-8")
        self.assertNotIn("execution_runtime", source)


if __name__ == "__main__":
    unittest.main()
