"""Worker and department registry — organizational chart as static data.

Departments are logical groupings only. Runtime staffing flows through
WorkerManager → WorkerAssignment; COO never addresses individual workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from agent.coo.models import PlanPhase


@dataclass(frozen=True)
class DepartmentDefinition:
    department_id: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class WorkerDefinition:
    worker_id: str
    name: str
    department_id: str
    phases: tuple[PlanPhase, ...]
    skill_ids: tuple[str, ...] = ()
    read_only: bool = False
    requires_ceo_approval: bool = False


DEPARTMENT_REGISTRY: Dict[str, DepartmentDefinition] = {
    "research": DepartmentDefinition(
        department_id="research",
        name="Research Department",
        description="Gather external and internal signals before editorial strategy.",
    ),
    "strategy": DepartmentDefinition(
        department_id="strategy",
        name="Strategy Department",
        description=(
            "Editorial strategy (topic selection, content direction). "
            "Not Repository 2 Strategy Observer."
        ),
    ),
    "writing": DepartmentDefinition(
        department_id="writing",
        name="Writing Department",
        description="Produce draft content and supporting assets.",
    ),
    "quality": DepartmentDefinition(
        department_id="quality",
        name="Quality Department",
        description="Validate content quality before approval queue.",
    ),
    "approval": DepartmentDefinition(
        department_id="approval",
        name="Approval Department",
        description="Prepare approval surfaces and CEO decision support.",
    ),
    "publishing": DepartmentDefinition(
        department_id="publishing",
        name="Publishing Department",
        description="Post-approval dispatch with scheduler awareness.",
    ),
    "learning": DepartmentDefinition(
        department_id="learning",
        name="Learning Department",
        description="Capture and propose learnings without auto-applying them.",
    ),
    "reporting": DepartmentDefinition(
        department_id="reporting",
        name="Reporting Department",
        description="Operational visibility for the CEO.",
    ),
}


WORKER_REGISTRY: Dict[str, WorkerDefinition] = {
    "research_worker": WorkerDefinition(
        worker_id="research_worker",
        name="Research Worker",
        department_id="research",
        phases=(PlanPhase.RESEARCH,),
        skill_ids=("trend_skill", "keyword_skill", "news_skill", "competitor_skill"),
    ),
    "strategy_worker": WorkerDefinition(
        worker_id="strategy_worker",
        name="Strategy Worker",
        department_id="strategy",
        phases=(PlanPhase.STRATEGY,),
        skill_ids=("topic_selection_skill", "strategy_brief_skill"),
        requires_ceo_approval=True,
    ),
    "draft_worker": WorkerDefinition(
        worker_id="draft_worker",
        name="Draft Worker",
        department_id="writing",
        phases=(PlanPhase.WRITER,),
        skill_ids=("draft_skill", "seo_skill", "formatting_skill", "image_planning_skill"),
    ),
    "quality_worker": WorkerDefinition(
        worker_id="quality_worker",
        name="Quality Worker",
        department_id="quality",
        phases=(PlanPhase.QUALITY,),
        skill_ids=("quality_check_skill",),
    ),
    "verify_worker": WorkerDefinition(
        worker_id="verify_worker",
        name="Verify Worker",
        department_id="quality",
        phases=(PlanPhase.VERIFY,),
        skill_ids=("verify_runtime",),
        requires_ceo_approval=True,
    ),
    "approval_worker": WorkerDefinition(
        worker_id="approval_worker",
        name="Approval Worker",
        department_id="approval",
        phases=(PlanPhase.APPROVAL_QUEUE, PlanPhase.APPROVAL_CHECK),
        skill_ids=("approval_queue_skill", "risk_check_skill", "approval_decision_skill"),
        requires_ceo_approval=True,
    ),
    "publisher_worker": WorkerDefinition(
        worker_id="publisher_worker",
        name="Publisher Worker",
        department_id="publishing",
        phases=(PlanPhase.PUBLISHER,),
        skill_ids=("publish_content",),
        requires_ceo_approval=True,
    ),
    "reporting_worker": WorkerDefinition(
        worker_id="reporting_worker",
        name="Reporting Worker",
        department_id="reporting",
        phases=(PlanPhase.CEO_REPORT,),
        skill_ids=("approval_review", "daily_brief"),
        read_only=True,
    ),
}


PHASE_TO_WORKER_ID: Dict[PlanPhase, str] = {
    PlanPhase.RESEARCH: "research_worker",
    PlanPhase.STRATEGY: "strategy_worker",
    PlanPhase.WRITER: "draft_worker",
    PlanPhase.QUALITY: "quality_worker",
    PlanPhase.APPROVAL_QUEUE: "approval_worker",
    PlanPhase.APPROVAL_CHECK: "approval_worker",
    PlanPhase.CEO_REPORT: "reporting_worker",
    PlanPhase.PUBLISHER: "publisher_worker",
    PlanPhase.VERIFY: "verify_worker",
}


def get_worker(worker_id: str) -> Optional[WorkerDefinition]:
    return WORKER_REGISTRY.get(worker_id)


def get_department(department_id: str) -> Optional[DepartmentDefinition]:
    return DEPARTMENT_REGISTRY.get(department_id)


def worker_for_phase(phase: PlanPhase) -> Optional[WorkerDefinition]:
    worker_id = PHASE_TO_WORKER_ID.get(phase)
    if worker_id is None:
        return None
    return WORKER_REGISTRY.get(worker_id)
