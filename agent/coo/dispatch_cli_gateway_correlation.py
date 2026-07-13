"""CLI gateway correlation explorer — Phase 13N.

Read-only ``gateway correlation show`` command wiring.
"""

from __future__ import annotations

from pathlib import Path

from agent.coo.dispatch_gateway_correlation_explorer import (
    CooDispatchGatewayCorrelationChain,
    GatewayCorrelationExplorerError,
    correlation_chain_exit_code,
    explore_gateway_correlation,
    format_gateway_correlation_chain,
    normalize_gateway_correlation_query,
)


def show_gateway_correlation_chain(
    *,
    gateway_request_id: str = "",
    pilot_attempt_id: str = "",
    execution_attempt_id: str = "",
    dispatch_run_id: str = "",
    ticket_id: str = "",
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> CooDispatchGatewayCorrelationChain:
    """Explore one read-only Gateway correlation chain."""
    query = normalize_gateway_correlation_query(
        gateway_request_id=gateway_request_id,
        pilot_attempt_id=pilot_attempt_id,
        execution_attempt_id=execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        ticket_id=ticket_id,
    )
    try:
        return explore_gateway_correlation(
            query,
            request_dir=request_dir,
            history_dir=history_dir,
            evidence_dir=evidence_dir,
            audit_dir=audit_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
    except GatewayCorrelationExplorerError:
        raise
    except (ValueError, KeyError) as exc:
        raise GatewayCorrelationExplorerError(str(exc)) from exc


def run_gateway_correlation_show(
    *,
    gateway_request_id: str = "",
    pilot_attempt_id: str = "",
    execution_attempt_id: str = "",
    dispatch_run_id: str = "",
    ticket_id: str = "",
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> tuple[str, int]:
    """Return formatted correlation output and CLI exit code."""
    chain = show_gateway_correlation_chain(
        gateway_request_id=gateway_request_id,
        pilot_attempt_id=pilot_attempt_id,
        execution_attempt_id=execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        ticket_id=ticket_id,
        request_dir=request_dir,
        history_dir=history_dir,
        evidence_dir=evidence_dir,
        audit_dir=audit_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
    )
    return format_gateway_correlation_chain(chain), correlation_chain_exit_code(chain)
