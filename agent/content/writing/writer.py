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


_META_DELIM = "---META---"
_META_DELIM_RE = re.compile(r"\n-{3,}\s*META\s*-{3,}\s*\n?", re.I)


def _split_meta(text: str) -> tuple:
    """Split writer output into (body, meta_block) on the ``---META---`` delimiter.

    ``meta_block`` is "" when the delimiter is absent, so callers degrade to
    plain appends for older-format output.
    """
    parts = _META_DELIM_RE.split(text or "", maxsplit=1)
    if len(parts) == 2:
        return parts[0].rstrip(), parts[1].strip()
    return text or "", ""


def _append_to_body(text: str, addition: str) -> str:
    """Append ``addition`` to the reader-facing body, before the meta block."""
    body, meta_block = _split_meta(text)
    body = f"{body}\n\n{addition}"
    return f"{body}\n\n{_META_DELIM}\n{meta_block}" if meta_block else body


def _ensure_meta_block(text: str, lines: List[str]) -> str:
    """Append ``lines`` after the ``---META---`` delimiter, creating it if
    missing, instead of tacking meta entries onto the last body paragraph.
    """
    if not lines:
        return text
    body, meta_block = _split_meta(text)
    meta_block = (meta_block + "\n" + "\n".join(lines)).strip() if meta_block else "\n".join(lines)
    return f"{body}\n\n{_META_DELIM}\n{meta_block}"


def _visible_for_check(text: str) -> str:
    """``text`` with the ``---META---`` marker itself removed.

    Presence checks like ``re.search(r"meta|...", enhanced)`` must not treat
    the delimiter token as if it were an actual "Meta description:" line.
    """
    return (text or "").replace(_META_DELIM, "")


def _ensure_emphasis_marks(content: str) -> str:
    """Only steps in for a genuinely bare body with almost no emphasis at all.

    Bold is meant to be sparse/irregular now (see _expression_goals), so this
    no longer tops bodies up toward a target count — it just guards against
    the 0-1-bold extreme where the HTML highlighter/red/blue styling never
    triggers anywhere in the post.
    """
    text = content or ""
    bold_count = len(re.findall(r"\*\*[^*]+\*\*", text))
    if bold_count >= 2:
        return text

    phrases = [
        "전문가 상담",
        "의사, 물리치료사",
        "동작을 멈추는 것이 우선",
        "핵심 기준",
        "확인 기준",
        "통증",
        "중단",
    ]
    for phrase in phrases:
        if phrase not in text:
            continue
        if f"**{phrase}**" in text:
            continue
        # Wrap first plain occurrence only.
        text = text.replace(phrase, f"**{phrase}**", 1)
        if len(re.findall(r"\*\*[^*]+\*\*", text)) >= 3:
            break
    return text


_META_LEAK_SENTENCE_RE = re.compile(
    r"[^.\n]*?"
    r"(?:독자(?:가|를\s*위해|에게)|타겟\s*독자(?:가|를\s*위해)?)"
    r"[^.\n]*?"
    r"(?:(?:판단할\s*수\s*있도록|이해하기\s*쉽도록|쉽게\s*알\s*수\s*있도록|도움이\s*되도록|하도록)[^.\n]*?)?"
    r"(?:정리했습니다|작성했습니다|구성했습니다|담았습니다|준비했습니다|정리해\s*봤습니다)"
    r"[^.\n]*\.?\s*"
)


def _strip_meta_leak_sentences(text: str) -> str:
    """Remove sentences where the model describes its own writing intent /
    target audience instead of just writing the content — e.g. "{카테고리}
    독자가 모바일에서 판단할 수 있도록 정리했습니다". These read as a leaked
    internal instruction, not something a reader-facing post would say.
    Whole sentence is dropped rather than rewritten in place: there's no
    reliable way to salvage a real point from a sentence that's purely
    about the writing process itself.
    """
    value = _META_LEAK_SENTENCE_RE.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", value)


