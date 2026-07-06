"""Session user-message binding for gateway tool fallbacks (COO ceo_message)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agent.skill_commands import extract_user_instruction_from_skill_message
from gateway.message_timestamps import render_user_content_with_timestamp
from gateway.session_context import (
    clear_session_vars,
    get_session_env,
    inbound_message_for_session_user_binding,
    resolve_session_user_message,
    set_session_user_message,
    set_session_vars,
)

_SINGLE_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "content-pipeline-coo" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n\n"
    "# Content Pipeline COO\n\n"
    "Skill body omitted in test.\n\n"
    "The user has provided the following instruction alongside the skill invocation: "
    "오늘 블로그 글 작성해서 보고해"
)


class TestInboundMessageForSessionUserBinding(unittest.TestCase):
    def test_prefers_persist_user_message_over_timestamped_message_text(self) -> None:
        clean = _SINGLE_SKILL_TURN
        ts = datetime(2026, 7, 7, 1, 0, 0, tzinfo=timezone.utc).timestamp()
        timestamped = render_user_content_with_timestamp(clean, ts, tz=timezone.utc)

        selected = inbound_message_for_session_user_binding(
            persist_user_message=clean,
            message_text=timestamped,
        )
        self.assertEqual(selected, clean)
        self.assertNotEqual(selected, timestamped)

    def test_falls_back_to_message_text_when_persist_unset(self) -> None:
        self.assertEqual(
            inbound_message_for_session_user_binding(
                persist_user_message=None,
                message_text="plain user request",
            ),
            "plain user request",
        )


class TestResolveSessionUserMessage(unittest.TestCase):
    def test_skill_expanded_clean_message_extracts_instruction(self) -> None:
        self.assertEqual(
            resolve_session_user_message(_SINGLE_SKILL_TURN),
            "오늘 블로그 글 작성해서 보고해",
        )

    def test_plain_message_passes_through(self) -> None:
        self.assertEqual(
            resolve_session_user_message("오늘 상태 보고해"),
            "오늘 상태 보고해",
        )

    def test_bare_slash_command_clears_binding(self) -> None:
        self.assertEqual(resolve_session_user_message("/content-pipeline-coo"), "")

    def test_timestamp_prefix_on_skill_message_fails_extraction(self) -> None:
        """Regression: message_text with timestamp must not be used for binding."""
        ts = datetime(2026, 7, 7, 1, 0, 0, tzinfo=timezone.utc).timestamp()
        timestamped = render_user_content_with_timestamp(
            _SINGLE_SKILL_TURN,
            ts,
            tz=timezone.utc,
        )
        extracted = extract_user_instruction_from_skill_message(timestamped)
        self.assertIsInstance(extracted, str)
        self.assertNotEqual(extracted, "오늘 블로그 글 작성해서 보고해")
        contaminated = resolve_session_user_message(timestamped)
        self.assertNotEqual(contaminated, "오늘 블로그 글 작성해서 보고해")
        self.assertTrue(contaminated.startswith("["))

    def test_clean_skill_message_still_extracts_with_timestamps_enabled_path(self) -> None:
        clean = _SINGLE_SKILL_TURN
        ts = datetime(2026, 7, 7, 1, 0, 0, tzinfo=timezone.utc).timestamp()
        timestamped = render_user_content_with_timestamp(clean, ts, tz=timezone.utc)
        bound = resolve_session_user_message(
            inbound_message_for_session_user_binding(
                persist_user_message=clean,
                message_text=timestamped,
            )
        )
        self.assertEqual(bound, "오늘 블로그 글 작성해서 보고해")


class TestSessionUserMessageContextVar(unittest.TestCase):
    def test_set_session_user_message_stores_resolved_instruction(self) -> None:
        tokens = set_session_vars(platform="discord")
        try:
            set_session_user_message(
                resolve_session_user_message(_SINGLE_SKILL_TURN),
            )
            self.assertEqual(
                get_session_env("HERMES_SESSION_USER_MESSAGE"),
                "오늘 블로그 글 작성해서 보고해",
            )
        finally:
            clear_session_vars(tokens)


if __name__ == "__main__":
    unittest.main()
