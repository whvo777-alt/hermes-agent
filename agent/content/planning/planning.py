"""Planning stage — ported from multi-content-pipeline/agents/research/planning.js.

Token-saving change (Big-Bang migration goal): receives a deterministic
SUMMARY of the Research output, not the full research text.
"""

from __future__ import annotations

from typing import Dict, Optional

from agent.content.llm_client import call_llm
from agent.content.prompts.prompt_builder import summarize_research

_PLANNING_SYSTEM = """너는 콘텐츠 파이프라인의 기획 에이전트다.
규칙:
- 블로그 1편의 발행 직전 품질 개선 계획만 만든다.
- 계획에는 제목 후보, 핵심 키워드, 독자 문제, 글 구조, 이미지 계획, 모바일 가독성, 신뢰 보강 포인트를 포함한다.
- 플랫폼이 네이버면 이미지/태그/모바일 문단/댓글 또는 이웃 CTA 중심으로 기획한다.
- 플랫폼이 티스토리면 애드센스 승인, 목차, H2/H3, 내부링크 후보, 대표 이미지 ALT 중심으로 기획한다.
- 플랫폼이 블로그스팟이면 slug/meta/FAQ/H2-H3 중심으로 기획한다.
- 민감 카테고리는 주의 표현과 면책 문구를 명시한다.
- 【절대 규칙】 Memory Check의 기작성 글·같은 메인 키워드·비슷한 제목은 절대 재사용하지 않는다. 중복이면 다른 키워드/각도로만 기획한다.
- 【절대 규칙】 대표 이미지는 이전 글과 같은 색·뱃지·레이아웃으로 복제하지 않는다. 글마다 다른 시각 스타일을 명시한다."""


def _format_memory_check(memory_check: Optional[Dict]) -> str:
    if not memory_check:
        return "Memory Check 없음"
    similar = memory_check.get("similarTopics") or []
    similar_text = (
        "\n".join(
            f"- {i.get('date')} / {i.get('platform')} / {i.get('category')} / {i.get('title')} / "
            f"risk={i.get('risk')} / reasons={', '.join(i.get('reasons', []))}"
            for i in similar
        )
        if similar
        else "- 최근 30일 유사 글 없음"
    )
    return (
        f"중복 위험도: {memory_check.get('duplicateRisk')}\n"
        f"차단 여부: {'차단(절대 재작성 금지)' if memory_check.get('blocked') else '허용'}\n"
        f"동일 메인 키워드 여부: {'있음' if memory_check.get('sameKeyword') else '없음'}\n"
        f"기작성 유사 글 수: {memory_check.get('similarCount')}\n{similar_text}"
    )


def run_planning(*, platform_id: str, platform_label: str, category_id: str, category_name: str,
                  target_audience: str, tone: str, topic_title: str, topic_keywords: list,
                  caution_hints: list, research_content: str, memory_check: Optional[Dict] = None) -> str:
    research_summary = summarize_research(research_content)

    user = f"""카테고리: {category_name}
플랫폼: {platform_label}
오늘의 주제: {topic_title}
타겟 독자: {target_audience}
톤앤매너: {tone}
키워드: {', '.join(topic_keywords)}
주의사항: {', '.join(caution_hints) or '없음'}

[리서치 핵심 요약]
{research_summary}

[Memory Check]
{_format_memory_check(memory_check)}

위 리서치 요약과 Memory Check를 기반으로 콘텐츠 기획안을 작성해줘.
포함할 내용:
1. 블로그 1편 기획
2. 플랫폼별 글 구조
3. 대표 이미지/본문 이미지 계획
4. 품질검수 체크포인트
5. 민감 표현/플랫폼 주의사항
6. 최근 유사 글이 있다면 차별화할 제목/관점/구성"""

    body = call_llm(system=_PLANNING_SYSTEM, user=user)

    return f"""---
type: planning
platform: {platform_id}
platform_label: {platform_label}
category: {category_id}
category_name: {category_name}
topic_title: {topic_title}
status: draft
---

{body}"""
