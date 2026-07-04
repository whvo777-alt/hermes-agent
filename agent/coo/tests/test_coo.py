"""Unit tests for Hermes COO orchestration layer."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
from agent.coo.worker_interface import (
    BaseWorker,
    WorkerContext,
    WorkerExecutionMode,
    WorkerResult,
)
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
        self.assertIsNotNone(result.worker_runtime_result)
        self.assertIsNotNone(payload["worker_runtime_result"])

    def test_orchestrate_includes_worker_runtime_result(self) -> None:
        import subprocess

        orchestrator = COOOrchestrator()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = orchestrator.orchestrate(
                "오늘 블로그 글 작성해서 보고해",
                run_date="2026-07-04",
            )

        self.assertIsNotNone(result.worker_runtime_result)
        self.assertTrue(result.worker_runtime_result.dry_run)
        self.assertGreater(len(result.worker_runtime_result.worker_results), 0)
        self.assertIn("sequenced", result.worker_runtime_result.summary)

    def test_orchestrate_blocked_worker_reflected_in_runtime_status(self) -> None:
        import subprocess

        from agent.coo.worker_runtime import WorkerRuntimeStatus

        orchestrator = COOOrchestrator()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = orchestrator.orchestrate("승인하고 발행해", run_date="2026-07-04")

        self.assertIsNotNone(result.worker_runtime_result)
        self.assertEqual(result.worker_runtime_result.status, WorkerRuntimeStatus.BLOCKED)
        self.assertIn("publisher_worker", result.worker_runtime_result.blocked_workers)

    def test_ceo_message_includes_worker_runtime_summary(self) -> None:
        import subprocess

        orchestrator = COOOrchestrator()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = orchestrator.orchestrate(
                "오늘 블로그 글 작성해서 보고해",
                run_date="2026-07-04",
            )

        self.assertIn("**Worker Runtime**:", result.ceo_message)
        self.assertIn("sequenced", result.ceo_message)
        self.assertIn("Provider results:", result.ceo_message)

    def test_ceo_message_shows_blocked_workers(self) -> None:
        import subprocess

        orchestrator = COOOrchestrator()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = orchestrator.orchestrate("승인하고 발행해", run_date="2026-07-04")

        self.assertIn("Blocked workers: publisher_worker", result.ceo_message)
        self.assertIn("Status: `blocked`", result.ceo_message)

    def test_ceo_message_runtime_not_started_without_assignments(self) -> None:
        orchestrator = COOOrchestrator()
        result = orchestrator.orchestrate("???", run_date="2026-07-04")

        self.assertIsNone(result.worker_runtime_result)
        self.assertIn("Worker Runtime**: not started", result.ceo_message)

    def test_accepts_optional_dependencies(self) -> None:
        intent_analyzer = MagicMock()
        execution_planner = MagicMock()
        execution_policy = MagicMock()
        skill_selector = MagicMock()
        pipeline_state_reader = MagicMock()
        worker_manager = MagicMock()
        worker_runtime = MagicMock()

        orchestrator = COOOrchestrator(
            intent_analyzer=intent_analyzer,
            execution_planner=execution_planner,
            execution_policy=execution_policy,
            skill_selector=skill_selector,
            pipeline_state_reader=pipeline_state_reader,
            worker_manager=worker_manager,
            worker_runtime=worker_runtime,
        )

        self.assertIs(orchestrator._intent, intent_analyzer)
        self.assertIs(orchestrator._planner, execution_planner)
        self.assertIs(orchestrator._policy, execution_policy)
        self.assertIs(orchestrator._selector, skill_selector)
        self.assertIs(orchestrator._state_reader, pipeline_state_reader)
        self.assertIs(orchestrator._worker_manager, worker_manager)
        self.assertIs(orchestrator._worker_runtime, worker_runtime)


class TestWorkerManager(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = WorkerManager()

    def test_worker_context_pipeline_root_is_legacy_documented(self) -> None:
        doc = WorkerContext.__doc__ or ""
        self.assertIn("ExecutionProvider", doc)
        self.assertIn("legacy", doc.lower())
        self.assertIn("Do not", doc)

    def test_worker_definition_skill_ids_documentation(self) -> None:
        from agent.coo.worker_registry import WorkerDefinition

        doc = WorkerDefinition.__doc__ or ""
        self.assertIn("planning", doc.lower())
        self.assertIn("not", doc.lower())
        self.assertIn("SKILL_CATALOG", doc)
        self.assertIn("SkillInvocation", doc)

    def test_validate_worker_registry_returns_warnings(self) -> None:
        from agent.coo.worker_registry import validate_worker_registry

        warnings = validate_worker_registry()
        self.assertIsInstance(warnings, list)
        self.assertGreater(len(warnings), 0)
        self.assertIn("runtime", warnings[0].lower())
        self.assertIn("future", warnings[0].lower())
        self.assertTrue(any("future skill" in warning for warning in warnings[1:]))

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


class DummyWorker(BaseWorker):
    """Test double for Worker Interface contract."""

    def __init__(self) -> None:
        self.worker_id = "dummy_worker"
        self.department_id = "research"
        self.supported_phases = (PlanPhase.RESEARCH,)

    def describe(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "department_id": self.department_id,
            "phases": [phase.value for phase in self.supported_phases],
        }


class TestWorkerInterface(unittest.TestCase):
    def _sample_context(
        self,
        *,
        mode: WorkerExecutionMode = WorkerExecutionMode.PLAN_ONLY,
        status: WorkerStatus = WorkerStatus.SELECTED,
    ) -> WorkerContext:
        from agent.coo.models import PolicyDecision, PolicyVerdict, WorkerAssignment

        assignment = WorkerAssignment(
            assignment_id="test-assignment-1",
            worker_id="dummy_worker",
            department_id="research",
            phases=[PlanPhase.RESEARCH],
            status=status,
        )
        plan = ExecutionPlanner().plan(
            IntentAnalyzer().analyze("오늘 상태 보고해", run_date="2026-07-04")
        )
        policy = PolicyDecision(
            verdict=PolicyVerdict.ALLOW,
            auto_apply=False,
            review_required=True,
            requires_ceo_approval=False,
            allowed_phases=[PlanPhase.RESEARCH],
        )
        return WorkerContext(
            assignment=assignment,
            plan=plan,
            policy=policy,
            skill_invocations=[],
            run_date="2026-07-04",
            pipeline_root="/opt/data/multi-content-pipeline",
            mode=mode,
        )

    def test_dummy_worker_describe_plan_perform(self) -> None:
        worker = DummyWorker()
        ctx = self._sample_context()

        description = worker.describe()
        self.assertEqual(description["worker_id"], "dummy_worker")

        plan_result = worker.plan(ctx)
        self.assertIsInstance(plan_result, WorkerResult)
        self.assertEqual(plan_result.worker_id, "dummy_worker")
        self.assertTrue(plan_result.dry_run)

        perform_result = worker.perform(ctx)
        self.assertIsInstance(perform_result, WorkerResult)
        self.assertEqual(perform_result.status, WorkerStatus.SELECTED)

    def test_worker_context_safeguard_defaults(self) -> None:
        ctx = self._sample_context()
        self.assertFalse(ctx.auto_apply)
        self.assertTrue(ctx.review_required)

    def test_perform_execute_mode_stays_dry_run(self) -> None:
        worker = DummyWorker()
        ctx = self._sample_context(mode=WorkerExecutionMode.EXECUTE)
        result = worker.perform(ctx)

        self.assertTrue(result.dry_run)
        self.assertFalse(result.auto_apply)
        self.assertTrue(result.review_required)
        self.assertEqual(result.mode, WorkerExecutionMode.EXECUTE)
        self.assertIn(
            result.status,
            (WorkerStatus.SELECTED, WorkerStatus.WAITING, WorkerStatus.BLOCKED),
        )

    def test_dry_run_rejects_execute_mode(self) -> None:
        from agent.coo.models import PolicyDecision, PolicyVerdict, WorkerAssignment

        manager = WorkerManager()
        assignment = WorkerAssignment(
            assignment_id="test-assignment-exec",
            worker_id="research_worker",
            department_id="research",
            phases=[PlanPhase.RESEARCH],
            status=WorkerStatus.SELECTED,
        )
        plan = ExecutionPlanner().plan(
            IntentAnalyzer().analyze("오늘 상태 보고해", run_date="2026-07-04")
        )
        policy = PolicyDecision(
            verdict=PolicyVerdict.ALLOW,
            auto_apply=False,
            review_required=True,
            requires_ceo_approval=False,
            allowed_phases=[PlanPhase.RESEARCH],
        )

        with self.assertRaises(RuntimeError) as ctx:
            manager.dry_run(
                [assignment],
                plan,
                policy,
                "/opt/data/multi-content-pipeline",
                mode=WorkerExecutionMode.EXECUTE,
            )
        self.assertIn("Phase 4", str(ctx.exception))

    def test_perform_calls_execution_provider_plan(self) -> None:
        from agent.coo.execution_provider import ExecutionProviderResult
        from agent.coo.models import PolicyDecision, PolicyVerdict, SkillInvocation, SkillInvocationStatus, WorkerAssignment

        mock_provider = MagicMock()
        mock_provider.plan.return_value = ExecutionProviderResult(
            provider_name="pipeline",
            adapter_status="planned",
            pipeline_root="/opt/data/multi-content-pipeline",
            entrypoint="node pipeline.js",
            skill_id="create_content",
            summary="Planned create_content",
        )

        assignment = WorkerAssignment(
            assignment_id="test-assignment-prov",
            worker_id="dummy_worker",
            department_id="writing",
            phases=[PlanPhase.WRITER],
            status=WorkerStatus.SELECTED,
            skill_invocations=[
                SkillInvocation(
                    skill_id="create_content",
                    skill_name="Create Content",
                    phase=PlanPhase.WRITER,
                    status=SkillInvocationStatus.SELECTED,
                )
            ],
        )
        plan = ExecutionPlanner().plan(
            IntentAnalyzer().analyze("오늘 블로그 글 작성", run_date="2026-07-04")
        )
        policy = PolicyDecision(
            verdict=PolicyVerdict.ALLOW,
            auto_apply=False,
            review_required=True,
            requires_ceo_approval=False,
            allowed_phases=[PlanPhase.WRITER],
        )
        ctx = WorkerContext(
            assignment=assignment,
            plan=plan,
            policy=policy,
            skill_invocations=list(assignment.skill_invocations),
            run_date="2026-07-04",
            pipeline_root="/opt/data/multi-content-pipeline",
            mode=WorkerExecutionMode.DRY_RUN,
            execution_provider=mock_provider,
        )

        worker = DummyWorker()
        result = worker.perform(ctx)

        mock_provider.plan.assert_called_once()
        call_request = mock_provider.plan.call_args[0][0]
        self.assertEqual(call_request.skill_id, "create_content")
        self.assertEqual(call_request.worker_id, "dummy_worker")
        self.assertEqual(call_request.mode.value, "dry_run")
        self.assertEqual(len(result.provider_results), 1)
        self.assertEqual(result.provider_results[0].skill_id, "create_content")

    def test_provider_result_includes_worker_attribution(self) -> None:
        import subprocess

        from agent.coo.execution_provider import ExecutionProvider
        from agent.coo.models import PolicyDecision, PolicyVerdict, SkillInvocation, SkillInvocationStatus, WorkerAssignment
        from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig

        assignment = WorkerAssignment(
            assignment_id="attr-test-1",
            worker_id="dummy_worker",
            department_id="writing",
            phases=[PlanPhase.WRITER],
            status=WorkerStatus.SELECTED,
            skill_invocations=[
                SkillInvocation(
                    skill_id="create_content",
                    skill_name="Create Content",
                    phase=PlanPhase.WRITER,
                    status=SkillInvocationStatus.SELECTED,
                )
            ],
        )
        plan = ExecutionPlanner().plan(
            IntentAnalyzer().analyze("오늘 블로그 글 작성", run_date="2026-07-04")
        )
        policy = PolicyDecision(
            verdict=PolicyVerdict.ALLOW,
            auto_apply=False,
            review_required=True,
            requires_ceo_approval=False,
            allowed_phases=[PlanPhase.WRITER],
        )
        provider = ExecutionProvider(
            adapter=PipelineAdapter(
                PipelineAdapterConfig(pipeline_root="/opt/data/multi-content-pipeline")
            )
        )
        ctx = WorkerContext(
            assignment=assignment,
            plan=plan,
            policy=policy,
            skill_invocations=list(assignment.skill_invocations),
            run_date="2026-07-04",
            pipeline_root="/opt/data/multi-content-pipeline",
            mode=WorkerExecutionMode.DRY_RUN,
            execution_provider=provider,
        )

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = DummyWorker().perform(ctx)

        self.assertEqual(len(result.provider_results), 1)
        self.assertEqual(result.provider_results[0].worker_id, "dummy_worker")
        self.assertEqual(result.provider_results[0].assignment_id, "attr-test-1")

    def test_worker_manager_dry_run_includes_provider_results(self) -> None:
        import subprocess

        from agent.coo.models import PolicyDecision, PolicyVerdict, SkillInvocation, SkillInvocationStatus, WorkerAssignment

        plan = ExecutionPlanner().plan(
            IntentAnalyzer().analyze("오늘 블로그 글 작성해서 보고해", run_date="2026-07-04")
        )
        policy = PolicyDecision(
            verdict=PolicyVerdict.ALLOW,
            auto_apply=False,
            review_required=True,
            requires_ceo_approval=False,
            allowed_phases=[PlanPhase.WRITER],
        )
        assignment = WorkerAssignment(
            assignment_id="test-draft-prov",
            worker_id="draft_worker",
            department_id="writing",
            phases=[PlanPhase.WRITER],
            status=WorkerStatus.SELECTED,
            skill_invocations=[
                SkillInvocation(
                    skill_id="create_content",
                    skill_name="Create Content",
                    phase=PlanPhase.WRITER,
                    status=SkillInvocationStatus.SELECTED,
                )
            ],
        )

        manager = WorkerManager()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            results = manager.dry_run(
                [assignment],
                plan,
                policy,
                "/opt/data/multi-content-pipeline",
                mode=WorkerExecutionMode.DRY_RUN,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0].provider_results), 1)
        self.assertEqual(results[0].provider_results[0].skill_id, "create_content")
        self.assertTrue(all(item.dry_run for item in results[0].provider_results))


class TestWorkerRuntime(unittest.TestCase):
    def test_create_and_report_sequences_workers_in_order(self) -> None:
        import subprocess

        from agent.coo.worker_runtime import WorkerRuntime

        orchestrated = COOOrchestrator().orchestrate(
            "오늘 블로그 글 작성해서 보고해",
            run_date="2026-07-04",
        )
        expected_ids = [
            "research_worker",
            "strategy_worker",
            "draft_worker",
            "quality_worker",
            "approval_worker",
            "reporting_worker",
        ]

        runtime = WorkerRuntime()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = runtime.run(
                orchestrated.worker_assignments,
                orchestrated.plan,
                orchestrated.policy,
                orchestrated.state.pipeline_root,
            )

        self.assertEqual(
            [worker_result.worker_id for worker_result in result.worker_results],
            expected_ids,
        )
        self.assertTrue(result.dry_run)
        self.assertIn("6 worker(s) sequenced", result.summary)

    def test_blocked_worker_reflected_in_runtime_status(self) -> None:
        import subprocess

        from agent.coo.worker_runtime import WorkerRuntime, WorkerRuntimeStatus

        orchestrated = COOOrchestrator().orchestrate(
            "승인하고 발행해",
            run_date="2026-07-04",
        )

        runtime = WorkerRuntime()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = runtime.run(
                orchestrated.worker_assignments,
                orchestrated.plan,
                orchestrated.policy,
                orchestrated.state.pipeline_root,
            )

        self.assertIn("publisher_worker", result.blocked_workers)
        self.assertEqual(result.status, WorkerRuntimeStatus.BLOCKED)
        self.assertGreater(result.status_counts.get("blocked", 0), 0)
        self.assertIn("publisher_worker", result.summary)

    def test_provider_results_included_in_runtime_result(self) -> None:
        import subprocess

        from agent.coo.models import PolicyDecision, PolicyVerdict, SkillInvocation, SkillInvocationStatus, WorkerAssignment
        from agent.coo.worker_runtime import WorkerRuntime

        plan = ExecutionPlanner().plan(
            IntentAnalyzer().analyze("오늘 블로그 글 작성", run_date="2026-07-04")
        )
        policy = PolicyDecision(
            verdict=PolicyVerdict.ALLOW,
            auto_apply=False,
            review_required=True,
            requires_ceo_approval=False,
            allowed_phases=[PlanPhase.WRITER],
        )
        assignment = WorkerAssignment(
            assignment_id="runtime-draft-prov",
            worker_id="draft_worker",
            department_id="writing",
            phases=[PlanPhase.WRITER],
            status=WorkerStatus.SELECTED,
            skill_invocations=[
                SkillInvocation(
                    skill_id="create_content",
                    skill_name="Create Content",
                    phase=PlanPhase.WRITER,
                    status=SkillInvocationStatus.SELECTED,
                )
            ],
        )

        runtime = WorkerRuntime()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = runtime.run(
                [assignment],
                plan,
                policy,
                "/opt/data/multi-content-pipeline",
            )

        self.assertEqual(len(result.provider_results), 1)
        self.assertEqual(result.provider_results[0].skill_id, "create_content")
        self.assertIn("ExecutionProvider planned 1 skill(s)", result.summary)
        self.assertEqual(result.provider_results[0].worker_id, "draft_worker")
        self.assertEqual(result.provider_results[0].assignment_id, "runtime-draft-prov")

    def test_worker_status_rank_shared_by_manager_and_runtime(self) -> None:
        from agent.coo.models import WorkerStatus, worker_status_rank
        from agent.coo.worker_manager import WorkerManager

        statuses = [WorkerStatus.SELECTED, WorkerStatus.BLOCKED, WorkerStatus.WAITING]
        expected = max(statuses, key=worker_status_rank)
        self.assertEqual(WorkerManager._merge_statuses(statuses), expected)
        self.assertGreater(worker_status_rank(WorkerStatus.FAILED), worker_status_rank(WorkerStatus.BLOCKED))
        self.assertGreater(worker_status_rank(WorkerStatus.BLOCKED), worker_status_rank(WorkerStatus.WAITING))
        self.assertGreater(worker_status_rank(WorkerStatus.WAITING), worker_status_rank(WorkerStatus.SELECTED))

    def test_validate_worker_registry_returns_warnings_not_exceptions(self) -> None:
        from agent.coo.worker_registry import validate_worker_registry

        warnings = validate_worker_registry()
        try:
            self.assertIsInstance(warnings, list)
        except Exception as exc:  # pragma: no cover - guard against future raises
            self.fail(f"validate_worker_registry must not raise: {exc}")


class TestExecutionContract(unittest.TestCase):
    def test_boundary_policy_defaults(self) -> None:
        from agent.coo.execution_contract import default_boundary_policy

        policy = default_boundary_policy()
        self.assertFalse(policy.auto_apply)
        self.assertTrue(policy.review_required)
        self.assertFalse(policy.allow_publish)
        self.assertFalse(policy.allow_approval_decision)
        self.assertFalse(policy.allow_learning_apply)
        self.assertFalse(policy.allow_strategy_apply)
        self.assertTrue(policy.repository2_read_only)

    def test_skill_execution_request_safeguard_defaults(self) -> None:
        from agent.coo.execution_contract import SkillExecutionRequest

        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-1",
            run_date="2026-07-04",
        )
        self.assertFalse(request.auto_apply)
        self.assertTrue(request.review_required)

    def test_execute_publish_blocked_when_allow_publish_false(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionStatus,
            evaluate_skill_execution,
            validate_skill_execution_request,
        )
        from agent.coo.execution_contract import SkillExecutionRequest
        from agent.coo.skills_catalog import RISK_CATEGORY_PUBLISH, get_skill

        definition = get_skill("publish_content")
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertEqual(definition.risk_category, RISK_CATEGORY_PUBLISH)
        self.assertTrue(definition.requires_ceo_approval)

        request = SkillExecutionRequest(
            skill_id="publish_content",
            worker_id="publisher_worker",
            assignment_id="a-pub-1",
            run_date="2026-07-04",
            mode=SkillExecutionMode.EXECUTE,
        )
        ok, error = validate_skill_execution_request(request)
        self.assertFalse(ok)
        self.assertIsNotNone(error)

        result = evaluate_skill_execution(request)
        self.assertEqual(result.status, SkillExecutionStatus.BLOCKED)
        self.assertTrue(result.dry_run)
        self.assertIn("Publish", result.blocked_reason)

    def test_worker_plan_only_rejects_skill_execute(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionStatus,
            SkillExecutionRequest,
            evaluate_skill_execution,
            validate_mode_compatibility,
            validate_skill_execution_request,
        )

        ok, _ = validate_mode_compatibility("plan_only", SkillExecutionMode.EXECUTE)
        self.assertFalse(ok)

        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-1",
            run_date="2026-07-04",
            mode=SkillExecutionMode.EXECUTE,
            worker_mode="plan_only",
        )
        ok, error = validate_skill_execution_request(request)
        self.assertFalse(ok)
        self.assertIsNotNone(error)

        result = evaluate_skill_execution(request)
        self.assertEqual(result.status, SkillExecutionStatus.BLOCKED)

    def test_worker_dry_run_rejects_skill_execute(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionStatus,
            SkillExecutionRequest,
            evaluate_skill_execution,
            validate_mode_compatibility,
            validate_skill_execution_request,
        )

        ok, _ = validate_mode_compatibility("dry_run", SkillExecutionMode.EXECUTE)
        self.assertFalse(ok)

        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-2",
            run_date="2026-07-04",
            mode=SkillExecutionMode.EXECUTE,
            worker_mode="dry_run",
        )
        ok, error = validate_skill_execution_request(request)
        self.assertFalse(ok)
        self.assertIsNotNone(error)

        result = evaluate_skill_execution(request)
        self.assertEqual(result.status, SkillExecutionStatus.BLOCKED)

    def test_worker_execute_allows_skill_dry_run(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionStatus,
            SkillExecutionRequest,
            evaluate_skill_execution,
            validate_mode_compatibility,
            validate_skill_execution_request,
        )

        ok, error = validate_mode_compatibility("execute", SkillExecutionMode.DRY_RUN)
        self.assertTrue(ok)
        self.assertIsNone(error)

        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-3",
            run_date="2026-07-04",
            mode=SkillExecutionMode.DRY_RUN,
            worker_mode="execute",
        )
        ok, error = validate_skill_execution_request(request)
        self.assertTrue(ok)
        self.assertIsNone(error)

        result = evaluate_skill_execution(request)
        self.assertEqual(result.status, SkillExecutionStatus.PLANNED)
        self.assertTrue(result.dry_run)

    def test_allowed_execute_request_planned_with_dry_run_false(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionStatus,
            SkillExecutionRequest,
            evaluate_skill_execution,
            validate_skill_execution_request,
        )

        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-4",
            run_date="2026-07-04",
            mode=SkillExecutionMode.EXECUTE,
            worker_mode="execute",
        )
        ok, error = validate_skill_execution_request(request)
        self.assertTrue(ok)
        self.assertIsNone(error)

        result = evaluate_skill_execution(request)
        self.assertEqual(result.status, SkillExecutionStatus.PLANNED)
        self.assertFalse(result.dry_run)
        self.assertEqual(result.mode, SkillExecutionMode.EXECUTE)

    def test_execution_artifact_ref_is_path_metadata_only(self) -> None:
        from dataclasses import fields

        from agent.coo.execution_contract import ExecutionArtifactRef

        artifact = ExecutionArtifactRef(
            path="outputs/2026-07-04/_reports/daily.json",
            kind="report",
            summary="Daily brief",
        )
        field_names = {field.name for field in fields(artifact)}
        self.assertEqual(field_names, {"path", "kind", "summary"})
        self.assertNotIn("blob", field_names)
        self.assertNotIn("content", field_names)


class TestPipelineAdapter(unittest.TestCase):
    def test_pipeline_adapter_config_defaults(self) -> None:
        from agent.coo.pipeline_adapter import PipelineAdapterConfig

        config = PipelineAdapterConfig()
        self.assertEqual(config.pipeline_root, "/opt/data/multi-content-pipeline")
        self.assertFalse(config.allow_execute)
        self.assertEqual(config.timeout_seconds, 60)
        self.assertTrue(config.repository2_read_only)

    def test_validate_root_checks_directory_existence(self) -> None:
        from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig

        missing = PipelineAdapter(
            PipelineAdapterConfig(pipeline_root="/nonexistent/pipeline/root/phase4a")
        )
        ok, error = missing.validate_root()
        self.assertFalse(ok)
        self.assertIn("not found", error)

        if Path("/opt/data/multi-content-pipeline").is_dir():
            present = PipelineAdapter()
            ok, error = present.validate_root()
            self.assertTrue(ok)
            self.assertEqual(error, "")

    def test_dry_run_returns_result_without_subprocess(self) -> None:
        import subprocess

        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionRequest,
            SkillExecutionStatus,
        )
        from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            adapter = PipelineAdapter(
                PipelineAdapterConfig(pipeline_root="/opt/data/multi-content-pipeline")
            )
            request = SkillExecutionRequest(
                skill_id="create_content",
                worker_id="draft_worker",
                assignment_id="a-dry-1",
                run_date="2026-07-04",
                mode=SkillExecutionMode.DRY_RUN,
                worker_mode="dry_run",
            )
            result = adapter.dry_run(request)

        self.assertEqual(result.status, SkillExecutionStatus.PLANNED)
        self.assertTrue(result.dry_run)
        self.assertIn("Dry-run ready", result.summary)
        self.assertFalse(result.auto_apply)
        self.assertTrue(result.review_required)

    def test_dispatch_raises_runtime_error_in_phase_4a(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionRequest,
        )
        from agent.coo.pipeline_adapter import PipelineAdapter

        adapter = PipelineAdapter()
        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-disp-1",
            run_date="2026-07-04",
            mode=SkillExecutionMode.EXECUTE,
            worker_mode="execute",
        )
        with self.assertRaises(RuntimeError) as ctx:
            adapter.dispatch(request)
        self.assertIn("allow_execute", str(ctx.exception))

    def test_publish_skill_blocked_before_adapter(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionRequest,
            SkillExecutionStatus,
        )
        from agent.coo.pipeline_adapter import PipelineAdapter

        adapter = PipelineAdapter()
        request = SkillExecutionRequest(
            skill_id="publish_content",
            worker_id="publisher_worker",
            assignment_id="a-pub-dry",
            run_date="2026-07-04",
            mode=SkillExecutionMode.DRY_RUN,
        )
        result = adapter.dry_run(request)
        self.assertEqual(result.status, SkillExecutionStatus.BLOCKED)
        self.assertTrue(result.dry_run)
        self.assertIn("Publish", result.blocked_reason)

    def test_request_entrypoint_override_uses_catalog(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionRequest,
            SkillExecutionStatus,
        )
        from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig
        from agent.coo.skills_catalog import get_skill

        definition = get_skill("create_content")
        self.assertIsNotNone(definition)
        assert definition is not None

        adapter = PipelineAdapter(
            PipelineAdapterConfig(pipeline_root="/opt/data/multi-content-pipeline")
        )
        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-override-1",
            run_date="2026-07-04",
            mode=SkillExecutionMode.DRY_RUN,
            worker_mode="dry_run",
            entrypoint_hint="npm run evil-override",
        )
        result = adapter.dry_run(request)
        self.assertEqual(result.status, SkillExecutionStatus.PLANNED)
        self.assertIn("entrypoint='node pipeline.js'", result.summary)

        plan = adapter.plan(request)
        self.assertEqual(plan.entrypoint_hint, definition.entrypoint_hint)
        self.assertEqual(plan.entrypoint_hint, "node pipeline.js")
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn("evil-override", plan.warnings[0])
        self.assertIn("ignored", plan.warnings[0])

    def test_entrypoint_override_warning_in_adapter_warnings_list(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionRequest,
        )
        from agent.coo.pipeline_adapter import (
            PipelineAdapter,
            PipelineAdapterConfig,
            PipelineAdapterStatus,
        )

        adapter = PipelineAdapter(
            PipelineAdapterConfig(pipeline_root="/opt/data/multi-content-pipeline")
        )
        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-warn-1",
            run_date="2026-07-04",
            mode=SkillExecutionMode.PLAN_ONLY,
            entrypoint_hint="npm run evil-override",
        )
        result = adapter.plan(request)
        self.assertEqual(result.status, PipelineAdapterStatus.PLANNED)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("npm run evil-override", result.warnings[0])
        self.assertIn("node pipeline.js", result.warnings[0])

    def test_blocked_plan_preserves_entrypoint_override_warning(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionRequest,
        )
        from agent.coo.pipeline_adapter import (
            PipelineAdapter,
            PipelineAdapterStatus,
        )

        adapter = PipelineAdapter()
        request = SkillExecutionRequest(
            skill_id="publish_content",
            worker_id="publisher_worker",
            assignment_id="a-warn-blocked",
            run_date="2026-07-04",
            mode=SkillExecutionMode.DRY_RUN,
            entrypoint_hint="npm run evil-override",
        )
        result = adapter.plan(request)
        self.assertEqual(result.status, PipelineAdapterStatus.BLOCKED)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("evil-override", result.warnings[0])
        self.assertIn("Publish", result.blocked_reason)

    def test_repository2_read_only_false_raises_runtime_error(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionRequest,
        )
        from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig

        adapter = PipelineAdapter(
            PipelineAdapterConfig(
                pipeline_root="/opt/data/multi-content-pipeline",
                repository2_read_only=False,
            )
        )
        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-ro-1",
            run_date="2026-07-04",
            mode=SkillExecutionMode.DRY_RUN,
            worker_mode="dry_run",
        )
        with self.assertRaises(RuntimeError) as ctx:
            adapter.dry_run(request)
        self.assertIn("repository2_read_only", str(ctx.exception))

        with self.assertRaises(RuntimeError):
            adapter.plan(request)

        with self.assertRaises(RuntimeError):
            adapter.dispatch(request)


class TestExecutionProvider(unittest.TestCase):
    def test_plan_delegates_to_pipeline_adapter(self) -> None:
        import subprocess

        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionRequest,
        )
        from agent.coo.execution_provider import ExecutionProvider
        from agent.coo.pipeline_adapter import (
            PipelineAdapterResult,
            PipelineAdapterStatus,
        )

        mock_adapter = MagicMock()
        mock_adapter.plan.return_value = PipelineAdapterResult(
            status=PipelineAdapterStatus.PLANNED,
            skill_id="create_content",
            entrypoint_hint="node pipeline.js",
            pipeline_root="/opt/data/multi-content-pipeline",
            summary="Planned create_content via 'node pipeline.js' (no dispatch).",
            run_date="2026-07-04",
            parameters={"topic": "ai"},
            root_valid=True,
            warnings=["catalog entrypoint used"],
        )

        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-prov-1",
            run_date="2026-07-04",
            mode=SkillExecutionMode.PLAN_ONLY,
            parameters={"topic": "ai"},
        )

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            provider = ExecutionProvider(adapter=mock_adapter)
            result = provider.plan(request)

        mock_adapter.plan.assert_called_once_with(request, None)
        self.assertEqual(result.provider_name, "pipeline")
        self.assertEqual(result.adapter_status, PipelineAdapterStatus.PLANNED.value)
        self.assertEqual(result.entrypoint, "node pipeline.js")
        self.assertEqual(result.parameters, {"topic": "ai"})
        self.assertTrue(result.dry_run)
        self.assertEqual(result.skill_id, "create_content")
        self.assertEqual(result.warnings, ["catalog entrypoint used"])

    def test_provider_warnings_copy_adapter_warnings(self) -> None:
        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionRequest,
        )
        from agent.coo.execution_provider import ExecutionProvider
        from agent.coo.pipeline_adapter import (
            PipelineAdapterResult,
            PipelineAdapterStatus,
        )

        adapter_warnings = [
            "request.entrypoint_hint 'npm run evil' ignored for create_content; "
            "using catalog entrypoint 'node pipeline.js'."
        ]
        mock_adapter = MagicMock()
        mock_adapter.plan.return_value = PipelineAdapterResult(
            status=PipelineAdapterStatus.BLOCKED,
            skill_id="publish_content",
            entrypoint_hint="npm run preflight:publish",
            pipeline_root="/opt/data/multi-content-pipeline",
            summary="Blocked: publish_content",
            blocked_reason="Publish skills are blocked",
            warnings=adapter_warnings,
        )

        request = SkillExecutionRequest(
            skill_id="publish_content",
            worker_id="publisher_worker",
            assignment_id="a-prov-warn",
            run_date="2026-07-04",
            mode=SkillExecutionMode.DRY_RUN,
            entrypoint_hint="npm run evil",
        )
        provider = ExecutionProvider(adapter=mock_adapter)
        result = provider.plan(request)

        self.assertEqual(result.warnings, adapter_warnings)
        result.warnings.append("mutated")
        self.assertEqual(len(adapter_warnings), 1)

    def test_plan_integration_no_subprocess(self) -> None:
        import subprocess

        from agent.coo.execution_contract import (
            SkillExecutionMode,
            SkillExecutionRequest,
        )
        from agent.coo.execution_provider import ExecutionProvider
        from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig

        request = SkillExecutionRequest(
            skill_id="create_content",
            worker_id="draft_worker",
            assignment_id="a-prov-2",
            run_date="2026-07-04",
            mode=SkillExecutionMode.PLAN_ONLY,
            worker_mode="plan_only",
        )

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            provider = ExecutionProvider(
                adapter=PipelineAdapter(
                    PipelineAdapterConfig(pipeline_root="/opt/data/multi-content-pipeline")
                )
            )
            result = provider.plan(request)

        self.assertEqual(result.provider_name, "pipeline")
        self.assertEqual(result.entrypoint, "node pipeline.js")
        self.assertTrue(result.dry_run)
        self.assertFalse(result.parameters.get("auto_apply", False))

    def test_provider_has_no_dispatch(self) -> None:
        from agent.coo.execution_provider import ExecutionProvider

        self.assertFalse(hasattr(ExecutionProvider, "dispatch"))


class TestCEOApprovalReport(unittest.TestCase):
    def test_create_and_report_builds_approval_report(self) -> None:
        import subprocess

        from agent.coo.approval_report import CEOApprovalReportStatus, build_approval_report

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = COOOrchestrator().orchestrate(
                "오늘 블로그 글 작성해서 보고해",
                run_date="2026-07-04",
            )

        report = build_approval_report(result)
        self.assertEqual(report.task_kind, TaskKind.CREATE_AND_REPORT.value)
        self.assertEqual(report.run_date, "2026-07-04")
        self.assertFalse(report.auto_apply)
        self.assertTrue(report.review_required)
        self.assertEqual(report.status, CEOApprovalReportStatus.READY)
        self.assertEqual(report.runtime_status, "selected")
        self.assertGreater(len(report.selected_workers), 0)
        self.assertIn("sequenced", report.worker_summary)

    def test_blocked_worker_approval_report(self) -> None:
        import subprocess

        from agent.coo.approval_report import CEOApprovalReportStatus, build_approval_report

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = COOOrchestrator().orchestrate("승인하고 발행해", run_date="2026-07-04")

        report = build_approval_report(result)
        self.assertEqual(report.status, CEOApprovalReportStatus.BLOCKED)
        self.assertIn("publisher_worker", report.blocked_workers)
        self.assertEqual(report.runtime_status, "blocked")

    def test_approval_report_markdown_includes_worker_runtime_section(self) -> None:
        import subprocess

        from agent.coo.approval_report import build_approval_report

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = COOOrchestrator().orchestrate(
                "오늘 블로그 글 작성해서 보고해",
                run_date="2026-07-04",
            )

        markdown = build_approval_report(result).to_markdown()
        self.assertIn("## CEO Approval Report", markdown)
        self.assertIn("### Worker Runtime", markdown)
        self.assertIn("Provider results:", markdown)
        self.assertIn("auto_apply: `False`", markdown)
        self.assertIn("review_required: `True`", markdown)

    def test_safeguards_enforced_in_report(self) -> None:
        from agent.coo.approval_report import build_approval_report

        result = COOOrchestrator().orchestrate("???", run_date="2026-07-04")
        report = build_approval_report(result)
        self.assertFalse(report.auto_apply)
        self.assertTrue(report.review_required)


class TestCEOApprovalReportStatusMapping(unittest.TestCase):
    def test_runtime_status_to_report_status_mapping(self) -> None:
        from agent.coo.approval_report import CEOApprovalReportStatus, runtime_status_to_report_status
        from agent.coo.worker_runtime import WorkerRuntimeStatus

        self.assertEqual(
            runtime_status_to_report_status(None),
            CEOApprovalReportStatus.NOT_STARTED,
        )
        self.assertEqual(
            runtime_status_to_report_status(WorkerRuntimeStatus.FAILED),
            CEOApprovalReportStatus.BLOCKED,
        )
        self.assertEqual(
            runtime_status_to_report_status(WorkerRuntimeStatus.WAITING),
            CEOApprovalReportStatus.PENDING,
        )
        self.assertEqual(
            runtime_status_to_report_status(WorkerRuntimeStatus.SELECTED),
            CEOApprovalReportStatus.READY,
        )

    def test_build_approval_report_uses_runtime_status_mapping(self) -> None:
        from agent.coo.approval_report import CEOApprovalReportStatus, build_approval_report
        from agent.coo.worker_runtime import WorkerRuntimeResult, WorkerRuntimeStatus

        orchestrated = COOOrchestrator().orchestrate("???", run_date="2026-07-04")
        orchestrated.worker_runtime_result = WorkerRuntimeResult(
            status=WorkerRuntimeStatus.FAILED,
            run_date="2026-07-04",
            summary="Simulated failed runtime.",
        )
        report = build_approval_report(orchestrated)
        self.assertEqual(report.runtime_status, "failed")
        self.assertEqual(report.status, CEOApprovalReportStatus.BLOCKED)

        orchestrated.worker_runtime_result = WorkerRuntimeResult(
            status=WorkerRuntimeStatus.WAITING,
            run_date="2026-07-04",
            summary="Simulated waiting runtime.",
            waiting_workers=["approval_worker"],
        )
        report = build_approval_report(orchestrated)
        self.assertEqual(report.runtime_status, "waiting")
        self.assertEqual(report.status, CEOApprovalReportStatus.PENDING)

    def test_coo_payload_keeps_distinct_ceo_message_and_approval_report(self) -> None:
        import subprocess

        from tools.coo_tools import coo_orchestrate

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate("오늘 상태 보고해", run_date="2026-07-04")
        payload = json.loads(raw)

        self.assertIn("ceo_message", payload)
        self.assertIn("approval_report_markdown", payload)
        self.assertNotEqual(payload["ceo_message"], payload["approval_report_markdown"])
        self.assertIn("**COO Plan**", payload["ceo_message"])
        self.assertIn("## CEO Approval Report", payload["approval_report_markdown"])
        self.assertIn("### Worker Runtime", payload["approval_report_markdown"])
        self.assertNotIn("## CEO Approval Report", payload["ceo_message"])


class TestCEOApprovalSession(unittest.TestCase):
    def test_create_session_from_report_defaults_pending(self) -> None:
        import subprocess

        from agent.coo.approval_report import build_approval_report
        from agent.coo.approval_session import (
            CEOApprovalSessionStatus,
            CEOApprovalSessionStore,
            create_approval_session,
        )

        store = CEOApprovalSessionStore()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            orchestrated = COOOrchestrator().orchestrate(
                "오늘 블로그 글 작성해서 보고해",
                run_date="2026-07-04",
            )
        report = build_approval_report(orchestrated)
        session = create_approval_session(report, orchestrated, store=store)

        self.assertTrue(session.session_id)
        self.assertEqual(session.status, CEOApprovalSessionStatus.PENDING)
        self.assertEqual(session.task_kind, report.task_kind)
        self.assertFalse(session.auto_apply)
        self.assertTrue(session.review_required)
        self.assertEqual(session.requester_id, "CEO")
        self.assertEqual(session.channel_id, "")
        self.assertTrue(session.expires_at)
        self.assertIs(store.get(session.session_id), session)

    def test_create_session_stores_custom_requester_and_channel(self) -> None:
        from agent.coo.approval_report import build_approval_report
        from agent.coo.approval_session import CEOApprovalSessionStore, create_approval_session

        store = CEOApprovalSessionStore()
        orchestrated = COOOrchestrator().orchestrate("???", run_date="2026-07-04")
        report = build_approval_report(orchestrated)
        session = create_approval_session(
            report,
            orchestrated,
            requester_id="ceo-user-42",
            channel_id="discord-chan-99",
            store=store,
        )

        self.assertEqual(session.requester_id, "ceo-user-42")
        self.assertEqual(session.channel_id, "discord-chan-99")

    def test_wrong_requester_cannot_approve(self) -> None:
        from agent.coo.approval_report import CEOApprovalReport, CEOApprovalReportStatus
        from agent.coo.approval_session import (
            CEOApprovalSessionStore,
            approve_session,
            create_approval_session,
        )

        store = CEOApprovalSessionStore()
        report = CEOApprovalReport(
            status=CEOApprovalReportStatus.READY,
            task_kind="daily_brief",
            run_date="2026-07-04",
            runtime_status="selected",
            worker_summary="test",
        )
        orchestrated = COOOrchestrator().orchestrate("오늘 상태 보고해", run_date="2026-07-04")
        session = create_approval_session(
            report,
            orchestrated,
            requester_id="CEO",
            store=store,
        )

        with self.assertRaises(ValueError):
            approve_session(
                session.session_id,
                reviewer="COO",
                requester_id="COO",
                store=store,
            )

    def test_not_started_report_skips_session_in_coo_orchestrate(self) -> None:
        import subprocess

        from agent.coo.approval_session import CEOApprovalSessionStore
        from tools.coo_tools import coo_orchestrate

        store = CEOApprovalSessionStore()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate("???", run_date="2026-07-04", session_store=store)
        payload = json.loads(raw)

        self.assertIsNone(payload["approval_session"])
        self.assertEqual(len(store.list_sessions()), 0)

    def test_expire_pending_sessions_marks_overdue_as_expired(self) -> None:
        from datetime import datetime, timezone

        from agent.coo.approval_report import CEOApprovalReport, CEOApprovalReportStatus
        from agent.coo.approval_session import (
            CEOApprovalSessionStatus,
            CEOApprovalSessionStore,
            create_approval_session,
        )

        store = CEOApprovalSessionStore()
        report = CEOApprovalReport(
            status=CEOApprovalReportStatus.READY,
            task_kind="daily_brief",
            run_date="2026-07-04",
            runtime_status="selected",
            worker_summary="test",
        )
        orchestrated = COOOrchestrator().orchestrate("오늘 상태 보고해", run_date="2026-07-04")
        session = create_approval_session(report, orchestrated, store=store)
        session.expires_at = "2020-01-01T00:00:00+00:00"
        store.save(session)

        expired_count = store.expire_pending_sessions(
            now=datetime(2026, 7, 5, tzinfo=timezone.utc)
        )

        self.assertEqual(expired_count, 1)
        refreshed = store.get(session.session_id)
        assert refreshed is not None
        self.assertEqual(refreshed.status, CEOApprovalSessionStatus.EXPIRED)

    def test_approve_session_transitions_to_approved_without_execution(self) -> None:
        import subprocess

        from agent.coo.approval_report import build_approval_report
        from agent.coo.approval_session import (
            CEOApprovalSessionStatus,
            CEOApprovalSessionStore,
            approve_session,
            create_approval_session,
        )

        store = CEOApprovalSessionStore()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            orchestrated = COOOrchestrator().orchestrate(
                "승인하고 발행해",
                run_date="2026-07-04",
            )
        session = create_approval_session(
            build_approval_report(orchestrated),
            orchestrated,
            store=store,
        )
        approved = approve_session(session.session_id, reviewer="CEO", store=store)

        self.assertEqual(approved.status, CEOApprovalSessionStatus.APPROVED)
        self.assertEqual(approved.reviewer, "CEO")
        self.assertTrue(approved.approved_at)
        self.assertEqual(approved.execution_ticket_id, "")
        self.assertFalse(approved.execution_dispatched)
        self.assertFalse(approved.publish_dispatched)

    def test_reject_session_transitions_to_rejected(self) -> None:
        from agent.coo.approval_report import CEOApprovalReport, CEOApprovalReportStatus
        from agent.coo.approval_session import (
            CEOApprovalSessionStatus,
            CEOApprovalSessionStore,
            create_approval_session,
            reject_session,
        )

        store = CEOApprovalSessionStore()
        report = CEOApprovalReport(
            status=CEOApprovalReportStatus.READY,
            task_kind="daily_brief",
            run_date="2026-07-04",
            runtime_status="selected",
            worker_summary="test",
        )
        orchestrated = COOOrchestrator().orchestrate("???", run_date="2026-07-04")
        session = create_approval_session(report, orchestrated, store=store)
        rejected = reject_session(
            session.session_id,
            reviewer="CEO",
            reason="Not ready",
            store=store,
        )

        self.assertEqual(rejected.status, CEOApprovalSessionStatus.REJECTED)
        self.assertEqual(rejected.rejection_reason, "Not ready")
        self.assertTrue(rejected.rejected_at)

    def test_coo_orchestrate_includes_approval_session(self) -> None:
        import subprocess

        from agent.coo.approval_session import CEOApprovalSessionStore
        from tools.coo_tools import coo_orchestrate

        store = CEOApprovalSessionStore()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate(
                "오늘 상태 보고해",
                run_date="2026-07-04",
                session_store=store,
            )
        payload = json.loads(raw)

        self.assertIn("approval_session", payload)
        self.assertIn("approval_report_markdown", payload)
        self.assertEqual(payload["approval_session"]["status"], "pending")
        self.assertFalse(payload["approval_session"]["auto_apply"])
        self.assertTrue(payload["approval_session"]["review_required"])
        self.assertEqual(len(store.list_sessions()), 1)


class TestCooOrchestrateTool(unittest.TestCase):
    def test_registered_in_registry(self) -> None:
        import tools.coo_tools  # noqa: F401 — side-effect registration

        from tools.registry import registry

        entry = registry.get_entry("coo_orchestrate")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.toolset, "coo")
        self.assertEqual(entry.schema["name"], "coo_orchestrate")

    def test_coo_orchestrate_returns_required_payload(self) -> None:
        import subprocess

        from tools.coo_tools import coo_orchestrate

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
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
            "runtime_summary",
            "runtime_status",
            "runtime_provider_results",
            "approval_report_markdown",
            "approval_session",
        ):
            self.assertIn(key, payload)

        self.assertEqual(payload["intent"]["task_kind"], TaskKind.DAILY_BRIEF.value)
        self.assertFalse(payload["auto_apply"])
        self.assertTrue(payload["review_required"])
        self.assertIsInstance(payload["policy_decisions"], list)
        self.assertIsInstance(payload["worker_assignments"], list)
        self.assertIsInstance(payload["runtime_provider_results"], list)
        self.assertIn("sequenced", payload["runtime_summary"])
        self.assertIn("## CEO Approval Report", payload["approval_report_markdown"])
        self.assertIn("### Worker Runtime", payload["approval_report_markdown"])

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
