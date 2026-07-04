"""Unit tests for Hermes COO orchestration layer."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from agent.coo.execution_planner import ExecutionPlanner
from agent.coo.execution_policy import ExecutionPolicy
from agent.coo.intent_analysis import IntentAnalyzer
from agent.coo.models import (
    ApprovalSnapshot,
    PipelineState,
    PlanPhase,
    PolicyVerdict,
    PublishingSnapshot,
    RuntimeSnapshot,
    SkillInvocationStatus,
    TaskKind,
    WorkerStatus,
)
from agent.coo.orchestrator import COOOrchestrator
from agent.coo.skill_selection import SkillSelector
from agent.coo.worker_manager import WorkerManager


class TestIntentAnalyzer(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = IntentAnalyzer()

    def test_create_and_report_korean(self) -> None:
        intent = self.analyzer.analyze("오늘 블로그 글 작성해서 보고해")
        self.assertEqual(intent.task_kind, TaskKind.CREATE_AND_REPORT)
        self.assertGreaterEqual(intent.confidence, 0.7)

    def test_approve_and_publish_korean(self) -> None:
        intent = self.analyzer.analyze("승인하고 발행해")
        self.assertEqual(intent.task_kind, TaskKind.APPROVE_AND_PUBLISH)
        self.assertGreaterEqual(intent.confidence, 0.9)


class TestExecutionPlanner(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = ExecutionPlanner()
        self.analyzer = IntentAnalyzer()

    def test_create_plan_includes_publish_wait(self) -> None:
        intent = self.analyzer.analyze("오늘 블로그 글 작성해서 보고해", run_date="2026-07-04")
        plan = self.planner.plan(intent)
        phases = [step.phase for step in plan.steps]
        self.assertIn(PlanPhase.APPROVAL_QUEUE, phases)
        self.assertIn(PlanPhase.PUBLISH_WAIT, phases)
        self.assertEqual(plan.run_date, "2026-07-04")


class TestExecutionPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ExecutionPolicy()
        self.planner = ExecutionPlanner()
        self.analyzer = IntentAnalyzer()

    def test_publish_blocked_without_approved_items(self) -> None:
        intent = self.analyzer.analyze("승인하고 발행해", run_date="2026-07-04")
        plan = self.planner.plan(intent)
        state = PipelineState(
            run_date="2026-07-04",
            pipeline_root="/opt/data/multi-content-pipeline",
            approvals=ApprovalSnapshot(
                total=2,
                pending=2,
                approved=0,
                queue_exists=True,
            ),
        )
        decision = self.policy.evaluate(plan, state)
        self.assertIn(PlanPhase.PUBLISHER, decision.blocked_phases)
        self.assertFalse(decision.auto_apply)
        self.assertTrue(decision.review_required)

    def test_safeguard_block_rule_forces_block_verdict(self) -> None:
        from agent.coo.config import COOConfig, COOPolicyDefaults

        bad_policy = ExecutionPolicy(
            COOConfig(
                pipeline_root="/opt/data/multi-content-pipeline",
                policy=COOPolicyDefaults(auto_apply=True, review_required=True),
            )
        )
        intent = self.analyzer.analyze("오늘 상태 보고해", run_date="2026-07-04")
        plan = self.planner.plan(intent)
        state = PipelineState(
            run_date="2026-07-04",
            pipeline_root="/opt/data/multi-content-pipeline",
        )
        decision = bad_policy.evaluate(plan, state)
        self.assertEqual(decision.verdict, PolicyVerdict.BLOCK)
        self.assertTrue(
            any(
                rule.rule == "safeguard_defaults" and rule.verdict is PolicyVerdict.BLOCK
                for rule in decision.rules
            )
        )

    def test_publish_deferred_when_scheduler_active(self) -> None:
        intent = self.analyzer.analyze("승인하고 발행해", run_date="2026-07-04")
        plan = self.planner.plan(intent)
        state = PipelineState(
            run_date="2026-07-04",
            pipeline_root="/opt/data/multi-content-pipeline",
            approvals=ApprovalSnapshot(
                total=2,
                pending=0,
                approved=2,
                queue_exists=True,
            ),
            publishing=PublishingSnapshot(confirmed_queue_exists=True, confirmed_item_count=1),
            runtime=RuntimeSnapshot(scheduler_active=True, scheduler_state="active"),
        )
        decision = self.policy.evaluate(plan, state)
        self.assertIn(PlanPhase.PUBLISHER, decision.deferred_phases)


class TestSkillSelection(unittest.TestCase):
    def test_selects_create_content_for_create_task(self) -> None:
        orchestrator = COOOrchestrator()
        result = orchestrator.orchestrate(
            "오늘 블로그 글 작성해서 보고해",
            run_date="2026-07-04",
        )
        selected_ids = {
            skill.skill_id
            for skill in result.skills
            if skill.status is SkillInvocationStatus.SELECTED
        }
        self.assertIn("create_content", selected_ids)
        self.assertIn("approval_review", selected_ids)
        publish_skills = [
            skill for skill in result.skills if skill.skill_id == "publish_content"
        ]
        self.assertEqual(publish_skills, [])


class TestOrchestrator(unittest.TestCase):
    def test_orchestrate_returns_serializable_payload(self) -> None:
        orchestrator = COOOrchestrator()
        result = orchestrator.orchestrate("오늘 상태 보고해", run_date="2026-07-04")
        payload = result.to_dict()
        self.assertEqual(payload["intent"]["task_kind"], TaskKind.DAILY_BRIEF.value)
        self.assertEqual(payload["policy"]["auto_apply"], False)
        self.assertEqual(payload["policy"]["review_required"], True)
        self.assertTrue(result.ceo_message)

    def test_accepts_optional_dependencies(self) -> None:
        intent_analyzer = MagicMock()
        execution_planner = MagicMock()
        execution_policy = MagicMock()
        skill_selector = MagicMock()
        pipeline_state_reader = MagicMock()

        orchestrator = COOOrchestrator(
            intent_analyzer=intent_analyzer,
            execution_planner=execution_planner,
            execution_policy=execution_policy,
            skill_selector=skill_selector,
            pipeline_state_reader=pipeline_state_reader,
        )

        self.assertIs(orchestrator._intent, intent_analyzer)
        self.assertIs(orchestrator._planner, execution_planner)
        self.assertIs(orchestrator._policy, execution_policy)
        self.assertIs(orchestrator._selector, skill_selector)
        self.assertIs(orchestrator._state_reader, pipeline_state_reader)


class TestWorkerManager(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = WorkerManager()

    def test_worker_status_includes_cancelled(self) -> None:
        self.assertIn("cancelled", {status.value for status in WorkerStatus})

    def test_create_and_report_staffs_department_workers(self) -> None:
        orchestrator = COOOrchestrator()
        result = orchestrator.orchestrate(
            "오늘 블로그 글 작성해서 보고해",
            run_date="2026-07-04",
        )
        worker_ids = [assignment.worker_id for assignment in result.worker_assignments]
        self.assertEqual(
            worker_ids,
            [
                "research_worker",
                "strategy_worker",
                "draft_worker",
                "quality_worker",
                "approval_worker",
                "reporting_worker",
            ],
        )
        departments = [assignment.department_id for assignment in result.worker_assignments]
        self.assertEqual(
            departments,
            ["research", "strategy", "writing", "quality", "approval", "reporting"],
        )
        for assignment in result.worker_assignments:
            self.assertFalse(assignment.auto_apply)
            self.assertTrue(assignment.review_required)

    def test_approve_and_publish_staffs_approval_and_publisher(self) -> None:
        orchestrator = COOOrchestrator()
        result = orchestrator.orchestrate(
            "승인하고 발행해",
            run_date="2026-07-04",
        )
        worker_ids = [assignment.worker_id for assignment in result.worker_assignments]
        self.assertEqual(worker_ids, ["approval_worker", "publisher_worker"])

    def test_blocked_phase_produces_blocked_assignment(self) -> None:
        orchestrator = COOOrchestrator()
        result = orchestrator.orchestrate(
            "승인하고 발행해",
            run_date="2026-07-04",
        )
        publisher = next(
            assignment
            for assignment in result.worker_assignments
            if assignment.worker_id == "publisher_worker"
        )
        self.assertEqual(publisher.status, WorkerStatus.BLOCKED)
        self.assertIn(PlanPhase.PUBLISHER, publisher.phases)


class TestCooOrchestrateTool(unittest.TestCase):
    def test_registered_in_registry(self) -> None:
        import tools.coo_tools  # noqa: F401 — side-effect registration

        from tools.registry import registry

        entry = registry.get_entry("coo_orchestrate")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.toolset, "coo")
        self.assertEqual(entry.schema["name"], "coo_orchestrate")

    def test_coo_orchestrate_returns_required_payload(self) -> None:
        from tools.coo_tools import coo_orchestrate

        raw = coo_orchestrate("오늘 상태 보고해", run_date="2026-07-04")
        payload = json.loads(raw)

        for key in (
            "intent",
            "plan",
            "policy_decisions",
            "selected_skills",
            "blocked_skills",
            "deferred_skills",
            "approval_required",
            "review_required",
            "auto_apply",
            "summary",
            "worker_assignments",
        ):
            self.assertIn(key, payload)

        self.assertEqual(payload["intent"]["task_kind"], TaskKind.DAILY_BRIEF.value)
        self.assertFalse(payload["auto_apply"])
        self.assertTrue(payload["review_required"])
        self.assertIsInstance(payload["policy_decisions"], list)
        self.assertIsInstance(payload["worker_assignments"], list)

    def test_coo_toolset_resolves_coo_orchestrate(self) -> None:
        from toolsets import resolve_toolset

        self.assertIn("coo_orchestrate", resolve_toolset("coo"))

    def test_coo_orchestrate_rejects_empty_message(self) -> None:
        from tools.coo_tools import coo_orchestrate

        raw = coo_orchestrate("")
        payload = json.loads(raw)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
