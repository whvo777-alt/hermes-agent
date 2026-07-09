"""Dispatch execution runner tests (Phase 10F)."""

from __future__ import annotations

import subprocess
import unittest
import uuid
from unittest.mock import patch

from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionRequest,
    DispatchExecutionRequestStore,
    DispatchExecutionRunStatus,
    DispatchExecutionRunStore,
    DispatchUnlockTokenStore,
    create_dispatch_execution_request,
    create_dispatch_unlock_token,
    start_dispatch_run,
)
from agent.coo.execution_dispatch_runner import run_approved_dispatch
from agent.coo.execution_dispatcher import ExecutionDispatchPlanStore
from agent.coo.execution_execute import ExecuteGate, ExecuteGateStatus, ExecuteGateStore
from agent.coo.execution_ticket import ExecutionTicketStatus, ExecutionTicketStore
from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig
from agent.coo.tests.test_execution_dispatch_runtime import _approved_unlock_context


def _seed_dispatch_stores():
    ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
    ticket_store = ExecutionTicketStore()
    plan_store = ExecutionDispatchPlanStore()
    gate_store = ExecuteGateStore()
    token_store = DispatchUnlockTokenStore()
    dispatch_request_store = DispatchExecutionRequestStore()
    dispatch_run_store = DispatchExecutionRunStore()

    ticket_store.save(ticket)
    plan_store.save(plan)
    gate_store.save(gate)

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
    dispatch_request = create_dispatch_execution_request(
        token,
        request_store=dispatch_request_store,
    )

    return {
        "ticket": ticket,
        "plan": plan,
        "gate": gate,
        "token": token,
        "dispatch_request": dispatch_request,
        "ticket_store": ticket_store,
        "plan_store": plan_store,
        "gate_store": gate_store,
        "token_store": token_store,
        "dispatch_request_store": dispatch_request_store,
        "dispatch_run_store": dispatch_run_store,
    }


def _fake_adapter(success: bool = True) -> PipelineAdapter:
    def executor(command: str, pipeline_root: str, run_date: str) -> tuple[int, str, str]:
        if success:
            return 0, "ok", ""
        return 1, "", "fake adapter failure"

    return PipelineAdapter(
        PipelineAdapterConfig(allow_execute=True, pipeline_root="/tmp/fake-pipeline"),
        executor=executor,
    )


