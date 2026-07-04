"""Execution Planner — map CEO tasks to ordered Execution Engine phases."""

from __future__ import annotations

from typing import List

from agent.coo.models import ExecutionPlan, IntentResult, PlanPhase, PlanStep, TaskKind
from agent.coo.pipeline_state import resolve_run_date


class ExecutionPlanner:
    """Produce a phase-ordered plan without invoking the Execution Engine."""

    def plan(self, intent: IntentResult) -> ExecutionPlan:
        run_date = intent.run_date or resolve_run_date()
        builders = {
            TaskKind.CREATE_AND_REPORT: self._plan_create_and_report,
            TaskKind.APPROVE_AND_PUBLISH: self._plan_approve_and_publish,
            TaskKind.REVIEW_APPROVALS: self._plan_review_approvals,
            TaskKind.DAILY_BRIEF: self._plan_daily_brief,
            TaskKind.VERIFY_RUNTIME: self._plan_verify_runtime,
            TaskKind.UNKNOWN: self._plan_unknown,
        }
        builder = builders[intent.task_kind]
        return builder(run_date, intent)

    def _plan_create_and_report(self, run_date: str, intent: IntentResult) -> ExecutionPlan:
        steps: List[PlanStep] = [
            PlanStep(PlanPhase.RESEARCH, "Collect research inputs", "create_content"),
            PlanStep(PlanPhase.STRATEGY, "Apply strategy context inside pipeline", "create_content"),
            PlanStep(PlanPhase.WRITER, "Generate platform content drafts", "create_content"),
            PlanStep(PlanPhase.QUALITY, "Run quality gate checks", "create_content"),
            PlanStep(PlanPhase.APPROVAL_QUEUE, "Build Discord approval queue", "create_content"),
            PlanStep(PlanPhase.CEO_REPORT, "Prepare CEO approval report", "approval_review"),
            PlanStep(
                PlanPhase.PUBLISH_WAIT,
                "Hold publishing until CEO approval",
                required=False,
            ),
        ]
        return ExecutionPlan(
            task_kind=TaskKind.CREATE_AND_REPORT,
            run_date=run_date,
            steps=steps,
            summary="Create today's content, queue approvals, and report to CEO.",
            notes=[
                "Pipeline phases execute inside create_content (pipeline.js).",
                "Publishing remains deferred until explicit CEO approval.",
            ],
        )

    def _plan_approve_and_publish(self, run_date: str, intent: IntentResult) -> ExecutionPlan:
        steps = [
            PlanStep(PlanPhase.APPROVAL_CHECK, "Verify CEO approvals are recorded", "approval_review"),
            PlanStep(PlanPhase.PUBLISHER, "Run post-approval publishing workflow", "publish_content"),
        ]
        return ExecutionPlan(
            task_kind=TaskKind.APPROVE_AND_PUBLISH,
            run_date=run_date,
            steps=steps,
            summary="Confirm approvals, then invoke publisher skill.",
            notes=["COO never auto-approves; CEO must have approved items first."],
        )

    def _plan_review_approvals(self, run_date: str, intent: IntentResult) -> ExecutionPlan:
        steps = [
            PlanStep(PlanPhase.APPROVAL_CHECK, "Inspect approval queue status", "approval_review"),
            PlanStep(PlanPhase.CEO_REPORT, "Summarize pending CEO decisions", "daily_brief"),
        ]
        return ExecutionPlan(
            task_kind=TaskKind.REVIEW_APPROVALS,
            run_date=run_date,
            steps=steps,
            summary="Review approval queue and report pending items to CEO.",
        )

    def _plan_daily_brief(self, run_date: str, intent: IntentResult) -> ExecutionPlan:
        steps = [
            PlanStep(PlanPhase.CEO_REPORT, "Aggregate read-only operational brief", "daily_brief"),
        ]
        return ExecutionPlan(
            task_kind=TaskKind.DAILY_BRIEF,
            run_date=run_date,
            steps=steps,
            summary="Generate read-only daily brief for CEO.",
        )

    def _plan_verify_runtime(self, run_date: str, intent: IntentResult) -> ExecutionPlan:
        steps = [
            PlanStep(PlanPhase.VERIFY, "Run Closed Beta verification chain", "verify_runtime"),
        ]
        return ExecutionPlan(
            task_kind=TaskKind.VERIFY_RUNTIME,
            run_date=run_date,
            steps=steps,
            summary="Validate runtime and pipeline safeguards before release.",
        )

    def _plan_unknown(self, run_date: str, intent: IntentResult) -> ExecutionPlan:
        return ExecutionPlan(
            task_kind=TaskKind.UNKNOWN,
            run_date=run_date,
            steps=[],
            summary="Intent not recognized — COO requires clarification from CEO.",
            notes=[f"Unmatched input: {intent.raw_text!r}"],
        )
