"""Tests for agent.content.images.section_infographics.extract_all_qa_pairs.

Added for FAQPage JSON-LD (agent.content.structured_data) -- unlike the
infographic card's _extract_qa (hard-capped at 2 pairs and scoped to one
section's lines), this must return every 질문:/답변: pair in the whole
document so the schema reflects the writer's full FAQ section.
"""

from __future__ import annotations

from agent.content.images.section_infographics import extract_all_qa_pairs


def test_returns_all_pairs_beyond_the_infographic_two_pair_cap():
    markdown = "\n".join(
        f"질문: 질문{n}?\n답변: 답변{n}." for n in range(1, 5)
    )
    pairs = extract_all_qa_pairs(markdown)
    assert len(pairs) == 4
    assert pairs[0] == ("질문1?", "답변1.")
    assert pairs[3] == ("질문4?", "답변4.")


def test_no_qa_markers_returns_empty_list():
    assert extract_all_qa_pairs("## 그냥 본문\n마커 없는 문단입니다.") == []


def test_accepts_q_a_english_markers_too():
    markdown = "Q: What is this?\nA: An answer."
    assert extract_all_qa_pairs(markdown) == [("What is this?", "An answer.")]


def test_ignores_unpaired_question_without_following_answer():
    markdown = "질문: 답 없는 질문?\n그냥 본문이 이어집니다.\n\n질문: 진짜 질문?\n답변: 진짜 답."
    assert extract_all_qa_pairs(markdown) == [("진짜 질문?", "진짜 답.")]
