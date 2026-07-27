"""Tests for agent.content.structured_data — Article + conditional FAQPage
JSON-LD builders added so every future WordPress/Blogspot post carries
structured data (there was none in the pipeline before this)."""

from __future__ import annotations

import json

from agent.content.structured_data import (
    build_article_jsonld,
    build_faqpage_jsonld,
    build_structured_data_html,
    render_jsonld_script_tag,
)


def test_article_jsonld_uses_fixed_coco_blog_author_and_publisher():
    data = build_article_jsonld(title="테스트 글", description="설명")
    assert data["author"] == {"@type": "Organization", "name": "COCO Blog"}
    assert data["publisher"] == {"@type": "Organization", "name": "COCO Blog"}


def test_article_jsonld_omits_image_when_not_given():
    data = build_article_jsonld(title="테스트 글")
    assert "image" not in data


def test_article_jsonld_includes_image_when_given():
    data = build_article_jsonld(title="테스트 글", image_url="https://x.com/hero.png")
    assert data["image"] == "https://x.com/hero.png"


def test_article_jsonld_truncates_long_headline():
    long_title = "가" * 200
    data = build_article_jsonld(title=long_title)
    assert len(data["headline"]) <= 110


def test_article_jsonld_sets_date_published_and_modified():
    data = build_article_jsonld(title="t", date_published_iso="2026-07-27T00:00:00+09:00")
    assert data["datePublished"] == "2026-07-27T00:00:00+09:00"
    assert data["dateModified"] == "2026-07-27T00:00:00+09:00"


def test_faqpage_jsonld_none_when_no_pairs():
    assert build_faqpage_jsonld([]) is None
    assert build_faqpage_jsonld(None) is None


def test_faqpage_jsonld_builds_all_pairs():
    pairs = [("질문1?", "답변1."), ("질문2?", "답변2.")]
    data = build_faqpage_jsonld(pairs)
    assert data["@type"] == "FAQPage"
    assert len(data["mainEntity"]) == 2
    assert data["mainEntity"][0]["name"] == "질문1?"
    assert data["mainEntity"][0]["acceptedAnswer"]["text"] == "답변1."


def test_render_jsonld_script_tag_is_valid_json_inside_script_tag():
    tag = render_jsonld_script_tag({"a": 1})
    assert tag.startswith('<script type="application/ld+json">')
    assert tag.endswith("</script>")
    inner = tag[len('<script type="application/ld+json">'):-len("</script>")]
    assert json.loads(inner) == {"a": 1}


def test_render_jsonld_script_tag_escapes_closing_script_sequence():
    tag = render_jsonld_script_tag({"a": "</script>malicious"})
    assert "</script>malicious" not in tag
    assert "<\\/script>malicious" in tag


def test_build_structured_data_html_without_faq_emits_only_article_script():
    html = build_structured_data_html(title="제목", description="설명")
    assert html.count("<script") == 1
    assert "FAQPage" not in html


def test_build_structured_data_html_with_faq_emits_two_scripts():
    html = build_structured_data_html(
        title="제목", qa_pairs=[("질문?", "답변.")],
    )
    assert html.count("<script") == 2
    assert "FAQPage" in html
    assert "Article" in html
