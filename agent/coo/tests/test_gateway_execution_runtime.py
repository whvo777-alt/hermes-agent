"""Gateway execution runtime bridge tests (Phase 8C)."""

from __future__ import annotations

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
from agent.coo.execution_runtime import ExecutionRunStatus, ExecutionRunStore
from agent.coo.execution_ticket import (
    ExecutionTicket,
    ExecutionTicketStatus,
    ExecutionTicketStore,
    get_default_ticket_store,
)
from agent.coo.gateway_execution_runtime import (
    get_latest_dry_run_for_gateway_ticket,
    start_dry_run_for_gateway_session,
    start_dry_run_for_gateway_ticket,
)
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
        reason="manual gateway runtime plan",
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
    ticket_status: ExecutionTicketStatus = ExecutionTicketStatus.DISPATCH_PENDING,
    plan_status: DispatchPlanStatus = DispatchPlanStatus.PLANNED,
    dispatchable_skills: list[str] | None = None,
) -> tuple[ExecutionTicket, ExecutionDispatchPlan]:
    ticket = _manual_ticket(status=ticket_status)
    plan = _manual_plan(
        ticket,
        status=plan_status,
        dispatchable_skills=dispatchable_skills,
    )
    ticket_store.save(ticket)
    plan_store.save(plan)
    return ticket, plan


