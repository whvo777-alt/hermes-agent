"""Dispatch execution runner — token-gated orchestration (Phase 10F).

Orchestrates approved dispatch: validates unlock token, opens scoped context,
invokes PipelineAdapter.dispatch(), and commits state only on success.
No Repository 2 execution unless a caller injects an executor into the adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from agent.coo.dispatch_unlock_context import dispatch_unlock_scope
from agent.coo.execution_contract import (
    SkillExecutionMode,
    SkillExecutionRequest,
    SkillExecutionStatus,
)
from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionRun,
    DispatchExecutionRunStore,
    DispatchExecutionRequestStore,
    DispatchUnlockTokenStore,
    assert_dispatch_generation_matches,
    consume_dispatch_unlock_token,
    get_default_dispatch_execution_request_store,
    get_default_dispatch_execution_run_store,
    get_default_dispatch_unlock_token_store,
    mark_dispatch_run_completed,
    mark_dispatch_run_failed,
    mark_dispatch_run_running,
    start_dispatch_run,
    _assert_token_usable,
)
from agent.coo.execution_dispatcher import (
    ExecutionDispatchPlanStore,
    get_default_dispatch_plan_store,
)
from agent.coo.execution_execute import (
    ExecuteGateStatus,
    ExecuteGateStore,
    get_default_execute_gate_store,
)
from agent.coo.execution_runtime import (
    ExecutionRunStore,
    get_default_execution_run_store,
)
from agent.coo.execution_ticket import (
    ExecutionTicket,
    ExecutionTicketStatus,
    ExecutionTicketStore,
    get_default_ticket_store,
)
from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig
from agent.coo.production_executor_confirmation import (
    ProductionExecutorConfirmation,
    ProductionExecutorConfirmationStore,
    get_default_production_executor_confirmation_store,
)
from agent.coo.production_executor_policy import (
    ProductionExecutorPolicy,
    assert_production_executor_allowed,
)
from agent.coo.skills_catalog import get_skill

_ALLOWED_DISPATCH_SKILLS = frozenset({"create_content"})
_DEFAULT_RUNTIME_WORKER_ID = "coo_dispatch_runner"


def _assert_requester_matches(ticket: ExecutionTicket, requested_by: str) -> None:
    if requested_by != ticket.requester_id:
        raise ValueError(
            f"Requester {requested_by!r} is not authorized for ticket {ticket.ticket_id} "
            f"(owner: {ticket.requester_id!r})"
        )


def _assert_create_content_only(target_skills: List[str]) -> None:
    if not target_skills:
        raise ValueError("Cannot dispatch with empty target_skills.")
    for skill_id in target_skills:
        if skill_id not in _ALLOWED_DISPATCH_SKILLS:
            raise ValueError(
                f"Dispatch target skill {skill_id!r} is not allowed in Phase 10F."
            )
        definition = get_skill(skill_id)
        if definition is not None and definition.read_only:
            raise ValueError(f"Read-only skill {skill_id!r} cannot be dispatched.")


def _commit_dispatch_success(
    ticket: ExecutionTicket,
    plan,
    run: DispatchExecutionRun,
) -> None:
    ticket.status = ExecutionTicketStatus.DISPATCHED
    ticket.execution_dispatched = True
    ticket.repository2_touched = True
    plan.executed = True
    plan.repository2_touched = True
    # publish_dispatched intentionally remains False.


def _catalog_entrypoint(skill_id: str) -> str:
    definition = get_skill(skill_id)
    if definition is not None:
        return definition.entrypoint_hint
    return ""


def _run_production_policy_gate(
    *,
    production_policy: ProductionExecutorPolicy,
    confirmation: Optional[ProductionExecutorConfirmation],
    ticket: ExecutionTicket,
    plan,
    gate,
    token,
    dispatch_request,
    pipeline_root: str,
    target_skills: List[str],
    dry_run_store: ExecutionRunStore,
) -> tuple[dict, str]:
    dry_run = dry_run_store.get(token.dry_run_run_id)
    if dry_run is None:
        raise KeyError(f"Dry-run not found for token: {token.dry_run_run_id}")

    entrypoint = _catalog_entrypoint(target_skills[0]) if target_skills else ""
    checklist = assert_production_executor_allowed(
        production_policy,
        ticket=ticket,
        plan=plan,
        dry_run=dry_run,
        gate=gate,
        token=token,
        dispatch_request=dispatch_request,
        pipeline_root=pipeline_root,
        entrypoint=entrypoint,
        target_skills=target_skills,
        confirmation=confirmation,
    )
    if not checklist.get("all_passed"):
        summary = str(checklist.get("summary") or "Production executor policy check failed.")
        return checklist, summary
    return checklist, ""


def run_approved_dispatch(
    unlock_token_id: str,
    *,
    requested_by: str,
    ticket_store: Optional[ExecutionTicketStore] = None,
    plan_store: Optional[ExecutionDispatchPlanStore] = None,
    gate_store: Optional[ExecuteGateStore] = None,
    token_store: Optional[DispatchUnlockTokenStore] = None,
    dispatch_request_store: Optional[DispatchExecutionRequestStore] = None,
    dispatch_run_store: Optional[DispatchExecutionRunStore] = None,
    dry_run_store: Optional[ExecutionRunStore] = None,
    adapter: Optional[PipelineAdapter] = None,
    production_policy: Optional[ProductionExecutorPolicy] = None,
    confirmation: Optional[ProductionExecutorConfirmation] = None,
    confirmation_store: Optional[ProductionExecutorConfirmationStore] = None,
    audit_dir: Optional[Path] = None,
    execution_attempt_id: str = "",
) -> DispatchExecutionRun:
    """Run token-gated dispatch for an approved unlock token."""
    tickets = ticket_store or get_default_ticket_store()
    plans = plan_store or get_default_dispatch_plan_store()
    gates = gate_store or get_default_execute_gate_store()
    tokens = token_store or get_default_dispatch_unlock_token_store()
    dispatch_requests = dispatch_request_store or get_default_dispatch_execution_request_store()
    runs = dispatch_run_store or get_default_dispatch_execution_run_store()
    dry_runs = dry_run_store or get_default_execution_run_store()
    confirmations = confirmation_store or get_default_production_executor_confirmation_store()

    token = tokens.get(unlock_token_id)
    if token is None:
        raise KeyError(f"Dispatch unlock token not found: {unlock_token_id}")

    _assert_token_usable(token)

    ticket = tickets.get(token.ticket_id)
    if ticket is None:
        raise KeyError(f"Execution ticket not found: {token.ticket_id}")

    plan = plans.get(token.plan_id)
    if plan is None:
        raise KeyError(f"Dispatch plan not found: {token.plan_id}")

    gate = gates.get(token.gate_id)
    if gate is None:
        raise KeyError(f"Execute gate not found: {token.gate_id}")

    _assert_requester_matches(ticket, requested_by)
    assert_dispatch_generation_matches(token, ticket)

    if gate.status is not ExecuteGateStatus.APPROVED:
        raise ValueError(
            f"Cannot dispatch with gate {gate.gate_id} in status {gate.status.value}"
        )
    if gate.decided_by != ticket.requester_id:
        raise ValueError(
            f"Gate {gate.gate_id} decided_by {gate.decided_by!r} does not match ticket owner "
            f"{ticket.requester_id!r}"
        )

    dispatch_request = dispatch_requests.get_by_execute_request(token.execute_request_id)
    if dispatch_request is None:
        raise KeyError(
            f"Dispatch execution request not found for execute request "
            f"{token.execute_request_id}"
        )

    if dispatch_request.unlock_token_id != token.token_id:
        raise ValueError(
            f"Dispatch request unlock_token_id {dispatch_request.unlock_token_id!r} "
            f"does not match token {token.token_id!r}"
        )
    if dispatch_request.execute_request_id != token.execute_request_id:
        raise ValueError(
            "Dispatch request execute_request_id does not match token execute_request_id."
        )
    if list(dispatch_request.target_skills) != list(token.target_skills):
        raise ValueError("Dispatch request target_skills do not match token target_skills.")

    _assert_create_content_only(list(dispatch_request.target_skills))

    pipeline = adapter or PipelineAdapter(
        PipelineAdapterConfig(allow_execute=True, repository2_read_only=True)
    )

    run = start_dispatch_run(dispatch_request, run_store=runs)
    mark_dispatch_run_running(run, run_store=runs)

    policy_enabled = (
        production_policy is not None and production_policy.enabled
    )
    if policy_enabled:
        try:
            checklist, policy_failure = _run_production_policy_gate(
                production_policy=production_policy,
                confirmation=confirmation,
                ticket=ticket,
                plan=plan,
                gate=gate,
                token=token,
                dispatch_request=dispatch_request,
                pipeline_root=pipeline.config.pipeline_root,
                target_skills=list(dispatch_request.target_skills),
                dry_run_store=dry_runs,
            )
        except (KeyError, ValueError) as exc:
            mark_dispatch_run_failed(
                run,
                summary=str(exc),
                skill_results=[],
                run_store=runs,
            )
            return run

        if policy_failure:
            mark_dispatch_run_failed(
                run,
                summary=policy_failure,
                skill_results=[],
                run_store=runs,
            )
            return run

        if pipeline._executor is None:
            mark_dispatch_run_failed(
                run,
                summary="Pipeline dispatch executor is not configured.",
                skill_results=[],
                run_store=runs,
            )
            return run

        from agent.coo.dispatch_execution_audit import (
            build_dispatch_execution_audit,
            write_dispatch_execution_audit,
        )

        dry_run = dry_runs.get(token.dry_run_run_id)
        assert dry_run is not None
        entrypoint = _catalog_entrypoint(dispatch_request.target_skills[0])
        audit = build_dispatch_execution_audit(
            dispatch_run=run,
            ticket=ticket,
            plan=plan,
            dry_run=dry_run,
            gate=gate,
            token=token,
            dispatch_request=dispatch_request,
            executor_policy=production_policy,
            pipeline_root=pipeline.config.pipeline_root,
            entrypoint=entrypoint,
            run_date=plan.run_date,
            pre_execution_checklist=checklist,
            requested_by=requested_by,
            operator_id=confirmation.operator_id if confirmation else "",
            operator_name=confirmation.operator_name if confirmation else "",
            confirmation_id=confirmation.confirmation_id if confirmation else "",
            execution_attempt_id=execution_attempt_id,
        )
        write_dispatch_execution_audit(audit, audit_dir)

    skill_results: List[dict] = []
    executor_invoked = False
    try:
        with dispatch_unlock_scope(
            token,
            ticket=ticket,
            plan=plan,
            gate=gate,
            pipeline_root=pipeline.config.pipeline_root,
        ):
            for skill_id in dispatch_request.target_skills:
                request = SkillExecutionRequest(
                    skill_id=skill_id,
                    worker_id=_DEFAULT_RUNTIME_WORKER_ID,
                    assignment_id=ticket.ticket_id,
                    run_date=plan.run_date,
                    mode=SkillExecutionMode.EXECUTE,
                    worker_mode="execute",
                    auto_apply=False,
                    review_required=True,
                )
                if pipeline._executor is not None:
                    executor_invoked = True
                result = pipeline.dispatch(
                    request,
                    unlock_token_id=token.token_id,
                )
                skill_results.append(result.to_dict())
                if result.status is not SkillExecutionStatus.COMPLETED:
                    mark_dispatch_run_failed(
                        run,
                        summary=result.blocked_reason or result.summary,
                        skill_results=skill_results,
                        run_store=runs,
                    )
                    return run
    except Exception as exc:
        mark_dispatch_run_failed(
            run,
            summary=str(exc),
            skill_results=skill_results,
            run_store=runs,
        )
        return run

    consume_dispatch_unlock_token(token.token_id, token_store=tokens)
    mark_dispatch_run_completed(
        run,
        skill_results,
        summary="Dispatch completed successfully.",
        run_store=runs,
    )
    _commit_dispatch_success(ticket, plan, run)
    if policy_enabled and confirmation is not None and executor_invoked:
        confirmations.consume(confirmation.confirmation_id)
    return run
