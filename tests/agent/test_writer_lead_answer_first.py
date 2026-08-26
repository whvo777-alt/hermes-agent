"""Regression tests for answer-first writing guidance in the shared prompt."""

import pytest

from agent.content.prompts.prompt_builder import build_system_prompt


_PLATFORM_CASES = (
    ("wordpress", "health"),
    ("blogspot", "self-dev"),
    ("tistory", "finance"),
    ("naver", "it-tech"),
)


def _system_prompts():
    return [
        build_system_prompt(
            platform_id=platform_id,
            category_id=category_id,
            research_summary="",
            planning_summary="",
        )
        for platform_id, category_id in _PLATFORM_CASES
    ]


def test_lead_requires_answer_in_first_two_sentences_for_all_platforms():
    prompts = _system_prompts()

    assert all("첫 두 문장 안에 이 글의 답을 먼저 쓴다" in prompt for prompt in prompts)


def test_lead_forbids_preview_sentences_for_all_platforms():
    prompts = _system_prompts()

    assert all("도입에 예고 문장을 쓰지 않는다" in prompt for prompt in prompts)


def test_lead_includes_both_answer_first_examples_for_all_platforms():
    prompts = _system_prompts()

    assert all("답이 둘째 문단으로 밀림" in prompt for prompt in prompts)
    assert all("첫 두 문장에 답" in prompt for prompt in prompts)


def test_lead_guidance_is_present_in_each_of_the_four_platform_prompts():
    prompts = _system_prompts()

    assert len(prompts) == 4
    assert all("답을 쓴 뒤에 왜 그런지" in prompt for prompt in prompts)
    assert all("위 본보기는 모양을 보여주는 것일 뿐이다" in prompt for prompt in prompts)