def _enhance_blog_quality(*, platform_id: str, category_name: str, topic_title: str, content: str) -> str:
    enhanced = _strip_meta_leak_sentences(content)
    # Strip internal pipeline jargon that falsely trips the quality gate.
    enhanced = re.sub(r"제공된\s*리서치\s*요약과\s*기획안을\s*바탕으로\s*작성한\s*", "", enhanced)
    enhanced = re.sub(r"리서치\s*요약과\s*기획안을\s*바탕으로\s*", "", enhanced)
    enhanced = re.sub(r"(?<![가-힣])기획안(?![가-힣])", "사전 조사 내용", enhanced)
    enhanced = re.sub(r"콘텐츠\s*개요", "핵심 요약", enhanced)
    enhanced = re.sub(r"구성안\s*\(초안\)", "본문 구성", enhanced)
    enhanced = re.sub(r"아웃라인만", "목차", enhanced)
    enhanced = re.sub(r"섹션\s*개요", "소주제", enhanced)
    enhanced = re.sub(r"작성\s*계획\s*문서", "본문", enhanced)
    enhanced = re.sub(
        r"\n##\s*(?:▶\s*)?\[?(?:모바일\s*최적화\s*체크|애드센스\s*승인\s*체크)\]?\s*\n"
        r"[\s\S]*?(?=\n##\s|\Z)",
        "\n",
        enhanced,
    )
    enhanced = re.sub(r"2023년?|2024년?|2025년?", "2026년 기준", enhanced)
    enhanced = re.sub(
        r"\n?!\[[^\]]*\]\((?:https?://(?:example\.com|via\.placeholder\.com|source\.unsplash\.com|"
        r"images\.unsplash\.com|unsplash\.com|placehold\.co|placekitten\.com)[^)]*|[^)]*placeholder[^)]*)\)\s*"
        r"(?:\n\*?(?:ALT|alt|\*\*ALT)[^\n]*\*?)?",
        "",
        enhanced,
        flags=re.I,
    )
    enhanced = re.sub(r"무조건\s+", "일률적인 ", enhanced)
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
        enhanced = _append_to_body(enhanced, f"## 한눈에 보는 핵심 요약\n- 오늘 주제는 {topic_title}.\n- 핵심만 짚으면, 확인할 기준과 주의할 점 두 가지다.")

    if not re.search(r"정보 제공 목적|개인 상황|전문가|상담|보장하지 않습니다|참고", enhanced):
        enhanced = _append_to_body(enhanced, "[정보 제공 안내]\n이 글은 일반적인 정보 제공 목적입니다. 개인 상황에 따라 적용 결과가 달라질 수 있으므로 중요한 결정 전에는 추가 확인이 필요합니다.")

    if platform_id == "naver":
        existing = {m for m in re.findall(r"\[이미지(\d+)(?:[^\]]*)?\]", enhanced)}
        missing = [i for i in range(1, 6) if str(i) not in existing]
        if missing:
            blocks = "\n\n".join(
                f"[이미지{i}: {topic_title} 관련 모바일 카드형 보강 이미지]\n이미지 설명: 작은 화면에서도 핵심 메시지가 읽히도록 짧은 문구와 여백 중심으로 구성합니다."
                for i in missing
            )
            enhanced = _append_to_body(enhanced, f"## 모바일 이미지 배치 보강\n{blocks}")
        if not re.search(r"이웃|댓글", enhanced):
            enhanced = _append_to_body(enhanced, f"궁금한 점은 댓글로 남겨주세요. 이웃 추가하면 {category_name} 관련 새 글을 계속 받아볼 수 있습니다.")

    if platform_id == "tistory":
        if not re.search(r"목차", enhanced):
            enhanced = f"## 목차\n1. {topic_title} 핵심 요약\n2. 기준과 체크리스트\n3. 실전 적용 방법\n4. 마무리\n\n{enhanced}"
        if not re.search(r"내부링크|관련 글|함께 읽", enhanced):
            enhanced = _append_to_body(enhanced, f"## 내부링크 후보\n- {category_name} 기본 가이드\n- {topic_title} 체크리스트\n- {category_name} 관련 최신 글")
        if not re.search(r"대표 이미지 ALT|이미지 ALT|ALT", enhanced):
            enhanced = _append_to_body(enhanced, f"대표 이미지 ALT: {topic_title}를 이해하기 쉽게 정리한 {category_name} 카드형 대표 이미지")

    if platform_id in ("blogspot", "wordpress"):
        # Force Hangul slug lines — replace English-only writer slugs.
        hangul_slug = re.sub(r"[^\w가-힣]+", "-", topic_title, flags=re.UNICODE)
        hangul_slug = re.sub(r"-{2,}", "-", hangul_slug).strip("-")[:48] or category_name

        def _ko_slug_line(match: re.Match) -> str:
            value = (match.group(2) or "").strip().strip("`")
            if re.search(r"[가-힣]", value):
                return match.group(0)
            prefix = match.group(1)
            return f"{prefix} `{hangul_slug}`"

        enhanced = re.sub(
            r"(?im)^(\**\s*(?:URL\s*slug|URL\s*슬러그|슬러그|slug)\s*:?\**\s*:?\s*)(.+)$",
            _ko_slug_line,
            enhanced,
        )
        missing_meta = []
        if not re.search(r"slug|URL 슬러그|슬러그", _visible_for_check(enhanced), flags=re.I):
            missing_meta.append(f"**URL slug:** `{hangul_slug}`")
        enhanced = _ensure_meta_block(enhanced, missing_meta)

    if platform_id == "blogspot":
        # Keep light SEO hints at the end for the quality gate only.
        missing_meta = []
        if not re.search(r"meta|메타 디스크립션", _visible_for_check(enhanced), flags=re.I):
            missing_meta.append(
                f"**Meta description:** {topic_title} 핵심 기준과 오늘 할 체크를 "
                "짧게 정리한 실전 가이드입니다."
            )
        if not re.search(r"태그 후보|Tags?:", _visible_for_check(enhanced), flags=re.I):
            missing_meta.append(
                f"**태그 후보:** {topic_title.split()[0] if topic_title.split() else category_name}, "
                f"자기계발, 루틴, 체크리스트, 습관, 생산성"
            )
        enhanced = _ensure_meta_block(enhanced, missing_meta)
        if not re.search(r"개인\s*상황|개인차|상황에\s*따라", enhanced):
            enhanced = _append_to_body(
                enhanced,
                "[정보 제공 안내]\n"
                "이 글은 일반적인 자기계발 정보입니다. 개인 상황·업무 강도에 따라 "
                "적용 방식이 달라질 수 있으니, 무리한 계획보다 지속 가능한 단위로 조정하세요.",
            )
        # Never keep AdSense / mobile ops checklists in the body.
        enhanced = re.sub(
            r"\n##\s*(?:▶\s*)?\[?(?:모바일\s*최적화\s*체크|애드센스\s*승인\s*체크)\]?\s*\n"
            r"[\s\S]*?(?=\n##\s|\Z)",
            "\n",
            enhanced,
        )

    enhanced = _ensure_emphasis_marks(enhanced)
    return enhanced


