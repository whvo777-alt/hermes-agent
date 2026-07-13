"""CLI production activation proposal — Phase 14C.

Creates proposed activation artifacts only. No approval, arm, active, or execution.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.coo.production_activation_state import (
    ACTIVATION_STATE_DISABLED,
    ACTIVATION_STATE_PROPOSED,
    ACTIVATION_SCOPE_MAINTENANCE_WINDOW,
    ACTIVATION_SCOPE_TICKET_SCOPED,
    ActivationRequest,
    ActivationScope,
    ActivationStateTransition,
    ProductionActivationStateError,
    ROLE_OPERATOR,
    validate_activation_request,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    append_activation_proposal,
)

PROPOSAL_EXPIRY_HOURS = 72
REASON_PROPOSAL_CREATED = "proposal_created"

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
        "rollback_commit",
        "repository_attestation_hash",
        "requested_by",
    }
)


class ProductionActivationCliError(ValueError):
    """Raised when activation proposal CLI input is invalid."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_sha(value: str, limit: int = 12) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def resolve_git_head_commit(*, repo_root: Path | None = None) -> str:
    """Read current git HEAD commit SHA without subprocess."""
    root = (repo_root or Path.cwd()).resolve()
    git_dir = root / ".git"
    head_path = git_dir / "HEAD"
    if not head_path.is_file():
        raise ProductionActivationCliError(
            "git HEAD is unavailable; tested_commit_sha cannot be verified"
        )
    head_text = head_path.read_text(encoding="utf-8").strip()
    if head_text.startswith("ref: "):
        ref_path = git_dir / head_text[5:].strip()
        if not ref_path.is_file():
            raise ProductionActivationCliError(
                "git HEAD ref is unavailable; tested_commit_sha cannot be verified"
            )
        return ref_path.read_text(encoding="utf-8").strip().lower()
    return head_text.lower()


def _validate_head_sha_match(tested_commit_sha: str, *, repo_root: Path | None) -> str:
    normalized = (tested_commit_sha or "").strip().lower()
    if not normalized:
        raise ProductionActivationCliError("tested_commit_sha is required")
    head = resolve_git_head_commit(repo_root=repo_root)
    if len(normalized) < 40:
        if not head.startswith(normalized):
            raise ProductionActivationCliError(
                "tested_commit_sha does not match current repository HEAD"
            )
        return head
    if normalized != head:
        raise ProductionActivationCliError(
            "tested_commit_sha does not match current repository HEAD"
        )
    return normalized


def build_production_activation_proposal(
    *,
    tested_commit_sha: str,
    release_tag: str,
    repository_attestation_hash: str,
    requested_by: str,
    rollback_commit: str,
    scope_type: str,
    platform: str,
    ticket_id: str = "",
    maintenance_window_start: str = "",
    maintenance_window_end: str = "",
    repo_root: Path | None = None,
    activation_request_id: str | None = None,
    now: datetime | None = None,
) -> ActivationRequest:
    """Build and validate a proposed activation artifact (no persistence)."""
    verified_sha = _validate_head_sha_match(
        tested_commit_sha,
        repo_root=repo_root,
    )
    current = now or datetime.now(timezone.utc)
    created_at = current.isoformat()
    expires_at = (current + timedelta(hours=PROPOSAL_EXPIRY_HOURS)).isoformat()
    request_id = activation_request_id or str(uuid.uuid4())

    scope = ActivationScope(
        scope_type=scope_type.strip(),
        platform=platform.strip().lower(),
        publish_allowed=False,
        ticket_id=(ticket_id or "").strip(),
        maintenance_window_start=(maintenance_window_start or "").strip(),
        maintenance_window_end=(maintenance_window_end or "").strip(),
    )

    request = ActivationRequest(
        activation_request_id=request_id,
        tested_commit_sha=verified_sha,
        release_tag=release_tag.strip(),
        repository_attestation_hash=repository_attestation_hash.strip().lower(),
        requested_by=requested_by.strip(),
        approved_by=(),
        security_reviewed_by="",
        activation_scope=scope,
        rollback_commit=rollback_commit.strip().lower(),
        state=ACTIVATION_STATE_PROPOSED,
        created_at=created_at,
        updated_at=created_at,
        state_history=(
            ActivationStateTransition(
                from_state=ACTIVATION_STATE_DISABLED,
                to_state=ACTIVATION_STATE_PROPOSED,
                actor=requested_by.strip(),
                role=ROLE_OPERATOR,
                timestamp=created_at,
                reason_code=REASON_PROPOSAL_CREATED,
            ),
        ),
        approval_history=(),
        expires_at=expires_at,
        armed_expires_at="",
        active_expires_at="",
    )
    return validate_activation_request(request)


