"""CLI confirm-run bundle cross-validation — Phase 10V.

Loads and validates persisted dispatch bundles before operator confirmations
are minted. No dispatch execution, subprocess, or persistence writes beyond
confirmation creation after validation passes.
"""

from __future__ import annotations

from pathlib import Path

from agent.coo.dispatch_bundle_store import (
    DispatchExecutionBundle,
    read_bundle,
    validate_bundle_for_cli_execution,
)


def validate_confirm_run_bundle_evidence(
    *,
    ticket_id: str,
    plan_id: str,
    unlock_token_id: str,
    dispatch_request_id: str,
    bundle_dir: Path | None = None,
) -> DispatchExecutionBundle:
    """Fail-closed bundle validation before confirm-run persists confirmation."""
    normalized_ticket_id = ticket_id.strip()
    normalized_plan_id = plan_id.strip()
    normalized_unlock_token_id = unlock_token_id.strip()
    normalized_dispatch_request_id = dispatch_request_id.strip()

    if not normalized_ticket_id:
        raise ValueError("ticket_id is required")
    if not normalized_plan_id:
        raise ValueError("plan_id is required")
    if not normalized_unlock_token_id:
        raise ValueError("unlock_token_id is required")
    if not normalized_dispatch_request_id:
        raise ValueError("dispatch_request_id is required")

    bundle = read_bundle(
        normalized_ticket_id,
        bundle_dir=bundle_dir,
        reject_consumed=True,
    )
    validate_bundle_for_cli_execution(bundle)

    if bundle.ticket_id != normalized_ticket_id:
        raise ValueError("CLI ticket_id does not match bundle ticket_id.")
    if bundle.plan_id != normalized_plan_id:
        raise ValueError("CLI plan_id does not match bundle plan_id.")
    if bundle.unlock_token_id != normalized_unlock_token_id:
        raise ValueError("CLI unlock_token_id does not match bundle unlock_token_id.")
    if bundle.dispatch_request_id != normalized_dispatch_request_id:
        raise ValueError(
            "CLI dispatch_request_id does not match bundle dispatch_request_id."
        )

    return bundle


def execute_coo_dispatch_confirm_run(
    *,
    ticket_id: str,
    plan_id: str,
    unlock_token_id: str,
    dispatch_request_id: str,
    operator_id: str,
    operator_name: str,
    confirmation_reason: str,
    confirmation_phrase: str,
    pipeline_root: str,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> "ProductionExecutorConfirmation":
    """Validate bundle evidence, then mint a persisted operator confirmation."""
    from agent.coo.dispatch_pipeline_root_trust import assert_pipeline_root_trusted
    from agent.coo.production_executor_confirmation import (
        ProductionExecutorConfirmation,
        create_production_executor_confirmation,
    )

    attested_pipeline_root = assert_pipeline_root_trusted(pipeline_root)
    validate_confirm_run_bundle_evidence(
        ticket_id=ticket_id,
        plan_id=plan_id,
        unlock_token_id=unlock_token_id,
        dispatch_request_id=dispatch_request_id,
        bundle_dir=bundle_dir,
    )
    return create_production_executor_confirmation(
        ticket_id=ticket_id,
        plan_id=plan_id,
        unlock_token_id=unlock_token_id,
        dispatch_request_id=dispatch_request_id,
        operator_id=operator_id,
        operator_name=operator_name,
        confirmation_reason=confirmation_reason,
        confirmation_phrase=confirmation_phrase,
        attested_pipeline_root=attested_pipeline_root,
        persist_to_file=True,
        confirmation_dir=confirmation_dir,
    )
