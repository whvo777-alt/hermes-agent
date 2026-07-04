"""Execution Contract — skill request/result models and boundary evaluation.

Phase 3D: contract definition and evaluation only. No subprocess, no Repository 2
mutation, no actual skill dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from agent.coo.models import PlanPhase
from agent.coo.skills_catalog import (
    RISK_CATEGORY_APPROVAL_DECISION,
    RISK_CATEGORY_LEARNING_APPLY,
    RISK_CATEGORY_PUBLISH,
    RISK_CATEGORY_STRATEGY_APPLY,
    resolve_risk_category,
)


class SkillExecutionMode(str, Enum):
    """How a skill invocation should behave."""

    PLAN_ONLY = "plan_only"
    DRY_RUN = "dry_run"
    EXECUTE = "execute"  # evaluated in Phase 3D; dispatched in Phase 4+


class SkillExecutionStatus(str, Enum):
    """Lifecycle status for a skill execution attempt."""

    PLANNED = "planned"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Mode strength: skill mode must not exceed worker mode.
_MODE_RANK: Dict[str, int] = {
    SkillExecutionMode.PLAN_ONLY.value: 0,
    SkillExecutionMode.DRY_RUN.value: 1,
    SkillExecutionMode.EXECUTE.value: 2,
}


@dataclass(frozen=True)
class ExecutionArtifactRef:
    """Read-only artifact reference — path metadata only, never a content blob."""

    path: str
    kind: str
    summary: str = ""


@dataclass(frozen=True)
class ExecutionBoundaryPolicy:
    """Non-negotiable execution gates between Hermes Agent and Repository 2."""

    auto_apply: bool = False
    review_required: bool = True
    allow_publish: bool = False
    allow_approval_decision: bool = False
    allow_learning_apply: bool = False
    allow_strategy_apply: bool = False
    repository2_read_only: bool = True


@dataclass
class SkillExecutionRequest:
    """Worker → Skill invocation request (no execution in Phase 3D)."""

    skill_id: str
    worker_id: str
    assignment_id: str
    run_date: str
    mode: SkillExecutionMode = SkillExecutionMode.PLAN_ONLY
    phase: Optional[PlanPhase] = None
    entrypoint_hint: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    auto_apply: bool = False
    review_required: bool = True
    worker_mode: Optional[str] = None  # plan_only | dry_run | execute (Worker layer)


@dataclass
class SkillExecutionResult:
    """Outcome of boundary evaluation or (future) skill dispatch."""

    skill_id: str
    status: SkillExecutionStatus
    mode: SkillExecutionMode
    dry_run: bool
    summary: str
    artifacts: List[ExecutionArtifactRef] = field(default_factory=list)
    blocked_reason: str = ""
    auto_apply: bool = False
    review_required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "status": self.status.value,
            "mode": self.mode.value,
            "dry_run": self.dry_run,
            "summary": self.summary,
            "artifacts": [
                {"path": artifact.path, "kind": artifact.kind, "summary": artifact.summary}
                for artifact in self.artifacts
            ],
            "blocked_reason": self.blocked_reason,
            "auto_apply": self.auto_apply,
            "review_required": self.review_required,
        }


def _normalize_mode(mode: Union[str, SkillExecutionMode, Enum]) -> str:
    """Normalize worker/skill mode to a canonical string (avoids worker_interface import)."""
    if isinstance(mode, Enum):
        return str(mode.value).lower()
    return str(mode).lower()


def validate_mode_compatibility(
    worker_mode: Union[str, SkillExecutionMode, Enum],
    skill_mode: Union[str, SkillExecutionMode, Enum],
) -> Tuple[bool, Optional[str]]:
    """Skill execution mode must not be stronger than the worker's mode.

    Worker PLAN_ONLY → skill PLAN_ONLY only.
    Worker DRY_RUN   → skill PLAN_ONLY or DRY_RUN.
    Worker EXECUTE   → skill PLAN_ONLY, DRY_RUN, or EXECUTE.

    Accepts enum instances or string values (``plan_only``, ``dry_run``, ``execute``)
    so callers need not import ``WorkerExecutionMode`` from ``worker_interface``.
    """
    worker_key = _normalize_mode(worker_mode)
    skill_key = _normalize_mode(skill_mode)
    worker_rank = _MODE_RANK.get(worker_key)
    skill_rank = _MODE_RANK.get(skill_key)
    if worker_rank is None:
        return False, f"Unknown worker mode: {worker_mode!r}"
    if skill_rank is None:
        return False, f"Unknown skill mode: {skill_mode!r}"
    if skill_rank > worker_rank:
        return False, (
            f"Skill mode {skill_key!r} exceeds worker mode {worker_key!r} "
            "(skill execution cannot be stronger than worker execution)."
        )
    return True, None


def default_boundary_policy() -> ExecutionBoundaryPolicy:
    """Return the canonical closed-beta execution boundary defaults."""
    return ExecutionBoundaryPolicy()


def _boundary_violation(
    request: SkillExecutionRequest,
    boundary: ExecutionBoundaryPolicy,
) -> Optional[str]:
    if request.auto_apply and not boundary.auto_apply:
        return "auto_apply is forbidden (autoApply must remain false)."
    if not request.review_required and boundary.review_required:
        return "review_required must remain true."

    risk = resolve_risk_category(request.skill_id)
    if risk == RISK_CATEGORY_PUBLISH and not boundary.allow_publish:
        return "Publish skills are blocked until CEO explicitly authorizes publishing."
    if risk == RISK_CATEGORY_APPROVAL_DECISION and not boundary.allow_approval_decision:
        return "Approval decision skills are blocked — no auto-approval."
    if risk == RISK_CATEGORY_LEARNING_APPLY and not boundary.allow_learning_apply:
        return "Learning apply skills are blocked — no learning auto-apply."
    if risk == RISK_CATEGORY_STRATEGY_APPLY and not boundary.allow_strategy_apply:
        return "Strategy apply skills are blocked — no strategy auto-apply."
    return None


def _mode_compatibility_violation(request: SkillExecutionRequest) -> Optional[str]:
    if request.worker_mode is None:
        return None
    ok, error = validate_mode_compatibility(request.worker_mode, request.mode)
    if not ok:
        return error
    return None


def evaluate_skill_execution(
    request: SkillExecutionRequest,
    boundary: Optional[ExecutionBoundaryPolicy] = None,
) -> SkillExecutionResult:
    """Evaluate a skill request against boundary policy — no engine dispatch."""
    policy = boundary or default_boundary_policy()

    mode_error = _mode_compatibility_violation(request)
    if mode_error:
        return SkillExecutionResult(
            skill_id=request.skill_id,
            status=SkillExecutionStatus.BLOCKED,
            mode=request.mode,
            dry_run=True,
            summary=f"Blocked: {request.skill_id}",
            blocked_reason=mode_error,
            auto_apply=False,
            review_required=True,
        )

    violation = _boundary_violation(request, policy)
    if violation:
        return SkillExecutionResult(
            skill_id=request.skill_id,
            status=SkillExecutionStatus.BLOCKED,
            mode=request.mode,
            dry_run=True,
            summary=f"Blocked: {request.skill_id}",
            blocked_reason=violation,
            auto_apply=False,
            review_required=True,
        )

    dry_run = request.mode is not SkillExecutionMode.EXECUTE
    if request.mode is SkillExecutionMode.PLAN_ONLY:
        status = SkillExecutionStatus.PLANNED
        summary = f"Planned skill evaluation for {request.skill_id} (no dispatch)."
    elif request.mode is SkillExecutionMode.DRY_RUN:
        status = SkillExecutionStatus.PLANNED
        summary = f"Dry-run validation for {request.skill_id} (no dispatch)."
    else:
        # EXECUTE: contract evaluation only in Phase 3D — adapter dispatch is Phase 4.
        status = SkillExecutionStatus.PLANNED
        summary = (
            f"Execute intent recorded for {request.skill_id}; "
            "adapter dispatch deferred to Phase 4."
        )

    return SkillExecutionResult(
        skill_id=request.skill_id,
        status=status,
        mode=request.mode,
        dry_run=dry_run,
        summary=summary,
        auto_apply=False,
        review_required=True,
    )


def validate_skill_execution_request(
    request: SkillExecutionRequest,
    boundary: Optional[ExecutionBoundaryPolicy] = None,
) -> Tuple[bool, Optional[str]]:
    """Return (ok, error_message). Fails closed on boundary violations."""
    mode_error = _mode_compatibility_violation(request)
    if mode_error:
        return False, mode_error

    policy = boundary or default_boundary_policy()
    violation = _boundary_violation(request, policy)
    if violation:
        return False, violation
    return True, None
