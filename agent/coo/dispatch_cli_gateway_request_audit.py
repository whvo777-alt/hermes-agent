"""CLI gateway request audit — Phase 13M Step 2.

Read-only ``gateway audit show`` command wiring. No writes, subprocess,
Discord/Gateway execution, or Repository2 access.
"""

from __future__ import annotations

from pathlib import Path

from agent.coo.dispatch_gateway_request_audit import (
    CooDispatchGatewayRequestAuditSummary,
    format_gateway_request_audit_summary,
    summarize_gateway_request_audit,
)
from agent.coo.dispatch_gateway_request_store import (
    DispatchGatewayRequestStoreError,
    normalize_gateway_request_id,
)

_FORBIDDEN_OUTPUT_TOKENS = frozenset(
    {
        "pipeline_root",
        "unlock_token",
        "unlock_token_id",
        "confirmation_phrase",
        "argv",
        "cwd",
        "env",
        "stdout",
        "stderr",
        "operator_reason",
        "secret",
        "token",
        "snapshot",
    }
)


def normalize_gateway_request_audit_id(gateway_request_id: str) -> str:
    """Normalize and validate a gateway request id for audit lookup."""
    try:
        return normalize_gateway_request_id(gateway_request_id)
    except DispatchGatewayRequestStoreError as exc:
        raise ValueError(str(exc)) from exc


def show_gateway_request_audit(
    gateway_request_id: str,
    *,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> CooDispatchGatewayRequestAuditSummary:
    """Load read-only gateway request audit correlation for one request id."""
    normalized = normalize_gateway_request_audit_id(gateway_request_id)
    return summarize_gateway_request_audit(
        normalized,
        request_dir=request_dir,
        history_dir=history_dir,
        evidence_dir=evidence_dir,
        audit_dir=audit_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
    )


def format_gateway_request_audit_cli_output(
    summary: CooDispatchGatewayRequestAuditSummary,
) -> str:
    """Format safe CLI stdout for one gateway request audit summary."""
    output = format_gateway_request_audit_summary(summary)
    lowered = output.lower()
    for token in _FORBIDDEN_OUTPUT_TOKENS:
        if token in lowered:
            raise ValueError(f"Unsafe gateway request audit output field: {token!r}")
    return output


def run_gateway_request_audit_show(
    gateway_request_id: str,
    *,
    request_dir: Path | None = None,
    history_dir: Path | None = None,
    evidence_dir: Path | None = None,
    audit_dir: Path | None = None,
    bundle_dir: Path | None = None,
    confirmation_dir: Path | None = None,
) -> str:
    """Return formatted audit output or raise on lookup/validation failure."""
    summary = show_gateway_request_audit(
        gateway_request_id,
        request_dir=request_dir,
        history_dir=history_dir,
        evidence_dir=evidence_dir,
        audit_dir=audit_dir,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
    )
    return format_gateway_request_audit_cli_output(summary)
