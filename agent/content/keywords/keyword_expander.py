"""Naver Search Ad API keyword expansion for category seed lists.

Fixes the exhaustion failure already observed in production ("이미 쓴
키워드=[...] 사용 가능한 새 주제가 없습니다" -- self-dev's 25 hand-picked
keywords ran out). agent/content/config/categories.py's CATEGORIES stays
hand-curated and untouched; this module only ever writes to
cache/<category_id>.json, and get_expanded_keywords() is the one read
path categories.get_effective_keywords() calls to merge core + cached.

The read path (get_expanded_keywords, _load_cache) is pure stdlib on
purpose -- no import of naver_ad_client (and therefore no hard
dependency on `requests`) at module load time, since categories.py's
get_effective_keywords() runs on orchestrator.py's hot path for every
content generation. Only expand_category() (called from the weekly cron
script, never from the hot path) imports the network client, lazily.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent / "cache"
_DEFAULT_CACHE_TTL_DAYS = 7
_DEFAULT_TOP_N = 30
_MIN_TOTAL_SEARCH_VOLUME = 1000
_MAX_CACHED_CANDIDATES = 30
_LLM_BATCH_SIZE = 100
_LLM_PREFILTER_SIZE = 100
_ROOT_MAX_PER_GROUP = 12

_CATEGORY_PROFILES = {
    "self-dev": "직장인·취준생의 생산성, 습관, 시간관리, 집중력과 실행 루틴을 다루는 자기계발 정보",
    "health": "30~50대의 다이어트·운동·수면·스트레칭을 다루는 확인 기준 중심의 건강 정보",
    "finance": "투자 초보와 직장인을 위한 적금·신용·ETF·주식·절세 중심의 근거 기반 재테크 정보",
    "it-tech": "직장인과 얼리어답터를 위한 소프트웨어·클라우드·데이터·업무 자동화 중심의 IT 정보",
    "parenting": "0~7세 자녀 부모를 위한 육아·살림·유아학습·아이 안전 중심의 따뜻하고 실용적인 정보",
    "travel": "주말 여행족을 위한 국내 여행지·여행 코스·숙박·여행 준비 중심의 실용적인 여행 정보",
}

# Representative seeds per category, hand-picked from categories.py's
# existing core keyword lists. Naver's keywordstool accepts at most 5
# seeds per call (see naver_ad_client._MAX_SEEDS_PER_CALL).
SEED_KEYWORDS_BY_CATEGORY: Dict[str, List[str]] = {
    "self-dev": ["시간관리", "목표설정", "습관", "계획표", "생산성"],
    "health": ["다이어트식단", "홈트레이닝", "수면부족"],
    "finance": ["적금추천", "신용점수", "ETF추천"],
    "it-tech": ["엑셀함수", "클라우드백업", "사진정리"],
    "parenting": ["이유식", "유아놀이", "육아", "아기수면교육"],
    "travel": ["국내여행지", "당일치기여행", "캠핑장추천"],
}

# Reduced from the original RESEARCH_AGENT.md 5-factor formula:
#   score = search_volume*0.25 + competition_inverse*0.30 + cpc*0.20
#         + trend_slope*0.15 + platform_fit*0.10
# Naver's keywordstool only returns search volume + competition. Search volume
# is intentionally weighted higher so low-volume candidates cannot all tie at
# the competition floor. cpc/trend/platform_fit keep weight 0.0 until a real
# data source exists for them (see score_keyword's docstring).
_VOLUME_WEIGHT = 0.60
_COMPETITION_WEIGHT = 0.40
_CPC_WEIGHT = 0.0
_TREND_WEIGHT = 0.0
_PLATFORM_FIT_WEIGHT = 0.0

_COMPETITION_MAP = {"낮음": 1.0, "중간": 0.5, "높음": 0.1}
_DEFAULT_COMPETITION = 0.5  # unrecognized compIdx value -> treat as medium


def _normalize(value: float, *, minimum: float, maximum: float) -> float:
    """Min-max scale into [0, 1]. A flat batch (max == min) collapses to
    1.0 -- every candidate in a same-volume batch is weighted equally
    rather than arbitrarily favoring one."""
    if maximum <= minimum:
        return 1.0
    return max(0.0, min(1.0, (value - minimum) / (maximum - minimum)))


def _normalize_log_volume(total_volume: int, *, volume_min: float, volume_max: float) -> float:
    """Min-max scale log10(total_volume + 1) against the batch range."""
    return _normalize(
        math.log10(total_volume + 1),
        minimum=math.log10(volume_min + 1),
        maximum=math.log10(volume_max + 1),
    )


def score_keyword(
    pc_qc: int,
    mobile_qc: int,
    comp_idx: str,
    *,
    volume_min: float,
    volume_max: float,
    cpc_norm: Optional[float] = None,
    trend_slope: Optional[float] = None,
    platform_fit: Optional[float] = None,
) -> float:
    """Score one Naver keyword candidate for seed-list expansion.

    volume_min/volume_max: the (pc_qc + mobile_qc) range across the
    *other candidates fetched in the same batch* -- log10(total + 1) is
    min-max normalized relative to siblings, not an absolute scale.

    cpc_norm / trend_slope / platform_fit: accepted but currently
    unweighted (0.0) -- no data source yet. Wiring one in later (e.g.
    trend_slope computed from cache history once enough weekly snapshots
    exist) is a 2-constant + 1-argument change, not a signature change.
    """
    search_volume_norm = _normalize_log_volume(
        pc_qc + mobile_qc,
        volume_min=volume_min,
        volume_max=volume_max,
    )
    competition_inverse = _COMPETITION_MAP.get(comp_idx, _DEFAULT_COMPETITION)

    score = search_volume_norm * _VOLUME_WEIGHT + competition_inverse * _COMPETITION_WEIGHT
    if cpc_norm is not None:
        score += cpc_norm * _CPC_WEIGHT
    if trend_slope is not None:
        score += trend_slope * _TREND_WEIGHT
    if platform_fit is not None:
        score += platform_fit * _PLATFORM_FIT_WEIGHT
    return round(score, 4)


def _parse_qc(raw) -> int:
    try:
        return int(str(raw).replace("<", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0


# The deterministic safety filter intentionally errs on the side of blocking
# medical-risk keywords, individual supplement/product brands, and local
# businesses. The LLM filter is still used for broader category relevance,
# but these safety classes must not depend on a model response.
_REGION_NAMES = [
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
    "수원", "성남", "고양", "용인", "청주", "전주", "천안", "포항",
    "창원", "김해", "안산", "안양", "평택", "춘천", "원주", "부천",
    "잠실", "강남", "서초", "마포", "송파", "분당", "판교", "일산",
    "송도", "동탄", "구로", "영등포", "광명",
]
_TOPICLESS_INSTITUTION_SUFFIXES = [
    "강의", "학원", "센터", "클래스", "스터디", "자격증", "과외", "교육", "수업",
]
_BARE_REGION_COMPOUND_RE = re.compile(
    "^(?:" + "|".join(_REGION_NAMES) + r")\s*(?:" + "|".join(_TOPICLESS_INSTITUTION_SUFFIXES) + ")$"
)
_LOCAL_BUSINESS_SUFFIXES = [
    "업체", "매장", "몰", "하우스", "스토어", "샵", "센터", "지점",
    "헬스장", "피트니스", "짐", "스튜디오", "학원", "공방",
    "병원", "의원", "한의원", "약국", "클리닉", "피부과", "성형외과",
]
_REGION_LOCAL_BUSINESS_RE = re.compile(
    "^(?:" + "|".join(_REGION_NAMES) + r").*(?:" + "|".join(_LOCAL_BUSINESS_SUFFIXES) + ")$"
)

_MEDICAL_RISK_TERMS = frozenset({
    "제니칼", "위고비", "삭센다", "오젬픽", "큐시미아", "콘트라브",
    "디에타민", "펜터민", "푸리민", "식욕억제제", "비만약", "다이어트약",
    "처방약", "전문의약품", "의약품",
})
_HEALTH_PRODUCT_BRAND_TERMS = frozenset({
    "안국건강", "고농축행감환", "공비환", "머슬부스터", "자임당", "알파cd",
})
_MEDICAL_RISK_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:고주파|지방흡입|지방분해|윤곽주사|보톡스|필러|리프팅|시술|수술|주사)",
        r"(?:병원|의원|한의원|약국|피부과|성형외과|이비인후과|내과|정형외과|산부인과)",
        r"(?:비만|다이어트|체중).*(?:환|정|캡슐|정제|약)",
    )
)
_HEALTH_PRODUCT_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:캡슐|정제|구미|분말)$",
        r"(?:프로틴|부스터)$",
    )
)
_PURCHASE_INTENT_TERMS = frozenset({
    "정기배송", "무료체험", "체험팩", "샘플", "밀키트", "배달", "택배", "직구",
    "쇼핑몰", "최저가", "할인", "쿠폰", "렌탈", "중고", "세트", "카세트",
    "영양제", "보조제", "유산균", "프로바이오틱스", "콜라겐", "루테인", "오메가3",
    "멜라토닌", "락티움", "추출물", "쉐이크", "스무디", "착즙",
    "운동기구", "머신", "덤벨", "철봉", "마사지기", "안마기", "워킹패드",
    "헬스장", "필라테스", "크로스핏", "요가원", "도시락", "볶음밥", "컵밥",
    "KODEX", "코덱스", "TIGER", "ACE", "PLUS", "SOL", "TQQQ", "SOXL", "QQQ",
    "PLUSETF", "SOLETF", "SOLACTIVE",
    "주가", "시세", "환율", "증시", "지수", "매수종목", "추천종목", "종목추천", "선물",
    "유망주", "투자클럽",
})
_PURCHASE_INTENT_ALPHA_BOUNDARY_TERMS = frozenset({"ACE", "SOL", "PLUS"})
_PURCHASE_INTENT_ALPHA_BOUNDARY_PATTERNS = {
    term.casefold(): re.compile(
        rf"(?<![a-z]){re.escape(term.casefold())}(?![a-z])"
    )
    for term in _PURCHASE_INTENT_ALPHA_BOUNDARY_TERMS
}
_PURCHASE_INTENT_SANITIZE_PHRASES = (
    "가볼만한곳",
    "수상레저",
    "자격증",
    "선물거래",
    "해외선물",
    "선물옵션",
)
_VOLUME_SUFFIX_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:mg|g|kg|ml|l|밀리그램|그램|킬로그램|밀리리터|리터)$",
    re.IGNORECASE,
)


def _keyword_key(keyword: str) -> str:
    return re.sub(r"\s+", "", keyword.strip()).casefold()


def _is_bare_region_compound(keyword: str) -> bool:
    """Return True for topicless or local region/business compounds."""
    normalized = _keyword_key(keyword)
    return bool(
        _BARE_REGION_COMPOUND_RE.match(normalized)
        or _REGION_LOCAL_BUSINESS_RE.match(normalized)
    )


def _is_safety_blocked(keyword: str) -> bool:
    """Block medical-risk, health-product, and local-business keywords."""
    normalized = _keyword_key(keyword)
    if not normalized:
        return True
    if _is_bare_region_compound(normalized):
        return True
    if any(term in normalized for term in _MEDICAL_RISK_TERMS):
        return True
    if any(term in normalized for term in _HEALTH_PRODUCT_BRAND_TERMS):
        return True
    if any(pattern.search(normalized) for pattern in _MEDICAL_RISK_PATTERNS):
        return True
    return any(pattern.search(normalized) for pattern in _HEALTH_PRODUCT_PATTERNS)


def _is_purchase_intent_blocked(keyword: str) -> bool:
    """Block commercial/purchase-intent terms separately from safety rules."""
    normalized = _keyword_key(keyword)
    for phrase in _PURCHASE_INTENT_SANITIZE_PHRASES:
        normalized = normalized.replace(_keyword_key(phrase), "")
    for term in _PURCHASE_INTENT_TERMS:
        term_key = term.casefold()
        if term in _PURCHASE_INTENT_ALPHA_BOUNDARY_TERMS:
            if _PURCHASE_INTENT_ALPHA_BOUNDARY_PATTERNS[term_key].search(normalized):
                return True
        elif term_key in normalized:
            return True
    return bool(_VOLUME_SUFFIX_RE.search(normalized))


def _is_deterministically_blocked(keyword: str) -> bool:
    """Backward-compatible union of safety and purchase-intent filters."""
    return _is_safety_blocked(keyword) or _is_purchase_intent_blocked(keyword)


def _parse_candidates(
    raw_candidates: List[dict],
    *,
    excluded_keys: Optional[set[str]] = None,
    audit: Optional[dict] = None,
) -> List[dict]:
    """Parse, normalize, deduplicate, and apply ordered deterministic filters."""
    parsed = []
    seen = set(excluded_keys or set())
    if audit is not None:
        audit.setdefault("raw_candidates", len(raw_candidates))
        audit.setdefault("safety_removed", [])
        audit.setdefault("purchase_removed", [])
    for row in raw_candidates:
        keyword = str(row.get("relKeyword", "")).strip()
        key = _keyword_key(keyword)
        if not keyword or key in seen:
            continue
        seen.add(key)
        if _is_safety_blocked(keyword):
            if audit is not None:
                audit["safety_removed"].append(keyword)
            continue
        if _is_purchase_intent_blocked(keyword):
            if audit is not None:
                audit["purchase_removed"].append(keyword)
            continue
        parsed.append({
            "keyword": keyword,
            "pc_qc": _parse_qc(row.get("monthlyPcQcCnt")),
            "mobile_qc": _parse_qc(row.get("monthlyMobileQcCnt")),
            "comp_idx": row.get("compIdx", ""),
        })
    return parsed


def _total_search_volume(candidate: dict) -> int:
    return _parse_qc(candidate.get("pc_qc", 0)) + _parse_qc(candidate.get("mobile_qc", 0))


def _score_parsed_candidates(candidates: List[dict]) -> List[dict]:
    volumes = [_total_search_volume(c) for c in candidates]
    volume_min = min(volumes) if volumes else 0
    volume_max = max(volumes) if volumes else 0
    for candidate in candidates:
        candidate["score"] = score_keyword(
            candidate["pc_qc"], candidate["mobile_qc"], candidate["comp_idx"],
            volume_min=volume_min, volume_max=volume_max,
        )
    return sorted(candidates, key=lambda c: c["score"], reverse=True)


def _score_candidates(raw_candidates: List[dict]) -> List[dict]:
    prepared = [
        candidate for candidate in _parse_candidates(raw_candidates)
        if _total_search_volume(candidate) >= _MIN_TOTAL_SEARCH_VOLUME
    ]
    return _score_parsed_candidates(prepared)


def _revalidate_cached_candidates(
    candidates: List[dict], *, excluded_keys: Optional[set[str]] = None
) -> List[dict]:
    """Rebuild cached records from current raw fields before merging.

    Cached scores are intentionally ignored: they may have been produced by a
    previous scoring formula. Records without the fields required for the
    current scoring formula are discarded instead of being guessed.
    """
    seen = set(excluded_keys or set())
    validated = []
    for cached in candidates:
        if not isinstance(cached, dict):
            continue
        keyword = str(cached.get("keyword", "")).strip()
        key = _keyword_key(keyword)
        if not keyword or key in seen:
            continue
        if _is_safety_blocked(keyword) or _is_purchase_intent_blocked(keyword):
            continue
        if "pc_qc" not in cached or "mobile_qc" not in cached or "comp_idx" not in cached:
            continue
        pc_raw = cached.get("pc_qc")
        mobile_raw = cached.get("mobile_qc")
        comp_idx = str(cached.get("comp_idx", "")).strip()
        if pc_raw is None or mobile_raw is None or not comp_idx:
            continue
        try:
            pc_qc = int(str(pc_raw).replace("<", "").replace(",", "").strip())
            mobile_qc = int(str(mobile_raw).replace("<", "").replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        record = dict(cached)
        record.update({
            "keyword": keyword,
            "pc_qc": pc_qc,
            "mobile_qc": mobile_qc,
            "comp_idx": comp_idx,
        })
        record.pop("score", None)
        seen.add(key)
        validated.append(record)
    return validated


def _cache_path(category_id: str) -> Path:
    return _CACHE_DIR / f"{category_id}.json"


def _load_cache(category_id: str) -> Optional[dict]:
    path = _cache_path(category_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(category_id: str, data: dict) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(category_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _is_cache_fresh(cache: dict, *, ttl_days: int) -> bool:
    fetched_at = cache.get("fetched_at")
    if not fetched_at:
        return False
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    age_days = (datetime.now(timezone.utc) - fetched).total_seconds() / 86400
    return age_days < ttl_days


def _filter_relevant_keywords(
    category_id: str,
    category_name: str,
    candidates: List[dict],
    *,
    category_profile: str = "",
    audit: Optional[dict] = None,
) -> Optional[List[dict]]:
    """Filter score-prefiltered candidates in <=100-item fail-closed batches.

    ``None`` means at least one batch failed. The caller must not write a cache
    for the fetched category in that case. ``audit`` is populated so the
    runner can distinguish an actual ``none`` response from an exception.
    """
    if audit is None:
        audit = {}
    audit.update(
        {
            "input_candidates": len(candidates),
            "batch_size": _LLM_BATCH_SIZE,
            "batch_count": (len(candidates) + _LLM_BATCH_SIZE - 1) // _LLM_BATCH_SIZE,
            "calls": 0,
            "completed_batches": 0,
            "removed_count": 0,
            "removed_keywords": [],
            "responses": [],
            "status": "pending",
        }
    )
    if not candidates:
        audit["status"] = "none"
        return candidates

    try:
        from agent.content.llm_client import call_llm
    except Exception as exc:  # noqa: BLE001 — caller skips the category
        logger.warning("Keyword relevance filter unavailable for %s: %s", category_id, exc)
        audit.update({"status": "error", "error": type(exc).__name__})
        return None

    profile = category_profile or _CATEGORY_PROFILES.get(category_id, category_name)
    system = (
        "당신은 한국어 정보성 블로그의 키워드 편집자입니다. "
        f"카테고리 이름은 '{category_name}'이고, 카테고리 성격은 '{profile}'입니다. "
        "각 키워드로 이 카테고리에 맞는 정보성 블로그 글 한 편을 쓸 수 있는가를 판단하세요. "
        "다음 기준에 해당하면 제거하세요. "
        "(1) 실시간 수치 조회: 삼성전자주가, 금시세, 일본환율, 나스닥, 춘천날씨, 주가지수. "
        "(2) 상품·기구·브랜드: 덤벨, 런닝머신, 로잉머신, 웹하드, 눈높이영어, 잉크앤페더. "
        "(3) 단독 조각어: 극복, 데이터, 백업, 앨범, 초보자, 매수, 고양이, 스포츠처럼 "
        "구체적인 정보 주제가 아닌 단어. "
        "(4) 카테고리 불일치: parenting/육아·살림에 비즈니스영어회화, "
        "travel/여행에 사우나·목욕탕처럼 카테고리와 맞지 않는 주제. "
        "(5) 음식 메뉴명: 노량진컵밥, 뭉티기, 계란볶음밥, 쭈꾸미처럼 단순 메뉴·음식명. "
        "(6) 자격증·시험 일정: SQLD, 컴퓨터활용능력시험일정처럼 자격증·시험 또는 일정 조회. "
        "(7) 식재료명·고기 부위명: 우둔살, 소고기우둔살, 냉동소고기, 오다리처럼 "
        "단순 식재료·고기 부위 자체를 가리키는 단어. "
        "(8) 사전·번역기·달력 같은 범용 도구 이름: 국어사전, 영어사전, 캘린더, 단어장처럼 "
        "특정 문제를 해결하는 방법이 아닌 범용 도구명. "
        "또한 인명·닉네임, 특정 학원·기관·지역 업체, 의료기관·의약품·시술·건강제품 브랜드는 제거하세요. "
        "일반적인 방법·비교·체크리스트·원리처럼 한 편의 정보성 글로 확장 가능한 주제는 유지하세요. "

        "출력은 제거할 항목 번호만 쉼표로 출력하세요(예: '3,7,12'). "
        "모든 항목을 유지하면 정확히 'none'만 출력하고 설명은 쓰지 마세요."
    )

    filtered: List[dict] = []
    for batch_start in range(0, len(candidates), _LLM_BATCH_SIZE):
        batch = candidates[batch_start:batch_start + _LLM_BATCH_SIZE]
        keyword_list = "\n".join(
            f"{index}. {candidate['keyword']}"
            for index, candidate in enumerate(batch, start=1)
        )
        user = f"카테고리: {category_name}\n{keyword_list}"
        audit["calls"] += 1
        try:
            response = call_llm(system=system, user=user)
        except Exception as exc:  # noqa: BLE001 — caller skips the category
            logger.warning(
                "Keyword relevance filter batch %d failed for %s: %s",
                batch_start // _LLM_BATCH_SIZE + 1,
                category_id,
                exc,
            )
            audit.update({"status": "error", "error": type(exc).__name__})
            return None

        response_text = str(response or "").strip()
        if re.fullmatch(r"(?i)(none|없음)", response_text):
            remove_indices = set()
            audit["responses"].append("none")
        elif re.fullmatch(r"\d+(?:\s*,\s*\d+)*", response_text):
            remove_indices = {int(n.strip()) for n in response_text.split(",")}
            if not remove_indices or any(index < 1 or index > len(batch) for index in remove_indices):
                logger.warning("Keyword relevance filter returned invalid indices for %s", category_id)
                audit.update({"status": "error", "error": "invalid_indices"})
                return None
            audit["responses"].append("removed")
        else:
            logger.warning("Keyword relevance filter returned an unparseable response for %s", category_id)
            audit.update({"status": "error", "error": "unparseable_response"})
            return None

        for index, candidate in enumerate(batch, start=1):
            if index in remove_indices:
                audit["removed_keywords"].append(candidate["keyword"])
            else:
                filtered.append(candidate)
        audit["removed_count"] = len(audit["removed_keywords"])
        audit["completed_batches"] += 1

    audit["status"] = "ok"
    return filtered


def _deduplicate_records(candidates: List[dict]) -> List[dict]:
    seen = set()
    deduplicated = []
    for candidate in candidates:
        keyword = str(candidate.get("keyword", "")).strip()
        key = _keyword_key(keyword)
        if not keyword or key in seen:
            continue
        seen.add(key)
        deduplicated.append(candidate)
    return deduplicated


def _two_char_fragments(keyword: str) -> set[str]:
    normalized = _keyword_key(keyword)
    if len(normalized) < 2:
        return set()
    return {
        normalized[index:index + 2]
        for index in range(len(normalized) - 1)
    }


def _limit_root_overlap(
    candidates: List[dict],
    *,
    max_per_fragment: int = _ROOT_MAX_PER_GROUP,
    audit: Optional[dict] = None,
    protected_keys: Optional[set[str]] = None,
) -> tuple[List[dict], List[dict]]:
    """Keep score-ordered candidates while capping each 2-char fragment.

    Candidates rejected by the cap are returned in score order as a reserve
    so callers can fill a shortfall without losing their identity in audit.
    ``protected_keys`` is used only for candidates selected from that reserve
    during the final merge; those candidates are allowed to survive the
    second cap pass without consuming another fragment slot.
    """
    fragment_counts: Dict[str, int] = {}
    limited = []
    reserve = []
    protected = protected_keys or set()
    if audit is not None:
        audit.setdefault("root_overlap_removed", [])
    for candidate in candidates:
        keyword = str(candidate.get("keyword", ""))
        keyword_key = _keyword_key(keyword)
        fragments = _two_char_fragments(keyword)
        saturated = [
            fragment
            for fragment in sorted(fragments)
            if fragment_counts.get(fragment, 0) >= max_per_fragment
        ]
        if saturated and keyword_key not in protected:
            if audit is not None:
                audit["root_overlap_removed"].append(keyword)
            reserve.append(candidate)
            continue
        limited.append(candidate)
        if keyword_key not in protected:
            for fragment in fragments:
                fragment_counts[fragment] = fragment_counts.get(fragment, 0) + 1
    return limited, reserve


def _sort_and_cap_candidates(
    candidates: List[dict], *, protected_keys: Optional[set[str]] = None
) -> List[dict]:
    ordered = sorted(
        _deduplicate_records(candidates),
        key=lambda candidate: (
            -float(candidate.get("score", 0)),
            -_total_search_volume(candidate),
            candidate.get("added_at", ""),
        ),
    )
    limited, _ = _limit_root_overlap(ordered, protected_keys=protected_keys)
    return limited[:_MAX_CACHED_CANDIDATES]


def _fetch_related_keywords_with_retry(
    seeds: List[str],
    *,
    attempts: int = 3,
    delay_seconds: float = 2.0,
) -> List[dict]:
    """Fetch related keywords, retrying Naver HTTP 429 with linear backoff."""
    from agent.content.keywords.naver_ad_client import NaverAdApiError, NaverAdClient

    client = NaverAdClient()
    max_attempts = max(1, attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            return client.related_keywords(seeds)
        except NaverAdApiError as exc:
            if "HTTP 429" not in str(exc) or attempt >= max_attempts:
                raise
            wait_seconds = delay_seconds * attempt
            logger.warning(
                "Naver keywordtool rate-limited for seeds=%s; retry %d/%d in %.1fs",
                seeds,
                attempt,
                max_attempts - 1,
                wait_seconds,
            )
            time.sleep(wait_seconds)
    return []


def expand_category(
    category_id: str,
    *,
    force: bool = False,
    cache_ttl_days: int = _DEFAULT_CACHE_TTL_DAYS,
    top_n: int = _DEFAULT_TOP_N,
    api_retry_attempts: int = 3,
    api_retry_delay_seconds: float = 2.0,
) -> dict:
    """Fetch/refresh one category's expanded-keyword cache. Network call
    (via naver_ad_client, imported here rather than at module level -- see
    module docstring) only happens when the cache is missing/stale/forced.
    """
    seeds = SEED_KEYWORDS_BY_CATEGORY.get(category_id)
    if not seeds:
        return {"category_id": category_id, "status": "no_seeds_configured", "added": 0}

    existing = _load_cache(category_id)
    if not force and existing and _is_cache_fresh(existing, ttl_days=cache_ttl_days):
        return {
            "category_id": category_id, "status": "cache_fresh", "added": 0,
            "cached_candidates": len(existing.get("candidates", [])),
        }

    from agent.content.config.categories import find_category
    category = find_category(category_id)
    core_keys = {_keyword_key(keyword) for keyword in (category.keywords if category else [])}
    existing_candidates = (existing or {}).get("candidates", [])
    already_cached = {
        _keyword_key(str(candidate.get("keyword", "")))
        for candidate in existing_candidates
    }

    from agent.content.keywords.naver_ad_client import NaverAdApiError

    try:
        raw = _fetch_related_keywords_with_retry(
            seeds,
            attempts=api_retry_attempts,
            delay_seconds=api_retry_delay_seconds,
        )
    except NaverAdApiError as exc:
        return {"category_id": category_id, "status": "api_error", "error": str(exc), "added": 0}

    filter_audit: Dict[str, object] = {
        "root_overlap_removed": [],
        "root_overlap_backfilled": [],
    }
    prepared = _parse_candidates(
        raw,
        excluded_keys=core_keys | already_cached,
        audit=filter_audit,
    )
    volume_filtered = [
        candidate for candidate in prepared
        if _total_search_volume(candidate) >= _MIN_TOTAL_SEARCH_VOLUME
    ]
    scored = _score_parsed_candidates(volume_filtered)
    llm_input = scored[:_LLM_PREFILTER_SIZE]
    llm_audit = {
        "input_candidates": len(llm_input),
        "batch_size": _LLM_BATCH_SIZE,
        "batch_count": (len(llm_input) + _LLM_BATCH_SIZE - 1) // _LLM_BATCH_SIZE,
        "calls": 0,
        "completed_batches": 0,
        "removed_count": 0,
        "removed_keywords": [],
        "responses": [],
        "status": "pending",
    }
    relevant = _filter_relevant_keywords(
        category_id,
        category.name if category else category_id,
        llm_input,
        category_profile=_CATEGORY_PROFILES.get(category_id, category_id),
        audit=llm_audit,
    )
    filter_audit["parsed_candidates"] = len(prepared)
    filter_audit["volume_eligible_candidates"] = len(volume_filtered)
    filter_audit["score_sorted_candidates"] = len(scored)
    filter_audit["llm_input_candidates"] = len(llm_input)
    if relevant is None:
        return {
            "category_id": category_id,
            "status": "llm_filter_error",
            "added": 0,
            "cached_candidates": len(existing_candidates),
            "filter_audit": filter_audit,
            "llm_filter": llm_audit,
        }

    limited_candidates, reserve_candidates = _limit_root_overlap(
        relevant,
        audit=filter_audit,
    )
    new_candidates = limited_candidates[:top_n]
    backfilled_keys: set[str] = set()
    if len(new_candidates) < top_n:
        backfilled = reserve_candidates[:top_n - len(new_candidates)]
        new_candidates.extend(backfilled)
        filter_audit["root_overlap_backfilled"] = [
            candidate["keyword"] for candidate in backfilled
        ]
        backfilled_keys = {
            _keyword_key(candidate["keyword"]) for candidate in backfilled
        }
    protected_backfill_keys = backfilled_keys
    added_before_merge = len(new_candidates)

    now = datetime.now(timezone.utc).isoformat()
    for c in new_candidates:
        c["added_at"] = now

    existing_clean = [
        candidate
        for candidate in _revalidate_cached_candidates(
            existing_candidates,
            excluded_keys=core_keys,
        )
        if _total_search_volume(candidate) >= _MIN_TOTAL_SEARCH_VOLUME
    ]
    rescored_merge = _score_parsed_candidates(
        [dict(candidate) for candidate in existing_clean + new_candidates]
    )
    merged = _sort_and_cap_candidates(
        rescored_merge,
        protected_keys=protected_backfill_keys,
    )
    new_keys = {_keyword_key(candidate["keyword"]) for candidate in new_candidates}
    added_after_merge = sum(
        1 for candidate in merged if _keyword_key(candidate["keyword"]) in new_keys
    )

    cache_data = {
        "fetched_at": now,
        "seeds": seeds,
        "candidates": merged,
    }
    _save_cache(category_id, cache_data)

    return {
        "category_id": category_id,
        "status": "ok",
        "added": added_after_merge,
        "added_before_merge": added_before_merge,
        "total_cached": len(cache_data["candidates"]),
        "filter_audit": filter_audit,
        "llm_filter": llm_audit,
    }


def expand_all(
    categories: Optional[Sequence[str]] = None,
    *,
    category_delay_seconds: float = 0.0,
    **kwargs,
) -> List[dict]:
    ids = list(categories) if categories else list(SEED_KEYWORDS_BY_CATEGORY.keys())
    results = []
    for index, category_id in enumerate(ids):
        if index and category_delay_seconds > 0:
            time.sleep(category_delay_seconds)
        results.append(expand_category(category_id, **kwargs))
    return results


def get_expanded_keywords(category_id: str, *, min_score: float = 0.0) -> List[str]:
    """Read path for categories.get_effective_keywords(): cached expanded
    keywords scoring >= min_score. Pure local file read -- no network, no
    naver_ad_client import, safe to call from the hot content-gen path."""
    cache = _load_cache(category_id)
    if not cache:
        return []
    return [c["keyword"] for c in cache.get("candidates", []) if c.get("score", 0) >= min_score]
