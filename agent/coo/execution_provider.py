"""Execution Provider — worker-facing facade over PipelineAdapter.

Phase 4B: plan generation only. Workers reach Repository 2 metadata exclusively
through this provider; they must not import PipelineAdapter or R2 paths directly.

    Worker → ExecutionProvider.plan() → PipelineAdapter.plan()
                  ↑
         Execution Contract (inside adapter.plan)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.coo.execution_contract import (
    ExecutionBoundaryPolicy,
    SkillExecutionRequest,
)
from agent.coo.pipeline_adapter import (
    PipelineAdapter,
    PipelineAdapterResult,
)

DEFAULT_PROVIDER_NAME = "pipeline"


@dataclass
class ExecutionProviderResult:
    """Worker-safe execution plan — no subprocess, no dispatch."""

    provider_name: str
    adapter_status: str
    pipeline_root: str
    entrypoint: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    dry_run: bool = True
    skill_id: str = ""
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "adapter_status": self.adapter_status,
            "pipeline_root": self.pipeline_root,
            "entrypoint": self.entrypoint,
            "parameters": dict(self.parameters),
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
            "skill_id": self.skill_id,
            "summary": self.summary,
        }


class ExecutionProvider:
    """Single entry point for workers to obtain execution plans.

    Phase 4B: ``plan()`` only — delegates to ``PipelineAdapter.plan()``.
    No ``dispatch()``, no subprocess, no Repository 2 mutation.
    """

    def __init__(
        self,
        adapter: Optional[PipelineAdapter] = None,
        provider_name: str = DEFAULT_PROVIDER_NAME,
    ) -> None:
        self._adapter = adapter or PipelineAdapter()
        self._provider_name = provider_name

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def plan(
        self,
        request: SkillExecutionRequest,
        boundary: Optional[ExecutionBoundaryPolicy] = None,
    ) -> ExecutionProviderResult:
        """Build an execution plan via PipelineAdapter — no dispatch."""
        adapter_result = self._adapter.plan(request, boundary)
        return _adapter_result_to_provider_result(
            adapter_result,
            provider_name=self._provider_name,
        )


def _adapter_result_to_provider_result(
    adapter_result: PipelineAdapterResult,
    *,
    provider_name: str,
) -> ExecutionProviderResult:
    """Map adapter plan output to the worker-facing provider result."""
    return ExecutionProviderResult(
        provider_name=provider_name,
        adapter_status=adapter_result.status.value,
        pipeline_root=adapter_result.pipeline_root,
        entrypoint=adapter_result.entrypoint_hint,
        parameters=dict(adapter_result.parameters),
        warnings=list(adapter_result.warnings),
        dry_run=True,  # Phase 4B: plan-only — never dispatches
        skill_id=adapter_result.skill_id,
        summary=adapter_result.summary,
    )
