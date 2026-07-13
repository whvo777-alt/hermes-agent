"""Operator guidance — Phase 13Q.

Read-only mapping from recommended_action codes to in-repo runbook references.
No execution, subprocess, or secret disclosure.
"""

from __future__ import annotations

from dataclasses import dataclass

# Canonical recommended_action codes (union of operational, dashboard, correlation).
ACTION_NO_ACTION_REQUIRED = "no_action_required"
ACTION_RUN_GATEWAY_PILOT_DRY_RUN = "run_gateway_pilot_dry_run"
ACTION_RUN_GATEWAY_MOCK_PILOT = "run_gateway_mock_pilot"
ACTION_COLLECT_MORE_HISTORY = "collect_more_history"
ACTION_COLLECT_MORE_PILOT_HISTORY = "collect_more_pilot_history"
ACTION_INSPECT_LATEST_FAILURE = "inspect_latest_failure"
ACTION_INVESTIGATE_RECENT_FAILURE = "investigate_recent_failure"
ACTION_INSPECT_MISSING_EVIDENCE = "inspect_missing_evidence"
ACTION_INSPECT_EXECUTION_FAILURE = "inspect_execution_failure"
ACTION_INSPECT_CONSUME_STATE = "inspect_consume_state"
ACTION_RESOLVE_RECOVERY_REQUIRED = "resolve_recovery_required"
ACTION_RESOLVE_RECOVERY_ISSUE = "resolve_recovery_issue"
ACTION_RESOLVE_CORRELATION_MISMATCH = "resolve_correlation_mismatch"
ACTION_RESOLVE_REGRESSION_FAILURE = "resolve_regression_failure"
ACTION_STAGE_GATEWAY = "stage_gateway"
ACTION_MAINTAIN_PRODUCTION_BLOCK = "maintain_production_block"
ACTION_PROVIDE_MORE_SPECIFIC_ID = "provide_more_specific_id"
ACTION_REQUEST_NOT_FOUND = "request_not_found"
ACTION_INSPECT_REGRESSION = "inspect_regression"
ACTION_INSPECT_CONSUME_DRIFT = "inspect_consume_drift"
ACTION_PROVIDE_SAME_TICKET_REQUESTS = "provide_same_ticket_requests"

KNOWN_RECOMMENDED_ACTIONS = frozenset(
    {
        ACTION_NO_ACTION_REQUIRED,
        ACTION_RUN_GATEWAY_PILOT_DRY_RUN,
        ACTION_RUN_GATEWAY_MOCK_PILOT,
        ACTION_COLLECT_MORE_HISTORY,
        ACTION_COLLECT_MORE_PILOT_HISTORY,
        ACTION_INSPECT_LATEST_FAILURE,
        ACTION_INVESTIGATE_RECENT_FAILURE,
        ACTION_INSPECT_MISSING_EVIDENCE,
        ACTION_INSPECT_EXECUTION_FAILURE,
        ACTION_INSPECT_CONSUME_STATE,
        ACTION_RESOLVE_RECOVERY_REQUIRED,
        ACTION_RESOLVE_RECOVERY_ISSUE,
        ACTION_RESOLVE_CORRELATION_MISMATCH,
        ACTION_RESOLVE_REGRESSION_FAILURE,
        ACTION_STAGE_GATEWAY,
        ACTION_MAINTAIN_PRODUCTION_BLOCK,
        ACTION_PROVIDE_MORE_SPECIFIC_ID,
        ACTION_REQUEST_NOT_FOUND,
        ACTION_INSPECT_REGRESSION,
        ACTION_INSPECT_CONSUME_DRIFT,
        ACTION_PROVIDE_SAME_TICKET_REQUESTS,
    }
)

_FORBIDDEN_OUTPUT_KEYS = frozenset(
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
        "channel_id",
        "requester_metadata",
    }
)


@dataclass(frozen=True)
class CooDispatchOperatorGuidance:
    """Safe read-only operator guidance for one recommended_action code."""

    recommended_action: str
    runbook_name: str
    runbook_section: str
    guidance_summary: str
    production_execution_allowed: bool = False


