from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from agent.content.keywords import keyword_expander as ke
from agent.content.keywords.naver_ad_client import NaverAdApiError


def _row(keyword: str, pc: int = 100, mobile: int = 100, comp: str = "낮음") -> dict:
    return {
        "relKeyword": keyword,
        "monthlyPcQcCnt": pc,
        "monthlyMobileQcCnt": mobile,
        "compIdx": comp,
    }


def _candidate(
    keyword: str,
    score: float,
    total: int,
    added_at: str,
) -> dict:
    return {
        "keyword": keyword,
        "pc_qc": total // 2,
        "mobile_qc": total - total // 2,
        "comp_idx": "낮음",
        "score": score,
        "added_at": added_at,
    }


def test_seed_lists_are_specific_and_include_travel() -> None:
    assert ke.SEED_KEYWORDS_BY_CATEGORY == {
        "self-dev": ["시간관리", "독서습관", "집중력향상"],
        "health": ["다이어트식단", "홈트레이닝", "수면부족"],
        "finance": ["적금추천", "신용점수", "ETF추천"],
        "it-tech": ["엑셀함수", "클라우드백업", "사진정리"],
        "parenting": ["유아영어", "아기놀이", "아기수면교육"],
        "travel": ["국내여행지", "당일치기여행", "캠핑장추천"],
    }


def test_related_keyword_fetch_retries_http_429(monkeypatch) -> None:
    attempts = 0
    sleeps = []

    class FakeClient:
        def related_keywords(self, seeds):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise NaverAdApiError("Naver Ad API HTTP 429: too many requests")
            return [_row("재시도성공", 100, 100)]

    monkeypatch.setattr(
        "agent.content.keywords.naver_ad_client.NaverAdClient",
        FakeClient,
    )
    monkeypatch.setattr(ke.time, "sleep", sleeps.append)

    assert ke._fetch_related_keywords_with_retry(
        ["시간관리"], attempts=3, delay_seconds=2
    ) == [_row("재시도성공", 100, 100)]
    assert attempts == 3
    assert sleeps == [2, 4]


def test_expand_all_waits_between_categories(monkeypatch) -> None:
    sleeps = []
    monkeypatch.setattr(ke.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        ke,
        "expand_category",
        lambda category_id, **kwargs: {"category_id": category_id, "status": "ok"},
    )

    result = ke.expand_all(
        categories=["self-dev", "health", "finance"],
        category_delay_seconds=3,
    )

    assert [item["category_id"] for item in result] == ["self-dev", "health", "finance"]
    assert sleeps == [3, 3]


def test_score_weights_prioritize_search_volume() -> None:
    assert ke._VOLUME_WEIGHT == 0.60
    assert ke._COMPETITION_WEIGHT == 0.40
    assert ke.score_keyword(
        10,
        10,
        "낮음",
        volume_min=20,
        volume_max=100,
    ) == 0.4


def test_score_uses_log_normalized_search_volume() -> None:
    total = 100
    minimum = 20
    maximum = 10000
    log_norm = (math.log10(total + 1) - math.log10(minimum + 1)) / (
        math.log10(maximum + 1) - math.log10(minimum + 1)
    )
    expected = round(log_norm * 0.60 + 0.1 * 0.40, 4)

    assert ke.score_keyword(
        50,
        50,
        "높음",
        volume_min=minimum,
        volume_max=maximum,
    ) == expected


def test_deterministic_safety_filter_blocks_medical_products_procedures_and_local_businesses() -> None:
    blocked = [
        "제니칼",
        "부평식욕억제제",
        "아디포고주파",
        "잠실한의원다이어트",
        "대구시내헬스장",
        "안국건강",
        "고농축행감환",
        "머슬부스터",
        "알파CD",
    ]
    kept = [
        "한달에5키로감량",
        "빠르게살빼기",
        "초보자 근력 운동",
    ]

    assert all(ke._is_deterministically_blocked(keyword) for keyword in blocked)
    assert not any(ke._is_deterministically_blocked(keyword) for keyword in kept)


def test_health_product_suffix_does_not_block_general_topics_ending_in_hwan_or_jeong() -> None:
    kept = [
        "목표설정",
        "노트북설정",
        "스마트폰설정",
        "윈도우설정",
        "여행일정",
        "국내여행일정",
        "운동일정",
        "대출상환",
        "포인트전환",
        "자기결정",
    ]

    assert not any(ke._is_deterministically_blocked(keyword) for keyword in kept)


