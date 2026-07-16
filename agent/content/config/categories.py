"""Content categories — ported verbatim from multi-content-pipeline/config/categories.js."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    target_audience: str
    tone: str
    keywords: List[str] = field(default_factory=list)
    caution_hints: List[str] = field(default_factory=list)
    sensitive: bool = False


CATEGORIES: List[Category] = [
    Category(
        id="it-tech",
        name="IT/테크 리뷰",
        target_audience="20~40대 IT 관심 직장인, 얼리어답터",
        tone="친근하고 전문적, 유머 약간, 쉬운 설명",
        keywords=["스마트폰", "노트북", "이어폰", "앱", "가전", "리뷰"],
        caution_hints=["제품 스펙과 가격은 발행 전 최신 정보 확인 필요"],
    ),
    Category(
        id="finance",
        name="재테크/경제",
        target_audience="20~40대 투자 초보, 재테크 관심 직장인",
        tone="신뢰감 있고 쉽게 설명, 과장 없이, 근거 기반",
        keywords=["주식", "부동산", "절세", "적금", "ETF", "투자"],
        sensitive=True,
        caution_hints=["수익 보장 표현 금지", "투자 판단은 개인 책임임을 명시"],
    ),
    Category(
        id="travel",
        name="여행/라이프스타일",
        target_audience="20~40대 여행 좋아하는 직장인, 주말 여행족",
        tone="설레고 감성적, 실용 정보 포함, 생생한 현장감",
        keywords=["국내여행", "해외여행", "맛집", "숙박", "항공", "여행팁"],
        caution_hints=["운영 시간과 요금은 발행 전 최신 정보 확인 필요"],
    ),
    Category(
        id="health",
        name="건강/헬스",
        target_audience="건강 관심 30~50대, 다이어트/운동 초보",
        tone="따뜻하고 전문적, 의학 정보는 신중하게",
        keywords=["다이어트", "운동", "영양제", "건강식품", "헬스", "요가"],
        sensitive=True,
        caution_hints=["치료/효과 보장 표현 금지", "전문가 상담 권장 문구 포함"],
    ),
    Category(
        id="parenting",
        name="육아/살림",
        target_audience="0~7세 자녀를 둔 부모, 신혼부부, 살림 초보",
        tone="따뜻하고 공감 가득, 실용적이고 솔직한 후기",
        keywords=["육아", "살림", "유아용품", "이유식", "태교", "정리정돈"],
        sensitive=True,
        caution_hints=["아이 안전 관련 단정 표현 금지", "개별 상황 차이 명시"],
    ),
    Category(
        id="self-dev",
        name="자기계발",
        target_audience="20~35대 성장 원하는 직장인, 취준생, 사이드 프로젝트 관심자",
        tone="동기부여, 실용적이고 구체적, 경험 기반의 솔직함",
        keywords=["독서", "습관", "생산성", "시간관리", "목표설정", "마인드셋"],
        caution_hints=["과장된 성공 보장 표현 금지"],
    ),
]

_BY_ID = {category.id: category for category in CATEGORIES}


def find_category(category_id: str) -> Optional[Category]:
    return _BY_ID.get(category_id)
