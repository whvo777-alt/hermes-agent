"""COO dispatch gateway enablement state — Phase 13F.

Read-only loader for ``coo.dispatch.gateway.enablement``. No config writes,
subprocess, Gateway/Discord calls, or automatic state transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

GATEWAY_STATE_DISABLED = "disabled"
GATEWAY_STATE_STAGED = "staged"
GATEWAY_STATE_ENABLED = "enabled"

_VALID_GATEWAY_STATES = frozenset(
    {
        GATEWAY_STATE_DISABLED,
        GATEWAY_STATE_STAGED,
        GATEWAY_STATE_ENABLED,
    }
)
_KNOWN_GATEWAY_CONFIG_KEYS = frozenset({"enablement"})

RECOMMENDED_NEXT_PHASE_DISABLED = "Phase 13G Gateway Read-Only Status"
RECOMMENDED_NEXT_PHASE_STAGED = "Phase 13I Mock Gateway Dispatch"
RECOMMENDED_NEXT_PHASE_ENABLED_NO_FACADE = (
    "Phase 13H Connect Gateway Execution Facade"
)
RECOMMENDED_NEXT_PHASE_ENABLED_READY = "Phase 13I Mock Gateway Dispatch"

GATEWAY_CHECK_REASON_FACADE_NOT_CONNECTED = "facade_not_connected"
GATEWAY_CHECK_REASON_INVALID_CONFIG = "invalid_gateway_config"


class DispatchGatewayEnablementError(ValueError):
    """Raised when gateway enablement config is malformed."""


@dataclass(frozen=True)
class CooDispatchGatewayEnablement:
    """Safe read-only gateway enablement snapshot."""

    gateway_state: str
    gateway_enabled: bool
    gateway_staged: bool
    gateway_execution_configured: bool
    production_execution_allowed: bool = False
    production_root_hard_deny: bool = True
    valid: bool = True


def _gateway_config_section(merged_config: Mapping[str, Any]) -> dict[str, Any] | None:
    coo = merged_config.get("coo")
    if coo is None:
        return None
    if not isinstance(coo, dict):
        raise DispatchGatewayEnablementError("config coo section must be a mapping.")
    dispatch = coo.get("dispatch")
    if dispatch is None:
        return None
    if not isinstance(dispatch, dict):
        raise DispatchGatewayEnablementError(
            "config coo.dispatch section must be a mapping."
        )
    gateway = dispatch.get("gateway")
    if gateway is None:
        return None
    if not isinstance(gateway, dict):
        raise DispatchGatewayEnablementError(
            "config coo.dispatch.gateway section must be a mapping."
        )
    return dict(gateway)


def _parse_gateway_enablement_state(raw_gateway: Mapping[str, Any] | None) -> str:
    if raw_gateway is None:
        return GATEWAY_STATE_DISABLED

    unknown_keys = set(raw_gateway) - _KNOWN_GATEWAY_CONFIG_KEYS
    if unknown_keys:
        joined = ", ".join(sorted(unknown_keys))
        raise DispatchGatewayEnablementError(
            f"Unknown coo.dispatch.gateway config keys: {joined}"
        )

    enablement = raw_gateway.get("enablement", GATEWAY_STATE_DISABLED)
    if not isinstance(enablement, str):
        raise DispatchGatewayEnablementError(
            "coo.dispatch.gateway.enablement must be a string."
        )
    normalized = enablement.strip()
    if not normalized:
        raise DispatchGatewayEnablementError(
            "coo.dispatch.gateway.enablement must be non-empty."
        )
    if normalized not in _VALID_GATEWAY_STATES:
        raise DispatchGatewayEnablementError(
            "coo.dispatch.gateway.enablement must be one of: "
            "disabled, staged, enabled."
        )
    return normalized


def _gateway_execution_facade_connected() -> bool:
    """Read-only probe for gateway execution facade wiring (Phase 13H+)."""
    try:
        from agent.coo.dispatch_gateway_execution_facade import (
            GATEWAY_EXECUTION_FACADE_CONNECTED,
        )
    except ImportError:
        return False
    return GATEWAY_EXECUTION_FACADE_CONNECTED is True


def _build_enablement_from_state(state: str) -> CooDispatchGatewayEnablement:
    facade_connected = _gateway_execution_facade_connected()
    return CooDispatchGatewayEnablement(
        gateway_state=state,
        gateway_enabled=state == GATEWAY_STATE_ENABLED,
        gateway_staged=state == GATEWAY_STATE_STAGED,
        gateway_execution_configured=facade_connected,
        production_execution_allowed=False,
        production_root_hard_deny=True,
        valid=True,
    )


def load_dispatch_gateway_enablement(
    merged_config: Mapping[str, Any] | None = None,
) -> CooDispatchGatewayEnablement:
    """Load and validate gateway enablement from merged Hermes config."""
    if merged_config is None:
        merged_config = {}

    try:
        gateway_section = _gateway_config_section(merged_config)
        state = _parse_gateway_enablement_state(gateway_section)
    except DispatchGatewayEnablementError:
        return CooDispatchGatewayEnablement(
            gateway_state=GATEWAY_STATE_DISABLED,
            gateway_enabled=False,
            gateway_staged=False,
            gateway_execution_configured=False,
            production_execution_allowed=False,
            production_root_hard_deny=True,
            valid=False,
        )

    enablement = _build_enablement_from_state(state)
    if enablement.production_execution_allowed:
        return CooDispatchGatewayEnablement(
            gateway_state=state,
            gateway_enabled=state == GATEWAY_STATE_ENABLED,
            gateway_staged=state == GATEWAY_STATE_STAGED,
            gateway_execution_configured=enablement.gateway_execution_configured,
            production_execution_allowed=False,
            production_root_hard_deny=True,
            valid=False,
        )
    return enablement


def gateway_execution_intentionally_blocked(
    enablement: CooDispatchGatewayEnablement,
) -> bool:
    """True when gateway dispatch execution should remain blocked by policy."""
    if not enablement.valid:
        return True
    return enablement.gateway_state in {
        GATEWAY_STATE_DISABLED,
        GATEWAY_STATE_STAGED,
    }


def resolve_gateway_recommended_next_phase(
    enablement: CooDispatchGatewayEnablement,
) -> str:
    """Return the recommended next phase for gateway enablement."""
    if not enablement.valid:
        return RECOMMENDED_NEXT_PHASE_DISABLED
    if enablement.gateway_state == GATEWAY_STATE_DISABLED:
        return RECOMMENDED_NEXT_PHASE_DISABLED
    if enablement.gateway_state == GATEWAY_STATE_STAGED:
        return RECOMMENDED_NEXT_PHASE_STAGED
    if enablement.gateway_execution_configured:
        return RECOMMENDED_NEXT_PHASE_ENABLED_READY
    return RECOMMENDED_NEXT_PHASE_ENABLED_NO_FACADE


def evaluate_gateway_production_check(
    enablement: CooDispatchGatewayEnablement,
) -> str:
    """Map gateway enablement to production readiness/signoff check status."""
    from agent.coo.dispatch_cli_production_readiness import (
        CHECK_BLOCKED,
        CHECK_FAIL,
    )
    from agent.coo.dispatch_gateway_execution_facade import (
        evaluate_gateway_execution_facade,
    )

    if not enablement.valid:
        return CHECK_FAIL
    if enablement.gateway_state == GATEWAY_STATE_ENABLED:
        if not enablement.gateway_execution_configured:
            return CHECK_FAIL
        facade = evaluate_gateway_execution_facade()
        if not facade.valid or not facade.facade_connected:
            return CHECK_FAIL
        if facade.execution_enabled and facade.production_execution_allowed:
            return CHECK_FAIL
        if not facade.execution_enabled:
            return CHECK_BLOCKED
        return CHECK_FAIL
    return CHECK_BLOCKED