def test_purchase_intent_and_volume_suffix_filters_are_deterministic() -> None:
    blocked = [
        "정기배송",
        "무료체험",
        "체험팩",
        "샘플",
        "밀키트",
        "배달",
        "택배",
        "직구",
        "쇼핑몰",
        "최저가",
        "할인",
        "쿠폰",
        "렌탈",
        "중고",
        "세트",
        "닭가슴살200G",
        "프로틴1kg",
    ]
    kept = ["제품후기", "여행추천", "노트북리뷰", "목표설정"]

    assert all(ke._is_deterministically_blocked(keyword) for keyword in blocked)
    assert not any(ke._is_deterministically_blocked(keyword) for keyword in kept)


def test_requested_purchase_terms_are_deterministic_except_overblocking_terms() -> None:
    blocked = [
        "영양제", "보조제", "유산균", "프로바이오틱스", "콜라겐", "루테인", "오메가3",
        "멜라토닌", "락티움", "추출물", "쉐이크", "스무디", "착즙", "운동기구", "머신",
        "덤벨", "철봉", "마사지기", "안마기", "워킹패드", "헬스장", "필라테스",
        "크로스핏", "요가원", "도시락", "볶음밥", "컵밥",
        "KODEX", "코덱스", "TIGER", "ACE", "PLUS", "SOL", "TQQQ", "SOXL", "QQQ",
        "주가", "시세", "환율", "증시", "지수", "매수종목", "추천종목", "종목추천",
        "유망주", "투자클럽",
    ]

    assert all(ke._is_purchase_intent_blocked(keyword) for keyword in blocked)


def test_known_cross_boundary_phrases_are_removed_before_purchase_matching() -> None:
    kept = [
        "파주가볼만한곳",
        "남양주가볼만한곳",
        "경기도광주가볼만한곳",
        "가평빠지수상레저",
        "엑셀자격증시험",
    ]
    blocked = [
        "코스피지수",
        "삼성전자주가",
        "주가전망",
        "상장지수펀드",
        "미국증시실시간",
        "금시세전망",
        "엔화환율전망",
        "아시아증시마감",
        "코스닥지수전망",
        "원달러환율전망",
        "현대차주가전망",
        "카세트USB",
        "카세트테이프MP3변환",
        "홈트세트",
        "KTX할인",
        "중고헬스기구",
        "런닝머신대여",
    ]

    assert all(not ke._is_purchase_intent_blocked(keyword) for keyword in kept)
    assert all(ke._is_purchase_intent_blocked(keyword) for keyword in blocked)


def test_short_english_purchase_terms_require_alpha_boundaries() -> None:
    kept = [
        "DATACENTER",
        "SPACE",
        "INTERFACE",
        "MARKETPLACE",
        "SOLUTION",
        "SOLIDWORKS",
        "CONSOLE",
    ]
    blocked = [
        "ACE미국배당다우존스",
        "SOL미국배당다우존스",
        "TIGERETF",
        "KODEX200",
        "TQQQ",
        "SOXL",
        "PLUSETF",
        "SOLETF",
        "차이나전기차SOLACTIVE",
    ]

    assert all(not ke._is_purchase_intent_blocked(keyword) for keyword in kept)
    assert all(ke._is_purchase_intent_blocked(keyword) for keyword in blocked)


def test_root_overlap_cap_keeps_at_most_eight_candidates_per_two_char_fragment() -> None:
    candidates = [
        _candidate("이유식", 0.9, 900, "2026-08-01T00:00:00+00:00"),
        _candidate("초기이유식", 0.8, 800, "2026-08-01T00:00:01+00:00"),
        _candidate("이유식식단", 0.7, 700, "2026-08-01T00:00:02+00:00"),
        _candidate("이유식배달", 0.6, 600, "2026-08-01T00:00:03+00:00"),
        _candidate("이유식레시피", 0.5, 500, "2026-08-01T00:00:04+00:00"),
        _candidate("이유식책", 0.4, 400, "2026-08-01T00:00:05+00:00"),
        _candidate("이유식큐브", 0.3, 300, "2026-08-01T00:00:06+00:00"),
        _candidate("이유식단계", 0.2, 200, "2026-08-01T00:00:07+00:00"),
        _candidate("이유식용기", 0.1, 100, "2026-08-01T00:00:08+00:00"),
    ]

    audit = {}
    limited = ke._limit_root_overlap(candidates, audit=audit)

    assert [candidate["keyword"] for candidate in limited] == [
        "이유식",
        "초기이유식",
        "이유식식단",
        "이유식배달",
        "이유식레시피",
        "이유식책",
        "이유식큐브",
        "이유식단계",
    ]
    assert audit["root_overlap_removed"] == ["이유식용기"]


