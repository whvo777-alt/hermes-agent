"""Worker Manager — staff workers from COO plan and policy (plan-only).

COO delegates staffing here so it never addresses individual workers or
skills directly. No Execution Engine invocation occurs in this module.
"""

from __future__ import annotations

import uuid
from typing import Dict, List

from agent.coo.models import (
    ExecutionPlan,
    PlanPhase,
    PolicyDecision,
    PolicyVerdict,
    SkillInvocation,
    WorkerAssignment,
    WorkerStatus,
)
from agent.coo.worker_interface import WorkerContext, WorkerExecutionMode, WorkerResult
from agent.coo.worker_registry import instantiate_worker, worker_for_phase


# Phase status merge priority (highest wins when one assignment spans phases):
# FAILED > CANCELLED > COMPLETED > BLOCKED > WAITING > WORKING > SELECTED > PLANNED
_STATUS_RANK = {
    WorkerStatus.PLANNED: 0,
    WorkerStatus.SELECTED: 1,
    WorkerStatus.WORKING: 2,
    WorkerStatus.WAITING: 3,
    WorkerStatus.BLOCKED: 4,
    WorkerStatus.COMPLETED: 5,
    WorkerStatus.CANCELLED: 6,
    WorkerStatus.FAILED: 7,
}

_CEO_BOUNDARY_PHASES = frozenset({
    PlanPhase.APPROVAL_QUEUE,
    PlanPhase.APPROVAL_CHECK,
    PlanPhase.PUBLISHER,
    PlanPhase.PUBLISH_WAIT,
})


class WorkerManager:
    """Map ExecutionPlan + PolicyDecision to WorkerAssignment records."""

    def assign(
        self,
        plan: ExecutionPlan,
        policy: PolicyDecision,
        skills: List[SkillInvocation],
    ) -> List[WorkerAssignment]:
        if not plan.steps:
            return []

        skills_by_phase = self._index_skills_by_phase(skills)
        grouped: Dict[str, Dict[str, object]] = {}
        order: List[str] = []

        for step in plan.steps:
            if step.phase is PlanPhase.PUBLISH_WAIT:
                continue

            definition = worker_for_phase(step.phase)
            if definition is None:
                continue

            worker_id = definition.worker_id
            if worker_id not in grouped:
                grouped[worker_id] = {
                    "definition": definition,
                    "phases": [],
                    "statuses": [],
                }
                order.append(worker_id)

            entry = grouped[worker_id]
            phases: List[PlanPhase] = entry["phases"]  # type: ignore[assignment]
            statuses: List[WorkerStatus] = entry["statuses"]  # type: ignore[assignment]
            phases.append(step.phase)
            statuses.append(self._status_for_phase(step.phase, policy))

        assignments: List[WorkerAssignment] = []
        for worker_id in order:
            entry = grouped[worker_id]
            definition = entry["definition"]
            phases = entry["phases"]  # type: ignore[assignment]
            statuses = entry["statuses"]  # type: ignore[assignment]
            phase_set = set(phases)
            worker_skills = [
                skill
                for phase in phases
                for skill in skills_by_phase.get(phase, [])
            ]
            status = self._merge_statuses(statuses)
            reason = self._reason_for_assignment(status, policy, phases)

            assignments.append(
                WorkerAssignment(
                    assignment_id=str(uuid.uuid4()),
                    worker_id=definition.worker_id,
                    department_id=definition.department_id,
                    phases=phases,
                    status=status,
                    skill_invocations=worker_skills,
                    requires_ceo_approval=(
                        policy.requires_ceo_approval
                        or definition.requires_ceo_approval
                        or bool(phase_set & _CEO_BOUNDARY_PHASES)
                    ),
                    reason=reason,
                    auto_apply=False,
                    review_required=True,
                )
            )

        return assignments

    def build_context(
        self,
        assignment: WorkerAssignment,
        plan: ExecutionPlan,
        policy: PolicyDecision,
        pipeline_root: str,
        mode: WorkerExecutionMode = WorkerExecutionMode.PLAN_ONLY,
        prior_results: List[WorkerResult] | None = None,
    ) -> WorkerContext:
        """Build a WorkerContext for plan/perform calls (no execution)."""
        return WorkerContext(
            assignment=assignment,
            plan=plan,
            policy=policy,
            skill_invocations=list(assignment.skill_invocations),
            run_date=plan.run_date,
            pipeline_root=pipeline_root,
            mode=mode,
            prior_results=list(prior_results or []),
            auto_apply=False,
            review_required=True,
        )

    def dry_run(
        self,
        assignments: List[WorkerAssignment],
        plan: ExecutionPlan,
        policy: PolicyDecision,
        pipeline_root: str,
        mode: WorkerExecutionMode = WorkerExecutionMode.DRY_RUN,
    ) -> List[WorkerResult]:
        """Run plan/perform dry-run for staffed workers — no skill execution."""
        if mode is WorkerExecutionMode.EXECUTE:
            raise RuntimeError("EXECUTE mode is not available until Phase 4.")

        results: List[WorkerResult] = []
        for assignment in assignments:
            worker = instantiate_worker(assignment.worker_id)
            if worker is None:
                continue
            ctx = self.build_context(
                assignment,
                plan,
                policy,
                pipeline_root,
                mode=mode,
                prior_results=results,
            )
            if mode is WorkerExecutionMode.PLAN_ONLY:
                results.append(worker.plan(ctx))
            else:
                results.append(worker.perform(ctx))
        return results

    @staticmethod
    def _index_skills_by_phase(
        skills: List[SkillInvocation],
    ) -> Dict[PlanPhase, List[SkillInvocation]]:
        indexed: Dict[PlanPhase, List[SkillInvocation]] = {}
        for skill in skills:
            indexed.setdefault(skill.phase, []).append(skill)
        return indexed

    @staticmethod
    def _status_for_phase(phase: PlanPhase, policy: PolicyDecision) -> WorkerStatus:
        if phase in policy.blocked_phases:
            return WorkerStatus.BLOCKED
        if phase in policy.deferred_phases or phase is PlanPhase.PUBLISH_WAIT:
            return WorkerStatus.WAITING
        if policy.verdict is PolicyVerdict.BLOCK:
            return WorkerStatus.BLOCKED
        if phase in policy.allowed_phases:
            return WorkerStatus.SELECTED
        return WorkerStatus.PLANNED

    @staticmethod
    def _merge_statuses(statuses: List[WorkerStatus]) -> WorkerStatus:
        if not statuses:
            return WorkerStatus.PLANNED
        return max(statuses, key=lambda status: _STATUS_RANK[status])

    @staticmethod
    def _reason_for_assignment(
        status: WorkerStatus,
        policy: PolicyDecision,
        phases: List[PlanPhase],
    ) -> str:
        if status is WorkerStatus.BLOCKED:
            return policy.summary or f"Blocked phases: {', '.join(p.value for p in phases)}"
        if status is WorkerStatus.WAITING:
            return policy.summary or f"Waiting on prerequisites for: {', '.join(p.value for p in phases)}"
        if status is WorkerStatus.SELECTED:
            return f"Staffed for phases: {', '.join(p.value for p in phases)}"
        return policy.summary or "Worker assignment planned."
