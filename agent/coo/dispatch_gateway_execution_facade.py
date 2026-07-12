"""Gateway execution facade — Phase 13H scaffold / 13I mock dispatch.

Single entry point for mock-only gateway dispatch via injected runners.
No real subprocess, bounded runner factory, or Repository2 execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.coo.dispatch_gateway_enablement import (
    GATEWAY_STATE_DISABLED,
    GATEWAY_STATE_ENABLED,
    GATEWAY_STATE_STAGED,
    load_dispatch_gateway_enablement,
)
from agent.coo.dispatch_gateway_request_store import (
    CooDispatchGatewayRequestRecord,
    DispatchGatewayRequestStoreError,
    REQUEST_STATUS_BLOCKED,
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_PREPARED,
    normalize_gateway_request_id,
    read_gateway_request,
    reserve_gateway_request,
    transition_gateway_request,
)
from agent.coo.dispatch_pipeline_root_trust import assert_pipeline_root_allowed
from agent.coo.dispatch_runner_binding_state import (
    RUNNER_BINDING_STATE_BOUND,
    CooDispatchRunnerBindingState,
    load_dispatch_runner_binding_state,
)

GATEWAY_EXECUTION_FACADE_CONNECTED = True
GATEWAY_EXECUTION_FACADE_VERSION = "13I-mock"
GATEWAY_EXECUTION_ISOLATED_SUPPORTED = True

GATEWAY_EXECUTION_SCOPE_ISOLATED_MOCK = "isolated_mock"

RECOMMENDED_NEXT_PHASE_SCAFFOLD = "Phase 13I Mock Gateway Dispatch"
RECOMMENDED_NEXT_PHASE_MOCK_READY = "Phase 13J Gateway Pilot Dispatch"
RECOMMENDED_NEXT_PHASE_NOT_READY = (
    "Resolve gateway execution facade validation before mock dispatch."
)

RECOMMENDED_ACTION_STAGE_GATEWAY = "stage_gateway_for_mock_dispatch"
RECOMMENDED_ACTION_ENABLE_MOCK_OPT_IN = "enable_mock_gateway_dispatch_opt_in"
RECOMMENDED_ACTION_RESOLVE_FAILURE = "resolve_gateway_dispatch_failure"
RECOMMENDED_ACTION_RETRY_WITH_NEW_REQUEST_ID = "retry_with_new_gateway_request_id"

FAILURE_NONE = "none"
FAILURE_GATEWAY_DISABLED = "gateway_disabled"
FAILURE_GATEWAY_ENABLED_NOT_SUPPORTED = "enabled_state_not_supported_for_mock_gateway_dispatch"
FAILURE_MOCK_DISPATCH_NOT_ALLOWED = "mock_dispatch_not_allowed"
FAILURE_INJECTED_RUNNER_MISSING = "injected_runner_missing"
FAILURE_INJECTED_RUNNER_NOT_CALLABLE = "injected_runner_not_callable"
FAILURE_BINDING_NOT_BOUND = "binding_not_bound"
FAILURE_EXECUTOR_DISABLED = "executor_disabled"
FAILURE_PRODUCTION_ROOT_DENIED = "production_root_denied"
FAILURE_OPERATOR_READINESS_FAILED = "operator_readiness_failed"
FAILURE_REGRESSION_BLOCKED = "regression_blocked"
FAILURE_SIGNOFF_NOT_READY = "signoff_not_ready"
FAILURE_DUPLICATE_GATEWAY_REQUEST_ID = "duplicate_gateway_request_id"
FAILURE_GATEWAY_REQUEST_IN_PROGRESS = "gateway_request_in_progress"
FAILURE_GATEWAY_REQUEST_ALREADY_COMPLETED = "gateway_request_already_completed"
FAILURE_GATEWAY_REQUEST_REQUIRES_NEW_ID = "gateway_request_requires_new_request_id"
FAILURE_MALFORMED_GATEWAY_REQUEST_ID = "malformed_gateway_request_id"
FAILURE_REQUEST_PERSISTENCE_FAILED = "request_persistence_failed"
FAILURE_CORRELATION_MISMATCH = "correlation_mismatch"
FAILURE_DISPATCH_FAILED = "dispatch_failed"
FAILURE_FACADE_NOT_CONNECTED = "facade_not_connected"
FAILURE_FACADE_INVALID = "facade_invalid"

RESULT_STATUS_BLOCKED = "blocked"
RESULT_STATUS_FAILED = "failed"
RESULT_STATUS_COMPLETED = "completed"
RESULT_STATUS_ALREADY_COMPLETED = "already_completed"
RESULT_STATUS_IN_PROGRESS = "in_progress"

_FORBIDDEN_EXECUTE_KWARGS = frozenset(
    {
        "subprocess_runner",
        "node_path",
        "use_runner_provider",
        "use_real_bounded_runner",
        "harness_profile",
        "node_executable",
        "harness_max_output_bytes",
        "harness_max_timeout_seconds",
    }
)


class GatewayExecutionFacadeError(ValueError):
    """Raised when gateway execution facade state is invalid."""


class GatewayExecutionNotEnabled(RuntimeError):
    """Raised when gateway dispatch execution is not enabled."""


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


@dataclass(frozen=True)
class CooDispatchGatewayDispatchResult:
    """Safe gateway mock dispatch result."""

    gateway_request_id: str
    accepted: bool
    status: str
    dry_run: bool
    failure_reason_code: str
    execution_attempt_id: str = ""
    dispatch_run_id: str = ""
    consumed: bool = False
    production_execution_allowed: bool = False
    gateway_execution_scope: str = GATEWAY_EXECUTION_SCOPE_ISOLATED_MOCK
    gateway_state: str = GATEWAY_STATE_DISABLED
    recommended_action: str = RECOMMENDED_ACTION_RESOLVE_FAILURE


def _read_facade_connected_marker() -> bool:
    return GATEWAY_EXECUTION_FACADE_CONNECTED is True


def _mock_execution_capable(gateway_state: str) -> bool:
    return (
        _read_facade_connected_marker()
        and GATEWAY_EXECUTION_ISOLATED_SUPPORTED
        and gateway_state == GATEWAY_STATE_STAGED
    )


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

    execution_enabled = _mock_execution_capable(enablement.gateway_state)
    facade = CooDispatchGatewayExecutionFacade(
        facade_connected=True,
        execution_enabled=execution_enabled,
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
    if facade.gateway_state == GATEWAY_STATE_ENABLED:
        return RECOMMENDED_NEXT_PHASE_NOT_READY
    if facade.execution_enabled:
        return RECOMMENDED_NEXT_PHASE_MOCK_READY
    return RECOMMENDED_NEXT_PHASE_SCAFFOLD


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


def format_gateway_dispatch_result(
    result: CooDispatchGatewayDispatchResult,
) -> str:
    """Format safe gateway dispatch result fields for operator review."""
    lines = [
        "Gateway Mock Dispatch Result",
        "",
        f"gateway_request_id: {result.gateway_request_id}",
        f"accepted: {str(result.accepted).lower()}",
        f"status: {result.status}",
        f"dry_run: {str(result.dry_run).lower()}",
        f"execution_attempt_id: {result.execution_attempt_id or '(none)'}",
        f"dispatch_run_id: {result.dispatch_run_id or '(none)'}",
        f"consumed: {str(result.consumed).lower()}",
        f"failure_reason_code: {result.failure_reason_code}",
        (
            "production_execution_allowed: "
            f"{str(result.production_execution_allowed).lower()}"
        ),
        f"gateway_execution_scope: {result.gateway_execution_scope}",
        f"gateway_state: {result.gateway_state}",
        f"recommended_action: {result.recommended_action}",
    ]
    return "\n".join(lines)


def _result_from_existing_record(
    record: CooDispatchGatewayRequestRecord,
    *,
    status: str,
    failure_reason_code: str,
    recommended_action: str,
    accepted: bool = False,
) -> CooDispatchGatewayDispatchResult:
    return CooDispatchGatewayDispatchResult(
        gateway_request_id=record.gateway_request_id,
        accepted=accepted,
        status=status,
        dry_run=record.dry_run,
        execution_attempt_id=record.execution_attempt_id,
        dispatch_run_id=record.dispatch_run_id,
        consumed=record.status == REQUEST_STATUS_COMPLETED and not record.dry_run,
        failure_reason_code=failure_reason_code,
        gateway_state=record.gateway_state,
        recommended_action=recommended_action,
    )


def _blocked_result(
    *,
    gateway_request_id: str,
    gateway_state: str,
    dry_run: bool,
    failure_reason_code: str,
    recommended_action: str = RECOMMENDED_ACTION_RESOLVE_FAILURE,
    status: str = RESULT_STATUS_BLOCKED,
) -> CooDispatchGatewayDispatchResult:
    return CooDispatchGatewayDispatchResult(
        gateway_request_id=gateway_request_id,
        accepted=False,
        status=status,
        dry_run=dry_run,
        failure_reason_code=failure_reason_code,
        gateway_state=gateway_state,
        recommended_action=recommended_action,
    )


def _resolve_binding_state(
    binding_state: CooDispatchRunnerBindingState | Mapping[str, Any] | None,
) -> CooDispatchRunnerBindingState:
    if binding_state is None:
        return load_dispatch_runner_binding_state()
    if isinstance(binding_state, CooDispatchRunnerBindingState):
        return binding_state
    if isinstance(binding_state, Mapping):
        state = str(binding_state.get("state", "")).strip()
        return CooDispatchRunnerBindingState(state=state, state_valid=bool(state))
    raise GatewayExecutionFacadeError("binding_state must be a mapping or dataclass.")


def _dispatch_run_id_for_attempt(execution_attempt_id: str) -> str:
    if not execution_attempt_id:
        return ""
    from agent.coo.dispatch_cli_pilot_history import _dispatch_run_id_for_attempt

    return _dispatch_run_id_for_attempt(execution_attempt_id)


def execute_gateway_dispatch(
    *,
    ticket_id: str,
    confirmation_id: str,
    unlock_token_id: str,
    requester_id: str,
    pipeline_root: str,
    gateway_request_id: str,
    merged_config: Mapping[str, Any] | None = None,
    binding_state: CooDispatchRunnerBindingState | Mapping[str, Any] | None = None,
    injected_runner: Callable[..., Any] | None = None,
    dry_run: bool = False,
    allow_mock_gateway_dispatch: bool = False,
    request_dir=None,
    **kwargs: Any,
) -> CooDispatchGatewayDispatchResult:
    """Mock-only gateway dispatch via injected runner and file-backed dispatch core."""
    if kwargs:
        forbidden = sorted(set(kwargs) & _FORBIDDEN_EXECUTE_KWARGS)
        if forbidden:
            joined = ", ".join(forbidden)
            raise GatewayExecutionFacadeError(
                f"Forbidden gateway dispatch parameters: {joined}"
            )

    if merged_config is None:
        merged_config = {}

    facade = load_gateway_execution_facade(merged_config=merged_config)
    gateway_state = facade.gateway_state

    try:
        normalized_request_id = normalize_gateway_request_id(gateway_request_id)
    except DispatchGatewayRequestStoreError:
        return _blocked_result(
            gateway_request_id=gateway_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_MALFORMED_GATEWAY_REQUEST_ID,
        )

    existing = read_gateway_request(normalized_request_id, request_dir=request_dir)
    if existing is not None:
        if existing.status == REQUEST_STATUS_COMPLETED:
            return _result_from_existing_record(
                existing,
                status=RESULT_STATUS_ALREADY_COMPLETED,
                failure_reason_code=FAILURE_GATEWAY_REQUEST_ALREADY_COMPLETED,
                recommended_action=RECOMMENDED_ACTION_RETRY_WITH_NEW_REQUEST_ID,
            )
        if existing.status == REQUEST_STATUS_PREPARED:
            return _blocked_result(
                gateway_request_id=normalized_request_id,
                gateway_state=gateway_state,
                dry_run=dry_run,
                failure_reason_code=FAILURE_GATEWAY_REQUEST_IN_PROGRESS,
                status=RESULT_STATUS_IN_PROGRESS,
            )
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_GATEWAY_REQUEST_REQUIRES_NEW_ID,
            recommended_action=RECOMMENDED_ACTION_RETRY_WITH_NEW_REQUEST_ID,
        )

    if not facade.valid or not facade.facade_connected:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_FACADE_INVALID,
        )

    if gateway_state == GATEWAY_STATE_DISABLED:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_GATEWAY_DISABLED,
            recommended_action=RECOMMENDED_ACTION_STAGE_GATEWAY,
        )

    if gateway_state == GATEWAY_STATE_ENABLED:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_GATEWAY_ENABLED_NOT_SUPPORTED,
        )

    if not allow_mock_gateway_dispatch:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_MOCK_DISPATCH_NOT_ALLOWED,
            recommended_action=RECOMMENDED_ACTION_ENABLE_MOCK_OPT_IN,
        )

    if not dry_run:
        if injected_runner is None:
            return _blocked_result(
                gateway_request_id=normalized_request_id,
                gateway_state=gateway_state,
                dry_run=False,
                failure_reason_code=FAILURE_INJECTED_RUNNER_MISSING,
            )
        if not callable(injected_runner):
            return _blocked_result(
                gateway_request_id=normalized_request_id,
                gateway_state=gateway_state,
                dry_run=False,
                failure_reason_code=FAILURE_INJECTED_RUNNER_NOT_CALLABLE,
            )

    try:
        assert_pipeline_root_allowed(pipeline_root)
    except ValueError:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_PRODUCTION_ROOT_DENIED,
        )

    binding = _resolve_binding_state(binding_state)
    if binding.state != RUNNER_BINDING_STATE_BOUND:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_BINDING_NOT_BOUND,
        )

    from agent.coo.dispatch_executor_config import load_dispatch_executor_policy

    policy = load_dispatch_executor_policy(merged_config=merged_config)
    if not policy.enabled:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_EXECUTOR_DISABLED,
        )

    from agent.coo.dispatch_cli_readiness import evaluate_dispatch_operator_readiness

    readiness = evaluate_dispatch_operator_readiness(
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        pipeline_root=pipeline_root,
        merged_config=merged_config,
    )
    if not readiness.ready:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_OPERATOR_READINESS_FAILED,
        )

    from agent.coo.dispatch_cli_pilot_regression_gate import (
        evaluate_pilot_regression_gate,
    )

    regression = evaluate_pilot_regression_gate(
        ticket_id=ticket_id,
        dry_run=dry_run,
    )
    if not dry_run and not regression.live_pilot_allowed:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_REGRESSION_BLOCKED,
        )

    from agent.coo.dispatch_cli_production_signoff import (
        evaluate_dispatch_production_signoff,
    )

    signoff = evaluate_dispatch_production_signoff(merged_config=merged_config)
    if not signoff.signoff_ready:
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_SIGNOFF_NOT_READY,
        )

    prepared_record = CooDispatchGatewayRequestRecord(
        gateway_request_id=normalized_request_id,
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        execution_attempt_id="",
        dispatch_run_id="",
        status=REQUEST_STATUS_PREPARED,
        dry_run=dry_run,
        failure_reason_code=FAILURE_NONE,
        production_execution_allowed=False,
        gateway_state=GATEWAY_STATE_STAGED,
    )
    try:
        reserve_gateway_request(prepared_record, request_dir=request_dir)
    except DispatchGatewayRequestStoreError as exc:
        message = str(exc).lower()
        if "already exists" in message:
            if "prepared" in message:
                return _blocked_result(
                    gateway_request_id=normalized_request_id,
                    gateway_state=gateway_state,
                    dry_run=dry_run,
                    failure_reason_code=FAILURE_GATEWAY_REQUEST_IN_PROGRESS,
                    status=RESULT_STATUS_IN_PROGRESS,
                )
            return _blocked_result(
                gateway_request_id=normalized_request_id,
                gateway_state=gateway_state,
                dry_run=dry_run,
                failure_reason_code=FAILURE_DUPLICATE_GATEWAY_REQUEST_ID,
            )
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_REQUEST_PERSISTENCE_FAILED,
        )

    from agent.coo.dispatch_cli_run import execute_coo_dispatch_run

    run_error = ""
    run_result = None
    try:
        run_result = execute_coo_dispatch_run(
            ticket_id=ticket_id,
            confirmation_id=confirmation_id,
            unlock_token_id=unlock_token_id,
            requester_id=requester_id,
            pipeline_root=pipeline_root,
            dry_run=dry_run,
            subprocess_runner=injected_runner,
            merged_config=merged_config,
        )
    except ValueError as exc:
        run_error = str(exc)

    if run_result is None:
        try:
            transition_gateway_request(
                normalized_request_id,
                status=REQUEST_STATUS_FAILED,
                failure_reason_code=FAILURE_DISPATCH_FAILED,
                request_dir=request_dir,
            )
        except DispatchGatewayRequestStoreError:
            return _blocked_result(
                gateway_request_id=normalized_request_id,
                gateway_state=gateway_state,
                dry_run=dry_run,
                failure_reason_code=FAILURE_REQUEST_PERSISTENCE_FAILED,
            )
        return CooDispatchGatewayDispatchResult(
            gateway_request_id=normalized_request_id,
            accepted=False,
            status=RESULT_STATUS_FAILED,
            dry_run=dry_run,
            failure_reason_code=FAILURE_DISPATCH_FAILED,
            gateway_state=gateway_state,
            recommended_action=RECOMMENDED_ACTION_RESOLVE_FAILURE,
        )

    execution_attempt_id = run_result.execution_attempt_id
    dispatch_run_id = _dispatch_run_id_for_attempt(execution_attempt_id)
    if (
        run_result.ticket_id != ticket_id
        or run_result.confirmation_id != confirmation_id
    ):
        try:
            transition_gateway_request(
                normalized_request_id,
                status=REQUEST_STATUS_FAILED,
                execution_attempt_id=execution_attempt_id,
                dispatch_run_id=dispatch_run_id,
                failure_reason_code=FAILURE_CORRELATION_MISMATCH,
                request_dir=request_dir,
            )
        except DispatchGatewayRequestStoreError:
            pass
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=dry_run,
            failure_reason_code=FAILURE_CORRELATION_MISMATCH,
            status=RESULT_STATUS_FAILED,
        )

    dispatch_succeeded = (
        run_result.status == "preflight_passed"
        if dry_run
        else run_result.consumed and run_result.status == "completed"
    )
    if not dry_run and run_result.status == "completed" and not dispatch_run_id:
        try:
            transition_gateway_request(
                normalized_request_id,
                status=REQUEST_STATUS_FAILED,
                execution_attempt_id=execution_attempt_id,
                failure_reason_code=FAILURE_CORRELATION_MISMATCH,
                request_dir=request_dir,
            )
        except DispatchGatewayRequestStoreError:
            pass
        return _blocked_result(
            gateway_request_id=normalized_request_id,
            gateway_state=gateway_state,
            dry_run=False,
            failure_reason_code=FAILURE_CORRELATION_MISMATCH,
            status=RESULT_STATUS_FAILED,
        )

    terminal_status = REQUEST_STATUS_COMPLETED if dispatch_succeeded else REQUEST_STATUS_FAILED
    failure_reason = FAILURE_NONE if dispatch_succeeded else FAILURE_DISPATCH_FAILED
    try:
        transition_gateway_request(
            normalized_request_id,
            status=terminal_status,
            execution_attempt_id=execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            failure_reason_code=failure_reason,
            request_dir=request_dir,
        )
    except DispatchGatewayRequestStoreError:
        return CooDispatchGatewayDispatchResult(
            gateway_request_id=normalized_request_id,
            accepted=False,
            status=RESULT_STATUS_FAILED,
            dry_run=dry_run,
            execution_attempt_id=execution_attempt_id,
            dispatch_run_id=dispatch_run_id,
            consumed=run_result.consumed,
            failure_reason_code=FAILURE_REQUEST_PERSISTENCE_FAILED,
            gateway_state=gateway_state,
            recommended_action=RECOMMENDED_ACTION_RESOLVE_FAILURE,
        )

    return CooDispatchGatewayDispatchResult(
        gateway_request_id=normalized_request_id,
        accepted=dispatch_succeeded,
        status=RESULT_STATUS_COMPLETED if dispatch_succeeded else RESULT_STATUS_FAILED,
        dry_run=dry_run,
        execution_attempt_id=execution_attempt_id,
        dispatch_run_id=dispatch_run_id,
        consumed=run_result.consumed,
        failure_reason_code=failure_reason,
        gateway_state=gateway_state,
        recommended_action=(
            RECOMMENDED_ACTION_RESOLVE_FAILURE
            if not dispatch_succeeded
            else RECOMMENDED_ACTION_ENABLE_MOCK_OPT_IN
        ),
    )
