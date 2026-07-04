"""Execution Policy — state-aware execution gate (not a command allowlist)."""

from __future__ import annotations

from typing import Dict, List, Set

from agent.coo.config import COOConfig, get_coo_config
from agent.coo.models import (
    ExecutionPlan,
    PipelineState,
    PlanPhase,
    PolicyDecision,
    PolicyRuleResult,
    PolicyVerdict,
    TaskKind,
)


class ExecutionPolicy:
    """Evaluate whether a plan may proceed given current pipeline state."""

    def __init__(self, config: COOConfig | None = None) -> None:
        self._config = config or get_coo_config()

    def evaluate(self, plan: ExecutionPlan, state: PipelineState) -> PolicyDecision:
        rules: List[PolicyRuleResult] = []
        allowed: Set[PlanPhase] = set()
        blocked: Set[PlanPhase] = set()
        deferred: Set[PlanPhase] = set()

        rules.append(self._rule_safeguards())
        rules.extend(self._rules_for_task(plan, state, allowed, blocked, deferred))

        requires_ceo = any(
            phase in {PlanPhase.APPROVAL_QUEUE, PlanPhase.PUBLISHER, PlanPhase.PUBLISH_WAIT}
            for phase in (step.phase for step in plan.steps)
        )

        has_block_rule = any(rule.verdict is PolicyVerdict.BLOCK for rule in rules)

        if has_block_rule:
            verdict = PolicyVerdict.BLOCK
            summary = "Execution blocked by policy safeguards or pipeline state."
        elif plan.task_kind is TaskKind.UNKNOWN or not plan.steps:
            verdict = PolicyVerdict.REQUIRE_CEO
            summary = "Clarify CEO intent before invoking Execution Engine skills."
        elif blocked:
            if allowed:
                verdict = PolicyVerdict.DEFER
                summary = "Partial execution allowed; blocked phases require CEO action."
            else:
                verdict = PolicyVerdict.BLOCK
                summary = "Execution blocked by current pipeline state and policy."
        elif deferred and not allowed:
            verdict = PolicyVerdict.DEFER
            summary = "Execution deferred until prerequisites or CEO approval are satisfied."
        elif requires_ceo or plan.task_kind in {
            TaskKind.CREATE_AND_REPORT,
            TaskKind.APPROVE_AND_PUBLISH,
        }:
            verdict = PolicyVerdict.REQUIRE_CEO
            summary = "Plan may proceed with CEO review boundaries enforced."
        else:
            verdict = PolicyVerdict.ALLOW
            summary = "Plan may proceed under read-only or verification mode."

        return PolicyDecision(
            verdict=verdict,
            auto_apply=self._config.policy.auto_apply,
            review_required=self._config.policy.review_required,
            requires_ceo_approval=requires_ceo or verdict is PolicyVerdict.REQUIRE_CEO,
            allowed_phases=sorted(allowed, key=lambda p: p.value),
            blocked_phases=sorted(blocked, key=lambda p: p.value),
            deferred_phases=sorted(deferred, key=lambda p: p.value),
            rules=rules,
            summary=summary,
        )

    def _rule_safeguards(self) -> PolicyRuleResult:
        policy = self._config.policy
        if policy.auto_apply or not policy.review_required:
            return PolicyRuleResult(
                rule="safeguard_defaults",
                verdict=PolicyVerdict.BLOCK,
                message="COO policy must keep autoApply=false and reviewRequired=true.",
            )
        return PolicyRuleResult(
            rule="safeguard_defaults",
            verdict=PolicyVerdict.ALLOW,
            message="Safeguards enforced: autoApply=false, reviewRequired=true.",
        )

    def _rules_for_task(
        self,
        plan: ExecutionPlan,
        state: PipelineState,
        allowed: Set[PlanPhase],
        blocked: Set[PlanPhase],
        deferred: Set[PlanPhase],
    ) -> List[PolicyRuleResult]:
        handlers: Dict[TaskKind, callable] = {
            TaskKind.CREATE_AND_REPORT: self._evaluate_create_and_report,
            TaskKind.APPROVE_AND_PUBLISH: self._evaluate_approve_and_publish,
            TaskKind.REVIEW_APPROVALS: self._evaluate_review_approvals,
            TaskKind.DAILY_BRIEF: self._evaluate_read_only,
            TaskKind.VERIFY_RUNTIME: self._evaluate_verify,
            TaskKind.UNKNOWN: self._evaluate_unknown,
        }
        handler = handlers[plan.task_kind]
        return handler(plan, state, allowed, blocked, deferred)

    def _evaluate_create_and_report(
        self,
        plan: ExecutionPlan,
        state: PipelineState,
        allowed: Set[PlanPhase],
        blocked: Set[PlanPhase],
        deferred: Set[PlanPhase],
    ) -> List[PolicyRuleResult]:
        rules: List[PolicyRuleResult] = []
        engine_phases = {
            PlanPhase.RESEARCH,
            PlanPhase.STRATEGY,
            PlanPhase.WRITER,
            PlanPhase.QUALITY,
            PlanPhase.APPROVAL_QUEUE,
            PlanPhase.CEO_REPORT,
        }
        allowed.update(engine_phases)
        deferred.add(PlanPhase.PUBLISH_WAIT)
        rules.append(
            PolicyRuleResult(
                rule="create_content_boundary",
                verdict=PolicyVerdict.REQUIRE_CEO,
                message=(
                    "Content creation may run, but publishing waits for CEO approval "
                    "after approval queue generation."
                ),
            )
        )
        if state.approvals.queue_exists and state.approvals.pending > 0:
            rules.append(
                PolicyRuleResult(
                    rule="existing_pending_queue",
                    verdict=PolicyVerdict.DEFER,
                    message=(
                        f"Approval queue already has {state.approvals.pending} pending item(s) "
                        f"for {state.run_date}; new run may duplicate CEO review work."
                    ),
                )
            )
        return rules

    def _evaluate_approve_and_publish(
        self,
        plan: ExecutionPlan,
        state: PipelineState,
        allowed: Set[PlanPhase],
        blocked: Set[PlanPhase],
        deferred: Set[PlanPhase],
    ) -> List[PolicyRuleResult]:
        rules: List[PolicyRuleResult] = []

        if not state.approvals.queue_exists:
            blocked.update({PlanPhase.APPROVAL_CHECK, PlanPhase.PUBLISHER})
            rules.append(
                PolicyRuleResult(
                    rule="approval_queue_missing",
                    verdict=PolicyVerdict.BLOCK,
                    message="No approval queue found — run create_content first.",
                )
            )
            return rules

        if state.approvals.approved == 0:
            blocked.add(PlanPhase.PUBLISHER)
            deferred.add(PlanPhase.APPROVAL_CHECK)
            rules.append(
                PolicyRuleResult(
                    rule="no_approved_items",
                    verdict=PolicyVerdict.BLOCK,
                    message=(
                        f"Approval queue has {state.approvals.pending} pending item(s) "
                        "and no approved items — CEO approval required before publish."
                    ),
                )
            )
            return rules

        allowed.add(PlanPhase.APPROVAL_CHECK)
        rules.append(
            PolicyRuleResult(
                rule="approval_complete",
                verdict=PolicyVerdict.ALLOW,
                message=f"{state.approvals.approved} approved item(s) ready for publisher review.",
            )
        )

        if not state.publishing.confirmed_queue_exists:
            deferred.add(PlanPhase.PUBLISHER)
            rules.append(
                PolicyRuleResult(
                    rule="confirmed_queue_missing",
                    verdict=PolicyVerdict.DEFER,
                    message=(
                        "Approved content exists, but confirmed publishing queue is missing. "
                        "CEO must confirm publishing plan before dispatch."
                    ),
                )
            )
        elif state.runtime.scheduler_active:
            deferred.add(PlanPhase.PUBLISHER)
            rules.append(
                PolicyRuleResult(
                    rule="scheduler_runtime_active",
                    verdict=PolicyVerdict.DEFER,
                    message=(
                        "Scheduler Runtime owns dispatch; publish_content must defer dispatch-due "
                        "unless CEO provides explicit manual override."
                    ),
                )
            )
        else:
            allowed.add(PlanPhase.PUBLISHER)
            rules.append(
                PolicyRuleResult(
                    rule="publisher_ready",
                    verdict=PolicyVerdict.REQUIRE_CEO,
                    message="Publisher skill may run with CEO oversight; live publish remains off by default.",
                )
            )

        return rules

    def _evaluate_review_approvals(
        self,
        plan: ExecutionPlan,
        state: PipelineState,
        allowed: Set[PlanPhase],
        blocked: Set[PlanPhase],
        deferred: Set[PlanPhase],
    ) -> List[PolicyRuleResult]:
        allowed.update({PlanPhase.APPROVAL_CHECK, PlanPhase.CEO_REPORT})
        if not state.approvals.queue_exists:
            return [
                PolicyRuleResult(
                    rule="approval_queue_missing",
                    verdict=PolicyVerdict.DEFER,
                    message="No approval queue yet — create_content has not produced review items.",
                )
            ]
        return [
            PolicyRuleResult(
                rule="approval_review_ready",
                verdict=PolicyVerdict.ALLOW,
                message=(
                    f"Approval queue ready: {state.approvals.pending} pending, "
                    f"{state.approvals.approved} approved."
                ),
            )
        ]

    def _evaluate_read_only(
        self,
        plan: ExecutionPlan,
        state: PipelineState,
        allowed: Set[PlanPhase],
        blocked: Set[PlanPhase],
        deferred: Set[PlanPhase],
    ) -> List[PolicyRuleResult]:
        allowed.update({step.phase for step in plan.steps})
        return [
            PolicyRuleResult(
                rule="read_only_brief",
                verdict=PolicyVerdict.ALLOW,
                message="Read-only briefing does not mutate Execution Engine state.",
            )
        ]

    def _evaluate_verify(
        self,
        plan: ExecutionPlan,
        state: PipelineState,
        allowed: Set[PlanPhase],
        blocked: Set[PlanPhase],
        deferred: Set[PlanPhase],
    ) -> List[PolicyRuleResult]:
        allowed.update({PlanPhase.VERIFY})
        return [
            PolicyRuleResult(
                rule="verify_runtime",
                verdict=PolicyVerdict.REQUIRE_CEO,
                message="Verification may execute pipeline side effects — CEO should expect review.",
            )
        ]

    def _evaluate_unknown(
        self,
        plan: ExecutionPlan,
        state: PipelineState,
        allowed: Set[PlanPhase],
        blocked: Set[PlanPhase],
        deferred: Set[PlanPhase],
    ) -> List[PolicyRuleResult]:
        blocked.update({step.phase for step in plan.steps})
        return [
            PolicyRuleResult(
                rule="unknown_intent",
                verdict=PolicyVerdict.REQUIRE_CEO,
                message="Unrecognized CEO request — ask for clarification before planning skills.",
            )
        ]
