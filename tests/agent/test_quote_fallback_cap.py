"""Regression coverage for the published-post design issue: almost every
section-infographic image ended up being the generic "quote" fallback card
because most sections had no structured data (table/checklist/Q&A/etc).
extract_infographic_specs() now caps how many "quote" cards can be picked
(quote_cap, default 2) -- remaining slots are simply left unfilled (fewer
total in-body images) instead of padded with repetitive fallback cards.
"""

from __future__ import annotations

from agent.content.images.section_infographics import extract_infographic_specs

_ALL_PROSE_MD = "\n\n".join(
    f"## 섹션 {n}\n이것은 섹션 {n}의 일반 서술형 문단입니다. 구조화된 표나 체크리스트는 없습니다."
    for n in range(1, 6)
)

_MIXED_MD = """## 표 섹션
| 항목 | 값 |
|---|---|
| A | 1 |
| B | 2 |
| C | 3 |

## 프롬프트1
그냥 서술형 문단입니다. 구조 없음. 충분히 긴 문장으로 인용구가 뽑힐 정도입니다.

## 프롬프트2
또 다른 서술형 문단입니다. 구조 없음. 이 문단도 충분히 길게 작성되어 있습니다.

## 프롬프트3
세번째 서술형 문단입니다. 구조 없음. 역시 충분한 길이를 갖추고 있습니다.

## 프롬프트4
네번째 서술형 문단입니다. 구조 없음. 마지막 문단도 충분히 깁니다.
"""


def test_all_prose_document_is_capped_at_default_quote_cap():
    specs = extract_infographic_specs(_ALL_PROSE_MD, max_count=5, style_seed="t1")
    assert len(specs) == 2
    assert all(s.shape == "quote" for s in specs)


def test_quote_cap_zero_yields_no_images_for_all_prose_document():
    specs = extract_infographic_specs(_ALL_PROSE_MD, max_count=5, style_seed="t1", quote_cap=0)
    assert specs == []


def test_quote_cap_one_yields_exactly_one():
    specs = extract_infographic_specs(_ALL_PROSE_MD, max_count=5, style_seed="t1", quote_cap=1)
    assert len(specs) == 1


def test_structured_sections_are_never_capped_only_quote_is():
    specs = extract_infographic_specs(_MIXED_MD, max_count=5, style_seed="t2")
    shapes = [s.shape for s in specs]
    assert "grid" in shapes
    assert shapes.count("quote") <= 2
    # The real table section must always survive the cap -- only the
    # generic fallback is bounded.
    assert len(specs) == 3  # 1 grid + quote_cap(2) quote, 2 leftover prose sections unfilled


def test_max_count_still_bounds_total_when_quote_cap_is_generous():
    specs = extract_infographic_specs(_ALL_PROSE_MD, max_count=1, style_seed="t1", quote_cap=5)
    assert len(specs) == 1