def _expression_goals(*, platform_id: str, category_id: str) -> str:
    """Quality-first practical writing goals for WordPress and Blogspot."""
    shared = """- 본문 분량 4,000~6,500자 (3,000자 미만·속이 빈 짧은 글 금지, 8,000자 초과 금지).
- H2 4~5개. 흐름: 독자 문제 → 판단 기준 → 실전 적용(상황별) → 함정/주의 → 오늘 체크·마무리.
- 사람이 쓴 호흡: 문장 길이를 섞고, 한 줄 팁만 나열하지 않는다. 각 팁에 왜/어떻게를 붙인다.
- AI 요약체·양산 블로그 리스트 재탕·가짜 1인칭 후기 금지. 남 글 베낀 느낌을 내지 않는다.
- 대비(나쁜 선택 vs 나은 선택)를 보여줄 땐 매번 같은 "나쁜 예 → 고친 예" 화살표 문장으로 쓰지 않는다.
  질문형("이렇게 하고 있다면?"), 일화형(짧은 상황 묘사), 서술형(문단 안에 자연스럽게 녹이기) 등으로
  글마다 다르게 표현한다. 상황별 차이(시간/체력/일정/통증 등)를 구체적으로.
- 【강조는 불규칙하게 — 단조로운 결론투 금지】
  - `**볼드**`는 글 전체에서 정말 중요한 곳에만 띄엄띄엄(총 3~6곳 정도, 문단마다 넣지 않는다).
  - 모든 단락 끝에 굵은 결론 문장을 다는 습관을 피한다. 강조 없이 끝나는 단락이 더 많아야 자연스럽다.
  - 주의·통증·중단·전문가 상담처럼 정말 중요한 경고만 `**볼드**`.
  - 단계형으로 쓸 경우 제목은 `### 1단계: …` 형식(형광펜 처리됨) — 다만 매번 단계형일 필요는 없다.
- 글 구조를 매번 똑같이 맞추지 않는다: 체크리스트·표·번호 목록은 필요할 때만 넣고, 없어도 된다.
  넣을 때도 체크리스트 항목 수, 단계 개수를 매번 다르게 한다 (고정된 5단계+표+체크리스트 조합 금지).
- H1에 숫자를 넣고, 포커스 키워드는 본문 6~12회만 자연스럽게.
- URL slug는 **한글**로 쓴다 (예: `요가-초보-확인-기준`). 영어 전용 slug 금지.
- 외부 링크 0~1개. 없는 URL 금지. slug/meta/태그/ALT는 글 맨 아래에만.
- 사람 냄새: 가벼운 개인적 어투, 문장 길이의 완급, 가끔 던지는 반문, 뉘앙스 있는 의견(단정 아님)을 섞는다.
  다만 카테고리 규칙(특히 health/finance/parenting의 YMYL 안전장치)은 절대 흔들지 않는다.
- 본문에 글쓰기 의도·독자 타겟·작성 목적을 설명하는 문장을 쓰지 않는다
  (예: "독자가 ~하도록 정리했습니다", "타겟 독자를 위해 ~했습니다" 같은 문장 금지 — 그냥 본문 내용만 쓴다)."""

    if category_id == "health" or platform_id == "wordpress":
        return f"""표현 목표 (밀도 있는 건강 정보 — 자격 가장 금지):
{shared}
- 독자가 “무엇을 확인·피하면 되는지”가 남도록 쓴다. 치료·효과 보장 금지."""

    if category_id == "self-dev" or platform_id == "blogspot":
        return f"""표현 목표 (밀도 있는 자기계발 실행 가이드 — 자격 가장 금지):
{shared}
- 독자가 “오늘 실행·측정할 문장”을 가져가게 쓴다. FAQ는 생략하거나 Q 2개만.
- 동기부여 구호·성공 보장 문구 금지."""

    return f"""표현 목표 (밀도 있는 실전 가이드):
{shared}
- 독자가 바로 판단·실행할 기준을 남긴다."""


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

