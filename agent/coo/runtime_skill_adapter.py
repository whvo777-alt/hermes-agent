"""Runtime skill adapter — thin facade over PipelineAdapter.dry_run() for execution runs.

Phase 8E: exposes ``dry_run_skill()`` only. No dispatch(), no subprocess, no
Repository 2 mutation. Workers use ExecutionProvider.plan(); execution runtime
uses this module for adapter-backed dry-run previews.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.coo.execution_contract import (
    ExecutionBoundaryPolicy,
    SkillExecutionMode,
    SkillExecutionRequest,
    SkillExecutionStatus,
    default_boundary_policy,
)
from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig
from agent.coo.skills_catalog import get_skill

_DEFAULT_RUNTIME_WORKER_ID = "coo_runtime"
_RESULT_SOURCE = "pipeline_adapter_dry_run"

_DISPATCHABLE_STATUS_MAP: Dict[SkillExecutionStatus, str] = {
    SkillExecutionStatus.PLANNED: "planned",
    SkillExecutionStatus.FAILED: "failed",
    SkillExecutionStatus.BLOCKED: "blocked",
}

_PREVIEW_STATUS_MAP: Dict[str, str] = {
    "planned": "preview_planned",
    "failed": "preview_failed",
    "blocked": "preview_blocked",
}


def _default_adapter() -> PipelineAdapter:
    return PipelineAdapter(
        PipelineAdapterConfig(
            allow_execute=False,
            repository2_read_only=True,
        )
    )


def _embed_status(adapter_status: SkillExecutionStatus) -> str:
    return _DISPATCHABLE_STATUS_MAP.get(adapter_status, adapter_status.value)


def _catalog_entrypoint(skill_id: str) -> str:
    definition = get_skill(skill_id)
    if definition is None:
        return ""
    return definition.entrypoint_hint


def _skill_result_to_dict(
    skill_id: str,
    *,
    adapter_status: SkillExecutionStatus,
    summary: str,
    blocked_reason: str = "",
    entrypoint_hint: str = "",
) -> Dict[str, Any]:
    return {
        "skill_id": skill_id,
        "dry_run": True,
        "status": _embed_status(adapter_status),
        "adapter_status": adapter_status.value,
        "summary": summary,
        "blocked_reason": blocked_reason,
        "entrypoint_hint": entrypoint_hint,
        "source": _RESULT_SOURCE,
    }


def dry_run_skill(
    skill_id: str,
    *,
    run_date: str,
    ticket_id: str,
    adapter: Optional[PipelineAdapter] = None,
    boundary: Optional[ExecutionBoundaryPolicy] = None,
) -> Dict[str, Any]:
    """Run PipelineAdapter.dry_run() for one skill and return an embed-compatible dict."""
    policy = boundary or default_boundary_policy()
    pipeline = adapter or _default_adapter()
    entrypoint_hint = _catalog_entrypoint(skill_id)

    request = SkillExecutionRequest(
        skill_id=skill_id,
        worker_id=_DEFAULT_RUNTIME_WORKER_ID,
        assignment_id=ticket_id,
        run_date=run_date,
        mode=SkillExecutionMode.DRY_RUN,
        worker_mode="dry_run",
        auto_apply=False,
        review_required=True,
    )

    result = pipeline.dry_run(request, policy)
    return _skill_result_to_dict(
        skill_id,
        adapter_status=result.status,
        summary=result.summary,
        blocked_reason=result.blocked_reason,
        entrypoint_hint=entrypoint_hint,
    )


def to_preview_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Map a dispatchable-style dry-run result to preview bucket embed status."""
    base_status = str(result.get("status", ""))
    preview_status = _PREVIEW_STATUS_MAP.get(base_status, f"preview_{base_status}")
    return {**result, "status": preview_status}
