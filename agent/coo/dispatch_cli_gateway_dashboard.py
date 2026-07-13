"""CLI gateway operator dashboard — Phase 13O.

Read-only ``gateway dashboard`` and ``gateway correlation diff`` wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from agent.coo.dispatch_gateway_operator_dashboard import (
    CooDispatchGatewayCorrelationDiff,
    CooDispatchGatewayOperatorDashboardSummary,
    GatewayOperatorDashboardError,
    build_gateway_correlation_diff,
    build_operator_dashboard_summary,
    correlation_diff_exit_code,
    dashboard_exit_code,
    format_gateway_correlation_diff,
    format_operator_dashboard_summary,
)


def show_operator_dashboard(
    *,
    ticket_id: str = "",
    session_id: str = "",
    limit: int | None = None,
    merged_config: Mapping[str, Any] | None = None,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> CooDispatchGatewayOperatorDashboardSummary:
    """Build one read-only operator dashboard summary."""
    try:
        return build_operator_dashboard_summary(
            ticket_id=ticket_id,
            session_id=session_id,
            limit=limit,
            merged_config=merged_config,
            request_dir=request_dir,
            history_dir=history_dir,
            evidence_dir=evidence_dir,
            audit_dir=audit_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
    except GatewayOperatorDashboardError:
        raise
    except (ValueError, KeyError) as exc:
        raise GatewayOperatorDashboardError(str(exc)) from exc


def run_operator_dashboard(
    *,
    ticket_id: str = "",
    session_id: str = "",
    limit: int | None = None,
    merged_config: Mapping[str, Any] | None = None,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> tuple[str, int]:
    """Return formatted dashboard output and CLI exit code."""
    summary = show_operator_dashboard(
        ticket_id=ticket_id,
        session_id=session_id,
        limit=limit,
        merged_config=merged_config,
        request_dir=request_dir,
        history_dir=history_dir,
        evidence_dir=evidence_dir,
        audit_dir=audit_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
    )
    return format_operator_dashboard_summary(summary), dashboard_exit_code(summary)


def show_gateway_correlation_diff(
    *,
    left_gateway_request_id: str,
    right_gateway_request_id: str,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> CooDispatchGatewayCorrelationDiff:
    """Build one read-only correlation diff."""
    try:
        return build_gateway_correlation_diff(
            left_gateway_request_id=left_gateway_request_id,
            right_gateway_request_id=right_gateway_request_id,
            request_dir=request_dir,
            history_dir=history_dir,
            evidence_dir=evidence_dir,
            audit_dir=audit_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
        )
    except GatewayOperatorDashboardError:
        raise
    except (ValueError, KeyError) as exc:
        raise GatewayOperatorDashboardError(str(exc)) from exc


def run_gateway_correlation_diff(
    *,
    left_gateway_request_id: str,
    right_gateway_request_id: str,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> tuple[str, int]:
    """Return formatted correlation diff output and CLI exit code."""
    diff = show_gateway_correlation_diff(
        left_gateway_request_id=left_gateway_request_id,
        right_gateway_request_id=right_gateway_request_id,
        request_dir=request_dir,
        history_dir=history_dir,
        evidence_dir=evidence_dir,
        audit_dir=audit_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
    )
    return format_gateway_correlation_diff(diff), correlation_diff_exit_code(diff)
