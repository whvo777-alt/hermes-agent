"""Discord COO CEO approval handler entry point — Phase 6C-1/6C-2/6C-3.

Prepares in-memory COO approval session payloads, pure-dict embed/component UI
payloads, and optional discord.py Embed/View objects for Discord handler wiring.

This phase only builds Discord UI objects.
Button callbacks are inert.
No approval/rejection is executed here.
No execution ticket is created.
Repository2 is not touched.

This module is for COO CEO approval sessions only.
This module is unrelated to ``tools/approval.py`` ``resolve_gateway_approval()``.
``resolve_gateway_approval`` is the legacy/general exec approval queue used by
``DiscordAdapter.send_exec_approval()`` and ``ExecApprovalView``.
This module does not dispatch execution.
This module does not create execution tickets.
This module does not auto-approve or auto-publish.
This module does not send Discord messages.

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
_EMBED_COLOR = 0x3498DB  # COO approval blue — distinct from exec approval orange (0xE67E22)
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
_INERT_CALLBACK_MESSAGE = "Handler wiring pending."
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


def _map_button_style(discord_mod: Any, style_name: str) -> Any:
    style_key = str(style_name or "secondary").lower()
    style_map = {
        "primary": discord_mod.ButtonStyle.primary,
        "success": discord_mod.ButtonStyle.success,
        "danger": discord_mod.ButtonStyle.danger,
        "secondary": discord_mod.ButtonStyle.secondary,
    }
    return style_map.get(style_key, discord_mod.ButtonStyle.secondary)


def _make_inert_button_callback(custom_id: str):
    """Return an inert discord.py button callback — no approval side effects."""

    async def _callback(interaction: Any) -> None:
        logger.debug(
            "COO approval button pressed (inert callback): custom_id=%s",
            custom_id,
        )
        response = getattr(interaction, "response", None)
        if response is not None and hasattr(response, "send_message"):
            await response.send_message(_INERT_CALLBACK_MESSAGE, ephemeral=True)

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


def build_discord_view_from_components(component_payloads: List[Dict[str, Any]]) -> Any:
    """Build an inert discord.py ``View`` when available, otherwise dict fallback."""
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
        )
        button.callback = _make_inert_button_callback(button.custom_id)
        view.add_item(button)
    return view


def prepare_coo_approval_render_items(
    session_payload: Dict[str, Any],
) -> tuple[Any, Any]:
    """Build COO approval embed and inert view objects for Discord render wiring."""
    embed_payload = build_coo_approval_embed_payload(session_payload)
    component_payloads = build_coo_approval_components(session_payload)
    embed = build_discord_embed_from_payload(embed_payload)
    view = build_discord_view_from_components(component_payloads)
    return embed, view
