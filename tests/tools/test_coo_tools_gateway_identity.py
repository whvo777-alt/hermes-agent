"""COO approval session identity wiring for gateway Discord users."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from agent.coo.approval_session import CEOApprovalSessionStore
from agent.coo.discord_approval_adapter import approve_discord_session
from plugins.platforms.discord.coo_approval import execute_coo_approval_button_action
from tools.coo_tools import coo_orchestrate


class TestCooToolsGatewayApprovalIdentity(unittest.TestCase):
    def test_coo_orchestrate_uses_gateway_session_user_and_channel_ids(self) -> None:
        from gateway.session_context import clear_session_vars, set_session_vars

        store = CEOApprovalSessionStore()
        tokens = set_session_vars(
            platform="discord",
            user_id="987654321012345678",
            chat_id="111222333444555666",
            thread_id="222333444555666777",
        )
        try:
            with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
                raw = coo_orchestrate(
                    "오늘 상태 보고해",
                    run_date="2026-07-04",
                    session_store=store,
                )
        finally:
            clear_session_vars(tokens)

        payload = json.loads(raw)
        session = payload["approval_session"]
        self.assertEqual(session["requester_id"], "987654321012345678")
        self.assertEqual(session["channel_id"], "222333444555666777")
        self.assertEqual(session["execution_ticket_id"], "")
        self.assertFalse(session["execution_dispatched"])
        self.assertFalse(session["publish_dispatched"])

    def test_coo_orchestrate_explicit_requester_overrides_gateway_context(self) -> None:
        from gateway.session_context import clear_session_vars, set_session_vars

        store = CEOApprovalSessionStore()
        tokens = set_session_vars(
            platform="discord",
            user_id="111111111111111111",
            chat_id="999999999999999999",
        )
        try:
            with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
                raw = coo_orchestrate(
                    "오늘 상태 보고해",
                    run_date="2026-07-04",
                    session_store=store,
                    requester_id="987654321012345678",
                    channel_id="111222333444555666",
                )
        finally:
            clear_session_vars(tokens)

        session = json.loads(raw)["approval_session"]
        self.assertEqual(session["requester_id"], "987654321012345678")
        self.assertEqual(session["channel_id"], "111222333444555666")

    def test_coo_orchestrate_without_gateway_context_defaults_to_ceo(self) -> None:
        store = CEOApprovalSessionStore()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate(
                "오늘 상태 보고해",
                run_date="2026-07-04",
                session_store=store,
            )

        session = json.loads(raw)["approval_session"]
        self.assertEqual(session["requester_id"], "CEO")
        self.assertEqual(session["channel_id"], "")

    def test_matching_discord_user_can_approve_coo_orchestrate_session(self) -> None:
        from gateway.session_context import clear_session_vars, set_session_vars

        store = CEOApprovalSessionStore()
        tokens = set_session_vars(
            platform="discord",
            user_id="987654321012345678",
            chat_id="111222333444555666",
        )
        try:
            with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
                raw = coo_orchestrate(
                    "오늘 상태 보고해",
                    run_date="2026-07-04",
                    session_store=store,
                )
        finally:
            clear_session_vars(tokens)

        session = json.loads(raw)["approval_session"]
        approved = approve_discord_session(
            session["session_id"],
            "987654321012345678",
            store=store,
        )
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["execution_ticket_id"])
        self.assertFalse(approved["execution_dispatched"])
        self.assertFalse(approved["publish_dispatched"])

    def test_other_discord_user_cannot_approve_coo_orchestrate_session(self) -> None:
        from gateway.session_context import clear_session_vars, set_session_vars

        store = CEOApprovalSessionStore()
        tokens = set_session_vars(
            platform="discord",
            user_id="987654321012345678",
            chat_id="111222333444555666",
        )
        try:
            with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
                raw = coo_orchestrate(
                    "오늘 상태 보고해",
                    run_date="2026-07-04",
                    session_store=store,
                )
        finally:
            clear_session_vars(tokens)

        session = json.loads(raw)["approval_session"]
        with self.assertRaises(ValueError):
            approve_discord_session(
                session["session_id"],
                "000000000000000001",
                store=store,
            )

    def test_discord_button_approve_uses_same_store_and_requester(self) -> None:
        from gateway.session_context import clear_session_vars, set_session_vars

        store = CEOApprovalSessionStore()
        tokens = set_session_vars(
            platform="discord",
            user_id="987654321012345678",
            chat_id="111222333444555666",
        )
        try:
            with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
                raw = coo_orchestrate(
                    "오늘 상태 보고해",
                    run_date="2026-07-04",
                    session_store=store,
                )
        finally:
            clear_session_vars(tokens)

        session = json.loads(raw)["approval_session"]
        result = execute_coo_approval_button_action(
            action="approve",
            session_id=session["session_id"],
            discord_user_id="987654321012345678",
            store=store,
        )
        self.assertEqual(result["status"], "approved")


class TestCooOrchestrateCeoMessageFallback(unittest.TestCase):
    def test_explicit_ceo_message_unchanged(self) -> None:
        store = CEOApprovalSessionStore()
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate(
                "오늘 상태 보고해",
                run_date="2026-07-04",
                session_store=store,
            )

        payload = json.loads(raw)
        self.assertEqual(payload["intent"]["task_kind"], "daily_brief")
        self.assertIsNotNone(payload["approval_session"])

    def test_empty_ceo_message_uses_gateway_user_message_fallback(self) -> None:
        from gateway.session_context import clear_session_vars, set_session_vars

        store = CEOApprovalSessionStore()
        tokens = set_session_vars(
            platform="discord",
            user_id="987654321012345678",
            chat_id="111222333444555666",
            user_message="오늘 블로그 글 작성해서 보고해",
        )
        try:
            with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
                raw = coo_orchestrate(
                    "",
                    run_date="2026-07-04",
                    session_store=store,
                )
        finally:
            clear_session_vars(tokens)

        payload = json.loads(raw)
        self.assertEqual(payload["intent"]["task_kind"], "create_and_report")
        session = payload["approval_session"]
        self.assertIsNotNone(session)
        self.assertEqual(session["execution_ticket_id"], "")
        self.assertFalse(session["execution_dispatched"])
        self.assertFalse(session["publish_dispatched"])

    def test_empty_ceo_message_without_context_still_errors(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate("")

        payload = json.loads(raw)
        self.assertIn("error", payload)
        self.assertEqual(payload["error"], "ceo_message is required")
        self.assertNotIn("approval_session", payload)

    def test_explicit_ceo_message_overrides_gateway_fallback(self) -> None:
        from gateway.session_context import clear_session_vars, set_session_vars

        store = CEOApprovalSessionStore()
        tokens = set_session_vars(
            platform="discord",
            user_message="승인하고 발행해",
        )
        try:
            with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
                raw = coo_orchestrate(
                    "오늘 상태 보고해",
                    run_date="2026-07-04",
                    session_store=store,
                )
        finally:
            clear_session_vars(tokens)

        payload = json.loads(raw)
        self.assertEqual(payload["intent"]["task_kind"], "daily_brief")


if __name__ == "__main__":
    unittest.main()