{_expression_goals(platform_id=platform_id, category_id=category_id)}

중요(품질 게이트 통과 조건):
- 독자에게 보이는 완성 블로그 본문만 작성한다. 내부 작업 문서처럼 쓰지 않는다.
- 본문/출처/메타에 '기획안', '콘텐츠 개요', '구성안(초안)', '아웃라인만', '섹션 개요', '작성 계획 문서' 표현을 절대 쓰지 않는다.
- Research/Planning은 참고만 하고, 그것을 인용·언급하지 않은 채 자연스러운 글로 녹여 쓴다.
- 치료·예방·효과를 단정하거나 수익/원금/성공/합격을 보장하는 표현을 쓰지 않는다.
- H1 1개, H2 4~5개, 본문 4,000~6,500자. 개인차 안내를 포함한다.
- 건강 주제면 전문가 상담 권장, 자기계발 주제면 상황별 조정·복구 기준.
- AI 요약체·양산 블로그 재탕·가짜 후기로 분량을 채우지 않는다. 대비 표현은 매번 다른 방식(질문형/일화형/서술형)으로.
- 볼드는 아껴 쓴다(3~6곳, 모든 단락 끝마다 X). 구조(단계/표/체크리스트 유무·개수)는 매번 다르게 가져간다."""

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