class TestExecutionDispatchRunner(unittest.TestCase):
    def test_happy_path_completes_run_and_commits_state(self) -> None:
        ctx = _seed_dispatch_stores()
        ticket = ctx["ticket"]
        plan = ctx["plan"]
        token = ctx["token"]

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch.object(PipelineAdapter, "validate_root", return_value=(True, "")),
        ):
            run = run_approved_dispatch(
                token.token_id,
                requested_by=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(success=True),
            )

        self.assertEqual(run.status, DispatchExecutionRunStatus.COMPLETED)
        self.assertTrue(run.repository2_touched)
        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCHED)
        self.assertTrue(ticket.execution_dispatched)
        self.assertTrue(ticket.repository2_touched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertTrue(plan.executed)
        self.assertTrue(plan.repository2_touched)
        self.assertTrue(ctx["token_store"].get(token.token_id).consumed)

    def test_adapter_failure_leaves_flags_and_token_unchanged(self) -> None:
        ctx = _seed_dispatch_stores()
        ticket = ctx["ticket"]
        plan = ctx["plan"]
        token = ctx["token"]
        before_ticket = (
            ticket.status,
            ticket.execution_dispatched,
            ticket.publish_dispatched,
            ticket.repository2_touched,
        )
        before_plan = (plan.executed, plan.repository2_touched)

        with patch.object(PipelineAdapter, "validate_root", return_value=(True, "")):
            run = run_approved_dispatch(
                token.token_id,
                requested_by=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(success=False),
            )

        self.assertEqual(run.status, DispatchExecutionRunStatus.FAILED)
        self.assertEqual(
            (
                ticket.status,
                ticket.execution_dispatched,
                ticket.publish_dispatched,
                ticket.repository2_touched,
            ),
            before_ticket,
        )
        self.assertEqual((plan.executed, plan.repository2_touched), before_plan)
        self.assertFalse(ctx["token_store"].get(token.token_id).consumed)

    def test_wrong_requester_raises(self) -> None:
        ctx = _seed_dispatch_stores()
        with self.assertRaises(ValueError) as exc:
            run_approved_dispatch(
                ctx["token"].token_id,
                requested_by="other-user",
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(),
            )
        self.assertIn("not authorized", str(exc.exception))

    def test_gate_not_approved_raises(self) -> None:
        ctx = _seed_dispatch_stores()
        gate = ctx["gate"]
        gate.status = ExecuteGateStatus.PENDING
        ctx["gate_store"].save(gate)

        with self.assertRaises(ValueError) as exc:
            run_approved_dispatch(
                ctx["token"].token_id,
                requested_by=ctx["ticket"].requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(),
            )
        self.assertIn("pending", str(exc.exception))

    def test_generation_mismatch_raises(self) -> None:
        ctx = _seed_dispatch_stores()
        ctx["ticket"].dispatch_generation = 99

        with self.assertRaises(ValueError) as exc:
            run_approved_dispatch(
                ctx["token"].token_id,
                requested_by=ctx["ticket"].requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(),
            )
        self.assertIn("generation", str(exc.exception))

    def test_stale_dispatch_request_token_id_raises(self) -> None:
        ctx = _seed_dispatch_stores()
        stale = DispatchExecutionRequest(
            dispatch_request_id=str(uuid.uuid4()),
            execute_request_id=ctx["token"].execute_request_id,
            gate_id=ctx["gate"].gate_id,
            ticket_id=ctx["ticket"].ticket_id,
            plan_id=ctx["plan"].plan_id,
            dry_run_run_id=ctx["token"].dry_run_run_id,
            unlock_token_id="stale-token-id",
            target_skills=list(ctx["token"].target_skills),
            requested_by=ctx["ticket"].requester_id,
            requested_at="2026-07-07T00:00:00+00:00",
        )
        store = DispatchExecutionRequestStore()
        store.save(stale)
        # Replace index by saving under same execute_request_id - actually store blocks duplicate
        # Instead mutate the saved request's unlock_token_id in a fresh store copy
        dispatch_store = ctx["dispatch_request_store"]
        saved = dispatch_store.get_by_execute_request(ctx["token"].execute_request_id)
        assert saved is not None
        mutated = DispatchExecutionRequest(
            dispatch_request_id=saved.dispatch_request_id,
            execute_request_id=saved.execute_request_id,
            gate_id=saved.gate_id,
            ticket_id=saved.ticket_id,
            plan_id=saved.plan_id,
            dry_run_run_id=saved.dry_run_run_id,
            unlock_token_id="stale-token-id",
            target_skills=saved.target_skills,
            requested_by=saved.requested_by,
            requested_at=saved.requested_at,
        )
        dispatch_store._requests[mutated.dispatch_request_id] = mutated

        with self.assertRaises(ValueError) as exc:
            run_approved_dispatch(
                ctx["token"].token_id,
                requested_by=ctx["ticket"].requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=dispatch_store,
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(),
            )
        self.assertIn("unlock_token_id", str(exc.exception))

    def test_same_dispatch_request_second_run_raises(self) -> None:
        ctx = _seed_dispatch_stores()
        with patch.object(PipelineAdapter, "validate_root", return_value=(True, "")):
            run_approved_dispatch(
                ctx["token"].token_id,
                requested_by=ctx["ticket"].requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(success=False),
            )

        with self.assertRaises(ValueError) as exc:
            start_dispatch_run(
                ctx["dispatch_request"],
                run_store=ctx["dispatch_run_store"],
            )
        self.assertIn("already exists", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
