"""Tests for production executor factory (Phase 10N)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.production_executor_factory import (
    _ALLOWED_FACTORY_ENTRYPOINT,
    _TIMEOUT_EXIT_CODE,
    _run_bounded_subprocess,
    build_pipeline_dispatch_executor,
    default_evidence_dir,
)
from agent.coo.production_executor_policy import ProductionExecutorPolicy


def _enabled_policy(*, pipeline_root: str) -> ProductionExecutorPolicy:
    return ProductionExecutorPolicy(
        enabled=True,
        allowed_pipeline_roots=(pipeline_root,),
    )


class TestProductionExecutorFactory(unittest.TestCase):
    def test_policy_disabled_raises(self) -> None:
        with self.assertRaises(ValueError) as exc:
            build_pipeline_dispatch_executor(
                ProductionExecutorPolicy(enabled=False),
                pipeline_root="/tmp/fake-pipeline",
                entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                node_path="/usr/bin/node",
            )
        self.assertIn("disabled", str(exc.exception))

    def test_empty_allowlist_raises(self) -> None:
        policy = ProductionExecutorPolicy(
            enabled=True,
            allowed_pipeline_roots=(),
        )
        with self.assertRaises(ValueError) as exc:
            build_pipeline_dispatch_executor(
                policy,
                pipeline_root="/tmp/fake-pipeline",
                entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                node_path="/usr/bin/node",
            )
        self.assertIn("allowed_pipeline_roots", str(exc.exception))

    def test_root_outside_allowlist_raises(self) -> None:
        policy = _enabled_policy(pipeline_root="/tmp/allowed-root")
        with self.assertRaises(ValueError) as exc:
            build_pipeline_dispatch_executor(
                policy,
                pipeline_root="/tmp/other-root",
                entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                node_path="/usr/bin/node",
            )
        self.assertIn("outside", str(exc.exception))

    def test_symlink_escape_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as allowed_dir:
            with tempfile.TemporaryDirectory() as outside_dir:
                link_path = os.path.join(allowed_dir, "escape-link")
                os.symlink(outside_dir, link_path)
                policy = _enabled_policy(pipeline_root=allowed_dir)
                with self.assertRaises(ValueError):
                    build_pipeline_dispatch_executor(
                        policy,
                        pipeline_root=link_path,
                        entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                        node_path="/usr/bin/node",
                    )

    def test_entrypoint_not_allowed_raises(self) -> None:
        policy = _enabled_policy(pipeline_root="/tmp/fake-pipeline")
        with self.assertRaises(ValueError) as exc:
            build_pipeline_dispatch_executor(
                policy,
                pipeline_root="/tmp/fake-pipeline",
                entrypoint="node other.js",
                node_path="/usr/bin/node",
            )
        self.assertIn("entrypoint", str(exc.exception))

    def test_npm_publish_preflight_blocked(self) -> None:
        policy = _enabled_policy(pipeline_root="/tmp/fake-pipeline")
        for entrypoint in (
            "npm run preflight:publish",
            "npm run publish",
            "node pipeline.js publish",
            "node preflight.js",
        ):
            with self.assertRaises(ValueError, msg=entrypoint):
                build_pipeline_dispatch_executor(
                    policy,
                    pipeline_root="/tmp/fake-pipeline",
                    entrypoint=entrypoint,
                    node_path="/usr/bin/node",
                )

    def test_node_missing_raises_without_calling_runner(self) -> None:
        policy = _enabled_policy(pipeline_root="/tmp/fake-pipeline")
        runner_calls: list[tuple] = []

        def runner(argv, cwd, env, timeout_seconds):
            runner_calls.append((argv, cwd, env, timeout_seconds))
            return 0, "", ""

        with patch("agent.coo.production_executor_factory.shutil.which", return_value=None):
            with self.assertRaises(ValueError) as exc:
                build_pipeline_dispatch_executor(
                    policy,
                    pipeline_root="/tmp/fake-pipeline",
                    entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                    subprocess_runner=runner,
                )
        self.assertIn("Node executable not found", str(exc.exception))
        self.assertEqual(runner_calls, [])

    def test_valid_policy_builds_executor_and_calls_runner(self) -> None:
        with tempfile.TemporaryDirectory() as pipeline_root:
            policy = _enabled_policy(pipeline_root=pipeline_root)
            captured: dict[str, object] = {}

            def runner(argv, cwd, env, timeout_seconds):
                captured["argv"] = argv
                captured["cwd"] = cwd
                captured["env"] = env
                captured["timeout_seconds"] = timeout_seconds
                return 0, "ok-output", ""

            executor = build_pipeline_dispatch_executor(
                policy,
                pipeline_root=pipeline_root,
                entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                subprocess_runner=runner,
                node_path="/usr/bin/fake-node",
                evidence_run_id="evidence-1",
            )

            with (
                patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
                patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
                patch(
                    "agent.coo.production_executor_factory.get_hermes_home",
                ) as mock_home,
            ):
                hermes_home = Path(pipeline_root) / ".hermes"
                hermes_home.mkdir()
                mock_home.return_value = hermes_home
                exit_code, stdout_text, stderr_text = executor(
                    _ALLOWED_FACTORY_ENTRYPOINT,
                    pipeline_root,
                    "2026-07-07",
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout_text, "ok-output")
            self.assertEqual(stderr_text, "")
            self.assertEqual(
                captured["argv"],
                ["/usr/bin/fake-node", "pipeline.js", "--run-date", "2026-07-07"],
            )
            self.assertEqual(captured["cwd"], os.path.realpath(pipeline_root))
            self.assertEqual(captured["timeout_seconds"], policy.max_runtime_seconds)

    def test_stdout_stderr_truncated_in_return_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as pipeline_root:
            policy = _enabled_policy(pipeline_root=pipeline_root)
            long_output = "x" * 100_000

            def runner(argv, cwd, env, timeout_seconds):
                return 0, long_output, long_output

            executor = build_pipeline_dispatch_executor(
                policy,
                pipeline_root=pipeline_root,
                entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                subprocess_runner=runner,
                node_path="/usr/bin/fake-node",
                evidence_run_id="evidence-truncate",
                max_output_bytes=1000,
            )

            with patch(
                "agent.coo.production_executor_factory.get_hermes_home",
            ) as mock_home:
                hermes_home = Path(pipeline_root) / ".hermes"
                hermes_home.mkdir(exist_ok=True)
                mock_home.return_value = hermes_home
                exit_code, stdout_text, stderr_text = executor(
                    _ALLOWED_FACTORY_ENTRYPOINT,
                    pipeline_root,
                    "2026-07-07",
                )

            self.assertEqual(exit_code, 0)
            self.assertLessEqual(len(stdout_text.encode("utf-8")), 1100)
            self.assertIn("...[truncated]", stdout_text)
            self.assertIn("...[truncated]", stderr_text)
            stdout_path = hermes_home / "coo" / "execution-evidence" / "evidence-truncate.stdout.log"
            self.assertTrue(stdout_path.is_file())
            self.assertGreater(len(stdout_path.read_text(encoding="utf-8")), 1000)

    def test_evidence_rejects_repository2_like_path_without_creating_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            fake_repo_evidence = Path(tmp) / "fake-multi-content-pipeline" / "outputs" / "evidence"
            policy = _enabled_policy(pipeline_root="/tmp/fake-pipeline")

            with patch(
                "agent.coo.production_executor_factory.get_hermes_home",
                return_value=hermes_home,
            ):
                with self.assertRaises(ValueError):
                    build_pipeline_dispatch_executor(
                        policy,
                        pipeline_root="/tmp/fake-pipeline",
                        entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                        node_path="/usr/bin/fake-node",
                        evidence_dir=fake_repo_evidence,
                    )
            self.assertFalse(fake_repo_evidence.exists())

    def test_timeout_result_handling(self) -> None:
        with tempfile.TemporaryDirectory() as pipeline_root:
            policy = _enabled_policy(pipeline_root=pipeline_root)

            def runner(argv, cwd, env, timeout_seconds):
                return _TIMEOUT_EXIT_CODE, "", "timeout after 5s"

            executor = build_pipeline_dispatch_executor(
                policy,
                pipeline_root=pipeline_root,
                entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                subprocess_runner=runner,
                node_path="/usr/bin/fake-node",
                evidence_run_id="evidence-timeout",
            )

            with patch(
                "agent.coo.production_executor_factory.get_hermes_home",
            ) as mock_home:
                hermes_home = Path(pipeline_root) / ".hermes"
                hermes_home.mkdir(exist_ok=True)
                mock_home.return_value = hermes_home
                exit_code, _stdout, stderr_text = executor(
                    _ALLOWED_FACTORY_ENTRYPOINT,
                    pipeline_root,
                    "2026-07-07",
                )

            self.assertEqual(exit_code, _TIMEOUT_EXIT_CODE)
            self.assertIn("timeout", stderr_text.lower())
            meta_path = hermes_home / "coo" / "execution-evidence" / "evidence-timeout.meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["exit_code"], _TIMEOUT_EXIT_CODE)

    def test_default_evidence_dir_under_hermes_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            with patch(
                "agent.coo.production_executor_factory.get_hermes_home",
                return_value=hermes_home,
            ):
                self.assertEqual(
                    default_evidence_dir(),
                    hermes_home / "coo" / "execution-evidence",
                )

    def test_invalid_calendar_dates_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as pipeline_root:
            policy = _enabled_policy(pipeline_root=pipeline_root)
            runner_calls: list[tuple] = []

            def runner(argv, cwd, env, timeout_seconds):
                runner_calls.append((argv, cwd, env, timeout_seconds))
                return 0, "", ""

            executor = build_pipeline_dispatch_executor(
                policy,
                pipeline_root=pipeline_root,
                entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                subprocess_runner=runner,
                node_path="/usr/bin/fake-node",
            )

            for invalid_date in ("2026-13-40", "2026-02-30"):
                with self.assertRaises(ValueError) as exc:
                    executor(
                        _ALLOWED_FACTORY_ENTRYPOINT,
                        pipeline_root,
                        invalid_date,
                    )
                self.assertIn("valid calendar date", str(exc.exception))
            self.assertEqual(runner_calls, [])

    def test_valid_calendar_dates_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as pipeline_root:
            policy = _enabled_policy(pipeline_root=pipeline_root)
            captured_dates: list[str] = []

            def runner(argv, cwd, env, timeout_seconds):
                captured_dates.append(argv[-1])
                return 0, "", ""

            executor = build_pipeline_dispatch_executor(
                policy,
                pipeline_root=pipeline_root,
                entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                subprocess_runner=runner,
                node_path="/usr/bin/fake-node",
                evidence_run_id="evidence-valid-dates",
            )

            with patch(
                "agent.coo.production_executor_factory.get_hermes_home",
            ) as mock_home:
                hermes_home = Path(pipeline_root) / ".hermes"
                hermes_home.mkdir(exist_ok=True)
                mock_home.return_value = hermes_home
                for valid_date in ("2026-07-09", "2024-02-29"):
                    exit_code, _, _ = executor(
                        _ALLOWED_FACTORY_ENTRYPOINT,
                        pipeline_root,
                        valid_date,
                    )
                    self.assertEqual(exit_code, 0)
            self.assertEqual(captured_dates, ["2026-07-09", "2024-02-29"])

    def test_bounded_subprocess_timeout_expired_branch(self) -> None:
        with patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["node"], timeout=1),
        ):
            exit_code, _stdout, stderr_text = _run_bounded_subprocess(
                ["node", "pipeline.js"],
                cwd="/tmp/fake-pipeline",
                env={},
                timeout_seconds=1,
            )
        self.assertEqual(exit_code, _TIMEOUT_EXIT_CODE)
        self.assertIn("timeout", stderr_text.lower())


if __name__ == "__main__":
    unittest.main()
