"""Regression tests for draft-check results attached to publish results."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import agent.content.publish_on_approval as publish_module
import agent.content.publish_check as publish_check_module
from agent.coo.approval_session import CEOApprovalSessionStatus
from agent.coo.daily_blog_bundle import DailyBlogApprovalBundle


def _bundle_with_content(tmp_path, platform: str, content: str):
    blog_file = tmp_path / f"{platform}.md"
    blog_file.write_text(content, encoding="utf-8")
    item = SimpleNamespace(
        platform=platform,
        platform_label=platform,
        category_id="health",
        category_name="건강",
        topic_title="테스트 제목",
        quality_score=100,
        quality_passed=True,
        quality_warnings=[],
        human_review_items=[],
        blog_file=str(blog_file),
        blog_summary="요약",
        blog_preview="미리보기",
        preview_chunks=[],
        image_file="",
        image_alt="",
        revision_requested=False,
        session=SimpleNamespace(status=CEOApprovalSessionStatus.APPROVED),
    )
    return cast(
        DailyBlogApprovalBundle,
        SimpleNamespace(items=[item], run_date="2026-09-09"),
    )


def _long_markdown() -> str:
    return "# 테스트 제목\n\n![그림](https://example.com/image.png)\n\n" + ("본문 내용입니다. " * 300)


def _stub_wordpress(monkeypatch, result=None):
    calls = []

    def fake_create_wordpress_draft(**kwargs):
        calls.append(kwargs)
        return dict(result or {"link": "https://example.com/draft", "id": 7, "status": "draft"})

    monkeypatch.setattr(publish_module, "create_wordpress_draft", fake_create_wordpress_draft)
    monkeypatch.setattr(publish_module, "_build_section_infographic_results", lambda **kwargs: [])
    monkeypatch.setattr(
        publish_module,
        "_attach_wordpress_infographics",
        lambda **kwargs: (kwargs["html"], []),
    )
    monkeypatch.setattr(
        publish_module,
        "ensure_wordpress_tags",
        lambda **kwargs: {"apiCalled": False, "names": [], "ids": []},
    )
    monkeypatch.setattr(
        publish_module,
        "build_structured_data_html",
        lambda **kwargs: "",
    )
    monkeypatch.setattr(
        "agent.content.seo_enrich.enrich_wordpress_markdown_for_seo",
        lambda markdown, **kwargs: {
            "markdown": markdown,
            "keywordThinned": False,
            "externalAdded": 0,
            "internalCount": 0,
            "internalLinksFallbackOnly": False,
            "focusKeywordCount": 0,
        },
    )
    monkeypatch.setattr(
        "agent.content.images.section_ai_images.build_section_ai_images",
        lambda *args, **kwargs: [],
    )
    return calls


def _stub_blogspot(monkeypatch, result=None):
    calls = []

    def fake_create_blogspot_draft(**kwargs):
        calls.append(kwargs)
        return dict(result or {"postId": "42", "url": "https://example.com/draft", "status": "draft"})

    monkeypatch.setattr(publish_module, "create_blogspot_draft", fake_create_blogspot_draft)
    monkeypatch.setattr(
        publish_module,
        "_attach_blogspot_infographics",
        lambda markdown, **kwargs: markdown,
    )
    monkeypatch.setattr(
        publish_module,
        "build_structured_data_html",
        lambda **kwargs: "",
    )
    return calls


def test_wordpress_result_contains_draft_check(tmp_path, monkeypatch):
    monkeypatch.delenv("WORDPRESS_SITE_URL", raising=False)
    _stub_wordpress(monkeypatch)

    result = publish_module.publish_approved_item(
        _bundle_with_content(tmp_path, "wordpress", _long_markdown()),
        "wordpress",
    )

    assert "check" in result


def test_blogspot_check_contains_image_count_and_warnings(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOGSPOT_SITE_URL", "https://blog.example.com")
    _stub_blogspot(monkeypatch)

    result = publish_module.publish_approved_item(
        _bundle_with_content(tmp_path, "blogspot", _long_markdown()),
        "blogspot",
    )

    assert result["check"]["image_count"] == 1
    assert isinstance(result["check"]["warnings"], list)


def test_wordpress_publish_succeeds_when_check_raises(tmp_path, monkeypatch):
    calls = _stub_wordpress(monkeypatch, {"id": 7, "status": "draft"})

    def raise_check(*args, **kwargs):
        raise RuntimeError("checker failed")

    monkeypatch.setattr(publish_check_module, "check_draft_html", raise_check)

    result = publish_module.publish_approved_item(
        _bundle_with_content(tmp_path, "wordpress", _long_markdown()),
        "wordpress",
    )

    assert calls
    assert result["status"] == "draft"
    assert result["check"] == {}


def test_blogspot_check_exception_returns_empty_check(tmp_path, monkeypatch):
    calls = _stub_blogspot(monkeypatch, {"postId": "42", "status": "draft"})

    def raise_check(*args, **kwargs):
        raise RuntimeError("checker failed")

    monkeypatch.setattr(publish_check_module, "check_draft_html", raise_check)

    result = publish_module.publish_approved_item(
        _bundle_with_content(tmp_path, "blogspot", _long_markdown()),
        "blogspot",
    )

    assert calls
    assert result["postId"] == "42"
    assert result["check"] == {}


def test_blogspot_existing_result_keys_are_preserved(tmp_path, monkeypatch):
    _stub_blogspot(
        monkeypatch,
        {
            "postId": "42",
            "url": "https://example.com/draft",
            "selfLink": "https://example.com/self",
            "status": "draft",
        },
    )

    result = publish_module.publish_approved_item(
        _bundle_with_content(tmp_path, "blogspot", _long_markdown()),
        "blogspot",
    )

    assert result["postId"] == "42"
    assert result["url"] == "https://example.com/draft"
    assert result["selfLink"] == "https://example.com/self"
    assert result["status"] == "draft"
    assert "check" in result


def test_missing_blogspot_site_url_does_not_count_internal_links(tmp_path, monkeypatch):
    monkeypatch.delenv("BLOGSPOT_SITE_URL", raising=False)
    _stub_blogspot(monkeypatch)
    content = _long_markdown() + "\n[내부 글](https://blog.example.com/inside)\n"

    result = publish_module.publish_approved_item(
        _bundle_with_content(tmp_path, "blogspot", content),
        "blogspot",
    )

    assert result["check"]["internal_links"] == 0
