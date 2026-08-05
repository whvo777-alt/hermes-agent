"""Regression tests for Tistory draft body/meta file separation."""

from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from unittest.mock import patch

from agent.coo.daily_blog_bundle import (
    DailyBlogApprovalBundleStore,
    create_daily_blog_approval_bundle,
)
from agent.coo.models import COOOrchestrationResult
from agent.content.config.categories import Category
from agent.content.config.platforms import Platform
from agent.content.images.hero_image import HeroImage
from agent.content.orchestrator import ContentDraft
from agent.content.orchestrator import (
    _extract_tistory_seo_meta,
    _inject_tistory_frontmatter,
    _save_draft_files,
    _validate_tistory_metadata,
    generate_platform_draft,
)
from agent.content.publish_on_approval import extract_seo_meta
from agent.content.quality.quality_gate import QualityGateResult
from agent.content.writing.writer import write_blog_post


def test_tistory_writer_puts_production_notes_under_meta_delimiter():
    def _fake_call_llm(*, system: str, user: str) -> str:
        return "# 제목\n\n독자에게 보이는 본문입니다."

    with patch("agent.content.writing.writer.call_llm", side_effect=_fake_call_llm):
        content = write_blog_post(
            platform_id="tistory",
            platform_label="티스토리",
            category_id="finance",
            category_name="재테크/경제",
            target_audience="재테크 관심 독자",
            tone="실전형",
            topic_title="ETF 투자 기준",
            topic_keywords=["ETF"],
            category_keywords=["주식", "ETF"],
            caution_hints=[],
            current_date="2026-08-03",
            research_content="리서치 내용",
            planning_content="기획 내용",
        )

    body, meta = content.split("---META---", maxsplit=1)
    assert "## 내부링크 후보" not in body
    assert "대표 이미지 ALT:" not in body
    assert "## 내부링크 후보" in meta
    assert "대표 이미지 ALT:" in meta


def test_extract_seo_meta_reads_tistory_title():
    content = (
        "---META---\n"
        "제목: 금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준\n"
        "태그: 금현물, KRX금시장, 금ETF, 금투자, 재테크\n"
    )

    common_meta = extract_seo_meta(content)
    assert common_meta["title"] == (
        "금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준"
    )
    assert "태그 후보" not in common_meta
    assert _extract_tistory_seo_meta(content)["태그 후보"] == (
        "금현물, KRX금시장, 금ETF, 금투자, 재테크"
    )


def test_extract_seo_meta_keeps_meta_title_separate_from_title():
    content = "---META---\nMeta title: 기존 SEO 제목\n제목: 최종 발행 제목\n"

    assert extract_seo_meta(content) == {
        "meta title": "기존 SEO 제목",
        "title": "최종 발행 제목",
    }


def test_tistory_prompt_requires_publication_metadata():
    prompt = (
        Path(__file__).resolve().parents[2]
        / "agent"
        / "content"
        / "prompts"
        / "platform"
        / "tistory.md"
    ).read_text(encoding="utf-8")

    assert "제목: 공백 포함 25~40자" in prompt
    assert "태그: 5~8개를 쉼표로 구분" in prompt
    assert "URL slug: 영문 소문자와 하이픈만" in prompt
    assert "Meta description: 1~2문장" in prompt


def test_non_tistory_writer_frontmatter_remains_without_publication_fields():
    def _fake_call_llm(*, system: str, user: str) -> str:
        return "# 독자용 제목\n\n본문입니다."

    for platform_id, platform_label in (
        ("naver", "네이버"),
        ("wordpress", "WordPress"),
        ("blogspot", "Blogspot"),
    ):
        with patch("agent.content.writing.writer.call_llm", side_effect=_fake_call_llm):
            content = write_blog_post(
                platform_id=platform_id,
                platform_label=platform_label,
                category_id="finance",
                category_name="재테크/경제",
                target_audience="재테크 관심 독자",
                tone="실전형",
                topic_title="국내주식",
                topic_keywords=["금현물"],
                category_keywords=["주식", "금현물"],
                caution_hints=[],
                current_date="2026-08-05",
                research_content="리서치 내용",
                planning_content="기획 내용",
            )

        frontmatter = content.split("\n\n", 1)[0]
        assert frontmatter == "\n".join(
            [
                "---",
                f"platform: {platform_id}",
                f"platform_label: {platform_label}",
                "category: finance",
                "category_name: 재테크/경제",
                "topic_title: 국내주식",
                "status: draft",
                "---",
            ]
        )
        assert "\ntitle:" not in frontmatter
        assert "\ntags:" not in frontmatter
        assert "\nslug:" not in frontmatter
        assert "\ndescription:" not in frontmatter


