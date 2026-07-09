"""Execution Ticket foundation tests (Phase 7B / 7B Fix)."""

from __future__ import annotations

import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent.coo.approval_report import CEOApprovalReport, CEOApprovalReportStatus, build_approval_report
from agent.coo.approval_session import (
    CEOApprovalSession,
    CEOApprovalSessionStatus,
    CEOApprovalSessionStore,
    approve_session,
    create_approval_session,
)
from agent.coo.execution_ticket import (
    ExecutionTicket,
    ExecutionTicketStatus,
    ExecutionTicketStore,
    bump_dispatch_generation,
    create_ticket_from_approval_session,
)
from agent.coo.orchestrator import COOOrchestrator
from agent.coo.skills_catalog import get_skill


def _approved_session_from_orchestrate(
    *,
    session_store: CEOApprovalSessionStore,
    message: str = "오늘 블로그 글 작성해서 보고해",
    run_date: str = "2026-07-07",
    requester_id: str = "discord-user-1",
    channel_id: str = "discord-chan-1",
) -> CEOApprovalSession:
    with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
        orchestrated = COOOrchestrator().orchestrate(message, run_date=run_date)
    report = build_approval_report(orchestrated)
    session = create_approval_session(
        report,
        orchestrated,
        requester_id=requester_id,
        channel_id=channel_id,
        store=session_store,
    )
    return approve_session(
        session.session_id,
        reviewer=requester_id,
        requester_id=requester_id,
        store=session_store,
    )


