"""CLI dispatch gateway status — Phase 13F.

Read-only gateway enablement summary for operator review.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.coo.dispatch_gateway_enablement import (
    CooDispatchGatewayEnablement,
    load_dispatch_gateway_enablement,
    resolve_gateway_recommended_next_phase,
)


def summarize_dispatch_gateway_status(
    *,
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchGatewayEnablement:
    """Load gateway enablement without mutating config or invoking subprocess."""
    return load_dispatch_gateway_enablement(merged_config=merged_config)


def format_dispatch_gateway_status_summary(
    summary: CooDispatchGatewayEnablement,
) -> str:
    """Format safe gateway status fields for CLI stdout."""
    recommended = resolve_gateway_recommended_next_phase(summary)
    lines = [
        "Gateway Enablement Status",
        "",
        f"gateway_state: {summary.gateway_state}",
        f"gateway_enabled: {str(summary.gateway_enabled).lower()}",
        f"gateway_staged: {str(summary.gateway_staged).lower()}",
        (
            "gateway_execution_configured: "
            f"{str(summary.gateway_execution_configured).lower()}"
        ),
        (
            "production_execution_allowed: "
            f"{str(summary.production_execution_allowed).lower()}"
        ),
        (
            "production_root_hard_deny: "
            f"{str(summary.production_root_hard_deny).lower()}"
        ),
        f"valid: {str(summary.valid).lower()}",
        f"recommended_next_phase: {recommended}",
    ]
    return "\n".join(lines)