def test_tistory_metadata_validation_keeps_only_accepted_values():
    metadata = _validate_tistory_metadata(
        {
            "title": "금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준",
            "태그 후보": "금현물, KRX금시장, 금ETF, 금투자, 재테크",
            "URL slug": "gold-investing-krx-etf",
            "Meta description": "금현물 투자를 시작할 때 KRX 금시장과 금 ETF를 비교하고 선택 기준을 정리합니다.",
        }
    )

    assert metadata == {
        "title": "금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준",
        "tags": "금현물, KRX금시장, 금ETF, 금투자, 재테크",
        "slug": "gold-investing-krx-etf",
        "description": "금현물 투자를 시작할 때 KRX 금시장과 금 ETF를 비교하고 선택 기준을 정리합니다.",
    }


def test_tistory_metadata_validation_does_not_fallback_to_topic_title():
    metadata = _validate_tistory_metadata(
        {
            "title": "짧은 제목",
            "태그 후보": "금현물, 금ETF, 재테크",
            "URL slug": "국내주식-입문",
            "Meta description": "요약입니다.",
        }
    )

    assert metadata == {"title": "", "tags": "", "slug": "", "description": "요약입니다."}


def test_tistory_frontmatter_injection_omits_empty_values():
    source = """---
platform: tistory
platform_label: 티스토리
category: finance
category_name: 재테크/경제
topic_title: 국내주식
status: draft
---

본문입니다.
"""

    result = _inject_tistory_frontmatter(
        source,
        {"title": "", "tags": "금현물, 금ETF", "slug": "", "description": "요약입니다."},
    )

    assert "\ntitle: " not in result
    assert "tags: 금현물, 금ETF" in result
    assert "\nslug: " not in result
    assert "description: 요약입니다." in result


def test_tistory_preview_prefers_frontmatter_title_over_body_h1():
    from agent.coo.daily_blog_bundle import build_structured_preview

    content = """---
platform: tistory
topic_title: 국내주식
title: 금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준
---

본문 첫 문장입니다. 사람이 읽을 수 있는 충분한 도입부입니다.
"""

    summary, preview, _chunks = build_structured_preview(content)

    assert summary == "금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준"
    assert preview.startswith("# 금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준")

def test_save_draft_files_strips_h1_and_writes_meta_separately():
    source = (
        "---\n"
        "platform: tistory\n"
        "---\n\n"
        "## 목차\n"
        "1. 핵심\n\n"
        "# 제목\n\n"
        "본문입니다.\n\n"
        "---META---\n"
        "## 내부링크 후보\n"
        "- 관련 글\n"
        "대표 이미지 ALT: 카드형 이미지\n"
    )

    with tempfile.TemporaryDirectory(prefix="tistory-draft-", dir=Path(__file__).resolve().parents[2]) as directory:
        tmp_path = Path(directory)
        blog_file, notes_file, saved_body = _save_draft_files(
            tmp_path,
            source,
            separate_meta=True,
        )

        assert blog_file == tmp_path / "blog.md"
        assert notes_file == tmp_path / "notes.md"
        assert notes_file is not None
        assert blog_file.read_text(encoding="utf-8") == saved_body
        assert "# 제목" not in saved_body
        assert "## 목차" in saved_body
        assert "본문입니다." in saved_body
        assert "---META---" not in saved_body
        assert notes_file.read_text(encoding="utf-8") == (
            "## 내부링크 후보\n"
            "- 관련 글\n"
            "대표 이미지 ALT: 카드형 이미지\n"
        )


def test_save_draft_files_without_meta_keeps_single_blog_file():
    source = "---\nplatform: tistory\n---\n\n# 제목\n\n본문입니다.\n"

    with tempfile.TemporaryDirectory(prefix="tistory-draft-", dir=Path(__file__).resolve().parents[2]) as directory:
        tmp_path = Path(directory)
        blog_file, notes_file, saved_body = _save_draft_files(
            tmp_path,
            source,
            separate_meta=True,
        )

        assert blog_file.exists()
        assert notes_file is None
        assert not (tmp_path / "notes.md").exists()
        assert "# 제목" not in saved_body
        assert "본문입니다." in saved_body


