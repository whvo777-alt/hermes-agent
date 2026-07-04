"""COO domain models — intent, plan, policy, and skill selection artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskKind(str, Enum):
    """CEO-facing work categories mapped from natural language."""

    CREATE_AND_REPORT = "create_and_report"
    APPROVE_AND_PUBLISH = "approve_and_publish"
    REVIEW_APPROVALS = "review_approvals"
    DAILY_BRIEF = "daily_brief"
    VERIFY_RUNTIME = "verify_runtime"
    UNKNOWN = "unknown"


class PlanPhase(str, Enum):
    """Logical Execution Engine phases (COO view, not engine internals)."""

    RESEARCH = "research"
    STRATEGY = "strategy"
    WRITER = "writer"
    QUALITY = "quality"
    APPROVAL_QUEUE = "approval_queue"
    CEO_REPORT = "ceo_report"
    PUBLISH_WAIT = "publish_wait"
    APPROVAL_CHECK = "approval_check"
    PUBLISHER = "publisher"
    VERIFY = "verify"


class PolicyVerdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    DEFER = "defer"
    REQUIRE_CEO = "require_ceo"


class SkillInvocationStatus(str, Enum):
    SELECTED = "selected"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    SKIPPED = "skipped"


@dataclass
class IntentResult:
    raw_text: str
    task_kind: TaskKind
    run_date: Optional[str] = None
    confidence: float = 0.0
    signals: List[str] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanStep:
    phase: PlanPhase
    description: str
    skill_id: Optional[str] = None
    required: bool = True


@dataclass
class ExecutionPlan:
    task_kind: TaskKind
    run_date: str
    steps: List[PlanStep]
    summary: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class ApprovalSnapshot:
    total: int = 0
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    queue_exists: bool = False
    queue_path: Optional[str] = None


@dataclass
class PublishingSnapshot:
    plan_exists: bool = False
    confirmed_queue_exists: bool = False
    confirmed_item_count: int = 0
    dispatch_result_exists: bool = False


@dataclass
class RuntimeSnapshot:
    scheduler_active: bool = False
    scheduler_status_path: Optional[str] = None
    scheduler_state: Optional[str] = None


@dataclass
class PipelineState:
    """Read-only view of Execution Engine artifacts for policy decisions."""

    run_date: str
    pipeline_root: str
    run_report_exists: bool = False
    daily_assignments_exists: bool = False
    approvals: ApprovalSnapshot = field(default_factory=ApprovalSnapshot)
    publishing: PublishingSnapshot = field(default_factory=PublishingSnapshot)
    runtime: RuntimeSnapshot = field(default_factory=RuntimeSnapshot)
    warnings: List[str] = field(default_factory=list)


@dataclass
class PolicyRuleResult:
    rule: str
    verdict: PolicyVerdict
    message: str


@dataclass
class PolicyDecision:
    verdict: PolicyVerdict
    auto_apply: bool
    review_required: bool
    requires_ceo_approval: bool
    allowed_phases: List[PlanPhase] = field(default_factory=list)
    blocked_phases: List[PlanPhase] = field(default_factory=list)
    deferred_phases: List[PlanPhase] = field(default_factory=list)
    rules: List[PolicyRuleResult] = field(default_factory=list)
    summary: str = ""


@dataclass
class SkillInvocation:
    skill_id: str
    skill_name: str
    phase: PlanPhase
    status: SkillInvocationStatus
    reason: str = ""
    entrypoint_hint: str = ""
    dry_run: bool = True
    requires_ceo_approval: bool = False


@dataclass
class COOOrchestrationResult:
    intent: IntentResult
    state: PipelineState
    plan: ExecutionPlan
    policy: PolicyDecision
    skills: List[SkillInvocation]
    ceo_message: str = ""
    next_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": {
                "raw_text": self.intent.raw_text,
                "task_kind": self.intent.task_kind.value,
                "run_date": self.intent.run_date,
                "confidence": self.intent.confidence,
                "signals": self.intent.signals,
            },
            "state": {
                "run_date": self.state.run_date,
                "pipeline_root": self.state.pipeline_root,
                "approvals": {
                    "total": self.state.approvals.total,
                    "pending": self.state.approvals.pending,
                    "approved": self.state.approvals.approved,
                    "queue_exists": self.state.approvals.queue_exists,
                },
                "publishing": {
                    "plan_exists": self.state.publishing.plan_exists,
                    "confirmed_queue_exists": self.state.publishing.confirmed_queue_exists,
                    "confirmed_item_count": self.state.publishing.confirmed_item_count,
                },
                "runtime": {
                    "scheduler_active": self.state.runtime.scheduler_active,
                    "scheduler_state": self.state.runtime.scheduler_state,
                },
                "warnings": self.state.warnings,
            },
            "plan": {
                "task_kind": self.plan.task_kind.value,
                "run_date": self.plan.run_date,
                "summary": self.plan.summary,
                "steps": [
                    {
                        "phase": step.phase.value,
                        "description": step.description,
                        "skill_id": step.skill_id,
                        "required": step.required,
                    }
                    for step in self.plan.steps
                ],
            },
            "policy": {
                "verdict": self.policy.verdict.value,
                "auto_apply": self.policy.auto_apply,
                "review_required": self.policy.review_required,
                "requires_ceo_approval": self.policy.requires_ceo_approval,
                "summary": self.policy.summary,
                "allowed_phases": [p.value for p in self.policy.allowed_phases],
                "blocked_phases": [p.value for p in self.policy.blocked_phases],
                "deferred_phases": [p.value for p in self.policy.deferred_phases],
                "rules": [
                    {
                        "rule": rule.rule,
                        "verdict": rule.verdict.value,
                        "message": rule.message,
                    }
                    for rule in self.policy.rules
                ],
            },
            "skills": [
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
                for skill in self.skills
            ],
            "ceo_message": self.ceo_message,
            "next_actions": self.next_actions,
        }
