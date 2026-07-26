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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent / "cache"
_DEFAULT_CACHE_TTL_DAYS = 7
_DEFAULT_TOP_N = 20

# Representative seeds per category, hand-picked from categories.py's
# existing core keyword lists. Naver's keywordstool accepts at most 5
# seeds per call (see naver_ad_client._MAX_SEEDS_PER_CALL).
SEED_KEYWORDS_BY_CATEGORY: Dict[str, List[str]] = {
    "self-dev": ["자기계발", "습관", "루틴"],
    "health": ["건강", "운동", "다이어트"],
    "finance": ["재테크", "가계부", "저축"],
    "it-tech": ["IT", "노트북", "생산성도구"],
    "parenting": ["육아", "이유식", "유아용품"],
}

# Reduced from the original RESEARCH_AGENT.md 5-factor formula:
#   score = search_volume*0.25 + competition_inverse*0.30 + cpc*0.20
#         + trend_slope*0.15 + platform_fit*0.10
# Naver's keywordstool only returns search volume + competition, so the
# 0.25:0.30 ratio is renormalized to sum to 1.0 across just those two
# factors (0.30/(0.25+0.30)=0.545..., rounded to 0.45:0.55 per the
# explicit call to weight competition higher -- new blogs should prefer
# low-competition keywords). cpc/trend/platform_fit keep weight 0.0 until
# a real data source exists for them (see score_keyword's docstring).
_VOLUME_WEIGHT = 0.45
_COMPETITION_WEIGHT = 0.55
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
    *other candidates fetched in the same batch* -- search_volume_norm is
    relative to siblings, not an absolute scale, per design.

    cpc_norm / trend_slope / platform_fit: accepted but currently
    unweighted (0.0) -- no data source yet. Wiring one in later (e.g.
    trend_slope computed from cache history once enough weekly snapshots
    exist) is a 2-constant + 1-argument change, not a signature change.
    """
    search_volume_norm = _normalize(pc_qc + mobile_qc, minimum=volume_min, maximum=volume_max)
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


def _score_candidates(raw_candidates: List[dict]) -> List[dict]:
    parsed = []
    for row in raw_candidates:
        keyword = str(row.get("relKeyword", "")).strip()
        if not keyword:
            continue
        parsed.append({
            "keyword": keyword,
            "pc_qc": _parse_qc(row.get("monthlyPcQcCnt")),
            "mobile_qc": _parse_qc(row.get("monthlyMobileQcCnt")),
            "comp_idx": row.get("compIdx", ""),
        })

    volumes = [c["pc_qc"] + c["mobile_qc"] for c in parsed]
    volume_min = min(volumes) if volumes else 0
    volume_max = max(volumes) if volumes else 0
    for c in parsed:
        c["score"] = score_keyword(
            c["pc_qc"], c["mobile_qc"], c["comp_idx"],
            volume_min=volume_min, volume_max=volume_max,
        )
    return sorted(parsed, key=lambda c: c["score"], reverse=True)


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
    category_id: str, category_name: str, candidates: List[dict]
) -> List[dict]:
    """Drop candidates an LLM flags as clearly unrelated to the category
    (person names/nicknames, school or academy names, other obviously
    off-topic proper nouns) -- a light pass, not real semantic filtering.

    Best-effort: any failure (missing call_llm, LLM error, unparseable
    response, or the LLM flagging literally everything as unrelated --
    almost certainly a bad response, not a real signal) returns
    ``candidates`` unchanged. This must never shrink the seed pool because
    the filter itself broke.
    """
    if not candidates:
        return candidates
    try:
        from agent.content.llm_client import call_llm
    except Exception:  # noqa: BLE001 — filter is optional, never blocking
        return candidates

    keyword_list = "\n".join(f"{i}. {c['keyword']}" for i, c in enumerate(candidates, start=1))
    system = (
        "You review Korean keyword candidates for a blog content category. "
        "Only flag a keyword as unrelated if it is CLEARLY unrelated -- a "
        "person's name/nickname (e.g. ending in 쌤/선생님/강사), a specific "
        "school/academy/institution name, or an obviously off-topic proper "
        "noun. When in doubt, keep it. Output ONLY a comma-separated list of "
        'the item numbers to REMOVE (e.g. "3,7,12"). Output "none" if every '
        "keyword is fine. No explanation."
    )
    user = f"카테고리: {category_name}\n\n{keyword_list}"

    try:
        response = call_llm(system=system, user=user)
    except Exception as exc:  # noqa: BLE001 — filter is optional, never blocking
        logger.warning("Keyword relevance filter failed for %s: %s", category_id, exc)
        return candidates

    if re.search(r"(?i)^\s*(none|없음)\s*$", response.strip()):
        return candidates
    remove_indices = {int(n) for n in re.findall(r"\d+", response)}
    if not remove_indices:
        return candidates

    filtered = [c for i, c in enumerate(candidates, start=1) if i not in remove_indices]
    if not filtered:
        logger.warning(
            "Keyword relevance filter for %s flagged all %d candidates -- "
            "ignoring, keeping unfiltered", category_id, len(candidates),
        )
        return candidates
    return filtered


def expand_category(
    category_id: str,
    *,
    force: bool = False,
    cache_ttl_days: int = _DEFAULT_CACHE_TTL_DAYS,
    top_n: int = _DEFAULT_TOP_N,
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
    core_keywords = set(category.keywords) if category else set()
    already_cached = {c["keyword"] for c in (existing or {}).get("candidates", [])}

    from agent.content.keywords.naver_ad_client import NaverAdApiError, NaverAdClient

    try:
        raw = NaverAdClient().related_keywords(seeds)
    except NaverAdApiError as exc:
        return {"category_id": category_id, "status": "api_error", "error": str(exc), "added": 0}

    scored = _score_candidates(raw)
    new_candidates = [
        c for c in scored
        if c["keyword"] not in core_keywords and c["keyword"] not in already_cached
    ][:top_n]

    now = datetime.now(timezone.utc).isoformat()
    for c in new_candidates:
        c["added_at"] = now

    merged = (existing.get("candidates", []) if existing else []) + new_candidates
    merged = _filter_relevant_keywords(category_id, category.name if category else category_id, merged)

    cache_data = {
        "fetched_at": now,
        "seeds": seeds,
        "candidates": merged,
    }
    _save_cache(category_id, cache_data)

    return {
        "category_id": category_id, "status": "ok",
        "added": len(new_candidates), "total_cached": len(cache_data["candidates"]),
    }


def expand_all(categories: Optional[Sequence[str]] = None, **kwargs) -> List[dict]:
    ids = list(categories) if categories else list(SEED_KEYWORDS_BY_CATEGORY.keys())
    return [expand_category(cid, **kwargs) for cid in ids]


def get_expanded_keywords(category_id: str, *, min_score: float = 0.0) -> List[str]:
    """Read path for categories.get_effective_keywords(): cached expanded
    keywords scoring >= min_score. Pure local file read -- no network, no
    naver_ad_client import, safe to call from the hot content-gen path."""
    cache = _load_cache(category_id)
    if not cache:
        return []
    return [c["keyword"] for c in cache.get("candidates", []) if c.get("score", 0) >= min_score]
