"""Discord COO CEO approval handler — Phase 6C-1 through 6C-5.

Prepares in-memory COO approval session payloads, pure-dict embed/component UI
payloads, and optional discord.py Embed/View objects for Discord handler wiring.

Button callbacks update Approval Session state
(approve/reject/refresh/prepare_plan/dry_run_preview/execution_review).
Prepare Plan creates a dispatch plan only — no Repository2 execution.
Dry Run Preview starts a synthetic dry-run only — no Repository2 execution.
Execution Review creates ExecuteRequest + ExecuteGate only — no execution.

This module is for COO CEO approval sessions only.
This module is unrelated to ``tools/approval.py`` ``resolve_gateway_approval()``.
``resolve_gateway_approval`` is the legacy/general exec approval queue used by
``DiscordAdapter.send_exec_approval()`` and ``ExecApprovalView``.
This module does not dispatch execution.
This module does not create execution tickets.
This module does not auto-approve or auto-publish at the pipeline layer.
This module does not send Discord messages except via button interaction responses.

Approval Session TTL is 24 hours (``agent/coo/approval_session.py``).
COO approval View timeout intentionally matches the session TTL.
Persistent views (``timeout=None``) are deferred until restart-safe handler
registration is designed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from agent.coo.approval_report import CEOApprovalReport
from agent.coo.discord_approval_adapter import create_discord_approval_session
from agent.coo.models import COOOrchestrationResult

logger = logging.getLogger(__name__)

DiscordSnowflake = Union[str, int]

_EMBED_TITLE = "Hermes COO Approval Required"
_EMBED_COLOR = 0x1ABC9C  # COO approval teal — distinct from exec approval orange/green/blue/purple
_EMBED_FOOTER_TEXT = "Plan only. No execution or publish is dispatched."
_EMBED_FOOTER_TEXT_DRY_RUN = (
    "Dry run preview only. No execution or publish is dispatched."
)
_EMBED_FOOTER_TEXT_EXECUTION_REVIEW = (
    "Execution review only. No execution or publish is dispatched."
)
_EMBED_FOOTER_TEXT_NO_PLAN = "Approval only. No execution will be dispatched."
# Discord hard limits (per-element) used by this builder.
_FIELD_VALUE_MAX = 1024  # max chars per embed field value
_DESCRIPTION_MAX = 3500  # stays under Discord's 4096 description cap with headroom
_EMBED_TOTAL_MAX = 6000  # Discord aggregate embed character budget
# UI readability cap for inline field values — not a Discord API hard limit
# (Discord allows up to 1024 chars per field value via ``_FIELD_VALUE_MAX``).
_INLINE_FIELD_VALUE_MAX = 256
_CUSTOM_ID_MAX = 100
_COO_APPROVAL_CUSTOM_ID_PREFIX = "coo_approval"
_ALLOWED_COO_APPROVAL_ACTIONS = frozenset({
    "approve",
    "reject",
    "refresh",
    "prepare_plan",
    "dry_run_preview",
    "execution_review",
    "approve_execution_review",
    "reject_execution_review",
})
_PREPARE_PLAN_EPHEMERAL = "Plan Ready — Not Executed"
_DRY_RUN_PREVIEW_EPHEMERAL = "Dry Run Preview — Not Executed"
_EXECUTION_REVIEW_EPHEMERAL = "Execution Review Ready — Not Executed"
_APPROVE_EXECUTION_REVIEW_EPHEMERAL = "Execution Review Approved — Not Executed"
_REJECT_EXECUTION_REVIEW_EPHEMERAL = "Execution Review Rejected — Not Executed"
_ERR_SESSION_NOT_FOUND = "Approval session not found."
_ERR_NOT_ALLOWED = "You are not allowed to approve this session."
_ERR_SESSION_EXPIRED = "Approval session expired."
_ERR_GENERIC = "Unable to process approval action."
_TERMINAL_APPROVAL_STATUSES = frozenset({"approved", "rejected", "expired", "cancelled"})
# Matches CEO approval session TTL (24 hours). Not ``timeout=None`` — persistent
# views require bot-restart-safe custom_id registration (deferred to a later phase).
_COO_APPROVAL_VIEW_TIMEOUT_SECONDS = 24 * 60 * 60

_discord_module: Any = None
_discord_import_checked = False

if TYPE_CHECKING:
    from agent.coo.approval_session import CEOApprovalSessionStore


def _get_discord_module() -> Any:
    """Return the discord.py module when installed, otherwise ``None``."""
    global _discord_module, _discord_import_checked
    if not _discord_import_checked:
        try:
            import discord as discord_mod

            _discord_module = discord_mod
        except ImportError:
            _discord_module = None
        _discord_import_checked = True
    return _discord_module


def discord_ui_available() -> bool:
    """Return whether discord.py is importable in the current environment."""
    return _get_discord_module() is not None


def normalize_discord_snowflake(value: DiscordSnowflake) -> str:
    """Normalize Discord user/channel snowflakes to string IDs."""
    return str(value)


def _truncate(value: Any, max_len: int) -> str:
    text = str(value if value is not None else "")
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def _require_session_id(session_payload: Dict[str, Any]) -> str:
    session_id = str(session_payload.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("session_payload must include a non-empty session_id")
    return session_id


def _execution_ticket_label(session_payload: Dict[str, Any]) -> str:
    ticket_id = str(session_payload.get("execution_ticket_id") or "").strip()
    if not ticket_id:
        return "Not created"
    return _truncate(ticket_id, _FIELD_VALUE_MAX)


def _dispatch_label(session_payload: Dict[str, Any], key: str) -> str:
    return "Dispatched" if bool(session_payload.get(key)) else "Not dispatched"


def _format_skill_list(skills: List[str]) -> str:
    if not skills:
        return "(none)"
    return ", ".join(f"`{skill_id}`" for skill_id in skills)


def _format_excluded_skills(
    excluded: List[str],
    exclusion_reasons: Dict[str, str],
) -> str:
    if not excluded:
        return "(none)"
    parts: List[str] = []
    for skill_id in excluded:
        reason = exclusion_reasons.get(skill_id, "")
        if reason:
            parts.append(f"`{skill_id}` — {reason}")
        else:
            parts.append(f"`{skill_id}`")
    return _truncate(", ".join(parts), _FIELD_VALUE_MAX)


def _lookup_dispatch_plan_for_session(
    session_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Best-effort dispatch plan lookup for embed rendering."""
    ticket_id = str(session_payload.get("execution_ticket_id") or "").strip()
    if not ticket_id:
        return None
    try:
        from agent.coo.gateway_execution_dispatcher import get_dispatch_plan_for_gateway_ticket

        return get_dispatch_plan_for_gateway_ticket(ticket_id)
    except Exception as exc:
        logger.warning(
            "COO approval dispatch plan lookup failed for ticket %s: %s",
            ticket_id[:64],
            exc,
        )
        return None


