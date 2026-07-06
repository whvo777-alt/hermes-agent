"""Gateway COO approval Discord render dispatch — Phase 6C-7.

When ``coo_orchestrate`` returns an ``approval_session`` payload, schedule
``DiscordAdapter.send_coo_approval()`` on the active platform adapter.

Render only: no execution tickets, no Repository 2 access, no approval logic.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

from agent.async_utils import safe_schedule_threadsafe

logger = logging.getLogger(__name__)

_COO_ORCHESTRATE_TOOL = "coo_orchestrate"


def _mask_id(value: Any, *, tail: int = 4) -> str:
    """Mask a Discord snowflake for logs — last ``tail`` digits only."""
    text = str(value or "").strip()
    if not text:
        return "(none)"
    if len(text) <= tail:
        return f"***{text}"
    return f"***{text[-tail:]}"


def _short_session_id(session_id: Any) -> str:
    """Return the first 8 characters of a session id for log correlation."""
    text = str(session_id or "").strip()
    if not text:
        return "(none)"
    return text[:8]


def _adapter_has_send_coo_approval(adapter: Any) -> bool:
    return adapter is not None and getattr(type(adapter), "send_coo_approval", None) is not None


def _log_extract_observability(session_payload: Optional[Dict[str, Any]]) -> None:
    if session_payload is None:
        logger.info("COO approval dispatch extract: approval_session_found=False")
        return
    logger.info(
        "COO approval dispatch extract: approval_session_found=True "
        "session_id=%s status=%s requester_id=%s channel_id=%s",
        _short_session_id(session_payload.get("session_id")),
        session_payload.get("status", ""),
        _mask_id(session_payload.get("requester_id")),
        _mask_id(session_payload.get("channel_id")),
    )


def extract_coo_approval_session_from_tool_result(
    function_result: Any,
) -> Optional[Dict[str, Any]]:
    """Return ``approval_session`` from a ``coo_orchestrate`` tool result."""
    if function_result is None:
        return None
    try:
        if isinstance(function_result, dict):
            payload = function_result
        else:
            payload = json.loads(str(function_result))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    session = payload.get("approval_session")
    if not isinstance(session, dict) or not session:
        return None
    return session


def schedule_coo_approval_discord_render(
    *,
    adapter: Any,
    chat_id: str,
    session_payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    store: Any = None,
    loop: Any,
    run_still_current: Optional[Callable[[], bool]] = None,
) -> None:
    """Schedule ``send_coo_approval`` when the adapter exposes it."""
    if run_still_current is not None and not run_still_current():
        logger.info("COO approval dispatch skip: run_still_current=false")
        return
    if not _adapter_has_send_coo_approval(adapter):
        logger.info("COO approval dispatch skip: adapter missing send_coo_approval")
        return
    if not session_payload:
        logger.info("COO approval dispatch skip: approval_session missing/null/empty")
        return
    if not str(chat_id or "").strip():
        logger.info("COO approval dispatch skip: chat_id missing")
        return

    session_id_short = _short_session_id(session_payload.get("session_id"))

    async def _dispatch() -> None:
        try:
            result = await adapter.send_coo_approval(
                chat_id,
                session_payload,
                metadata=metadata,
                store=store,
            )
            if getattr(result, "success", False):
                logger.info(
                    "COO approval Discord render success session_id=%s",
                    session_id_short,
                )
            else:
                logger.warning(
                    "COO approval Discord render failed session_id=%s error=%s",
                    session_id_short,
                    getattr(result, "error", "unknown error"),
                )
        except Exception as exc:
            logger.warning(
                "COO approval Discord render failed session_id=%s error=%s",
                session_id_short,
                exc,
            )

    scheduled = safe_schedule_threadsafe(
        _dispatch(),
        loop,
        logger=logger,
        log_message="send_coo_approval scheduling error",
    )
    if scheduled is None:
        logger.warning(
            "COO approval dispatch skip: send_coo_approval schedule failed session_id=%s",
            session_id_short,
        )
        return
    logger.info(
        "COO approval Discord render scheduled session_id=%s",
        session_id_short,
    )


def maybe_dispatch_coo_approval_after_tool(
    *,
    tool_name: str,
    function_result: Any,
    adapter: Any,
    chat_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    store: Any = None,
    loop: Any,
    run_still_current: Optional[Callable[[], bool]] = None,
) -> None:
    """Dispatch COO approval UI when a tool result includes a session payload."""
    logger.info(
        "COO approval dispatch: tool_name=%s has_adapter=%s has_send_coo_approval=%s has_chat_id=%s",
        tool_name,
        adapter is not None,
        _adapter_has_send_coo_approval(adapter),
        bool(str(chat_id or "").strip()),
    )
    if tool_name != _COO_ORCHESTRATE_TOOL:
        logger.debug(
            "COO approval dispatch skip: tool_name=%s (not coo_orchestrate)",
            tool_name,
        )
        return
    session_payload = extract_coo_approval_session_from_tool_result(function_result)
    _log_extract_observability(session_payload)
    if session_payload is None:
        logger.info("COO approval dispatch skip: approval_session missing/null/empty")
        return
    schedule_coo_approval_discord_render(
        adapter=adapter,
        chat_id=chat_id,
        session_payload=session_payload,
        metadata=metadata,
        store=store,
        loop=loop,
        run_still_current=run_still_current,
    )