class TestGatewayExecutionRuntimeBridge(unittest.TestCase):
    def setUp(self) -> None:
        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()

    def test_happy_path_ticket_returns_request_and_run_dicts(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        ticket, plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        result = start_dry_run_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            reason="discord dry-run preview",
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
        )

        self.assertEqual(result["request"]["ticket_id"], ticket.ticket_id)
        self.assertEqual(result["request"]["plan_id"], plan.plan_id)
        self.assertEqual(result["request"]["reason"], "discord dry-run preview")
        self.assertEqual(result["run"]["ticket_id"], ticket.ticket_id)
        self.assertEqual(result["run"]["status"], ExecutionRunStatus.COMPLETED.value)
        self.assertTrue(result["run"]["dry_run"])
        self.assertEqual(len(run_store.list_runs()), 1)

    def test_happy_path_session_reuses_ticket_bridge(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        ticket, plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        result = start_dry_run_for_gateway_session(
            ticket.approval_session_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
        )

        self.assertEqual(result["request"]["approval_session_id"], ticket.approval_session_id)
        self.assertEqual(result["request"]["plan_id"], plan.plan_id)
        self.assertEqual(result["run"]["approval_session_id"], ticket.approval_session_id)

    def test_missing_ticket_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            start_dry_run_for_gateway_ticket(
                str(uuid.uuid4()),
                requester_id="discord-user-1",
                ticket_store=ExecutionTicketStore(),
                plan_store=ExecutionDispatchPlanStore(),
                run_store=ExecutionRunStore(),
            )

    def test_missing_session_ticket_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            start_dry_run_for_gateway_session(
                str(uuid.uuid4()),
                requester_id="discord-user-1",
                ticket_store=ExecutionTicketStore(),
                plan_store=ExecutionDispatchPlanStore(),
                run_store=ExecutionRunStore(),
            )

    def test_missing_plan_raises_key_error(self) -> None:
        ticket_store = ExecutionTicketStore()
        ticket = _manual_ticket()
        ticket_store.save(ticket)

        with self.assertRaises(KeyError):
            start_dry_run_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ticket_store,
                plan_store=ExecutionDispatchPlanStore(),
                run_store=ExecutionRunStore(),
            )

    def test_wrong_requester_raises_value_error(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        with self.assertRaises(ValueError):
            start_dry_run_for_gateway_ticket(
                ticket.ticket_id,
                requester_id="other-user",
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=ExecutionRunStore(),
            )

    def test_ticket_not_dispatch_pending_raises_value_error(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
            ticket_status=ExecutionTicketStatus.CREATED,
        )

        with self.assertRaises(ValueError):
            start_dry_run_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=ExecutionRunStore(),
            )

    def test_plan_not_planned_raises_value_error(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
            plan_status=DispatchPlanStatus.BLOCKED,
        )

        with self.assertRaises(ValueError):
            start_dry_run_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=ExecutionRunStore(),
            )

    def test_publish_content_in_dispatchable_raises_value_error(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
            dispatchable_skills=["create_content", "publish_content"],
        )

        with self.assertRaises(ValueError):
            start_dry_run_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=ExecutionRunStore(),
            )

    def test_dry_run_preserves_ticket_plan_and_run_safety_flags(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        ticket, plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        result = start_dry_run_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
        )

        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCH_PENDING)
        self.assertFalse(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertFalse(ticket.repository2_touched)
        self.assertFalse(plan.executed)
        self.assertFalse(plan.repository2_touched)
        self.assertTrue(result["run"]["dry_run"])
        self.assertFalse(result["run"]["repository2_touched"])

    def test_two_calls_create_two_runs(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        first = start_dry_run_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            reason="first",
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
        )
        second = start_dry_run_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            reason="second",
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
        )

        self.assertNotEqual(first["request"]["request_id"], second["request"]["request_id"])
        self.assertNotEqual(first["run"]["run_id"], second["run"]["run_id"])
        self.assertEqual(len(run_store.list_runs()), 2)

    def test_get_latest_dry_run_for_gateway_ticket_returns_latest(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        run_store = ExecutionRunStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        first = start_dry_run_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            reason="first",
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
        )
        second = start_dry_run_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            reason="second",
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
        )

        latest = get_latest_dry_run_for_gateway_ticket(
            ticket.ticket_id,
            run_store=run_store,
        )

        assert latest is not None
        self.assertEqual(latest["run_id"], second["run"]["run_id"])
        self.assertNotEqual(latest["run_id"], first["run"]["run_id"])

    def test_get_latest_returns_none_when_missing(self) -> None:
        self.assertIsNone(
            get_latest_dry_run_for_gateway_ticket(
                str(uuid.uuid4()),
                run_store=ExecutionRunStore(),
            )
        )

    def test_no_subprocess_on_gateway_dry_run(self) -> None:
        ticket_store = ExecutionTicketStore()
        plan_store = ExecutionDispatchPlanStore()
        ticket, _plan = _seed_ticket_and_plan(
            ticket_store=ticket_store,
            plan_store=plan_store,
        )

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            start_dry_run_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=ExecutionRunStore(),
            )

    def test_gateway_runtime_surface_has_no_forbidden_functions(self) -> None:
        import agent.coo.gateway_execution_runtime as gateway_execution_runtime_mod

        self.assertFalse(hasattr(gateway_execution_runtime_mod, "execute"))
        self.assertFalse(hasattr(gateway_execution_runtime_mod, "dispatch_now"))
        self.assertFalse(hasattr(gateway_execution_runtime_mod, "run_real"))

    def test_gateway_dispatcher_has_no_dry_run_functions(self) -> None:
        import agent.coo.gateway_execution_dispatcher as gateway_execution_dispatcher_mod

        self.assertFalse(
            hasattr(gateway_execution_dispatcher_mod, "start_dry_run_for_gateway_ticket")
        )
        self.assertFalse(
            hasattr(gateway_execution_dispatcher_mod, "start_dry_run_for_gateway_session")
        )

    def test_discord_coo_approval_only_lazy_imports_gateway_execution_runtime(self) -> None:
        discord_path = (
            Path(__file__).resolve().parents[3]
            / "plugins/platforms/discord/coo_approval.py"
        )
        source = discord_path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith("from agent.coo.gateway_execution_runtime import"):
                self.fail(
                    "gateway_execution_runtime must be imported lazily inside functions"
                )
            if line.startswith("import agent.coo.gateway_execution_runtime"):
                self.fail(
                    "gateway_execution_runtime must be imported lazily inside functions"
                )


if __name__ == "__main__":
    unittest.main()
