"""Pipeline Adapter — sole Hermes layer that knows Repository 2 paths and entrypoints.

Phase 4A: DRY_RUN only. No subprocess, no node/npm invocation, no Repository 2 mutation.
Every adapter path evaluates the execution contract before doing adapter work.

Entrypoint source of truth: ``skills_catalog.SkillDefinition.entrypoint_hint`` only.
Workers must never pass Repository 2 shell commands via ``SkillExecutionRequest``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.coo.execution_contract import (
    ExecutionBoundaryPolicy,
    SkillExecutionRequest,
    SkillExecutionResult,
    SkillExecutionStatus,
    evaluate_skill_execution,
)
from agent.coo.skills_catalog import get_skill

logger = logging.getLogger(__name__)


class PipelineAdapterStatus(str, Enum):
    """Adapter-local lifecycle status (maps to SkillExecutionResult on export)."""

    PLANNED = "planned"
    BLOCKED = "blocked"
    WAITING = "waiting"
    DRY_RUN_READY = "dry_run_ready"
    FAILED = "failed"


@dataclass(frozen=True)
class PipelineAdapterConfig:
    """Configuration for Repository 2 pipeline access."""

    pipeline_root: str = "/opt/data/multi-content-pipeline"
    allow_execute: bool = False
    timeout_seconds: int = 60
    repository2_read_only: bool = True


@dataclass
class PipelineAdapterResult:
    """Execution plan produced by the adapter (metadata only — no subprocess)."""

    status: PipelineAdapterStatus
    skill_id: str
    entrypoint_hint: str
    pipeline_root: str
    summary: str
    run_date: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    blocked_reason: str = ""
    root_valid: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "skill_id": self.skill_id,
            "entrypoint_hint": self.entrypoint_hint,
            "pipeline_root": self.pipeline_root,
            "summary": self.summary,
            "run_date": self.run_date,
            "parameters": dict(self.parameters),
            "blocked_reason": self.blocked_reason,
            "root_valid": self.root_valid,
            "warnings": list(self.warnings),
        }


class PipelineAdapter:
    """Translates SkillExecutionRequest into contract-checked adapter outcomes.

    Workers must not reference Repository 2 paths or shell entrypoints directly —
    only this adapter resolves catalog entrypoint hints against ``pipeline_root``.
    """

    def __init__(self, config: Optional[PipelineAdapterConfig] = None) -> None:
        self.config = config or PipelineAdapterConfig()

    def _assert_repository_read_only(self) -> None:
        """Fail closed when the Repository 2 read-only guard is disabled.

        Phase 4A: invoked by plan(), dry_run(), and dispatch().
        TODO(Phase 4B+): every future write/mutation path (file patch, prompt apply,
        strategy apply, publish dispatch) MUST call this guard before touching R2.
        """
        if not self.config.repository2_read_only:
            raise RuntimeError(
                "repository2_read_only must remain true; Repository 2 mutation is forbidden."
            )

    def validate_root(self) -> Tuple[bool, str]:
        """Return (ok, error_message). Checks pipeline_root directory existence only."""
        root = Path(self.config.pipeline_root)
        if root.is_dir():
            return True, ""
        return False, f"Pipeline root not found: {self.config.pipeline_root}"

    def _catalog_entrypoint(self, skill_id: str) -> str:
        """Return entrypoint from skills_catalog only — never from the request."""
        definition = get_skill(skill_id)
        if definition is not None:
            return definition.entrypoint_hint
        return ""

    def _entrypoint_override_warning(
        self,
        request: SkillExecutionRequest,
        catalog_entrypoint: str,
    ) -> Optional[str]:
        """Log and return a warning when request tries to override the catalog entrypoint."""
        if not request.entrypoint_hint:
            return None
        if request.entrypoint_hint == catalog_entrypoint:
            return None
        message = (
            f"request.entrypoint_hint {request.entrypoint_hint!r} ignored for "
            f"{request.skill_id}; using catalog entrypoint {catalog_entrypoint!r}."
        )
        logger.warning(message)
        return message

    def _warnings_from_override(self, override_warning: Optional[str]) -> List[str]:
        if override_warning:
            return [override_warning]
        return []

    def plan(
        self,
        request: SkillExecutionRequest,
        boundary: Optional[ExecutionBoundaryPolicy] = None,
    ) -> PipelineAdapterResult:
        """Build an execution plan from a skill request — no dispatch."""
        contract = evaluate_skill_execution(request, boundary)
        entrypoint = self._catalog_entrypoint(request.skill_id)
        override_warning = self._entrypoint_override_warning(request, entrypoint)
        warnings = self._warnings_from_override(override_warning)

        if contract.status is SkillExecutionStatus.BLOCKED:
            return PipelineAdapterResult(
                status=PipelineAdapterStatus.BLOCKED,
                skill_id=request.skill_id,
                entrypoint_hint=entrypoint,
                pipeline_root=self.config.pipeline_root,
                summary=f"Blocked: {request.skill_id}",
                run_date=request.run_date,
                parameters=dict(request.parameters),
                blocked_reason=contract.blocked_reason,
                root_valid=False,
                warnings=warnings,
            )

        self._assert_repository_read_only()

        root_ok, root_error = self.validate_root()
        if not root_ok:
            return PipelineAdapterResult(
                status=PipelineAdapterStatus.FAILED,
                skill_id=request.skill_id,
                entrypoint_hint=entrypoint,
                pipeline_root=self.config.pipeline_root,
                summary=f"Plan failed: {request.skill_id}",
                run_date=request.run_date,
                parameters=dict(request.parameters),
                blocked_reason=root_error,
                root_valid=False,
                warnings=warnings,
            )

        summary = (
            f"Planned {request.skill_id} via {entrypoint!r} "
            f"under {self.config.pipeline_root} (no dispatch)."
        )
        if override_warning:
            summary = f"{summary} {override_warning}"

        return PipelineAdapterResult(
            status=PipelineAdapterStatus.PLANNED,
            skill_id=request.skill_id,
            entrypoint_hint=entrypoint,
            pipeline_root=self.config.pipeline_root,
            summary=summary,
            run_date=request.run_date,
            parameters=dict(request.parameters),
            root_valid=True,
            warnings=warnings,
        )

    def dry_run(
        self,
        request: SkillExecutionRequest,
        boundary: Optional[ExecutionBoundaryPolicy] = None,
    ) -> SkillExecutionResult:
        """Evaluate contract + adapter readiness — returns SkillExecutionResult, no subprocess."""
        contract = evaluate_skill_execution(request, boundary)
        if contract.status is SkillExecutionStatus.BLOCKED:
            return contract

        self._assert_repository_read_only()

        root_ok, root_error = self.validate_root()
        if not root_ok:
            return SkillExecutionResult(
                skill_id=request.skill_id,
                status=SkillExecutionStatus.FAILED,
                mode=request.mode,
                dry_run=True,
                summary=f"Dry-run failed: {request.skill_id}",
                blocked_reason=root_error,
                auto_apply=False,
                review_required=True,
            )

        entrypoint = self._catalog_entrypoint(request.skill_id)
        override_warning = self._entrypoint_override_warning(request, entrypoint)
        summary = (
            f"Dry-run ready for {request.skill_id}: "
            f"entrypoint={entrypoint!r}, root={self.config.pipeline_root} "
            "(Phase 4A — no subprocess)."
        )
        if override_warning:
            summary = f"{summary} {override_warning}"

        return SkillExecutionResult(
            skill_id=request.skill_id,
            status=SkillExecutionStatus.PLANNED,
            mode=request.mode,
            dry_run=True,
            summary=summary,
            auto_apply=False,
            review_required=True,
        )

    def dispatch(
        self,
        request: SkillExecutionRequest,
        boundary: Optional[ExecutionBoundaryPolicy] = None,
    ) -> SkillExecutionResult:
        """Phase 4A: always forbidden — raises RuntimeError after contract evaluation."""
        contract = evaluate_skill_execution(request, boundary)
        if contract.status is SkillExecutionStatus.BLOCKED:
            return contract

        self._assert_repository_read_only()

        # TODO(Phase 4B+): subprocess dispatch MUST call _assert_repository_read_only()
        # before any write-capable adapter operation.
        if not self.config.allow_execute:
            raise RuntimeError(
                "Execute dispatch is not available (allow_execute=false; Phase 4A is DRY_RUN only)."
            )
        raise RuntimeError("Actual pipeline dispatch is not available in Phase 4A.")
