"""Tests for Blogger post label extraction.

Regression coverage for: create_blogspot_draft() started receiving the
stripped ``publishable`` markdown (frontmatter + SEO-meta block removed)
instead of raw ``blog_content`` back on 2026-07-21 (commit 29658ebf7), but
build_blogger_post()'s ``labels`` still came from re-parsing whatever
markdown it was handed — so extract_labels() had nothing left to find and
every Blogspot draft got an empty "labels" array. Fixed by extracting
labels from the raw content at the call site and passing them in
explicitly, the same pattern already used for ``title``.
"""

from __future__ import annotations

from agent.content.publishers.blogspot import build_blogger_post, create_blogspot_draft, update_blogspot_draft

_RAW_MARKDOWN = """---
platform: blogspot
category: self-dev
category_name: 자기계발
topic_title: 테스트 주제
status: draft
---

# 테스트 글

본문 내용입니다.

태그 후보: 테스트태그, 두번째태그, 세번째태그
"""

_STRIPPED_MARKDOWN = "# 테스트 글\n\n본문 내용입니다.\n"  # frontmatter + meta block already gone


def test_build_blogger_post_uses_explicit_labels_when_given():
    post = build_blogger_post(_STRIPPED_MARKDOWN, title="테스트", labels=["자기계발", "테스트태그"])
    assert post["labels"] == ["자기계발", "테스트태그"]


def test_build_blogger_post_falls_back_to_extracting_from_markdown():
    """Backward compat: no labels passed -> old re-extraction behavior."""
    post = build_blogger_post(_RAW_MARKDOWN, title="테스트")
    assert "자기계발" in post["labels"]
    assert "테스트태그" in post["labels"]


def test_build_blogger_post_on_stripped_markdown_without_explicit_labels_is_empty():
    """Documents the bug this fixes: stripped markdown alone has nothing
    for extract_labels() to find -- this is exactly why the caller must
    pass labels explicitly instead of relying on the fallback."""
    post = build_blogger_post(_STRIPPED_MARKDOWN, title="테스트")
    assert post["labels"] == []


def test_create_blogspot_draft_dry_run_carries_explicit_labels():
    result = create_blogspot_draft(
        markdown=_STRIPPED_MARKDOWN, title="테스트", labels=["자기계발", "테스트태그"],
        blog_id=None, client_id=None, client_secret=None, refresh_token=None, live=False,
    )
    assert result["postPreview"]["labels"] == ["자기계발", "테스트태그"]


def test_update_blogspot_draft_no_longer_raises_missing_title():
    """update_blogspot_draft() previously had no title param at all and
    called build_blogger_post(markdown) without one -- a required
    keyword-only arg -- so any real call raised TypeError."""
    result = update_blogspot_draft(
        markdown=_STRIPPED_MARKDOWN, title="테스트", post_id="123", labels=["자기계발"],
        blog_id=None, client_id=None, client_secret=None, refresh_token=None, live=False,
    )
    assert result["postPreview"]["title"] == "테스트"
    assert result["postPreview"]["labels"] == ["자기계발"]


def test_build_blogger_post_appends_extra_html_after_markdown_conversion():
    """extra_content_html (e.g. JSON-LD <script> tags, see
    agent/content/structured_data.py) must land after markdown_to_html()
    runs, never mixed into the markdown source -- a <script> tag embedded
    in raw markdown gets HTML-escaped by the fallback paragraph handler."""
    post = build_blogger_post(
        _STRIPPED_MARKDOWN, title="테스트",
        extra_content_html='<script type="application/ld+json">{"a":1}</script>',
    )
    assert post["content"].endswith('<script type="application/ld+json">{"a":1}</script>')
    assert "&lt;script" not in post["content"]


def test_build_blogger_post_no_extra_html_is_backward_compatible():
    post = build_blogger_post(_STRIPPED_MARKDOWN, title="테스트")
    assert "<script" not in post["content"]


def test_create_blogspot_draft_dry_run_carries_extra_content_html():
    result = create_blogspot_draft(
        markdown=_STRIPPED_MARKDOWN, title="테스트",
        blog_id=None, client_id=None, client_secret=None, refresh_token=None, live=False,
        extra_content_html="<script>x</script>",
    )
    assert result["postPreview"]["content"].endswith("<script>x</script>")
