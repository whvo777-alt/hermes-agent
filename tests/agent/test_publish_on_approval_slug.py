"""Tests for WordPress slug generation in agent.content.publish_on_approval.

Regression coverage for a real published-post bug: the SEO checker flagged
"URL에 하이픈(-)이 사용되지 않았습니다". _prefer_korean_slug() trusted any
writer-supplied "URL slug" field verbatim as long as it contained Hangul,
without ever running it through _slugify()'s space/punctuation-to-hyphen
normalization -- so a space-separated candidate published with no hyphens
at all.
"""

from __future__ import annotations

import re

from agent.content.publish_on_approval import _prefer_korean_slug, _slugify


def test_slugify_converts_spaces_to_hyphens():
    assert _slugify("한의학 확인 기준 7가지") == "한의학-확인-기준-7가지"


def test_prefer_korean_slug_normalizes_space_separated_writer_slug():
    """The bug: a writer-provided slug with spaces instead of hyphens used
    to be returned untouched just because it contained Hangul."""
    raw = "한의학 확인 기준 7가지"
    result = _prefer_korean_slug(raw_slug=raw, title="한의학 확인 기준 7가지, 한의원 방문 전", focus_keyword="한의학")
    assert "-" in result
    assert " " not in result
    assert result == "한의학-확인-기준-7가지"


def test_prefer_korean_slug_leaves_already_hyphenated_writer_slug_intact():
    raw = "한의학-확인-기준-7가지"
    result = _prefer_korean_slug(raw_slug=raw, title="한의학 확인 기준 7가지", focus_keyword="한의학")
    assert result == "한의학-확인-기준-7가지"


def test_prefer_korean_slug_falls_back_to_title_slug_for_english_only_candidate():
    result = _prefer_korean_slug(
        raw_slug="oriental-medicine-checklist",
        title="한의학 확인 기준 7가지",
        focus_keyword="한의학",
    )
    assert "-" in result
    assert re.search(r"[가-힣]", result)
