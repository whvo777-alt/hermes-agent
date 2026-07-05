"""Unit tests for Discord COO approval handler entry point (Phase 6C-1)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from agent.coo.approval_report import build_approval_report
from agent.coo.approval_session import CEOApprovalSessionStore
from agent.coo.orchestrator import COOOrchestrator
from plugins.platforms.discord.coo_approval import (
    _calculate_embed_size,
    build_coo_approval_components,
    build_coo_approval_embed_payload,
    build_coo_approval_session_payload,
    normalize_discord_snowflake,
)


def _sample_session_payload(**overrides: object) -> dict:
    payload = {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "status": "pending",
        "task_kind": "daily_brief",
        "run_date": "2026-07-04",
        "report_status": "ready",
        "runtime_status": "selected",
        "requester_id": "987654321012345678",
        "channel_id": "111222333444555666",
        "execution_ticket_id": "",
        "execution_dispatched": False,
        "publish_dispatched": False,
    }
    payload.update(overrides)
    return payload


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


class TestDiscordCooApprovalUiPayload(unittest.TestCase):
    def test_embed_payload_includes_core_session_fields(self) -> None:
        session = _sample_session_payload()
        embed = build_coo_approval_embed_payload(session)

        field_map = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(embed["title"], "Hermes COO Approval Required")
        self.assertIn(session["session_id"], field_map["Session ID"])
        self.assertEqual(field_map["Status"], "pending")
        self.assertEqual(field_map["Requester"], session["requester_id"])
        self.assertEqual(field_map["Channel"], session["channel_id"])
        self.assertEqual(field_map["Report Status"], "ready")
        self.assertEqual(field_map["Runtime Status"], "selected")
        self.assertIn("Approval only", embed["footer"]["text"])

    def test_embed_payload_shows_execution_not_created_or_dispatched(self) -> None:
        embed = build_coo_approval_embed_payload(_sample_session_payload())
        field_map = {field["name"]: field["value"] for field in embed["fields"]}

        self.assertEqual(field_map["Execution Ticket"], "Not created")
        self.assertEqual(field_map["Execution"], "Not dispatched")
        self.assertEqual(field_map["Publish"], "Not dispatched")

    def test_components_include_approve_reject_refresh_with_prefix(self) -> None:
        session = _sample_session_payload()
        components = build_coo_approval_components(session)

        self.assertEqual(len(components), 3)
        labels = [button["label"] for button in components]
        self.assertEqual(labels, ["Approve", "Reject", "Refresh"])
        for button in components:
            self.assertTrue(button["custom_id"].startswith("coo_approval:"))
            self.assertIn(session["session_id"], button["custom_id"])

        self.assertEqual(
            components[0]["custom_id"],
            f"coo_approval:approve:{session['session_id']}",
        )
        self.assertEqual(
            components[1]["custom_id"],
            f"coo_approval:reject:{session['session_id']}",
        )
        self.assertEqual(
            components[2]["custom_id"],
            f"coo_approval:refresh:{session['session_id']}",
        )

    def test_components_builder_requires_session_id(self) -> None:
        with self.assertRaises(ValueError):
            build_coo_approval_components({})

    def test_embed_payload_truncates_long_field_values(self) -> None:
        long_session_id = "x" * 2000
        embed = build_coo_approval_embed_payload(
            _sample_session_payload(session_id=long_session_id)
        )
        field_map = {field["name"]: field["value"] for field in embed["fields"]}

        self.assertLessEqual(len(field_map["Session ID"]), 1024)
        self.assertTrue(field_map["Session ID"].endswith("..."))

    def test_execution_ticket_id_truncates_when_long(self) -> None:
        long_ticket = "ticket-" + ("x" * 3000)
        embed = build_coo_approval_embed_payload(
            _sample_session_payload(execution_ticket_id=long_ticket)
        )
        field_map = {field["name"]: field["value"] for field in embed["fields"]}

        self.assertLessEqual(len(field_map["Execution Ticket"]), 1024)
        self.assertTrue(field_map["Execution Ticket"].endswith("..."))

    def test_embed_total_length_stays_within_discord_limit(self) -> None:
        session = _sample_session_payload(
            session_id="s" * 2000,
            execution_ticket_id="t" * 3000,
            report_status="r" * 2000,
            runtime_status="x" * 2000,
            task_kind="k" * 800,
            requester_id="u" * 500,
            channel_id="c" * 500,
        )
        embed = build_coo_approval_embed_payload(session)

        self.assertLessEqual(_calculate_embed_size(embed), 6000)


if __name__ == "__main__":
    unittest.main()
