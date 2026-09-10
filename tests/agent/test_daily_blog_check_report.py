"""Tests for appending draft checks to the Discord publish report."""

import agent.content.publish_check as publish_check
import plugins.platforms.discord.coo_approval as coo_approval


_CHECK = {
    "hero_image": True,
    "image_count": 2,
    "expected_images": 0,
    "internal_links": 1,
    "blockers": [],
    "text_chars": 2345,
    "warnings": [],
}


def test_check_is_appended_after_existing_report():
    result = coo_approval._daily_blog_result_message(
        {
            "item": {"platform": "blogspot", "platform_label": "블로그스팟"},
            "publish_result": {
                "postId": "123",
                "url": "https://example.com/draft",
                "status": "draft",
                "check": _CHECK,
            },
        },
        "approve",
    )

    assert result.startswith(
        "Blogspot 초안 생성 완료\n"
        "- 글 ID: `123`\n"
        "- 상태: `draft`\n"
        "- 확인 링크: https://example.com/draft\n"
        "- 공개 발행: 실행되지 않음\n\n"
    )
    assert "점검  블로그스팟 초안" in result
    assert "본문 글자      2,345자" in result


def test_empty_check_does_not_append_anything():
    message = "기존 발행 보고"

    assert coo_approval._append_draft_check(message, {"check": {}}) == message


def test_missing_check_key_does_not_raise():
    message = "기존 발행 보고"

    assert coo_approval._append_draft_check(message, {"status": "draft"}) == message


def test_none_result_does_not_raise():
    message = "기존 발행 보고"

    assert coo_approval._append_draft_check(message, None) == message


def test_existing_report_text_is_preserved_exactly():
    result = coo_approval._daily_blog_result_message(
        {
            "item": {"platform": "wordpress", "platform_label": "워드프레스"},
            "publish_result": {
                "response": {
                    "id": 123,
                    "status": "draft",
                    "adminLink": "https://example.com/wp-admin/post.php?id=123",
                },
                "hero_image_status": "attached",
                "check": _CHECK,
            },
        },
        "approve",
    )

    expected_report = (
        "WordPress 초안 생성 완료\n"
        "- 글 ID: `123`\n"
        "- 상태: `draft`\n"
        "- 관리자 확인 링크: https://example.com/wp-admin/post.php?id=123\n"
        "- 대표이미지: 정상 첨부됨\n"
        "- 공개 발행: 실행되지 않음"
    )
    assert result[: len(expected_report)] == expected_report


def test_1900_character_report_never_exceeds_discord_limit():
    message = "기" * 1900

    result = coo_approval._append_draft_check(message, {"check": _CHECK})

    assert len(result) <= 2000


def test_insufficient_room_drops_check_report_instead_of_splitting_it():
    message = "기" * 1880

    result = coo_approval._append_draft_check(message, {"check": _CHECK})

    assert result == message


def test_formatter_exception_returns_original_report(monkeypatch):
    message = "기존 발행 보고"

    def raise_formatter(*args, **kwargs):
        raise RuntimeError("formatter failed")

    monkeypatch.setattr(publish_check, "format_check_report", raise_formatter)

    assert coo_approval._append_draft_check(message, {"check": _CHECK}) == message
