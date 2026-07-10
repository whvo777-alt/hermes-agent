"""CLI dispatch run orchestration — Phase 10Q persistence wiring.

Loads bundle + confirmation files, validates fail-closed, hydrates in-memory
stores, and delegates to run_approved_dispatch(). No real subprocess unless a
caller explicitly injects subprocess_runner (tests only).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from agent.coo.dispatch_bundle_store import (
    DispatchExecutionBundle,
    mark_bundle_consumed,
    read_bundle,
    validate_bundle_for_cli_execution,
)
from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionMode,
    DispatchExecutionRequest,
    DispatchExecutionRequestStore,
    DispatchExecutionRunStatus,
    DispatchExecutionRunStore,
    DispatchUnlockToken,
    DispatchUnlockTokenStore,
)
from agent.coo.execution_dispatch_runner import run_approved_dispatch
from agent.coo.execution_dispatcher import (
    DispatchPlanStatus,
    ExecutionDispatchPlan,
    ExecutionDispatchPlanStore,
)
from agent.coo.execution_execute import (
    ExecuteGate,
    ExecuteGateStatus,
    ExecuteGateStore,
    ExecuteRequest,
    ExecuteRequestMode,
    ExecuteRequestStore,
)
from agent.coo.execution_runtime import (
    ExecutionRequest,
    ExecutionRequestStore,
    ExecutionRun,
    ExecutionRunMode,
    ExecutionRunStatus,
    ExecutionRunStore,
)
from agent.coo.execution_ticket import (
    ExecutionTicket,
    ExecutionTicketStatus,
    ExecutionTicketStore,
)
from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig
from agent.coo.production_executor_confirmation import (
    ProductionExecutorConfirmation,
    ProductionExecutorConfirmationStore,
    mark_confirmation_consumed_file,
    read_confirmation,
    validate_confirmation_for_cli_execution,
)
from agent.coo.production_executor_factory import (
    SubprocessRunner,
    _ALLOWED_FACTORY_ENTRYPOINT,
    build_pipeline_dispatch_executor,
)
from agent.coo.production_executor_policy import ProductionExecutorPolicy
from hermes_constants import get_hermes_home

if TYPE_CHECKING:
    from agent.coo.dispatch_cli_preflight import CooDispatchPreflightSummary


@dataclass(frozen=True)
class CooDispatchRunResult:
    """Outcome of a CLI dispatch run attempt."""

    ticket_id: str
    confirmation_id: str
    dispatch_request_id: str
    status: str
    consumed: bool
    dry_run_only: bool = False
    preflight: Optional["CooDispatchPreflightSummary"] = None


def _object_from_dict(
    cls: type,
    payload: Dict[str, Any],
    *,
    enum_fields: Dict[str, type] | None = None,
    tuple_fields: frozenset[str] = frozenset(),
) -> Any:
    enum_fields = enum_fields or {}
    kwargs: Dict[str, Any] = {}
    for field in dataclasses.fields(cls):
        if field.name not in payload:
            continue
        value = payload[field.name]
        if field.name in enum_fields:
            kwargs[field.name] = enum_fields[field.name](str(value))
        elif field.name in tuple_fields and isinstance(value, list):
            kwargs[field.name] = tuple(value)
        else:
            kwargs[field.name] = value
    return cls(**kwargs)


def hydrate_dispatch_evidence_from_bundle(
    bundle: DispatchExecutionBundle,
) -> Dict[str, Any]:
    """Rebuild policy-check evidence objects from a bundle snapshot only."""
    snap = bundle.snapshot
    ticket = _object_from_dict(
        ExecutionTicket,
        snap["ticket"],
        enum_fields={"status": ExecutionTicketStatus},
    )
    plan = _object_from_dict(
        ExecutionDispatchPlan,
        snap["plan"],
        enum_fields={"status": DispatchPlanStatus},
    )
    dry_run = _object_from_dict(
        ExecutionRun,
        snap["dry_run"],
        enum_fields={
            "status": ExecutionRunStatus,
            "mode": ExecutionRunMode,
        },
    )
    dry_run_request = _object_from_dict(
        ExecutionRequest,
        snap["dry_run_request"],
        enum_fields={"mode": ExecutionRunMode},
    )
    execute_request = _object_from_dict(
        ExecuteRequest,
        snap["execute_request"],
        enum_fields={"mode": ExecuteRequestMode},
    )
    gate = _object_from_dict(
        ExecuteGate,
        snap["gate"],
        enum_fields={"status": ExecuteGateStatus},
    )
    token = _object_from_dict(
        DispatchUnlockToken,
        snap["unlock_token"],
        tuple_fields=frozenset({"target_skills"}),
    )
    dispatch_request = _object_from_dict(
        DispatchExecutionRequest,
        snap["dispatch_request"],
        enum_fields={"mode": DispatchExecutionMode},
    )
    _assert_hydrated_evidence_matches_bundle(
        bundle,
        ticket=ticket,
        plan=plan,
        dry_run=dry_run,
        dry_run_request=dry_run_request,
        execute_request=execute_request,
        gate=gate,
        token=token,
        dispatch_request=dispatch_request,
    )
    return {
        "ticket": ticket,
        "plan": plan,
        "dry_run": dry_run,
        "dry_run_request": dry_run_request,
        "execute_request": execute_request,
        "gate": gate,
        "token": token,
        "dispatch_request": dispatch_request,
    }


def hydrate_dispatch_stores_from_bundle(
    bundle: DispatchExecutionBundle,
) -> Dict[str, Any]:
    """Populate in-memory stores from a validated bundle snapshot.

    Evidence-only restore: every record is rebuilt exclusively from the
    persisted snapshot dicts. No new ticket/token/request/gate IDs are minted,
    no remint/prepare helpers are invoked, and no statuses are recomputed.
    """
    evidence = hydrate_dispatch_evidence_from_bundle(bundle)
    ticket = evidence["ticket"]
    plan = evidence["plan"]
    dry_run = evidence["dry_run"]
    dry_run_request = evidence["dry_run_request"]
    execute_request = evidence["execute_request"]
    gate = evidence["gate"]
    token = evidence["token"]
    dispatch_request = evidence["dispatch_request"]

    ticket_store = ExecutionTicketStore()
    plan_store = ExecutionDispatchPlanStore()
    dry_run_store = ExecutionRunStore()
    dry_run_request_store = ExecutionRequestStore()
    execute_request_store = ExecuteRequestStore()
    gate_store = ExecuteGateStore()
    token_store = DispatchUnlockTokenStore()
    dispatch_request_store = DispatchExecutionRequestStore()
    dispatch_run_store = DispatchExecutionRunStore()

    ticket_store.save(ticket)
    plan_store.save(plan)
    dry_run_store.save(dry_run)
    dry_run_request_store.save(dry_run_request)
    execute_request_store.save(execute_request)
    gate_store.save(gate)
    token_store.save(token)
    dispatch_request_store.save(dispatch_request)

    return {
        "ticket": ticket,
        "plan": plan,
        "gate": gate,
        "token": token,
        "dispatch_request": dispatch_request,
        "ticket_store": ticket_store,
        "plan_store": plan_store,
        "dry_run_store": dry_run_store,
        "dry_run_request_store": dry_run_request_store,
        "execute_request_store": execute_request_store,
        "gate_store": gate_store,
        "token_store": token_store,
        "dispatch_request_store": dispatch_request_store,
        "dispatch_run_store": dispatch_run_store,
    }


def _assert_hydrated_evidence_matches_bundle(
    bundle: DispatchExecutionBundle,
    *,
    ticket: ExecutionTicket,
    plan: ExecutionDispatchPlan,
    dry_run: ExecutionRun,
    dry_run_request: ExecutionRequest,
    execute_request: ExecuteRequest,
    gate: ExecuteGate,
    token: DispatchUnlockToken,
    dispatch_request: DispatchExecutionRequest,
) -> None:
    """Ensure hydrated records match bundle evidence without mutation."""
    snap = bundle.snapshot
    checks = (
        (ticket.ticket_id, bundle.ticket_id, "ticket.ticket_id"),
        (plan.plan_id, bundle.plan_id, "plan.plan_id"),
        (dry_run.run_id, bundle.dry_run_run_id, "dry_run.run_id"),
        (execute_request.execute_request_id, bundle.execute_request_id, "execute_request_id"),
        (gate.gate_id, bundle.gate_id, "gate.gate_id"),
        (token.token_id, bundle.unlock_token_id, "unlock_token.token_id"),
        (dispatch_request.dispatch_request_id, bundle.dispatch_request_id, "dispatch_request_id"),
        (token.dispatch_generation, bundle.dispatch_generation, "dispatch_generation"),
        (dry_run_request.request_id, snap["dry_run_request"]["request_id"], "dry_run_request.request_id"),
        (dry_run.request_id, snap["dry_run"]["request_id"], "dry_run.request_id"),
    )
    for actual, expected, label in checks:
        if actual != expected:
            raise ValueError(
                f"Hydrated {label} {actual!r} does not match bundle evidence {expected!r}"
            )


def _persist_dispatch_consumption(
    *,
    ticket_id: str,
    confirmation_id: str,
    bundle_dir: Path | None,
    confirmation_dir: Path | None,
) -> None:
    """Mark confirmation and bundle consumed; fail-closed on partial persistence."""
    mark_confirmation_consumed_file(
        confirmation_id,
        confirmation_dir=confirmation_dir,
    )
    try:
        mark_bundle_consumed(ticket_id, bundle_dir=bundle_dir)
    except (ValueError, OSError, KeyError) as exc:
        raise ValueError(
            "Dispatch run completed but persisted bundle consume failed; "
            "confirmation may already be consumed."
        ) from exc


def execute_coo_dispatch_run(
    *,
    ticket_id: str,
    confirmation_id: str,
    unlock_token_id: str,
    requester_id: str,
    pipeline_root: str,
    dry_run: bool = False,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    audit_dir: Path | None = None,
    evidence_dir: Path | None = None,
    subprocess_runner: SubprocessRunner | None = None,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchRunResult:
    """Load persisted state, validate, and run dispatch (mock runner in tests only)."""
    if not ticket_id.strip():
        raise ValueError("ticket_id is required")
    if not confirmation_id.strip():
        raise ValueError("confirmation_id is required")
    if not unlock_token_id.strip():
        raise ValueError("unlock_token_id is required")
    if not requester_id.strip():
        raise ValueError("requester_id is required")
    if not pipeline_root.strip():
        raise ValueError("pipeline_root is required")

    from hermes_cli.coo_dispatch import assert_cli_pipeline_root_trusted

    assert_cli_pipeline_root_trusted(pipeline_root)

    bundle = read_bundle(ticket_id, bundle_dir=bundle_dir, reject_consumed=True)
    validate_bundle_for_cli_execution(bundle)

    if bundle.unlock_token_id != unlock_token_id:
        raise ValueError(
            "CLI unlock_token_id does not match bundle unlock_token_id."
        )
    if bundle.requester_id != requester_id:
        raise ValueError("CLI requester_id does not match bundle requester_id.")

    confirmation = read_confirmation(
        confirmation_id,
        confirmation_dir=confirmation_dir,
        reject_consumed=True,
    )
    validate_confirmation_for_cli_execution(
        confirmation,
        bundle=bundle,
        expected_confirmation_id=confirmation_id,
    )

    if dry_run:
        from agent.coo.dispatch_cli_preflight import run_dispatch_policy_preflight

        preflight = run_dispatch_policy_preflight(
            bundle=bundle,
            confirmation=confirmation,
            pipeline_root=pipeline_root,
            merged_config=merged_config,
        )
        return CooDispatchRunResult(
            ticket_id=bundle.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            dispatch_request_id=bundle.dispatch_request_id,
            status="preflight_passed" if preflight.all_passed else "preflight_failed",
            consumed=False,
            dry_run_only=True,
            preflight=preflight,
        )

    if subprocess_runner is None:
        raise ValueError("production runner is not configured")

    stores = hydrate_dispatch_stores_from_bundle(bundle)
    token = stores["token"]
    dispatch_request = stores["dispatch_request"]

    confirmation_store = ProductionExecutorConfirmationStore()
    confirmation_store.save(confirmation)

    resolved_audit_dir = audit_dir or (get_hermes_home() / "coo" / "audit")
    resolved_evidence_dir = evidence_dir or (get_hermes_home() / "coo" / "execution-evidence")

    policy = ProductionExecutorPolicy(
        enabled=True,
        allowed_pipeline_roots=(pipeline_root,),
    )
    executor = build_pipeline_dispatch_executor(
        policy,
        pipeline_root=pipeline_root,
        entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
        subprocess_runner=subprocess_runner,
        node_path="/usr/bin/node",
        evidence_dir=resolved_evidence_dir,
    )
    adapter = PipelineAdapter(
        PipelineAdapterConfig(allow_execute=True, pipeline_root=pipeline_root),
        executor=executor,
    )

    run = run_approved_dispatch(
        token.token_id,
        requested_by=requester_id,
        ticket_store=stores["ticket_store"],
        plan_store=stores["plan_store"],
        gate_store=stores["gate_store"],
        token_store=stores["token_store"],
        dispatch_request_store=stores["dispatch_request_store"],
        dispatch_run_store=stores["dispatch_run_store"],
        dry_run_store=stores["dry_run_store"],
        adapter=adapter,
        production_policy=policy,
        confirmation=confirmation,
        confirmation_store=confirmation_store,
        audit_dir=resolved_audit_dir,
    )

    if run.status is not DispatchExecutionRunStatus.COMPLETED:
        return CooDispatchRunResult(
            ticket_id=bundle.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            status=run.status.value,
            consumed=False,
        )

    _persist_dispatch_consumption(
        ticket_id=ticket_id,
        confirmation_id=confirmation.confirmation_id,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
    )

    return CooDispatchRunResult(
        ticket_id=bundle.ticket_id,
        confirmation_id=confirmation.confirmation_id,
        dispatch_request_id=dispatch_request.dispatch_request_id,
        status=run.status.value,
        consumed=True,
    )
