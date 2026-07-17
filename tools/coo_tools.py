"""COO orchestration tool — exposes Hermes COO planning to the agent loop.

For most intents this tool only plans/selects skills (no side effects).
**Exception**: when intent is CREATE_AND_REPORT (e.g. "오늘 블로그 글 4개
작성해서 보고해줘"), it runs Hermes' own content pipeline
(agent/content/orchestrator.py — Research→Planning→Writing→Quality, real
LLM calls, local file writes under Hermes' own data dir) for the 4
launch-policy platforms and wraps the results in one CEO approval bundle.
It never calls a platform publisher, never auto-approves, and never touches
Repository 2 (multi-content-pipeline) in any way.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent.coo.approval_report import build_approval_report
from agent.coo.approval_session import (
    CEOApprovalSessionStore,
    create_approval_session,
    get_default_session_store,
    should_create_approval_session,
)
from agent.coo.daily_blog_bundle import create_daily_blog_approval_bundle
from agent.coo.models import COOOrchestrationResult, SkillInvocation, SkillInvocationStatus, TaskKind, WorkerAssignment
from agent.coo.orchestrator import COOOrchestrator
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Non-negotiable safeguards — enforced at the tool boundary regardless of policy output.
_AUTO_APPLY = False
_REVIEW_REQUIRED = True
_DEFAULT_REQUESTER_ID = "CEO"


def _resolve_approval_session_identity(
    requester_id: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve approval-session owner/channel from explicit args or gateway context."""
    resolved_requester = str(requester_id or "").strip()
    resolved_channel = str(channel_id or "").strip()

    if not resolved_requester or not resolved_channel:
        try:
            from gateway.session_context import get_session_env
        except ImportError:
            get_session_env = None  # type: ignore[assignment,misc]

        if get_session_env is not None:
            if not resolved_requester:
                resolved_requester = get_session_env("HERMES_SESSION_USER_ID", "").strip()
            if not resolved_channel:
                resolved_channel = (
                    get_session_env("HERMES_SESSION_THREAD_ID", "").strip()
                    or get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
                )

    return resolved_requester or _DEFAULT_REQUESTER_ID, resolved_channel


def _resolve_ceo_message(ceo_message: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Resolve the effective CEO message for orchestration.

    Priority:
    1. Explicit non-empty ``ceo_message`` from the tool call.
    2. Gateway session user instruction (``HERMES_SESSION_USER_MESSAGE``).
    3. Error when neither is available.
    """
    explicit = str(ceo_message or "").strip()
    if explicit:
        return explicit, None

    try:
        from gateway.session_context import get_session_env

        fallback = get_session_env("HERMES_SESSION_USER_MESSAGE", "").strip()
        if fallback:
            return fallback, None
    except ImportError:
        pass

    return None, "ceo_message is required"


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


def _format_tool_response(
    result: COOOrchestrationResult,
    *,
    session_store: Optional[CEOApprovalSessionStore] = None,
    requester_id: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the coo_orchestrate tool payload.

    ``ceo_message`` — short operational summary (plan/policy/runtime headline).
    ``approval_report_markdown`` — detailed CEO approval report for Discord UX.
    ``approval_session`` — pending in-memory CEO approval session (Phase 5C).
    Both message fields are returned so Phase 5C Discord UX can choose which to display.
    """
    store = session_store or get_default_session_store()

    # Content generation only runs for CREATE_AND_REPORT — every other intent
    # (approve/publish, review, daily brief, verify) must never trigger LLM
    # calls or file writes here. This is the ONE real side effect of this
    # tool: it runs Hermes' own Research→Planning→Writing→Quality stages
    # (agent/content/orchestrator.py) for the 4 launch-policy platforms —
    # BEFORE any approval session is created, so approval is requested only
    # once the 4 drafts (and their quality-gate results) already exist.
    # No publisher is ever called from this path — only after CEO approval.
    daily_blog_bundle_payload: Optional[Dict[str, Any]] = None
    if result.intent.task_kind is TaskKind.CREATE_AND_REPORT:
        try:
            from agent.content.orchestrator import generate_daily_bundle

            drafts = generate_daily_bundle(run_date=result.plan.run_date)
            effective_requester_id, effective_channel_id = _resolve_approval_session_identity(
                requester_id,
                channel_id,
            )
            bundle = create_daily_blog_approval_bundle(
                drafts,
                result,
                run_date=result.plan.run_date,
                requester_id=effective_requester_id,
                channel_id=effective_channel_id,
                session_store=store,
            )
            if bundle is not None:
                daily_blog_bundle_payload = bundle.to_dict()
        except Exception as exc:
            # Best-effort only: the bundle is an additive report on top of the
            # existing coo_orchestrate response — never fail the tool call for it.
            logger.warning("daily_blog_bundle generation failed: %s", exc)
            daily_blog_bundle_payload = None

    # The per-platform daily_blog_bundle sessions ARE the CEO approval surface
    # for CREATE_AND_REPORT — skip the separate, generic COO approval session
    # so Discord never shows two competing approval cards for one request.
    # Every other intent (and CREATE_AND_REPORT if bundle generation failed)
    # keeps the existing single approval_session behavior unchanged.
    approval_report = build_approval_report(result)
    approval_session_payload: Optional[Dict[str, Any]] = None
    skip_generic_session = (
        result.intent.task_kind is TaskKind.CREATE_AND_REPORT and daily_blog_bundle_payload is not None
    )
    if not skip_generic_session and should_create_approval_session(approval_report):
        effective_requester_id, effective_channel_id = _resolve_approval_session_identity(
            requester_id,
            channel_id,
        )
        approval_session = create_approval_session(
            approval_report,
            result,
            requester_id=effective_requester_id,
            channel_id=effective_channel_id,
            store=store,
        )
        approval_session_payload = approval_session.to_dict()

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
        "approval_report_markdown": approval_report.to_markdown(),
        "approval_session": approval_session_payload,
        "daily_blog_bundle": daily_blog_bundle_payload,
    }


