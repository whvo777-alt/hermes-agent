"""Shared dispatch bundle load/validate — Phase 11E.

Read-only bundle persistence checks for confirm-run and pre-run validation.
No confirmation access, subprocess, factory, runner, or dispatch execution.
"""

from __future__ import annotations

from pathlib import Path

from agent.coo.dispatch_bundle_store import (
    DispatchExecutionBundle,
    read_bundle,
    validate_bundle_for_cli_execution,
)


def load_validated_dispatch_bundle_for_cli(
    *,
    ticket_id: str,
    bundle_dir: Path | None = None,
    reject_consumed: bool = True,
) -> DispatchExecutionBundle:
    """Load a bundle file and run fail-closed CLI execution validation."""
    normalized_ticket_id = (ticket_id or "").strip()
    if not normalized_ticket_id:
        raise ValueError("ticket_id is required")

    bundle = read_bundle(
        normalized_ticket_id,
        bundle_dir=bundle_dir,
        reject_consumed=reject_consumed,
    )
    validate_bundle_for_cli_execution(bundle)
    return bundle
