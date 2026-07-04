"""Skill Selection — map approved plan phases to Execution Engine skills."""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from agent.coo.models import (
    ExecutionPlan,
    PlanPhase,
    PlanStep,
    PolicyDecision,
    PolicyVerdict,
    SkillInvocation,
    SkillInvocationStatus,
)
from agent.coo.skills_catalog import SKILL_CATALOG, SkillDefinition, get_skill


class SkillSelector:
    """Select skills to invoke based on planner output and policy decision."""

    def select(self, plan: ExecutionPlan, policy: PolicyDecision) -> List[SkillInvocation]:
        if policy.verdict is PolicyVerdict.BLOCK and not policy.allowed_phases:
            return []

        selected: Dict[str, SkillInvocation] = {}
        for step in plan.steps:
            invocation = self._select_for_step(step, policy)
            if invocation is None:
                continue
            existing = selected.get(invocation.skill_id)
            if existing is None or self._status_rank(invocation.status) > self._status_rank(existing.status):
                selected[invocation.skill_id] = invocation

        return list(selected.values())

    def _select_for_step(
        self,
        step: PlanStep,
        policy: PolicyDecision,
    ) -> Optional[SkillInvocation]:
        status = self._phase_status(step.phase, policy)
        if status is SkillInvocationStatus.SKIPPED:
            return None

        skill_id = step.skill_id or self._default_skill_for_phase(step.phase)
        if not skill_id:
            return SkillInvocation(
                skill_id="none",
                skill_name="(none)",
                phase=step.phase,
                status=SkillInvocationStatus.BLOCKED,
                reason=f"No skill mapped for phase {step.phase.value}",
            )

        definition = get_skill(skill_id)
        if definition is None:
            return SkillInvocation(
                skill_id=skill_id,
                skill_name=skill_id,
                phase=step.phase,
                status=SkillInvocationStatus.BLOCKED,
                reason=f"Unknown skill id: {skill_id}",
            )

        return SkillInvocation(
            skill_id=definition.skill_id,
            skill_name=definition.name,
            phase=step.phase,
            status=status,
            reason=self._reason_for_status(step, policy, status),
            entrypoint_hint=definition.entrypoint_hint,
            dry_run=not (step.phase is PlanPhase.PUBLISHER and status is SkillInvocationStatus.SELECTED),
            requires_ceo_approval=policy.requires_ceo_approval
            or step.phase in {PlanPhase.APPROVAL_QUEUE, PlanPhase.PUBLISHER, PlanPhase.PUBLISH_WAIT},
        )

    def _phase_status(self, phase: PlanPhase, policy: PolicyDecision) -> SkillInvocationStatus:
        allowed: Set[PlanPhase] = set(policy.allowed_phases)
        blocked: Set[PlanPhase] = set(policy.blocked_phases)
        deferred: Set[PlanPhase] = set(policy.deferred_phases)

        if phase in blocked:
            return SkillInvocationStatus.BLOCKED
        if phase in deferred:
            return SkillInvocationStatus.DEFERRED
        if phase in allowed:
            return SkillInvocationStatus.SELECTED
        if phase is PlanPhase.PUBLISH_WAIT:
            return SkillInvocationStatus.DEFERRED
        if policy.verdict in {PolicyVerdict.REQUIRE_CEO, PolicyVerdict.DEFER}:
            return SkillInvocationStatus.DEFERRED
        return SkillInvocationStatus.SKIPPED

    def _default_skill_for_phase(self, phase: PlanPhase) -> Optional[str]:
        for skill_id, definition in SKILL_CATALOG.items():
            if phase in definition.phases:
                return skill_id
        return None

    def _reason_for_status(
        self,
        step: PlanStep,
        policy: PolicyDecision,
        status: SkillInvocationStatus,
    ) -> str:
        if status is SkillInvocationStatus.SELECTED:
            return step.description
        if status is SkillInvocationStatus.DEFERRED:
            return policy.summary or f"Phase {step.phase.value} deferred by policy."
        if status is SkillInvocationStatus.BLOCKED:
            return policy.summary or f"Phase {step.phase.value} blocked by policy."
        return step.description

    @staticmethod
    def _status_rank(status: SkillInvocationStatus) -> int:
        return {
            SkillInvocationStatus.SKIPPED: 0,
            SkillInvocationStatus.DEFERRED: 1,
            SkillInvocationStatus.BLOCKED: 2,
            SkillInvocationStatus.SELECTED: 3,
        }[status]