def test_save_draft_files_writes_tistory_html_after_blog_file():
    source = "# 제목\n\n**핵심: 본문입니다.**\n"

    with tempfile.TemporaryDirectory(prefix="tistory-draft-", dir=Path(__file__).resolve().parents[2]) as directory:
        tmp_path = Path(directory)
        _save_draft_files(tmp_path, source, separate_meta=True, platform_id="tistory")

        html_file = tmp_path / "blog.tistory.html"
        assert html_file.exists()
        html = html_file.read_text(encoding="utf-8")
        assert "<html" not in html.lower()
        assert "<head" not in html.lower()
        assert "<style" not in html.lower()
        assert "style=" in html


def test_save_draft_files_does_not_write_tistory_html_for_wordpress():
    source = "# 제목\n\n본문입니다.\n"

    with tempfile.TemporaryDirectory(prefix="tistory-draft-", dir=Path(__file__).resolve().parents[2]) as directory:
        tmp_path = Path(directory)
        _save_draft_files(tmp_path, source, platform_id="wordpress")

        assert not (tmp_path / "blog.tistory.html").exists()


def test_save_draft_files_keeps_non_tistory_format_unchanged():
    source = "---\nplatform: wordpress\n---\n\n# 제목\n\n본문\n\n---META---\n메모\n"

    with tempfile.TemporaryDirectory(prefix="tistory-draft-", dir=Path(__file__).resolve().parents[2]) as directory:
        tmp_path = Path(directory)
        blog_file, notes_file, saved_body = _save_draft_files(tmp_path, source)

        assert saved_body == source
        assert blog_file.read_text(encoding="utf-8") == source
        assert notes_file is None
        assert not (tmp_path / "notes.md").exists()


def test_tistory_orchestrator_uses_disabled_image_without_generating_or_inserting():
    platform = SimpleNamespace(id="tistory", label="티스토리")
    category = SimpleNamespace(
        id="finance",
        name="재테크/경제",
        keywords=["ETF"],
        target_audience="재테크 관심 독자",
        tone="실전형",
        caution_hints=[],
    )
    topic = {
        "topic_id": "tistory-no-hero",
        "topic_title": "ETF 투자 기준",
        "topic_keywords": ["ETF"],
    }
    body = "# ETF 투자 기준\n\n본문입니다."
    prepared = {
        "blog_content": body,
        "h1_title": "ETF 투자 기준",
        "seo_title": "ETF 투자 기준 | ETF",
        "rewrite_attempted": False,
        "rewrite_failed": False,
    }
    mocks = {
        "find_platform": Mock(return_value=platform),
        "get_launch_category": Mock(return_value=category),
        "get_recent_feedback": Mock(return_value=[]),
        "sync_written_corpus": Mock(return_value={}),
        "_pick_daily_topic": Mock(return_value=topic),
        "build_memory_check": Mock(return_value={"blocked": False}),
        "run_research": Mock(return_value="리서치"),
        "run_planning": Mock(return_value="기획"),
        "write_blog_post": Mock(return_value=body),
        "_prepare_title_for_image_and_quality": Mock(return_value=prepared),
        "create_title_card": Mock(),
        "insert_hero_image": Mock(),
        "find_recent_topics": Mock(return_value=[]),
        "run_quality_gate": Mock(return_value=QualityGateResult(True, 100)),
        "_save_draft_files": Mock(return_value=(Path("/tmp/blog.md"), None, body)),
        "add_content": Mock(return_value={"memory": {}}),
        "save_memory": Mock(),
    }

    with patch.multiple("agent.content.orchestrator", **mocks):
        draft = generate_platform_draft(platform_id="tistory", run_date="2026-08-05")

    assert draft.image.status == "disabled"
    assert draft.image.file == ""
    assert draft.image.alt == ""
    assert draft.image.caption == ""
    assert draft.to_dict()["image"]["status"] == "disabled"
    mocks["create_title_card"].assert_not_called()
    mocks["insert_hero_image"].assert_not_called()


