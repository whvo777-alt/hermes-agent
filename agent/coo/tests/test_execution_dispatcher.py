"""Execution dispatcher plan-only foundation tests (Phase 7E)."""

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
    create_dispatch_plan_from_ticket,
)
from agent.coo.execution_ticket import (
    ExecutionTicket,
    ExecutionTicketStatus,
    ExecutionTicketStore,
    create_ticket_from_approval_session,
)
from agent.coo.orchestrator import COOOrchestrator
from agent.coo.skills_catalog import get_skill


def _ticket_from_orchestrate(
    *,
    session_store: CEOApprovalSessionStore,
    ticket_store: ExecutionTicketStore,
    message: str = "오늘 블로그 글 작성해서 보고해",
    run_date: str = "2026-07-07",
    requester_id: str = "discord-user-1",
) -> ExecutionTicket:
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
    return create_ticket_from_approval_session(approved, store=ticket_store)


def _manual_ticket(
    *,
    status: ExecutionTicketStatus = ExecutionTicketStatus.CREATED,
    selected_skills: list[str] | None = None,
    entrypoints: list[str] | None = None,
    requester_id: str = "discord-user-1",
) -> ExecutionTicket:
    skills = selected_skills or ["create_content"]
    resolved_entrypoints = entrypoints
    if resolved_entrypoints is None:
        resolved_entrypoints = [
            get_skill(skill_id).entrypoint_hint if get_skill(skill_id) else ""
            for skill_id in skills
        ]
    return ExecutionTicket(
        ticket_id=str(uuid.uuid4()),
        approval_session_id=str(uuid.uuid4()),
        status=status,
        task_kind="create_content",
        run_date="2026-07-07",
        selected_skills=skills,
        entrypoints=resolved_entrypoints,
        requester_id=requester_id,
    )


