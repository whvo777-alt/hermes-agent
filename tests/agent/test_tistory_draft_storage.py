"""Regression tests for Tistory draft body/meta file separation."""

from __future__ import annotations

from pathlib import Path
import tempfile
from unittest.mock import patch

from agent.content.orchestrator import _save_draft_files
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
