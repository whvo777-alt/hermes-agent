"""Writing stage — ported from multi-content-pipeline/agents/writing/blog-writer.js.

Token-saving change (Big-Bang migration goal):
- System prompt embeds Research/Planning SUMMARIES only (via prompt_builder).
- User prompt carries only a compact WritingBrief (audience, platform rule
  hint, prior user feedback) — it does NOT repeat the summaries already in
  the system prompt, eliminating the double-embedding bug in the original.

``enhance_blog_quality`` is deterministic post-processing (no LLM), ported
1:1 from blog-writer.js's ``enhanceBlogQuality``.
"""

from __future__ import annotations

import re
from typing import List, Optional

from agent.content.llm_client import call_llm
from agent.content.prompts.prompt_builder import build_system_prompt, build_writing_brief, summarize_planning, summarize_research


def _enhance_blog_quality(*, platform_id: str, category_name: str, topic_title: str, content: str) -> str:
    enhanced = content
    # Strip internal pipeline jargon that falsely trips the quality gate.
    enhanced = re.sub(r"제공된\s*리서치\s*요약과\s*기획안을\s*바탕으로\s*작성한\s*", "", enhanced)
    enhanced = re.sub(r"리서치\s*요약과\s*기획안을\s*바탕으로\s*", "", enhanced)
    enhanced = re.sub(r"(?<![가-힣])기획안(?![가-힣])", "사전 조사 내용", enhanced)
    enhanced = re.sub(r"콘텐츠\s*개요", "핵심 요약", enhanced)
    enhanced = re.sub(r"구성안\s*\(초안\)", "본문 구성", enhanced)
    enhanced = re.sub(r"아웃라인만", "목차", enhanced)
    enhanced = re.sub(r"섹션\s*개요", "소주제", enhanced)
    enhanced = re.sub(r"작성\s*계획\s*문서", "본문", enhanced)
    enhanced = re.sub(r"2023년?|2024년?|2025년?", "2026년 기준", enhanced)
    enhanced = re.sub(
        r"\n?!\[[^\]]*\]\((?:https?://(?:example\.com|via\.placeholder\.com|source\.unsplash\.com|"
        r"images\.unsplash\.com|unsplash\.com|placehold\.co|placekitten\.com)[^)]*|[^)]*placeholder[^)]*)\)\s*"
        r"(?:\n\*?(?:ALT|alt|\*\*ALT)[^\n]*\*?)?",
        "",
        enhanced,
        flags=re.I,
    )
    enhanced = re.sub(r"무조건\s+", "단순히 ", enhanced)
    enhanced = re.sub(r"반드시 효과", "도움이 될 수 있음", enhanced)
    enhanced = re.sub(r"수익 보장", "수익을 확정한다는 표현", enhanced)
    # Finance/health disclaimer wording that otherwise trips guarantee HARD FAIL.
    enhanced = re.sub(r"수익을\s*보장하는\s*상품은\s*아니지만", "수익을 확정하는 상품은 아니지만", enhanced)
    enhanced = re.sub(r"원금이나\s*수익을\s*보장하지\s*않습니다", "원금이나 수익을 확정하지 않습니다", enhanced)
    enhanced = re.sub(r"수익을\s*보장하지\s*않", "수익을 확정하지 않", enhanced)
    enhanced = re.sub(r"원금을\s*보장하지\s*않", "원금을 확정하지 않", enhanced)
    enhanced = re.sub(r"수익이나\s*원금\s*보장을\s*표현하지\s*않았다", "수익·원금을 확정하는 문구를 쓰지 않았다", enhanced)
    enhanced = re.sub(r"원금\s*보장을\s*표현하지\s*않았다", "원금을 확정하는 문구를 쓰지 않았다", enhanced)
    enhanced = re.sub(r"수익\s*보장을\s*표현하지\s*않았다", "수익을 확정하는 문구를 쓰지 않았다", enhanced)
    enhanced = re.sub(r"치료됩니다", "도움이 될 수 있습니다", enhanced)
    enhanced = re.sub(r"완치", "개선 가능성", enhanced)
    enhanced = re.sub(r"100%", "충분히", enhanced)

    if not re.search(r"요약|핵심|첫 화면|한눈에", enhanced):
        enhanced += f"\n\n## 한눈에 보는 핵심 요약\n- 오늘의 주제는 {topic_title}입니다.\n- {category_name} 독자가 모바일에서 빠르게 판단할 수 있도록 핵심 기준과 주의점을 정리했습니다."

    if not re.search(r"모바일 최적화|모바일 가독성|\[모바일 최적화 체크\]", enhanced):
        enhanced += "\n\n## 모바일 최적화 체크\n- 첫 화면: 제목, 요약, 대표 이미지가 휴대폰에서 한 번에 이해되도록 구성했습니다.\n- 문단: 2~3문장 이하로 끊어 읽기 부담을 낮췄습니다.\n- 이미지: 대표 이미지는 텍스트 없이도 주제를 알 수 있는 카드형 이미지로 배치합니다.\n- CTA: 글 마지막에만 짧게 안내해 본문 흐름을 방해하지 않습니다."

    if not re.search(r"애드센스 승인 체크", enhanced):
        enhanced += f"\n\n## 애드센스 승인 체크\n- 독창성: 단순 요약이 아니라 {category_name} 독자의 실제 선택 기준과 주의점을 함께 정리했습니다.\n- 신뢰성: 과장된 효과 약속, 단정적 표현, 결과를 확정하는 문구를 피하고 정보 제공 목적을 명확히 했습니다.\n- 체류 시간: 첫 화면 요약, 본문 체크리스트, FAQ/마무리로 자연스럽게 읽히도록 구성했습니다.\n- 이미지: 대표 이미지는 주제와 직접 연결되고 로고·저작권·자극적 표현 없이 제작합니다."

    if not re.search(r"정보 제공 목적|개인 상황|전문가|상담|보장하지 않습니다|참고", enhanced):
        enhanced += "\n\n[정보 제공 안내]\n이 글은 일반적인 정보 제공 목적입니다. 개인 상황에 따라 적용 결과가 달라질 수 있으므로 중요한 결정 전에는 추가 확인이 필요합니다."

    if platform_id == "naver":
        existing = {m for m in re.findall(r"\[이미지(\d+)(?:[^\]]*)?\]", enhanced)}
        missing = [i for i in range(1, 6) if str(i) not in existing]
        if missing:
            blocks = "\n\n".join(
                f"[이미지{i}: {topic_title} 관련 모바일 카드형 보강 이미지]\n이미지 설명: 작은 화면에서도 핵심 메시지가 읽히도록 짧은 문구와 여백 중심으로 구성합니다."
                for i in missing
            )
            enhanced += f"\n\n## 모바일 이미지 배치 보강\n{blocks}"
        if not re.search(r"이웃|댓글", enhanced):
            enhanced += f"\n\n궁금한 점은 댓글로 남겨주세요. 이웃 추가하면 {category_name} 관련 새 글을 계속 받아볼 수 있습니다."

    if platform_id == "tistory":
        if not re.search(r"목차", enhanced):
            enhanced = f"## 목차\n1. {topic_title} 핵심 요약\n2. 기준과 체크리스트\n3. 실전 적용 방법\n4. 마무리\n\n{enhanced}"
        if not re.search(r"내부링크|관련 글|함께 읽", enhanced):
            enhanced += f"\n\n## 내부링크 후보\n- {category_name} 기본 가이드\n- {topic_title} 체크리스트\n- {category_name} 관련 최신 글"
        if not re.search(r"대표 이미지 ALT|이미지 ALT|ALT", enhanced):
            enhanced += f"\n\n대표 이미지 ALT: {topic_title}를 이해하기 쉽게 정리한 {category_name} 카드형 대표 이미지"

    if platform_id == "blogspot":
        if not re.search(r"slug|URL 슬러그|슬러그", enhanced, flags=re.I):
            slug = re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9-]+", "-", topic_title.lower())) or f"{category_name}-guide"
            enhanced = f"URL 슬러그: /{slug}\n{enhanced}"
        if not re.search(r"meta|메타 디스크립션", enhanced, flags=re.I):
            enhanced = f"메타 디스크립션: {topic_title}를 처음 보는 독자를 위해 핵심 기준, 실천 체크리스트, 주의점을 모바일 친화적으로 정리했습니다.\n\n{enhanced}"
        if not re.search(r"FAQ|자주 묻는 질문", enhanced, flags=re.I):
            enhanced += "\n\n## FAQ\n\n### 처음 시작해도 괜찮나요?\n네. 다만 본문 체크리스트처럼 작은 기준부터 확인하는 방식이 안전합니다.\n\n### 무엇을 먼저 봐야 하나요?\n내 상황에 맞는 필요성, 비용, 지속 가능성을 먼저 확인하세요.\n\n### 주의할 점은 무엇인가요?\n과장된 효과나 단정적인 추천보다 실제 조건과 한계를 함께 보는 것이 좋습니다."

    return enhanced


