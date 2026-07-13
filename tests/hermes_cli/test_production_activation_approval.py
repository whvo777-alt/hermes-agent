"""Phase 14D tests — production activation multi-party approval."""

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
    ProductionActivationApprovalError,
    record_release_approver_approval,
    record_security_reviewer_approval,
    run_activation_approve,
    run_activation_security_review,
    run_activation_status,
    show_activation_approval_status,
)
from agent.coo.production_activation_state import (
    ACTIVATION_PLATFORM_CLI,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_STATE_APPROVED,
    ACTIVATION_STATE_PROPOSED,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    activation_request_to_dict,
    append_activation_proposal,
    load_activation_request,
    save_activation_request,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser

_TESTED_SHA = "ca269dab24ffceb43ddfeb44c76a5120f987dc46"
_ROLLBACK_SHA = "18a03673739262534847af0296458239511bb7e6"
_ATTESTATION_HASH = "a" * 64

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
    "approver-b",
    "approver-c",
    "reviewer-d",
    "operator-a",
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


class TestProductionActivationApproval(unittest.TestCase):
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

    def _artifact_path(self, activation_id: str) -> Path:
        return self.store_dir / f"{activation_id}.json"

    def test_first_release_approver_keeps_proposed(self) -> None:
        activation_id = self._propose()
        status = record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        self.assertEqual(status.state, ACTIVATION_STATE_PROPOSED)
        self.assertEqual(status.release_approver_count, 1)
        self.assertFalse(status.quorum_satisfied)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_PROPOSED)
        self.assertEqual(len(loaded.approval_history), 1)

    def test_second_release_approver_still_proposed_without_security(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        status = record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-c",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=1),
        )
        self.assertEqual(status.state, ACTIVATION_STATE_PROPOSED)
        self.assertEqual(status.release_approver_count, 2)
        self.assertFalse(status.quorum_satisfied)
        self.assertEqual(status.recommended_action, "collect_security_review")

    def test_security_review_after_two_approvers_transitions_to_approved(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-c",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=1),
        )
        status = record_security_reviewer_approval(
            activation_request_id=activation_id,
            reviewer_id="reviewer-d",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=2),
        )
        self.assertEqual(status.state, ACTIVATION_STATE_APPROVED)
        self.assertTrue(status.quorum_satisfied)
        self.assertEqual(status.recommended_action, "activation_approved_wait_for_arm")
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_APPROVED)
        self.assertEqual(len(loaded.approved_by), 2)
        self.assertEqual(loaded.security_reviewed_by, "reviewer-d")
        approved_transitions = [
            item
            for item in loaded.state_history
            if item.from_state == ACTIVATION_STATE_PROPOSED
            and item.to_state == ACTIVATION_STATE_APPROVED
        ]
        self.assertEqual(len(approved_transitions), 1)

    def test_security_review_first_then_approvers_reaches_approved(self) -> None:
        activation_id = self._propose()
        record_security_reviewer_approval(
            activation_request_id=activation_id,
            reviewer_id="reviewer-d",
            store_dir=self.store_dir,
            now=self._now,
        )
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=1),
        )
        status = record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-c",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=2),
        )
        self.assertEqual(status.state, ACTIVATION_STATE_APPROVED)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(len(loaded.approval_history), 3)

    def test_requester_cannot_approve(self) -> None:
        activation_id = self._propose()
        with self.assertRaises(ProductionActivationApprovalError):
            record_release_approver_approval(
                activation_request_id=activation_id,
                approver_id="operator-a",
                store_dir=self.store_dir,
                now=self._now,
            )

    def test_duplicate_actor_no_mutation(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        status = record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=5),
        )
        self.assertTrue(status.duplicate_recorded)
        self.assertEqual(status.recommended_action, "duplicate_approval")
        self.assertEqual(_artifact_digest(path), digest_before)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(len(loaded.approval_history), 1)

    def test_approver_and_reviewer_same_identity_rejected(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        with self.assertRaises(ProductionActivationApprovalError):
            record_security_reviewer_approval(
                activation_request_id=activation_id,
                reviewer_id="approver-b",
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=1),
            )

    def test_invalid_role_rejected(self) -> None:
        activation_id = self._propose()
        with self.assertRaises(ProductionActivationApprovalError):
            run_activation_approve(
                activation_request_id=activation_id,
                approver_id="approver-b",
                approver_role="production_executor",
                store_dir=self.store_dir,
                now=self._now,
            )

    def test_expired_proposal_rejects_approval(self) -> None:
        activation_id = self._propose()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        expired_now = datetime.fromisoformat(
            loaded.expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        with self.assertRaises(ProductionActivationApprovalError):
            record_release_approver_approval(
                activation_request_id=activation_id,
                approver_id="approver-b",
                store_dir=self.store_dir,
                now=expired_now,
            )
        status = show_activation_approval_status(
            activation_request_id=activation_id,
            store_dir=self.store_dir,
            now=expired_now,
        )
        self.assertTrue(status.expired)
        self.assertEqual(status.recommended_action, "proposal_expired")

    def test_approved_artifact_reapproval_is_idempotent_status(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-c",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=1),
        )
        record_security_reviewer_approval(
            activation_request_id=activation_id,
            reviewer_id="reviewer-d",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=2),
        )
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        status = record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-e",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=3),
        )
        self.assertEqual(status.state, ACTIVATION_STATE_APPROVED)
        self.assertEqual(status.recommended_action, "activation_approved_wait_for_arm")
        self.assertEqual(_artifact_digest(path), digest_before)

    def test_immutable_sha_drift_fail_closed(self) -> None:
        activation_id = self._propose()
        path = self._artifact_path(activation_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["approval_history"] = [
            {
                "approver_id": "approver-b",
                "role": "release_approver",
                "timestamp": "2026-07-13T12:00:00+00:00",
                "approval_id": "aid-1",
                "activation_request_id": activation_id,
                "decision": "approved",
                "reason_code": "release_approval_recorded",
                "tested_commit_sha": "deadbeef" * 8,
                "release_tag": payload["release_tag"],
            }
        ]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with self.assertRaises(ProductionActivationApprovalError):
            record_release_approver_approval(
                activation_request_id=activation_id,
                approver_id="approver-c",
                store_dir=self.store_dir,
                now=self._now,
            )

    def test_corrupted_artifact_fail_closed(self) -> None:
        activation_id = self._propose()
        path = self._artifact_path(activation_id)
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ProductionActivationApprovalError):
            show_activation_approval_status(
                activation_request_id=activation_id,
                store_dir=self.store_dir,
            )

    def test_approval_history_append_only(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        first = load_activation_request(activation_id, store_dir=self.store_dir)
        first_record = first.approval_history[0]
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-c",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=1),
        )
        second = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(second.approval_history[0], first_record)
        self.assertEqual(len(second.approval_history), 2)

    def test_state_history_single_proposed_to_approved_transition(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-c",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=1),
        )
        record_security_reviewer_approval(
            activation_request_id=activation_id,
            reviewer_id="reviewer-d",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=2),
        )
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        transitions = [
            item
            for item in loaded.state_history
            if item.from_state == ACTIVATION_STATE_PROPOSED
            and item.to_state == ACTIVATION_STATE_APPROVED
        ]
        self.assertEqual(len(transitions), 1)

    def test_safe_output_fields(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        output, exit_code = run_activation_status(
            activation_request_id=activation_id,
            store_dir=self.store_dir,
            now=self._now,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("activation_request_id:", output)
        self.assertIn("release_approver_count: 1", output)
        self.assertIn("production_execution_allowed: false", output)
        lowered = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_no_subprocess(self) -> None:
        activation_id = self._propose()
        with patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("no subprocess"),
        ), patch.object(
            subprocess,
            "Popen",
            side_effect=AssertionError("no subprocess"),
        ):
            record_release_approver_approval(
                activation_request_id=activation_id,
                approver_id="approver-b",
                store_dir=self.store_dir,
                now=self._now,
            )
            run_activation_security_review(
                activation_request_id=activation_id,
                reviewer_id="reviewer-d",
                store_dir=self.store_dir,
                now=self._now + timedelta(minutes=1),
            )

    def test_cli_parser_approve_security_status_subcommands(self) -> None:
        parser = build_coo_dispatch_parser()
        approve_args = parser.parse_args(
            [
                "production",
                "activation",
                "approve",
                "--activation-request-id",
                "req-1",
                "--approver-id",
                "approver-b",
                "--approver-role",
                "release_approver",
            ]
        )
        self.assertEqual(
            approve_args.coo_dispatch_production_activation_command,
            "approve",
        )
        security_args = parser.parse_args(
            [
                "production",
                "activation",
                "security-review",
                "--activation-request-id",
                "req-1",
                "--reviewer-id",
                "reviewer-d",
            ]
        )
        self.assertEqual(
            security_args.coo_dispatch_production_activation_command,
            "security-review",
        )
        status_args = parser.parse_args(
            [
                "production",
                "activation",
                "status",
                "--activation-request-id",
                "req-1",
            ]
        )
        self.assertEqual(
            status_args.coo_dispatch_production_activation_command,
            "status",
        )

    def test_store_payload_keeps_production_execution_disallowed(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-c",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=1),
        )
        record_security_reviewer_approval(
            activation_request_id=activation_id,
            reviewer_id="reviewer-d",
            store_dir=self.store_dir,
            now=self._now + timedelta(minutes=2),
        )
        payload = json.loads(self._artifact_path(activation_id).read_text(encoding="utf-8"))
        self.assertFalse(payload["production_execution_allowed"])
        self.assertEqual(payload["state"], ACTIVATION_STATE_APPROVED)

    def test_save_activation_request_requires_existing_artifact(self) -> None:
        activation_id = self._propose()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self._artifact_path(activation_id).unlink()
        with self.assertRaises(ProductionActivationStoreError):
            save_activation_request(loaded, store_dir=self.store_dir)

    def test_approval_records_include_immutable_fields(self) -> None:
        activation_id = self._propose()
        record_release_approver_approval(
            activation_request_id=activation_id,
            approver_id="approver-b",
            store_dir=self.store_dir,
            now=self._now,
        )
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        record = loaded.approval_history[0]
        self.assertEqual(record.tested_commit_sha, _TESTED_SHA)
        self.assertEqual(record.release_tag, "v1.0.0-rc.1")
        self.assertEqual(record.decision, "approved")
        self.assertTrue(record.approval_id)
        self.assertEqual(record.activation_request_id, activation_id)

    def test_round_trip_dict_preserves_approval_records(self) -> None:
        activation_id = self._propose()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        payload = activation_request_to_dict(loaded)
        self.assertIn("approval_history", payload)
        self.assertEqual(payload["approval_history"], [])


if __name__ == "__main__":
    unittest.main()