def format_activation_proposal_output(request: ActivationRequest) -> str:
    """Format safe proposal CLI output."""
    validated = validate_activation_request(request)
    scope = validated.activation_scope
    lines = [
        "Production Activation Proposal",
        "",
        f"activation_request_id: {validated.activation_request_id}",
        f"state: {validated.state}",
        f"tested_commit_sha: {_short_sha(validated.tested_commit_sha)}",
        f"release_tag: {validated.release_tag}",
        "",
        "[Scope]",
        f"scope_type: {scope.scope_type}",
        f"platform: {scope.platform}",
        "publish_allowed: false",
    ]
    if scope.scope_type == ACTIVATION_SCOPE_TICKET_SCOPED:
        lines.append(f"ticket_id: {scope.ticket_id}")
    if scope.scope_type == ACTIVATION_SCOPE_MAINTENANCE_WINDOW:
        lines.append(f"maintenance_window_start: {scope.maintenance_window_start}")
        lines.append(f"maintenance_window_end: {scope.maintenance_window_end}")
    lines.extend(
        [
            "",
            f"expires_at: {validated.expires_at}",
            "",
            "[Safety]",
            "production_execution_allowed: false",
        ]
    )
    output = "\n".join(lines)
    lowered = output.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ProductionActivationCliError(
                f"Unsafe activation proposal output field: {token!r}"
            )
    return output


def propose_production_activation(
    *,
    tested_commit_sha: str,
    release_tag: str,
    repository_attestation_hash: str,
    requested_by: str,
    rollback_commit: str,
    scope_type: str,
    platform: str,
    ticket_id: str = "",
    maintenance_window_start: str = "",
    maintenance_window_end: str = "",
    repo_root: Path | None = None,
    store_dir: Path | None = None,
) -> ActivationRequest:
    """Create and persist one proposed activation artifact."""
    try:
        request = build_production_activation_proposal(
            tested_commit_sha=tested_commit_sha,
            release_tag=release_tag,
            repository_attestation_hash=repository_attestation_hash,
            requested_by=requested_by,
            rollback_commit=rollback_commit,
            scope_type=scope_type,
            platform=platform,
            ticket_id=ticket_id,
            maintenance_window_start=maintenance_window_start,
            maintenance_window_end=maintenance_window_end,
            repo_root=repo_root,
        )
        return append_activation_proposal(request, store_dir=store_dir)
    except ProductionActivationStateError as exc:
        raise ProductionActivationCliError(str(exc)) from exc
    except ProductionActivationStoreError as exc:
        raise ProductionActivationCliError(str(exc)) from exc


def run_production_activation_propose(
    *,
    tested_commit_sha: str,
    release_tag: str,
    repository_attestation_hash: str,
    requested_by: str,
    rollback_commit: str,
    scope_type: str,
    platform: str,
    ticket_id: str = "",
    maintenance_window_start: str = "",
    maintenance_window_end: str = "",
    repo_root: Path | None = None,
    store_dir: Path | None = None,
) -> tuple[str, int]:
    """Return formatted proposal output and CLI exit code."""
    request = propose_production_activation(
        tested_commit_sha=tested_commit_sha,
        release_tag=release_tag,
        repository_attestation_hash=repository_attestation_hash,
        requested_by=requested_by,
        rollback_commit=rollback_commit,
        scope_type=scope_type,
        platform=platform,
        ticket_id=ticket_id,
        maintenance_window_start=maintenance_window_start,
        maintenance_window_end=maintenance_window_end,
        repo_root=repo_root,
        store_dir=store_dir,
    )
    return format_activation_proposal_output(request), 0
