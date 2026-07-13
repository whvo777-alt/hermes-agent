"""Phase 14E tests — production activation arm/disarm and TTL."""

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
from agent.coo.production_activation_approval import (
    record_release_approver_approval,
    record_security_reviewer_approval,
)
from agent.coo.production_activation_arm import (
    ARM_TTL_MINUTES,
    CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
    ProductionActivationArmError,
    arm_production_activation,
    disarm_production_activation,
    maybe_expire_armed_activation,
    refresh_activation_lifecycle,
    run_activation_arm,
    run_activation_disarm,
    run_activation_status,
    show_activation_lifecycle_status,
)
from agent.coo.production_activation_state import (
    ACTIVATION_PLATFORM_CLI,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_STATE_APPROVED,
    ACTIVATION_STATE_ARMED,
    ACTIVATION_STATE_REVOKED,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    append_activation_proposal,
    load_activation_request,
    save_activation_request,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser

_TESTED_SHA = "ca269dab24ffceb43ddfeb44c76a5120f987dc46"
_ROLLBACK_SHA = "18a03673739262534847af0296458239511bb7e6"
_ATTESTATION_HASH = "a" * 64
_EXECUTOR_ID = "executor-e"

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
    "approver-b",
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


class TestProductionActivationArm(unittest.TestCase):
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

    def _propose(self, **overrides: object) -> str:
        with patch(
            "agent.coo.dispatch_cli_production_activation.resolve_git_head_commit",
            return_value=_TESTED_SHA,
        ):
            request = build_production_activation_proposal(
                **_proposal_kwargs(**overrides),
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

    def _approved_id(self) -> str:
        activation_id = self._propose()
        self._approve(activation_id)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_APPROVED)
        return activation_id

    def _artifact_path(self, activation_id: str) -> Path:
        return self.store_dir / f"{activation_id}.json"

    def _arm(
        self,
        activation_id: str,
        *,
        executor_id: str = _EXECUTOR_ID,
        phrase: str = CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
        now: datetime | None = None,
    ):
        return arm_production_activation(
            activation_request_id=activation_id,
            executor_id=executor_id,
            phrase=phrase,
            store_dir=self.store_dir,
            repo_root=self.repo_root,
            now=now or self._now + timedelta(minutes=4),
        )

    def test_approved_valid_executor_and_phrase_arms(self) -> None:
        activation_id = self._approved_id()
        status = self._arm(activation_id)
        self.assertEqual(status.state, ACTIVATION_STATE_ARMED)
        self.assertTrue(status.executor_assigned)
        self.assertTrue(status.phrase_verified)
        self.assertTrue(status.armed)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_ARMED)
        self.assertEqual(loaded.executor_id, _EXECUTOR_ID)
        self.assertTrue(loaded.phrase_verified)

    def test_armed_expires_at_about_fifteen_minutes(self) -> None:
        activation_id = self._approved_id()
        arm_time = self._now + timedelta(minutes=4)
        self._arm(activation_id, now=arm_time)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        armed_expires = datetime.fromisoformat(loaded.armed_expires_at)
        expected = arm_time + timedelta(minutes=ARM_TTL_MINUTES)
        self.assertEqual(armed_expires, expected)

    def test_effective_expiry_capped_by_proposal_expires_at(self) -> None:
        activation_id = self._approved_id()
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        early_expiry = (self._now + timedelta(minutes=10)).isoformat()
        from agent.coo.production_activation_state import ActivationRequest

        capped_request = ActivationRequest(
            **{
                **request.__dict__,
                "expires_at": early_expiry,
            }
        )
        save_activation_request(capped_request, store_dir=self.store_dir)
        arm_time = self._now + timedelta(minutes=4)
        self._arm(activation_id, now=arm_time)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        armed_expires = datetime.fromisoformat(loaded.armed_expires_at)
        self.assertEqual(armed_expires, datetime.fromisoformat(early_expiry))

    def test_wrong_phrase_zero_mutation(self) -> None:
        activation_id = self._approved_id()
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        with self.assertRaises(ProductionActivationArmError):
            self._arm(activation_id, phrase="WRONG-PHRASE")
        self.assertEqual(_artifact_digest(path), digest_before)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_APPROVED)

    def test_requester_cannot_be_executor(self) -> None:
        activation_id = self._approved_id()
        with self.assertRaises(ProductionActivationArmError):
            self._arm(activation_id, executor_id="operator-a")

    def test_approver_cannot_be_executor(self) -> None:
        activation_id = self._approved_id()
        with self.assertRaises(ProductionActivationArmError):
            self._arm(activation_id, executor_id="approver-b")

    def test_security_reviewer_cannot_be_executor(self) -> None:
        activation_id = self._approved_id()
        with self.assertRaises(ProductionActivationArmError):
            self._arm(activation_id, executor_id="reviewer-d")

    def test_insufficient_quorum_rejects_arm(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=1),
        )
        with self.assertRaises(ProductionActivationArmError):
            self._arm(activation_id)

    def test_head_mismatch_rejects_arm(self) -> None:
        activation_id = self._approved_id()
        with patch(
            "agent.coo.production_activation_arm.resolve_git_head_commit",
            return_value="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        ):
            with self.assertRaises(ProductionActivationArmError):
                self._arm(activation_id)

    def test_expired_proposal_rejects_arm(self) -> None:
        activation_id = self._approved_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        expired_now = datetime.fromisoformat(
            loaded.expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        with self.assertRaises(ProductionActivationArmError):
            self._arm(activation_id, now=expired_now)

    def test_duplicate_same_executor_arm_idempotent(self) -> None:
        activation_id = self._approved_id()
        self._arm(activation_id)
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        status = self._arm(activation_id)
        self.assertTrue(status.already_armed)
        self.assertEqual(_artifact_digest(path), digest_before)

    def test_different_executor_rearm_conflict(self) -> None:
        activation_id = self._approved_id()
        self._arm(activation_id)
        with self.assertRaises(ProductionActivationArmError):
            self._arm(activation_id, executor_id="executor-f")

    def test_armed_manual_disarm_revokes(self) -> None:
        activation_id = self._approved_id()
        self._arm(activation_id)
        status = disarm_production_activation(
            activation_request_id=activation_id,
            actor_id=_EXECUTOR_ID,
            reason_code="manual_disarm",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=5),
        )
        self.assertEqual(status.state, ACTIVATION_STATE_REVOKED)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.disarm_reason_code, "manual_disarm")

    def test_approved_operator_cancel_revokes(self) -> None:
        activation_id = self._approved_id()
        status = disarm_production_activation(
            activation_request_id=activation_id,
            actor_id="operator-z",
            reason_code="operator_cancelled",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=4),
        )
        self.assertEqual(status.state, ACTIVATION_STATE_REVOKED)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.disarm_reason_code, "operator_cancelled")
        self.assertFalse(loaded.executor_id)

    def test_armed_ttl_expiry_revokes(self) -> None:
        activation_id = self._approved_id()
        arm_time = self._now + timedelta(minutes=4)
        self._arm(activation_id, now=arm_time)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        expired_now = datetime.fromisoformat(
            loaded.armed_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        request = refresh_activation_lifecycle(
            activation_request_id=activation_id,
            store_dir=self.store_dir,
            now=expired_now,
        )
        self.assertEqual(request.state, ACTIVATION_STATE_REVOKED)
        self.assertEqual(request.disarm_reason_code, "arm_expired")

    def test_expired_armed_does_not_transition_to_active(self) -> None:
        activation_id = self._approved_id()
        self._arm(activation_id)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        expired_now = datetime.fromisoformat(
            loaded.armed_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        request = refresh_activation_lifecycle(
            activation_request_id=activation_id,
            store_dir=self.store_dir,
            now=expired_now,
        )
        self.assertNotEqual(request.state, "active")
        self.assertEqual(request.state, ACTIVATION_STATE_REVOKED)

    def test_revoked_artifact_rearm_rejected(self) -> None:
        activation_id = self._approved_id()
        disarm_production_activation(
            activation_request_id=activation_id,
            actor_id="operator-z",
            reason_code="operator_cancelled",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=4),
        )
        with self.assertRaises(ProductionActivationArmError):
            self._arm(activation_id)

    def test_phrase_plaintext_not_stored(self) -> None:
        activation_id = self._approved_id()
        self._arm(activation_id)
        payload = json.loads(self._artifact_path(activation_id).read_text(encoding="utf-8"))
        self.assertTrue(payload["phrase_verified"])
        self.assertNotIn("phrase", payload)
        self.assertNotIn(CONFIRM_PRODUCTION_ACTIVATION_PHRASE, json.dumps(payload))

    def test_status_safe_output(self) -> None:
        activation_id = self._approved_id()
        self._arm(activation_id)
        output, exit_code = run_activation_status(
            activation_request_id=activation_id,
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=4),
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("executor_assigned: true", output)
        self.assertIn("phrase_verified: true", output)
        self.assertIn("armed: true", output)
        self.assertIn("production_execution_allowed: false", output)
        lowered = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_state_history_append_only(self) -> None:
        activation_id = self._approved_id()
        before = load_activation_request(activation_id, store_dir=self.store_dir)
        self._arm(activation_id)
        after = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(after.state_history[: len(before.state_history)], before.state_history)
        self.assertEqual(len(after.state_history), len(before.state_history) + 1)

    def test_duplicate_disarm_idempotent(self) -> None:
        activation_id = self._approved_id()
        self._arm(activation_id)
        disarm_production_activation(
            activation_request_id=activation_id,
            actor_id=_EXECUTOR_ID,
            reason_code="manual_disarm",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=5),
        )
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        status = disarm_production_activation(
            activation_request_id=activation_id,
            actor_id=_EXECUTOR_ID,
            reason_code="manual_disarm",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=6),
        )
        self.assertTrue(status.already_revoked)
        self.assertEqual(_artifact_digest(path), digest_before)

    def test_atomic_write_failure_preserves_artifact(self) -> None:
        activation_id = self._approved_id()
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        with patch(
            "agent.coo.production_activation_store._atomic_replace_json",
            side_effect=OSError("write failed"),
        ):
            with self.assertRaises(ProductionActivationArmError):
                self._arm(activation_id)
        self.assertEqual(_artifact_digest(path), digest_before)

    def test_no_subprocess(self) -> None:
        activation_id = self._approved_id()
        with patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("no subprocess"),
        ), patch.object(
            subprocess,
            "Popen",
            side_effect=AssertionError("no subprocess"),
        ):
            self._arm(activation_id)
            run_activation_disarm(
                activation_request_id=activation_id,
                actor_id=_EXECUTOR_ID,
                reason_code="manual_disarm",
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=5),
            )

    def test_cli_parser_arm_disarm_subcommands(self) -> None:
        parser = build_coo_dispatch_parser()
        arm_args = parser.parse_args(
            [
                "production",
                "activation",
                "arm",
                "--activation-request-id",
                "req-1",
                "--executor-id",
                "executor-e",
                "--phrase",
                CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
            ]
        )
        self.assertEqual(arm_args.coo_dispatch_production_activation_command, "arm")
        disarm_args = parser.parse_args(
            [
                "production",
                "activation",
                "disarm",
                "--activation-request-id",
                "req-1",
                "--actor-id",
                "operator-z",
                "--reason-code",
                "manual_disarm",
            ]
        )
        self.assertEqual(disarm_args.coo_dispatch_production_activation_command, "disarm")

    def test_store_keeps_production_execution_disallowed(self) -> None:
        activation_id = self._approved_id()
        self._arm(activation_id)
        payload = json.loads(self._artifact_path(activation_id).read_text(encoding="utf-8"))
        self.assertFalse(payload["production_execution_allowed"])

    def test_maybe_expire_without_persist(self) -> None:
        activation_id = self._approved_id()
        arm_time = self._now + timedelta(minutes=4)
        self._arm(activation_id, now=arm_time)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        expired_now = datetime.fromisoformat(
            loaded.armed_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        revoked, changed = maybe_expire_armed_activation(
            loaded,
            persist=False,
            now=expired_now,
        )
        self.assertTrue(changed)
        self.assertEqual(revoked.state, ACTIVATION_STATE_REVOKED)
        still_armed = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(still_armed.state, ACTIVATION_STATE_ARMED)

    def test_run_activation_arm_cli_wrapper(self) -> None:
        activation_id = self._approved_id()
        output, exit_code = run_activation_arm(
            activation_request_id=activation_id,
            executor_id=_EXECUTOR_ID,
            phrase=CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
            store_dir=self.store_dir,
            repo_root=self.repo_root,
            now=self._now + timedelta(minutes=4),
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("state: armed", output)

    def test_show_status_after_approve_recommends_arm_confirmation(self) -> None:
        activation_id = self._approved_id()
        status = show_activation_lifecycle_status(
            activation_request_id=activation_id,
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=4),
        )
        self.assertEqual(status.recommended_action, "collect_arm_confirmation")


if __name__ == "__main__":
    unittest.main()
