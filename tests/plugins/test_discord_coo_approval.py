"""Unit tests for Discord COO approval handler entry point (Phase 6C-1)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from agent.coo.approval_report import build_approval_report
from agent.coo.approval_session import CEOApprovalSessionStore
from agent.coo.orchestrator import COOOrchestrator
from plugins.platforms.discord.coo_approval import (
    build_coo_approval_session_payload,
    normalize_discord_snowflake,
)


class TestDiscordCooApprovalEntryPoint(unittest.TestCase):
    def _ready_report_and_orchestration(self):
        import subprocess

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            orchestrated = COOOrchestrator().orchestrate(
                "오늘 상태 보고해",
                run_date="2026-07-04",
            )
        return build_approval_report(orchestrated), orchestrated

    def test_normalize_discord_snowflake_accepts_int_and_str(self) -> None:
        self.assertEqual(normalize_discord_snowflake(987654321012345678), "987654321012345678")
        self.assertEqual(normalize_discord_snowflake("111222333444555666"), "111222333444555666")

    def test_build_payload_stores_discord_user_and_channel_ids(self) -> None:
        store = CEOApprovalSessionStore()
        report, orchestrated = self._ready_report_and_orchestration()
        payload = build_coo_approval_session_payload(
            report,
            orchestrated,
            discord_user_id=987654321012345678,
            discord_channel_id="111222333444555666",
            store=store,
        )

        self.assertIsInstance(payload, dict)
        assert payload is not None
        self.assertEqual(payload["requester_id"], "987654321012345678")
        self.assertEqual(payload["channel_id"], "111222333444555666")
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["execution_ticket_id"], "")
        self.assertFalse(payload["execution_dispatched"])
        self.assertFalse(payload["publish_dispatched"])

    def test_build_payload_returns_none_for_not_started_report(self) -> None:
        store = CEOApprovalSessionStore()
        orchestrated = COOOrchestrator().orchestrate("???", run_date="2026-07-04")
        report = build_approval_report(orchestrated)
        payload = build_coo_approval_session_payload(
            report,
            orchestrated,
            discord_user_id="987654321012345678",
            discord_channel_id="111222333444555666",
            store=store,
        )

        self.assertIsNone(payload)
        self.assertEqual(len(store.list_sessions()), 0)

    def test_entry_point_exposes_prepare_only_not_approve_reject(self) -> None:
        import plugins.platforms.discord.coo_approval as coo_entry

        self.assertTrue(hasattr(coo_entry, "build_coo_approval_session_payload"))
        self.assertTrue(hasattr(coo_entry, "normalize_discord_snowflake"))
        self.assertFalse(hasattr(coo_entry, "approve_discord_session"))
        self.assertFalse(hasattr(coo_entry, "reject_discord_session"))


if __name__ == "__main__":
    unittest.main()