class TestExecutionDispatcherFoundation(unittest.TestCase):
    def test_created_ticket_produces_planned_dispatch_plan(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        ticket = _ticket_from_orchestrate(
            session_store=session_store,
            ticket_store=ticket_store,
        )

        plan = create_dispatch_plan_from_ticket(
            ticket,
            requested_by="discord-user-1",
            reason="CEO approved content run",
        )

        self.assertEqual(plan.status, DispatchPlanStatus.PLANNED)
        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)
        self.assertIn("create_content", plan.dispatchable_skills)
        self.assertIn("approval_review", plan.preview_only_skills)
        self.assertEqual(plan.entrypoints_metadata, ticket.entrypoints)

    def test_dispatch_pending_ticket_raises_value_error(self) -> None:
        ticket = _manual_ticket(status=ExecutionTicketStatus.DISPATCH_PENDING)
        with self.assertRaises(ValueError):
            create_dispatch_plan_from_ticket(ticket, requested_by="discord-user-1")

    def test_dispatched_ticket_raises_value_error(self) -> None:
        ticket = _manual_ticket(status=ExecutionTicketStatus.DISPATCHED)
        with self.assertRaises(ValueError):
            create_dispatch_plan_from_ticket(ticket, requested_by="discord-user-1")

    def test_cancelled_ticket_raises_value_error(self) -> None:
        ticket = _manual_ticket(status=ExecutionTicketStatus.CANCELLED)
        with self.assertRaises(ValueError):
            create_dispatch_plan_from_ticket(ticket, requested_by="discord-user-1")

    def test_requested_by_mismatch_raises_value_error(self) -> None:
        ticket = _manual_ticket(requester_id="owner-1")
        with self.assertRaises(ValueError):
            create_dispatch_plan_from_ticket(ticket, requested_by="other-user")

    def test_create_content_is_dispatchable(self) -> None:
        ticket = _manual_ticket(selected_skills=["create_content"])
        plan = create_dispatch_plan_from_ticket(ticket, requested_by="discord-user-1")
        self.assertEqual(plan.dispatchable_skills, ["create_content"])
        self.assertEqual(plan.preview_only_skills, [])
        self.assertEqual(plan.excluded_skills, [])

    def test_approval_review_is_preview_only(self) -> None:
        ticket = _manual_ticket(
            selected_skills=["approval_review"],
            entrypoints=[get_skill("approval_review").entrypoint_hint],
        )
        plan = create_dispatch_plan_from_ticket(ticket, requested_by="discord-user-1")
        self.assertEqual(plan.preview_only_skills, ["approval_review"])
        self.assertEqual(plan.dispatchable_skills, [])
        self.assertEqual(plan.excluded_skills, [])

    def test_publish_content_is_excluded(self) -> None:
        ticket = _manual_ticket(
            selected_skills=["publish_content"],
            entrypoints=[get_skill("publish_content").entrypoint_hint],
        )
        plan = create_dispatch_plan_from_ticket(ticket, requested_by="discord-user-1")
        self.assertEqual(plan.excluded_skills, ["publish_content"])
        self.assertIn("publish_content", plan.exclusion_reasons)
        self.assertEqual(plan.dispatchable_skills, [])
        self.assertEqual(plan.preview_only_skills, [])

    def test_unknown_skill_is_excluded(self) -> None:
        ticket = _manual_ticket(selected_skills=["not_a_real_skill"])
        plan = create_dispatch_plan_from_ticket(ticket, requested_by="discord-user-1")
        self.assertEqual(plan.excluded_skills, ["not_a_real_skill"])
        self.assertIn("not_a_real_skill", plan.exclusion_reasons)

    def test_plan_and_ticket_execution_flags_remain_false(self) -> None:
        ticket = _manual_ticket()
        plan = create_dispatch_plan_from_ticket(ticket, requested_by="discord-user-1")
        self.assertFalse(plan.executed)
        self.assertFalse(plan.repository2_touched)
        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(ticket.repository2_touched)

    def test_no_subprocess_on_dispatch_plan_creation(self) -> None:
        ticket = _manual_ticket()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            create_dispatch_plan_from_ticket(ticket, requested_by="discord-user-1")

    def test_dispatcher_has_no_execute_run_or_dispatch_now(self) -> None:
        import agent.coo.execution_dispatcher as execution_dispatcher_mod

        self.assertFalse(hasattr(execution_dispatcher_mod, "execute"))
        self.assertFalse(hasattr(execution_dispatcher_mod, "run"))
        self.assertFalse(hasattr(execution_dispatcher_mod, "dispatch_now"))

    def test_dispatcher_does_not_import_pipeline_adapter(self) -> None:
        import agent.coo.execution_dispatcher as execution_dispatcher_mod

        source = inspect.getsource(execution_dispatcher_mod)
        self.assertNotIn("pipeline_adapter", source)

    def test_orchestrate_approve_ticket_plan_flow(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        ticket = _ticket_from_orchestrate(
            session_store=session_store,
            ticket_store=ticket_store,
        )

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            plan = create_dispatch_plan_from_ticket(
                ticket,
                requested_by="discord-user-1",
            )

        self.assertEqual(plan.status, DispatchPlanStatus.PLANNED)
        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)
        self.assertIn("research_worker", ticket.selected_workers)
        self.assertNotIn("publish_content", plan.dispatchable_skills)
        self.assertNotIn("publish_content", plan.preview_only_skills)

    def test_to_dict_includes_skill_classification(self) -> None:
        ticket = _manual_ticket(
            selected_skills=["create_content", "approval_review", "publish_content"],
            entrypoints=[
                get_skill("create_content").entrypoint_hint,
                get_skill("approval_review").entrypoint_hint,
                get_skill("publish_content").entrypoint_hint,
            ],
        )
        plan = create_dispatch_plan_from_ticket(ticket, requested_by="discord-user-1")
        payload = plan.to_dict()

        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["dispatchable_skills"], ["create_content"])
        self.assertEqual(payload["preview_only_skills"], ["approval_review"])
        self.assertEqual(payload["excluded_skills"], ["publish_content"])

    def test_dispatch_plan_preserves_request_audit_fields(self) -> None:
        ticket = _manual_ticket(requester_id="CEO")
        plan = create_dispatch_plan_from_ticket(
            ticket,
            requested_by="CEO",
            reason="manual dispatch check",
        )
        payload = plan.to_dict()

        self.assertTrue(plan.request_id)
        self.assertEqual(plan.requested_by, "CEO")
        self.assertEqual(plan.reason, "manual dispatch check")
        self.assertTrue(plan.requested_at)
        self.assertEqual(payload["request_id"], plan.request_id)
        self.assertEqual(payload["requested_by"], "CEO")
        self.assertEqual(payload["requested_at"], plan.requested_at)
        self.assertEqual(payload["reason"], "manual dispatch check")


if __name__ == "__main__":
    unittest.main()
