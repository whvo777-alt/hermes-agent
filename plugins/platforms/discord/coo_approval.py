"""Discord COO CEO approval handler entry point — Phase 6C-1/6C-2.

Prepares in-memory COO approval session payloads and pure-dict embed/component
UI payloads for Discord handler wiring. Future handlers should call
``build_coo_approval_session_payload()``, ``build_coo_approval_embed_payload()``,
and ``build_coo_approval_components()`` — this module does not call the
Discord API or render discord.py objects.

This module is for COO CEO approval sessions only.
This module is unrelated to ``tools/approval.py`` ``resolve_gateway_approval()``.
``resolve_gateway_approval`` is the legacy/general exec approval queue used by
``DiscordAdapter.send_exec_approval()`` and ``ExecApprovalView``.
This module does not dispatch execution.
This module does not create execution tickets.
This module does not auto-approve or auto-publish.
Button payloads are inert until Phase 6C-3 handler wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from agent.coo.approval_report import CEOApprovalReport
from agent.coo.discord_approval_adapter import create_discord_approval_session
from agent.coo.models import COOOrchestrationResult

DiscordSnowflake = Union[str, int]

_EMBED_TITLE = "Hermes COO Approval Required"
_EMBED_COLOR = 0xE67E22  # warm orange — distinct from exec approval embeds
_EMBED_FOOTER_TEXT = "Approval only. No execution will be dispatched."
# Discord hard limits (per-element) used by this builder.
_FIELD_VALUE_MAX = 1024  # max chars per embed field value
_DESCRIPTION_MAX = 3500  # stays under Discord's 4096 description cap with headroom
_EMBED_TOTAL_MAX = 6000  # Discord aggregate embed character budget
# UI readability cap for inline field values — not a Discord API hard limit
# (Discord allows up to 1024 chars per field value via ``_FIELD_VALUE_MAX``).
_INLINE_FIELD_VALUE_MAX = 256
_CUSTOM_ID_MAX = 100
_COO_APPROVAL_CUSTOM_ID_PREFIX = "coo_approval"

if TYPE_CHECKING:
    from agent.coo.approval_session import CEOApprovalSessionStore


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


def build_coo_approval_embed_payload(session_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build a pure-dict Discord embed payload for a COO approval session."""
    session_id = _require_session_id(session_payload)
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

    embed_payload = {
        "title": _EMBED_TITLE,
        "description": description,
        "color": _EMBED_COLOR,
        "fields": fields,
        "footer": {"text": _EMBED_FOOTER_TEXT},
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


def build_coo_approval_components(session_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build pure-dict Discord button payloads for COO approval interactions."""
    session_id = _require_session_id(session_payload)
    return [
        {
            "type": "button",
            "label": "Approve",
            "style": "success",
            "custom_id": _build_coo_approval_custom_id("approve", session_id),
        },
        {
            "type": "button",
            "label": "Reject",
            "style": "danger",
            "custom_id": _build_coo_approval_custom_id("reject", session_id),
        },
        {
            "type": "button",
            "label": "Refresh",
            "style": "secondary",
            "custom_id": _build_coo_approval_custom_id("refresh", session_id),
        },
    ]