class OperatorGuidanceError(ValueError):
    """Raised when guidance cannot be resolved safely."""


def _entry(
    runbook_name: str,
    runbook_section: str,
    guidance_summary: str,
) -> tuple[str, str, str]:
    return runbook_name, runbook_section, guidance_summary


_GUIDANCE_ENTRIES: dict[str, tuple[str, str, str]] = {
    ACTION_NO_ACTION_REQUIRED: _entry(
        "Gateway_Runbook",
        "recommended-action-codes",
        "No operator intervention required; continue monitoring.",
    ),
    ACTION_RUN_GATEWAY_PILOT_DRY_RUN: _entry(
        "Gateway_Runbook",
        "gateway-pilot",
        "Run gateway pilot dry-run preflight before live mock.",
    ),
    ACTION_RUN_GATEWAY_MOCK_PILOT: _entry(
        "Gateway_Runbook",
        "gateway-pilot",
        "Run staged gateway mock pilot when regression allows.",
    ),
    ACTION_COLLECT_MORE_HISTORY: _entry(
        "Pilot_Runbook",
        "history",
        "Collect more pilot history before escalating mock pilot.",
    ),
    ACTION_COLLECT_MORE_PILOT_HISTORY: _entry(
        "Pilot_Runbook",
        "history",
        "Collect more pilot history before escalating mock pilot.",
    ),
    ACTION_INSPECT_LATEST_FAILURE: _entry(
        "Operator_Checklist",
        "pilot-incident",
        "Inspect the latest pilot failure and correlation chain.",
    ),
    ACTION_INVESTIGATE_RECENT_FAILURE: _entry(
        "Operator_Checklist",
        "pilot-incident",
        "Inspect the latest pilot failure and correlation chain.",
    ),
    ACTION_INSPECT_MISSING_EVIDENCE: _entry(
        "Gateway_Runbook",
        "correlation-explorer",
        "Inspect missing evidence or audit for the latest request.",
    ),
    ACTION_INSPECT_EXECUTION_FAILURE: _entry(
        "Gateway_Runbook",
        "correlation-explorer",
        "Inspect execution failure details in the correlation chain.",
    ),
    ACTION_INSPECT_CONSUME_STATE: _entry(
        "Recovery_Runbook",
        "when-recovery-required",
        "Review consume state before repair or recovery actions.",
    ),
    ACTION_RESOLVE_RECOVERY_REQUIRED: _entry(
        "Recovery_Runbook",
        "manual-recovery",
        "Follow manual recovery flow for the consume pair.",
    ),
    ACTION_RESOLVE_RECOVERY_ISSUE: _entry(
        "Recovery_Runbook",
        "manual-recovery",
        "Follow manual recovery flow for the consume pair.",
    ),
    ACTION_RESOLVE_CORRELATION_MISMATCH: _entry(
        "Gateway_Runbook",
        "correlation-explorer",
        "Resolve correlation mismatch across request and history stores.",
    ),
    ACTION_RESOLVE_REGRESSION_FAILURE: _entry(
        "Pilot_Runbook",
        "regression",
        "Resolve pilot regression failure before mock pilot.",
    ),
    ACTION_STAGE_GATEWAY: _entry(
        "Gateway_Runbook",
        "gateway-state",
        "Stage gateway enablement before mock pilot work.",
    ),
    ACTION_MAINTAIN_PRODUCTION_BLOCK: _entry(
        "Production_Signoff",
        "why-production-stays-blocked",
        "Maintain production block; do not enable production execution.",
    ),
    ACTION_PROVIDE_MORE_SPECIFIC_ID: _entry(
        "Gateway_Runbook",
        "correlation-explorer",
        "Provide a more specific opaque correlation query id.",
    ),
    ACTION_REQUEST_NOT_FOUND: _entry(
        "CLI_Command_Reference",
        "gateway",
        "Verify gateway request id and retry correlation lookup.",
    ),
    ACTION_INSPECT_REGRESSION: _entry(
        "Operator_Checklist",
        "gateway-incident",
        "Inspect regression drift between gateway requests.",
    ),
    ACTION_INSPECT_CONSUME_DRIFT: _entry(
        "Recovery_Runbook",
        "correlation-during-recovery",
        "Inspect consume drift between correlated requests.",
    ),
    ACTION_PROVIDE_SAME_TICKET_REQUESTS: _entry(
        "Gateway_Runbook",
        "correlation-explorer",
        "Compare gateway requests from the same ticket only.",
    ),
}

