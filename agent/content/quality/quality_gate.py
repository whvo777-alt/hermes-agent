"""Quality Gate — ported from multi-content-pipeline/agents/quality/quality-gate.js.

Three-tier policy (Google-guidance aligned, as revised in that repo):
- HARD FAIL: real publish-risk items only. Any one blocks ``passed``.
- IMPORTANT warning: -5 score each. Never blocks ``passed`` alone.
- RECOMMENDATION warning: -1 score each.

Pure/deterministic — no LLM, no network calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_PLACEHOLDER_PATTERNS = [
    ("your-", re.compile(r"\byour-[a-z0-9._/?=&%:#-]*", re.I)),
    ("example.com", re.compile(r"\b(?:https?://)?(?:www\.)?example\.com\b[^\s)]*", re.I)),
    ("placeholder", re.compile(r"\b[a-z0-9._/?=&%:#-]*placeholder[a-z0-9._/?=&%:#-]*\b", re.I)),
    ("dummy", re.compile(r"\b[a-z0-9._/?=&%:#-]*dummy[a-z0-9._/?=&%:#-]*\b", re.I)),
    ("lorem ipsum", re.compile(r"lorem\s+ipsum", re.I)),
    ("TODO", re.compile(r"\bTODO\b")),
    ("FIXME", re.compile(r"\bFIXME\b")),
    ("준비중", re.compile(r"준비중")),
    ("링크 입력", re.compile(r"링크\s*입력")),
    ("이미지 URL", re.compile(r"이미지\s*URL")),
    ("your-image-url", re.compile(r"\byour-image-url[a-z0-9._/?=&%:#-]*", re.I)),
    ("your-blog-url", re.compile(r"\byour-blog-url[a-z0-9._/?=&%:#-]*", re.I)),
    ("localhost", re.compile(r"\bhttps?://localhost(?::\d+)?[^\s)]*", re.I)),
    ("127.0.0.1", re.compile(r"\bhttps?://127\.0\.0\.1(?::\d+)?[^\s)]*", re.I)),
    ("test.com", re.compile(r"\b(?:https?://)?(?:www\.)?test\.com\b[^\s)]*", re.I)),
]


def _find_placeholder_matches(content: str) -> List[Dict[str, str]]:
    found: Dict[str, str] = {}
    for label, pattern in _PLACEHOLDER_PATTERNS:
        for match in pattern.findall(content or ""):
            normalized = match.strip() if isinstance(match, str) else str(match).strip()
            if normalized and normalized not in found:
                found[normalized] = label
    return [{"label": label, "value": value} for value, label in found.items()]


def _count_image_slots(content: str) -> int:
    return len(re.findall(r"\[이미지\d+(?::[^\]]*)?\]", content or ""))


def _count_inline_images(content: str) -> int:
    return len(re.findall(r"!\[[^\]]*\]\([^)]+\)", content or ""))


def _has_broken_image_markup(content: str) -> bool:
    return bool(re.search(r"!\[[^\]]*\]\(\s*\)", content or ""))


def _count_tags(content: str) -> int:
    return len(re.findall(r"#[^\s#]+", content or ""))


def _count_h2_headings(content: str) -> int:
    return len(re.findall(r"^##\s+.+", content or "", flags=re.M))


def _has_h1(content: str) -> bool:
    return bool(re.search(r"^#\s+.+", content or "", flags=re.M))


def _strip_frontmatter(content: str) -> str:
    return re.sub(r"^---[\s\S]*?---\n", "", content or "")


def _extract_frontmatter_field(content: str, key: str) -> Optional[str]:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)\s*$", content or "", flags=re.M)
    return match.group(1).strip() if match else None


def _normalize_for_compare(content: str) -> str:
    value = _strip_frontmatter(content)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


# ---- HARD FAIL 판정 도우미 ----

def _is_planning_document(content: str) -> bool:
    value = content or ""
    if re.search(r"^status:\s*(planning|research|outline)\s*$", value, flags=re.M | re.I):
        return True
    return bool(re.search(r"기획안|콘텐츠\s*개요|구성안\s*\(초안\)|아웃라인만|섹션\s*개요|작성\s*계획\s*문서", value))


def _extract_paragraphs(content: str) -> List[str]:
    lines = _strip_frontmatter(content).split("\n")
    paragraphs: List[str] = []
    buffer: List[str] = []

    def flush() -> None:
        text = " ".join(buffer).strip()
        if len(text) >= 40:
            paragraphs.append(text)
        buffer.clear()

    for raw_line in lines:
        line = raw_line.strip()
        if (
            not line
            or re.match(r"^#{1,6}\s", line)
            or re.match(r"^[-*]\s", line)
            or re.match(r"^!\[", line)
            or re.match(r"^>", line)
        ):
            flush()
            continue
        buffer.append(line)
    flush()
    return paragraphs


def _is_near_empty_body(content: str) -> bool:
    return len(_extract_paragraphs(content)) < 3


def _has_readable_structure(content: str) -> bool:
    return not any(len(p) > 900 for p in _extract_paragraphs(content))


def _has_title_body_mismatch(content: str) -> bool:
    value = content or ""
    h1_match = re.search(r"^#\s+(.+)$", value, flags=re.M)
    if not h1_match:
        return False
    title = h1_match.group(1).strip()
    tokens = [t for t in re.split(r"[\s,·:()\-/]+", title) if len(t) >= 2]
    if not tokens:
        return False
    body_after_h1 = value[h1_match.end():]
    return not any(t in body_after_h1 for t in tokens)


def _has_excessive_repetition(content: str) -> bool:
    text = _strip_frontmatter(content)
    sentences = re.split(r"(?<=[.!?])\s|(?<=다\.)\s|(?<=요\.)\s|\n", text)
    counts: Dict[str, int] = {}
    for raw in sentences:
        s = re.sub(r"\s+", " ", raw).strip()
        if len(s) < 20:
            continue
        counts[s] = counts.get(s, 0) + 1
        if counts[s] >= 3:
            return True
    return False


def _is_duplicate_of_sibling(content: str, sibling_contents: Optional[List[str]] = None) -> bool:
    normalized = _normalize_for_compare(content)
    if not normalized:
        return False
    for sibling in sibling_contents or []:
        norm_sibling = _normalize_for_compare(sibling)
        if not norm_sibling:
            continue
        if normalized == norm_sibling:
            return True
        if len(normalized) > 200 and len(norm_sibling) > 200 and normalized[:200] == norm_sibling[:200]:
            return True
    return False


def _check_platform_category_match(content: str, platform_id: str, category_id: str) -> Dict[str, bool]:
    platform_field = _extract_frontmatter_field(content, "platform")
    category_field = _extract_frontmatter_field(content, "category_id") or _extract_frontmatter_field(content, "category")
    return {
        "platform_ok": platform_field == platform_id,
        "category_ok": category_field == category_id,
    }


def _has_guarantee_claim(content: str) -> bool:
    return bool(re.search(
        r"무조건\s*(효과|수익|낫|성공)|반드시\s*효과|수익\s*(을\s*)?보장|원금\s*(을\s*)?보장|"
        r"손실\s*(이|가)?\s*(전혀\s*)?없|완치(됩니다|된다)|100\s*%\s*(효과|보장|성공)|"
        r"비밀\s*공식|고수익\s*보장|평생\s*효과\s*보장",
        content or "",
    ))


def _has_fake_usage_claim(content: str) -> bool:
    return bool(re.search(
        r"제가\s*직접\s*(사용해|써|구매해)|직접\s*써\s*본\s*결과|내돈내산|제가\s*써\s*본\s*(제품|후기)",
        content or "",
    ))


def _has_unsourced_statistical_claim(content: str) -> bool:
    value = content or ""
    claim_pattern = re.compile(
        r"연구\s*(결과|에\s*따르면)|조사\s*(결과|에\s*따르면)|통계에\s*따르면|보고서에\s*따르면|"
        r"전문가들?은\s*(입을\s*모아|말한다)|데이터에\s*따르면"
    )
    if not claim_pattern.search(value):
        return False
    source_pattern = re.compile(r"출처|참고\s*자료|자료\s*출처|발표\s*기관|공식\s*발표|링크\s*참고")
    return not source_pattern.search(value)


def _has_broken_image(content: str, image: Optional[Dict[str, Any]]) -> bool:
    if not image or not image.get("file") or not image.get("alt") or not image.get("caption"):
        return True
    if _has_broken_image_markup(content):
        return True
    return False


def _has_slug(content: str) -> bool:
    return bool(re.search(r"^slug\s*:", content or "", flags=re.M | re.I)) or bool(
        re.search(r"URL\s*슬러그|슬러그", content or "", flags=re.I)
    )


def _get_slug_warning_tier(platform_id: str) -> Optional[str]:
    if platform_id == "naver":
        return None
    if platform_id == "tistory":
        return "recommendation"
    return "important"  # wordpress, blogspot


# ---- WARNING 판정 도우미 ----

def _has_faq_section(content: str) -> bool:
    return bool(re.search(r"FAQ|자주\s*묻는\s*질문", content or "", flags=re.I))


def _has_meta_description(content: str) -> bool:
    return bool(re.search(r"meta_description\s*:|meta\s*description|메타\s*디스크립션", content or "", flags=re.I))


def _has_schema(content: str) -> bool:
    return bool(re.search(r"application/ld\+json|schema\.org|구조화\s*데이터|Schema\s*마크업", content or "", flags=re.I))


def _has_cta(content: str) -> bool:
    return bool(re.search(r"이웃|댓글|구독해|공유해|팔로우", content or "", flags=re.I))


def _has_internal_links(content: str) -> bool:
    return bool(re.search(r"내부링크|관련\s*글|함께\s*읽", content or "", flags=re.I))


def _has_toc(content: str) -> bool:
    return bool(re.search(r"목차", content or ""))


def _has_author_byline(content: str) -> bool:
    return bool(re.search(r"작성자\s*:|글쓴이\s*:", content or "", flags=re.I))


def _has_ai_disclosure(content: str) -> bool:
    return bool(re.search(r"AI(가|로)?\s*(작성|생성|도움)|자동\s*생성|본\s*글은\s*AI", content or "", flags=re.I))


def _has_freshness_date(content: str) -> bool:
    return bool(re.search(r"\d{4}년|기준일|최신\s*정보\s*기준", content or ""))


_PLATFORM_LABELS = {"wordpress": "WordPress", "tistory": "티스토리", "naver": "네이버", "blogspot": "블로그스팟"}


def _build_platform_checks(platform_id: str, content: str) -> Dict[str, Any]:
    value = content or ""

    if platform_id == "naver":
        image_slots = _count_image_slots(value)
        tags = _count_tags(value)
        return {
            "imageSlots": image_slots >= 5,
            "imageSlotCount": image_slots,
            "tags": tags >= 10,
            "tagCount": tags,
            "cta": _has_cta(value),
            "specAccuracyNote": (re.search(r"확인|참고|출처", value) is not None) if re.search(r"버전|사양", value) else True,
            "freshnessDate": _has_freshness_date(value),
        }

    if platform_id == "tistory":
        return {
            "infoPurposeStated": bool(re.search(r"정보\s*제공\s*목적", value)),
            "riskDisclosure": bool(re.search(r"손실\s*가능성|개인\s*판단|투자\s*책임", value)),
        }

    if platform_id == "blogspot":
        return {
            "concreteExample": bool(re.search(r"예를\s*들어|예시", value)),
            "actionSteps": bool(re.search(r"체크리스트|실행\s*단계|단계별", value)),
        }

    if platform_id == "wordpress":
        return {
            "individualDifferenceNote": bool(re.search(r"개인차|개인\s*상황에\s*따라", value)),
            "expertConsultNote": bool(re.search(r"전문가\s*상담|의사와\s*상담", value)),
            "sourceReference": bool(re.search(r"출처|참고\s*자료", value)),
            "freshnessDate": _has_freshness_date(value),
        }

    return {}


@dataclass
class QualityGateResult:
    passed: bool
    score: int
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "score": self.score,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }


def run_quality_gate(
    *,
    topic_title: str,
    category_id: str,
    platform_id: str,
    content_type: str,
    content: str,
    image: Optional[Dict[str, Any]],
    sibling_contents: Optional[List[str]] = None,
) -> QualityGateResult:
    errors: List[str] = []
    important_warnings: List[str] = []
    recommendation_warnings: List[str] = []
    platform_checks = _build_platform_checks(platform_id, content)
    human_review_needed: List[str] = []

    def important(message: str) -> None:
        important_warnings.append(message)

    def recommend(message: str) -> None:
        recommendation_warnings.append(message)

    # 1) HARD FAIL
    if content_type != "blog":
        errors.append(f"[HARD FAIL] 지원하지 않는 콘텐츠 타입: {content_type}")

    if _is_planning_document(content):
        errors.append("[HARD FAIL] planning 문서 또는 기획안 형태로 판단됨 (완성형 본문 아님)")

    if _is_near_empty_body(content):
        errors.append("[HARD FAIL] 실제 본문이 거의 없음")

    if _has_title_body_mismatch(content):
        errors.append("[HARD FAIL] 제목과 본문의 심각한 불일치")

    if _has_excessive_repetition(content):
        errors.append("[HARD FAIL] 동일 문장 과도 반복 또는 의미 없는 문장 반복")

    if _is_duplicate_of_sibling(content, sibling_contents):
        errors.append("[HARD FAIL] 다른 플랫폼 원고와 동일하거나 거의 동일한 본문 (복사 의심)")

    for match in _find_placeholder_matches(content):
        errors.append(f"[HARD FAIL] 미완성 placeholder 발견 ({match['label']}): {match['value']}")

    if _has_broken_image(content, image):
        errors.append("[HARD FAIL] 대표 이미지 파일/ALT/캡션 누락 또는 깨진 이미지 URL")

    category_match = _check_platform_category_match(content, platform_id, category_id)
    if not category_match["platform_ok"]:
        errors.append("[HARD FAIL] frontmatter platform 값이 대상 플랫폼과 불일치")
    if not category_match["category_ok"]:
        errors.append("[HARD FAIL] frontmatter category id가 대상 카테고리와 불일치")

    if _has_guarantee_claim(content):
        errors.append("[HARD FAIL] 건강 치료/예방/효과 보장 또는 재테크 수익/원금 보장 표현 포함")

    if _has_fake_usage_claim(content):
        errors.append("[HARD FAIL] 직접 사용하지 않은 제품을 실제 사용 후기처럼 표현")

    if _has_unsourced_statistical_claim(content):
        errors.append("[HARD FAIL] 출처가 필요한 연구/통계 인용인데 출처가 전혀 없음")
        human_review_needed.append("통계/연구 인용의 사실관계 및 출처 정확성은 자동 검사로 완전히 검증 불가 — 사람 검토 필요")

    # 2) IMPORTANT WARNING
    if not _has_h1(content):
        important("H1 제목 누락")

    h2_count = _count_h2_headings(content)
    if h2_count < 2:
        important(f"H2 소제목 부족 (권장 2개 이상, 현재 {h2_count}개)")

    slug_tier = _get_slug_warning_tier(platform_id)
    if slug_tier and not _has_slug(content):
        platform_label = _PLATFORM_LABELS.get(platform_id, platform_id)
        message = f"URL 슬러그 없음 ({platform_label} 발행 단계에서 최종 확인 필요)"
        if slug_tier == "important":
            important(message)
        else:
            recommend(message)

    # 3) RECOMMENDATION WARNING
    if not _has_readable_structure(content):
        recommend("문단이 지나치게 길어 가독성이 떨어질 수 있음")
    if not _has_faq_section(content):
        recommend("FAQ 섹션 없음 (권장 사항, 필수 아님)")
    if not _has_meta_description(content):
        recommend("Meta Description 없음 (권장 사항, 필수 아님)")
    if not _has_schema(content):
        recommend("Schema/구조화 데이터 없음 (권장 사항, 필수 아님)")
    if not _has_author_byline(content):
        recommend("author byline 없음 (권장 사항)")
    if not _has_ai_disclosure(content):
        recommend("AI/자동화 작성 안내 없음 (권장 사항)")
    if not _has_cta(content):
        recommend("CTA(댓글/구독/공유 유도) 없음 (권장 사항)")
    if not _has_internal_links(content):
        recommend("내부 링크 없음 (권장 사항)")
    if not _has_toc(content):
        recommend("TOC(목차) 없음 (권장 사항)")
    if platform_id != "naver" and _count_inline_images(content) <= 1:
        recommend(f"본문 이미지 1개뿐이거나 이미지 슬롯 부족 (현재 {_count_inline_images(content)}개)")
    if re.search(r"2023|2024|2025", content or ""):
        recommend("과거 연도 표현 확인 필요: 최신성 점검")
    image_status = (image or {}).get("status")
    if image_status not in ("ready", "local_ready_upload_required"):
        recommend(f"대표 이미지 상태 확인 필요: {image_status or 'missing'}")
    if topic_title and topic_title not in (content or ""):
        recommend("본문 또는 frontmatter의 주제명 확인 필요")

    # 4) 플랫폼별 추가 검사
    if platform_id == "naver":
        if not platform_checks["imageSlots"]:
            recommend(f"네이버 이미지 슬롯 부족: {platform_checks['imageSlotCount']}/5")
        if not platform_checks["tags"]:
            recommend(f"네이버 태그 부족: {platform_checks['tagCount']}/10")
        if not platform_checks["specAccuracyNote"]:
            important("네이버 제품 사양/버전 정보 정확성 확인 필요")
        if not platform_checks["freshnessDate"]:
            important("네이버 정보 기준일 표시 확인 필요")
        human_review_needed.append("실사용 후기처럼 보이지 않는지, 제품 사양이 실제 최신 정보와 일치하는지 사람 확인 필요")

    if platform_id == "tistory":
        if not platform_checks["infoPurposeStated"]:
            important("티스토리 투자 권유로 오해되지 않도록 정보 제공 목적 명시 확인 필요")
        if not platform_checks["riskDisclosure"]:
            important("티스토리 손실 가능성/개인 판단 필요 안내 확인 필요")
        if re.search(r"이웃 추가", content or ""):
            recommend("티스토리에 맞지 않는 네이버식 이웃 CTA 확인 필요")
        human_review_needed.append("금융/세금/제도 관련 수치는 기준일과 출처를 사람이 최종 확인 필요")

    if platform_id == "blogspot":
        if not platform_checks["concreteExample"]:
            recommend("블로그스팟 구체적 예시 부족 확인 필요 (흔한 조언 나열 방지)")
        if not platform_checks["actionSteps"]:
            recommend("블로그스팟 실행 단계/체크리스트 부족 확인 필요")

    if platform_id == "wordpress":
        if not platform_checks["individualDifferenceNote"]:
            important("WordPress 개인차 안내 문구 확인 필요")
        if not platform_checks["expertConsultNote"]:
            important("WordPress 의료 전문가 상담 권장 문구 확인 필요")
        if not platform_checks["sourceReference"]:
            important("WordPress 건강 정보 출처/참고 근거 확인 필요")
        if not platform_checks["freshnessDate"]:
            recommend("WordPress 정보 기준일/최신성 확인 필요")
        human_review_needed.append("건강 정보의 의학적 정확성은 자동 검사로 보장할 수 없음 — 사람 검토 필요")

    warnings = important_warnings + recommendation_warnings
    hard_fail_count = len(errors)
    score = max(0, 100 - hard_fail_count * 30 - len(important_warnings) * 5 - len(recommendation_warnings) * 1)

    return QualityGateResult(
        passed=hard_fail_count == 0,
        score=score,
        errors=errors,
        warnings=warnings,
        metadata={
            "platformChecks": platform_checks,
            "hardFailCount": hard_fail_count,
            "importantWarnings": important_warnings,
            "recommendationWarnings": recommendation_warnings,
            "warningCount": len(warnings),
            "humanReviewNeeded": list(dict.fromkeys(human_review_needed)),
        },
    )
