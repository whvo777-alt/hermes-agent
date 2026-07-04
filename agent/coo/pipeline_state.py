"""Read-only Execution Engine state for COO policy decisions."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from agent.coo.config import COOConfig, get_coo_config
from agent.coo.models import (
    ApprovalSnapshot,
    PipelineState,
    PublishingSnapshot,
    RuntimeSnapshot,
)

logger = logging.getLogger(__name__)

_APPROVED_STATUSES = frozenset(
    {"approved", "confirmed", "ready_to_publish", "published"}
)
_REJECTED_STATUSES = frozenset({"rejected", "cancelled"})


def resolve_run_date(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    return date.today().isoformat()


class PipelineStateReader:
    """Inspect multi-content-pipeline artifacts without mutating Repository 2."""

    def __init__(self, config: Optional[COOConfig] = None) -> None:
        self._config = config or get_coo_config()

    @property
    def pipeline_root(self) -> Path:
        return self._config.pipeline_root

    def read(self, run_date: Optional[str] = None) -> PipelineState:
        resolved_date = resolve_run_date(run_date)
        root = self.pipeline_root
        reports_dir = root / "outputs" / resolved_date / "_reports"
        warnings: list[str] = []

        if not root.is_dir():
            warnings.append(f"Execution Engine root not found: {root}")

        approvals = self._read_approval_snapshot(reports_dir)
        publishing = self._read_publishing_snapshot(root, reports_dir)
        runtime = self._read_runtime_snapshot(root)

        return PipelineState(
            run_date=resolved_date,
            pipeline_root=str(root),
            run_report_exists=(reports_dir / "run_report.md").is_file(),
            daily_assignments_exists=(reports_dir / "daily_assignments.json").is_file(),
            approvals=approvals,
            publishing=publishing,
            runtime=runtime,
            warnings=warnings,
        )

    def _read_json(self, path: Path) -> Optional[Dict[str, Any]]:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Could not read JSON at %s: %s", path, exc)
            return None

    def _count_statuses(self, items: Iterable[Dict[str, Any]]) -> tuple[int, int, int]:
        pending = approved = rejected = 0
        for item in items:
            status = str(item.get("status", "")).lower()
            if status in _APPROVED_STATUSES:
                approved += 1
            elif status in _REJECTED_STATUSES:
                rejected += 1
            else:
                pending += 1
        return pending, approved, rejected

    def _read_approval_snapshot(self, reports_dir: Path) -> ApprovalSnapshot:
        queue_path = reports_dir / "discord_approval_queue.json"
        payload = self._read_json(queue_path)
        if payload is None:
            return ApprovalSnapshot(queue_path=str(queue_path))

        items = payload.get("items") or []
        pending, approved, rejected = self._count_statuses(items)
        return ApprovalSnapshot(
            total=len(items),
            pending=pending,
            approved=approved,
            rejected=rejected,
            queue_exists=True,
            queue_path=str(queue_path),
        )

    def _read_publishing_snapshot(
        self,
        root: Path,
        reports_dir: Path,
    ) -> PublishingSnapshot:
        plan_exists = (reports_dir / "publishing_plan.json").is_file()
        confirmed_path = root / "data" / "confirmed_publishing_queue.json"
        confirmed_payload = self._read_json(confirmed_path)
        confirmed_count = 0
        if confirmed_payload:
            queues = confirmed_payload.get("queues") or []
            for queue in queues:
                items = queue.get("items") or []
                confirmed_count += len(items)

        dispatch_exists = (root / "data" / "publishing_dispatch_result.json").is_file()
        return PublishingSnapshot(
            plan_exists=plan_exists,
            confirmed_queue_exists=confirmed_payload is not None,
            confirmed_item_count=confirmed_count,
            dispatch_result_exists=dispatch_exists,
        )

    def _read_runtime_snapshot(self, root: Path) -> RuntimeSnapshot:
        status_path = root / "data" / "scheduler_runtime_status.json"
        lock_path = root / "data" / "scheduler_runtime.lock"
        payload = self._read_json(status_path)

        state: Optional[str] = None
        active = False
        if payload:
            state = str(payload.get("state") or "").lower() or None
            active = state not in {None, "", "inactive", "stopped"}
        elif lock_path.is_file():
            active = True
            state = "active_lock_present"

        return RuntimeSnapshot(
            scheduler_active=active,
            scheduler_status_path=str(status_path) if status_path.is_file() else None,
            scheduler_state=state,
        )
