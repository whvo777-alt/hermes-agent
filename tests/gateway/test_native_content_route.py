from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from gateway.native_content_route import (
    handle_native_content_request,
    is_native_content_request,
    resolve_native_platforms,
)
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class TestNativeContentRoute(unittest.IsolatedAsyncioTestCase):
    def _source(self) -> SessionSource:
        return SessionSource(
            platform=Platform.DISCORD,
            chat_id="1519394215946092758",
            user_id="42",
            thread_id="99",
        )

    def test_matches_discord_plaintext_daily_blog_request(self) -> None:
        event = MessageEvent(text="오늘 블로그 글 4개 작성해서 보고해줘")
        self.assertTrue(is_native_content_request(event, self._source()))

    def test_matches_natural_request_without_date_or_count(self) -> None:
        event = MessageEvent(text="블로그 글 작성해서 보고해줘")
        self.assertTrue(is_native_content_request(event, self._source()))

    def test_matches_blogspot_spaced_subject_without_second_blog_word(self) -> None:
        # Real Discord phrasing that previously fell through to content-pipeline-coo.
        event = MessageEvent(text="블로그 스팟 글 작성해서 보고해줘")
        self.assertTrue(is_native_content_request(event, self._source()))
        self.assertEqual(resolve_native_platforms(event.text), ["blogspot"])

    def test_matches_wordpress_subject_without_second_blog_word(self) -> None:
        event = MessageEvent(text="워드프레스 글 작성해서 보고해줘")
        self.assertTrue(is_native_content_request(event, self._source()))
        self.assertEqual(resolve_native_platforms(event.text), ["wordpress"])

    def test_matches_short_write_verbs(self) -> None:
        for text, platform in (
            ("블로그 스팟 글 써줘", "blogspot"),
            ("블로그스팟에 글 작성해줘", "blogspot"),
            ("워드프레스 글 작성해줘", "wordpress"),
            ("티스토리 글 올려줘", "tistory"),
        ):
            event = MessageEvent(text=text)
            self.assertTrue(is_native_content_request(event, self._source()), text)
            self.assertEqual(resolve_native_platforms(text), [platform], text)

    def test_matches_any_blog_write_phrasing(self) -> None:
        samples = [
            ("워드프레스 블로그글 하고 블로그스팟 블로그글 2개 작성해서 보고해줘", ["wordpress", "blogspot"]),
            ("블로그 초안 만들어줘", ["wordpress"]),
            ("워드프레스 포스팅 해줘", ["wordpress"]),
            ("blogspot draft 작성해", ["blogspot"]),
            ("오늘 네이버 블로그 글 써봐", ["naver"]),
            ("원고 2개 작성해줘 워드프레스 블로그스팟", ["wordpress", "blogspot"]),
        ]
        for text, platforms in samples:
            event = MessageEvent(text=text)
            self.assertTrue(is_native_content_request(event, self._source()), text)
            self.assertEqual(resolve_native_platforms(text), platforms, text)

    def test_does_not_match_non_write_chatter(self) -> None:
        for text in ("승인", "토큰 갱신 방법 알려줘", "skill install 어떻게 해"):
            event = MessageEvent(text=text)
            self.assertFalse(is_native_content_request(event, self._source()), text)

    def test_does_not_match_slash_command(self) -> None:
        event = MessageEvent(text="/content 오늘 블로그 글 4개 작성해서 보고해줘")
        self.assertFalse(is_native_content_request(event, self._source()))

    def test_resolve_platforms_defaults_to_wordpress_only(self) -> None:
        self.assertEqual(resolve_native_platforms("블로그 글 작성해서 보고해줘"), ["wordpress"])

    def test_resolve_platforms_all_four_when_count_requested(self) -> None:
        self.assertEqual(
            resolve_native_platforms("오늘 블로그 글 4개 작성해서 보고해줘"),
            ["wordpress", "blogspot", "tistory", "naver"],
        )

    def test_resolve_platforms_blogspot_only(self) -> None:
        self.assertEqual(
            resolve_native_platforms("블로그스팟 블로그 글 작성해서 보고해줘"),
            ["blogspot"],
        )

    async def test_handler_uses_native_coo_tool_without_skill_or_repository2(self) -> None:
        event = MessageEvent(text="오늘 블로그 글 4개 작성해서 보고해줘")
        payload = {
            "plan": {"run_date": "2026-07-17"},
            "daily_blog_bundle": {
                "run_date": "2026-07-17",
                "items": [
                    {
                        "platform": "naver",
                        "topic_title": "테스트 주제",
                        "quality_score": 99,
                        "quality_passed": True,
                        "blog_file": "/opt/data/.hermes/content/2026-07-17/naver/blog.md",
                    }
                ],
            },
        }
        approval_sender = AsyncMock()
        with patch("tools.coo_tools.coo_orchestrate", return_value=json.dumps(payload)) as coo:
            response = await handle_native_content_request(
                event,
                self._source(),
                approval_sender=approval_sender,
            )

        coo.assert_called_once()
        self.assertEqual(
            coo.call_args.kwargs.get("platforms"),
            ["wordpress", "blogspot", "tistory", "naver"],
        )
        approval_sender.assert_awaited_once_with(payload["daily_blog_bundle"]["items"][0])
        assert response is not None
        self.assertIn("승인 카드에서 확인해 주세요", response)
        self.assertIn("테스트 주제", response)
        # No internal call-stack / pipeline jargon in the user-facing report.
        self.assertNotIn("호출 스택", response)
        self.assertNotIn("Observed Python stack", response)
        self.assertNotIn("content-pipeline-coo", response)
        self.assertNotIn("Hermes COO Approval Required", response)
        self.assertNotIn("/opt/data/multi-content-pipeline", response)
        self.assertNotIn("run_report.md", response)
        self.assertNotIn("publishing_plan.md", response)

    async def test_handler_defaults_to_wordpress_only(self) -> None:
        event = MessageEvent(text="블로그 글 작성해서 보고해줘")
        payload = {
            "plan": {"run_date": "2026-07-18"},
            "daily_blog_bundle": {
                "run_date": "2026-07-18",
                "items": [
                    {
                        "platform": "wordpress",
                        "topic_title": "워드프레스 테스트",
                        "quality_score": 90,
                        "quality_passed": True,
                        "blog_file": "/tmp/wordpress.md",
                    }
                ],
            },
        }
        approval_sender = AsyncMock()
        with patch("tools.coo_tools.coo_orchestrate", return_value=json.dumps(payload)) as coo:
            response = await handle_native_content_request(
                event,
                self._source(),
                approval_sender=approval_sender,
            )

        self.assertEqual(coo.call_args.kwargs.get("platforms"), ["wordpress"])
        self.assertEqual(approval_sender.await_count, 1)
        assert response is not None
        self.assertIn("wordpress 원고 1개를 작성했습니다", response)
        self.assertNotIn("호출 스택", response)

    async def test_handler_sends_one_approval_card_per_platform(self) -> None:
        event = MessageEvent(text="오늘 블로그 글 4개 작성해서 보고해줘")
        items = [
            {
                "platform": platform,
                "topic_title": f"{platform} 테스트",
                "quality_score": 90,
                "quality_passed": True,
                "blog_file": f"/tmp/{platform}.md",
            }
            for platform in ("wordpress", "blogspot", "tistory", "naver")
        ]
        payload = {
            "plan": {"run_date": "2026-07-18"},
            "daily_blog_bundle": {
                "run_date": "2026-07-18",
                "items": items,
            },
        }
        approval_sender = AsyncMock()
        with patch("tools.coo_tools.coo_orchestrate", return_value=json.dumps(payload)):
            response = await handle_native_content_request(
                event,
                self._source(),
                approval_sender=approval_sender,
            )

        self.assertEqual(approval_sender.await_count, 4)
        self.assertEqual(
            [call.args[0]["platform"] for call in approval_sender.await_args_list],
            ["wordpress", "blogspot", "tistory", "naver"],
        )
        assert response is not None
        self.assertIn("원고 4개를 작성했습니다", response)


if __name__ == "__main__":
    unittest.main()
