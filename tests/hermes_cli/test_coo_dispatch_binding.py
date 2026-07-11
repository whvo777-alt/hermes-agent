"""Phase 11J tests — dispatch runner binding CLI."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_cli.coo_dispatch import build_coo_dispatch_parser, main


def _enabled_config(pipeline_root: str) -> dict:
    return {
        "coo": {
            "dispatch": {
                "executor": {
                    "enabled": True,
                    "allowed_pipeline_roots": [pipeline_root],
                }
            }
        }
    }


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    (home / "coo").mkdir(parents=True)
    return home


class TestCooDispatchBindingCli(unittest.TestCase):
    def test_binding_status_missing_file_reports_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                stdout = io.StringIO()
                with patch.object(sys, "stdout", stdout):
                    exit_code = main(["binding", "status"])
        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("runner_binding_state: unbound", output)
        self.assertIn("runner_bound: false", output)
        self.assertNotIn("/opt/data/multi-content-pipeline", output)

    def test_binding_stage_and_reset_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                with (
                    patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
                    patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
                ):
                    stage_stdout = io.StringIO()
                    with patch.object(sys, "stdout", stage_stdout):
                        stage_exit = main(
                            [
                                "binding",
                                "stage",
                                "--operator-id",
                                "op-1",
                                "--reason",
                                "prepare",
                            ]
                        )
                    reset_stdout = io.StringIO()
                    with patch.object(sys, "stdout", reset_stdout):
                        reset_exit = main(
                            [
                                "binding",
                                "reset",
                                "--operator-id",
                                "op-2",
                                "--reason",
                                "abort",
                            ]
                        )
                    payload = json.loads(
                        (hermes_home / "coo" / "dispatch-runner-binding.json").read_text(
                            encoding="utf-8"
                        )
                    )
        self.assertEqual(stage_exit, 0)
        self.assertEqual(reset_exit, 0)
        self.assertIn("transition: unbound_to_staged", stage_stdout.getvalue())
        self.assertIn("transition: staged_to_unbound", reset_stdout.getvalue())
        self.assertEqual(set(payload.keys()), {"version", "state", "updated_at", "operator_id", "reason"})
        self.assertNotIn("runner_command", payload)
        self.assertNotIn("secret", payload)

    def test_binding_stage_rejects_bound_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            state_path = hermes_home / "coo" / "dispatch-runner-binding.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "state": "bound",
                        "updated_at": "2026-07-11T00:00:00+00:00",
                        "operator_id": "op-bound",
                        "reason": "bound",
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                stderr = io.StringIO()
                with patch.object(sys, "stderr", stderr):
                    exit_code = main(
                        [
                            "binding",
                            "stage",
                            "--operator-id",
                            "op-1",
                            "--reason",
                            "stage",
                        ]
                    )
        self.assertEqual(exit_code, 1)
        self.assertIn("bound", stderr.getvalue().lower())

    def test_binding_stage_rejects_empty_operator_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                stderr = io.StringIO()
                with patch.object(sys, "stderr", stderr):
                    exit_code = main(
                        [
                            "binding",
                            "stage",
                            "--operator-id",
                            " ",
                            "--reason",
                            "stage",
                        ]
                    )
        self.assertEqual(exit_code, 1)
        self.assertIn("operator_id", stderr.getvalue())

    def test_binding_bind_from_staged_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            with (
                patch(
                    "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                    return_value=hermes_home,
                ),
                patch(
                    "hermes_cli.config.load_config",
                    return_value=_enabled_config(isolated_root),
                ),
                patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
                patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
                patch(
                    "agent.coo.dispatch_cli_run.build_pipeline_dispatch_executor",
                    side_effect=AssertionError("no factory"),
                ),
                patch(
                    "agent.coo.dispatch_cli_run.run_approved_dispatch",
                    side_effect=AssertionError("no runner"),
                ),
            ):
                stage_exit = main(
                    [
                        "binding",
                        "stage",
                        "--operator-id",
                        "op-1",
                        "--reason",
                        "prepare",
                    ]
                )
                bind_stdout = io.StringIO()
                with patch.object(sys, "stdout", bind_stdout):
                    bind_exit = main(
                        [
                            "binding",
                            "bind",
                            "--operator-id",
                            "op-bind",
                            "--reason",
                            "record bound",
                        ]
                    )
                payload = json.loads(
                    (hermes_home / "coo" / "dispatch-runner-binding.json").read_text(
                        encoding="utf-8"
                    )
                )
        self.assertEqual(stage_exit, 0)
        self.assertEqual(bind_exit, 0)
        self.assertIn("transition: staged_to_bound", bind_stdout.getvalue())
        self.assertEqual(payload["state"], "bound")
        self.assertNotIn("runner_command", payload)
        self.assertNotIn(isolated_root, bind_stdout.getvalue())

    def test_binding_bind_rejects_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            isolated_root = str(Path(tmp) / "fake-pipeline")
            Path(isolated_root).mkdir()
            with (
                patch(
                    "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                    return_value=hermes_home,
                ),
                patch(
                    "hermes_cli.config.load_config",
                    return_value=_enabled_config(isolated_root),
                ),
            ):
                stderr = io.StringIO()
                with patch.object(sys, "stderr", stderr):
                    exit_code = main(
                        [
                            "binding",
                            "bind",
                            "--operator-id",
                            "op-1",
                            "--reason",
                            "bind",
                        ]
                    )
        self.assertEqual(exit_code, 1)
        self.assertIn("staged", stderr.getvalue().lower())

    def test_binding_bind_rejects_empty_operator_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                stderr = io.StringIO()
                with patch.object(sys, "stderr", stderr):
                    exit_code = main(
                        [
                            "binding",
                            "bind",
                            "--operator-id",
                            " ",
                            "--reason",
                            "bind",
                        ]
                    )
        self.assertEqual(exit_code, 1)
        self.assertIn("operator_id", stderr.getvalue())

    def test_binding_parsers_registered(self) -> None:
        parser = build_coo_dispatch_parser()
        for args in (
            ["binding", "status"],
            ["binding", "stage", "--operator-id", "op", "--reason", "why"],
            ["binding", "reset", "--operator-id", "op", "--reason", "why"],
            ["binding", "bind", "--operator-id", "op", "--reason", "why"],
        ):
            parsed = parser.parse_args(args)
            self.assertEqual(parsed.coo_dispatch_command, "binding")


if __name__ == "__main__":
    unittest.main()
