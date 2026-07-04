"""Worker Runtime Sequencer — ordered dry-run across staffed worker assignments.

Phase 4D: sequence ``WorkerManager.dry_run()`` results only. No subprocess, no
Repository 2 mutation, no actual skill execution.

Runtime dispatch always uses ``SkillInvocation`` from skill selection — never
``WorkerDefinition.skill_ids``. Registry drift is informational only; see
``validate_worker_registry()`` (warnings, never exceptions).

    COO → WorkerManager.assign() → WorkerRuntime.run()
              → Worker.perform() → ExecutionProvider.plan() (per worker)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from agent.coo.execution_provider import ExecutionProviderResult
from agent.coo.models import (
    ExecutionPlan,
    PolicyDecision,
    WorkerAssignment,
    WorkerStatus,
    worker_status_rank,
)
from agent.coo.worker_interface import WorkerExecutionMode, WorkerResult
from agent.coo.worker_manager import WorkerManager


class WorkerRuntimeStatus(str, Enum):
    """Aggregated lifecycle status for a sequenced worker runtime pass."""

    PLANNED = "planned"
    SELECTED = "selected"
    WORKING = "working"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_WORKER_TO_RUNTIME: Dict[WorkerStatus, WorkerRuntimeStatus] = {
    WorkerStatus.PLANNED: WorkerRuntimeStatus.PLANNED,
    WorkerStatus.SELECTED: WorkerRuntimeStatus.SELECTED,
    WorkerStatus.WORKING: WorkerRuntimeStatus.WORKING,
    WorkerStatus.WAITING: WorkerRuntimeStatus.WAITING,
    WorkerStatus.BLOCKED: WorkerRuntimeStatus.BLOCKED,
    WorkerStatus.COMPLETED: WorkerRuntimeStatus.COMPLETED,
    WorkerStatus.FAILED: WorkerRuntimeStatus.FAILED,
    WorkerStatus.CANCELLED: WorkerRuntimeStatus.CANCELLED,
}


@dataclass
class WorkerRuntimeResult:
    """Outcome of a sequenced worker runtime pass — CEO report input."""

    status: WorkerRuntimeStatus
    run_date: str
    worker_results: List[WorkerResult] = field(default_factory=list)
    status_counts: Dict[str, int] = field(default_factory=dict)
    blocked_workers: List[str] = field(default_factory=list)
    waiting_workers: List[str] = field(default_factory=list)
    selected_workers: List[str] = field(default_factory=list)
    provider_results: List[ExecutionProviderResult] = field(default_factory=list)
    summary: str = ""
    dry_run: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "run_date": self.run_date,
            "worker_results": [result.to_dict() for result in self.worker_results],
            "status_counts": dict(self.status_counts),
            "blocked_workers": list(self.blocked_workers),
            "waiting_workers": list(self.waiting_workers),
            "selected_workers": list(self.selected_workers),
            "provider_results": [result.to_dict() for result in self.provider_results],
            "summary": self.summary,
            "dry_run": self.dry_run,
        }


class WorkerRuntime:
    """Sequence worker assignments through WorkerManager dry-run — no execution."""

    def __init__(self, worker_manager: Optional[WorkerManager] = None) -> None:
        self._manager = worker_manager or WorkerManager()

    def run(
        self,
        assignments: List[WorkerAssignment],
        plan: ExecutionPlan,
        policy: PolicyDecision,
        pipeline_root: str,
        mode: WorkerExecutionMode = WorkerExecutionMode.DRY_RUN,
    ) -> WorkerRuntimeResult:
        """Process assignments in order and aggregate worker/provider outcomes."""
        worker_results = self._manager.dry_run(
            assignments,
            plan,
            policy,
            pipeline_root,
            mode=mode,
        )
        return _aggregate_runtime_result(worker_results, plan.run_date)


def _aggregate_runtime_result(
    worker_results: List[WorkerResult],
    run_date: str,
) -> WorkerRuntimeResult:
    status_counts: Dict[str, int] = {}
    blocked_workers: List[str] = []
    waiting_workers: List[str] = []
    selected_workers: List[str] = []
    provider_results: List[ExecutionProviderResult] = []

    for result in worker_results:
        key = result.status.value
        status_counts[key] = status_counts.get(key, 0) + 1
        if result.status is WorkerStatus.BLOCKED:
            blocked_workers.append(result.worker_id)
        elif result.status is WorkerStatus.WAITING:
            waiting_workers.append(result.worker_id)
        elif result.status is WorkerStatus.SELECTED:
            selected_workers.append(result.worker_id)
        provider_results.extend(result.provider_results)

    overall = WorkerRuntimeStatus.PLANNED
    if worker_results:
        worst_worker_status = max(
            worker_results,
            key=lambda result: worker_status_rank(result.status),
        ).status
        overall = _WORKER_TO_RUNTIME[worst_worker_status]

    summary = _build_ceo_summary(
        run_date=run_date,
        worker_count=len(worker_results),
        status_counts=status_counts,
        blocked_workers=blocked_workers,
        waiting_workers=waiting_workers,
        selected_workers=selected_workers,
        provider_count=len(provider_results),
    )

    return WorkerRuntimeResult(
        status=overall,
        run_date=run_date,
        worker_results=worker_results,
        status_counts=status_counts,
        blocked_workers=blocked_workers,
        waiting_workers=waiting_workers,
        selected_workers=selected_workers,
        provider_results=provider_results,
        summary=summary,
        dry_run=True,
    )


def _build_ceo_summary(
    *,
    run_date: str,
    worker_count: int,
    status_counts: Dict[str, int],
    blocked_workers: List[str],
    waiting_workers: List[str],
    selected_workers: List[str],
    provider_count: int,
) -> str:
    parts = [
        f"Worker runtime dry-run for {run_date}: {worker_count} worker(s) sequenced.",
        f"Selected={status_counts.get('selected', 0)}, "
        f"Waiting={status_counts.get('waiting', 0)}, "
        f"Blocked={status_counts.get('blocked', 0)}.",
    ]
    if selected_workers:
        parts.append(f"Active: {', '.join(selected_workers)}.")
    if waiting_workers:
        parts.append(f"Waiting: {', '.join(waiting_workers)}.")
    if blocked_workers:
        parts.append(f"Blocked: {', '.join(blocked_workers)}.")
    if provider_count:
        parts.append(f"ExecutionProvider planned {provider_count} skill(s).")
    parts.append("No engine dispatch (Phase 4D).")
    return " ".join(parts)