def test_root_overlap_cap_limits_english_examples_by_two_char_fragment() -> None:
    candidates = [
        _candidate(
            f"영어{suffix}",
            0.9 - index / 10,
            900 - index * 10,
            f"2026-08-01T00:00:0{index}+00:00",
        )
        for index, suffix in enumerate(
            ["단어", "발음", "사전", "회화", "문법", "공부", "읽기", "쓰기", "듣기"]
        )
    ]

    limited = ke._limit_root_overlap(candidates)

    assert len(limited) == 8
    assert [candidate["keyword"] for candidate in limited] == [
        "영어단어",
        "영어발음",
        "영어사전",
        "영어회화",
        "영어문법",
        "영어공부",
        "영어읽기",
        "영어쓰기",
    ]


def test_default_top_n_is_30_and_merge_cap_remains_30() -> None:
    assert ke._DEFAULT_TOP_N == 30
    assert ke._MAX_CACHED_CANDIDATES == 30


def test_raw_candidates_are_deduplicated_and_total_volume_floor_is_20() -> None:
    scored = ke._score_candidates(
        [
            _row("주제A", pc=10, mobile=10),
            _row("주제 A", pc=10, mobile=10),
            _row("주제B", pc=9, mobile=10),
        ]
    )

    assert [candidate["keyword"] for candidate in scored] == ["주제A"]


def test_llm_filter_failure_does_not_write_or_replace_existing_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ke, "_CACHE_DIR", tmp_path)
    existing = {
        "fetched_at": "2026-07-23T00:00:00+00:00",
        "seeds": ke.SEED_KEYWORDS_BY_CATEGORY["health"],
        "candidates": [_candidate("기존안전키워드", 0.8, 200, "2026-07-23T00:00:00+00:00")],
    }
    cache_path = tmp_path / "health.json"
    cache_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

    class FakeClient:
        def related_keywords(self, seeds):
            return [_row("새키워드", 100, 100)]

    monkeypatch.setattr(
        "agent.content.keywords.naver_ad_client.NaverAdClient",
        FakeClient,
    )
    monkeypatch.setattr(ke, "_filter_relevant_keywords", lambda *args, **kwargs: None)

    result = ke.expand_category("health", force=True)

    assert result["status"] == "llm_filter_error"
    assert json.loads(cache_path.read_text(encoding="utf-8")) == existing


