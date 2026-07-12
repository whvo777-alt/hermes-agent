"""CLI dispatch gateway status — Phase 13F / 13H.

Read-only gateway enablement and facade summary for operator review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.dispatch_gateway_enablement import (
    CooDispatchGatewayEnablement,
    load_dispatch_gateway_enablement,
    resolve_gateway_recommended_next_phase,
)
from agent.coo.dispatch_gateway_execution_facade import (
    CooDispatchGatewayExecutionFacade,
    evaluate_gateway_execution_facade,
)


@dataclass(frozen=True)
class CooDispatchGatewayStatusSummary:
    """Safe read-only gateway status including facade scaffold fields."""

    enablement: CooDispatchGatewayEnablement
    facade: CooDispatchGatewayExecutionFacade


def summarize_dispatch_gateway_status(
    *,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchGatewayStatusSummary:
    """Load gateway enablement and facade without mutating config."""
    if merged_config is None:
        merged_config = {}
    enablement = load_dispatch_gateway_enablement(merged_config=merged_config)
    facade = evaluate_gateway_execution_facade(merged_config=merged_config)
    return CooDispatchGatewayStatusSummary(enablement=enablement, facade=facade)


def format_dispatch_gateway_status_summary(
    summary: CooDispatchGatewayStatusSummary | CooDispatchGatewayEnablement,
) -> str:
    """Format safe gateway status fields for CLI stdout."""
    if isinstance(summary, CooDispatchGatewayEnablement):
        enablement = summary
        facade = evaluate_gateway_execution_facade()
    else:
        enablement = summary.enablement
        facade = summary.facade

    recommended = resolve_gateway_recommended_next_phase(enablement)
    lines = [
        "Gateway Enablement Status",
        "",
        f"gateway_state: {enablement.gateway_state}",
        f"gateway_enabled: {str(enablement.gateway_enabled).lower()}",
        f"gateway_staged: {str(enablement.gateway_staged).lower()}",
        (
            "gateway_execution_configured: "
            f"{str(enablement.gateway_execution_configured).lower()}"
        ),
        f"facade_connected: {str(facade.facade_connected).lower()}",
        f"execution_enabled: {str(facade.execution_enabled).lower()}",
        (
            "isolated_execution_supported: "
            f"{str(facade.isolated_execution_supported).lower()}"
        ),
        (
            "production_execution_allowed: "
            f"{str(enablement.production_execution_allowed).lower()}"
        ),
        (
            "production_root_hard_deny: "
            f"{str(enablement.production_root_hard_deny).lower()}"
        ),
        f"valid: {str(enablement.valid).lower()}",
        f"recommended_next_phase: {recommended}",
    ]
    return "\n".join(lines)
