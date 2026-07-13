"""Phase 14H-1 tests — controlled production activation active transition."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_production_activation import build_production_activation_proposal
from agent.coo.production_activation_active import (
    ACTION_ACTIVATION_ALREADY_ACTIVE,
    ACTION_ACTIVE_STATE_READY_WAIT_FOR_EXECUTION_GATE,
    ProductionActivationActiveError,
    activate_production_activation,
    run_activation_activate,
    run_activation_active_status,
)
from agent.coo.production_activation_arm import (
    CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
    arm_production_activation,
)
from agent.coo.production_activation_approval import (
    record_release_approver_approval,
    record_security_reviewer_approval,
)
from agent.coo.production_activation_dry_run import run_production_activation_dry_run
from agent.coo.production_activation_kill_switch import suspend_production_activation
from agent.coo.production_activation_state import (
    ACTIVATION_PLATFORM_CLI,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_ARMED,
    ACTIVATION_STATE_SUSPENDED,
)
from agent.coo.production_activation_store import (
    append_activation_proposal,
    load_activation_request,
)
from agent.coo.production_executor_confirmation import (
    REQUIRED_CONFIRMATION_PHRASE,
    create_production_executor_confirmation,
    write_confirmation,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser

_TESTED_SHA = "ca269dab24ffceb43ddfeb44c76a5120f987dc46"
_ROLLBACK_SHA = "18a03673739262534847af0296458239511bb7e6"
_ATTESTATION_HASH = "a" * 64
_EXECUTOR_ID = "executor-e"
_TICKET_ID = "ticket-active-1"

_FORBIDDEN_OUTPUT_TOKENS = (
    "pipeline_root",
    "confirmation_phrase",
    "unlock_token",
    "/opt/data/",
    "pipeline.js",
    "argv",
    "cwd",
    "stdout",
    "stderr",
    "secret",
    "rollback_commit",
    "repository_attestation_hash",
    "executor-e",
    "operator-a",
    "confirm-production-activation",
    "confirm-repository2-execution",
    "dry_run_key",
    "active_actor_id",
)


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    (home / "coo" / "production-activation").mkdir(parents=True)
    (home / "coo" / "production-activation-dry-run").mkdir(parents=True)
    (home / "coo" / "confirmations").mkdir(parents=True)
    return home


def _enabled_executor_config(pipeline_root: Path) -> dict:
    return {
        "coo": {
            "dispatch": {
                "executor": {
                    "enabled": True,
                    "allowed_pipeline_roots": [str(pipeline_root.resolve())],
                }
            }
        }
    }


def _init_git_repo(repo_root: Path, commit_sha: str = _TESTED_SHA) -> None:
    git_dir = repo_root / ".git"
    refs_dir = git_dir / "refs" / "heads"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text(f"{commit_sha}\n", encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def _artifact_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _gate_patch_context():
    return patch.multiple(
        "agent.coo.production_activation_active_gate",
        _probe_signoff_ready=lambda **_: True,
        _probe_cutover_ready=lambda **_: True,
        _probe_regression_clear=lambda: True,
        _probe_recovery_required=lambda request: False,
        _probe_repair_lock_held=lambda request: False,
    )


class TestProductionActivationActive(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.hermes_home = _hermes_home(self.tmp_path)
        self.repo_root = self.tmp_path / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)
        self.mirror_root = self.tmp_path / "isolated-mirror"
        self.mirror_root.mkdir()
        self.store_dir = self.hermes_home / "coo" / "production-activation"
        self.history_dir = self.hermes_home / "coo" / "production-activation-dry-run"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.merged_config = _enabled_executor_config(self.mirror_root)
        self.env_patch = patch.dict(
            "os.environ",
            {"HERMES_HOME": str(self.hermes_home)},
        )
        self.env_patch.start()
        self._now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.env_patch.stop()
        self._tmp.cleanup()

    def _propose(self) -> str:
        with patch(
            "agent.coo.dispatch_cli_production_activation.resolve_git_head_commit",
            return_value=_TESTED_SHA,
        ):
            request = build_production_activation_proposal(
                tested_commit_sha=_TESTED_SHA,
                release_tag="v1.0.0-rc.1",
                repository_attestation_hash=_ATTESTATION_HASH,
                requested_by="operator-a",
                rollback_commit=_ROLLBACK_SHA,
                scope_type=ACTIVATION_SCOPE_ONE_SHOT,
                platform=ACTIVATION_PLATFORM_CLI,
                repo_root=self.repo_root,
                now=self._now,
            )
            append_activation_proposal(request, store_dir=self.store_dir)
        return request.activation_request_id

    def _approve(self, activation_id: str) -> None:
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=1),
        )
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-c",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=2),
        )
        record_security_reviewer_approval(
            activation_request_id=activation_id,
            reviewer_id="reviewer-d",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=3),
        )

    def _armed_id(self) -> str:
        activation_id = self._propose()
        self._approve(activation_id)
        arm_production_activation(
            activation_request_id=activation_id,
            executor_id=_EXECUTOR_ID,
            phrase=CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
            store_dir=self.store_dir,
            repo_root=self.repo_root,
            now=self._now + timedelta(minutes=4),
        )
        return activation_id

    def _write_confirmation(self) -> str:
        confirmation = create_production_executor_confirmation(
            ticket_id=_TICKET_ID,
            plan_id="plan-1",
            unlock_token_id="token-1",
            dispatch_request_id="req-1",
            operator_id="op-active",
            operator_name="Active Operator",
            confirmation_reason="active test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            attested_pipeline_root=str(self.mirror_root.resolve()),
        )
        from dataclasses import replace

        confirmation = replace(confirmation, confirmation_id="conf-active-1")
        write_confirmation(confirmation, confirmation_dir=self.confirmation_dir)
        return confirmation.confirmation_id

    def _ready_dry_run(self, activation_id: str) -> None:
        confirmation_id = self._write_confirmation()
        with _gate_patch_context():
            run_production_activation_dry_run(
                activation_request_id=activation_id,
                ticket_id=_TICKET_ID,
                confirmation_id=confirmation_id,
                pipeline_root=str(self.mirror_root),
                repo_root=self.repo_root,
                store_dir=self.store_dir,
                history_dir=self.history_dir,
                confirmation_dir=self.confirmation_dir,
                merged_config=self.merged_config,
                now=self._now + timedelta(minutes=5),
            )

    def _activate_kwargs(self, activation_id: str, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "activation_request_id": activation_id,
            "actor_id": _EXECUTOR_ID,
            "actor_role": "production_executor",
            "phrase": CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
            "repo_root": self.repo_root,
            "store_dir": self.store_dir,
            "history_dir": self.history_dir,
            "merged_config": self.merged_config,
            "now": self._now + timedelta(minutes=6),
        }
        base.update(overrides)
        return base

    def _artifact_path(self, activation_id: str) -> Path:
        return self.store_dir / f"{activation_id}.json"

    def test_armed_gate_dry_run_ready_valid_actor_phrase_becomes_active(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            status = activate_production_activation(**self._activate_kwargs(activation_id))
        self.assertTrue(status.active)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_ACTIVE)
        self.assertTrue(loaded.dry_run_event_id)
        self.assertTrue(loaded.dry_run_key)

    def test_state_history_armed_to_active_once(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            activate_production_activation(**self._activate_kwargs(activation_id))
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        armed_to_active = [
            item
            for item in loaded.state_history
            if item.from_state == ACTIVATION_STATE_ARMED
            and item.to_state == ACTIVATION_STATE_ACTIVE
        ]
        self.assertEqual(len(armed_to_active), 1)

    def test_control_history_append_only(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        before = load_activation_request(activation_id, store_dir=self.store_dir)
        with _gate_patch_context():
            activate_production_activation(**self._activate_kwargs(activation_id))
        after = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertGreater(len(after.control_history), len(before.control_history))
        event_types = [item.event_type for item in after.control_history]
        self.assertIn("active_transition_evaluated", event_types)
        self.assertIn("active_entered", event_types)

    def test_active_expires_at_set(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        activate_at = self._now + timedelta(minutes=6)
        with _gate_patch_context():
            activate_production_activation(
                **self._activate_kwargs(activation_id, now=activate_at)
            )
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertTrue(loaded.active_expires_at)
        active_expires = datetime.fromisoformat(
            loaded.active_expires_at.replace("Z", "+00:00")
        )
        expected = activate_at + timedelta(minutes=60)
        self.assertEqual(active_expires, expected)

    def test_production_execution_disallowed_and_repository2_not_attempted(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            output, exit_code = run_activation_activate(**self._activate_kwargs(activation_id))
        self.assertEqual(exit_code, 0)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("repository2_execution_attempted: false", output)

    def test_active_does_not_call_execution(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context(), patch(
            "agent.coo.dispatch_cli_run.execute_coo_dispatch_run",
            side_effect=AssertionError("execution must not run"),
        ):
            activate_production_activation(**self._activate_kwargs(activation_id))

    def test_dry_run_record_missing_blocked(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context(), self.assertRaises(ProductionActivationActiveError):
            activate_production_activation(**self._activate_kwargs(activation_id))

    def test_dry_run_not_ready_blocked(self) -> None:
        activation_id = self._armed_id()
        confirmation_id = self._write_confirmation()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_regression_clear",
            return_value=False,
        ):
            run_production_activation_dry_run(
                activation_request_id=activation_id,
                ticket_id=_TICKET_ID,
                confirmation_id=confirmation_id,
                pipeline_root=str(self.mirror_root),
                repo_root=self.repo_root,
                store_dir=self.store_dir,
                history_dir=self.history_dir,
                confirmation_dir=self.confirmation_dir,
                merged_config=self.merged_config,
                now=self._now + timedelta(minutes=5),
            )
        with _gate_patch_context(), self.assertRaises(ProductionActivationActiveError):
            activate_production_activation(**self._activate_kwargs(activation_id))

    def test_dry_run_sha_mismatch_blocked(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        path = self.history_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["records"][-1]["tested_commit_sha"] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with _gate_patch_context(), self.assertRaises(ProductionActivationActiveError):
            activate_production_activation(**self._activate_kwargs(activation_id))

    def test_dry_run_tag_mismatch_blocked(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        path = self.history_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["records"][-1]["release_tag"] = "v9.9.9"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with _gate_patch_context(), self.assertRaises(ProductionActivationActiveError):
            activate_production_activation(**self._activate_kwargs(activation_id))

    def test_armed_ttl_expired_blocked(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        expired_now = datetime.fromisoformat(
            loaded.armed_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        with _gate_patch_context(), self.assertRaises(ProductionActivationActiveError):
            activate_production_activation(
                **self._activate_kwargs(activation_id, now=expired_now)
            )

    def test_gate_false_blocked(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_signoff_ready",
            return_value=False,
        ), self.assertRaises(ProductionActivationActiveError):
            activate_production_activation(**self._activate_kwargs(activation_id))

    def test_wrong_executor_blocked(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context(), self.assertRaises(ProductionActivationActiveError):
            activate_production_activation(
                **self._activate_kwargs(activation_id, actor_id="executor-other")
            )

    def test_wrong_role_blocked(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context(), self.assertRaises(ProductionActivationActiveError):
            activate_production_activation(
                **self._activate_kwargs(activation_id, actor_role="operator")
            )

    def test_wrong_phrase_zero_mutation(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        with _gate_patch_context(), self.assertRaises(ProductionActivationActiveError):
            activate_production_activation(
                **self._activate_kwargs(activation_id, phrase="WRONG-PHRASE")
            )
        self.assertEqual(_artifact_digest(path), digest_before)

    def test_duplicate_same_actor_idempotent(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            activate_production_activation(**self._activate_kwargs(activation_id))
            path = self._artifact_path(activation_id)
            digest_before = _artifact_digest(path)
            status = activate_production_activation(**self._activate_kwargs(activation_id))
        self.assertTrue(status.already_active)
        self.assertEqual(
            status.recommended_action,
            ACTION_ACTIVATION_ALREADY_ACTIVE,
        )
        self.assertEqual(_artifact_digest(path), digest_before)

    def test_different_actor_conflict(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            activate_production_activation(**self._activate_kwargs(activation_id))
            with self.assertRaises(ProductionActivationActiveError):
                activate_production_activation(
                    **self._activate_kwargs(activation_id, actor_id="executor-other")
                )

    def test_suspended_activate_blocked(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            suspend_production_activation(
                activation_request_id=activation_id,
                actor_id="operator-z",
                actor_role="operator",
                reason_code="manual_suspend",
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=5),
            )
            with self.assertRaises(ProductionActivationActiveError):
                activate_production_activation(**self._activate_kwargs(activation_id))

    def test_active_suspend_regression(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            activate_production_activation(**self._activate_kwargs(activation_id))
            suspend_production_activation(
                activation_request_id=activation_id,
                actor_id="operator-z",
                actor_role="operator",
                reason_code="manual_suspend",
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=7),
            )
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_SUSPENDED)

    def test_phrase_not_stored_in_artifact(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            activate_production_activation(**self._activate_kwargs(activation_id))
        payload = json.loads(self._artifact_path(activation_id).read_text(encoding="utf-8"))
        encoded = json.dumps(payload).lower()
        self.assertNotIn("confirm-production-activation", encoded)
        self.assertNotIn("confirmation_phrase", encoded)

    def test_safe_output(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            output, _ = run_activation_activate(**self._activate_kwargs(activation_id))
        sanitized = output
        for allowed in (
            "repository2_execution_attempted: false",
            "production_execution_allowed: false",
            "dry_run_verified:",
            "active_gate_ready:",
            "executor_assigned:",
        ):
            sanitized = sanitized.replace(allowed, "")
        lowered = sanitized.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_atomic_write_failure_preserves_artifact(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_store._atomic_replace_json",
            side_effect=OSError("write failed"),
        ), self.assertRaises(ProductionActivationActiveError):
            activate_production_activation(**self._activate_kwargs(activation_id))
        self.assertEqual(_artifact_digest(path), digest_before)

    def test_no_subprocess_or_bounded_runner(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("no subprocess"),
        ), patch.object(
            subprocess,
            "Popen",
            side_effect=AssertionError("no subprocess"),
        ), patch(
            "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
            side_effect=AssertionError("no bounded runner"),
        ), _gate_patch_context():
            run_activation_activate(**self._activate_kwargs(activation_id))

    def test_active_status_cli(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            activate_production_activation(**self._activate_kwargs(activation_id))
            output, exit_code = run_activation_active_status(
                activation_request_id=activation_id,
                store_dir=self.store_dir,
                history_dir=self.history_dir,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=7),
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("active: true", output)

    def test_cli_parser_activate_and_active_status(self) -> None:
        parser = build_coo_dispatch_parser()
        activate_args = parser.parse_args(
            [
                "production",
                "activation",
                "activate",
                "--activation-request-id",
                "req-1",
                "--actor-id",
                _EXECUTOR_ID,
                "--actor-role",
                "production_executor",
                "--phrase",
                CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
            ]
        )
        self.assertEqual(activate_args.coo_dispatch_production_activation_command, "activate")
        status_args = parser.parse_args(
            [
                "production",
                "activation",
                "active-status",
                "--activation-request-id",
                "req-1",
            ]
        )
        self.assertEqual(
            status_args.coo_dispatch_production_activation_command,
            "active-status",
        )

    def test_success_recommended_action(self) -> None:
        activation_id = self._armed_id()
        self._ready_dry_run(activation_id)
        with _gate_patch_context():
            status = activate_production_activation(**self._activate_kwargs(activation_id))
        self.assertEqual(
            status.recommended_action,
            ACTION_ACTIVE_STATE_READY_WAIT_FOR_EXECUTION_GATE,
        )


if __name__ == "__main__":
    unittest.main()
