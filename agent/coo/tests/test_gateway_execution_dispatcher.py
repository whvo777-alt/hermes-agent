"""Gateway execution dispatcher bridge tests (Phase 7G)."""

from __future__ import annotations

import inspect
import subprocess
import unittest
import uuid
from pathlib import Path
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
    ExecutionDispatchPlanStore,
    get_default_dispatch_plan_store,
)
from agent.coo.execution_ticket import (
    ExecutionTicket,
    ExecutionTicketStatus,
    ExecutionTicketStore,
    create_ticket_from_approval_session,
    get_default_ticket_store,
)
from agent.coo.gateway_approval import (
    approve_gateway_session,
    create_gateway_approval_session,
)
from agent.coo.gateway_execution_dispatcher import (
    create_dispatch_plan_for_gateway_session,
    create_dispatch_plan_for_gateway_ticket,
    get_dispatch_plan_for_gateway_ticket,
)
from agent.coo.orchestrator import COOOrchestrator


def _created_ticket(
    *,
    session_store: CEOApprovalSessionStore,
    ticket_store: ExecutionTicketStore,
    requester_id: str = "discord-user-1",
) -> ExecutionTicket:
    with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
        orchestrated = COOOrchestrator().orchestrate(
            "오늘 블로그 글 작성해서 보고해",
            run_date="2026-07-07",
        )
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


class TestGatewayExecutionDispatcherBridge(unittest.TestCase):
    def setUp(self) -> None:
        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()

    def test_created_ticket_returns_planned_dispatch_plan(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket = _created_ticket(session_store=session_store, ticket_store=ticket_store)

        plan_dict = create_dispatch_plan_for_gateway_ticket(
            ticket.ticket_id,
            requester_id="discord-user-1",
            reason="manual plan request",
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        self.assertEqual(plan_dict["status"], DispatchPlanStatus.PLANNED.value)
        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)
        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(ticket.repository2_touched)
        self.assertEqual(plan_dict["requested_by"], "discord-user-1")
        self.assertEqual(plan_dict["reason"], "manual plan request")

    def test_wrong_requester_raises_value_error(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket = _created_ticket(session_store=session_store, ticket_store=ticket_store)

        with self.assertRaises(ValueError):
            create_dispatch_plan_for_gateway_ticket(
                ticket.ticket_id,
                requester_id="other-user",
                ticket_store=ticket_store,
                plan_store=plan_store,
            )

    def test_missing_ticket_raises_key_error(self) -> None:
        plan_store = ExecutionDispatchPlanStore()
        with self.assertRaises(KeyError):
            create_dispatch_plan_for_gateway_ticket(
                str(uuid.uuid4()),
                requester_id="discord-user-1",
                plan_store=plan_store,
            )

    def test_same_ticket_request_returns_same_plan(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket = _created_ticket(session_store=session_store, ticket_store=ticket_store)

        first = create_dispatch_plan_for_gateway_ticket(
            ticket.ticket_id,
            requester_id="discord-user-1",
            ticket_store=ticket_store,
            plan_store=plan_store,
        )
        second = create_dispatch_plan_for_gateway_ticket(
            ticket.ticket_id,
            requester_id="discord-user-1",
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(len(plan_store.list_plans()), 1)

    def test_plan_store_rejects_duplicate_ticket_plan_id(self) -> None:
        plan_store = ExecutionDispatchPlanStore()
        ticket_id = str(uuid.uuid4())
        first = ExecutionDispatchPlan(
            plan_id="plan-1",
            request_id="request-1",
            ticket_id=ticket_id,
            approval_session_id="session-1",
            status=DispatchPlanStatus.PLANNED,
            run_date="2026-07-07",
        )
        second = ExecutionDispatchPlan(
            plan_id="plan-2",
            request_id="request-2",
            ticket_id=ticket_id,
            approval_session_id="session-1",
            status=DispatchPlanStatus.PLANNED,
            run_date="2026-07-07",
        )

        plan_store.save(first)
        with self.assertRaises(ValueError):
            plan_store.save(second)

        self.assertIs(plan_store.get_by_ticket(ticket_id), first)

    def test_approve_gateway_session_does_not_create_dispatch_plan(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            orchestrated = COOOrchestrator().orchestrate(
                "오늘 상태 보고해",
                run_date="2026-07-07",
            )
        report = build_approval_report(orchestrated)
        created = create_gateway_approval_session(
            report,
            orchestrated,
            requester_id="discord-user-1",
            channel_id="discord-chan-1",
            store=session_store,
        )
        assert created is not None

        approve_gateway_session(
            created["session_id"],
            requester_id="discord-user-1",
            store=session_store,
            ticket_store=ticket_store,
        )

        self.assertEqual(len(plan_store.list_plans()), 0)
        ticket = ticket_store.get_by_session(created["session_id"])
        assert ticket is not None
        self.assertIsNone(get_dispatch_plan_for_gateway_ticket(
            ticket.ticket_id,
            plan_store=plan_store,
        ))

    def test_create_dispatch_plan_for_gateway_session(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket = _created_ticket(session_store=session_store, ticket_store=ticket_store)

        plan_dict = create_dispatch_plan_for_gateway_session(
            ticket.approval_session_id,
            requester_id="discord-user-1",
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        self.assertEqual(plan_dict["ticket_id"], ticket.ticket_id)
        self.assertEqual(plan_dict["status"], DispatchPlanStatus.PLANNED.value)

    def test_missing_session_ticket_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            create_dispatch_plan_for_gateway_session(
                str(uuid.uuid4()),
                requester_id="discord-user-1",
            )

    def test_no_subprocess_on_gateway_plan_request(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket = _created_ticket(session_store=session_store, ticket_store=ticket_store)

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            create_dispatch_plan_for_gateway_ticket(
                ticket.ticket_id,
                requester_id="discord-user-1",
                ticket_store=ticket_store,
                plan_store=plan_store,
            )

    def test_gateway_bridge_has_no_execute_run_or_dispatch_now(self) -> None:
        import agent.coo.gateway_execution_dispatcher as gateway_execution_dispatcher_mod

        self.assertFalse(hasattr(gateway_execution_dispatcher_mod, "execute"))
        self.assertFalse(hasattr(gateway_execution_dispatcher_mod, "run"))
        self.assertFalse(hasattr(gateway_execution_dispatcher_mod, "dispatch_now"))

    def test_discord_coo_approval_does_not_import_gateway_execution_dispatcher(self) -> None:
        discord_path = (
            Path(__file__).resolve().parents[3]
            / "plugins/platforms/discord/coo_approval.py"
        )
        source = discord_path.read_text(encoding="utf-8")
        self.assertNotIn("gateway_execution_dispatcher", source)


if __name__ == "__main__":
    unittest.main()