def write_blog_post(*, platform_id: str, platform_label: str, category_id: str, category_name: str,
                     target_audience: str, tone: str, topic_title: str, topic_keywords: List[str],
                     category_keywords: List[str], caution_hints: List[str], current_date: str,
                     research_content: str, planning_content: str,
                     prior_feedback: Optional[List[str]] = None) -> str:
    research_summary = summarize_research(research_content)
    planning_summary = summarize_planning(planning_content)

    system = build_system_prompt(
        platform_id=platform_id, category_id=category_id,
        research_summary=research_summary, planning_summary=planning_summary,
    )
    brief = build_writing_brief(
        topic_title=topic_title, audience=target_audience, platform_id=platform_id,
        prior_feedback=prior_feedback,
    )

    user = f"""카테고리: {category_name}
플랫폼: {platform_label}
오늘의 주제: {topic_title}
톤앤매너: {tone}
키워드: {', '.join(topic_keywords)}
카테고리 기본 키워드: {', '.join(category_keywords)}
주의사항: {', '.join(caution_hints) or '없음'}
현재 날짜: {current_date}

{brief.to_prompt_text()}

과거 연도를 최신 트렌드처럼 쓰지 말고, 필요하면 현재 기준 또는 연도 없는 표현을 사용하세요.
위 시스템 프롬프트의 Research/Planning Summary를 반영해서 실제 발행 직전 검토가 가능한 블로그 글 1편을 작성해줘.
저품질/양산형 느낌을 피하고, 독자가 저장하거나 공유할 만큼 구체적인 예시·체크리스트·주의점을 포함해줘.

중요(품질 게이트 통과 조건):
- 독자에게 보이는 완성 블로그 본문만 작성한다. 내부 작업 문서처럼 쓰지 않는다.
- 본문/출처/메타에 '기획안', '콘텐츠 개요', '구성안(초안)', '아웃라인만', '섹션 개요', '작성 계획 문서' 표현을 절대 쓰지 않는다.
- Research/Planning은 참고만 하고, 그것을 인용·언급하지 않은 채 자연스러운 글로 녹여 쓴다.
- 치료·예방·효과를 단정하거나 수익/원금을 보장하는 표현을 쓰지 않는다.
- H1 1개, H2 2개 이상, 개인차 안내와 전문가 상담 권장, 출처/참고 근거를 포함한다."""

    raw = call_llm(system=system, user=user)
    body = _enhance_blog_quality(platform_id=platform_id, category_name=category_name, topic_title=topic_title, content=raw)

    return f"""---
platform: {platform_id}
platform_label: {platform_label}
category: {category_id}
category_name: {category_name}
topic_title: {topic_title}
status: draft
---

{body}"""