def test_llm_filter_exception_is_fail_closed(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("agent.content.llm_client.call_llm", fail)

    assert ke._filter_relevant_keywords("health", "건강", [{"keyword": "안전한주제"}]) is None


def test_llm_filter_batches_at_100_and_combines_removed_indices(monkeypatch) -> None:
    candidates = [{"keyword": f"키워드{i:03d}"} for i in range(205)]
    calls = []

    def fake_call_llm(*, system, user):
        calls.append((system, user))
        if len(calls) == 1:
            return "1,3"
        if len(calls) == 2:
            return "none"
        return "2"

    monkeypatch.setattr("agent.content.llm_client.call_llm", fake_call_llm)
    audit = {}

    filtered = ke._filter_relevant_keywords(
        "health",
        "건강/헬스",
        candidates,
        category_profile="운동·수면·영양 중심의 실전 건강 정보",
        audit=audit,
    )

    assert filtered is not None
    assert len(calls) == 3
    assert [len(user.splitlines()) - 1 for _, user in calls] == [100, 100, 5]
    assert [candidate["keyword"] for candidate in filtered[:3]] == [
        "키워드001",
        "키워드003",
        "키워드004",
    ]
    assert filtered[-1]["keyword"] == "키워드204"
    assert audit["input_candidates"] == 205
    assert audit["batch_size"] == 100
    assert audit["batch_count"] == 3
    assert audit["calls"] == 3
    assert audit["removed_count"] == 3
    assert audit["removed_keywords"] == ["키워드000", "키워드002", "키워드201"]
    assert audit["status"] == "ok"


def test_llm_filter_batch_failure_returns_none_and_records_failure(monkeypatch) -> None:
    candidates = [{"keyword": f"키워드{i:03d}"} for i in range(101)]
    calls = 0

    def fake_call_llm(*, system, user):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "none"
        raise RuntimeError("second batch failed")

    monkeypatch.setattr("agent.content.llm_client.call_llm", fake_call_llm)
    audit = {}

    assert ke._filter_relevant_keywords(
        "health",
        "건강/헬스",
        candidates,
        audit=audit,
    ) is None
    assert calls == 2
    assert audit["batch_count"] == 2
    assert audit["calls"] == 2
    assert audit["status"] == "error"
    assert audit["completed_batches"] == 1


def test_llm_prompt_contains_category_profile_and_requested_relevance_rules(monkeypatch) -> None:
    captured = {}

    def fake_call_llm(*, system, user):
        captured["system"] = system
        captured["user"] = user
        return "none"

    monkeypatch.setattr("agent.content.llm_client.call_llm", fake_call_llm)

    result = ke._filter_relevant_keywords(
        "parenting",
        "육아/살림",
        [{"keyword": "비즈니스영어회화"}],
        category_profile="0~7세 자녀 부모 대상의 따뜻하고 실용적인 육아·살림 정보",
    )

    assert result == [{"keyword": "비즈니스영어회화"}]
    assert "육아/살림" in captured["system"]
    assert "따뜻하고 실용적인 육아·살림 정보" in captured["system"]
    assert "정보성 블로그 글 한 편을 쓸 수 있는가" in captured["system"]
    for phrase in (
        "실시간 수치 조회",
        "상품·기구·브랜드",
        "단독 조각어",
        "카테고리 불일치",
        "음식 메뉴명",
        "자격증·시험 일정",
        "식재료명·고기 부위명",
        "우둔살",
        "사전·번역기·달력",
        "국어사전",
        "비즈니스영어회화",
        "사우나",
        "목욕탕",
        "SQLD",
    ):
        assert phrase in captured["system"]


def test_expand_scores_and_caps_before_and_after_llm_in_the_requested_order(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ke, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(ke, "_limit_root_overlap", lambda candidates, **kwargs: candidates)
    raw = [
        _row(f"주{i:03d}어", pc=1000 + i, mobile=1000 + i)
        for i in range(120)
    ]
    captured = {}

    class FakeClient:
        def related_keywords(self, seeds):
            return raw

    def fake_filter(category_id, category_name, candidates, **kwargs):
        captured["candidates"] = list(candidates)
        kwargs["audit"]["status"] = "ok"
        return candidates

    monkeypatch.setattr("agent.content.keywords.naver_ad_client.NaverAdClient", FakeClient)
    monkeypatch.setattr(ke, "_filter_relevant_keywords", fake_filter)

    result = ke.expand_category("health", force=True, top_n=30)
    saved = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert len(captured["candidates"]) == 100
    assert captured["candidates"] == sorted(
        captured["candidates"], key=lambda candidate: candidate["score"], reverse=True
    )
    assert len(saved["candidates"]) == 30
    assert result["llm_filter"]["input_candidates"] == 100
    assert result["llm_filter"]["status"] == "ok"


def test_root_cap_is_eight_after_llm_filter(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ke, "_CACHE_DIR", tmp_path)
    raw = [_row(f"다이어트{i}운동", pc=1000 - i, mobile=1000 - i) for i in range(10)]

    class FakeClient:
        def related_keywords(self, seeds):
            return raw

    monkeypatch.setattr("agent.content.keywords.naver_ad_client.NaverAdClient", FakeClient)
    monkeypatch.setattr(ke, "_filter_relevant_keywords", lambda *args, **kwargs: args[2])

    ke.expand_category("health", force=True, top_n=30)
    saved = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))

    assert len(saved["candidates"]) == 8


def test_merge_tie_breaks_by_volume_then_oldest_added_at() -> None:
    old = "2026-07-23T00:00:00+00:00"
    new = "2026-08-01T00:00:00+00:00"
    candidates = [
        _candidate("동점낮은검색량", 0.5, 100, old),
        _candidate("동점높은검색량", 0.5, 200, new),
        _candidate("동점오래된검색량", 0.5, 200, old),
    ]

    sorted_candidates = ke._sort_and_cap_candidates(candidates)

    assert [candidate["keyword"] for candidate in sorted_candidates] == [
        "동점오래된검색량",
        "동점높은검색량",
        "동점낮은검색량",
    ]


