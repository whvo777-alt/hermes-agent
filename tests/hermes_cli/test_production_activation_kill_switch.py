"""Phase 14F tests — active gate, kill switch, suspend/revoke."""

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
from agent.coo.production_activation_active_gate import (
    BLOCK_ARMED_TTL_EXPIRED,
    BLOCK_ATTESTATION_INVALID,
    BLOCK_CUTOVER_NOT_READY,
    BLOCK_EXECUTOR_INVALID,
    BLOCK_HEAD_SHA_MISMATCH,
    BLOCK_KILL_SWITCH_UNAVAILABLE,
    BLOCK_PHRASE_NOT_VERIFIED,
    BLOCK_QUORUM_INVALID,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_REGRESSION_FAIL,
    BLOCK_REPAIR_LOCK_HELD,
    BLOCK_ROLLBACK_MISSING,
    BLOCK_SIGNOFF_NOT_READY,
    BLOCK_WRONG_STATE,
    ProductionActivationActiveGateError,
    evaluate_active_gate,
    evaluate_and_record_active_gate,
    run_activation_gate,
)
from agent.coo.production_activation_approval import (
    record_release_approver_approval,
    record_security_reviewer_approval,
)
from agent.coo.production_activation_arm import (
    CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
    arm_production_activation,
    maybe_expire_armed_activation,
)
from agent.coo.production_activation_kill_switch import (
    ProductionActivationKillSwitchError,
    revoke_production_activation,
    run_activation_revoke,
    run_activation_suspend,
    suspend_production_activation,
)
from agent.coo.production_activation_state import (
    ACTIVATION_PLATFORM_CLI,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_STATE_ARMED,
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
)
from agent.coo.production_activation_store import (
    append_activation_proposal,
    load_activation_request,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser

_TESTED_SHA = "ca269dab24ffceb43ddfeb44c76a5120f987dc46"
_ROLLBACK_SHA = "18a03673739262534847af0296458239511bb7e6"
_ATTESTATION_HASH = "a" * 64
_EXECUTOR_ID = "executor-e"

_GATE_PATCHES = {
    "agent.coo.production_activation_active_gate._probe_signoff_ready": True,
    "agent.coo.production_activation_active_gate._probe_cutover_ready": True,
    "agent.coo.production_activation_active_gate._probe_regression_clear": True,
    "agent.coo.production_activation_active_gate._probe_recovery_required": False,
    "agent.coo.production_activation_active_gate._probe_repair_lock_held": False,
}

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
)


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    (home / "coo" / "production-activation").mkdir(parents=True)
    return home


def _activation_store_dir(hermes_home: Path) -> Path:
    return hermes_home / "coo" / "production-activation"


def _init_git_repo(repo_root: Path, commit_sha: str = _TESTED_SHA) -> None:
    git_dir = repo_root / ".git"
    refs_dir = git_dir / "refs" / "heads"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text(f"{commit_sha}\n", encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def _proposal_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tested_commit_sha": _TESTED_SHA,
        "release_tag": "v1.0.0-rc.1",
        "repository_attestation_hash": _ATTESTATION_HASH,
        "requested_by": "operator-a",
        "rollback_commit": _ROLLBACK_SHA,
        "scope_type": ACTIVATION_SCOPE_ONE_SHOT,
        "platform": ACTIVATION_PLATFORM_CLI,
    }
    base.update(overrides)
    return base


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


