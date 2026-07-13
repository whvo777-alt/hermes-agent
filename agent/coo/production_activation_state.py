"""Production activation state model — Phase 14B.

State machine, artifact dataclasses, and validation only.
No persistence writes, CLI, subprocess, Repository2 access, or execution.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Sequence

ACTIVATION_STATE_DISABLED = "disabled"
ACTIVATION_STATE_PROPOSED = "proposed"
ACTIVATION_STATE_APPROVED = "approved"
ACTIVATION_STATE_ARMED = "armed"
ACTIVATION_STATE_ACTIVE = "active"
ACTIVATION_STATE_SUSPENDED = "suspended"
ACTIVATION_STATE_REVOKED = "revoked"

KNOWN_ACTIVATION_STATES = frozenset(
    {
        ACTIVATION_STATE_DISABLED,
        ACTIVATION_STATE_PROPOSED,
        ACTIVATION_STATE_APPROVED,
        ACTIVATION_STATE_ARMED,
        ACTIVATION_STATE_ACTIVE,
        ACTIVATION_STATE_SUSPENDED,
        ACTIVATION_STATE_REVOKED,
    }
)

ACTIVATION_SCOPE_ONE_SHOT = "one_shot"
ACTIVATION_SCOPE_TICKET_SCOPED = "ticket_scoped"
ACTIVATION_SCOPE_MAINTENANCE_WINDOW = "maintenance_window"

KNOWN_ACTIVATION_SCOPE_TYPES = frozenset(
    {
        ACTIVATION_SCOPE_ONE_SHOT,
        ACTIVATION_SCOPE_TICKET_SCOPED,
        ACTIVATION_SCOPE_MAINTENANCE_WINDOW,
    }
)

ACTIVATION_PLATFORM_CLI = "cli"
ACTIVATION_PLATFORM_GATEWAY = "gateway"

KNOWN_ACTIVATION_PLATFORMS = frozenset(
    {
        ACTIVATION_PLATFORM_CLI,
        ACTIVATION_PLATFORM_GATEWAY,
    }
)

ROLE_OPERATOR = "operator"
ROLE_RELEASE_APPROVER = "release_approver"
ROLE_SECURITY_REVIEWER = "security_reviewer"
ROLE_PRODUCTION_EXECUTOR = "production_executor"
ROLE_INCIDENT_COMMANDER = "incident_commander"

MIN_APPROVER_COUNT = 2

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$")
_OPERATOR_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    ACTIVATION_STATE_DISABLED: frozenset({ACTIVATION_STATE_PROPOSED}),
    ACTIVATION_STATE_PROPOSED: frozenset({ACTIVATION_STATE_APPROVED}),
    ACTIVATION_STATE_APPROVED: frozenset({ACTIVATION_STATE_ARMED}),
    ACTIVATION_STATE_ARMED: frozenset({ACTIVATION_STATE_ACTIVE}),
    ACTIVATION_STATE_ACTIVE: frozenset({ACTIVATION_STATE_SUSPENDED}),
    ACTIVATION_STATE_SUSPENDED: frozenset(
        {ACTIVATION_STATE_ACTIVE, ACTIVATION_STATE_REVOKED}
    ),
    ACTIVATION_STATE_REVOKED: frozenset(),
}

_FORBIDDEN_OUTPUT_TOKENS = frozenset(
    {
        "pipeline_root",
        "confirmation_phrase",
        "unlock_token",
        "unlock_token_id",
        "repository2",
        "argv",
        "cwd",
        "env",
        "stdout",
        "stderr",
        "secret",
        "token",
        "filesystem",
        "/opt/data/",
        "pipeline.js",
        "operator_reason",
    }
)


class ProductionActivationStateError(ValueError):
    """Raised when activation state or artifact validation fails."""


class ActivationScopeType(str, Enum):
    """Activation scope category."""

    ONE_SHOT = ACTIVATION_SCOPE_ONE_SHOT
    TICKET_SCOPED = ACTIVATION_SCOPE_TICKET_SCOPED
    MAINTENANCE_WINDOW = ACTIVATION_SCOPE_MAINTENANCE_WINDOW


class ActivationPlatform(str, Enum):
    """Surface allowed for a scoped activation."""

    CLI = ACTIVATION_PLATFORM_CLI
    GATEWAY = ACTIVATION_PLATFORM_GATEWAY


@dataclass(frozen=True)
class ActivationScope:
    """Bounded activation scope; publish is always disabled."""

    scope_type: str
    platform: str
    publish_allowed: bool = False
    ticket_id: str = ""
    maintenance_window_start: str = ""
    maintenance_window_end: str = ""

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "scope_type": self.scope_type,
            "platform": self.platform,
            "publish_allowed": self.publish_allowed,
            "ticket_id": self.ticket_id,
            "maintenance_window_start": self.maintenance_window_start,
            "maintenance_window_end": self.maintenance_window_end,
        }


@dataclass(frozen=True)
class ActivationStateTransition:
    """Append-only activation state transition record."""

    from_state: str
    to_state: str
    actor: str
    role: str
    timestamp: str
    reason_code: str


@dataclass(frozen=True)
class ActivationApprovalRecord:
    """Append-only approval history entry."""

    approver_id: str
    role: str
    timestamp: str
    approval_id: str = ""
    activation_request_id: str = ""
    decision: str = "approved"
    reason_code: str = ""
    tested_commit_sha: str = ""
    release_tag: str = ""


@dataclass(frozen=True)
class ActivationRequest:
    """Production activation artifact (in-memory model; no store in Phase 14B)."""

    activation_request_id: str
    tested_commit_sha: str
    release_tag: str
    repository_attestation_hash: str
    requested_by: str
    approved_by: tuple[str, ...]
    security_reviewed_by: str
    activation_scope: ActivationScope
    rollback_commit: str
    state: str
    created_at: str
    updated_at: str
    state_history: tuple[ActivationStateTransition, ...]
    approval_history: tuple[ActivationApprovalRecord, ...]
    expires_at: str
    armed_expires_at: str
    active_expires_at: str


def _parse_iso8601(value: str, *, field_name: str) -> datetime:
    text = (value or "").strip()
    if not text:
        raise ProductionActivationStateError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionActivationStateError(
            f"{field_name} must be a valid ISO-8601 timestamp"
        ) from exc
    return parsed


def _validate_operator_id(value: str, *, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ProductionActivationStateError(f"{field_name} is required")
    if "/" in normalized or "\\" in normalized or ".." in normalized:
        raise ProductionActivationStateError(
            f"{field_name} must not contain path separators"
        )
    if not _OPERATOR_ID_RE.match(normalized):
        raise ProductionActivationStateError(f"{field_name} has invalid format")
    return normalized


def _validate_uuid(value: str, *, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ProductionActivationStateError(f"{field_name} is required")
    try:
        parsed = uuid.UUID(normalized)
    except ValueError as exc:
        raise ProductionActivationStateError(
            f"{field_name} must be a valid UUID"
        ) from exc
    return str(parsed)


def _validate_commit_sha(value: str, *, field_name: str) -> str:
    normalized = (value or "").strip().lower()
    if not normalized:
        raise ProductionActivationStateError(f"{field_name} is required")
    if not _SHA_RE.match(normalized):
        raise ProductionActivationStateError(
            f"{field_name} must be a 7-40 character git commit SHA"
        )
    return normalized


def _validate_release_tag(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ProductionActivationStateError("release_tag is required")
    if not _TAG_RE.match(normalized):
        raise ProductionActivationStateError(
            "release_tag must match v<major>.<minor>.<patch>[-suffix]"
        )
    return normalized


def _validate_attestation_hash(value: str) -> str:
    normalized = (value or "").strip().lower()
    if not normalized:
        raise ProductionActivationStateError(
            "repository_attestation_hash is required"
        )
    if not _SHA256_RE.match(normalized):
        raise ProductionActivationStateError(
            "repository_attestation_hash must be a 64-character SHA-256 hex digest"
        )
    return normalized


def validate_activation_scope(scope: ActivationScope) -> ActivationScope:
    """Validate activation scope invariants."""
    scope_type = (scope.scope_type or "").strip()
    if scope_type not in KNOWN_ACTIVATION_SCOPE_TYPES:
        raise ProductionActivationStateError(
            f"activation_scope.scope_type must be one of: "
            f"{sorted(KNOWN_ACTIVATION_SCOPE_TYPES)}"
        )

    platform = (scope.platform or "").strip().lower()
    if platform not in KNOWN_ACTIVATION_PLATFORMS:
        raise ProductionActivationStateError(
            f"activation_scope.platform must be one of: "
            f"{sorted(KNOWN_ACTIVATION_PLATFORMS)}"
        )

    if scope.publish_allowed:
        raise ProductionActivationStateError(
            "activation_scope.publish_allowed must remain false"
        )

    ticket_id = (scope.ticket_id or "").strip()
    if scope_type == ACTIVATION_SCOPE_TICKET_SCOPED and not ticket_id:
        raise ProductionActivationStateError(
            "activation_scope.ticket_id is required for ticket_scoped activations"
        )
    if ticket_id and ("/" in ticket_id or "\\" in ticket_id):
        raise ProductionActivationStateError(
            "activation_scope.ticket_id must not contain path separators"
        )

    window_start = (scope.maintenance_window_start or "").strip()
    window_end = (scope.maintenance_window_end or "").strip()
    if scope_type == ACTIVATION_SCOPE_MAINTENANCE_WINDOW:
        if not window_start or not window_end:
            raise ProductionActivationStateError(
                "maintenance_window_start and maintenance_window_end are required "
                "for maintenance_window scope"
            )
        start_dt = _parse_iso8601(
            window_start,
            field_name="activation_scope.maintenance_window_start",
        )
        end_dt = _parse_iso8601(
            window_end,
            field_name="activation_scope.maintenance_window_end",
        )
        if end_dt <= start_dt:
            raise ProductionActivationStateError(
                "maintenance_window_end must follow maintenance_window_start"
            )
    elif window_start or window_end:
        raise ProductionActivationStateError(
            "maintenance_window fields are only allowed for maintenance_window scope"
        )

    return ActivationScope(
        scope_type=scope_type,
        platform=platform,
        publish_allowed=False,
        ticket_id=ticket_id,
        maintenance_window_start=window_start,
        maintenance_window_end=window_end,
    )


def validate_activation_transition(from_state: str, to_state: str) -> None:
    """Fail closed when a state transition is not explicitly allowed."""
    normalized_from = (from_state or "").strip()
    normalized_to = (to_state or "").strip()
    if normalized_from not in KNOWN_ACTIVATION_STATES:
        raise ProductionActivationStateError(f"invalid from_state: {normalized_from!r}")
    if normalized_to not in KNOWN_ACTIVATION_STATES:
        raise ProductionActivationStateError(f"invalid to_state: {normalized_to!r}")
    allowed = _ALLOWED_TRANSITIONS.get(normalized_from, frozenset())
    if normalized_to not in allowed:
        raise ProductionActivationStateError(
            f"transition {normalized_from!r} -> {normalized_to!r} is not allowed"
        )


def _validate_state_history(
    history: Sequence[ActivationStateTransition],
    *,
    expected_state: str,
) -> None:
    if not history:
        if expected_state == ACTIVATION_STATE_DISABLED:
            return
        raise ProductionActivationStateError(
            "state_history is required once activation leaves disabled"
        )

    previous_to = ACTIVATION_STATE_DISABLED
    previous_ts: datetime | None = None
    for index, transition in enumerate(history):
        validate_activation_transition(transition.from_state, transition.to_state)
        if transition.from_state != previous_to:
            raise ProductionActivationStateError(
                f"state_history[{index}] from_state does not chain from prior transition"
            )
        _validate_operator_id(transition.actor, field_name="transition.actor")
        if not (transition.role or "").strip():
            raise ProductionActivationStateError("transition.role is required")
        if not (transition.reason_code or "").strip():
            raise ProductionActivationStateError("transition.reason_code is required")
        ts = _parse_iso8601(transition.timestamp, field_name="transition.timestamp")
        if previous_ts is not None and ts < previous_ts:
            raise ProductionActivationStateError(
                "state_history timestamps must be monotonic (append-only)"
            )
        previous_to = transition.to_state
        previous_ts = ts

    if previous_to != expected_state:
        raise ProductionActivationStateError(
            "state_history terminal state must match activation state"
        )


def _validate_pending_approval_history(
    request: ActivationRequest,
    approval_history: Sequence[ActivationApprovalRecord],
) -> None:
    """Validate partial approval records while activation remains proposed."""
    allowed_roles = frozenset({ROLE_RELEASE_APPROVER, ROLE_SECURITY_REVIEWER})
    seen_actor_roles: set[tuple[str, str]] = set()
    release_approvers: set[str] = set()
    security_reviewers: set[str] = set()
    previous_ts: datetime | None = None

    for index, record in enumerate(approval_history):
        actor = _validate_operator_id(
            record.approver_id,
            field_name=f"approval_history[{index}].approver_id",
        )
        role = (record.role or "").strip()
        if role not in allowed_roles:
            raise ProductionActivationStateError(
                f"approval_history[{index}].role must be release_approver or "
                "security_reviewer during proposed state"
            )
        if actor == request.requested_by:
            raise ProductionActivationStateError(
                "requester cannot appear in approval_history"
            )
        actor_role = (actor, role)
        if actor_role in seen_actor_roles:
            raise ProductionActivationStateError(
                "duplicate approval actor and role in approval_history"
            )
        seen_actor_roles.add(actor_role)

        decision = (record.decision or "").strip().lower()
        if decision != "approved":
            raise ProductionActivationStateError(
                "approval_history decision must be approved during proposed state"
            )

        if record.tested_commit_sha and record.tested_commit_sha != request.tested_commit_sha:
            raise ProductionActivationStateError(
                "approval_history tested_commit_sha drift detected"
            )
        if record.release_tag and record.release_tag != request.release_tag:
            raise ProductionActivationStateError(
                "approval_history release_tag drift detected"
            )

        ts = _parse_iso8601(record.timestamp, field_name="approval.timestamp")
        if previous_ts is not None and ts < previous_ts:
            raise ProductionActivationStateError(
                "approval_history timestamps must be monotonic (append-only)"
            )
        previous_ts = ts

        if role == ROLE_RELEASE_APPROVER:
            if actor in security_reviewers:
                raise ProductionActivationStateError(
                    "release approver cannot match security reviewer identity"
                )
            release_approvers.add(actor)
        else:
            if actor in release_approvers:
                raise ProductionActivationStateError(
                    "security reviewer cannot match release approver identity"
                )
            security_reviewers.add(actor)


def _validate_approval_history(
    request: ActivationRequest,
    approval_history: Sequence[ActivationApprovalRecord],
) -> None:
    if request.state == ACTIVATION_STATE_DISABLED:
        if approval_history:
            raise ProductionActivationStateError(
                "approval_history must be empty in disabled state"
            )
        return

    if request.state == ACTIVATION_STATE_PROPOSED:
        _validate_pending_approval_history(request, approval_history)
        if request.approved_by:
            raise ProductionActivationStateError(
                "approved_by must be empty while activation remains proposed"
            )
        if request.security_reviewed_by:
            raise ProductionActivationStateError(
                "security_reviewed_by must be empty while activation remains proposed"
            )
        return

    if not approval_history:
        raise ProductionActivationStateError(
            "approval_history is required from approved state onward"
        )

    previous_ts: datetime | None = None
    for index, record in enumerate(approval_history):
        approver = _validate_operator_id(
            record.approver_id,
            field_name=f"approval_history[{index}].approver_id",
        )
        if approver == request.requested_by:
            raise ProductionActivationStateError(
                "requester cannot appear in approval_history"
            )
        if not (record.role or "").strip():
            raise ProductionActivationStateError("approval.role is required")
        ts = _parse_iso8601(record.timestamp, field_name="approval.timestamp")
        if previous_ts is not None and ts < previous_ts:
            raise ProductionActivationStateError(
                "approval_history timestamps must be monotonic (append-only)"
            )
        previous_ts = ts


def _validate_approver_list(request: ActivationRequest) -> None:
    approvers = tuple(
        _validate_operator_id(item, field_name="approved_by entry")
        for item in request.approved_by
    )
    if request.state in {
        ACTIVATION_STATE_APPROVED,
        ACTIVATION_STATE_ARMED,
        ACTIVATION_STATE_ACTIVE,
        ACTIVATION_STATE_SUSPENDED,
        ACTIVATION_STATE_REVOKED,
    }:
        if len(approvers) < MIN_APPROVER_COUNT:
            raise ProductionActivationStateError(
                f"approved_by requires at least {MIN_APPROVER_COUNT} distinct approvers"
            )
        if len(set(approvers)) != len(approvers):
            raise ProductionActivationStateError(
                "approved_by entries must be distinct"
            )
        if request.requested_by in approvers:
            raise ProductionActivationStateError(
                "requested_by must not appear in approved_by"
            )
        _validate_operator_id(
            request.security_reviewed_by,
            field_name="security_reviewed_by",
        )
        if request.security_reviewed_by in approvers:
            raise ProductionActivationStateError(
                "security_reviewed_by must be distinct from approved_by entries"
            )
    elif approvers:
        raise ProductionActivationStateError(
            "approved_by must be empty before approved state"
        )


def _validate_ttl_fields(request: ActivationRequest) -> None:
    created = _parse_iso8601(request.created_at, field_name="created_at")
    updated = _parse_iso8601(request.updated_at, field_name="updated_at")
    if updated < created:
        raise ProductionActivationStateError(
            "updated_at must not precede created_at"
        )

    expires_text = (request.expires_at or "").strip()
    armed_text = (request.armed_expires_at or "").strip()
    active_text = (request.active_expires_at or "").strip()

    if request.state in {
        ACTIVATION_STATE_APPROVED,
        ACTIVATION_STATE_ARMED,
        ACTIVATION_STATE_ACTIVE,
        ACTIVATION_STATE_SUSPENDED,
        ACTIVATION_STATE_REVOKED,
    }:
        if not expires_text:
            raise ProductionActivationStateError(
                "expires_at is required from approved state onward"
            )

    if request.state in {
        ACTIVATION_STATE_ARMED,
        ACTIVATION_STATE_ACTIVE,
        ACTIVATION_STATE_SUSPENDED,
    }:
        if not armed_text:
            raise ProductionActivationStateError(
                "armed_expires_at is required from armed state onward"
            )

    if request.state == ACTIVATION_STATE_ACTIVE:
        if not active_text:
            raise ProductionActivationStateError(
                "active_expires_at is required in active state"
            )

    expires_at: datetime | None = None
    armed_expires_at: datetime | None = None
    active_expires_at: datetime | None = None

    if expires_text:
        expires_at = _parse_iso8601(expires_text, field_name="expires_at")
        if expires_at < created:
            raise ProductionActivationStateError(
                "expires_at must not precede created_at"
            )
    if armed_text:
        armed_expires_at = _parse_iso8601(
            armed_text,
            field_name="armed_expires_at",
        )
        if armed_expires_at < updated:
            raise ProductionActivationStateError(
                "armed_expires_at must not precede updated_at"
            )
    if active_text:
        active_expires_at = _parse_iso8601(
            active_text,
            field_name="active_expires_at",
        )
        if active_expires_at < updated:
            raise ProductionActivationStateError(
                "active_expires_at must not precede updated_at"
            )

    if armed_expires_at and active_expires_at and armed_expires_at > active_expires_at:
        raise ProductionActivationStateError(
            "armed_expires_at must not follow active_expires_at (TTL reversal)"
        )
    if expires_at and armed_expires_at and expires_at < armed_expires_at:
        raise ProductionActivationStateError(
            "expires_at must not precede armed_expires_at (TTL reversal)"
        )


def validate_activation_request(request: ActivationRequest) -> ActivationRequest:
    """Validate a production activation artifact; fail closed on any violation."""
    state = (request.state or "").strip()
    if state not in KNOWN_ACTIVATION_STATES:
        raise ProductionActivationStateError(f"invalid activation state: {state!r}")

    activation_request_id = _validate_uuid(
        request.activation_request_id,
        field_name="activation_request_id",
    )
    requested_by = _validate_operator_id(
        request.requested_by,
        field_name="requested_by",
    )

    scope = validate_activation_scope(request.activation_scope)

    if state == ACTIVATION_STATE_DISABLED:
        if any(
            (
                request.tested_commit_sha,
                request.release_tag,
                request.repository_attestation_hash,
                request.rollback_commit,
            )
        ):
            raise ProductionActivationStateError(
                "disabled activation must not include commit, tag, attestation, or rollback"
            )
        _validate_state_history(request.state_history, expected_state=state)
        _validate_approval_history(request, request.approval_history)
        _validate_approver_list(request)
        _validate_ttl_fields(request)
        return ActivationRequest(
            activation_request_id=activation_request_id,
            tested_commit_sha="",
            release_tag="",
            repository_attestation_hash="",
            requested_by=requested_by,
            approved_by=(),
            security_reviewed_by="",
            activation_scope=scope,
            rollback_commit="",
            state=state,
            created_at=request.created_at,
            updated_at=request.updated_at,
            state_history=request.state_history,
            approval_history=request.approval_history,
            expires_at="",
            armed_expires_at="",
            active_expires_at="",
        )

    tested_commit_sha = _validate_commit_sha(
        request.tested_commit_sha,
        field_name="tested_commit_sha",
    )
    release_tag = _validate_release_tag(request.release_tag)
    repository_attestation_hash = _validate_attestation_hash(
        request.repository_attestation_hash
    )
    rollback_commit = _validate_commit_sha(
        request.rollback_commit,
        field_name="rollback_commit",
    )

    _validate_state_history(request.state_history, expected_state=state)
    _validate_approval_history(request, request.approval_history)
    _validate_approver_list(request)
    _validate_ttl_fields(request)

    return ActivationRequest(
        activation_request_id=activation_request_id,
        tested_commit_sha=tested_commit_sha,
        release_tag=release_tag,
        repository_attestation_hash=repository_attestation_hash,
        requested_by=requested_by,
        approved_by=tuple(request.approved_by),
        security_reviewed_by=(request.security_reviewed_by or "").strip(),
        activation_scope=scope,
        rollback_commit=rollback_commit,
        state=state,
        created_at=request.created_at,
        updated_at=request.updated_at,
        state_history=request.state_history,
        approval_history=request.approval_history,
        expires_at=(request.expires_at or "").strip(),
        armed_expires_at=(request.armed_expires_at or "").strip(),
        active_expires_at=(request.active_expires_at or "").strip(),
    )


def _assert_safe_activation_output(output: str) -> None:
    lowered = output.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationStateError(
                f"Unsafe activation output field: {token!r}"
            )


def format_activation_request(request: ActivationRequest) -> str:
    """Format a safe read-only activation summary (no secrets or paths)."""
    validated = validate_activation_request(request)
    scope = validated.activation_scope
    lines = [
        "Production Activation Request",
        "",
        f"activation_request_id: {validated.activation_request_id}",
        f"state: {validated.state}",
        f"release_tag: {validated.release_tag or '(none)'}",
        f"tested_commit_sha: {validated.tested_commit_sha or '(none)'}",
        f"rollback_commit: {validated.rollback_commit or '(none)'}",
        f"repository_attestation_hash: {validated.repository_attestation_hash or '(none)'}",
        f"requested_by: {validated.requested_by}",
        f"approved_by_count: {len(validated.approved_by)}",
        f"security_reviewed_by: {validated.security_reviewed_by or '(none)'}",
        "",
        "[Scope]",
        f"scope_type: {scope.scope_type}",
        f"platform: {scope.platform}",
        "publish_allowed: false",
        f"ticket_id: {scope.ticket_id or '(none)'}",
        "",
        "[TTL]",
        f"expires_at: {validated.expires_at or '(none)'}",
        f"armed_expires_at: {validated.armed_expires_at or '(none)'}",
        f"active_expires_at: {validated.active_expires_at or '(none)'}",
        "",
        "[History]",
        f"state_transition_count: {len(validated.state_history)}",
        f"approval_record_count: {len(validated.approval_history)}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
    ]
    output = "\n".join(lines)
    _assert_safe_activation_output(output)
    return output
