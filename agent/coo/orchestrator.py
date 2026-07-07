"""COO Orchestrator — Intent → Plan → Policy → Skill Selection pipeline."""

from __future__ import annotations

from typing import List, Optional

from agent.coo.config import COOConfig, get_coo_config
from agent.coo.execution_planner import ExecutionPlanner
from agent.coo.execution_policy import ExecutionPolicy
from agent.coo.intent_analysis import IntentAnalyzer
from agent.coo.models import COOOrchestrationResult, PolicyVerdict, SkillInvocationStatus, TaskKind
from agent.coo.pipeline_state import PipelineStateReader
from agent.coo.skill_selection import SkillSelector
from agent.coo.worker_manager import WorkerManager
from agent.coo.worker_runtime import WorkerRuntime, WorkerRuntimeResult


class COOOrchestrator:
    """Hermes COO entry point — coordinates the four COO subsystems."""

    def __init__(
        self,
        config: Optional[COOConfig] = None,
        *,
        intent_analyzer: Optional[IntentAnalyzer] = None,
        execution_planner: Optional[ExecutionPlanner] = None,
        execution_policy: Optional[ExecutionPolicy] = None,
        skill_selector: Optional[SkillSelector] = None,
        pipeline_state_reader: Optional[PipelineStateReader] = None,
        worker_manager: Optional[WorkerManager] = None,
        worker_runtime: Optional[WorkerRuntime] = None,
    ) -> None:
        self._config = config or get_coo_config()
        self._intent = intent_analyzer or IntentAnalyzer()
        self._planner = execution_planner or ExecutionPlanner()
        self._policy = execution_policy or ExecutionPolicy(self._config)
        self._selector = skill_selector or SkillSelector()
        self._state_reader = pipeline_state_reader or PipelineStateReader(self._config)
        self._worker_manager = worker_manager or WorkerManager()
        self._worker_runtime = worker_runtime or WorkerRuntime(self._worker_manager)

    def orchestrate(
        self,
        ceo_message: str,
        run_date: Optional[str] = None,
    ) -> COOOrchestrationResult:
        intent = self._intent.analyze(ceo_message, run_date=run_date)
        state = self._state_reader.read(intent.run_date)
        plan = self._planner.plan(intent)
        policy = self._policy.evaluate(plan, state)
        skills = self._selector.select(plan, policy)
        worker_assignments = self._worker_manager.assign(plan, policy, skills)
        worker_runtime_result = None
        if worker_assignments:
            worker_runtime_result = self._worker_runtime.run(
                worker_assignments,
                plan,
                policy,
                state.pipeline_root,
            )
        ceo_message_out = self._build_ceo_message(
            intent,
            plan,
            policy,
            skills,
            state,
            worker_runtime_result,
        )
        next_actions = self._build_next_actions(intent, policy, skills, state)

        return COOOrchestrationResult(
            intent=intent,
            state=state,
            plan=plan,
            policy=policy,
            skills=skills,
            worker_assignments=worker_assignments,
            worker_runtime_result=worker_runtime_result,
            ceo_message=ceo_message_out,
            next_actions=next_actions,
        )

    def _build_ceo_message(
        self,
        intent,
        plan,
        policy,
        skills,
        state,
        worker_runtime_result: Optional[WorkerRuntimeResult] = None,
    ) -> str:
        if intent.task_kind is TaskKind.UNKNOWN:
            lines = [
                "요청을 업무(Task)로 변환하지 못했습니다. "
                "예: '오늘 블로그 글 작성해서 보고해', '승인하고 발행해', '오늘 상태 보고해'",
            ]
            lines.extend(self._worker_runtime_section(worker_runtime_result))
            return "\n".join(lines)

        selected = [s for s in skills if s.status is SkillInvocationStatus.SELECTED]
        deferred = [s for s in skills if s.status is SkillInvocationStatus.DEFERRED]
        blocked = [s for s in skills if s.status is SkillInvocationStatus.BLOCKED]

        lines = [
            f"**COO Plan** — {plan.summary}",
            f"- Task: `{intent.task_kind.value}` (confidence {intent.confidence:.0%})",
            f"- Run date: `{plan.run_date}`",
            f"- Policy: `{policy.verdict.value}` — {policy.summary}",
            f"- Safeguards: autoApply={policy.auto_apply}, reviewRequired={policy.review_required}",
        ]

        if selected:
            lines.append("- Selected skills:")
            for skill in selected:
                lines.append(
                    f"  - `{skill.skill_id}` ({skill.phase.value}) → {skill.entrypoint_hint}"
                )
        if deferred:
            lines.append("- Deferred:")
            for skill in deferred:
                lines.append(f"  - `{skill.skill_id}` — {skill.reason}")
        if blocked:
            lines.append("- Blocked:")
            for skill in blocked:
                lines.append(f"  - `{skill.skill_id}` — {skill.reason}")

        if state.approvals.queue_exists:
            lines.append(
                "- Approval queue: "
                f"{state.approvals.pending} pending / {state.approvals.approved} approved"
            )

        if policy.requires_ceo_approval:
            lines.append("- **CEO approval boundary active** — no auto-approve or auto-publish.")

        lines.extend(self._worker_runtime_section(worker_runtime_result))
        return "\n".join(lines)

    @staticmethod
    def _worker_runtime_section(
        worker_runtime_result: Optional[WorkerRuntimeResult],
    ) -> List[str]:
        """Format Worker Runtime dry-run results for the CEO message."""
        if worker_runtime_result is None:
            return ["- **Worker Runtime**: not started (no worker assignments)."]

        runtime = worker_runtime_result
        lines = [
            "- **Worker Runtime**:",
            f"  - Status: `{runtime.status.value}`",
            f"  - Summary: {runtime.summary}",
        ]
        if runtime.selected_workers:
            lines.append(f"  - Selected workers: {', '.join(runtime.selected_workers)}")
        else:
            lines.append("  - Selected workers: (none)")
        if runtime.waiting_workers:
            lines.append(f"  - Waiting workers: {', '.join(runtime.waiting_workers)}")
        else:
            lines.append("  - Waiting workers: (none)")
        if runtime.blocked_workers:
            lines.append(f"  - Blocked workers: {', '.join(runtime.blocked_workers)}")
        else:
            lines.append("  - Blocked workers: (none)")
        lines.append(f"  - Provider results: {len(runtime.provider_results)}")
        return lines

    def _build_next_actions(
        self,
        intent,
        policy,
        skills,
        state,
    ) -> List[str]:
        actions: List[str] = []

        if intent.task_kind is TaskKind.UNKNOWN:
            return [
                "Clarify CEO intent with examples (create/report, approve/publish, daily brief).",
            ]

        selected = [s.skill_id for s in skills if s.status is SkillInvocationStatus.SELECTED]
        if selected:
            actions.append(
                "Future dispatcher may invoke selected Execution Engine skills "
                "(plan-only until CEO Approval): "
                + ", ".join(selected)
            )
        else:
            actions.append(
                "Do not invoke Execution Engine via terminal until policy "
                "blockers are resolved and CEO approves."
            )

        if policy.requires_ceo_approval:
            actions.append("Present approval report to CEO before any publish step.")

        if intent.task_kind is TaskKind.CREATE_AND_REPORT:
            actions.append(
                "After CEO Approval, a future dispatcher may run create_content; "
                "until then deliver the COO plan report and wait at publish_wait."
            )
        elif intent.task_kind is TaskKind.APPROVE_AND_PUBLISH:
            if state.approvals.approved == 0:
                actions.append("CEO must approve queue items before publish_content can run.")
            elif state.runtime.scheduler_active:
                actions.append(
                    "Defer dispatch to Scheduler Runtime or obtain explicit CEO manual override."
                )

        if policy.verdict is PolicyVerdict.BLOCK:
            actions.append("Resolve blocked policy rules, then re-run COO orchestration.")

        return actions
