"""Governed runtime invoke contract — Phase 15I.

Orchestrates the terminal "invoke" transition of the governed runtime
chain (Phase 15C permission -> 15D session -> 15E boundary -> 15F
invocation -> 15G authorization -> 15H runtime start -> 15I invoke) by
calling each owner module's own ``consume_*()`` function in a fixed
order. This module never mutates another module's internal state
directly, never creates a bounded subprocess runner, never calls
``subprocess``/``node``/``npm``/``npx``/``pipeline.js``, and never wires
Gateway, Discord, or external publish. Real Repository2 execution remains
strictly out of scope for this phase.

Invariants enforced everywhere in this module:
    - production_execution_allowed is always False in every output.
    - original_repository2_execution_attempted is always False.
    - gateway_production_enabled / discord_production_enabled /
      external_publish_enabled are always False.
    - governed_runtime_invoked (the flag this module owns) is a
      deliberately new name, distinct from Phase 14's
      ``runtime_invoked``/``isolated_mirror_runtime_invoked`` on
      ``production_activation_live_runtime.py`` (which reflects whether an
      isolated /tmp mirror bounded subprocess actually ran). The two must
      never be confused: this module never runs any subprocess at all.
    - This module owns exactly one transition: its own
      ``governed_runtime_invoked`` record. It does not own, and never
      writes, permission/boundary/invocation/authorization consumption —
      those remain owned by their respective modules; this module only
      calls their public ``consume_*()`` functions in sequence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.production_execution_authorization import (
    ProductionExecutionAuthorizationError,
    consume_execution_authorization,
    load_execution_authorization_record,
)
from agent.coo.production_runtime_boundary import (
    RuntimeBoundaryError,
    consume_runtime_boundary,
    load_runtime_boundary_record,
)
from agent.coo.production_runtime_invocation import (
    ProductionRuntimeInvocationError,
    consume_runtime_invocation,
    load_runtime_invocation_record,
)
from agent.coo.production_runtime_permission import (
    ProductionRuntimePermissionError,
    consume_production_runtime_permission,
    load_runtime_permission_record,
)
from agent.coo.production_runtime_start import (
    BLOCK_RUNTIME_START_ALREADY_STARTED,
    RUNTIME_START_STARTED,
    ProductionRuntimeStartError,
    evaluate_production_runtime_start,
    load_runtime_start_record,
)

# The only blocking marker evaluate_production_runtime_start() is expected to
# report once a runtime-start contract already exists — every other marker
# indicates something in the upstream chain regressed after runtime-start was
# created (e.g. a permission got revoked) and must still block Phase 15I.
_ACCEPTABLE_POST_START_BLOCKS = frozenset({BLOCK_RUNTIME_START_ALREADY_STARTED})
from agent.coo.production_runtime_consume_store import (
    OneShotConsumeWriteConflict,
    read_consume_record,
    write_once_consume_record,
)
from hermes_constants import get_hermes_home

_INVOKE_STORE_DIR = "production-governed-runtime-invoke"
_INVOKE_STORE_VERSION = 1

GOVERNED_RUNTIME_INVOKE_NOT_READY = "GOVERNED_RUNTIME_INVOKE_NOT_READY"
GOVERNED_RUNTIME_INVOKE_READY = "GOVERNED_RUNTIME_INVOKE_READY"
GOVERNED_RUNTIME_INVOKE_COMPLETED = "GOVERNED_RUNTIME_INVOKE_COMPLETED"
GOVERNED_RUNTIME_INVOKE_FAILED = "GOVERNED_RUNTIME_INVOKE_FAILED"

BLOCK_RUNTIME_START_MISSING = "runtime_start_missing"
BLOCK_RUNTIME_START_NOT_READY = "runtime_start_not_ready"
BLOCK_RUNTIME_START_EXPIRED = "runtime_start_expired"
BLOCK_RUNTIME_START_SCOPE_MISMATCH = "runtime_start_scope_mismatch"
BLOCK_PERMISSION_ALREADY_CONSUMED = "permission_already_consumed"
BLOCK_BOUNDARY_ALREADY_CONSUMED = "boundary_already_consumed"
BLOCK_INVOCATION_ALREADY_CONSUMED = "invocation_already_consumed"
BLOCK_AUTHORIZATION_ALREADY_CONSUMED = "authorization_already_consumed"
BLOCK_ALREADY_INVOKED = "governed_runtime_already_invoked"
BLOCK_EXECUTOR_MISMATCH = "governed_runtime_invoke_executor_mismatch"
BLOCK_OPERATOR_MISMATCH = "governed_runtime_invoke_operator_mismatch"
BLOCK_PRODUCTION_EXECUTION_ENABLED = "production_execution_enabled"


class GovernedRuntimeInvokeError(ValueError):
    """Raised when the governed runtime invoke contract cannot proceed safely."""


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def default_governed_runtime_invoke_store_dir() -> Path:
    return get_hermes_home() / "coo" / _INVOKE_STORE_DIR


def _invoke_path(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> Path:
    normalized = (activation_request_id or "").strip()
    if not normalized:
        raise GovernedRuntimeInvokeError("activation_request_id is required")
    base = store_dir or default_governed_runtime_invoke_store_dir()
    return base / f"{normalized}.json"


@dataclass(frozen=True)
class GovernedRuntimeInvokeRecord:
    """Terminal record of a governed runtime invoke attempt.

    This is the ONLY place ``governed_runtime_invoked`` is ever set True,
    and only after all four upstream consume calls succeed. It never
    reflects, and must never be confused with, Phase 14's
    ``runtime_invoked``/``isolated_mirror_runtime_invoked``.
    """

    invoke_id: str
    activation_request_id: str
    runtime_start_id: str
    authorization_id: str
    runtime_invocation_id: str
    boundary_id: str
    session_id: str
    permission_id: str
    cutover_contract_id: str
    ticket_id: str
    confirmation_id: str
    executor_id: str
    operator_id: str
    invoked_by: str
    invoked_at: str
    status: str
    failure_reason_code: str
    permission_consumed: bool
    boundary_consumed: bool
    invocation_consumed: bool
    authorization_consumed: bool
    governed_runtime_invoked: bool
    tested_commit_sha: str
    release_tag: str
    production_execution_allowed: bool = False
    production_root_hard_deny: bool = True
    original_repository2_execution_attempted: bool = False
    gateway_production_enabled: bool = False
    discord_production_enabled: bool = False
    external_publish_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoke_id": self.invoke_id,
            "activation_request_id": self.activation_request_id,
            "runtime_start_id": self.runtime_start_id,
            "authorization_id": self.authorization_id,
            "runtime_invocation_id": self.runtime_invocation_id,
            "boundary_id": self.boundary_id,
            "session_id": self.session_id,
            "permission_id": self.permission_id,
            "cutover_contract_id": self.cutover_contract_id,
            "ticket_id": self.ticket_id,
            "confirmation_id": self.confirmation_id,
            "executor_id": self.executor_id,
            "operator_id": self.operator_id,
            "invoked_by": self.invoked_by,
            "invoked_at": self.invoked_at,
            "status": self.status,
            "failure_reason_code": self.failure_reason_code,
            "permission_consumed": self.permission_consumed,
            "boundary_consumed": self.boundary_consumed,
            "invocation_consumed": self.invocation_consumed,
            "authorization_consumed": self.authorization_consumed,
            "governed_runtime_invoked": self.governed_runtime_invoked,
            "tested_commit_sha": self.tested_commit_sha,
            "release_tag": self.release_tag,
            "production_execution_allowed": False,
            "production_root_hard_deny": True,
            "original_repository2_execution_attempted": False,
            "gateway_production_enabled": False,
            "discord_production_enabled": False,
            "external_publish_enabled": False,
        }


def load_governed_runtime_invoke_record(
    activation_request_id: str,
    *,
    store_dir: Path | None = None,
) -> GovernedRuntimeInvokeRecord | None:
    path = _invoke_path(activation_request_id, store_dir=store_dir)
    payload = read_consume_record(path)
    if payload is None:
        return None
    return GovernedRuntimeInvokeRecord(
        invoke_id=str(payload.get("invoke_id", "")),
        activation_request_id=str(payload.get("activation_request_id", "")),
        runtime_start_id=str(payload.get("runtime_start_id", "")),
        authorization_id=str(payload.get("authorization_id", "")),
        runtime_invocation_id=str(payload.get("runtime_invocation_id", "")),
        boundary_id=str(payload.get("boundary_id", "")),
        session_id=str(payload.get("session_id", "")),
        permission_id=str(payload.get("permission_id", "")),
        cutover_contract_id=str(payload.get("cutover_contract_id", "")),
        ticket_id=str(payload.get("ticket_id", "")),
        confirmation_id=str(payload.get("confirmation_id", "")),
        executor_id=str(payload.get("executor_id", "")),
        operator_id=str(payload.get("operator_id", "")),
        invoked_by=str(payload.get("invoked_by", "")),
        invoked_at=str(payload.get("invoked_at", "")),
        status=str(payload.get("status", "")),
        failure_reason_code=str(payload.get("failure_reason_code", "")),
        permission_consumed=bool(payload.get("permission_consumed", False)),
        boundary_consumed=bool(payload.get("boundary_consumed", False)),
        invocation_consumed=bool(payload.get("invocation_consumed", False)),
        authorization_consumed=bool(payload.get("authorization_consumed", False)),
        governed_runtime_invoked=bool(payload.get("governed_runtime_invoked", False)),
        tested_commit_sha=str(payload.get("tested_commit_sha", "")),
        release_tag=str(payload.get("release_tag", "")),
    )


@dataclass(frozen=True)
class GovernedRuntimeInvokeSummary:
    """Read-only readiness assessment for the governed runtime invoke step."""

    activation_request_id: str
    runtime_start_id: str
    runtime_start_valid: bool
    already_invoked: bool
    invoke_ready: bool
    invoke_state: str
    blocking_items: tuple[str, ...]
    warning_items: tuple[str, ...]
    recommended_action: str


def evaluate_governed_runtime_invoke(
    *,
    activation_request_id: str,
    authorization_id: str = "",
    executor_id: str = "",
    operator_id: str = "",
    supervisor_id: str = "",
    invoke_store_dir: Path | None = None,
    runtime_start_store_dir: Path | None = None,
    authorization_store_dir: Path | None = None,
    invocation_store_dir: Path | None = None,
    invocation_consume_store_dir: Path | None = None,
    boundary_store_dir: Path | None = None,
    boundary_consume_store_dir: Path | None = None,
    session_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
    permission_consume_store_dir: Path | None = None,
    authorization_consume_store_dir: Path | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    runtime_history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    e2e_history_dir: Path | None = None,
    signoff_store_dir: Path | None = None,
    validation_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    preflight_history_dir: Path | None = None,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    force_production_execution_allowed: bool | None = None,
    force_gateway_enabled: bool | None = None,
    force_discord_enabled: bool | None = None,
) -> GovernedRuntimeInvokeSummary:
    """Read-only assessment of whether a governed runtime invoke may proceed."""
    blocking: list[str] = []
    warnings: list[str] = []

    try:
        start_summary = evaluate_production_runtime_start(
            activation_request_id=activation_request_id,
            authorization_id=authorization_id,
            executor_id=executor_id,
            operator_id=operator_id,
            supervisor_id=supervisor_id,
            runtime_start_store_dir=runtime_start_store_dir,
            authorization_store_dir=authorization_store_dir,
            invocation_store_dir=invocation_store_dir,
            invocation_consume_store_dir=invocation_consume_store_dir,
            boundary_store_dir=boundary_store_dir,
            boundary_consume_store_dir=boundary_consume_store_dir,
            session_store_dir=session_store_dir,
            permission_store_dir=permission_store_dir,
            store_dir=store_dir,
            reservation_dir=reservation_dir,
            runtime_history_dir=runtime_history_dir,
            evidence_dir=evidence_dir,
            audit_dir=audit_dir,
            bundle_dir=bundle_dir,
            confirmation_dir=confirmation_dir,
            transaction_dir=transaction_dir,
            e2e_history_dir=e2e_history_dir,
            signoff_store_dir=signoff_store_dir,
            validation_store_dir=validation_store_dir,
            final_signoff_store_dir=final_signoff_store_dir,
            preflight_history_dir=preflight_history_dir,
            governed_cutover_store_dir=governed_cutover_store_dir,
            window_store_dir=window_store_dir,
            repo_root=repo_root,
            merged_config=merged_config,
            now=now,
            force_production_execution_allowed=force_production_execution_allowed,
            force_gateway_enabled=force_gateway_enabled,
            force_discord_enabled=force_discord_enabled,
        )
    except ProductionRuntimeStartError:
        start_summary = None
        blocking.append(BLOCK_RUNTIME_START_NOT_READY)

    runtime_start_record = load_runtime_start_record(
        activation_request_id, store_dir=runtime_start_store_dir
    )

    # Resolve already_invoked before evaluating runtime_start "not ready" /
    # "already consumed" markers below: once this activation's governed
    # invoke has already completed successfully, the very consumption it
    # performed (permission/boundary/invocation/authorization) is expected
    # to make evaluate_production_runtime_start() and the re-verification
    # block below report additional markers — those are confirmation of
    # success, not new problems, and must not be surfaced as blocking
    # alongside (or worse, instead of) BLOCK_ALREADY_INVOKED.
    already_invoked = False
    existing_invoke = load_governed_runtime_invoke_record(
        activation_request_id, store_dir=invoke_store_dir
    )
    if existing_invoke is not None and existing_invoke.governed_runtime_invoked:
        already_invoked = True

    runtime_start_valid = False
    current = _utc_now(now)
    if runtime_start_record is None:
        blocking.append(BLOCK_RUNTIME_START_MISSING)
    elif already_invoked:
        runtime_start_valid = True
        if authorization_id and runtime_start_record.authorization_id != authorization_id.strip():
            blocking.append(BLOCK_RUNTIME_START_SCOPE_MISMATCH)
        if executor_id and runtime_start_record.executor_id != executor_id.strip():
            blocking.append(BLOCK_EXECUTOR_MISMATCH)
        if operator_id and runtime_start_record.operator_id != operator_id.strip():
            blocking.append(BLOCK_OPERATOR_MISMATCH)
    else:
        # A runtime-start contract must already exist (RUNTIME_START_STARTED)
        # for Phase 15I to proceed — RUNTIME_START_READY means "not yet
        # started". Once started, evaluate_production_runtime_start() always
        # reports BLOCK_RUNTIME_START_ALREADY_STARTED on re-evaluation; that
        # single marker is expected and must not block Phase 15I. Any other
        # marker means something upstream regressed after start and must
        # still block.
        start_state_ok = (
            start_summary is not None
            and start_summary.runtime_start_state == RUNTIME_START_STARTED
            and set(start_summary.blocking_items) <= _ACCEPTABLE_POST_START_BLOCKS
        )
        if not start_state_ok:
            blocking.append(BLOCK_RUNTIME_START_NOT_READY)
        else:
            expires_dt = None
            try:
                from agent.coo.production_runtime_start import _parse_iso as _rs_parse_iso

                expires_dt = _rs_parse_iso(runtime_start_record.expires_at)
            except Exception:
                expires_dt = None
            if expires_dt is not None and current >= expires_dt:
                blocking.append(BLOCK_RUNTIME_START_EXPIRED)
            else:
                runtime_start_valid = True
        if authorization_id and runtime_start_record.authorization_id != authorization_id.strip():
            blocking.append(BLOCK_RUNTIME_START_SCOPE_MISMATCH)
        if executor_id and runtime_start_record.executor_id != executor_id.strip():
            blocking.append(BLOCK_EXECUTOR_MISMATCH)
        if operator_id and runtime_start_record.operator_id != operator_id.strip():
            blocking.append(BLOCK_OPERATOR_MISMATCH)

    if already_invoked:
        blocking.append(BLOCK_ALREADY_INVOKED)

    # Independent re-verification: even though runtime_start already checks
    # boundary/invocation consumption, and permission/authorization
    # consumption was historically unchecked upstream, re-verify all four
    # directly against their own consume stores as defense in depth. Skipped
    # once already_invoked is known True — at that point every one of these
    # would fire (this invoke consumed them all) and add no new information
    # beyond BLOCK_ALREADY_INVOKED.
    if runtime_start_record is not None and not already_invoked:
        try:
            permission_record = load_runtime_permission_record(
                activation_request_id, store_dir=permission_store_dir
            )
            if permission_record is not None:
                from agent.coo.production_runtime_permission import (
                    load_runtime_permission_consume_record,
                )

                if (
                    load_runtime_permission_consume_record(
                        permission_record.permission_id,
                        store_dir=permission_consume_store_dir,
                    )
                    is not None
                ):
                    blocking.append(BLOCK_PERMISSION_ALREADY_CONSUMED)
        except ProductionRuntimePermissionError:
            blocking.append(BLOCK_PERMISSION_ALREADY_CONSUMED)

        try:
            boundary_record = load_runtime_boundary_record(
                activation_request_id, store_dir=boundary_store_dir
            )
            if boundary_record is not None:
                from agent.coo.production_runtime_boundary import (
                    load_runtime_boundary_consume_record,
                )

                if (
                    load_runtime_boundary_consume_record(
                        boundary_record.boundary_id,
                        store_dir=boundary_consume_store_dir,
                    )
                    is not None
                ):
                    blocking.append(BLOCK_BOUNDARY_ALREADY_CONSUMED)
        except RuntimeBoundaryError:
            blocking.append(BLOCK_BOUNDARY_ALREADY_CONSUMED)

        try:
            invocation_record = load_runtime_invocation_record(
                activation_request_id, store_dir=invocation_store_dir
            )
            if invocation_record is not None:
                from agent.coo.production_runtime_invocation import (
                    load_runtime_invocation_consume_record,
                )

                if (
                    load_runtime_invocation_consume_record(
                        invocation_record.runtime_invocation_id,
                        store_dir=invocation_consume_store_dir,
                    )
                    is not None
                ):
                    blocking.append(BLOCK_INVOCATION_ALREADY_CONSUMED)
        except ProductionRuntimeInvocationError:
            blocking.append(BLOCK_INVOCATION_ALREADY_CONSUMED)

        try:
            authorization_record = load_execution_authorization_record(
                activation_request_id, store_dir=authorization_store_dir
            )
            if authorization_record is not None:
                from agent.coo.production_execution_authorization import (
                    load_execution_authorization_consume_record,
                )

                if (
                    load_execution_authorization_consume_record(
                        authorization_record.authorization_id,
                        store_dir=authorization_consume_store_dir,
                    )
                    is not None
                ):
                    blocking.append(BLOCK_AUTHORIZATION_ALREADY_CONSUMED)
        except ProductionExecutionAuthorizationError:
            blocking.append(BLOCK_AUTHORIZATION_ALREADY_CONSUMED)

    production_execution_allowed = bool(force_production_execution_allowed)
    gateway_enabled = bool(force_gateway_enabled)
    discord_enabled = bool(force_discord_enabled)
    if production_execution_allowed:
        blocking.append(BLOCK_PRODUCTION_EXECUTION_ENABLED)
    if gateway_enabled or discord_enabled:
        blocking.append(BLOCK_PRODUCTION_EXECUTION_ENABLED)

    unique_blocking = tuple(dict.fromkeys(blocking))
    invoke_ready = runtime_start_valid and not unique_blocking

    if already_invoked:
        invoke_state = GOVERNED_RUNTIME_INVOKE_COMPLETED
    elif invoke_ready:
        invoke_state = GOVERNED_RUNTIME_INVOKE_READY
    else:
        invoke_state = GOVERNED_RUNTIME_INVOKE_NOT_READY

    recommended_action = "reserve_and_consume_governed_runtime_invoke" if invoke_ready else "resolve_blocking_items"
    if already_invoked:
        recommended_action = "governed_runtime_invoke_already_completed"

    return GovernedRuntimeInvokeSummary(
        activation_request_id=activation_request_id,
        runtime_start_id=runtime_start_record.runtime_start_id if runtime_start_record else "",
        runtime_start_valid=runtime_start_valid,
        already_invoked=already_invoked,
        invoke_ready=invoke_ready,
        invoke_state=invoke_state,
        blocking_items=unique_blocking,
        warning_items=tuple(warnings),
        recommended_action=recommended_action,
    )


def reserve_and_consume_governed_runtime_invoke(
    activation_request_id: str,
    *,
    invoked_by: str,
    authorization_id: str = "",
    executor_id: str = "",
    operator_id: str = "",
    supervisor_id: str = "",
    invoke_store_dir: Path | None = None,
    runtime_start_store_dir: Path | None = None,
    authorization_store_dir: Path | None = None,
    authorization_consume_store_dir: Path | None = None,
    invocation_store_dir: Path | None = None,
    invocation_consume_store_dir: Path | None = None,
    boundary_store_dir: Path | None = None,
    boundary_consume_store_dir: Path | None = None,
    session_store_dir: Path | None = None,
    permission_store_dir: Path | None = None,
    permission_consume_store_dir: Path | None = None,
    store_dir: Path | None = None,
    reservation_dir: Path | None = None,
    runtime_history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
    transaction_dir: Path | None = None,
    e2e_history_dir: Path | None = None,
    signoff_store_dir: Path | None = None,
    validation_store_dir: Path | None = None,
    final_signoff_store_dir: Path | None = None,
    preflight_history_dir: Path | None = None,
    governed_cutover_store_dir: Path | None = None,
    window_store_dir: Path | None = None,
    repo_root: Path | None = None,
    merged_config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> GovernedRuntimeInvokeRecord:
    """Orchestrate the governed runtime invoke: consume permission, boundary,
    invocation, and authorization in a fixed order, each via its own owner
    module's ``consume_*()`` function.

    This function NEVER creates a bounded subprocess runner and NEVER
    invokes ``subprocess``, ``node``, ``npm``, ``npx``, or
    ``pipeline.js``. It never wires Gateway, Discord, or external publish.
    It is orchestration/bookkeeping only — the real Repository2 execution
    step remains a distinct, later phase.

    On full success, writes a single write-once
    ``GovernedRuntimeInvokeRecord`` with ``governed_runtime_invoked=True``.
    On partial failure (some but not all of the four consume calls
    succeeded), writes a FAILED record capturing exactly which steps
    completed before re-raising, so operators can see precisely how far
    the attempt got. Never retries automatically and never mutates any of
    the four owner artifacts other than by calling their own consume
    functions.
    """
    normalized_invoked_by = (invoked_by or "").strip()
    if not normalized_invoked_by:
        raise GovernedRuntimeInvokeError("invoked_by is required")

    summary = evaluate_governed_runtime_invoke(
        activation_request_id=activation_request_id,
        authorization_id=authorization_id,
        executor_id=executor_id,
        operator_id=operator_id,
        supervisor_id=supervisor_id,
        invoke_store_dir=invoke_store_dir,
        runtime_start_store_dir=runtime_start_store_dir,
        authorization_store_dir=authorization_store_dir,
        authorization_consume_store_dir=authorization_consume_store_dir,
        invocation_store_dir=invocation_store_dir,
        invocation_consume_store_dir=invocation_consume_store_dir,
        boundary_store_dir=boundary_store_dir,
        boundary_consume_store_dir=boundary_consume_store_dir,
        session_store_dir=session_store_dir,
        permission_store_dir=permission_store_dir,
        permission_consume_store_dir=permission_consume_store_dir,
        store_dir=store_dir,
        reservation_dir=reservation_dir,
        runtime_history_dir=runtime_history_dir,
        evidence_dir=evidence_dir,
        audit_dir=audit_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        transaction_dir=transaction_dir,
        e2e_history_dir=e2e_history_dir,
        signoff_store_dir=signoff_store_dir,
        validation_store_dir=validation_store_dir,
        final_signoff_store_dir=final_signoff_store_dir,
        preflight_history_dir=preflight_history_dir,
        governed_cutover_store_dir=governed_cutover_store_dir,
        window_store_dir=window_store_dir,
        repo_root=repo_root,
        merged_config=merged_config,
        now=now,
    )
    if summary.already_invoked:
        raise GovernedRuntimeInvokeError("governed_runtime_already_invoked")
    if not summary.invoke_ready:
        raise GovernedRuntimeInvokeError(
            f"governed_runtime_invoke_not_ready:{','.join(summary.blocking_items)}"
        )

    runtime_start_record = load_runtime_start_record(
        activation_request_id, store_dir=runtime_start_store_dir
    )
    if runtime_start_record is None:
        raise GovernedRuntimeInvokeError("runtime_start_missing")

    permission_record = load_runtime_permission_record(
        activation_request_id, store_dir=permission_store_dir
    )
    boundary_record = load_runtime_boundary_record(
        activation_request_id, store_dir=boundary_store_dir
    )
    invocation_record = load_runtime_invocation_record(
        activation_request_id, store_dir=invocation_store_dir
    )
    authorization_record = load_execution_authorization_record(
        activation_request_id, store_dir=authorization_store_dir
    )
    if (
        permission_record is None
        or boundary_record is None
        or invocation_record is None
        or authorization_record is None
    ):
        raise GovernedRuntimeInvokeError("upstream_artifact_missing")

    invoke_id = str(uuid.uuid4())
    invoked_at = _utc_now_iso(now)

    def _write_failure(
        *,
        failure_reason_code: str,
        permission_consumed: bool,
        boundary_consumed: bool,
        invocation_consumed: bool,
        authorization_consumed: bool,
    ) -> None:
        record = GovernedRuntimeInvokeRecord(
            invoke_id=invoke_id,
            activation_request_id=activation_request_id,
            runtime_start_id=runtime_start_record.runtime_start_id,
            authorization_id=authorization_record.authorization_id,
            runtime_invocation_id=invocation_record.runtime_invocation_id,
            boundary_id=boundary_record.boundary_id,
            session_id=runtime_start_record.session_id,
            permission_id=permission_record.permission_id,
            cutover_contract_id=runtime_start_record.cutover_contract_id,
            ticket_id=runtime_start_record.ticket_id,
            confirmation_id=runtime_start_record.confirmation_id,
            executor_id=runtime_start_record.executor_id,
            operator_id=runtime_start_record.operator_id,
            invoked_by=normalized_invoked_by,
            invoked_at=invoked_at,
            status=GOVERNED_RUNTIME_INVOKE_FAILED,
            failure_reason_code=failure_reason_code,
            permission_consumed=permission_consumed,
            boundary_consumed=boundary_consumed,
            invocation_consumed=invocation_consumed,
            authorization_consumed=authorization_consumed,
            governed_runtime_invoked=False,
            tested_commit_sha=runtime_start_record.tested_commit_sha,
            release_tag=runtime_start_record.release_tag,
        )
        try:
            write_once_consume_record(
                _invoke_path(activation_request_id, store_dir=invoke_store_dir),
                record.to_dict(),
            )
        except OneShotConsumeWriteConflict:
            pass

    permission_consumed = False
    boundary_consumed = False
    invocation_consumed = False
    authorization_consumed = False

    try:
        consume_production_runtime_permission(
            activation_request_id,
            permission_id=permission_record.permission_id,
            consumed_by=normalized_invoked_by,
            governed_invoke_id=invoke_id,
            store_dir=permission_store_dir,
            consume_store_dir=permission_consume_store_dir,
            now=now,
        )
        permission_consumed = True

        consume_runtime_boundary(
            activation_request_id,
            boundary_id=boundary_record.boundary_id,
            consumed_by=normalized_invoked_by,
            governed_invoke_id=invoke_id,
            store_dir=boundary_store_dir,
            consume_store_dir=boundary_consume_store_dir,
            now=now,
        )
        boundary_consumed = True

        consume_runtime_invocation(
            activation_request_id,
            runtime_invocation_id=invocation_record.runtime_invocation_id,
            consumed_by=normalized_invoked_by,
            governed_invoke_id=invoke_id,
            store_dir=invocation_store_dir,
            consume_store_dir=invocation_consume_store_dir,
            now=now,
        )
        invocation_consumed = True

        consume_execution_authorization(
            activation_request_id,
            authorization_id=authorization_record.authorization_id,
            consumed_by=normalized_invoked_by,
            governed_invoke_id=invoke_id,
            store_dir=authorization_store_dir,
            consume_store_dir=authorization_consume_store_dir,
            now=now,
        )
        authorization_consumed = True
    except (
        ProductionRuntimePermissionError,
        RuntimeBoundaryError,
        ProductionRuntimeInvocationError,
        ProductionExecutionAuthorizationError,
    ) as exc:
        _write_failure(
            failure_reason_code=str(exc),
            permission_consumed=permission_consumed,
            boundary_consumed=boundary_consumed,
            invocation_consumed=invocation_consumed,
            authorization_consumed=authorization_consumed,
        )
        raise GovernedRuntimeInvokeError(str(exc)) from exc

    record = GovernedRuntimeInvokeRecord(
        invoke_id=invoke_id,
        activation_request_id=activation_request_id,
        runtime_start_id=runtime_start_record.runtime_start_id,
        authorization_id=authorization_record.authorization_id,
        runtime_invocation_id=invocation_record.runtime_invocation_id,
        boundary_id=boundary_record.boundary_id,
        session_id=runtime_start_record.session_id,
        permission_id=permission_record.permission_id,
        cutover_contract_id=runtime_start_record.cutover_contract_id,
        ticket_id=runtime_start_record.ticket_id,
        confirmation_id=runtime_start_record.confirmation_id,
        executor_id=runtime_start_record.executor_id,
        operator_id=runtime_start_record.operator_id,
        invoked_by=normalized_invoked_by,
        invoked_at=invoked_at,
        status=GOVERNED_RUNTIME_INVOKE_COMPLETED,
        failure_reason_code="",
        permission_consumed=True,
        boundary_consumed=True,
        invocation_consumed=True,
        authorization_consumed=True,
        governed_runtime_invoked=True,
        tested_commit_sha=runtime_start_record.tested_commit_sha,
        release_tag=runtime_start_record.release_tag,
    )
    path = _invoke_path(activation_request_id, store_dir=invoke_store_dir)
    try:
        write_once_consume_record(path, record.to_dict())
    except OneShotConsumeWriteConflict as exc:
        raise GovernedRuntimeInvokeError("governed_runtime_already_invoked") from exc
    return record


def _assert_safe_output(output: str) -> None:
    lowered = output.lower()
    for marker in ("password", "secret", "token=", "phrase=", "confirmation_phrase"):
        if marker in lowered:
            raise GovernedRuntimeInvokeError("unsafe_output_blocked")


def format_governed_runtime_invoke_status(summary: GovernedRuntimeInvokeSummary) -> str:
    lines = [
        f"activation_request_id: {summary.activation_request_id}",
        f"invoke_state: {summary.invoke_state}",
        f"invoke_ready: {str(summary.invoke_ready).lower()}",
        f"already_invoked: {str(summary.already_invoked).lower()}",
        f"blocking_items: {', '.join(summary.blocking_items) or 'none'}",
        f"recommended_action: {summary.recommended_action}",
        "production_execution_allowed: false",
        "original_repository2_execution_attempted: false",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output


def run_governed_runtime_invoke_status(activation_request_id: str, **kwargs: Any) -> str:
    summary = evaluate_governed_runtime_invoke(
        activation_request_id=activation_request_id, **kwargs
    )
    return format_governed_runtime_invoke_status(summary)