class TestProductionActivationKillSwitch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.hermes_home = _hermes_home(self.tmp_path)
        self.repo_root = self.tmp_path / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)
        self.store_dir = _activation_store_dir(self.hermes_home)
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
                **_proposal_kwargs(),
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

    def _artifact_path(self, activation_id: str) -> Path:
        return self.store_dir / f"{activation_id}.json"

    def test_gate_ready_when_all_conditions_met(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context():
            assessment = evaluate_active_gate(
                load_activation_request(activation_id, store_dir=self.store_dir),
                repo_root=self.repo_root,
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=5),
            )
        self.assertTrue(assessment.gate_ready)
        self.assertEqual(assessment.current_state, ACTIVATION_STATE_ARMED)
        self.assertFalse(assessment.production_execution_allowed)

    def test_gate_ready_does_not_transition_to_active(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context():
            evaluate_and_record_active_gate(
                activation_request_id=activation_id,
                store_dir=self.store_dir,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_ARMED)

    def test_gate_ready_keeps_production_execution_disallowed(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context():
            output, exit_code = run_activation_gate(
                activation_request_id=activation_id,
                store_dir=self.store_dir,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("production_execution_allowed: false", output)

    def test_armed_ttl_expired_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        expired_now = datetime.fromisoformat(
            loaded.armed_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        with _gate_patch_context():
            assessment = evaluate_active_gate(
                loaded,
                repo_root=self.repo_root,
                now=expired_now,
            )
        self.assertFalse(assessment.gate_ready)
        self.assertIn(BLOCK_ARMED_TTL_EXPIRED, assessment.blocking_reasons)

    def test_quorum_invalid_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        from agent.coo.production_activation_state import ActivationRequest

        broken = ActivationRequest(
            **{
                **loaded.__dict__,
                "approved_by": ("approver-b",),
            }
        )
        with _gate_patch_context():
            assessment = evaluate_active_gate(
                broken,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_QUORUM_INVALID, assessment.blocking_reasons)

    def test_executor_invalid_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        from agent.coo.production_activation_state import ActivationRequest

        broken = ActivationRequest(**{**loaded.__dict__, "executor_id": ""})
        with _gate_patch_context():
            assessment = evaluate_active_gate(
                broken,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_EXECUTOR_INVALID, assessment.blocking_reasons)

    def test_phrase_not_verified_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        from agent.coo.production_activation_state import ActivationRequest

        broken = ActivationRequest(**{**loaded.__dict__, "phrase_verified": False})
        with _gate_patch_context():
            assessment = evaluate_active_gate(
                broken,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_PHRASE_NOT_VERIFIED, assessment.blocking_reasons)

    def test_head_mismatch_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate.resolve_git_head_commit",
            return_value="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        ):
            assessment = evaluate_active_gate(
                loaded,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_HEAD_SHA_MISMATCH, assessment.blocking_reasons)

    def test_recovery_required_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_recovery_required",
            return_value=True,
        ):
            assessment = evaluate_active_gate(
                loaded,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, assessment.blocking_reasons)

    def test_repair_lock_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_repair_lock_held",
            return_value=True,
        ):
            assessment = evaluate_active_gate(
                loaded,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_REPAIR_LOCK_HELD, assessment.blocking_reasons)

    def test_regression_fail_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_regression_clear",
            return_value=False,
        ):
            assessment = evaluate_active_gate(
                loaded,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_REGRESSION_FAIL, assessment.blocking_reasons)

    def test_signoff_not_ready_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_signoff_ready",
            return_value=False,
        ):
            assessment = evaluate_active_gate(
                loaded,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_SIGNOFF_NOT_READY, assessment.blocking_reasons)

    def test_cutover_not_ready_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_cutover_ready",
            return_value=False,
        ):
            assessment = evaluate_active_gate(
                loaded,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_CUTOVER_NOT_READY, assessment.blocking_reasons)

    def test_kill_switch_unavailable_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate.is_kill_switch_available",
            return_value=False,
        ):
            assessment = evaluate_active_gate(
                loaded,
                repo_root=self.repo_root,
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_KILL_SWITCH_UNAVAILABLE, assessment.blocking_reasons)

    def test_wrong_state_gate_false(self) -> None:
        activation_id = self._propose()
        self._approve(activation_id)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        with _gate_patch_context():
            assessment = evaluate_active_gate(
                loaded,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_WRONG_STATE, assessment.blocking_reasons)

    def test_armed_to_suspend_to_revoked(self) -> None:
        activation_id = self._armed_id()
        suspend_production_activation(
            activation_request_id=activation_id,
            actor_id="operator-z",
            actor_role="operator",
            reason_code="manual_suspend",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=5),
        )
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_SUSPENDED)
        revoke_production_activation(
            activation_request_id=activation_id,
            actor_id="operator-z",
            actor_role="incident_commander",
            reason_code="suspended_revoked",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=6),
        )
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_REVOKED)

    def test_duplicate_suspend_idempotent(self) -> None:
        activation_id = self._armed_id()
        suspend_production_activation(
            activation_request_id=activation_id,
            actor_id="operator-z",
            actor_role="operator",
            reason_code="manual_suspend",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=5),
        )
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        status = suspend_production_activation(
            activation_request_id=activation_id,
            actor_id="operator-z",
            actor_role="operator",
            reason_code="manual_suspend",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=6),
        )
        self.assertEqual(status.recommended_action, "already_suspended")
        self.assertEqual(_artifact_digest(path), digest_before)

    def test_duplicate_revoke_idempotent(self) -> None:
        activation_id = self._armed_id()
        suspend_production_activation(
            activation_request_id=activation_id,
            actor_id="operator-z",
            actor_role="operator",
            reason_code="manual_suspend",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=5),
        )
        revoke_production_activation(
            activation_request_id=activation_id,
            actor_id="operator-z",
            actor_role="operator",
            reason_code="suspended_revoked",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=6),
        )
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        status = revoke_production_activation(
            activation_request_id=activation_id,
            actor_id="operator-z",
            actor_role="operator",
            reason_code="suspended_revoked",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=7),
        )
        self.assertEqual(status.recommended_action, "already_revoked")
        self.assertEqual(_artifact_digest(path), digest_before)

    def test_invalid_actor_role_rejected(self) -> None:
        activation_id = self._armed_id()
        with self.assertRaises(ProductionActivationKillSwitchError):
            suspend_production_activation(
                activation_request_id=activation_id,
                actor_id="executor-e",
                actor_role="production_executor",
                reason_code="manual_suspend",
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=5),
            )

    def test_invalid_reason_code_rejected(self) -> None:
        activation_id = self._armed_id()
        with self.assertRaises(ProductionActivationKillSwitchError):
            suspend_production_activation(
                activation_request_id=activation_id,
                actor_id="operator-z",
                actor_role="operator",
                reason_code="not-a-real-reason",
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=5),
            )

    def test_control_history_append_only(self) -> None:
        activation_id = self._armed_id()
        before = load_activation_request(activation_id, store_dir=self.store_dir)
        with _gate_patch_context():
            evaluate_and_record_active_gate(
                activation_request_id=activation_id,
                store_dir=self.store_dir,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        after = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertGreater(len(after.control_history), len(before.control_history))
        self.assertEqual(
            after.control_history[: len(before.control_history)],
            before.control_history,
        )

    def test_state_history_append_only_on_suspend(self) -> None:
        activation_id = self._armed_id()
        before = load_activation_request(activation_id, store_dir=self.store_dir)
        suspend_production_activation(
            activation_request_id=activation_id,
            actor_id="operator-z",
            actor_role="operator",
            reason_code="manual_suspend",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=5),
        )
        after = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(after.state_history[0], before.state_history[0])

    def test_atomic_write_failure_preserves_artifact(self) -> None:
        activation_id = self._armed_id()
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        with patch(
            "agent.coo.production_activation_store._atomic_replace_json",
            side_effect=OSError("write failed"),
        ):
            with self.assertRaises(ProductionActivationKillSwitchError):
                suspend_production_activation(
                    activation_request_id=activation_id,
                    actor_id="operator-z",
                    actor_role="operator",
                    reason_code="manual_suspend",
                    store_dir=self.store_dir,
                    now=self._now + timedelta(minutes=5),
                )
        self.assertEqual(_artifact_digest(path), digest_before)

    def test_safe_output(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context():
            output, _ = run_activation_gate(
                activation_request_id=activation_id,
                store_dir=self.store_dir,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        lowered = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_no_active_transition_cli(self) -> None:
        parser = build_coo_dispatch_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "production",
                    "activation",
                    "active",
                    "--activation-request-id",
                    "req-1",
                ]
            )

    def test_expired_armed_not_active(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        expired_now = datetime.fromisoformat(
            loaded.armed_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        revoked, _ = maybe_expire_armed_activation(
            loaded,
            persist=True,
            store_dir=self.store_dir,
            now=expired_now,
        )
        self.assertEqual(revoked.state, ACTIVATION_STATE_REVOKED)

    def test_no_subprocess(self) -> None:
        activation_id = self._armed_id()
        with patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("no subprocess"),
        ), patch.object(
            subprocess,
            "Popen",
            side_effect=AssertionError("no subprocess"),
        ), _gate_patch_context():
            run_activation_gate(
                activation_request_id=activation_id,
                store_dir=self.store_dir,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
            run_activation_suspend(
                activation_request_id=activation_id,
                actor_id="operator-z",
                actor_role="operator",
                reason_code="manual_suspend",
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=5),
            )

    def test_cli_parser_gate_suspend_revoke(self) -> None:
        parser = build_coo_dispatch_parser()
        gate_args = parser.parse_args(
            [
                "production",
                "activation",
                "gate",
                "--activation-request-id",
                "req-1",
            ]
        )
        self.assertEqual(gate_args.coo_dispatch_production_activation_command, "gate")
        suspend_args = parser.parse_args(
            [
                "production",
                "activation",
                "suspend",
                "--activation-request-id",
                "req-1",
                "--actor-id",
                "operator-z",
                "--actor-role",
                "operator",
                "--reason-code",
                "manual_suspend",
            ]
        )
        self.assertEqual(suspend_args.coo_dispatch_production_activation_command, "suspend")
        revoke_args = parser.parse_args(
            [
                "production",
                "activation",
                "revoke",
                "--activation-request-id",
                "req-1",
                "--actor-id",
                "operator-z",
                "--actor-role",
                "operator",
                "--reason-code",
                "suspended_revoked",
            ]
        )
        self.assertEqual(revoke_args.coo_dispatch_production_activation_command, "revoke")

    def test_rollback_missing_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        from agent.coo.production_activation_state import ActivationRequest

        broken = ActivationRequest(**{**loaded.__dict__, "rollback_commit": ""})
        with _gate_patch_context():
            assessment = evaluate_active_gate(
                broken,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_ROLLBACK_MISSING, assessment.blocking_reasons)

    def test_attestation_invalid_gate_false(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        from agent.coo.production_activation_state import ActivationRequest

        broken = ActivationRequest(
            **{**loaded.__dict__, "repository_attestation_hash": "bad"}
        )
        with _gate_patch_context():
            assessment = evaluate_active_gate(
                broken,
                repo_root=self.repo_root,
                now=self._now + timedelta(minutes=5),
            )
        self.assertIn(BLOCK_ATTESTATION_INVALID, assessment.blocking_reasons)

    def test_revoke_from_armed_rejected(self) -> None:
        activation_id = self._armed_id()
        with self.assertRaises(ProductionActivationKillSwitchError):
            run_activation_revoke(
                activation_request_id=activation_id,
                actor_id="operator-z",
                actor_role="operator",
                reason_code="suspended_revoked",
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=5),
            )


if __name__ == "__main__":
    unittest.main()
