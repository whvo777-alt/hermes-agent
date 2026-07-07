"""Gateway execution ticket bridge tests (Phase 7C)."""

from __future__ import annotations

import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from agent.coo.approval_report import build_approval_report
from agent.coo.approval_session import CEOApprovalSessionStore
from agent.coo.execution_ticket import (
    ExecutionTicketStatus,
    ExecutionTicketStore,
    create_ticket_from_approval_session,
    get_default_ticket_store,
)
from agent.coo.gateway_approval import (
    approve_gateway_session,
    create_gateway_approval_session,
    create_ticket_for_gateway_session,
    expire_gateway_sessions,
    reject_gateway_session,
)
from agent.coo.orchestrator import COOOrchestrator


def _pending_gateway_session(
    *,
    session_store: CEOApprovalSessionStore,
    requester_id: str = "discord-user-123",
    channel_id: str = "discord-channel-456",
) -> dict:
    with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
        orchestrated = COOOrchestrator().orchestrate(
            "오늘 상태 보고해",
            run_date="2026-07-07",
        )
    report = build_approval_report(orchestrated)
    created = create_gateway_approval_session(
        report,
        orchestrated,
        requester_id=requester_id,
        channel_id=channel_id,
        store=session_store,
    )
    assert created is not None
    return created


class TestGatewayTicketBridge(unittest.TestCase):
    def setUp(self) -> None:
        get_default_ticket_store().clear()

    def test_approve_creates_ticket_with_created_status(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        created = _pending_gateway_session(session_store=session_store)

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                created["session_id"],
                requester_id="discord-user-123",
                store=session_store,
                ticket_store=ticket_store,
            )

        ticket = ticket_store.get_by_session(created["session_id"])
        assert ticket is not None
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["execution_ticket_id"], ticket.ticket_id)
        self.assertEqual(ticket.status, ExecutionTicketStatus.CREATED)

    def test_approve_twice_returns_same_ticket(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        created = _pending_gateway_session(session_store=session_store)

        approve_gateway_session(
            created["session_id"],
            requester_id="discord-user-123",
            store=session_store,
            ticket_store=ticket_store,
        )
        first_ticket = ticket_store.get_by_session(created["session_id"])
        assert first_ticket is not None

        second_ticket_dict = create_ticket_for_gateway_session(
            created["session_id"],
            session_store=session_store,
            ticket_store=ticket_store,
        )

        self.assertEqual(second_ticket_dict["ticket_id"], first_ticket.ticket_id)
        self.assertEqual(len(ticket_store.list_tickets()), 1)

    def test_reject_does_not_create_ticket(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        created = _pending_gateway_session(session_store=session_store)

        reject_gateway_session(
            created["session_id"],
            requester_id="discord-user-123",
            store=session_store,
        )

        self.assertIsNone(ticket_store.get_by_session(created["session_id"]))

    def test_expired_session_does_not_create_ticket(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        created = _pending_gateway_session(session_store=session_store)
        session = session_store.get(created["session_id"])
        assert session is not None
        session.expires_at = "2020-01-01T00:00:00+00:00"
        session_store.save(session)

        expire_gateway_sessions(
            now=datetime(2026, 7, 8, tzinfo=timezone.utc),
            store=session_store,
        )

        with self.assertRaises(ValueError):
            create_ticket_from_approval_session(session, store=ticket_store)

        self.assertIsNone(ticket_store.get_by_session(created["session_id"]))

    def test_execution_flags_remain_false_after_approve(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        created = _pending_gateway_session(session_store=session_store)

        approved = approve_gateway_session(
            created["session_id"],
            requester_id="discord-user-123",
            store=session_store,
            ticket_store=ticket_store,
        )
        ticket = ticket_store.get_by_session(created["session_id"])
        assert ticket is not None

        self.assertFalse(approved["execution_dispatched"])
        self.assertFalse(approved["publish_dispatched"])
        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(ticket.repository2_touched)

    def test_no_subprocess_on_gateway_ticket_bridge(self) -> None:
        session_store = CEOApprovalSessionStore()
        ticket_store = ExecutionTicketStore()
        created = _pending_gateway_session(session_store=session_store)

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approve_gateway_session(
                created["session_id"],
                requester_id="discord-user-123",
                store=session_store,
                ticket_store=ticket_store,
            )

    def test_no_dispatch_function_in_gateway_bridge(self) -> None:
        import agent.coo.gateway_approval as gateway_approval_mod

        self.assertFalse(hasattr(gateway_approval_mod, "dispatch"))
        self.assertFalse(hasattr(gateway_approval_mod, "dispatch_ticket"))


if __name__ == "__main__":
    unittest.main()