def test_merge_is_rescored_and_capped_at_30_with_tie_breakers(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ke, "_CACHE_DIR", tmp_path)
    old = "2026-07-23T00:00:00+00:00"
    existing = {
        "fetched_at": old,
        "seeds": ke.SEED_KEYWORDS_BY_CATEGORY["health"],
        "candidates": [
            _candidate("기존키워드", 0.7, 700, old),
        ],
    }
    (tmp_path / "health.json").write_text(
        json.dumps(existing, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def related_keywords(self, seeds):
            return [
                _row("신규낮은검색량", 10, 10),
                _row("신규높은검색량", 500, 500),
                _row("신규중간검색량", 200, 200),
            ]

    monkeypatch.setattr(
        "agent.content.keywords.naver_ad_client.NaverAdClient",
        FakeClient,
    )
    monkeypatch.setattr(ke, "_filter_relevant_keywords", lambda _category_id, _name, candidates, **kwargs: candidates)
    monkeypatch.setattr(ke, "_MAX_CACHED_CANDIDATES", 2)

    result = ke.expand_category("health", force=True, top_n=3)
    saved = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert len(saved["candidates"]) == 2
    assert [candidate["keyword"] for candidate in saved["candidates"]] == [
        "신규높은검색량",
        "기존키워드",
    ]


def test_existing_cache_revalidation_drops_blocked_and_incomplete_candidates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ke, "_CACHE_DIR", tmp_path)
    old = "2026-07-23T00:00:00+00:00"
    existing = {
        "fetched_at": old,
        "seeds": ke.SEED_KEYWORDS_BY_CATEGORY["health"],
        "candidates": [
            _candidate("기존안전키워드", 0.01, 600, old),
            _candidate("도시락", 0.99, 900, old),
            {"keyword": "누락검색량", "score": 0.99, "added_at": old},
            _candidate("다이어트약", 0.99, 900, old),
        ],
    }
    (tmp_path / "health.json").write_text(
        json.dumps(existing, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def related_keywords(self, seeds):
            return [_row("신규키워드", 300, 300)]

    monkeypatch.setattr("agent.content.keywords.naver_ad_client.NaverAdClient", FakeClient)
    monkeypatch.setattr(
        ke,
        "_filter_relevant_keywords",
        lambda _category_id, _name, candidates, **kwargs: candidates,
    )

    result = ke.expand_category("health", force=True)
    saved = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    keywords = [candidate["keyword"] for candidate in saved["candidates"]]

    assert result["status"] == "ok"
    assert "기존안전키워드" in keywords
    assert "도시락" not in keywords
    assert "누락검색량" not in keywords
    assert "다이어트약" not in keywords


def test_existing_cache_score_is_recomputed_before_merge(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ke, "_CACHE_DIR", tmp_path)
    old = "2026-07-23T00:00:00+00:00"
    existing_candidate = _candidate("기존고검색량", 0.0, 2000, old)
    existing_candidate["comp_idx"] = "높음"
    existing = {
        "fetched_at": old,
        "seeds": ke.SEED_KEYWORDS_BY_CATEGORY["health"],
        "candidates": [existing_candidate],
    }
    (tmp_path / "health.json").write_text(
        json.dumps(existing, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def related_keywords(self, seeds):
            return [_row("신규저검색량", 100, 100, "낮음")]

    monkeypatch.setattr("agent.content.keywords.naver_ad_client.NaverAdClient", FakeClient)
    monkeypatch.setattr(
        ke,
        "_filter_relevant_keywords",
        lambda _category_id, _name, candidates, **kwargs: candidates,
    )

    result = ke.expand_category("health", force=True)
    saved = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert [candidate["keyword"] for candidate in saved["candidates"]] == [
        "기존고검색량",
        "신규저검색량",
    ]
    assert saved["candidates"][0]["score"] == 0.64
    assert saved["candidates"][0]["score"] != existing_candidate["score"]


def test_added_counts_only_new_candidates_remaining_after_merge(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ke, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(ke, "_MAX_CACHED_CANDIDATES", 2)
    old = "2026-07-23T00:00:00+00:00"
    existing = {
        "fetched_at": old,
        "seeds": ke.SEED_KEYWORDS_BY_CATEGORY["health"],
        "candidates": [_candidate("기존고득점", 0.01, 2000, old)],
    }
    (tmp_path / "health.json").write_text(
        json.dumps(existing, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def related_keywords(self, seeds):
            return [
                _row("신규A", 1000, 0),
                _row("신규B", 900, 0),
                _row("신규C", 800, 0),
            ]

    monkeypatch.setattr("agent.content.keywords.naver_ad_client.NaverAdClient", FakeClient)
    monkeypatch.setattr(
        ke,
        "_filter_relevant_keywords",
        lambda _category_id, _name, candidates, **kwargs: candidates,
    )

    result = ke.expand_category("health", force=True, top_n=3)

    assert result["status"] == "ok"
    assert result["added_before_merge"] == 3
    assert result["added"] == 1
    assert result["total_cached"] == 2


def test_expanded_keywords_read_path_has_no_network_and_preserves_core_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ke, "_CACHE_DIR", tmp_path)
    (tmp_path / "health.json").write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "candidates": [
                    _candidate("확장키워드", 0.8, 200, "2026-07-23T00:00:00+00:00"),
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert ke.get_expanded_keywords("health") == ["확장키워드"]
