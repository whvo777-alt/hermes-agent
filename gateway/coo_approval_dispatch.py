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
        return
    if getattr(type(adapter), "send_coo_approval", None) is None:
        return
    if not session_payload:
        return

    async def _dispatch() -> None:
        try:
            result = await adapter.send_coo_approval(
                chat_id,
                session_payload,
                metadata=metadata,
                store=store,
            )
            if not getattr(result, "success", False):
                logger.warning(
                    "COO approval Discord render failed: %s",
                    getattr(result, "error", "unknown error"),
                )
        except Exception as exc:
            logger.warning("COO approval Discord render failed: %s", exc)

    safe_schedule_threadsafe(
        _dispatch(),
        loop,
        logger=logger,
        log_message="send_coo_approval scheduling error",
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
    if tool_name != _COO_ORCHESTRATE_TOOL:
        return
    session_payload = extract_coo_approval_session_from_tool_result(function_result)
    if session_payload is None:
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
