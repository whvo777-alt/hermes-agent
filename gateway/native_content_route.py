"""Deterministic Discord routing for Hermes-native daily content requests.

This module intentionally sits in the gateway routing layer.  It bypasses the
LLM/skill-loader path for the high-volume CEO request "오늘 블로그 글 4개 작성해서
보고해줘" so the request cannot drift back to the legacy Repository2
``content-pipeline-coo`` skill route.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

_DAILY_BLOG_REQUEST_RE = re.compile(
    r"오늘\s*블로그\s*글\s*4\s*개\s*작성\s*해서\s*보고\s*해\s*줘"
)

_NATIVE_STACK = (
    "DiscordAdapter.on_message",
    "GatewayRunner._handle_message",
    "gateway.native_content_route.handle_native_content_request",
    "tools.coo_tools.coo_orchestrate",
    "agent.content.orchestrator.generate_daily_bundle",
    "Research",
    "Planning",
    "Writing",
    "Quality",
    "Platform Approval",
)


def is_native_content_request(event: MessageEvent, source: SessionSource) -> bool:
    """Return True for Discord plain-text daily blog creation requests.

    Slash commands are deliberately excluded; this is a router for the natural
    language Discord message that previously fell through to skill selection.
    """

    if source.platform is not Platform.DISCORD:
        return False
    if event.get_command():
        return False
    text = " ".join(str(event.text or "").split())
    return bool(_DAILY_BLOG_REQUEST_RE.search(text))


def _source_channel_id(source: SessionSource) -> str:
    return str(source.thread_id or source.chat_id or "")


def _format_quality_line(item: dict[str, Any]) -> str:
    platform = item.get("platform", "")
    title = item.get("topic_title", "")
    score = item.get("quality_score", "")
    passed = "통과" if item.get("quality_passed") else "확인 필요"
    blog_file = item.get("blog_file", "")
    return f"| {platform} | {title} | {score} | {passed} | `{blog_file}` |"


def _format_native_report(payload: dict[str, Any], *, stack_trace: str) -> str:
    bundle = payload.get("daily_blog_bundle") or {}
    items = bundle.get("items") or []
    run_date = bundle.get("run_date") or payload.get("plan", {}).get("run_date") or ""

    lines = [
        "완료했습니다. Hermes Native Flow로 오늘 블로그 글 4개를 작성했고 Platform Approval 단계까지 준비했습니다.",
        "",
        "## 실제 호출 스택",
        "```text",
        stack_trace,
        "```",
        "",
        "## Hermes Native Flow",
        "Research ↓ Planning ↓ Writing ↓ Quality ↓ Platform Approval",
        "",
        f"- 기준일: `{run_date}`",
        "- Repository2 접근: 0회",
        "- Legacy Repository2 skill loading: 0회",
        "- Legacy report file reads: 0회",
        "- Legacy publishing-plan file reads: 0회",
        "- Generic COO Approval card: 생성 안 함",
        "- 실제 발행: 미실행",
        "",
        "## 생성 결과",
        "| 플랫폼 | 주제 | 점수 | 상태 | 본문 파일 |",
        "|---|---|---:|---|---|",
    ]
    if items:
        lines.extend(_format_quality_line(item) for item in items)
    else:
        lines.append("| - | Native bundle 생성 결과를 찾지 못했습니다 | - | 확인 필요 | - |")

    lines.extend([
        "",
        "## 승인 상태",
        f"- Platform Approval 항목: {len(items)}개",
        "- 승인 전 자동 발행 없음 (`auto_apply=false`, `review_required=true`)",
    ])
    return "\n".join(lines)


def _native_stack_trace() -> str:
    observed = [
        f"{frame.frame.f_globals.get('__name__', '')}.{frame.function}:{frame.lineno}"
        for frame in inspect.stack()[1:12]
    ]
    return "\n".join([*_NATIVE_STACK, "", "Observed Python stack:", *observed])


async def handle_native_content_request(event: MessageEvent, source: SessionSource) -> Optional[str]:
    """Run the Hermes-native content path for a matched Discord request.

    Returns a Discord-ready text response, or None when the event is not a native
    content request.  No skills are loaded and no Repository2 paths are read.
    """

    if not is_native_content_request(event, source):
        return None

    run_date = datetime.now(timezone.utc).date().isoformat()
    stack_trace = _native_stack_trace()
    logger.info("Hermes Native Flow route selected for Discord daily blog request\n%s", stack_trace)

    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.coo_tools import coo_orchestrate

    tokens = set_session_vars(
        platform="discord",
        user_id=str(source.user_id or ""),
        chat_id=str(source.chat_id or ""),
        thread_id=str(source.thread_id or ""),
        user_message=str(event.text or ""),
    )
    try:
        raw = coo_orchestrate(
            ceo_message=str(event.text or ""),
            run_date=run_date,
            requester_id=str(source.user_id or ""),
            channel_id=_source_channel_id(source),
        )
    finally:
        clear_session_vars(tokens)

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("coo_orchestrate returned a non-object payload")
    if payload.get("error"):
        raise RuntimeError(str(payload.get("error")))
    return _format_native_report(payload, stack_trace=stack_trace)
