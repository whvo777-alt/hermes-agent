"""Gateway execution facade — Phase 13H scaffold.

Single entry point for future Gateway dispatch execution wiring.
No subprocess, runner, pipeline adapter, or Repository2 execution in this phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_DISABLED,
    load_dispatch_gateway_enablement,
)

GATEWAY_EXECUTION_FACADE_CONNECTED = True
GATEWAY_EXECUTION_FACADE_VERSION = "13H-scaffold"
GATEWAY_EXECUTION_ENABLED = False
GATEWAY_EXECUTION_ISOLATED_SUPPORTED = False

RECOMMENDED_NEXT_PHASE_SCAFFOLD = "Phase 13I Mock Gateway Dispatch"
RECOMMENDED_NEXT_PHASE_NOT_READY = (
    "Resolve gateway execution facade validation before Phase 13I."
)


class GatewayExecutionFacadeError(ValueError):
    """Raised when gateway execution facade state is invalid."""


class GatewayExecutionNotEnabled(RuntimeError):
    """Raised when gateway dispatch execution is scaffold-only."""


@dataclass(frozen=True)
class CooDispatchGatewayExecutionFacade:
    """Safe read-only gateway execution facade snapshot."""

    facade_connected: bool
    execution_enabled: bool
    production_execution_allowed: bool
    isolated_execution_supported: bool
    gateway_state: str
    version: str
    valid: bool = True


def _read_facade_connected_marker() -> bool:
    return GATEWAY_EXECUTION_FACADE_CONNECTED is True


def _validate_facade_policy(
    facade: CooDispatchGatewayExecutionFacade,
) -> CooDispatchGatewayExecutionFacade:
    if facade.execution_enabled and facade.production_execution_allowed:
        return CooDispatchGatewayExecutionFacade(
            facade_connected=facade.facade_connected,
            execution_enabled=facade.execution_enabled,
            production_execution_allowed=False,
            isolated_execution_supported=facade.isolated_execution_supported,
            gateway_state=facade.gateway_state,
            version=facade.version,
            valid=False,
        )
    if facade.production_execution_allowed:
        return CooDispatchGatewayExecutionFacade(
            facade_connected=facade.facade_connected,
            execution_enabled=facade.execution_enabled,
            production_execution_allowed=False,
            isolated_execution_supported=facade.isolated_execution_supported,
            gateway_state=facade.gateway_state,
            version=facade.version,
            valid=False,
        )
    return facade


def load_gateway_execution_facade(
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchGatewayExecutionFacade:
    """Load gateway execution facade state without invoking dispatch."""
    if merged_config is None:
        merged_config = {}

    enablement = load_dispatch_gateway_enablement(merged_config=merged_config)
    marker_connected = _read_facade_connected_marker()
    if not marker_connected or not enablement.valid:
        return CooDispatchGatewayExecutionFacade(
            facade_connected=False,
            execution_enabled=False,
            production_execution_allowed=False,
            isolated_execution_supported=False,
            gateway_state=enablement.gateway_state,
            version=GATEWAY_EXECUTION_FACADE_VERSION,
            valid=False,
        )

    facade = CooDispatchGatewayExecutionFacade(
        facade_connected=True,
        execution_enabled=GATEWAY_EXECUTION_ENABLED,
        production_execution_allowed=False,
        isolated_execution_supported=GATEWAY_EXECUTION_ISOLATED_SUPPORTED,
        gateway_state=enablement.gateway_state,
        version=GATEWAY_EXECUTION_FACADE_VERSION,
        valid=True,
    )
    return _validate_facade_policy(facade)


def evaluate_gateway_execution_facade(
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchGatewayExecutionFacade:
    """Evaluate facade readiness markers and policy invariants."""
    return load_gateway_execution_facade(merged_config=merged_config)


def resolve_gateway_facade_recommended_next_phase(
    facade: CooDispatchGatewayExecutionFacade,
) -> str:
    """Return recommended next phase for gateway execution facade."""
    if not facade.valid or not facade.facade_connected:
        return RECOMMENDED_NEXT_PHASE_NOT_READY
    if facade.gateway_state == GATEWAY_STATE_DISABLED:
        return RECOMMENDED_NEXT_PHASE_NOT_READY
    if not facade.execution_enabled:
        return RECOMMENDED_NEXT_PHASE_SCAFFOLD
    return RECOMMENDED_NEXT_PHASE_NOT_READY


def format_gateway_execution_facade(
    facade: CooDispatchGatewayExecutionFacade,
) -> str:
    """Format safe gateway execution facade fields for CLI stdout."""
    recommended = resolve_gateway_facade_recommended_next_phase(facade)
    lines = [
        "Gateway Execution Facade",
        "",
        f"facade_version: {facade.version}",
        f"facade_connected: {str(facade.facade_connected).lower()}",
        f"execution_enabled: {str(facade.execution_enabled).lower()}",
        (
            "isolated_execution_supported: "
            f"{str(facade.isolated_execution_supported).lower()}"
        ),
        (
            "production_execution_allowed: "
            f"{str(facade.production_execution_allowed).lower()}"
        ),
        f"gateway_state: {facade.gateway_state}",
        f"recommended_next_phase: {recommended}",
    ]
    return "\n".join(lines)


def execute_gateway_dispatch(**_kwargs: Any) -> None:
    """Gateway dispatch entry point — scaffold only, no execution."""
    facade = load_gateway_execution_facade()
    if not facade.valid or not facade.facade_connected:
        raise GatewayExecutionNotEnabled(
            "Gateway execution facade is not connected."
        )
    if not facade.execution_enabled:
        raise GatewayExecutionNotEnabled(
            "Gateway dispatch execution is not enabled in the Phase 13H scaffold."
        )
    raise NotImplementedError(
        "Gateway dispatch execution is not implemented in the Phase 13H scaffold."
    )
