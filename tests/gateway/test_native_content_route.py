from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from gateway.config import Platform
from gateway.native_content_route import is_native_content_request, handle_native_content_request
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

    def test_does_not_match_slash_command(self) -> None:
        event = MessageEvent(text="/content 오늘 블로그 글 4개 작성해서 보고해줘")
        self.assertFalse(is_native_content_request(event, self._source()))

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
        with patch("tools.coo_tools.coo_orchestrate", return_value=json.dumps(payload)) as coo:
            response = await handle_native_content_request(event, self._source())

        coo.assert_called_once()
        assert response is not None
        self.assertIn("Hermes Native Flow", response)
        self.assertIn("Research ↓ Planning ↓ Writing ↓ Quality ↓ Platform Approval", response)
        self.assertIn("Legacy Repository2 skill loading: 0회", response)
        self.assertIn("Repository2 접근: 0회", response)
        self.assertNotIn("content-pipeline-coo", response)
        self.assertNotIn("Hermes COO Approval Required", response)
        self.assertNotIn("/opt/data/multi-content-pipeline", response)
        self.assertNotIn("run_report.md", response)
        self.assertNotIn("publishing_plan.md", response)


if __name__ == "__main__":
    unittest.main()