# Section anchor validation: section id -> heading substring expected in runbook file.
RUNBOOK_SECTION_ANCHORS: dict[str, dict[str, str]] = {
    "Gateway_Runbook.md": {
        "gateway-state": "Gateway state",
        "gateway-pilot": "Gateway pilot",
        "correlation-explorer": "Correlation explorer",
        "recommended-action-codes": "Recommended action codes",
    },
    "Recovery_Runbook.md": {
        "when-recovery-required": "When recovery is required",
        "manual-recovery": "Manual recovery operator flow",
        "correlation-during-recovery": "Correlation during recovery",
    },
    "Pilot_Runbook.md": {
        "history": "History",
        "regression": "Regression",
    },
    "Production_Signoff.md": {
        "why-production-stays-blocked": "Why production stays blocked",
    },
    "Operator_Checklist.md": {
        "pilot-incident": "Pilot incident",
        "gateway-incident": "Gateway incident",
    },
    "CLI_Command_Reference.md": {
        "gateway": "gateway",
    },
}


def normalize_recommended_action(recommended_action: str) -> str:
    normalized = (recommended_action or "").strip()
    if not normalized:
        raise OperatorGuidanceError("recommended_action is required")
    if "/" in normalized or "\\" in normalized:
        raise OperatorGuidanceError(
            "recommended_action must not contain path separators."
        )
    return normalized


def format_runbook_ref(guidance: CooDispatchOperatorGuidance) -> str:
    """Return safe runbook_ref code (no URL or filesystem path)."""
    return f"{guidance.runbook_name}#{guidance.runbook_section}"


def resolve_operator_guidance(recommended_action: str) -> CooDispatchOperatorGuidance:
    """Resolve read-only guidance for one recommended_action code."""
    normalized = normalize_recommended_action(recommended_action)
    if normalized not in KNOWN_RECOMMENDED_ACTIONS:
        raise OperatorGuidanceError(f"Unknown recommended_action: {normalized}")
    entry = _GUIDANCE_ENTRIES.get(normalized)
    if entry is None:
        raise OperatorGuidanceError(f"Unknown recommended_action: {normalized}")
    runbook_name, runbook_section, guidance_summary = entry
    return CooDispatchOperatorGuidance(
        recommended_action=normalized,
        runbook_name=runbook_name,
        runbook_section=runbook_section,
        guidance_summary=guidance_summary,
        production_execution_allowed=False,
    )


def append_guidance_output_lines(
    lines: list[str],
    recommended_action: str,
) -> None:
    """Append optional guidance lines; ignore unknown actions silently."""
    try:
        guidance = resolve_operator_guidance(recommended_action)
    except OperatorGuidanceError:
        return
    lines.extend(
        [
            f"runbook_ref: {format_runbook_ref(guidance)}",
            f"guidance_summary: {guidance.guidance_summary}",
        ]
    )


def _assert_safe_guidance_output(output: str) -> None:
    lowered = output.lower()
    for token in _FORBIDDEN_OUTPUT_KEYS:
        if token in lowered:
            raise OperatorGuidanceError(
                f"Unsafe operator guidance output field: {token!r}"
            )


def format_operator_guidance(guidance: CooDispatchOperatorGuidance) -> str:
    """Format safe operator guidance for CLI or Discord."""
    lines = [
        "Operator Guidance",
        "",
        f"recommended_action: {guidance.recommended_action}",
        f"runbook_name: {guidance.runbook_name}",
        f"runbook_section: {guidance.runbook_section}",
        f"runbook_ref: {format_runbook_ref(guidance)}",
        f"guidance_summary: {guidance.guidance_summary}",
        "",
        "[Safety]",
        "production_execution_allowed: false",
    ]
    output = "\n".join(lines)
    _assert_safe_guidance_output(output)
    return output
