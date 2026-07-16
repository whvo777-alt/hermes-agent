"""Research stage — ported from multi-content-pipeline/agents/research/category-research.js.

One LLM call. Output is frontmatter + markdown research report.
"""

from __future__ import annotations

from agent.content.llm_client import call_llm

_RESEARCH_SYSTEM = """너는 콘텐츠 파이프라인의 리서치 에이전트다.
규칙:
- 네이버 블로그는 국내 검색 의도, 이미지형 글, 후기/비교/팁형 키워드를 중시한다.
- 티스토리는 애드센스 승인, 목차, 내부링크 후보, 정보 신뢰성을 중시한다.
- 블로그스팟은 구글 SEO, URL 슬러그, 메타 디스크립션, FAQ 가능성을 중시한다.
- 재테크/건강/육아 카테고리는 민감 표현과 면책 필요성을 반드시 표시한다.
- 실제 웹 검색 전 단계이므로 카테고리와 주제 설정 기반의 시뮬레이션 리포트임을 명시한다."""


def run_research(*, platform_id: str, platform_label: str, category_id: str, category_name: str,
                  target_audience: str, tone: str, topic_title: str, topic_keywords: list,
                  category_keywords: list, caution_hints: list) -> str:
    user = f"""카테고리: {category_name}
플랫폼: {platform_label}
오늘의 주제: {topic_title}
타겟 독자: {target_audience}
톤앤매너: {tone}
키워드: {', '.join(topic_keywords)}
카테고리 기본 키워드: {', '.join(category_keywords)}
주의사항: {', '.join(caution_hints) or '없음'}

이 주제를 위한 블로그 리서치 리포트를 작성해줘.
포함할 내용:
1. 주제 적합 키워드 5개
2. 독자 검색 의도
3. 플랫폼별 SEO 주의사항
4. 민감 표현/주의사항
5. 글에서 반드시 답해야 할 질문 3개
6. 시뮬레이션 리서치임을 알리는 운영 메모"""

    body = call_llm(system=_RESEARCH_SYSTEM, user=user)

    return f"""---
type: research
platform: {platform_id}
platform_label: {platform_label}
category: {category_id}
category_name: {category_name}
topic_title: {topic_title}
status: draft
---

{body}"""