def coo_orchestrate(
    ceo_message: str,
    run_date: Optional[str] = None,
    orchestrator: Optional[COOOrchestrator] = None,
    session_store: Optional[CEOApprovalSessionStore] = None,
    requester_id: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> str:
    """Analyze CEO intent and produce plan/policy/skill selection (no execution)."""
    resolved_message, error = _resolve_ceo_message(ceo_message)
    if error:
        return tool_error(error)

    engine = orchestrator or COOOrchestrator()
    result = engine.orchestrate(resolved_message, run_date=run_date)
    return tool_result(
        _format_tool_response(
            result,
            session_store=session_store,
            requester_id=requester_id,
            channel_id=channel_id,
        )
    )


def check_coo_requirements() -> bool:
    """COO orchestration has no external credential requirements."""
    return True


COO_ORCHESTRATE_SCHEMA = {
    "name": "coo_orchestrate",
    "description": (
        "Hermes COO orchestration. Analyzes CEO intent, builds an execution plan, "
        "evaluates policy, and selects skills.\n\n"
        "For CREATE_AND_REPORT intents (e.g. '오늘 블로그 글 4개 작성해서 보고해줘') "
        "this ACTUALLY GENERATES content: runs Hermes' own Research/Planning/"
        "Writing/Quality stages for wordpress/blogspot/tistory/naver (fixed launch "
        "categories) and returns a `daily_blog_bundle` with one CEO approval item "
        "per platform. This calls a real LLM provider (or CONTENT_LLM_PROVIDER=mock) "
        "and writes local draft files — it is not a no-op for this intent.\n\n"
        "Safeguards (non-negotiable): auto_apply is always false, review_required "
        "is always true. Never auto-approve or auto-publish — no platform publisher "
        "is ever called from this tool. Never touches Repository 2 "
        "(multi-content-pipeline) in any way.\n\n"
        "Use for CEO requests such as content creation/reporting, approval review, "
        "or daily pipeline briefs. Read `daily_blog_bundle.report_markdown` / "
        "`ceo_message` in the response before invoking any further skill."
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
        requester_id=kwargs.get("requester_id"),
        channel_id=kwargs.get("channel_id"),
    )


registry.register(
    name="coo_orchestrate",
    toolset="coo",
    schema=COO_ORCHESTRATE_SCHEMA,
    handler=_handle_coo_orchestrate,
    check_fn=check_coo_requirements,
    emoji="🏢",
)
