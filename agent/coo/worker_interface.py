"""Worker interface — plan-only / dry-run worker contract (Phase 3C).

Workers are AI employees that own skills. This module defines the runtime
contract; no Execution Engine invocation occurs here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from agent.coo.models import (
    ExecutionPlan,
    PlanPhase,
    PolicyDecision,
    SkillInvocation,
    WorkerAssignment,
    WorkerStatus,
)
from agent.coo.execution_contract import SkillExecutionMode, SkillExecutionRequest
from agent.coo.execution_provider import ExecutionProvider, ExecutionProviderResult


class WorkerExecutionMode(str, Enum):
    """How a worker invocation should behave."""

    PLAN_ONLY = "plan_only"
    DRY_RUN = "dry_run"
    EXECUTE = "execute"  # defined for Phase 4; not used in Phase 3C


@dataclass(frozen=True)
class WorkerArtifactRef:
    """Read-only artifact reference — path only, no content blob."""

    path: str
    kind: str
    summary: str = ""


@dataclass
class WorkerContext:
    """Turn-local snapshot passed to worker plan/perform methods.

    **Execution path:** use ``execution_provider`` only. ``ExecutionProvider``
    (via ``PipelineAdapter``) owns pipeline configuration and ``pipeline_root``.

    **Do not** read ``pipeline_root`` for dispatch — it is a legacy compatibility
    field for pre-Phase-4C callers. Never combine ``pipeline_root`` with
    ``execution_provider`` configuration; pick the provider path only.
    """

    assignment: WorkerAssignment
    plan: ExecutionPlan
    policy: PolicyDecision
    skill_invocations: List[SkillInvocation]
    run_date: str
    pipeline_root: str  # Deprecated for execution path. ExecutionProvider owns pipeline configuration.
    mode: WorkerExecutionMode = WorkerExecutionMode.PLAN_ONLY
    prior_results: List["WorkerResult"] = field(default_factory=list)
    execution_provider: Optional[ExecutionProvider] = None
    auto_apply: bool = False
    review_required: bool = True


@dataclass
class WorkerResult:
    """Outcome of a worker plan or dry-run perform call."""

    worker_id: str
    assignment_id: str
    status: WorkerStatus
    phases: List[PlanPhase]
    skill_invocations: List[SkillInvocation]
    summary: str
    mode: WorkerExecutionMode
    artifacts: List[WorkerArtifactRef] = field(default_factory=list)
    dry_run: bool = True
    approval_required: bool = False
    review_required: bool = True
    auto_apply: bool = False
    reason: str = ""
    provider_results: List[ExecutionProviderResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "assignment_id": self.assignment_id,
            "status": self.status.value,
            "phases": [phase.value for phase in self.phases],
            "skill_invocations": [
                {
                    "skill_id": skill.skill_id,
                    "skill_name": skill.skill_name,
                    "phase": skill.phase.value,
                    "status": skill.status.value,
                    "reason": skill.reason,
                    "entrypoint_hint": skill.entrypoint_hint,
                    "dry_run": skill.dry_run,
                    "requires_ceo_approval": skill.requires_ceo_approval,
                }
                for skill in self.skill_invocations
            ],
            "summary": self.summary,
            "mode": self.mode.value,
            "artifacts": [
                {"path": artifact.path, "kind": artifact.kind, "summary": artifact.summary}
                for artifact in self.artifacts
            ],
            "dry_run": self.dry_run,
            "approval_required": self.approval_required,
            "review_required": self.review_required,
            "auto_apply": self.auto_apply,
            "reason": self.reason,
            "provider_results": [result.to_dict() for result in self.provider_results],
        }


class BaseWorker(ABC):
    """Abstract worker — plan and dry-run perform only in Phase 3C."""

    worker_id: str
    department_id: str
    supported_phases: tuple[PlanPhase, ...]

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        """Return worker metadata for registry / CEO reporting."""

    def plan(self, ctx: WorkerContext) -> WorkerResult:
        """Produce a dry-run plan result from context (no skill execution)."""
        return self._dry_run_result(ctx, action="plan")

    def perform(self, ctx: WorkerContext) -> WorkerResult:
        """Template-method perform — Phase 3C dry-run only via ``_do_perform``."""
        self._before_perform(ctx)
        result = self._do_perform(ctx)
        return self._after_perform(ctx, result)

    def _before_perform(self, ctx: WorkerContext) -> None:
        """Pre-perform hook — override in Phase 4 for validation/setup."""

    def _do_perform(self, ctx: WorkerContext) -> WorkerResult:
        """Core perform step. Phase 4C: provider plan only; no skill execution."""
        if ctx.mode is WorkerExecutionMode.EXECUTE:
            return self._dry_run_result(ctx, action="perform")

        provider = ctx.execution_provider or ExecutionProvider()
        provider_results = [
            provider.plan(self._skill_execution_request_for(ctx, invocation))
            for invocation in ctx.skill_invocations
        ]

        result = self._dry_run_result(ctx, action="perform")
        result.provider_results = provider_results
        if provider_results:
            skill_ids = ", ".join(item.skill_id for item in provider_results)
            result.summary = f"{result.summary} ExecutionProvider planned: {skill_ids}."
        return result

    @staticmethod
    def _worker_mode_to_skill_mode(mode: WorkerExecutionMode) -> SkillExecutionMode:
        if mode is WorkerExecutionMode.PLAN_ONLY:
            return SkillExecutionMode.PLAN_ONLY
        if mode is WorkerExecutionMode.DRY_RUN:
            return SkillExecutionMode.DRY_RUN
        return SkillExecutionMode.EXECUTE

    @staticmethod
    def _skill_execution_request(
        ctx: WorkerContext,
        invocation: SkillInvocation,
        *,
        worker_id: str,
    ) -> SkillExecutionRequest:
        """Build a contract request from worker context — no R2 paths on the request."""
        return SkillExecutionRequest(
            skill_id=invocation.skill_id,
            worker_id=worker_id,
            assignment_id=ctx.assignment.assignment_id,
            run_date=ctx.run_date,
            mode=BaseWorker._worker_mode_to_skill_mode(ctx.mode),
            phase=invocation.phase,
            auto_apply=False,
            review_required=True,
            worker_mode=ctx.mode.value,
        )

    def _skill_execution_request_for(self, ctx: WorkerContext, invocation: SkillInvocation) -> SkillExecutionRequest:
        return BaseWorker._skill_execution_request(ctx, invocation, worker_id=self.worker_id)

    def _after_perform(self, ctx: WorkerContext, result: WorkerResult) -> WorkerResult:
        """Post-perform hook — override in Phase 4 for result normalization."""
        return result

    def _dry_run_result(self, ctx: WorkerContext, *, action: str) -> WorkerResult:
        assignment = ctx.assignment
        status = assignment.status
        if status is WorkerStatus.PLANNED and ctx.mode is not WorkerExecutionMode.PLAN_ONLY:
            status = WorkerStatus.SELECTED

        summary = assignment.reason or (
            f"Dry-run {action} for {self.worker_id} "
            f"({', '.join(p.value for p in assignment.phases)})"
        )

        return WorkerResult(
            worker_id=self.worker_id,
            assignment_id=assignment.assignment_id,
            status=status,
            phases=list(assignment.phases),
            skill_invocations=list(ctx.skill_invocations),
            summary=summary,
            mode=ctx.mode,
            dry_run=True,
            approval_required=assignment.requires_ceo_approval,
            review_required=True,
            auto_apply=False,
            reason=assignment.reason,
        )
