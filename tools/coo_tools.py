"""COO orchestration tool — exposes Hermes COO planning to the agent loop.

This tool plans and selects Execution Engine skills only. It never executes
skills, mutates Repository 2 artifacts, auto-approves, or auto-publishes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.coo.models import COOOrchestrationResult, SkillInvocation, SkillInvocationStatus, WorkerAssignment
from agent.coo.orchestrator import COOOrchestrator
from tools.registry import registry, tool_error, tool_result

# Non-negotiable safeguards — enforced at the tool boundary regardless of policy output.
_AUTO_APPLY = False
_REVIEW_REQUIRED = True


def _skill_payload(skill: SkillInvocation) -> Dict[str, Any]:
    return {
        "skill_id": skill.skill_id,
        "skill_name": skill.skill_name,
        "phase": skill.phase.value,
        "status": skill.status.value,
        "reason": skill.reason,
        "entrypoint_hint": skill.entrypoint_hint,
        "dry_run": skill.dry_run,
        "requires_ceo_approval": skill.requires_ceo_approval,
    }


def _skills_by_status(
    skills: List[SkillInvocation],
    status: SkillInvocationStatus,
) -> List[Dict[str, Any]]:
    return [_skill_payload(skill) for skill in skills if skill.status is status]


def _assignment_payload(assignment: WorkerAssignment) -> Dict[str, Any]:
    return assignment.to_dict()


def _format_tool_response(result: COOOrchestrationResult) -> Dict[str, Any]:
    return {
        "intent": {
            "raw_text": result.intent.raw_text,
            "task_kind": result.intent.task_kind.value,
            "run_date": result.intent.run_date,
            "confidence": result.intent.confidence,
            "signals": result.intent.signals,
        },
        "plan": {
            "task_kind": result.plan.task_kind.value,
            "run_date": result.plan.run_date,
            "summary": result.plan.summary,
            "steps": [
                {
                    "phase": step.phase.value,
                    "description": step.description,
                    "skill_id": step.skill_id,
                    "required": step.required,
                }
                for step in result.plan.steps
            ],
            "notes": result.plan.notes,
        },
        "policy_decisions": [
            {
                "rule": rule.rule,
                "verdict": rule.verdict.value,
                "message": rule.message,
            }
            for rule in result.policy.rules
        ],
        "selected_skills": _skills_by_status(result.skills, SkillInvocationStatus.SELECTED),
        "blocked_skills": _skills_by_status(result.skills, SkillInvocationStatus.BLOCKED),
        "deferred_skills": _skills_by_status(result.skills, SkillInvocationStatus.DEFERRED),
        "approval_required": result.policy.requires_ceo_approval,
        "review_required": _REVIEW_REQUIRED,
        "auto_apply": _AUTO_APPLY,
        "summary": result.plan.summary or result.policy.summary,
        "worker_assignments": [
            _assignment_payload(assignment) for assignment in result.worker_assignments
        ],
        "runtime_summary": (
            result.worker_runtime_result.summary
            if result.worker_runtime_result is not None
            else ""
        ),
        "runtime_status": (
            result.worker_runtime_result.status.value
            if result.worker_runtime_result is not None
            else ""
        ),
        "runtime_provider_results": [
            provider_result.to_dict()
            for provider_result in (
                result.worker_runtime_result.provider_results
                if result.worker_runtime_result is not None
                else []
            )
        ],
        "ceo_message": result.ceo_message,
        "next_actions": result.next_actions,
    }


def coo_orchestrate(
    ceo_message: str,
    run_date: Optional[str] = None,
    orchestrator: Optional[COOOrchestrator] = None,
) -> str:
    """Analyze CEO intent and produce plan/policy/skill selection (no execution)."""
    if not ceo_message or not ceo_message.strip():
        return tool_error("ceo_message is required")

    engine = orchestrator or COOOrchestrator()
    result = engine.orchestrate(ceo_message.strip(), run_date=run_date)
    return tool_result(_format_tool_response(result))


def check_coo_requirements() -> bool:
    """COO orchestration has no external credential requirements."""
    return True


COO_ORCHESTRATE_SCHEMA = {
    "name": "coo_orchestrate",
    "description": (
        "Hermes COO orchestration for the content Execution Engine (Repository 2). "
        "Analyzes CEO intent, builds an execution plan, evaluates policy against "
        "current pipeline state, and selects skills — **without executing them**.\n\n"
        "Safeguards (non-negotiable): auto_apply is always false, review_required "
        "is always true. Never auto-approve, auto-publish, auto-apply strategy, "
        "or auto-apply learning. Does not mutate Repository 2 files.\n\n"
        "Use for CEO requests such as content creation/reporting, approval review, "
        "or daily pipeline briefs. Read `formatted_report` / `ceo_message` in the "
        "response before invoking any Execution Engine skill."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ceo_message": {
                "type": "string",
                "description": (
                    "CEO natural-language request, e.g. '오늘 블로그 글 작성해서 보고해' "
                    "or '승인하고 발행해'."
                ),
            },
            "run_date": {
                "type": "string",
                "description": "Optional pipeline run date (YYYY-MM-DD). Defaults to today.",
            },
        },
        "required": ["ceo_message"],
    },
}


def _handle_coo_orchestrate(args: Dict[str, Any], **kwargs: Any) -> str:
    return coo_orchestrate(
        ceo_message=args.get("ceo_message", ""),
        run_date=args.get("run_date"),
    )


registry.register(
    name="coo_orchestrate",
    toolset="coo",
    schema=COO_ORCHESTRATE_SCHEMA,
    handler=_handle_coo_orchestrate,
    check_fn=check_coo_requirements,
    emoji="🏢",
)
