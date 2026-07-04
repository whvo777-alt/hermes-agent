"""Execution Engine skill catalog — COO-facing skill metadata only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from agent.coo.models import PlanPhase


# Risk categories — source of truth for execution boundary evaluation.
RISK_CATEGORY_STANDARD = "standard"
RISK_CATEGORY_PUBLISH = "publish"
RISK_CATEGORY_APPROVAL_DECISION = "approval_decision"
RISK_CATEGORY_LEARNING_APPLY = "learning_apply"
RISK_CATEGORY_STRATEGY_APPLY = "strategy_apply"


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    name: str
    description: str
    entrypoint_hint: str
    phases: tuple[PlanPhase, ...]
    read_only: bool = False
    requires_ceo_approval: bool = False
    risk_category: str = RISK_CATEGORY_STANDARD


SKILL_CATALOG: Dict[str, SkillDefinition] = {
    "create_content": SkillDefinition(
        skill_id="create_content",
        name="Create Content",
        description=(
            "Run the content pipeline (research → writing → quality → approval queue). "
            "Wraps pipeline.js without mutating prompts, strategy, or learning."
        ),
        entrypoint_hint="node pipeline.js",
        phases=(
            PlanPhase.RESEARCH,
            PlanPhase.STRATEGY,
            PlanPhase.WRITER,
            PlanPhase.QUALITY,
            PlanPhase.APPROVAL_QUEUE,
        ),
    ),
    "approval_review": SkillDefinition(
        skill_id="approval_review",
        name="Approval Review",
        description="Prepare and validate approval surfaces for CEO review.",
        entrypoint_hint="npm run preview:approvals",
        phases=(PlanPhase.CEO_REPORT, PlanPhase.APPROVAL_CHECK),
        read_only=True,
    ),
    "publish_content": SkillDefinition(
        skill_id="publish_content",
        name="Publish Content",
        description=(
            "Post-approval publishing orchestration. Never auto-approves; "
            "respects scheduler runtime ownership of dispatch."
        ),
        entrypoint_hint="npm run preflight:publish",
        phases=(PlanPhase.PUBLISHER,),
        requires_ceo_approval=True,
        risk_category=RISK_CATEGORY_PUBLISH,
    ),
    "daily_brief": SkillDefinition(
        skill_id="daily_brief",
        name="Daily Brief",
        description="Read-only operational briefing for the CEO.",
        entrypoint_hint="read outputs/<date>/_reports/*",
        phases=(PlanPhase.CEO_REPORT,),
        read_only=True,
    ),
    "verify_runtime": SkillDefinition(
        skill_id="verify_runtime",
        name="Verify Runtime",
        description="Closed Beta verification chain and runtime smoke checks.",
        entrypoint_hint="npm run verify",
        phases=(PlanPhase.VERIFY,),
    ),
}


def get_skill(skill_id: str) -> Optional[SkillDefinition]:
    return SKILL_CATALOG.get(skill_id)


def resolve_risk_category(skill_id: str) -> str:
    """Return risk_category from catalog metadata; unknown skills are standard."""
    definition = get_skill(skill_id)
    if definition is None:
        return RISK_CATEGORY_STANDARD
    return definition.risk_category
