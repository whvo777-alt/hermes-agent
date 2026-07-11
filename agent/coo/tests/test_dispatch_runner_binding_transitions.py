"""Phase 11J tests — dispatch runner binding transitions."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_runner_binding_state import (
    RUNNER_BINDING_STATE_BOUND,
    RUNNER_BINDING_STATE_STAGED,
    RUNNER_BINDING_STATE_UNBOUND,
    DispatchRunnerBindingStateError,
    DispatchRunnerBindingTransitionError,
    _KNOWN_STATE_FILE_KEYS,
    default_runner_binding_state_path,
    load_dispatch_runner_binding_state,
    reset_dispatch_runner_binding,
    stage_dispatch_runner_binding,
)


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    (home / "coo").mkdir(parents=True)
    return home


class TestDispatchRunnerBindingTransitions(unittest.TestCase):
    def test_stage_from_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                binding = stage_dispatch_runner_binding(
                    operator_id="op-1",
                    reason="prepare binding",
                )
                payload = json.loads(
                    (hermes_home / "coo" / "dispatch-runner-binding.json").read_text(
                        encoding="utf-8"
                    )
                )
        self.assertEqual(binding.state, RUNNER_BINDING_STATE_STAGED)
        self.assertEqual(set(payload), _KNOWN_STATE_FILE_KEYS)
        self.assertNotIn("runner_command", payload)
        self.assertNotIn("token", payload)

    def test_reset_from_staged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                stage_dispatch_runner_binding(operator_id="op-1", reason="stage")
                binding = reset_dispatch_runner_binding(
                    operator_id="op-2",
                    reason="abort staging",
                )
                payload = json.loads(
                    (hermes_home / "coo" / "dispatch-runner-binding.json").read_text(
                        encoding="utf-8"
                    )
                )
        self.assertEqual(binding.state, RUNNER_BINDING_STATE_UNBOUND)
        self.assertEqual(payload["state"], RUNNER_BINDING_STATE_UNBOUND)
        self.assertEqual(payload["operator_id"], "op-2")

    def test_stage_idempotent_when_already_staged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                first = stage_dispatch_runner_binding(operator_id="op-1", reason="stage")
                path = hermes_home / "coo" / "dispatch-runner-binding.json"
                before_mtime = path.stat().st_mtime_ns
                second = stage_dispatch_runner_binding(operator_id="op-2", reason="again")
                after_mtime = path.stat().st_mtime_ns
        self.assertEqual(first.state, RUNNER_BINDING_STATE_STAGED)
        self.assertEqual(second.state, RUNNER_BINDING_STATE_STAGED)
        self.assertEqual(second.updated_at, first.updated_at)
        self.assertEqual(after_mtime, before_mtime)

    def test_reset_idempotent_when_already_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                binding = reset_dispatch_runner_binding(
                    operator_id="op-1",
                    reason="noop",
                )
                path = default_runner_binding_state_path()
        self.assertEqual(binding.state, RUNNER_BINDING_STATE_UNBOUND)
        self.assertFalse(path.exists())

    def test_bound_state_rejects_stage_and_reset(self) -> None:
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
                        "reason": "bound elsewhere",
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                with self.assertRaises(DispatchRunnerBindingTransitionError):
                    stage_dispatch_runner_binding(operator_id="op-1", reason="stage")
                with self.assertRaises(DispatchRunnerBindingTransitionError):
                    reset_dispatch_runner_binding(operator_id="op-1", reason="reset")

    def test_empty_operator_id_or_reason_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                with self.assertRaises(ValueError):
                    stage_dispatch_runner_binding(operator_id="", reason="ok")
                with self.assertRaises(ValueError):
                    reset_dispatch_runner_binding(operator_id="op-1", reason="  ")

    def test_atomic_write_uses_temp_file_and_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                with patch(
                    "agent.coo.dispatch_runner_binding_state.os.replace",
                    wraps=os.replace,
                ) as replace_mock:
                    stage_dispatch_runner_binding(operator_id="op-1", reason="stage")
        self.assertEqual(replace_mock.call_count, 1)

    def test_symlink_escape_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = _hermes_home(Path(tmp))
            outside = Path(tmp) / "outside-binding.json"
            outside.write_text("{}", encoding="utf-8")
            with patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=hermes_home,
            ):
                with self.assertRaises(DispatchRunnerBindingStateError):
                    stage_dispatch_runner_binding(
                        operator_id="op-1",
                        reason="stage",
                        state_path=outside,
                    )
                self.assertEqual(outside.read_text(encoding="utf-8"), "{}")

    def test_legacy_version_state_only_payload_still_loads(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
