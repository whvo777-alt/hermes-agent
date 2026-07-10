"""Gateway dispatch bridge tests (Phase 10H)."""

from __future__ import annotations

import inspect
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.coo.dispatch_bundle_store import read_bundle
from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionRequestStore,
    DispatchExecutionRunStatus,
    DispatchExecutionRunStore,
    DispatchUnlockTokenStore,
    get_default_dispatch_execution_request_store,
    get_default_dispatch_execution_run_store,
    get_default_dispatch_unlock_token_store,
)
from agent.coo.execution_dispatcher import (
    ExecutionDispatchPlanStore,
    get_default_dispatch_plan_store,
)
from agent.coo.execution_execute import (
    ExecuteGateStatus,
    ExecuteGateStore,
    ExecuteRequest,
    ExecuteRequestStore,
    get_default_execute_gate_store,
    get_default_execute_request_store,
)
from agent.coo.execution_runtime import (
    ExecutionRequestStore,
    ExecutionRunStore,
    get_default_execution_request_store,
    get_default_execution_run_store,
)
from agent.coo.execution_ticket import (
    ExecutionTicketStatus,
    ExecutionTicketStore,
    get_default_ticket_store,
)
from agent.coo.gateway_execution_dispatch import (
    get_latest_dispatch_for_gateway_ticket,
    maybe_remint_dispatch_token_for_gateway_ticket,
    prepare_dispatch_for_gateway_session,
    prepare_dispatch_for_gateway_ticket,
    run_dispatch_for_gateway_request,
)
from agent.coo.gateway_execution_execute import (
    approve_execute_gate_for_gateway_request,
    create_execute_request_for_gateway_ticket,
)
from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig
from agent.coo.production_executor_confirmation import (
    ProductionExecutorConfirmationStore,
    REQUIRED_CONFIRMATION_PHRASE,
    create_production_executor_confirmation,
)
from agent.coo.production_executor_policy import ProductionExecutorPolicy
from agent.coo.tests.test_gateway_execution_execute import (
    _manual_plan,
    _manual_ticket,
    _seed_ticket_and_plan,
    _start_gateway_dry_run,
)


def _approve_gateway_execute(
    ticket,
    *,
    ticket_store: ExecutionTicketStore,
    plan_store: ExecutionDispatchPlanStore,
    run_store: ExecutionRunStore,
    dry_run_request_store: ExecutionRequestStore,
    execute_request_store: ExecuteRequestStore,
    gate_store: ExecuteGateStore,
) -> str:
    execute_result = create_execute_request_for_gateway_ticket(
        ticket.ticket_id,
        requester_id=ticket.requester_id,
        ticket_store=ticket_store,
        plan_store=plan_store,
        run_store=run_store,
        dry_run_request_store=dry_run_request_store,
        execute_request_store=execute_request_store,
        gate_store=gate_store,
    )
    approve_execute_gate_for_gateway_request(
        execute_result["execute_request"]["execute_request_id"],
        reviewer_id=ticket.requester_id,
        gate_store=gate_store,
        ticket_store=ticket_store,
    )
    return execute_result["execute_request"]["execute_request_id"]


