"""Tests for the SEO title policy and pre-image title rewrite flow."""

from __future__ import annotations

import inspect

from agent.content.images.hero_image import extract_blog_title
from agent.content.markdown_html import extract_title
from agent.content.orchestrator import _prepare_title_for_image_and_quality
from agent.content.publish_on_approval import _build_rank_math_title
from agent.content.quality.quality_gate import run_quality_gate
from agent.content.seo_enrich import (
    SEO_TITLE_MAX_LENGTH,
    SEO_TITLE_MIN_LENGTH,
    truncate_seo_title,
)
from agent.content.writing import writer


def test_title_truncation_does_not_add_a_number_and_removes_old_argument():
    title = "본문 내용과 무관하게 숫자를 추가하지 않는 SEO 제목입니다"

    assert truncate_seo_title(title) == title
    assert "7가지" not in truncate_seo_title(title)
    assert "fallback_number" not in inspect.signature(truncate_seo_title).parameters


def test_title_truncation_stops_at_an_word_boundary():
    title = "첫 번째 제목 어절입니다 두 번째 제목 어절입니다 세 번째 제목 어절입니다 네 번째 제목 어절입니다 다섯 번째 제목 어절입니다"

    result = truncate_seo_title(title)

    assert len(result) <= 60
    assert result == title[:60].rsplit(" ", 1)[0]
    assert result in title


def test_rank_math_title_keeps_focus_prefix_without_number_insertion():
    result = _build_rank_math_title("본문에 키워드가 없는 제목", "핵심 키워드")

    assert result.startswith("핵심 키워드 | ")
    assert "7가지" not in result


def test_h1_length_is_reference_only_when_seo_title_is_in_range():
    content = "# 짧은 H1\n\n" + ("본문 문장입니다. " * 20)
    result = run_quality_gate(
        topic_title="짧은 H1",
        category_id="health",
        platform_id="wordpress",
        content_type="blog",
        content=content,
        image={"file": "image.png", "alt": "alt", "caption": "caption", "status": "ready"},
        seo_title="검색 결과에 노출되는 최종 SEO title 길이 기준 예시",
        seo_title_rewrite_attempted=False,
    )

    assert not any("TITLE_LENGTH_OUT_OF_RANGE" in warning for warning in result.warnings)
    assert result.metadata["title"]["h1Length"] == len("짧은 H1")
    assert SEO_TITLE_MIN_LENGTH <= result.metadata["title"]["seoTitleLength"] <= SEO_TITLE_MAX_LENGTH


def test_out_of_range_seo_title_is_reported_after_one_rewrite():
    content = "# 최종 H1 제목\n\n" + ("본문 문장입니다. " * 20)
    result = run_quality_gate(
        topic_title="최종 H1 제목",
        category_id="health",
        platform_id="wordpress",
        content_type="blog",
        content=content,
        image={"file": "image.png", "alt": "alt", "caption": "caption", "status": "ready"},
        seo_title="x" * 26,
        seo_title_rewrite_attempted=True,
    )

    assert result.warnings[0] == (
        "TITLE_LENGTH_OUT_OF_RANGE: SEO title 26자 "
        "(권장 28~40자, 재작성 1회 후 유지)"
    )


def test_rewrite_prompt_contains_focus_keyword_prefix_rule_and_lengths(monkeypatch):
    captured = {}

    def fake_call_llm(*, system, user):
        captured["system"] = system
        captured["user"] = user
        return "# 핵심 키워드를 자연스럽게 포함한 제목입니다"

    monkeypatch.setattr(writer, "call_llm", fake_call_llm)
    rewritten = writer.rewrite_title_for_seo_length(
        title="짧은 제목",
        focus_keyword="핵심 키워드",
        current_seo_title="핵심 키워드 | 짧은 제목",
        prefixed_seo_title="핵심 키워드 | 짧은 제목",
    )

    assert rewritten == "핵심 키워드를 자연스럽게 포함한 제목입니다"
    assert "focus keyword: 핵심 키워드" in captured["user"]
    assert "focus keyword를 제목에 자연스럽게 포함" in captured["user"]
    assert "focus_keyword | H1" in captured["user"]
    assert "접두어를 붙였을 때의 최종 SEO title 길이" in captured["user"]
    assert "글자 수를 맞추기 위해 본문에 없는 내용" in captured["user"]
    assert "원래 제목의 문형과 어투를 최대한 유지" in captured["user"]


def test_representative_title_examples_stay_in_seo_range_without_rewrite(monkeypatch):
    examples = [
        ("운동 후 심장이 너무 빨리 뛰면 강도를 낮춰야 할까?", "운동"),
        ("초보자 HIIT는 운동 시간보다 회복 간격이 먼저입니다", "HIIT"),
        ("집에서 하는 HIIT, 무릎 통증이 있을 때 달라지는 선택 기준", "HIIT"),
    ]
    rewrite_calls = []
    monkeypatch.setattr(writer, "call_llm", lambda **kwargs: rewrite_calls.append(kwargs) or "# 재작성 제목")

    for title, topic_keyword in examples:
        prepared = _prepare_title_for_image_and_quality(
            f"# {title}\n\n본문은 제목의 주제를 실제로 설명합니다.",
            topic_title=topic_keyword,
        )

        assert 28 <= len(title) <= 40
        assert 28 <= len(prepared["seo_title"]) <= 40
        assert prepared["rewrite_attempted"] is False

    assert rewrite_calls == []


def test_rewritten_h1_is_the_title_card_title_input(monkeypatch):
    original = "# 아주 짧은 제목\n\n본문 문장입니다. " + ("내용입니다. " * 20)
    rewritten = "핵심 키워드를 자연스럽게 포함한 충분히 긴 최종 제목 기준"
    monkeypatch.setattr(writer, "call_llm", lambda **_: f"# {rewritten}")

    prepared = _prepare_title_for_image_and_quality(
        original,
        topic_title="핵심 키워드 확인 기준",
    )
    final_h1 = extract_title(prepared["blog_content"])
    title_card_input = extract_blog_title(
        prepared["blog_content"],
        fallback="핵심 키워드 확인 기준",
    )

    assert prepared["rewrite_attempted"] is True
    assert final_h1 == rewritten
    assert title_card_input == final_h1
    assert prepared["seo_title"] == _build_rank_math_title(final_h1, prepared["focus_keyword"])


def test_rewrite_failure_has_a_distinct_quality_warning(monkeypatch):
    original = "# 아주 짧은 제목\n\n본문 문장입니다. " + ("내용입니다. " * 20)

    def fail_call_llm(**_):
        raise RuntimeError("rewrite provider unavailable")

    monkeypatch.setattr(writer, "call_llm", fail_call_llm)
    prepared = _prepare_title_for_image_and_quality(
        original,
        topic_title="핵심 키워드 확인 기준",
    )

    result = run_quality_gate(
        topic_title="핵심 키워드 확인 기준",
        category_id="health",
        platform_id="wordpress",
        content=prepared["blog_content"],
        content_type="blog",
        image={"file": "image.png", "alt": "alt", "caption": "caption", "status": "ready"},
        seo_title=prepared["seo_title"],
        seo_title_rewrite_attempted=prepared["rewrite_attempted"],
        seo_title_rewrite_failed=prepared["rewrite_failed"],
    )

    assert prepared["rewrite_attempted"] is True
    assert prepared["rewrite_failed"] is True
    assert any("재작성 실패 후 유지" in warning for warning in result.warnings)
