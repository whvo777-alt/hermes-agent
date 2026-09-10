"""Tests for batching Naver keyword-expansion seed calls."""

from agent.content.keywords import keyword_expander as ke
from agent.content.keywords.naver_ad_client import NaverAdApiError, _MAX_SEEDS_PER_CALL


def _rows(*keywords: str) -> list[dict]:
    return [{"relKeyword": keyword} for keyword in keywords]


class FakeClient:
    def __init__(self, responses=None, failures=None):
        self.calls = []
        self.responses = responses or {}
        self.failures = set(failures or ())

    def related_keywords(self, seeds):
        batch = tuple(seeds)
        self.calls.append(list(seeds))
        if batch in self.failures:
            raise NaverAdApiError("Naver Ad API HTTP 500: fake failure")
        return self.responses.get(batch, _rows(*(f"{seed}연관" for seed in seeds)))


def test_three_seeds_are_sent_in_one_call():
    client = FakeClient()

    ke._related_keywords_in_batches(client, ["하나", "둘", "셋"])

    assert client.calls == [["하나", "둘", "셋"]]


def test_five_seeds_are_sent_in_one_call_at_the_limit():
    client = FakeClient()
    seeds = ["하나", "둘", "셋", "넷", "다섯"]

    ke._related_keywords_in_batches(client, seeds)

    assert client.calls == [seeds]


def test_ten_seeds_are_split_into_two_batches_of_five():
    client = FakeClient()
    seeds = [f"씨앗{index}" for index in range(10)]

    ke._related_keywords_in_batches(client, seeds)

    assert client.calls == [seeds[:5], seeds[5:]]


def test_twelve_seeds_are_split_into_five_five_two():
    client = FakeClient()
    seeds = [f"씨앗{index}" for index in range(12)]

    ke._related_keywords_in_batches(client, seeds)

    assert client.calls == [seeds[:5], seeds[5:10], seeds[10:]]


def test_no_batch_exceeds_naver_seed_limit():
    client = FakeClient()
    seeds = [f"씨앗{index}" for index in range(13)]

    ke._related_keywords_in_batches(client, seeds)

    assert all(len(batch) <= _MAX_SEEDS_PER_CALL for batch in client.calls)


def test_duplicate_related_keywords_from_two_batches_are_merged_once():
    seeds = [f"씨앗{index}" for index in range(10)]
    client = FakeClient(
        responses={
            tuple(seeds[:5]): _rows("공통", "첫번째"),
            tuple(seeds[5:]): _rows("공통", "두번째"),
        }
    )

    result = ke._related_keywords_in_batches(client, seeds)

    assert [row["relKeyword"] for row in result] == ["공통", "첫번째", "두번째"]


def test_failed_batch_does_not_discard_later_results():
    seeds = ["실패", "첫번째", "두번째", "세번째", "네번째", "성공"]
    client = FakeClient(failures={tuple(seeds[:5])})

    result = ke._related_keywords_in_batches(client, seeds)

    assert client.calls == [seeds[:5], seeds[5:]]
    assert [row["relKeyword"] for row in result] == ["성공연관"]


def test_all_failed_batches_return_empty_list_without_raising():
    seeds = [f"씨앗{index}" for index in range(10)]
    client = FakeClient(failures={tuple(seeds[:5]), tuple(seeds[5:])})

    result = ke._related_keywords_in_batches(client, seeds)

    assert result == []


def test_self_dev_has_ten_seeds_and_preserves_the_original_five():
    assert ke.SEED_KEYWORDS_BY_CATEGORY["self-dev"] == [
        "시간관리", "목표설정", "습관", "계획표", "생산성",
        "다이어리", "플래너", "공부법", "독서법", "자격증공부",
    ]


def test_other_category_seed_lists_are_unchanged():
    assert {
        category_id: seeds
        for category_id, seeds in ke.SEED_KEYWORDS_BY_CATEGORY.items()
        if category_id != "self-dev"
    } == {
        "health": ["다이어트식단", "홈트레이닝", "수면부족"],
        "finance": ["적금추천", "신용점수", "ETF추천"],
        "it-tech": ["엑셀함수", "클라우드백업", "사진정리"],
        "parenting": ["이유식", "유아놀이", "육아", "아기수면교육"],
        "travel": ["국내여행지", "당일치기여행", "캠핑장추천"],
    }
