"""COO toolset exposure for Discord CEO approval entry (Phase 6C-9)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS, _DEFAULT_OFF_TOOLSETS, _get_platform_tools
from toolsets import resolve_toolset


class TestCooToolsetExposure(unittest.TestCase):
    def test_coo_in_configurable_toolsets(self) -> None:
        keys = {ts_key for ts_key, _, _ in CONFIGURABLE_TOOLSETS}
        self.assertIn("coo", keys)

    def test_coo_toolset_includes_coo_orchestrate(self) -> None:
        tools = resolve_toolset("coo")
        self.assertIn("coo_orchestrate", tools)

    def test_coo_is_opt_in_not_enabled_by_default_on_discord(self) -> None:
        self.assertIn("coo", _DEFAULT_OFF_TOOLSETS)
        enabled = _get_platform_tools({}, "discord")
        self.assertNotIn("coo", enabled)

    def test_discord_can_enable_coo_toolset_via_config(self) -> None:
        config = {"platform_toolsets": {"discord": ["web", "terminal", "coo"]}}
        enabled = _get_platform_tools(config, "discord")
        self.assertIn("coo", enabled)
        self.assertIn("coo_orchestrate", resolve_toolset("coo"))

    def test_hermes_discord_default_core_tools_unchanged(self) -> None:
        discord_tools = set(resolve_toolset("hermes-discord"))
        self.assertIn("terminal", discord_tools)
        self.assertIn("skill_view", discord_tools)
        self.assertIn("clarify", discord_tools)
        self.assertNotIn("coo_orchestrate", discord_tools)

    def test_coo_orchestrate_has_no_execution_side_effects(self) -> None:
        from tools.coo_tools import coo_orchestrate

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate("오늘 상태 보고해", run_date="2026-07-04")
        import json

        payload = json.loads(raw)
        session = payload.get("approval_session")
        if session is not None:
            self.assertEqual(session.get("execution_ticket_id"), "")
            self.assertFalse(session.get("execution_dispatched"))
            self.assertFalse(session.get("publish_dispatched"))

    def test_content_pipeline_coo_skill_documents_entry_and_coo_orchestrate(self) -> None:
        skill_path = (
            Path(__file__).resolve().parents[2]
            / "optional-skills"
            / "content-pipeline"
            / "content-pipeline-coo"
            / "SKILL.md"
        )
        text = skill_path.read_text(encoding="utf-8")
        self.assertIn("coo_orchestrate", text)
        self.assertIn("/content-pipeline-coo", text)
        self.assertIn("hermes skills install official/content-pipeline/content-pipeline-coo", text)


if __name__ == "__main__":
    unittest.main()
