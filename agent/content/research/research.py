"""Research stage — ported from multi-content-pipeline/agents/research/category-research.js.

One LLM call, optionally enriched with live web search evidence when a
search backend is configured (agent/web_search_registry.py). Search failure
or an unconfigured backend falls back to LLM-only research unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List

from agent.content.llm_client import call_llm
from agent.web_search_registry import get_active_search_provider

logger = logging.getLogger(__name__)

_RESEARCH_SYSTEM = """너는 콘텐츠 파이프라인의 리서치 에이전트다.
규칙:
- 네이버 블로그는 국내 검색 의도, 이미지형 글, 후기/비교/팁형 키워드를 중시한다.
- 티스토리는 애드센스 승인, 목차, 내부링크 후보, 정보 신뢰성을 중시한다.
- 블로그스팟은 구글 SEO, URL 슬러그, 메타 디스크립션, FAQ 가능성을 중시한다.
- 재테크/건강/육아 카테고리는 민감 표현과 면책 필요성을 반드시 표시한다.
- 아래 "검색 근거 자료"가 주어지면 이는 참고용 사실관계 확인 자료일 뿐이다.
  절대 원문을 그대로 옮겨 적지 말고, 반드시 네 언어로 재구성/요약해서 반영해라.
- 스니펫 문장을 5단어 이상 연속으로 그대로 베끼지 마라.
- 여러 출처의 내용을 종합해 새로운 문장으로 다시 써라. 특정 출처 하나의
  문장 구조를 그대로 따라가지 마라.
