"""Regression tests for author-note blocking at the live publish boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from agent.coo.approval_session import CEOApprovalSessionStatus
from agent.coo.daily_blog_bundle import DailyBlogApprovalBundle
from agent.content.publish_on_approval import PublishBlockedError, publish_approved_item


def _bundle_with_content(tmp_path, content: str):
    blog_file = tmp_path / "blog.md"
    blog_file.write_text(content, encoding="utf-8")
    item = SimpleNamespace(
        platform="naver",
        blog_file=str(blog_file),
        revision_requested=False,
        session=SimpleNamespace(status=CEOApprovalSessionStatus.APPROVED),
    )
    return cast(DailyBlogApprovalBundle, SimpleNamespace(items=[item]))


def test_live_publish_allows_experience_marker(tmp_path):
    bundle = _bundle_with_content(tmp_path, "첫 줄\n::경험::\n세 번째 줄\n")

    with patch(
        "agent.content.publish_on_approval.create_naver_draft",
        return_value={"ok": True},
    ) as publisher:
        result = publish_approved_item(bundle, "naver", live=True)

    assert result == {"ok": True}
    publisher.assert_called_once()


def test_live_publish_blocks_author_note_and_reports_line(tmp_path):
    bundle = _bundle_with_content(tmp_path, "첫 줄\n둘째 줄\n발행 전 확인\n")

    with patch("agent.content.publish_on_approval.create_naver_draft") as publisher:
        with pytest.raises(PublishBlockedError) as exc_info:
            publish_approved_item(bundle, "naver", live=True)

    assert "발행 전" in str(exc_info.value)
    assert "3번째 줄" in str(exc_info.value)
    publisher.assert_not_called()


def test_live_publish_allows_clean_content(tmp_path):
    bundle = _bundle_with_content(tmp_path, "# 완성된 제목\n\n완성된 본문입니다.\n")

    with patch(
        "agent.content.publish_on_approval.create_naver_draft",
        return_value={"ok": True},
    ) as publisher:
        result = publish_approved_item(bundle, "naver", live=True)

    assert result == {"ok": True}
    publisher.assert_called_once()


def test_live_publish_allows_expert_confirmation_sentence(tmp_path):
    bundle = _bundle_with_content(
        tmp_path,
        "# 완성된 제목\n\n전문가 확인이 필요합니다.\n",
    )

    with patch(
        "agent.content.publish_on_approval.create_naver_draft",
        return_value={"ok": True},
    ) as publisher:
        result = publish_approved_item(bundle, "naver", live=True)

    assert result == {"ok": True}
    publisher.assert_called_once()
