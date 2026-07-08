"""Runtime skill adapter tests (Phase 8E)."""

from __future__ import annotations

import inspect
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.execution_contract import SkillExecutionStatus
from agent.coo.pipeline_adapter import PipelineAdapter, PipelineAdapterConfig
from agent.coo.runtime_skill_adapter import dry_run_skill


class TestRuntimeSkillAdapter(unittest.TestCase):
    def test_create_content_dry_run_result(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = dry_run_skill(
                "create_content",
                run_date="2026-07-07",
                ticket_id="ticket-1",
            )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["source"], "pipeline_adapter_dry_run")
        self.assertEqual(result["skill_id"], "create_content")
        self.assertEqual(result["entrypoint_hint"], "node pipeline.js")
        if Path("/opt/data/multi-content-pipeline").is_dir():
            self.assertEqual(result["status"], "planned")
            self.assertEqual(result["adapter_status"], SkillExecutionStatus.PLANNED.value)
            self.assertIn("Dry-run ready", result["summary"])

    def test_approval_review_dry_run_result(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = dry_run_skill(
                "approval_review",
                run_date="2026-07-07",
                ticket_id="ticket-2",
            )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["source"], "pipeline_adapter_dry_run")
        self.assertEqual(result["skill_id"], "approval_review")
        self.assertEqual(result["entrypoint_hint"], "npm run preview:approvals")
        if Path("/opt/data/multi-content-pipeline").is_dir():
            self.assertEqual(result["status"], "planned")

    def test_publish_content_blocked_when_called_directly(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = dry_run_skill(
                "publish_content",
                run_date="2026-07-07",
                ticket_id="ticket-3",
            )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["adapter_status"], SkillExecutionStatus.BLOCKED.value)
        self.assertIn("Publish", result["blocked_reason"])

    def test_missing_r2_root_returns_failed(self) -> None:
        adapter = PipelineAdapter(
            PipelineAdapterConfig(pipeline_root="/nonexistent/pipeline/root/phase8e")
        )
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = dry_run_skill(
                "create_content",
                run_date="2026-07-07",
                ticket_id="ticket-4",
                adapter=adapter,
            )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["adapter_status"], SkillExecutionStatus.FAILED.value)
        self.assertIn("not found", result["blocked_reason"].lower())

    def test_subprocess_not_called(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            dry_run_skill(
                "create_content",
                run_date="2026-07-07",
                ticket_id="ticket-5",
            )

    def test_dispatch_not_called(self) -> None:
        adapter = PipelineAdapter(
            PipelineAdapterConfig(pipeline_root="/opt/data/multi-content-pipeline")
        )
        with patch.object(
            adapter,
            "dispatch",
            side_effect=AssertionError("dispatch must not be called"),
        ), patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = dry_run_skill(
                "create_content",
                run_date="2026-07-07",
                ticket_id="ticket-6",
                adapter=adapter,
            )

        self.assertEqual(result["source"], "pipeline_adapter_dry_run")

    def test_module_has_no_dispatch_function(self) -> None:
        import agent.coo.runtime_skill_adapter as adapter_mod

        self.assertFalse(hasattr(adapter_mod, "dispatch"))
        source = inspect.getsource(adapter_mod)
        self.assertNotIn("adapter.dispatch", source)
        self.assertNotIn(".dispatch(", source)


if __name__ == "__main__":
    unittest.main()