- 검색 근거 자료가 주어지지 않으면(검색 비활성/실패) 카테고리와 주제 설정
  기반의 시뮬레이션 리포트임을 명시한다."""

_MAX_QUERIES = 3
_RESULTS_PER_QUERY = 5
_MAX_EVIDENCE_ITEMS = 8


def _build_search_queries(*, topic_title: str, topic_keywords: list, category_name: str) -> List[str]:
    """Build up to _MAX_QUERIES search queries from existing pipeline inputs.

    No LLM call — keeps the added cost to network I/O only.
    """
    candidates = [topic_title]
    if topic_keywords:
        candidates.append(f"{topic_title} {topic_keywords[0]}")
        candidates.append(f"{category_name} {topic_keywords[0]} 2026")

    seen: set = set()
    queries: List[str] = []
    for raw in candidates:
        q = " ".join(str(raw or "").split())
        if q and q not in seen:
            seen.add(q)
            queries.append(q)
    return queries[:_MAX_QUERIES]


async def _run_searches(provider, queries: List[str]) -> List[Dict[str, Any]]:
    """Run all queries in parallel; return deduped hits (by URL).

    Any per-query failure just yields no hits for that query — never raises.
    """

    async def _search_one(query: str) -> List[Dict[str, Any]]:
        try:
            result = await asyncio.to_thread(provider.search, query, _RESULTS_PER_QUERY)
        except Exception as exc:  # noqa: BLE001 — provider failure -> no hits
            logger.warning("Research web search failed for %r: %s", query, exc)
            return []
        if not isinstance(result, dict) or not result.get("success"):
            logger.info(
                "Research web search unsuccessful for %r: %s",
                query, (result or {}).get("error", "unknown error"),
            )
            return []
        return list((result.get("data") or {}).get("web") or [])

    per_query_hits = await asyncio.gather(*(_search_one(q) for q in queries))

    seen_urls: set = set()
    evidence: List[Dict[str, Any]] = []
    for hits in per_query_hits:
        for hit in hits:
            url = str(hit.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            evidence.append(hit)
            if len(evidence) >= _MAX_EVIDENCE_ITEMS:
                return evidence
    return evidence


def _format_evidence_block(evidence: List[Dict[str, Any]]) -> str:
    if not evidence:
        return ""
    lines = ["검색 근거 자료 (요약/재구성 참고용 — 그대로 베끼지 말 것):", ""]
    for item in evidence:
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        desc = str(item.get("description") or "").strip()
        lines.append(f"- {title} ({url}): {desc}")
    return "\n".join(lines)


def _format_sources_section(evidence: List[Dict[str, Any]]) -> str:
    if not evidence:
        return ""
    lines = ["", "## 참고 출처 (검색 기반)", ""]
    for item in evidence:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        title = str(item.get("title") or "").strip() or url
        lines.append(f"- [{title}]({url})")
    return "\n".join(lines)


def extract_source_links(research_content: str, *, limit: int = 6) -> List[Dict[str, str]]:
    """조사 결과에서 실제로 본 주소만 뽑는다. 없으면 빈 목록."""
    out: List[Dict[str, str]] = []
    seen: set = set()
    for match in re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", str(research_content or "")):
        title = match.group(1).strip()
        url = match.group(2).strip()
        if not title or not url or url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url})
        if len(out) >= limit:
            break
    return out


def run_research(*, platform_id: str, platform_label: str, category_id: str, category_name: str,
                  target_audience: str, tone: str, topic_title: str, topic_keywords: list,
                  category_keywords: list, caution_hints: list) -> str:
    provider = get_active_search_provider()
    queries = _build_search_queries(
        topic_title=topic_title, topic_keywords=topic_keywords, category_name=category_name,
    )

    evidence: List[Dict[str, Any]] = []
    if provider is not None and queries:
        try:
            # NOTE: run_research() is a sync function that internally spins up
            # its own event loop via asyncio.run() to parallelize search calls.
            # This is only safe because every current caller reaches this function
            # via asyncio.to_thread() (see native_content_route.py -> coo_orchestrate
            # -> ... -> run_research), i.e. always from a plain thread with no
            # running event loop. If you ever call run_research() directly from
            # inside an async function/coroutine without asyncio.to_thread, this
            # will raise RuntimeError: asyncio.run() cannot be called from a
            # running event loop.
            evidence = asyncio.run(_run_searches(provider, queries))
        except Exception as exc:  # noqa: BLE001 — search must never break research
            logger.warning("Research web search stage failed entirely: %s", exc)
            evidence = []

    search_used = bool(evidence)
    backend_name = provider.name if provider is not None else "none"
    fallback_notice = (
        ""
        if search_used
        else (
            "\n\n> ⚠️ 웹 검색이 비활성화되어 있거나 실패했습니다. 이 리포트는 LLM "
            "내부 지식만으로 작성된 것이며 최신 정보가 반영되지 않았을 수 있습니다 (미검증)."
        )
    )
    evidence_block = _format_evidence_block(evidence)

    user = f"""카테고리: {category_name}
플랫폼: {platform_label}
오늘의 주제: {topic_title}
타겟 독자: {target_audience}
톤앤매너: {tone}
키워드: {', '.join(topic_keywords)}
카테고리 기본 키워드: {', '.join(category_keywords)}
주의사항: {', '.join(caution_hints) or '없음'}

{evidence_block}

이 주제를 위한 블로그 리서치 리포트를 작성해줘.
포함할 내용:
1. 주제 적합 키워드 5개
2. 독자 검색 의도
3. 플랫폼별 SEO 주의사항
4. 민감 표현/주의사항
5. 글에서 반드시 답해야 할 질문 3개
6. {"실시간 검색 근거를 반영했음을 알리는" if search_used else "시뮬레이션 리포트임을 알리는"} 운영 메모"""

    body = call_llm(system=_RESEARCH_SYSTEM, user=user)
    sources_section = _format_sources_section(evidence)

    return f"""---
type: research
platform: {platform_id}
platform_label: {platform_label}
category: {category_id}
category_name: {category_name}
topic_title: {topic_title}
status: draft
search_backend: {backend_name}
search_used: {str(search_used).lower()}
---

{body}{sources_section}{fallback_notice}"""