def _seed_approved_dispatch_pipeline(
    *,
    dispatchable_skills: list[str] | None = None,
) -> dict:
    ticket_store = ExecutionTicketStore()
    plan_store = ExecutionDispatchPlanStore()
    run_store = ExecutionRunStore()
    dry_run_request_store = ExecutionRequestStore()
    execute_request_store = ExecuteRequestStore()
    gate_store = ExecuteGateStore()
    token_store = DispatchUnlockTokenStore()
    dispatch_request_store = DispatchExecutionRequestStore()
    dispatch_run_store = DispatchExecutionRunStore()

    ticket, plan = _seed_ticket_and_plan(
        ticket_store=ticket_store,
        plan_store=plan_store,
        dispatchable_skills=dispatchable_skills,
    )
    _start_gateway_dry_run(
        ticket,
        ticket_store=ticket_store,
        plan_store=plan_store,
        run_store=run_store,
        request_store=dry_run_request_store,
    )
    _approve_gateway_execute(
        ticket,
        ticket_store=ticket_store,
        plan_store=plan_store,
        run_store=run_store,
        dry_run_request_store=dry_run_request_store,
        execute_request_store=execute_request_store,
        gate_store=gate_store,
    )

    return {
        "ticket": ticket,
        "plan": plan,
        "ticket_store": ticket_store,
        "plan_store": plan_store,
        "run_store": run_store,
        "dry_run_request_store": dry_run_request_store,
        "execute_request_store": execute_request_store,
        "gate_store": gate_store,
        "token_store": token_store,
        "dispatch_request_store": dispatch_request_store,
        "dispatch_run_store": dispatch_run_store,
    }


def _fake_adapter(success: bool = True) -> PipelineAdapter:
    def executor(command: str, pipeline_root: str, run_date: str) -> tuple[int, str, str]:
        if success:
            return 0, "ok", ""
        return 1, "", "fake failure"

    return PipelineAdapter(
        PipelineAdapterConfig(allow_execute=True, pipeline_root="/tmp/fake-pipeline"),
        executor=executor,
    )