class TestExecutionTicketFoundation(unittest.TestCase):
    def test_approved_session_creates_ticket(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        session = _approved_session_from_orchestrate(session_store=session_store)

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            ticket = create_ticket_from_approval_session(session, store=ticket_store)

        self.assertTrue(ticket.ticket_id)
        self.assertEqual(ticket.approval_session_id, session.session_id)
        self.assertEqual(ticket.status, ExecutionTicketStatus.CREATED)
        self.assertEqual(ticket.run_date, "2026-07-07")
        self.assertEqual(ticket.requester_id, "discord-user-1")
        self.assertEqual(ticket.channel_id, "discord-chan-1")
        self.assertEqual(ticket.reviewer, "discord-user-1")
        self.assertIs(ticket_store.get(ticket.ticket_id), ticket)
        self.assertIs(ticket_store.get_by_session(session.session_id), ticket)

    def test_pending_session_raises_value_error(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        report = CEOApprovalReport(
            status=CEOApprovalReportStatus.READY,
            task_kind="daily_brief",
            run_date="2026-07-07",
            runtime_status="selected",
            worker_summary="test",
        )
        orchestrated = COOOrchestrator().orchestrate("오늘 상태 보고해", run_date="2026-07-07")
        pending = create_approval_session(report, orchestrated, store=session_store)

        with self.assertRaises(ValueError):
            create_ticket_from_approval_session(pending, store=ticket_store)

    def test_rejected_session_raises_value_error(self) -> None:
        from agent.coo.approval_session import reject_session

        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        report = CEOApprovalReport(
            status=CEOApprovalReportStatus.READY,
            task_kind="daily_brief",
            run_date="2026-07-07",
            runtime_status="selected",
            worker_summary="test",
        )
        orchestrated = COOOrchestrator().orchestrate("오늘 상태 보고해", run_date="2026-07-07")
        session = create_approval_session(report, orchestrated, store=session_store)
        rejected = reject_session(session.session_id, reviewer="CEO", store=session_store)

        with self.assertRaises(ValueError):
            create_ticket_from_approval_session(rejected, store=ticket_store)

    def test_expired_session_raises_value_error(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        report = CEOApprovalReport(
            status=CEOApprovalReportStatus.READY,
            task_kind="daily_brief",
            run_date="2026-07-07",
            runtime_status="selected",
            worker_summary="test",
        )
        orchestrated = COOOrchestrator().orchestrate("오늘 상태 보고해", run_date="2026-07-07")
        session = create_approval_session(report, orchestrated, store=session_store)
        session.status = CEOApprovalSessionStatus.EXPIRED
        session_store.save(session)

        with self.assertRaises(ValueError):
            create_ticket_from_approval_session(session, store=ticket_store)

    def test_cancelled_session_raises_value_error(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        report = CEOApprovalReport(
            status=CEOApprovalReportStatus.READY,
            task_kind="daily_brief",
            run_date="2026-07-07",
            runtime_status="selected",
            worker_summary="test",
        )
        orchestrated = COOOrchestrator().orchestrate("오늘 상태 보고해", run_date="2026-07-07")
        session = create_approval_session(report, orchestrated, store=session_store)
        session.status = CEOApprovalSessionStatus.CANCELLED
        session_store.save(session)

        with self.assertRaises(ValueError):
            create_ticket_from_approval_session(session, store=ticket_store)

    def test_same_approved_session_returns_same_ticket(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        session = _approved_session_from_orchestrate(session_store=session_store)

        first = create_ticket_from_approval_session(session, store=ticket_store)
        second = create_ticket_from_approval_session(session, store=ticket_store)

        self.assertIs(first, second)
        self.assertEqual(len(ticket_store.list_tickets()), 1)

    def test_ticket_dispatch_flags_remain_false(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        session = _approved_session_from_orchestrate(session_store=session_store)
        ticket = create_ticket_from_approval_session(session, store=ticket_store)

        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(ticket.repository2_touched)

    def test_ticket_safety_flags(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        session = _approved_session_from_orchestrate(session_store=session_store)
        ticket = create_ticket_from_approval_session(session, store=ticket_store)

        self.assertFalse(ticket.auto_apply)
        self.assertTrue(ticket.review_required)

    def test_real_orchestrate_flow_populates_worker_and_skill_namespaces(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        session = _approved_session_from_orchestrate(session_store=session_store)
        ticket = create_ticket_from_approval_session(session, store=ticket_store)

        self.assertIn("research_worker", session.selected_workers)
        self.assertNotIn("create_content", session.selected_workers)
        self.assertIn("create_content", session.selected_skill_ids)
        self.assertIn("approval_review", session.selected_skill_ids)

        self.assertEqual(ticket.selected_workers, session.selected_workers)
        self.assertEqual(ticket.selected_skills, session.selected_skill_ids)
        self.assertEqual(
            ticket.entrypoints,
            [
                get_skill("create_content").entrypoint_hint,
                get_skill("approval_review").entrypoint_hint,
            ],
        )
        self.assertNotIn("", ticket.entrypoints)

    def test_missing_skill_ids_leave_entrypoints_empty_with_note(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        session = CEOApprovalSession(
            session_id=str(uuid.uuid4()),
            status=CEOApprovalSessionStatus.APPROVED,
            task_kind="create_content",
            run_date="2026-07-07",
            report_status="ready",
            runtime_status="selected",
            selected_workers=[
                "research_worker",
                "strategy_worker",
                "draft_worker",
            ],
            selected_skill_ids=[],
            reviewer="CEO",
        )
        session_store.save(session)

        ticket = create_ticket_from_approval_session(session, store=ticket_store)

        self.assertEqual(ticket.selected_workers, session.selected_workers)
        self.assertEqual(ticket.selected_skills, [])
        self.assertEqual(ticket.entrypoints, [])
        self.assertTrue(ticket.notes)
        self.assertIn("entrypoints unavailable", ticket.notes[0])
        self.assertIn("worker IDs", ticket.notes[0])

    def test_no_subprocess_on_ticket_creation(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        session = _approved_session_from_orchestrate(session_store=session_store)

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            create_ticket_from_approval_session(session, store=ticket_store)

    def test_no_file_persistence(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        session = _approved_session_from_orchestrate(session_store=session_store)
        create_ticket_from_approval_session(session, store=ticket_store)

        hermes_home = Path("/tmp/nonexistent-hermes-home-phase7b")
        ticket_files = list(hermes_home.glob("**/*ticket*")) if hermes_home.exists() else []
        self.assertEqual(ticket_files, [])

    def test_no_dispatch_function_in_module(self) -> None:
        import agent.coo.execution_ticket as execution_ticket_mod

        self.assertFalse(hasattr(execution_ticket_mod, "dispatch"))
        self.assertFalse(hasattr(execution_ticket_mod, "dispatch_ticket"))

    def test_to_dict_roundtrip_fields(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        session = _approved_session_from_orchestrate(session_store=session_store)
        ticket = create_ticket_from_approval_session(session, store=ticket_store)
        payload = ticket.to_dict()

        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["approval_session_id"], session.session_id)
        self.assertEqual(payload["task_kind"], session.task_kind)
        self.assertEqual(payload["run_date"], session.run_date)
        self.assertEqual(payload["selected_workers"], session.selected_workers)
        self.assertEqual(payload["selected_skills"], session.selected_skill_ids)
        self.assertEqual(payload["source_report_status"], session.report_status)
        self.assertEqual(payload["source_runtime_status"], session.runtime_status)


class TestExecutionTicketStoreInvariant(unittest.TestCase):
    def test_duplicate_session_ticket_id_raises_value_error(self) -> None:
        store = ExecutionTicketStore()
        session_id = "session-1"
        t1 = ExecutionTicket(
            ticket_id="ticket-1",
            approval_session_id=session_id,
            status=ExecutionTicketStatus.CREATED,
            task_kind="create_content",
            run_date="2026-07-07",
        )
        t2 = ExecutionTicket(
            ticket_id="ticket-2",
            approval_session_id=session_id,
            status=ExecutionTicketStatus.CREATED,
            task_kind="create_content",
            run_date="2026-07-07",
        )

        store.save(t1)
        with self.assertRaises(ValueError):
            store.save(t2)

        self.assertIs(store.get_by_session(session_id), t1)
        self.assertEqual(len(store.list_tickets()), 1)

    def test_same_ticket_id_resave_allowed(self) -> None:
        store = ExecutionTicketStore()
        ticket = ExecutionTicket(
            ticket_id="ticket-1",
            approval_session_id="session-1",
            status=ExecutionTicketStatus.CREATED,
            task_kind="create_content",
            run_date="2026-07-07",
            notes=["updated"],
        )

        store.save(ticket)
        store.save(ticket)

        self.assertIs(store.get("ticket-1"), ticket)
        self.assertEqual(store.get("ticket-1").notes, ["updated"])


class TestDispatchGeneration(unittest.TestCase):
    def test_dispatch_generation_defaults_to_zero(self) -> None:
        ticket = ExecutionTicket(
            ticket_id="ticket-gen-1",
            approval_session_id="session-gen-1",
            status=ExecutionTicketStatus.CREATED,
            task_kind="create_content",
            run_date="2026-07-07",
        )
        self.assertEqual(ticket.dispatch_generation, 0)

    def test_bump_dispatch_generation_increments(self) -> None:
        ticket = ExecutionTicket(
            ticket_id="ticket-gen-2",
            approval_session_id="session-gen-2",
            status=ExecutionTicketStatus.CREATED,
            task_kind="create_content",
            run_date="2026-07-07",
        )
        first = bump_dispatch_generation(ticket, reason="new dry-run")
        second = bump_dispatch_generation(ticket, reason="new execute request")

        self.assertEqual(first, 1)
        self.assertEqual(second, 2)
        self.assertEqual(ticket.dispatch_generation, 2)
        self.assertIn("dispatch_generation bumped: new dry-run", ticket.notes)
        self.assertIn("dispatch_generation bumped: new execute request", ticket.notes)


if __name__ == "__main__":
    unittest.main()
