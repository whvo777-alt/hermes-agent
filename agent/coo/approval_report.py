"""CEO Approval Report — structured Discord-ready briefing from COO orchestration.

Phase 5B: report generation only. No approval execution, no publish dispatch.

CEO-facing outputs (``coo_orchestrate`` response):

- ``ceo_message`` — short operational summary for quick scanning in chat.
- ``approval_report_markdown`` — detailed CEO approval report (``CEOApprovalReport.to_markdown()``).

Phase 5C Discord UX may surface either field; both are kept so product can choose
without changing the orchestration pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from agent.coo.models import COOOrchestrationResult, TaskKind
from agent.coo.worker_runtime import WorkerRuntimeStatus


class CEOApprovalReportStatus(str, Enum):
    """High-level approval report state for the CEO."""

    NOT_STARTED = "not_started"
    PENDING = "pending"
    READY = "ready"
    BLOCKED = "blocked"


_RUNTIME_TO_REPORT_STATUS: Dict[WorkerRuntimeStatus, CEOApprovalReportStatus] = {
    WorkerRuntimeStatus.FAILED: CEOApprovalReportStatus.BLOCKED,
    WorkerRuntimeStatus.CANCELLED: CEOApprovalReportStatus.BLOCKED,
    WorkerRuntimeStatus.BLOCKED: CEOApprovalReportStatus.BLOCKED,
    WorkerRuntimeStatus.WAITING: CEOApprovalReportStatus.PENDING,
    WorkerRuntimeStatus.WORKING: CEOApprovalReportStatus.PENDING,
    WorkerRuntimeStatus.PLANNED: CEOApprovalReportStatus.PENDING,
    WorkerRuntimeStatus.SELECTED: CEOApprovalReportStatus.READY,
    WorkerRuntimeStatus.COMPLETED: CEOApprovalReportStatus.READY,
}


def runtime_status_to_report_status(
    runtime_status: Optional[WorkerRuntimeStatus],
) -> CEOApprovalReportStatus:
    """Map ``WorkerRuntimeStatus`` to approval report status deterministically."""
    if runtime_status is None:
        return CEOApprovalReportStatus.NOT_STARTED
    return _RUNTIME_TO_REPORT_STATUS[runtime_status]


@dataclass
class CEOApprovalReportSection:
    """Named section in the CEO approval report."""

    title: str
    body_lines: List[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [f"### {self.title}"]
        lines.extend(self.body_lines)
        return "\n".join(lines)


@dataclass
class CEOApprovalReport:
    """Structured CEO approval briefing — no execution side effects."""

    status: CEOApprovalReportStatus
    task_kind: str
    run_date: str
    runtime_status: str
    worker_summary: str
    selected_workers: List[str] = field(default_factory=list)
    waiting_workers: List[str] = field(default_factory=list)
    blocked_workers: List[str] = field(default_factory=list)
    provider_result_count: int = 0
    approval_required: bool = False
    review_required: bool = True
    auto_apply: bool = False
    next_actions: List[str] = field(default_factory=list)
    sections: List[CEOApprovalReportSection] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "task_kind": self.task_kind,
            "run_date": self.run_date,
            "runtime_status": self.runtime_status,
            "worker_summary": self.worker_summary,
            "selected_workers": list(self.selected_workers),
            "waiting_workers": list(self.waiting_workers),
            "blocked_workers": list(self.blocked_workers),
            "provider_result_count": self.provider_result_count,
            "approval_required": self.approval_required,
            "review_required": self.review_required,
            "auto_apply": self.auto_apply,
            "next_actions": list(self.next_actions),
            "sections": [
                {"title": section.title, "body_lines": list(section.body_lines)}
                for section in self.sections
            ],
        }

    def to_markdown(self) -> str:
        """Render a Discord-friendly markdown approval report."""
        lines = [
            "## CEO Approval Report",
            "",
            f"**Task:** `{self.task_kind}`",
            f"**Run date:** `{self.run_date}`",
            f"**Report status:** `{self.status.value}`",
            "",
            "### Safeguards",
            f"- approval_required: `{self.approval_required}`",
            f"- review_required: `{self.review_required}`",
            f"- auto_apply: `{self.auto_apply}` *(no auto-approve / auto-publish)*",
            "",
        ]
        for section in self.sections:
            lines.append(section.to_markdown())
            lines.append("")
        if self.next_actions:
            lines.append("### Next Actions")
            for action in self.next_actions:
                lines.append(f"- {action}")
            lines.append("")
        lines.append(
            "*Dry-run report only — no approval or publish execution (Phase 5B).*"
        )
        return "\n".join(lines).strip()


def build_approval_report(orchestration_result: COOOrchestrationResult) -> CEOApprovalReport:
    """Build a CEO approval report from a COO orchestration result."""
    runtime = orchestration_result.worker_runtime_result
    task_kind = orchestration_result.intent.task_kind.value
    run_date = orchestration_result.plan.run_date

    if runtime is None:
        runtime_status = "not_started"
        worker_summary = "Worker runtime not started (no worker assignments)."
        selected_workers: List[str] = []
        waiting_workers: List[str] = []
        blocked_workers: List[str] = []
        provider_result_count = 0
        report_status = runtime_status_to_report_status(None)
    else:
        runtime_status = runtime.status.value
        worker_summary = runtime.summary
        selected_workers = list(runtime.selected_workers)
        waiting_workers = list(runtime.waiting_workers)
        blocked_workers = list(runtime.blocked_workers)
        provider_result_count = len(runtime.provider_results)
        report_status = runtime_status_to_report_status(runtime.status)

    worker_runtime_section = CEOApprovalReportSection(
        title="Worker Runtime",
        body_lines=[
            f"- Runtime status: `{runtime_status}`",
            f"- Summary: {worker_summary}",
            f"- Selected workers: {_format_worker_list(selected_workers)}",
            f"- Waiting workers: {_format_worker_list(waiting_workers)}",
            f"- Blocked workers: {_format_worker_list(blocked_workers)}",
            f"- Provider results: `{provider_result_count}`",
        ],
    )

    plan_section = CEOApprovalReportSection(
        title="COO Plan",
        body_lines=[
            f"- Plan summary: {orchestration_result.plan.summary}",
            f"- Policy: `{orchestration_result.policy.verdict.value}` — "
            f"{orchestration_result.policy.summary}",
        ],
    )

    if orchestration_result.intent.task_kind is TaskKind.UNKNOWN:
        report_status = runtime_status_to_report_status(
            runtime.status if runtime is not None else None
        )
        plan_section.body_lines.insert(
            0,
            "- Intent not recognized — clarification required before approval.",
        )

    return CEOApprovalReport(
        status=report_status,
        task_kind=task_kind,
        run_date=run_date,
        runtime_status=runtime_status,
        worker_summary=worker_summary,
        selected_workers=selected_workers,
        waiting_workers=waiting_workers,
        blocked_workers=blocked_workers,
        provider_result_count=provider_result_count,
        approval_required=orchestration_result.policy.requires_ceo_approval,
        review_required=True,
        auto_apply=False,
        next_actions=list(orchestration_result.next_actions),
        sections=[plan_section, worker_runtime_section],
    )


def _format_worker_list(workers: List[str]) -> str:
    if not workers:
        return "(none)"
    return ", ".join(f"`{worker_id}`" for worker_id in workers)
