"""Tests for bounded subprocess runner dispatch profile (Phase 12G)."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.bounded_subprocess_runner import (
    RUNNER_PROFILE_DISPATCH,
    RUNNER_PROFILE_RESTRICTED,
    BoundedSubprocessRunnerError,
    create_bounded_subprocess_runner,
    validate_dispatch_runner_argv_contract,
)
from agent.coo.dispatch_cli_runner_injection import (
    RUNNER_PROFILE_RESTRICTED as INJECTION_PROFILE_RESTRICTED,
    resolve_bounded_subprocess_runner,
    resolve_dispatch_run_subprocess_runner,
)
from agent.coo.dispatch_runner_binding_state import (
    RUNNER_BINDING_STATE_BOUND,
    CooDispatchRunnerBindingState,
)
from agent.coo.production_executor_factory import (
    _ALLOWED_FACTORY_ENTRYPOINT,
    build_pipeline_dispatch_executor,
)
from agent.coo.production_executor_policy import ProductionExecutorPolicy


def _python_argv(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _make_fake_node(workspace: Path) -> Path:
    node_bin = workspace / "bin"
    node_bin.mkdir(parents=True, exist_ok=True)
    node_path = node_bin / "node"
    node_path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            print("fake-node-ok")
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    node_path.chmod(0o755)
    return node_path


def _enabled_policy(*, pipeline_root: str) -> ProductionExecutorPolicy:
    return ProductionExecutorPolicy(
        enabled=True,
        allowed_pipeline_roots=(pipeline_root,),
        allowed_entrypoints=(_ALLOWED_FACTORY_ENTRYPOINT,),
        max_runtime_seconds=300,
    )


class TestDispatchRunnerProfileValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-dispatch-profile-")
        self.workspace = Path(self.tmp.name)
        self.allowed_root = self.workspace / "pipeline"
        self.allowed_root.mkdir()
        self.fake_node = _make_fake_node(self.workspace)
        self.node_path = str(self.fake_node.resolve())
        (self.allowed_root / "pipeline.js").write_text("// fixture only\n", encoding="utf-8")
        self.runner = create_bounded_subprocess_runner(
            (str(self.allowed_root),),
            profile=RUNNER_PROFILE_DISPATCH,
            node_executable=self.node_path,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }

    def _valid_argv(self, run_date: str = "2026-07-07") -> list[str]:
        return [self.node_path, "pipeline.js", "--run-date", run_date]

    def test_dispatch_profile_valid_argv_success(self) -> None:
        exit_code, stdout, stderr = self.runner(
            self._valid_argv(),
            str(self.allowed_root),
            self._env(),
            30,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("fake-node-ok", stdout)
        self.assertEqual(stderr, "")

    def test_restricted_profile_still_blocks_node(self) -> None:
        restricted = create_bounded_subprocess_runner((str(self.allowed_root),))
        with self.assertRaises(BoundedSubprocessRunnerError):
            restricted(
                self._valid_argv(),
                str(self.allowed_root),
                self._env(),
                30,
            )

    def test_rejects_extra_arg(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                self._valid_argv() + ["--extra"],
                str(self.allowed_root),
                self._env(),
                30,
            )

    def test_rejects_invalid_date(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                self._valid_argv("2026-13-40"),
                str(self.allowed_root),
                self._env(),
                30,
            )

    def test_rejects_non_calendar_date(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                self._valid_argv("2026-02-30"),
                str(self.allowed_root),
                self._env(),
                30,
            )

    def test_rejects_pipeline_script_rename(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                [self.node_path, "runner.js", "--run-date", "2026-07-07"],
                str(self.allowed_root),
                self._env(),
                30,
            )

    def test_rejects_flag_reorder(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                [self.node_path, "--run-date", "pipeline.js", "2026-07-07"],
                str(self.allowed_root),
                self._env(),
                30,
            )

    def test_rejects_npm_npx_bash_sh_executables(self) -> None:
        for blocked in ("npm", "npx", "bash", "sh"):
            with self.subTest(blocked=blocked):
                with self.assertRaises(BoundedSubprocessRunnerError):
                    self.runner(
                        [f"/usr/bin/{blocked}", "pipeline.js", "--run-date", "2026-07-07"],
                        str(self.allowed_root),
                        self._env(),
                        30,
                    )

    def test_create_rejects_missing_node_executable(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            create_bounded_subprocess_runner(
                (str(self.allowed_root),),
                profile=RUNNER_PROFILE_DISPATCH,
            )

    def test_create_rejects_relative_node_executable(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            create_bounded_subprocess_runner(
                (str(self.allowed_root),),
                profile=RUNNER_PROFILE_DISPATCH,
                node_executable="bin/node",
            )

    def test_create_rejects_non_node_basename(self) -> None:
        fake = self.workspace / "bin" / "python-node"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        with self.assertRaises(BoundedSubprocessRunnerError):
            create_bounded_subprocess_runner(
                (str(self.allowed_root),),
                profile=RUNNER_PROFILE_DISPATCH,
                node_executable=str(fake.resolve()),
            )

    def test_rejects_argv_node_mismatch(self) -> None:
        other_node = _make_fake_node(self.workspace / "other")
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                [str(other_node.resolve()), "pipeline.js", "--run-date", "2026-07-07"],
                str(self.allowed_root),
                self._env(),
                30,
            )

    def test_rejects_relative_argv_node(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                ["bin/node", "pipeline.js", "--run-date", "2026-07-07"],
                str(self.allowed_root),
                self._env(),
                30,
            )

    def test_rejects_cwd_outside_allowlist(self) -> None:
        outside = self.workspace / "outside"
        outside.mkdir()
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                self._valid_argv(),
                str(outside),
                self._env(),
                30,
            )

    def test_rejects_production_root_cwd(self) -> None:
        with self.assertRaises(ValueError):
            self.runner(
                self._valid_argv(),
                "/opt/data/multi-content-pipeline",
                self._env(),
                30,
            )

    def test_rejects_symlink_escape(self) -> None:
        outside = self.workspace / "outside"
        outside.mkdir()
        link = self.allowed_root / "escape-link"
        link.symlink_to(outside)
        with self.assertRaises(BoundedSubprocessRunnerError):
            self.runner(
                self._valid_argv(),
                str(link),
                self._env(),
                30,
            )

    def test_node_symlink_allowed_when_resolves_to_configured_node(self) -> None:
        link = self.workspace / "node-link"
        link.symlink_to(self.fake_node)
        runner = create_bounded_subprocess_runner(
            (str(self.allowed_root),),
            profile=RUNNER_PROFILE_DISPATCH,
            node_executable=str(link),
        )
        exit_code, stdout, stderr = runner(
            [str(link), "pipeline.js", "--run-date", "2026-07-07"],
            str(self.allowed_root),
            self._env(),
            30,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("fake-node-ok", stdout)

    def test_restricted_profile_rejects_node_executable_param(self) -> None:
        with self.assertRaises(BoundedSubprocessRunnerError):
            create_bounded_subprocess_runner(
                (str(self.allowed_root),),
                profile=RUNNER_PROFILE_RESTRICTED,
                node_executable=self.node_path,
            )


class TestFactoryDispatchArgvContract(unittest.TestCase):
    def test_factory_argv_passes_dispatch_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hermes-factory-contract-") as tmp:
            pipeline_root = Path(tmp) / "pipeline"
            pipeline_root.mkdir()
            fake_node = _make_fake_node(Path(tmp))
            node_path = str(fake_node.resolve())
            (pipeline_root / "pipeline.js").write_text("// fixture\n", encoding="utf-8")

            captured: dict[str, object] = {}

            def runner(argv, cwd, env, timeout_seconds):
                captured["argv"] = argv
                validate_dispatch_runner_argv_contract(argv, node_executable=node_path)
                return 0, "ok", ""

            policy = _enabled_policy(pipeline_root=str(pipeline_root))
            executor = build_pipeline_dispatch_executor(
                policy,
                pipeline_root=str(pipeline_root),
                entrypoint=_ALLOWED_FACTORY_ENTRYPOINT,
                subprocess_runner=runner,
                node_path=node_path,
            )

            with (
                patch.object(__import__("subprocess"), "run", side_effect=AssertionError("no subprocess")),
                patch(
                    "agent.coo.production_executor_factory.get_hermes_home",
                ) as mock_home,
            ):
                hermes_home = pipeline_root / ".hermes"
                hermes_home.mkdir()
                mock_home.return_value = hermes_home
                exit_code, stdout, stderr = executor(
                    _ALLOWED_FACTORY_ENTRYPOINT,
                    str(pipeline_root),
                    "2026-07-07",
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                captured["argv"],
                [node_path, "pipeline.js", "--run-date", "2026-07-07"],
            )

    def test_factory_argv_variants_rejected_by_dispatch_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hermes-factory-reject-") as tmp:
            fake_node = _make_fake_node(Path(tmp))
            node_path = str(fake_node.resolve())
            valid = [node_path, "pipeline.js", "--run-date", "2026-07-07"]
            validate_dispatch_runner_argv_contract(valid, node_executable=node_path)

            variants = (
                valid + ["--extra"],
                [node_path, "pipeline.js", "--run-date", "not-a-date"],
                [node_path, "other.js", "--run-date", "2026-07-07"],
                ["node", "pipeline.js", "--run-date", "2026-07-07"],
            )
            for variant in variants:
                with self.subTest(variant=variant):
                    with self.assertRaises(BoundedSubprocessRunnerError):
                        validate_dispatch_runner_argv_contract(
                            variant,
                            node_executable=node_path,
                        )


class TestDispatchProfileProviderWiring(unittest.TestCase):
    def test_provider_default_real_harness_uses_restricted_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "pipeline")
            Path(isolated_root).mkdir()
            config = {
                "coo": {
                    "dispatch": {
                        "executor": {
                            "enabled": True,
                            "allowed_pipeline_roots": [isolated_root],
                        },
                        "runner_provider": {"mode": "bounded"},
                    }
                }
            }
            with patch(
                "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
                wraps=create_bounded_subprocess_runner,
            ) as create_mock:
                resolve_bounded_subprocess_runner(
                    config,
                    binding_state=CooDispatchRunnerBindingState(
                        state=RUNNER_BINDING_STATE_BOUND
                    ),
                    use_real_bounded_runner=True,
                )
            _, kwargs = create_mock.call_args
            self.assertEqual(kwargs.get("profile"), RUNNER_PROFILE_RESTRICTED)
            self.assertIsNone(kwargs.get("node_executable"))

    def test_provider_dispatch_profile_requires_explicit_node_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            isolated_root = str(Path(tmp) / "pipeline")
            Path(isolated_root).mkdir()
            config = {
                "coo": {
                    "dispatch": {
                        "executor": {
                            "enabled": True,
                            "allowed_pipeline_roots": [isolated_root],
                        },
                        "runner_provider": {"mode": "bounded"},
                    }
                }
            }
            from agent.coo.dispatch_runner_provider import (
                DispatchRunnerProviderResolutionError,
            )

            with self.assertRaises(DispatchRunnerProviderResolutionError):
                resolve_bounded_subprocess_runner(
                    config,
                    binding_state=CooDispatchRunnerBindingState(
                        state=RUNNER_BINDING_STATE_BOUND
                    ),
                    use_real_bounded_runner=True,
                    harness_profile=RUNNER_PROFILE_DISPATCH,
                )

    def test_service_dispatch_profile_opt_in_passes_node_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            isolated_root = workspace / "pipeline"
            isolated_root.mkdir()
            fake_node = _make_fake_node(workspace)
            node_path = str(fake_node.resolve())
            config = {
                "coo": {
                    "dispatch": {
                        "executor": {
                            "enabled": True,
                            "allowed_pipeline_roots": [str(isolated_root)],
                        },
                        "runner_provider": {"mode": "bounded"},
                    }
                }
            }
            with patch(
                "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
                wraps=create_bounded_subprocess_runner,
            ) as create_mock:
                runner = resolve_dispatch_run_subprocess_runner(
                    use_runner_provider=True,
                    use_real_bounded_runner=True,
                    harness_profile=RUNNER_PROFILE_DISPATCH,
                    node_executable=node_path,
                    merged_config=config,
                    binding_state=CooDispatchRunnerBindingState(
                        state=RUNNER_BINDING_STATE_BOUND
                    ),
                )
            _, kwargs = create_mock.call_args
            self.assertEqual(kwargs.get("profile"), RUNNER_PROFILE_DISPATCH)
            self.assertEqual(kwargs.get("node_executable"), node_path)
            exit_code, stdout, stderr = runner(
                [node_path, "pipeline.js", "--run-date", "2026-07-07"],
                str(isolated_root),
                {"PATH": os.environ.get("PATH", "")},
                30,
            )
            self.assertEqual(exit_code, 0)
            self.assertIn("fake-node-ok", stdout)

    def test_default_cli_resolution_still_restricted_profile(self) -> None:
        self.assertEqual(INJECTION_PROFILE_RESTRICTED, RUNNER_PROFILE_RESTRICTED)


if __name__ == "__main__":
    unittest.main()