def _lookup_latest_dry_run_for_session(
    session_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Best-effort latest dry-run lookup for embed rendering."""
    ticket_id = str(session_payload.get("execution_ticket_id") or "").strip()
    if not ticket_id:
        return None
    try:
        from agent.coo.gateway_execution_runtime import get_latest_dry_run_for_gateway_ticket

        return get_latest_dry_run_for_gateway_ticket(ticket_id)
    except Exception as exc:
        logger.warning(
            "COO approval dry-run lookup failed for ticket %s: %s",
            ticket_id[:64],
            exc,
        )
        return None


def _lookup_latest_execution_review_for_session(
    session_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Best-effort latest execution review lookup for embed rendering."""
    ticket_id = str(session_payload.get("execution_ticket_id") or "").strip()
    if not ticket_id:
        return None
    try:
        from agent.coo.gateway_execution_execute import (
            get_latest_execute_request_for_gateway_ticket,
        )

        return get_latest_execute_request_for_gateway_ticket(ticket_id)
    except Exception as exc:
        logger.warning(
            "COO approval execution review lookup failed for ticket %s: %s",
            ticket_id[:64],
            exc,
        )
        return None


def _has_completed_dry_run_with_planned_skills(
    run_payload: Optional[Dict[str, Any]],
) -> bool:
    if run_payload is None:
        return False
    if str(run_payload.get("status") or "").strip().lower() != "completed":
        return False
    if not bool(run_payload.get("dry_run", True)):
        return False
    dispatchable_results = list(run_payload.get("dispatchable_results") or [])
    return any(
        str(result.get("status") or "").strip().lower() == "planned"
        for result in dispatchable_results
    )


def _execution_review_status_label(
    review_payload: Optional[Dict[str, Any]],
) -> str:
    if review_payload is None:
        return "Not prepared"
    gate = review_payload.get("gate")
    if not isinstance(gate, dict):
        return "Not prepared"
    gate_status = str(gate.get("status") or "").strip().lower()
    if gate_status == "pending":
        return "Ready — Not Executed"
    if gate_status == "approved":
        return "Approved — Not Executed"
    if gate_status == "rejected":
        return "Rejected — Not Executed"
    return "Not prepared"


def _format_dry_run_skill_results(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "(none)"
    parts: List[str] = []
    for result in results:
        skill_id = str(result.get("skill_id") or "unknown")
        status = str(result.get("status") or "unknown")
        parts.append(f"`{skill_id}`: {status} (dry-run)")
    return _truncate(", ".join(parts), _FIELD_VALUE_MAX)


def _calculate_embed_size(embed_payload: Dict[str, Any]) -> int:
    """Sum characters across embed title, description, field names/values, and footer."""
    total = len(str(embed_payload.get("title") or ""))
    total += len(str(embed_payload.get("description") or ""))
    footer = embed_payload.get("footer") or {}
    if isinstance(footer, dict):
        total += len(str(footer.get("text") or ""))
    for field in embed_payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        total += len(str(field.get("name") or ""))
        total += len(str(field.get("value") or ""))
    return total


def _enforce_embed_total_length(
    embed_payload: Dict[str, Any],
    max_total: int = _EMBED_TOTAL_MAX,
) -> Dict[str, Any]:
    """Shrink description and field values until the aggregate embed fits Discord's limit."""
    if _calculate_embed_size(embed_payload) <= max_total:
        return embed_payload

    description = str(embed_payload.get("description") or "")
    if description:
        overhead = _calculate_embed_size({**embed_payload, "description": ""})
        budget = max(0, max_total - overhead)
        if budget <= 3:
            embed_payload["description"] = ""
        else:
            embed_payload["description"] = _truncate(description, min(len(description), budget))

    while _calculate_embed_size(embed_payload) > max_total:
        fields = embed_payload.get("fields") or []
        if not fields:
            break
        idx = max(
            range(len(fields)),
            key=lambda i: len(str(fields[i].get("value") or "")),
        )
        field = fields[idx]
        value = str(field.get("value") or "")
        if len(value) <= 10:
            break
        reduce_by = _calculate_embed_size(embed_payload) - max_total + 1
        new_max = max(10, len(value) - reduce_by)
        field["value"] = _truncate(value, new_max)

    return embed_payload


def build_coo_approval_session_payload(
    report: CEOApprovalReport,
    orchestration_result: COOOrchestrationResult,
    discord_user_id: DiscordSnowflake,
    discord_channel_id: DiscordSnowflake,
    store: Optional["CEOApprovalSessionStore"] = None,
) -> Optional[Dict[str, Any]]:
    """Prepare a COO approval session payload for Discord handler wiring.

    Returns a session ``dict`` when Phase 5C policy allows session creation,
    otherwise ``None`` (for example ``NOT_STARTED`` reports). Does not send
    Discord messages or create Execution Tickets.
    """
    return create_discord_approval_session(
        report,
        orchestration_result,
        discord_user_id=normalize_discord_snowflake(discord_user_id),
        discord_channel_id=normalize_discord_snowflake(discord_channel_id),
        store=store,
    )


def build_coo_approval_embed_payload(
    session_payload: Dict[str, Any],
    plan_payload: Optional[Dict[str, Any]] = None,
    run_payload: Optional[Dict[str, Any]] = None,
    review_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a pure-dict Discord embed payload for a COO approval session."""
    session_id = _require_session_id(session_payload)
    plan = plan_payload if plan_payload is not None else _lookup_dispatch_plan_for_session(
        session_payload
    )
    run = run_payload if run_payload is not None else _lookup_latest_dry_run_for_session(
        session_payload
    )
    review = (
        review_payload
        if review_payload is not None
        else _lookup_latest_execution_review_for_session(session_payload)
    )
    task_kind = _truncate(session_payload.get("task_kind", ""), 128)
    run_date = _truncate(session_payload.get("run_date", ""), 64)
    status = _truncate(session_payload.get("status", ""), 64)
    requester_id = _truncate(session_payload.get("requester_id", ""), _INLINE_FIELD_VALUE_MAX)
    channel_id = _truncate(session_payload.get("channel_id", ""), _INLINE_FIELD_VALUE_MAX)
    runtime_status = str(session_payload.get("runtime_status") or "").strip()
    report_status = str(session_payload.get("report_status") or "").strip()

    description_parts = [
        "Review the COO orchestration outcome before approving or rejecting.",
    ]
    if task_kind:
        description_parts.append(f"**Task:** `{task_kind}`")
    if run_date:
        description_parts.append(f"**Run date:** `{run_date}`")
    description = _truncate("\n".join(description_parts), _DESCRIPTION_MAX)

    fields: List[Dict[str, Any]] = [
        {
            "name": "Session ID",
            "value": _truncate(session_id, _FIELD_VALUE_MAX),
            "inline": False,
        },
        {
            "name": "Status",
            "value": status or "unknown",
            "inline": True,
        },
        {
            "name": "Requester",
            "value": requester_id or "unknown",
            "inline": True,
        },
        {
            "name": "Channel",
            "value": channel_id or "unknown",
            "inline": True,
        },
    ]
    if report_status:
        fields.append(
            {
                "name": "Report Status",
                "value": _truncate(report_status, _INLINE_FIELD_VALUE_MAX),
                "inline": True,
            }
        )
    if runtime_status:
        fields.append(
            {
                "name": "Runtime Status",
                "value": _truncate(runtime_status, _INLINE_FIELD_VALUE_MAX),
                "inline": True,
            }
        )
    fields.extend(
        [
            {
                "name": "Execution Ticket",
                "value": _execution_ticket_label(session_payload),
                "inline": True,
            },
            {
                "name": "Execution",
                "value": _dispatch_label(session_payload, "execution_dispatched"),
                "inline": True,
            },
            {
                "name": "Publish",
                "value": _dispatch_label(session_payload, "publish_dispatched"),
                "inline": True,
            },
        ]
    )
    if plan:
        fields.extend(
            [
                {
                    "name": "Plan Status",
                    "value": "Plan Ready — Not Executed",
                    "inline": False,
                },
                {
                    "name": "Dispatchable",
                    "value": _truncate(
                        _format_skill_list(list(plan.get("dispatchable_skills") or [])),
                        _FIELD_VALUE_MAX,
                    ),
                    "inline": False,
                },
                {
                    "name": "Preview Only",
                    "value": _truncate(
                        _format_skill_list(list(plan.get("preview_only_skills") or [])),
                        _FIELD_VALUE_MAX,
                    ),
                    "inline": False,
                },
                {
                    "name": "Excluded",
                    "value": _format_excluded_skills(
                        list(plan.get("excluded_skills") or []),
                        dict(plan.get("exclusion_reasons") or {}),
                    ),
                    "inline": False,
                },
                {
                    "name": "Requested By",
                    "value": _truncate(plan.get("requested_by", ""), _INLINE_FIELD_VALUE_MAX)
                    or "unknown",
                    "inline": True,
                },
                {
                    "name": "Requested At",
                    "value": _truncate(plan.get("requested_at", ""), _INLINE_FIELD_VALUE_MAX)
                    or "unknown",
                    "inline": True,
                },
            ]
        )
    if run:
        fields.extend(
            [
                {
                    "name": "Dry Run Status",
                    "value": "Dry Run Preview — Not Executed",
                    "inline": False,
                },
                {
                    "name": "Dispatchable Results",
                    "value": _format_dry_run_skill_results(
                        list(run.get("dispatchable_results") or [])
                    ),
                    "inline": False,
                },
                {
                    "name": "Preview Results",
                    "value": _format_dry_run_skill_results(
                        list(run.get("preview_results") or [])
                    ),
                    "inline": False,
                },
                {
                    "name": "Blocked Skills",
                    "value": _truncate(
                        _format_skill_list(list(run.get("blocked_skills") or [])),
                        _FIELD_VALUE_MAX,
                    ),
                    "inline": False,
                },
                {
                    "name": "Run Summary",
                    "value": _truncate(run.get("summary", ""), _FIELD_VALUE_MAX) or "(none)",
                    "inline": False,
                },
                {
                    "name": "Run At",
                    "value": _truncate(
                        run.get("finished_at") or run.get("started_at") or "",
                        _INLINE_FIELD_VALUE_MAX,
                    )
                    or "unknown",
                    "inline": True,
                },
            ]
        )

    if review:
        execute_request = dict(review.get("execute_request") or {})
        gate = review.get("gate") if isinstance(review.get("gate"), dict) else None
        fields.extend(
            [
                {
                    "name": "Execution Review Status",
                    "value": _execution_review_status_label(review),
                    "inline": False,
                },
                {
                    "name": "Target Skills",
                    "value": _truncate(
                        _format_skill_list(list(execute_request.get("target_skills") or [])),
                        _FIELD_VALUE_MAX,
                    ),
                    "inline": False,
                },
                {
                    "name": "Review Requested At",
                    "value": _truncate(
                        execute_request.get("requested_at", ""),
                        _INLINE_FIELD_VALUE_MAX,
                    )
                    or "unknown",
                    "inline": True,
                },
            ]
        )
        if gate is not None:
            decided_by = str(gate.get("decided_by") or "").strip()
            if decided_by:
                fields.append(
                    {
                        "name": "Review Decided By",
                        "value": _truncate(decided_by, _INLINE_FIELD_VALUE_MAX),
                        "inline": True,
                    }
                )
            gate_reason = str(gate.get("reason") or "").strip()
            if gate_reason:
                fields.append(
                    {
                        "name": "Review Reason",
                        "value": _truncate(gate_reason, _FIELD_VALUE_MAX),
                        "inline": False,
                    }
                )

    if review:
        footer_text = _EMBED_FOOTER_TEXT_EXECUTION_REVIEW
    elif run:
        footer_text = _EMBED_FOOTER_TEXT_DRY_RUN
    elif plan:
        footer_text = _EMBED_FOOTER_TEXT
    else:
        footer_text = _EMBED_FOOTER_TEXT_NO_PLAN
    embed_payload = {
        "title": _EMBED_TITLE,
        "description": description,
        "color": _EMBED_COLOR,
        "fields": fields,
        "footer": {"text": footer_text},
    }
    return _enforce_embed_total_length(embed_payload)


def _build_coo_approval_custom_id(action: str, session_id: str) -> str:
    custom_id = f"{_COO_APPROVAL_CUSTOM_ID_PREFIX}:{action}:{session_id}"
    if len(custom_id) > _CUSTOM_ID_MAX:
        raise ValueError(
            f"COO approval custom_id exceeds Discord limit ({_CUSTOM_ID_MAX}): "
            f"{len(custom_id)} chars"
        )
    return custom_id


def parse_coo_approval_custom_id(custom_id: str) -> Dict[str, str]:
    """Parse ``coo_approval:<action>:<session_id>`` button custom IDs."""
    parts = str(custom_id or "").split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid COO approval custom_id: {custom_id!r}")
    prefix, action, session_id = parts
    if prefix != _COO_APPROVAL_CUSTOM_ID_PREFIX:
        raise ValueError(f"Invalid COO approval custom_id prefix: {prefix!r}")
    if action not in _ALLOWED_COO_APPROVAL_ACTIONS:
        raise ValueError(f"Invalid COO approval action: {action!r}")
    if not session_id.strip():
        raise ValueError("COO approval custom_id missing session_id")
    return {"prefix": prefix, "action": action, "session_id": session_id}


def coo_approval_error_message(exc: Exception) -> str:
    """Map approval-session exceptions to user-facing ephemeral messages."""
    if isinstance(exc, KeyError):
        return _ERR_SESSION_NOT_FOUND
    if isinstance(exc, ValueError):
        text = str(exc).lower()
        if "not authorized" in text:
            return _ERR_NOT_ALLOWED
        if "expired" in text:
            return _ERR_SESSION_EXPIRED
        if "cannot approve" in text or "cannot reject" in text:
            if "expired" in text:
                return _ERR_SESSION_EXPIRED
            return "Approval session is no longer pending."
    return _ERR_GENERIC


def _is_terminal_approval_status(status: str) -> bool:
    return str(status or "").strip().lower() in _TERMINAL_APPROVAL_STATUSES


def _should_disable_coo_approval_buttons(session_payload: Dict[str, Any]) -> bool:
    return _is_terminal_approval_status(str(session_payload.get("status") or ""))


def _should_disable_prepare_plan_button(session_payload: Dict[str, Any]) -> bool:
    status = str(session_payload.get("status") or "").strip().lower()
    ticket_id = str(session_payload.get("execution_ticket_id") or "").strip()
    if status != "approved":
        return True
    return not ticket_id


def _should_disable_dry_run_preview_button(session_payload: Dict[str, Any]) -> bool:
    status = str(session_payload.get("status") or "").strip().lower()
    ticket_id = str(session_payload.get("execution_ticket_id") or "").strip()
    if status != "approved":
        return True
    if not ticket_id:
        return True
    plan = _lookup_dispatch_plan_for_session(session_payload)
    return plan is None


def _should_disable_execution_review_button(session_payload: Dict[str, Any]) -> bool:
    status = str(session_payload.get("status") or "").strip().lower()
    ticket_id = str(session_payload.get("execution_ticket_id") or "").strip()
    if status != "approved":
        return True
    if not ticket_id:
        return True
    plan = _lookup_dispatch_plan_for_session(session_payload)
    if plan is None:
        return True
    run = _lookup_latest_dry_run_for_session(session_payload)
    return not _has_completed_dry_run_with_planned_skills(run)


def _should_show_execution_review_decision_buttons(
    session_payload: Dict[str, Any],
) -> bool:
    review = _lookup_latest_execution_review_for_session(session_payload)
    if review is None:
        return False
    gate = review.get("gate")
    if not isinstance(gate, dict):
        return False
    return str(gate.get("status") or "").strip().lower() == "pending"


def _assert_session_owner(
    session_payload: Dict[str, Any],
    user_id: str,
    session_id: str,
) -> None:
    owner = str(session_payload.get("requester_id") or "")
    if str(user_id) != owner:
        raise ValueError(
            f"Requester {user_id!r} is not authorized for session {session_id} "
            f"(owner: {owner!r})"
        )


def _ticket_id_from_session(session_payload: Dict[str, Any]) -> str:
    ticket_id = str(session_payload.get("execution_ticket_id") or "").strip()
    if not ticket_id:
        raise ValueError("Execution ticket not found for session")
    return ticket_id


def _terminal_status_message(status: str) -> str:
    normalized = str(status or "closed").strip().lower()
    return f"Approval session is already {normalized}."


def execute_coo_approval_button_action(
    *,
    action: str,
    session_id: str,
    discord_user_id: DiscordSnowflake,
    store: Optional["CEOApprovalSessionStore"] = None,
) -> Dict[str, Any]:
    """Run approve/reject/refresh against the in-memory approval session store."""
    from agent.coo.discord_approval_adapter import (
        approve_discord_session,
        get_discord_approval_session,
        reject_discord_session,
    )

    normalized_action = str(action).strip().lower()
    if normalized_action not in _ALLOWED_COO_APPROVAL_ACTIONS:
        raise ValueError(f"Invalid COO approval action: {action!r}")

    user_id = normalize_discord_snowflake(discord_user_id)

    if normalized_action == "refresh":
        session = get_discord_approval_session(session_id, store=store)
        if session is None:
            raise KeyError(session_id)
        return session

    if normalized_action == "prepare_plan":
        existing = get_discord_approval_session(session_id, store=store)
        if existing is None:
            raise KeyError(session_id)
        owner = str(existing.get("requester_id") or "")
        if str(user_id) != owner:
            raise ValueError(
                f"Requester {user_id!r} is not authorized for session {session_id} "
                f"(owner: {owner!r})"
            )
        if _should_disable_prepare_plan_button(existing):
            raise ValueError(
                f"Cannot prepare plan for session {session_id} in status {existing.get('status')}"
            )
        from agent.coo.gateway_execution_dispatcher import (
            create_dispatch_plan_for_gateway_session,
        )

        create_dispatch_plan_for_gateway_session(
            session_id,
            requester_id=user_id,
            reason="discord prepare plan",
        )
        refreshed = get_discord_approval_session(session_id, store=store)
        if refreshed is None:
            raise KeyError(session_id)
        return refreshed

    if normalized_action == "dry_run_preview":
        existing = get_discord_approval_session(session_id, store=store)
        if existing is None:
            raise KeyError(session_id)
        owner = str(existing.get("requester_id") or "")
        if str(user_id) != owner:
            raise ValueError(
                f"Requester {user_id!r} is not authorized for session {session_id} "
                f"(owner: {owner!r})"
            )
        if _should_disable_dry_run_preview_button(existing):
            raise ValueError(
                f"Cannot dry run preview for session {session_id} "
                f"in status {existing.get('status')}"
            )
        from agent.coo.gateway_execution_runtime import start_dry_run_for_gateway_session

        start_dry_run_for_gateway_session(
            session_id,
            requester_id=user_id,
            reason="discord dry run preview",
        )
        refreshed = get_discord_approval_session(session_id, store=store)
        if refreshed is None:
            raise KeyError(session_id)
        return refreshed

    if normalized_action == "execution_review":
        existing = get_discord_approval_session(session_id, store=store)
        if existing is None:
            raise KeyError(session_id)
        _assert_session_owner(existing, user_id, session_id)
        if _should_disable_execution_review_button(existing):
            raise ValueError(
                f"Cannot execution review for session {session_id} "
                f"in status {existing.get('status')}"
            )
        from agent.coo.gateway_execution_execute import (
            create_execute_request_for_gateway_session,
        )

        create_execute_request_for_gateway_session(
            session_id,
            requester_id=user_id,
            reason="discord execution review",
        )
        refreshed = get_discord_approval_session(session_id, store=store)
        if refreshed is None:
            raise KeyError(session_id)
        return refreshed

    if normalized_action in ("approve_execution_review", "reject_execution_review"):
        existing = get_discord_approval_session(session_id, store=store)
        if existing is None:
            raise KeyError(session_id)
        _assert_session_owner(existing, user_id, session_id)
        ticket_id = _ticket_id_from_session(existing)
        from agent.coo.gateway_execution_execute import (
            approve_execute_gate_for_gateway_request,
            get_latest_execute_request_for_gateway_ticket,
            reject_execute_gate_for_gateway_request,
        )

        review = get_latest_execute_request_for_gateway_ticket(ticket_id)
        if review is None:
            raise KeyError(f"Execute request not found for ticket: {ticket_id}")
        gate = review.get("gate")
        if not isinstance(gate, dict):
            raise ValueError(
                f"Execute gate not found for request: "
                f"{review.get('execute_request', {}).get('execute_request_id')}"
            )
        if str(gate.get("status") or "").strip().lower() != "pending":
            raise ValueError(
                f"Cannot {normalized_action.replace('_', ' ')} for gate in status "
                f"{gate.get('status')}"
            )
        execute_request_id = str(
            (review.get("execute_request") or {}).get("execute_request_id") or ""
        ).strip()
        if not execute_request_id:
            raise KeyError(f"Execute request not found for ticket: {ticket_id}")

        if normalized_action == "approve_execution_review":
            approve_execute_gate_for_gateway_request(
                execute_request_id,
                reviewer_id=user_id,
            )
        else:
            reject_execute_gate_for_gateway_request(
                execute_request_id,
                reviewer_id=user_id,
                reason="discord reject execution review",
            )
        refreshed = get_discord_approval_session(session_id, store=store)
        if refreshed is None:
            raise KeyError(session_id)
        return refreshed

    existing = get_discord_approval_session(session_id, store=store)
    if existing is None:
        raise KeyError(session_id)
    if _should_disable_coo_approval_buttons(existing) and normalized_action in ("approve", "reject"):
        raise ValueError(
            f"Cannot {normalized_action} session {session_id} in status {existing.get('status')}"
        )
    if existing.get("status") == "expired":
        raise ValueError(f"Cannot {normalized_action} expired session {session_id}")

    if normalized_action == "approve":
        return approve_discord_session(session_id, user_id, store=store)
    return reject_discord_session(session_id, user_id, reason="", store=store)


async def _respond_coo_approval_ephemeral(interaction: Any, message: str) -> None:
    response = getattr(interaction, "response", None)
    if response is None or not hasattr(response, "send_message"):
        return
    if hasattr(response, "is_done") and response.is_done():
        followup = getattr(interaction, "followup", None)
        if followup is not None and hasattr(followup, "send"):
            await followup.send(message, ephemeral=True)
        return
    await response.send_message(message, ephemeral=True)


async def _try_update_interaction_message(
    interaction: Any,
    session_payload: Dict[str, Any],
    store: Optional["CEOApprovalSessionStore"] = None,
) -> bool:
    embed_payload = build_coo_approval_embed_payload(session_payload)
    embed = build_discord_embed_from_payload(embed_payload)
    if isinstance(embed, dict):
        return False
    component_payloads = build_coo_approval_components(session_payload)
    view = build_discord_view_from_components(component_payloads, store=store)
    edit_kwargs: Dict[str, Any] = {"embed": embed}
    if view is not None and not isinstance(view, dict):
        edit_kwargs["view"] = view
    response = getattr(interaction, "response", None)
    if response is None:
        return False
    try:
        if hasattr(response, "is_done") and not response.is_done() and hasattr(response, "edit_message"):
            await response.edit_message(**edit_kwargs)
            return True
        message = getattr(interaction, "message", None)
        if message is not None and hasattr(message, "edit"):
            await message.edit(**edit_kwargs)
            return True
    except Exception as exc:
        logger.warning("COO approval interaction update failed: %s", exc)
    return False


def _coo_approval_button_component(
    *,
    label: str,
    style: str,
    custom_id: str,
    disabled: bool = False,
) -> Dict[str, Any]:
    component: Dict[str, Any] = {
        "type": "button",
        "label": label,
        "style": style,
        "custom_id": custom_id,
    }
    if disabled:
        component["disabled"] = True
    return component


def build_coo_approval_components(session_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build pure-dict Discord button payloads for COO approval interactions."""
    session_id = _require_session_id(session_payload)
    terminal_disabled = _should_disable_coo_approval_buttons(session_payload)
    prepare_plan_disabled = _should_disable_prepare_plan_button(session_payload)
    dry_run_preview_disabled = _should_disable_dry_run_preview_button(session_payload)
    execution_review_disabled = _should_disable_execution_review_button(session_payload)
    show_execution_review_decisions = _should_show_execution_review_decision_buttons(
        session_payload
    )
    components = [
        _coo_approval_button_component(
            label="Approve",
            style="success",
            custom_id=_build_coo_approval_custom_id("approve", session_id),
            disabled=terminal_disabled,
        ),
        _coo_approval_button_component(
            label="Reject",
            style="danger",
            custom_id=_build_coo_approval_custom_id("reject", session_id),
            disabled=terminal_disabled,
        ),
        _coo_approval_button_component(
            label="Refresh",
            style="secondary",
            custom_id=_build_coo_approval_custom_id("refresh", session_id),
            disabled=terminal_disabled,
        ),
        _coo_approval_button_component(
            label="Prepare Plan",
            style="primary",
            custom_id=_build_coo_approval_custom_id("prepare_plan", session_id),
            disabled=prepare_plan_disabled,
        ),
        _coo_approval_button_component(
            label="Dry Run Preview",
            style="secondary",
            custom_id=_build_coo_approval_custom_id("dry_run_preview", session_id),
            disabled=dry_run_preview_disabled,
        ),
        _coo_approval_button_component(
            label="Execution Review",
            style="primary",
            custom_id=_build_coo_approval_custom_id("execution_review", session_id),
            disabled=execution_review_disabled,
        ),
    ]
    if show_execution_review_decisions:
        components.extend(
            [
                _coo_approval_button_component(
                    label="Approve Execution Review",
                    style="success",
                    custom_id=_build_coo_approval_custom_id(
                        "approve_execution_review",
                        session_id,
                    ),
                ),
                _coo_approval_button_component(
                    label="Reject Execution Review",
                    style="danger",
                    custom_id=_build_coo_approval_custom_id(
                        "reject_execution_review",
                        session_id,
                    ),
                ),
            ]
        )
    return components


def _map_button_style(discord_mod: Any, style_name: str) -> Any:
    style_key = str(style_name or "secondary").lower()
    style_map = {
        "primary": discord_mod.ButtonStyle.primary,
        "success": discord_mod.ButtonStyle.success,
        "danger": discord_mod.ButtonStyle.danger,
        "secondary": discord_mod.ButtonStyle.secondary,
    }
    return style_map.get(style_key, discord_mod.ButtonStyle.secondary)


def _make_coo_approval_button_callback(
    custom_id: str,
    store: Optional["CEOApprovalSessionStore"] = None,
):
    """Return a discord.py button callback that updates approval session state only."""

    async def _callback(interaction: Any) -> None:
        try:
            parsed = parse_coo_approval_custom_id(custom_id)
            user = getattr(interaction, "user", None)
            if user is None or not getattr(user, "id", None):
                await _respond_coo_approval_ephemeral(interaction, _ERR_GENERIC)
                return

            from agent.coo.discord_approval_adapter import get_discord_approval_session

            if parsed["action"] in ("approve", "reject"):
                existing = get_discord_approval_session(parsed["session_id"], store=store)
                if existing is not None and _should_disable_coo_approval_buttons(existing):
                    await _try_update_interaction_message(interaction, existing, store=store)
                    await _respond_coo_approval_ephemeral(
                        interaction,
                        _terminal_status_message(str(existing.get("status") or "closed")),
                    )
                    return

            session_payload = execute_coo_approval_button_action(
                action=parsed["action"],
                session_id=parsed["session_id"],
                discord_user_id=user.id,
                store=store,
            )
            updated = await _try_update_interaction_message(
                interaction,
                session_payload,
                store=store,
            )
            if parsed["action"] == "prepare_plan":
                await _respond_coo_approval_ephemeral(interaction, _PREPARE_PLAN_EPHEMERAL)
            elif parsed["action"] == "dry_run_preview":
                await _respond_coo_approval_ephemeral(interaction, _DRY_RUN_PREVIEW_EPHEMERAL)
            elif parsed["action"] == "execution_review":
                await _respond_coo_approval_ephemeral(interaction, _EXECUTION_REVIEW_EPHEMERAL)
            elif parsed["action"] == "approve_execution_review":
                await _respond_coo_approval_ephemeral(
                    interaction,
                    _APPROVE_EXECUTION_REVIEW_EPHEMERAL,
                )
            elif parsed["action"] == "reject_execution_review":
                await _respond_coo_approval_ephemeral(
                    interaction,
                    _REJECT_EXECUTION_REVIEW_EPHEMERAL,
                )
            elif not updated:
                status = session_payload.get("status", "unknown")
                await _respond_coo_approval_ephemeral(
                    interaction,
                    f"Session status: {status}",
                )
        except Exception as exc:
            logger.warning("COO approval button action failed: %s", exc)
            await _respond_coo_approval_ephemeral(
                interaction,
                coo_approval_error_message(exc),
            )

    return _callback


def build_discord_embed_from_payload(embed_payload: Dict[str, Any]) -> Any:
    """Build a discord.py ``Embed`` when available, otherwise return a dict fallback."""
    discord_mod = _get_discord_module()
    if discord_mod is None:
        return {"_fallback": "embed", **dict(embed_payload)}

    embed = discord_mod.Embed(
        title=str(embed_payload.get("title") or ""),
        description=str(embed_payload.get("description") or ""),
        color=int(embed_payload.get("color") or 0),
    )
    for field in embed_payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        embed.add_field(
            name=str(field.get("name") or ""),
            value=str(field.get("value") or ""),
            inline=bool(field.get("inline", False)),
        )
    footer = embed_payload.get("footer") or {}
    if isinstance(footer, dict) and footer.get("text"):
        embed.set_footer(text=str(footer["text"]))
    return embed


def build_discord_view_from_components(
    component_payloads: List[Dict[str, Any]],
    store: Optional["CEOApprovalSessionStore"] = None,
) -> Any:
    """Build a discord.py ``View`` with COO approval button callbacks when available."""
    discord_mod = _get_discord_module()
    normalized = [dict(component) for component in component_payloads]
    if discord_mod is None:
        return {"_fallback": "view", "components": normalized}

    view = discord_mod.ui.View(timeout=_COO_APPROVAL_VIEW_TIMEOUT_SECONDS)
    for component in normalized:
        button = discord_mod.ui.Button(
            label=str(component.get("label") or "Button"),
            style=_map_button_style(discord_mod, str(component.get("style") or "secondary")),
            custom_id=str(component.get("custom_id") or ""),
            disabled=bool(component.get("disabled", False)),
        )
        button.callback = _make_coo_approval_button_callback(button.custom_id, store=store)
        view.add_item(button)
    return view


def prepare_coo_approval_render_items(
    session_payload: Dict[str, Any],
    store: Optional["CEOApprovalSessionStore"] = None,
) -> tuple[Any, Any]:
    """Build COO approval embed and view objects for Discord render wiring."""
    embed_payload = build_coo_approval_embed_payload(session_payload)
    component_payloads = build_coo_approval_components(session_payload)
    embed = build_discord_embed_from_payload(embed_payload)
    view = build_discord_view_from_components(component_payloads, store=store)
    return embed, view