def test_daily_blog_bundle_does_not_break_with_disabled_empty_image():
    draft = ContentDraft(
        platform=Platform(
            id="tistory",
            label="티스토리",
            publisher="tistory",
            capability="not_implemented",
        ),
        category=Category(
            id="finance",
            name="재테크/경제",
            target_audience="재테크 관심 독자",
            tone="실전형",
            keywords=["ETF"],
            caution_hints=[],
            sensitive=True,
        ),
        run_date="2026-08-05",
        topic_id="tistory-no-hero",
        topic_title="ETF 투자 기준",
        topic_keywords=["ETF"],
        research_content="리서치",
        planning_content="기획",
        blog_content="# ETF 투자 기준\n\n본문입니다.",
        image=HeroImage(
            role="hero",
            status="disabled",
            file="",
            alt="",
            caption="",
            prompt="",
            inserted_in="",
            created_at="",
        ),
        quality=QualityGateResult(True, 100),
        blog_file="/tmp/blog.md",
        title="금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준",
        tags="금현물, KRX금시장, 금ETF, 금투자, 재테크",
        slug="gold-investing-krx-etf",
        description="금현물 투자를 시작할 때 선택 기준을 정리합니다.",
    )
    session = SimpleNamespace()
    bundle_store = DailyBlogApprovalBundleStore()

    with patch(
        "agent.coo.daily_blog_bundle.create_approval_session",
        return_value=session,
    ):
        bundle = create_daily_blog_approval_bundle(
            [draft],
            cast(COOOrchestrationResult, SimpleNamespace()),
            run_date="2026-08-05",
            requester_id="tester",
            channel_id="channel",
            bundle_store=bundle_store,
        )

    assert bundle is not None
    assert bundle_store.get(bundle.bundle_id) is bundle
    assert bundle.items[0].image_file == ""
    assert bundle.items[0].image_alt == ""
    assert bundle.items[0].title == "금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준"
    assert bundle.items[0].tags == "금현물, KRX금시장, 금ETF, 금투자, 재테크"
    assert bundle.items[0].slug == "gold-investing-krx-etf"
    assert bundle.items[0].description == "금현물 투자를 시작할 때 선택 기준을 정리합니다."
    assert "- image: ``" in bundle.report.sections[0].body_lines


def test_tistory_orchestrator_materializes_publication_metadata_and_frontmatter(tmp_path):
    platform = SimpleNamespace(id="tistory", label="티스토리")
    category = SimpleNamespace(
        id="finance",
        name="재테크/경제",
        keywords=["금현물"],
        target_audience="재테크 관심 독자",
        tone="실전형",
        caution_hints=[],
    )
    topic = {
        "topic_id": "tistory-publication-info",
        "topic_title": "국내주식",
        "topic_keywords": ["금현물"],
    }
    source = """---
platform: tistory
platform_label: 티스토리
category: finance
category_name: 재테크/경제
topic_title: 국내주식
status: draft
---

# 금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준

본문입니다.

---META---
제목: 금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준
태그: 금현물, KRX금시장, 금ETF, 금투자, 재테크
URL slug: gold-investing-krx-etf
Meta description: 금현물 투자를 시작할 때 KRX 금시장과 금 ETF를 비교하고 선택 기준을 정리합니다.
"""
    prepared = {
        "blog_content": source,
        "h1_title": "금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준",
        "seo_title": "금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준",
        "rewrite_attempted": False,
        "rewrite_failed": False,
    }
    mocks = {
        "find_platform": Mock(return_value=platform),
        "get_launch_category": Mock(return_value=category),
        "get_recent_feedback": Mock(return_value=[]),
        "sync_written_corpus": Mock(return_value={}),
        "_pick_daily_topic": Mock(return_value=topic),
        "build_memory_check": Mock(return_value={"blocked": False}),
        "run_research": Mock(return_value="리서치"),
        "run_planning": Mock(return_value="기획"),
        "write_blog_post": Mock(return_value=source),
        "_prepare_title_for_image_and_quality": Mock(return_value=prepared),
        "create_title_card": Mock(),
        "insert_hero_image": Mock(),
        "find_recent_topics": Mock(return_value=[]),
        "run_quality_gate": Mock(return_value=QualityGateResult(True, 100)),
        "add_content": Mock(return_value={"memory": {}}),
        "save_memory": Mock(),
    }

    with patch.multiple("agent.content.orchestrator", **mocks):
        with patch("agent.content.orchestrator._drafts_dir", return_value=tmp_path):
            draft = generate_platform_draft(platform_id="tistory", run_date="2026-08-05")

    assert draft.title == "금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준"
    assert draft.tags == "금현물, KRX금시장, 금ETF, 금투자, 재테크"
    assert draft.slug == "gold-investing-krx-etf"
    assert draft.description.startswith("금현물 투자를 시작할 때")
    saved = Path(draft.blog_file).read_text(encoding="utf-8")
    assert "title: 금현물 투자 시작하기, KRX 금시장과 금 ETF를 고르는 기준" in saved
    assert "tags: 금현물, KRX금시장, 금ETF, 금투자, 재테크" in saved
    assert "slug: gold-investing-krx-etf" in saved
    assert "description: 금현물 투자를 시작할 때" in saved
    assert "---META---" not in saved
    assert "# 금현물 투자 시작하기" not in saved
