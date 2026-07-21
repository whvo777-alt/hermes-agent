"""Deterministic Discord routing for Hermes-native blog write requests.

Any Discord plaintext ask to write blog/post content must hit this route
before the LLM/skill loop — never ``content-pipeline-coo``, never manual
``/opt/data/blog_drafts`` fallbacks.

Default platform when none named: WordPress only.
Named platforms (one or many) are all selected.
``4개`` → all four launch platforms; bare ``2개`` → wordpress + blogspot.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Optional, Sequence

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

# Write / create intent (Korean + light English).
_WRITE_INTENT_RE = re.compile(
    r"(?:"
    r"작성\s*해서\s*보고\s*해(?:\s*줘)?"
    r"|작성\s*해(?:\s*줘|서)?"
    r"|써(?:\s*줘|서)?"
    r"|올려(?:\s*줘)?"
    r"|만들어(?:\s*줘)?"
    r"|써\s*봐(?:\s*줘)?"
    r"|초안\s*(?:만들|작성|생성)"
    r"|포스팅\s*(?:해|작성|만들)"
    r"|글\s*(?:좀\s*)?(?:써|작성|올려|만들)"
    r"|write|draft|create\s+post"
    r")",
    re.IGNORECASE,
)

# Blog / publishing context.
_BLOG_CONTEXT_RE = re.compile(
    r"(?:"
    r"블로그|blog"
    r"|블로그\s*글|블로그글|블\s*글"
    r"|원고|포스팅|포스트|초안"
    r"|워드\s*프레스|wordpress"
    r"|블로그\s*스팟|blogspot|블로거|blogger"
    r"|티스토리|tistory"
    r"|네이버(?:\s*블로그)?|naver"
    r")",
    re.IGNORECASE,
)

# Legacy explicit phrase (kept for tests / logs).
_DAILY_BLOG_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:(?:워드\s*프레스|wordpress|블로그\s*스팟|blogspot|티스토리|tistory|네이버|naver)\s*)?"
    r"(?:오늘\s*)?블로그\s*글(?:\s*[0-9]+\s*개)?\s*"
    r"(?:작성\s*해서\s*보고\s*해(?:\s*줘)?|작성\s*해(?:\s*줘)?|써(?:\s*줘)?|올려(?:\s*줘)?)"
    r"|"
    r"(?:워드\s*프레스|wordpress|블로그\s*스팟|blogspot|티스토리|tistory|네이버|naver)"
    r"(?:에)?\s*(?:블로그\s*)?글(?:\s*[0-9]+\s*개)?\s*"
    r"(?:작성\s*해서\s*보고\s*해(?:\s*줘)?|작성\s*해(?:\s*줘)?|써(?:\s*줘)?|올려(?:\s*줘)?)"
    r")",
    re.IGNORECASE,
)

# Do not hijack pure ops / approval / status chats.
_EXCLUDE_RE = re.compile(
    r"(?:"
    r"^\s*(?:승인|폐기|수정\s*요청)\s*$"
    r"|status\s*보고|상태만\s*보고"
    r"|스킬\s*설치|skill\s*install"
    r"|토큰\s*갱신|oauth|재인증"
    r")",
    re.IGNORECASE,
)

_ALL_LAUNCH_PLATFORMS: tuple[str, ...] = ("wordpress", "blogspot", "tistory", "naver")
_PLATFORM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("wordpress", r"워드\s*프레스|wordpress"),
    ("blogspot", r"블로그\s*스팟|blogspot|블로거|blogger"),
    ("tistory", r"티스토리|tistory"),
    ("naver", r"네이버|naver"),
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
    "플랫폼별 원고 승인",
)

ApprovalSender = Callable[[dict[str, Any]], Awaitable[Any]]


def resolve_native_platforms(text: str) -> List[str]:
    """Pick platforms for a Native Flow request.

    - ``4개`` → all launch platforms
    - Any named platforms → all of them (WP, Blogspot, Tistory, Naver order)
    - Bare ``2개`` with no names → wordpress + blogspot
    - Default → wordpress only
    """

    normalized = " ".join(str(text or "").split()).lower()
    if re.search(r"4\s*개", normalized):
        return list(_ALL_LAUNCH_PLATFORMS)

    named: List[str] = []
    for platform_id, pattern in _PLATFORM_PATTERNS:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            named.append(platform_id)
    if named:
        return named

    if re.search(r"2\s*개", normalized):
        return ["wordpress", "blogspot"]
    return ["wordpress"]


def is_native_content_request(event: MessageEvent, source: SessionSource) -> bool:
    """True for Discord plaintext blog-write asks — route to Native Flow only."""

    if source.platform is not Platform.DISCORD:
        return False
    if event.get_command():
        return False
    text = " ".join(str(event.text or "").split())
    if not text or _EXCLUDE_RE.search(text):
        return False
    if _DAILY_BLOG_REQUEST_RE.search(text):
        return True
    # Broad catch-all: any write intent + blog/platform context.
    return bool(_WRITE_INTENT_RE.search(text) and _BLOG_CONTEXT_RE.search(text))


def _source_channel_id(source: SessionSource) -> str:
    return str(source.thread_id or source.chat_id or "")


def _format_native_report(
    payload: dict[str, Any],
    *,
    stack_trace: str,
    platforms: Sequence[str],
) -> str:
    bundle = payload.get("daily_blog_bundle") or {}
    items = bundle.get("items") or []
    run_date = bundle.get("run_date") or payload.get("plan", {}).get("run_date") or ""
    platform_label = ", ".join(platforms)

    lines = [
        f"{platform_label} 원고 {len(items)}개를 작성했습니다. 아래 승인 카드에서 확인해 주세요.",
        "",
        f"- 기준일: `{run_date}`",
        "",
        "## 생성 결과",
        "| 플랫폼 | 주제 | 점수 | 상태 |",
        "|---|---|---:|---|",
    ]
    if items:
        lines.extend(
            f"| {item.get('platform', '')} | {item.get('topic_title', '')} "
            f"| {item.get('quality_score', '')} "
            f"| {'통과' if item.get('quality_passed') else '확인 필요'} |"
            for item in items
        )
    else:
        lines.append("| - | Native bundle 생성 결과를 찾지 못했습니다 | - | 확인 필요 |")

    lines.extend([
        "",
        "각 카드의 **승인 / 수정 요청 / 폐기** 버튼을 사용해 주세요.",
        "수정 요청은 학습 기록에 저장되며 업로드되지 않습니다.",
    ])
    return "\n".join(lines)


def _native_stack_trace() -> str:
    observed = [
        f"{frame.frame.f_globals.get('__name__', '')}.{frame.function}:{frame.lineno}"
        for frame in inspect.stack()[1:12]
    ]
    return "\n".join([*_NATIVE_STACK, "", "Observed Python stack:", *observed])


async def handle_native_content_request(
    event: MessageEvent,
    source: SessionSource,
    *,
    approval_sender: Optional[ApprovalSender] = None,
) -> Optional[str]:
    """Run the Hermes-native content path for a matched Discord request.

    Returns a Discord-ready text response, or None when the event is not a native
    content request.  No skills are loaded and no Repository2 paths are read.
    """

    if not is_native_content_request(event, source):
        return None

    text = str(event.text or "")
    platforms = resolve_native_platforms(text)
    run_date = datetime.now(timezone.utc).date().isoformat()
    stack_trace = _native_stack_trace()
    logger.info(
        "Hermes Native Flow route selected for Discord daily blog request platforms=%s\n%s",
        platforms,
        stack_trace,
    )

    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.coo_tools import coo_orchestrate

    tokens = set_session_vars(
        platform="discord",
        user_id=str(source.user_id or ""),
        chat_id=str(source.chat_id or ""),
        thread_id=str(source.thread_id or ""),
        user_message=text,
    )
    try:
        raw = await asyncio.to_thread(
            coo_orchestrate,
            ceo_message=text,
            run_date=run_date,
            requester_id=str(source.user_id or ""),
            channel_id=_source_channel_id(source),
            platforms=platforms,
        )
    finally:
        clear_session_vars(tokens)

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("coo_orchestrate returned a non-object payload")
    if payload.get("error"):
        raise RuntimeError(str(payload.get("error")))
    bundle = payload.get("daily_blog_bundle") or {}
    items = list(bundle.get("items") or [])
    if not items:
        raise RuntimeError(
            "플랫폼별 원고 승인 항목이 생성되지 않았습니다. "
            "레거시 COO 승인 카드로 전환하지 않습니다."
        )
    if approval_sender is not None:
        for item in items:
            result = await approval_sender(item)
            if hasattr(result, "success") and not result.success:
                raise RuntimeError(
                    f"Discord 원고 승인 화면 전송 실패: {getattr(result, 'error', '')}"
                )
            logger.info(
                "Discord 플랫폼별 원고 승인 전송 완료: platform=%s message_id=%s",
                item.get("platform"),
                getattr(result, "message_id", ""),
            )
    return _format_native_report(payload, stack_trace=stack_trace, platforms=platforms)
