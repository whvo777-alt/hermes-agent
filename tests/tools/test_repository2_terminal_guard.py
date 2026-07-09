"""Repository2 terminal execution guard tests (Phase 6D-4 / 6D-4A)."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import tools.terminal_tool as terminal_tool

from tools.repository2_terminal_guard import (
    REPOSITORY2_BLOCK_MESSAGE,
    check_repository2_terminal_block,
)

from agent.coo.dispatch_unlock_context import DispatchUnlockContext

REPOSITORY2_ROOT = "/opt/data/multi-content-pipeline"
SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills/content-pipeline/content-pipeline-coo/SKILL.md"
)


def _minimal_terminal_config(cwd="/tmp"):
    return {
        "env_type": "local",
        "cwd": cwd,
        "timeout": 30,
        "lifetime_seconds": 3600,
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
    }


def _valid_unlock_context(
    *,
    target_skills: tuple[str, ...] = ("create_content",),
) -> DispatchUnlockContext:
    return DispatchUnlockContext(
        token_id="token-1",
        ticket_id="ticket-1",
        plan_id="plan-1",
        execute_request_id="execute-request-1",
        gate_id="gate-1",
        dry_run_run_id="dry-run-1",
        target_skills=target_skills,
        dispatch_generation=0,
        pipeline_root=REPOSITORY2_ROOT,
        allowed_entrypoints=("node pipeline.js",),
    )


class TestRepository2TerminalGuard(unittest.TestCase):
    def test_blocks_node_pipeline_js_in_repository2_workdir(self) -> None:
        blocked = check_repository2_terminal_block(
            "node pipeline.js",
            effective_workdir=REPOSITORY2_ROOT,
            repository2_root=REPOSITORY2_ROOT,
        )
        self.assertEqual(blocked, REPOSITORY2_BLOCK_MESSAGE)

    def test_blocks_npm_run_verify_in_repository2_workdir(self) -> None:
        blocked = check_repository2_terminal_block(
            "npm run verify",
            effective_workdir=REPOSITORY2_ROOT,
            repository2_root=REPOSITORY2_ROOT,
        )
        self.assertEqual(blocked, REPOSITORY2_BLOCK_MESSAGE)

    def test_allows_safe_command_in_repository2_workdir(self) -> None:
        blocked = check_repository2_terminal_block(
            "ls -la outputs",
            effective_workdir=REPOSITORY2_ROOT,
            repository2_root=REPOSITORY2_ROOT,
        )
        self.assertIsNone(blocked)

    def test_allows_pipeline_js_outside_repository2_context(self) -> None:
        blocked = check_repository2_terminal_block(
            "node pipeline.js",
            effective_workdir="/tmp",
            repository2_root=REPOSITORY2_ROOT,
        )
        self.assertIsNone(blocked)

    def test_blocks_when_command_embeds_repository2_path(self) -> None:
        blocked = check_repository2_terminal_block(
            f"cd {REPOSITORY2_ROOT} && npm test",
            effective_workdir="/tmp",
            repository2_root=REPOSITORY2_ROOT,
        )
        self.assertEqual(blocked, REPOSITORY2_BLOCK_MESSAGE)

    def test_case_b_env_cwd_repository2_blocks_npm_run(self) -> None:
        """env.cwd = Repository2; npm run verify → blocked."""
        blocked = check_repository2_terminal_block(
            "npm run verify",
            effective_workdir=REPOSITORY2_ROOT,
            repository2_root=REPOSITORY2_ROOT,
        )
        self.assertEqual(blocked, REPOSITORY2_BLOCK_MESSAGE)

    def test_case_c_env_cwd_repository2_allows_ls(self) -> None:
        """env.cwd = Repository2; ls → allowed."""
        blocked = check_repository2_terminal_block(
            "ls",
            effective_workdir=REPOSITORY2_ROOT,
            repository2_root=REPOSITORY2_ROOT,
        )
        self.assertIsNone(blocked)

    def test_case_d_normal_cwd_allows_pipeline_js(self) -> None:
        """env.cwd = normal directory; node pipeline.js → allowed."""
        blocked = check_repository2_terminal_block(
            "node pipeline.js",
            effective_workdir="/tmp",
            repository2_root=REPOSITORY2_ROOT,
        )
        self.assertIsNone(blocked)

    def test_skill_md_forbids_terminal_repository2_execution(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        self.assertNotIn("unless COO selected the skill and policy allows it", text)
        self.assertIn("Never run", text)
        self.assertIn("node pipeline.js", text)
        self.assertIn("via **`terminal`**", text)
        self.assertIn("plan-only", text)

    def test_terminal_tool_returns_block_without_subprocess(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with patch.object(
                terminal_tool,
                "_get_env_config",
                return_value=_minimal_terminal_config(),
            ):
                raw = terminal_tool.terminal_tool(
                    command="node pipeline.js",
                    workdir=REPOSITORY2_ROOT,
                )

        payload = json.loads(raw)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["error"], REPOSITORY2_BLOCK_MESSAGE)
        self.assertEqual(payload["exit_code"], 1)

    def test_case_a_session_cwd_bypass_blocked_after_cd(self) -> None:
        """cd into Repository2 then node pipeline.js with workdir=None → blocked."""

        class FakeEnv:
            env = {}
            cwd = "/tmp"

            def execute(self, command, **kwargs):
                stripped = command.strip()
                if stripped.startswith("cd ") and "&&" not in stripped:
                    self.cwd = stripped[3:].strip().strip('"').strip("'")
                return {"output": "", "returncode": 0}

        task_id = "r2-session-cwd-bypass"
        fake_env = FakeEnv()

        with patch.object(FakeEnv, "execute", wraps=fake_env.execute) as execute_mock:
            with (
                patch.object(terminal_tool, "_active_environments", {task_id: fake_env}),
                patch.object(terminal_tool, "_last_activity", {}),
                patch.object(terminal_tool, "_task_env_overrides", {}),
                patch.object(terminal_tool, "_get_env_config", return_value=_minimal_terminal_config()),
                patch.object(terminal_tool, "_start_cleanup_thread", lambda: None),
                patch.object(terminal_tool, "_resolve_container_task_id", lambda value: value or "default"),
                patch.object(
                    terminal_tool,
                    "_check_all_guards",
                    lambda command, env_type, **kwargs: {"approved": True},
                ),
                patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            ):
                cd_result = json.loads(
                    terminal_tool.terminal_tool(
                        command=f"cd {REPOSITORY2_ROOT}",
                        task_id=task_id,
                    )
                )
                self.assertEqual(cd_result["exit_code"], 0)
                self.assertEqual(fake_env.cwd, REPOSITORY2_ROOT)

                blocked_result = json.loads(
                    terminal_tool.terminal_tool(
                        command="node pipeline.js",
                        task_id=task_id,
                    )
                )

        self.assertEqual(blocked_result["status"], "blocked")
        self.assertEqual(blocked_result["error"], REPOSITORY2_BLOCK_MESSAGE)
        self.assertEqual(execute_mock.call_count, 1)


class TestRepository2TerminalGuardScopedUnlock(unittest.TestCase):
    def test_valid_context_allows_node_pipeline_js_in_repository2(self) -> None:
        blocked = check_repository2_terminal_block(
            "node pipeline.js",
            effective_workdir=REPOSITORY2_ROOT,
            repository2_root=REPOSITORY2_ROOT,
            unlock_context=_valid_unlock_context(),
        )
        self.assertIsNone(blocked)

    def test_valid_context_blocks_npm_run_preview_approvals(self) -> None:
        blocked = check_repository2_terminal_block(
            "npm run preview:approvals",
            effective_workdir=REPOSITORY2_ROOT,
            repository2_root=REPOSITORY2_ROOT,
            unlock_context=_valid_unlock_context(),
        )
        self.assertEqual(blocked, REPOSITORY2_BLOCK_MESSAGE)

    def test_valid_context_blocks_npm_run_verify(self) -> None:
        blocked = check_repository2_terminal_block(
            "npm run verify",
            effective_workdir=REPOSITORY2_ROOT,
            repository2_root=REPOSITORY2_ROOT,
            unlock_context=_valid_unlock_context(),
        )
        self.assertEqual(blocked, REPOSITORY2_BLOCK_MESSAGE)

    def test_valid_context_blocks_publish_and_preflight(self) -> None:
        for command in ("npm run preflight:publish", "publish now"):
            with self.subTest(command=command):
                blocked = check_repository2_terminal_block(
                    command,
                    effective_workdir=REPOSITORY2_ROOT,
                    repository2_root=REPOSITORY2_ROOT,
                    unlock_context=_valid_unlock_context(),
                )
                self.assertEqual(blocked, REPOSITORY2_BLOCK_MESSAGE)

    def test_valid_context_blocks_pipeline_js_outside_repository2(self) -> None:
        blocked = check_repository2_terminal_block(
            "node pipeline.js",
            effective_workdir="/tmp",
            repository2_root=REPOSITORY2_ROOT,
            unlock_context=_valid_unlock_context(),
        )
        self.assertEqual(blocked, REPOSITORY2_BLOCK_MESSAGE)

    def test_valid_context_allows_ls_in_repository2(self) -> None:
        blocked = check_repository2_terminal_block(
            "ls -la outputs",
            effective_workdir=REPOSITORY2_ROOT,
            repository2_root=REPOSITORY2_ROOT,
            unlock_context=_valid_unlock_context(),
        )
        self.assertIsNone(blocked)

    def test_context_without_create_content_blocks_pipeline_js(self) -> None:
        blocked = check_repository2_terminal_block(
            "node pipeline.js",
            effective_workdir=REPOSITORY2_ROOT,
            repository2_root=REPOSITORY2_ROOT,
            unlock_context=_valid_unlock_context(target_skills=()),
        )
        self.assertEqual(blocked, REPOSITORY2_BLOCK_MESSAGE)


class TestRepository2TerminalGuardIntegration(unittest.TestCase):
    def test_terminal_tool_force_without_context_still_blocks(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with patch.object(
                terminal_tool,
                "_get_env_config",
                return_value=_minimal_terminal_config(),
            ):
                raw = terminal_tool.terminal_tool(
                    command="node pipeline.js",
                    workdir=REPOSITORY2_ROOT,
                    force=True,
                )

        payload = json.loads(raw)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["error"], REPOSITORY2_BLOCK_MESSAGE)
        self.assertEqual(payload["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
