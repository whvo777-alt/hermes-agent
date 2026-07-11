"""Phase 11I tests — dispatch runner binding state model."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_runner_binding_state import (
    RUNNER_BINDING_STATE_BOUND,
    RUNNER_BINDING_STATE_STAGED,
    RUNNER_BINDING_STATE_UNBOUND,
    DispatchRunnerBindingStateError,
    default_runner_binding_state_path,
    format_runner_binding_state_summary,
    load_dispatch_runner_binding_state,
    runner_binding_state_is_bound,
)


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    (home / "coo").mkdir(parents=True)
    return home


class TestDispatchRunnerBindingState(unittest.TestCase):
    def test_missing_state_file_defaults_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                binding = load_dispatch_runner_binding_state()
        self.assertEqual(binding.state, RUNNER_BINDING_STATE_UNBOUND)
        self.assertTrue(binding.state_valid)
        self.assertFalse(runner_binding_state_is_bound(binding))
        self.assertEqual(
            format_runner_binding_state_summary(binding),
            "runner_binding_state: unbound\nrunner_bound: false",
        )

    def test_valid_bound_state_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            state_path = hermes_home / "coo" / "dispatch-runner-binding.json"
            state_path.write_text(
                json.dumps({"version": 1, "state": "bound"}),
                encoding="utf-8",
            )
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                binding = load_dispatch_runner_binding_state()
        self.assertEqual(binding.state, RUNNER_BINDING_STATE_BOUND)
        self.assertTrue(runner_binding_state_is_bound(binding))

    def test_valid_staged_state_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            state_path = hermes_home / "coo" / "dispatch-runner-binding.json"
            state_path.write_text(
                json.dumps({"version": 1, "state": "staged"}),
                encoding="utf-8",
            )
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                binding = load_dispatch_runner_binding_state()
        self.assertEqual(binding.state, RUNNER_BINDING_STATE_STAGED)
        self.assertFalse(runner_binding_state_is_bound(binding))

    def test_corrupted_json_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            state_path = hermes_home / "coo" / "dispatch-runner-binding.json"
            state_path.write_text("{not-json", encoding="utf-8")
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                with self.assertRaises(DispatchRunnerBindingStateError):
                    load_dispatch_runner_binding_state()

    def test_unknown_state_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            state_path = hermes_home / "coo" / "dispatch-runner-binding.json"
            state_path.write_text(
                json.dumps({"version": 1, "state": "connected"}),
                encoding="utf-8",
            )
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                with self.assertRaises(DispatchRunnerBindingStateError):
                    load_dispatch_runner_binding_state()

    def test_unknown_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            state_path = hermes_home / "coo" / "dispatch-runner-binding.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "state": "unbound",
                        "runner_command": "node pipeline.js",
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                with self.assertRaises(DispatchRunnerBindingStateError):
                    load_dispatch_runner_binding_state()

    def test_state_path_outside_hermes_home_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            outside = Path(tmp) / "outside-binding.json"
            outside.write_text(
                json.dumps({"version": 1, "state": "unbound"}),
                encoding="utf-8",
            )
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                with self.assertRaises(DispatchRunnerBindingStateError):
                    load_dispatch_runner_binding_state(outside)

    def test_default_state_path_under_coo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                path = default_runner_binding_state_path()
        self.assertEqual(path, hermes_home / "coo" / "dispatch-runner-binding.json")


if __name__ == "__main__":
    unittest.main()
