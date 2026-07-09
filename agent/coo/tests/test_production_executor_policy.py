"""Tests for production executor policy (Phase 10L)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent.coo.execution_execute import ExecuteGateStatus
from agent.coo.execution_runtime import ExecutionRunStatus
from agent.coo.production_executor_confirmation import (
    create_production_executor_confirmation,
)
from agent.coo.production_executor_policy import (
    ProductionExecutorPolicy,
    assert_production_executor_allowed,
    get_default_production_executor_policy,
)
from agent.coo.tests.test_execution_dispatch_runtime import _approved_unlock_context


def _enabled_policy(*, pipeline_root: str) -> ProductionExecutorPolicy:
    return ProductionExecutorPolicy(
        enabled=True,
        allowed_pipeline_roots=(pipeline_root,),
    )


class TestProductionExecutorPolicy(unittest.TestCase):
    def test_default_policy_disabled(self) -> None:
        policy = get_default_production_executor_policy()
        self.assertFalse(policy.enabled)
        self.assertTrue(policy.requires_explicit_operator_confirmation)

    def test_disabled_policy_checklist_fails(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        from agent.coo.execution_dispatch_runtime import (
            DispatchExecutionRequest,
            DispatchUnlockTokenStore,
            create_dispatch_unlock_token,
        )

        token_store = DispatchUnlockTokenStore()
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
        dispatch_request = DispatchExecutionRequest(
            dispatch_request_id="req-1",
            execute_request_id=token.execute_request_id,
            gate_id=gate.gate_id,
            ticket_id=ticket.ticket_id,
            plan_id=plan.plan_id,
            dry_run_run_id=token.dry_run_run_id,
            unlock_token_id=token.token_id,
            target_skills=list(token.target_skills),
            requested_by=ticket.requester_id,
            requested_at="2026-07-07T00:00:00+00:00",
        )
        checklist = assert_production_executor_allowed(
            get_default_production_executor_policy(),
            ticket=ticket,
            plan=plan,
            dry_run=dry_run,
            gate=gate,
            token=token,
            dispatch_request=dispatch_request,
            pipeline_root="/tmp/fake-pipeline",
            entrypoint="node pipeline.js",
            target_skills=list(token.target_skills),
        )
        self.assertFalse(checklist["all_passed"])
        self.assertFalse(checklist["checks"][0]["passed"])

    def test_root_outside_allowlist_fails(self) -> None:
        ctx = self._policy_context("/tmp/allowed-root")
        policy = _enabled_policy(pipeline_root="/tmp/allowed-root")
        checklist = assert_production_executor_allowed(
            policy,
            **ctx,
            pipeline_root="/tmp/other-root",
            entrypoint="node pipeline.js",
            target_skills=["create_content"],
            confirmation=self._confirmation(ctx),
        )
        self.assertFalse(checklist["all_passed"])
        self.assertFalse(
            next(item for item in checklist["checks"] if item["name"] == "pipeline_root_allowed")[
                "passed"
            ]
        )

    def test_symlink_escape_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as allowed_dir:
            with tempfile.TemporaryDirectory() as outside_dir:
                link_path = os.path.join(allowed_dir, "escape-link")
                os.symlink(outside_dir, link_path)
                ctx = self._policy_context(allowed_dir)
                policy = _enabled_policy(pipeline_root=allowed_dir)
                checklist = assert_production_executor_allowed(
                    policy,
                    **ctx,
                    pipeline_root=link_path,
                    entrypoint="node pipeline.js",
                    target_skills=["create_content"],
                    confirmation=self._confirmation(ctx),
                )
                self.assertFalse(checklist["all_passed"])

    def test_entrypoint_not_allowed_fails(self) -> None:
        ctx = self._policy_context("/tmp/fake-pipeline")
        policy = _enabled_policy(pipeline_root="/tmp/fake-pipeline")
        checklist = assert_production_executor_allowed(
            policy,
            **ctx,
            pipeline_root="/tmp/fake-pipeline",
            entrypoint="node other.js",
            target_skills=["create_content"],
            confirmation=self._confirmation(ctx),
        )
        self.assertFalse(checklist["all_passed"])

    def test_npm_entrypoint_blocked(self) -> None:
        ctx = self._policy_context("/tmp/fake-pipeline")
        policy = _enabled_policy(pipeline_root="/tmp/fake-pipeline")
        checklist = assert_production_executor_allowed(
            policy,
            **ctx,
            pipeline_root="/tmp/fake-pipeline",
            entrypoint="npm run preflight:publish",
            target_skills=["create_content"],
            confirmation=self._confirmation(ctx),
        )
        self.assertFalse(checklist["all_passed"])

    def test_publish_target_blocked(self) -> None:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        from agent.coo.execution_dispatch_runtime import (
            DispatchExecutionRequest,
            DispatchUnlockToken,
        )

        token = DispatchUnlockToken(
            token_id="token-publish",
            ticket_id=ticket.ticket_id,
            plan_id=plan.plan_id,
            execute_request_id=execute_request.execute_request_id,
            gate_id=gate.gate_id,
            dry_run_run_id=dry_run.run_id,
            target_skills=("publish_content",),
            requested_by=ticket.requester_id,
            approved_by=gate.decided_by or ticket.requester_id,
            minted_at="2026-07-07T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00",
            dispatch_generation=ticket.dispatch_generation,
        )
        dispatch_request = DispatchExecutionRequest(
            dispatch_request_id="req-publish",
            execute_request_id=token.execute_request_id,
            gate_id=gate.gate_id,
            ticket_id=ticket.ticket_id,
            plan_id=plan.plan_id,
            dry_run_run_id=token.dry_run_run_id,
            unlock_token_id=token.token_id,
            target_skills=["publish_content"],
            requested_by=ticket.requester_id,
            requested_at="2026-07-07T00:00:00+00:00",
        )
        ctx = {
            "ticket": ticket,
            "plan": plan,
            "dry_run": dry_run,
            "gate": gate,
            "token": token,
            "dispatch_request": dispatch_request,
        }
        policy = _enabled_policy(pipeline_root="/tmp/fake-pipeline")
        checklist = assert_production_executor_allowed(
            policy,
            **ctx,
            pipeline_root="/tmp/fake-pipeline",
            entrypoint="node pipeline.js",
            target_skills=["publish_content"],
            confirmation=self._confirmation(ctx),
        )
        self.assertFalse(checklist["all_passed"])

    def test_gate_not_approved_fails(self) -> None:
        ctx = self._policy_context("/tmp/fake-pipeline")
        ctx["gate"].status = ExecuteGateStatus.PENDING
        policy = _enabled_policy(pipeline_root="/tmp/fake-pipeline")
        checklist = assert_production_executor_allowed(
            policy,
            **ctx,
            pipeline_root="/tmp/fake-pipeline",
            entrypoint="node pipeline.js",
            target_skills=["create_content"],
            confirmation=self._confirmation(ctx),
        )
        self.assertFalse(checklist["all_passed"])

    def test_token_consumed_fails(self) -> None:
        ctx = self._policy_context("/tmp/fake-pipeline")
        ctx["token"].consumed = True
        policy = _enabled_policy(pipeline_root="/tmp/fake-pipeline")
        checklist = assert_production_executor_allowed(
            policy,
            **ctx,
            pipeline_root="/tmp/fake-pipeline",
            entrypoint="node pipeline.js",
            target_skills=["create_content"],
            confirmation=self._confirmation(ctx),
        )
        self.assertFalse(checklist["all_passed"])

    def test_valid_policy_and_confirmation_passes(self) -> None:
        ctx = self._policy_context("/tmp/fake-pipeline")
        policy = _enabled_policy(pipeline_root="/tmp/fake-pipeline")
        checklist = assert_production_executor_allowed(
            policy,
            **ctx,
            pipeline_root="/tmp/fake-pipeline",
            entrypoint="node pipeline.js",
            target_skills=["create_content"],
            confirmation=self._confirmation(ctx),
        )
        self.assertTrue(checklist["all_passed"])

    def _policy_context(self, pipeline_root: str) -> dict:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
        from agent.coo.execution_dispatch_runtime import (
            DispatchExecutionRequest,
            DispatchUnlockTokenStore,
            create_dispatch_unlock_token,
        )

        token_store = DispatchUnlockTokenStore()
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
        dispatch_request = DispatchExecutionRequest(
            dispatch_request_id="req-policy",
            execute_request_id=token.execute_request_id,
            gate_id=gate.gate_id,
            ticket_id=ticket.ticket_id,
            plan_id=plan.plan_id,
            dry_run_run_id=token.dry_run_run_id,
            unlock_token_id=token.token_id,
            target_skills=list(token.target_skills),
            requested_by=ticket.requester_id,
            requested_at="2026-07-07T00:00:00+00:00",
        )
        return {
            "ticket": ticket,
            "plan": plan,
            "dry_run": dry_run,
            "gate": gate,
            "token": token,
            "dispatch_request": dispatch_request,
        }

    def _confirmation(self, ctx: dict):
        return create_production_executor_confirmation(
            ticket_id=ctx["ticket"].ticket_id,
            plan_id=ctx["plan"].plan_id,
            unlock_token_id=ctx["token"].token_id,
            dispatch_request_id=ctx["dispatch_request"].dispatch_request_id,
            operator_id="operator-1",
            operator_name="Operator One",
            confirmation_reason="policy test",
            confirmation_phrase="CONFIRM-REPOSITORY2-EXECUTION",
        )


if __name__ == "__main__":
    unittest.main()