class TestGatewayDispatchBridge(unittest.TestCase):
    def setUp(self) -> None:
        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()
        get_default_execution_run_store().clear()
        get_default_execution_request_store().clear()
        get_default_execute_request_store().clear()
        get_default_execute_gate_store().clear()
        get_default_dispatch_unlock_token_store().clear()
        get_default_dispatch_execution_request_store().clear()
        get_default_dispatch_execution_run_store().clear()

    def test_prepare_happy_path(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = prepare_dispatch_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                run_store=ctx["run_store"],
                dry_run_request_store=ctx["dry_run_request_store"],
                execute_request_store=ctx["execute_request_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
            )

        self.assertIn("token_id", result["unlock_token"])
        self.assertIn("dispatch_request_id", result["dispatch_request"])
        self.assertEqual(result["unlock_token"]["target_skills"], ["create_content"])
        self.assertFalse(result["unlock_token"]["consumed"])

    def test_prepare_idempotent(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        kwargs = {
            "requester_id": ticket.requester_id,
            "ticket_store": ctx["ticket_store"],
            "plan_store": ctx["plan_store"],
            "run_store": ctx["run_store"],
            "dry_run_request_store": ctx["dry_run_request_store"],
            "execute_request_store": ctx["execute_request_store"],
            "gate_store": ctx["gate_store"],
            "token_store": ctx["token_store"],
            "dispatch_request_store": ctx["dispatch_request_store"],
        }
        first = prepare_dispatch_for_gateway_ticket(ticket.ticket_id, **kwargs)
        second = prepare_dispatch_for_gateway_ticket(ticket.ticket_id, **kwargs)
        self.assertEqual(
            first["unlock_token"]["token_id"],
            second["unlock_token"]["token_id"],
        )

    def test_prepare_wrong_requester_raises(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        with self.assertRaises(ValueError) as exc:
            prepare_dispatch_for_gateway_ticket(
                ctx["ticket"].ticket_id,
                requester_id="other-user",
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                run_store=ctx["run_store"],
                dry_run_request_store=ctx["dry_run_request_store"],
                execute_request_store=ctx["execute_request_store"],
                gate_store=ctx["gate_store"],
            )
        self.assertIn("not authorized", str(exc.exception))

    def test_prepare_gate_not_approved_raises(self) -> None:
        from agent.coo.execution_execute import (
            create_execute_gate,
            create_execute_request_from_dry_run,
        )

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
        dry_run_result = _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )
        dry_run = run_store.get(dry_run_result["run"]["run_id"])
        dry_run_request = dry_run_request_store.get(dry_run_result["request"]["request_id"])
        assert dry_run is not None and dry_run_request is not None

        execute_request = create_execute_request_from_dry_run(
            dry_run,
            dry_run_request,
            plan,
            ticket,
            requested_by=ticket.requester_id,
            request_store=execute_request_store,
        )
        create_execute_gate(execute_request, gate_store=gate_store)

        with self.assertRaises(ValueError) as exc:
            prepare_dispatch_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=run_store,
                dry_run_request_store=dry_run_request_store,
                execute_request_store=execute_request_store,
                gate_store=gate_store,
            )
        self.assertIn("pending", str(exc.exception))

    def test_prepare_no_execute_request_raises(self) -> None:
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

        with self.assertRaises(KeyError) as exc:
            prepare_dispatch_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=run_store,
                dry_run_request_store=dry_run_request_store,
                execute_request_store=ExecuteRequestStore(),
                gate_store=ExecuteGateStore(),
            )
        self.assertIn("Execute request not found", str(exc.exception))

    def test_prepare_empty_target_skills_raises(self) -> None:
        from agent.coo.execution_execute import (
            ExecuteGateStore,
            ExecuteRequestStore,
            approve_execute_gate,
            create_execute_gate,
            create_execute_request_from_dry_run,
        )

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
        dry_run_result = _start_gateway_dry_run(
            ticket,
            ticket_store=ticket_store,
            plan_store=plan_store,
            run_store=run_store,
            request_store=dry_run_request_store,
        )
        dry_run = run_store.get(dry_run_result["run"]["run_id"])
        dry_run_request = dry_run_request_store.get(dry_run_result["request"]["request_id"])
        assert dry_run is not None and dry_run_request is not None

        execute_request = create_execute_request_from_dry_run(
            dry_run,
            dry_run_request,
            plan,
            ticket,
            requested_by=ticket.requester_id,
            request_store=execute_request_store,
        )
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
            target_skills=[],
            auto_apply=execute_request.auto_apply,
            review_required=execute_request.review_required,
        )
        execute_request_store.save(execute_request)
        gate = create_execute_gate(execute_request, gate_store=gate_store)
        approve_execute_gate(gate.gate_id, ticket.requester_id, gate_store=gate_store)

        with self.assertRaises(ValueError) as exc:
            prepare_dispatch_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ticket_store,
                plan_store=plan_store,
                run_store=run_store,
                dry_run_request_store=dry_run_request_store,
                execute_request_store=execute_request_store,
                gate_store=gate_store,
            )
        self.assertIn("target skills", str(exc.exception).lower())

    def test_prepare_session_delegates(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        result = prepare_dispatch_for_gateway_session(
            ticket.approval_session_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )
        self.assertIn("unlock_token", result)

    def test_remint_expired_token(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )
        old_token_id = prepare["unlock_token"]["token_id"]
        old_token = ctx["token_store"].get(old_token_id)
        assert old_token is not None
        old_token.expires_at = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        ctx["token_store"].save(old_token)

        remint = maybe_remint_dispatch_token_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
        )

        self.assertTrue(remint["reminted"])
        self.assertNotEqual(remint["unlock_token"]["token_id"], old_token_id)
        superseded = ctx["token_store"].get(old_token_id)
        assert superseded is not None
        self.assertTrue(superseded.superseded)

    def test_remint_consumed_token_raises(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )
        token = ctx["token_store"].get(prepare["unlock_token"]["token_id"])
        assert token is not None
        token.consumed = True
        ctx["token_store"].save(token)

        with self.assertRaises(ValueError) as exc:
            maybe_remint_dispatch_token_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                run_store=ctx["run_store"],
                dry_run_request_store=ctx["dry_run_request_store"],
                execute_request_store=ctx["execute_request_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
            )
        self.assertIn("consumed", str(exc.exception))

    def test_get_latest_dispatch_returns_state(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )

        latest = get_latest_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
            dispatch_run_store=ctx["dispatch_run_store"],
        )

        assert latest is not None
        self.assertTrue(latest["can_prepare"])
        self.assertTrue(latest["token_usable"])
        self.assertTrue(latest["can_run"])
        self.assertIsNotNone(latest["unlock_token"])
        self.assertIsNotNone(latest["dispatch_request"])

    def test_run_default_adapter_fails_without_real_execution(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        plan = ctx["plan"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )
        before_ticket = (
            ticket.status,
            ticket.execution_dispatched,
            plan.executed,
        )

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch.object(PipelineAdapter, "validate_root", return_value=(True, "")),
        ):
            result = run_dispatch_for_gateway_request(
                unlock_token_id=prepare["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
            )

        self.assertEqual(
            result["dispatch_run"]["status"],
            DispatchExecutionRunStatus.FAILED.value,
        )
        self.assertEqual(
            (ticket.status, ticket.execution_dispatched, plan.executed),
            before_ticket,
        )
        self.assertFalse(ctx["token_store"].get(prepare["unlock_token"]["token_id"]).consumed)

    def test_run_fake_adapter_completes_and_sets_flags(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch.object(PipelineAdapter, "validate_root", return_value=(True, "")),
        ):
            result = run_dispatch_for_gateway_request(
                unlock_token_id=prepare["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(success=True),
            )

        self.assertEqual(
            result["dispatch_run"]["status"],
            DispatchExecutionRunStatus.COMPLETED.value,
        )
        self.assertEqual(ticket.status, ExecutionTicketStatus.DISPATCHED)
        self.assertTrue(ticket.execution_dispatched)
        self.assertFalse(ticket.publish_dispatched)
        self.assertTrue(ctx["plan"].executed)

    def test_run_fake_failure_leaves_flags_unchanged(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        plan = ctx["plan"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )
        before = (ticket.status, ticket.execution_dispatched, plan.executed)

        with patch.object(PipelineAdapter, "validate_root", return_value=(True, "")):
            result = run_dispatch_for_gateway_request(
                unlock_token_id=prepare["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(success=False),
            )

        self.assertEqual(result["dispatch_run"]["status"], DispatchExecutionRunStatus.FAILED.value)
        self.assertEqual((ticket.status, ticket.execution_dispatched, plan.executed), before)

    def test_prepare_after_failed_run_recovers_can_run(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        kwargs = {
            "requester_id": ticket.requester_id,
            "ticket_store": ctx["ticket_store"],
            "plan_store": ctx["plan_store"],
            "run_store": ctx["run_store"],
            "dry_run_request_store": ctx["dry_run_request_store"],
            "execute_request_store": ctx["execute_request_store"],
            "gate_store": ctx["gate_store"],
            "token_store": ctx["token_store"],
            "dispatch_request_store": ctx["dispatch_request_store"],
            "dispatch_run_store": ctx["dispatch_run_store"],
        }
        prepare1 = prepare_dispatch_for_gateway_ticket(ticket.ticket_id, **kwargs)

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch.object(PipelineAdapter, "validate_root", return_value=(True, "")),
        ):
            run_dispatch_for_gateway_request(
                unlock_token_id=prepare1["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
            )

        blocked = get_latest_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
            dispatch_run_store=ctx["dispatch_run_store"],
        )
        assert blocked is not None
        self.assertFalse(blocked["can_run"])

        prepare2 = prepare_dispatch_for_gateway_ticket(ticket.ticket_id, **kwargs)
        old_request_id = prepare1["dispatch_request"]["dispatch_request_id"]

        self.assertNotEqual(
            prepare2["dispatch_request"]["dispatch_request_id"],
            old_request_id,
        )
        self.assertEqual(
            prepare2["dispatch_request"]["unlock_token_id"],
            prepare2["unlock_token"]["token_id"],
        )
        self.assertTrue(ctx["dispatch_request_store"].is_superseded(old_request_id))
        self.assertEqual(
            ctx["dispatch_request_store"].get(old_request_id).unlock_token_id,
            prepare1["unlock_token"]["token_id"],
        )

        recovered = get_latest_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
            dispatch_run_store=ctx["dispatch_run_store"],
        )
        assert recovered is not None
        self.assertTrue(recovered["can_run"])

        with patch.object(PipelineAdapter, "validate_root", return_value=(True, "")):
            success = run_dispatch_for_gateway_request(
                unlock_token_id=prepare2["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(success=True),
            )

        self.assertEqual(
            success["dispatch_run"]["status"],
            DispatchExecutionRunStatus.COMPLETED.value,
        )

    def test_prepare_after_completed_run_keeps_request(self) -> None:
        from agent.coo.execution_dispatch_runtime import DispatchExecutionRun

        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        kwargs = {
            "requester_id": ticket.requester_id,
            "ticket_store": ctx["ticket_store"],
            "plan_store": ctx["plan_store"],
            "run_store": ctx["run_store"],
            "dry_run_request_store": ctx["dry_run_request_store"],
            "execute_request_store": ctx["execute_request_store"],
            "gate_store": ctx["gate_store"],
            "token_store": ctx["token_store"],
            "dispatch_request_store": ctx["dispatch_request_store"],
            "dispatch_run_store": ctx["dispatch_run_store"],
        }
        prepare1 = prepare_dispatch_for_gateway_ticket(ticket.ticket_id, **kwargs)
        request_id = prepare1["dispatch_request"]["dispatch_request_id"]
        ctx["dispatch_run_store"].save(
            DispatchExecutionRun(
                dispatch_run_id=str(uuid.uuid4()),
                dispatch_request_id=request_id,
                ticket_id=ticket.ticket_id,
                plan_id=ctx["plan"].plan_id,
                status=DispatchExecutionRunStatus.COMPLETED,
                dry_run=False,
                summary="Dispatch completed successfully.",
            )
        )

        prepare2 = prepare_dispatch_for_gateway_ticket(ticket.ticket_id, **kwargs)
        self.assertEqual(
            prepare1["dispatch_request"]["dispatch_request_id"],
            prepare2["dispatch_request"]["dispatch_request_id"],
        )
        self.assertFalse(ctx["dispatch_request_store"].is_superseded(request_id))

    def test_prepare_after_running_run_keeps_request(self) -> None:
        from agent.coo.execution_dispatch_runtime import DispatchExecutionRun

        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        kwargs = {
            "requester_id": ticket.requester_id,
            "ticket_store": ctx["ticket_store"],
            "plan_store": ctx["plan_store"],
            "run_store": ctx["run_store"],
            "dry_run_request_store": ctx["dry_run_request_store"],
            "execute_request_store": ctx["execute_request_store"],
            "gate_store": ctx["gate_store"],
            "token_store": ctx["token_store"],
            "dispatch_request_store": ctx["dispatch_request_store"],
            "dispatch_run_store": ctx["dispatch_run_store"],
        }
        prepare1 = prepare_dispatch_for_gateway_ticket(ticket.ticket_id, **kwargs)
        request_id = prepare1["dispatch_request"]["dispatch_request_id"]
        ctx["dispatch_run_store"].save(
            DispatchExecutionRun(
                dispatch_run_id=str(uuid.uuid4()),
                dispatch_request_id=request_id,
                ticket_id=ticket.ticket_id,
                plan_id=ctx["plan"].plan_id,
                status=DispatchExecutionRunStatus.RUNNING,
                dry_run=False,
                summary="Dispatch in progress.",
            )
        )

        prepare2 = prepare_dispatch_for_gateway_ticket(ticket.ticket_id, **kwargs)
        self.assertEqual(
            prepare1["dispatch_request"]["dispatch_request_id"],
            prepare2["dispatch_request"]["dispatch_request_id"],
        )
        self.assertFalse(ctx["dispatch_request_store"].is_superseded(request_id))

    def test_second_run_same_dispatch_request_raises(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )

        with patch.object(PipelineAdapter, "validate_root", return_value=(True, "")):
            run_dispatch_for_gateway_request(
                dispatch_request_id=prepare["dispatch_request"]["dispatch_request_id"],
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(success=False),
            )

        with self.assertRaises(ValueError):
            run_dispatch_for_gateway_request(
                dispatch_request_id=prepare["dispatch_request"]["dispatch_request_id"],
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(success=False),
            )

    def test_generation_mismatch_raises_on_run(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )
        ticket.dispatch_generation = 99

        with patch.object(PipelineAdapter, "validate_root", return_value=(True, "")):
            with self.assertRaises(ValueError) as exc:
                run_dispatch_for_gateway_request(
                    unlock_token_id=prepare["unlock_token"]["token_id"],
                    requester_id=ticket.requester_id,
                    ticket_store=ctx["ticket_store"],
                    plan_store=ctx["plan_store"],
                    gate_store=ctx["gate_store"],
                    token_store=ctx["token_store"],
                    dispatch_request_store=ctx["dispatch_request_store"],
                    dispatch_run_store=ctx["dispatch_run_store"],
                    adapter=_fake_adapter(success=True),
                )
        self.assertIn("generation", str(exc.exception))

    def test_stale_dispatch_request_after_remint_blocks_can_run_until_prepare(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )
        old_token_id = prepare["unlock_token"]["token_id"]
        old_request_id = prepare["dispatch_request"]["dispatch_request_id"]
        old_token = ctx["token_store"].get(old_token_id)
        assert old_token is not None
        old_token.expires_at = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        ctx["token_store"].save(old_token)
        remint = maybe_remint_dispatch_token_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
        )

        latest = get_latest_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
            dispatch_run_store=ctx["dispatch_run_store"],
        )
        assert latest is not None
        self.assertFalse(latest["can_run"])
        self.assertNotEqual(
            latest["dispatch_request"]["unlock_token_id"],
            remint["unlock_token"]["token_id"],
        )

        with patch.object(PipelineAdapter, "validate_root", return_value=(True, "")):
            with self.assertRaises(ValueError) as exc:
                run_dispatch_for_gateway_request(
                    unlock_token_id=remint["unlock_token"]["token_id"],
                    requester_id=ticket.requester_id,
                    ticket_store=ctx["ticket_store"],
                    plan_store=ctx["plan_store"],
                    gate_store=ctx["gate_store"],
                    token_store=ctx["token_store"],
                    dispatch_request_store=ctx["dispatch_request_store"],
                    dispatch_run_store=ctx["dispatch_run_store"],
                    adapter=_fake_adapter(success=True),
                )
        self.assertIn("unlock_token_id", str(exc.exception))

        with patch.object(PipelineAdapter, "validate_root", return_value=(True, "")):
            with self.assertRaises(ValueError) as exc:
                run_dispatch_for_gateway_request(
                    dispatch_request_id=old_request_id,
                    requester_id=ticket.requester_id,
                    ticket_store=ctx["ticket_store"],
                    plan_store=ctx["plan_store"],
                    gate_store=ctx["gate_store"],
                    token_store=ctx["token_store"],
                    dispatch_request_store=ctx["dispatch_request_store"],
                    dispatch_run_store=ctx["dispatch_run_store"],
                    adapter=_fake_adapter(success=True),
                )
        self.assertIn("superseded", str(exc.exception).lower())

    def test_remint_then_prepare_realigns_dispatch_request(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )
        old_token_id = prepare["unlock_token"]["token_id"]
        old_request_id = prepare["dispatch_request"]["dispatch_request_id"]
        old_token = ctx["token_store"].get(old_token_id)
        assert old_token is not None
        old_token.expires_at = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        ctx["token_store"].save(old_token)

        maybe_remint_dispatch_token_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
        )

        reprepared = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )

        self.assertNotEqual(
            reprepared["dispatch_request"]["dispatch_request_id"],
            old_request_id,
        )
        self.assertEqual(
            reprepared["dispatch_request"]["unlock_token_id"],
            reprepared["unlock_token"]["token_id"],
        )
        self.assertEqual(
            ctx["dispatch_request_store"].get(old_request_id).unlock_token_id,
            old_token_id,
        )
        self.assertTrue(
            ctx["dispatch_request_store"].is_superseded(old_request_id),
        )

        latest = get_latest_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
            dispatch_run_store=ctx["dispatch_run_store"],
        )
        assert latest is not None
        self.assertTrue(latest["can_run"])

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch.object(PipelineAdapter, "validate_root", return_value=(True, "")),
        ):
            result = run_dispatch_for_gateway_request(
                unlock_token_id=reprepared["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                adapter=_fake_adapter(success=True),
            )

        self.assertEqual(
            result["dispatch_run"]["status"],
            DispatchExecutionRunStatus.COMPLETED.value,
        )

    def test_module_has_no_terminal_tool_or_subprocess(self) -> None:
        import agent.coo.gateway_execution_dispatch as module

        source = inspect.getsource(module)
        self.assertNotIn("terminal_tool", source)
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("subprocess.run", source)
        self.assertNotIn("subprocess.Popen", source)

    def test_discord_dispatch_wired_with_lazy_import_only(self) -> None:
        discord_path = (
            Path(__file__).resolve().parents[3]
            / "plugins/platforms/discord/coo_approval.py"
        )
        source = discord_path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith("from agent.coo.gateway_execution_dispatch import"):
                self.fail(
                    "gateway_execution_dispatch must be imported lazily inside functions"
                )
            if line.startswith("import agent.coo.gateway_execution_dispatch"):
                self.fail(
                    "gateway_execution_dispatch must be imported lazily inside functions"
                )
        self.assertIn("prepare_dispatch", source)
        self.assertIn("run_approved_plan", source)
        self.assertIn("remint_dispatch_token", source)


class TestGatewayDispatchProductionPolicyThreading(unittest.TestCase):
    def setUp(self) -> None:
        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()
        get_default_execution_run_store().clear()
        get_default_execution_request_store().clear()
        get_default_execute_request_store().clear()
        get_default_execute_gate_store().clear()
        get_default_dispatch_unlock_token_store().clear()
        get_default_dispatch_execution_request_store().clear()
        get_default_dispatch_execution_run_store().clear()

    def test_run_without_production_policy_threads_none_defaults(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )
        fake_run = MagicMock()
        fake_run.to_dict.return_value = {"status": "failed", "summary": "stub"}

        with patch(
            "agent.coo.gateway_execution_dispatch.run_approved_dispatch",
            return_value=fake_run,
        ) as mock_run:
            run_dispatch_for_gateway_request(
                unlock_token_id=prepare["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
            )

        kwargs = mock_run.call_args.kwargs
        self.assertIsNone(kwargs.get("production_policy"))
        self.assertIsNone(kwargs.get("confirmation"))
        self.assertIsNone(kwargs.get("confirmation_store"))
        self.assertIsNone(kwargs.get("audit_dir"))
        self.assertIsNone(kwargs.get("dry_run_store"))

    def test_run_threads_production_policy_confirmation_and_audit_dir(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
        )
        policy = ProductionExecutorPolicy(
            enabled=True,
            allowed_pipeline_roots=("/tmp/fake-pipeline",),
        )
        confirmation_store = ProductionExecutorConfirmationStore()
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=ctx["plan"].plan_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            dispatch_request_id=prepare["dispatch_request"]["dispatch_request_id"],
            operator_id="operator-1",
            operator_name="Operator One",
            confirmation_reason="gateway threading test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            confirmation_store=confirmation_store,
        )
        audit_dir = Path("/tmp/hermes-audit-threading")
        dry_run_store = ctx["run_store"]
        fake_run = MagicMock()
        fake_run.to_dict.return_value = {"status": "failed", "summary": "stub"}

        with patch(
            "agent.coo.gateway_execution_dispatch.run_approved_dispatch",
            return_value=fake_run,
        ) as mock_run:
            run_dispatch_for_gateway_request(
                unlock_token_id=prepare["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                dispatch_run_store=ctx["dispatch_run_store"],
                dry_run_store=dry_run_store,
                production_policy=policy,
                confirmation=confirmation,
                confirmation_store=confirmation_store,
                audit_dir=audit_dir,
            )

        kwargs = mock_run.call_args.kwargs
        self.assertIs(kwargs["production_policy"], policy)
        self.assertIs(kwargs["confirmation"], confirmation)
        self.assertIs(kwargs["confirmation_store"], confirmation_store)
        self.assertEqual(kwargs["audit_dir"], audit_dir)
        self.assertIs(kwargs["dry_run_store"], dry_run_store)


class TestGatewayDispatchBundlePersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.get_home_patch = patch(
            "agent.coo.dispatch_bundle_store.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.get_home_patch.start()

        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()
        get_default_execution_run_store().clear()
        get_default_execution_request_store().clear()
        get_default_execute_request_store().clear()
        get_default_execute_gate_store().clear()
        get_default_dispatch_unlock_token_store().clear()
        get_default_dispatch_execution_request_store().clear()
        get_default_dispatch_execution_run_store().clear()

    def tearDown(self) -> None:
        self.get_home_patch.stop()
        self.tmp.cleanup()

    def test_prepare_writes_bundle_file(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = prepare_dispatch_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                run_store=ctx["run_store"],
                dry_run_request_store=ctx["dry_run_request_store"],
                execute_request_store=ctx["execute_request_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                bundle_dir=self.bundle_dir,
            )
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        self.assertEqual(bundle.unlock_token_id, result["unlock_token"]["token_id"])
        self.assertEqual(
            bundle.dispatch_request_id,
            result["dispatch_request"]["dispatch_request_id"],
        )

    def test_remint_then_prepare_updates_bundle(self) -> None:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        prepare = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
            bundle_dir=self.bundle_dir,
        )
        old_token_id = prepare["unlock_token"]["token_id"]
        old_token = ctx["token_store"].get(old_token_id)
        assert old_token is not None
        old_token.expires_at = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        ctx["token_store"].save(old_token)

        remint = maybe_remint_dispatch_token_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            bundle_dir=self.bundle_dir,
        )
        after_remint = read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        self.assertEqual(after_remint.unlock_token_id, remint["unlock_token"]["token_id"])
        self.assertTrue(after_remint.snapshot.get("_remint_pending_prepare"))

        reprepared = prepare_dispatch_for_gateway_ticket(
            ticket.ticket_id,
            requester_id=ticket.requester_id,
            ticket_store=ctx["ticket_store"],
            plan_store=ctx["plan_store"],
            run_store=ctx["run_store"],
            dry_run_request_store=ctx["dry_run_request_store"],
            execute_request_store=ctx["execute_request_store"],
            gate_store=ctx["gate_store"],
            token_store=ctx["token_store"],
            dispatch_request_store=ctx["dispatch_request_store"],
            bundle_dir=self.bundle_dir,
        )
        after_prepare = read_bundle(ticket.ticket_id, bundle_dir=self.bundle_dir)
        self.assertEqual(
            after_prepare.unlock_token_id,
            reprepared["unlock_token"]["token_id"],
        )
        self.assertEqual(
            after_prepare.dispatch_request_id,
            reprepared["dispatch_request"]["dispatch_request_id"],
        )
        self.assertNotIn("_remint_pending_prepare", after_prepare.snapshot)


if __name__ == "__main__":
    unittest.main()
