"""Phase 14G tests — production activation dry-run contract."""

from __future__ import annotations

import hashlib
import json
import os
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
    CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
    arm_production_activation,
)
from agent.coo.production_activation_dry_run import (
    ACTION_ALREADY_EVALUATED,
    ACTION_PRODUCTION_DRY_RUN_READY_WAIT_FOR_PHASE_14H,
    BLOCK_ACTIVE_GATE_NOT_READY,
    BLOCK_ARMED_EXPIRED,
    BLOCK_AUDIT_STORE_UNAVAILABLE,
    BLOCK_CONFIRMATION_SCOPE_MISMATCH,
    BLOCK_CUTOVER_NOT_READY,
    BLOCK_KILL_SWITCH_UNAVAILABLE,
    BLOCK_MIRROR_ROOT_NOT_TRUSTED,
    BLOCK_PRODUCTION_ROOT_DENIED,
    BLOCK_PUBLISH_NOT_ALLOWED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_REGRESSION_BLOCKED,
    BLOCK_REPAIR_LOCK_HELD,
    BLOCK_SIGNOFF_NOT_READY,
    BLOCK_TICKET_SCOPE_MISMATCH,
    ProductionActivationDryRunError,
    _load_dry_run_records,
    _snapshot_has_publish_intent,
    evaluate_production_dry_run,
    run_activation_dry_run,
    run_production_activation_dry_run,
)
from agent.coo.production_activation_state import (
    ACTIVATION_PLATFORM_CLI,
    ACTIVATION_SCOPE_MAINTENANCE_WINDOW,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_SCOPE_TICKET_SCOPED,
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_APPROVED,
    ACTIVATION_STATE_ARMED,
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
    ActivationRequest,
)
from agent.coo.production_activation_store import (
    append_activation_proposal,
    load_activation_request,
    save_activation_request,
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
_TICKET_ID = "ticket-dry-run-1"
_PRODUCTION_ROOT = "/opt/data/multi-content-pipeline"

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
)


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    (home / "coo" / "production-activation").mkdir(parents=True)
    (home / "coo" / "production-activation-dry-run").mkdir(parents=True)
    (home / "coo" / "confirmations").mkdir(parents=True)
    return home


def _activation_store_dir(hermes_home: Path) -> Path:
    return hermes_home / "coo" / "production-activation"


def _dry_run_history_dir(hermes_home: Path) -> Path:
    return hermes_home / "coo" / "production-activation-dry-run"


def _confirmation_dir(hermes_home: Path) -> Path:
    return hermes_home / "coo" / "confirmations"


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


class TestProductionActivationDryRun(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.hermes_home = _hermes_home(self.tmp_path)
        self.repo_root = self.tmp_path / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)
        self.mirror_root = self.tmp_path / "isolated-mirror"
        self.mirror_root.mkdir()
        self.store_dir = _activation_store_dir(self.hermes_home)
        self.history_dir = _dry_run_history_dir(self.hermes_home)
        self.confirmation_dir = _confirmation_dir(self.hermes_home)
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

    def _armed_id(self, **proposal_overrides: object) -> str:
        activation_id = self._propose(**proposal_overrides)
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

    def _write_confirmation(
        self,
        *,
        ticket_id: str = _TICKET_ID,
        attested_root: str | None = None,
        consumed: bool = False,
        confirmation_id: str = "conf-dry-run-1",
    ) -> str:
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket_id,
            plan_id="plan-1",
            unlock_token_id="token-1",
            dispatch_request_id="req-1",
            operator_id="op-dry-run",
            operator_name="Dry Run Operator",
            confirmation_reason="dry-run test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            attested_pipeline_root=attested_root or str(self.mirror_root.resolve()),
        )
        if consumed:
            from dataclasses import replace

            confirmation = replace(
                confirmation,
                confirmation_id=confirmation_id,
                consumed=True,
                consumed_at=self._now.isoformat(),
            )
        else:
            from dataclasses import replace

            confirmation = replace(confirmation, confirmation_id=confirmation_id)
        write_confirmation(confirmation, confirmation_dir=self.confirmation_dir)
        return confirmation_id

    def _dry_run_kwargs(self, activation_id: str, **overrides: object) -> dict[str, object]:
        if "confirmation_id" not in overrides:
            overrides["confirmation_id"] = self._write_confirmation()
        base: dict[str, object] = {
            "activation_request_id": activation_id,
            "ticket_id": _TICKET_ID,
            "confirmation_id": overrides.pop("confirmation_id"),
            "pipeline_root": str(self.mirror_root),
            "repo_root": self.repo_root,
            "store_dir": self.store_dir,
            "history_dir": self.history_dir,
            "confirmation_dir": self.confirmation_dir,
            "merged_config": self.merged_config,
            "now": self._now + timedelta(minutes=5),
        }
        base.update(overrides)
        return base

    def test_armed_valid_mirror_ticket_confirmation_dry_run_ready(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context():
            assessment, recorded = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id),
            )
        self.assertTrue(recorded)
        self.assertTrue(assessment.dry_run_ready)
        self.assertTrue(assessment.active_gate_ready)
        self.assertTrue(assessment.ticket_scope_valid)
        self.assertTrue(assessment.confirmation_scope_valid)
        self.assertTrue(assessment.pipeline_root_trusted)
        self.assertTrue(assessment.isolated_mirror_only)
        self.assertTrue(assessment.draft_only)
        self.assertFalse(assessment.publish_allowed)
        self.assertFalse(assessment.production_execution_allowed)
        self.assertFalse(assessment.repository2_execution_attempted)
        self.assertEqual(
            assessment.recommended_action,
            ACTION_PRODUCTION_DRY_RUN_READY_WAIT_FOR_PHASE_14H,
        )

    def test_dry_run_ready_does_not_transition_to_active(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context():
            run_production_activation_dry_run(**self._dry_run_kwargs(activation_id))
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_ARMED)

    def test_dry_run_ready_keeps_production_execution_disallowed(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context():
            output, exit_code = run_activation_dry_run(**self._dry_run_kwargs(activation_id))
        self.assertEqual(exit_code, 0)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("repository2_execution_attempted: false", output)

    def test_production_root_denied(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context():
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(
                    activation_id,
                    pipeline_root=_PRODUCTION_ROOT,
                ),
            )
        self.assertFalse(assessment.dry_run_ready)
        self.assertIn(BLOCK_PRODUCTION_ROOT_DENIED, assessment.blocking_reasons)

    def test_mirror_symlink_escape_denied(self) -> None:
        activation_id = self._armed_id()
        escape_target = self.tmp_path / "outside-mirror"
        escape_target.mkdir()
        symlink_path = self.tmp_path / "mirror-link"
        symlink_path.symlink_to(escape_target, target_is_directory=True)
        confirmation_id = self._write_confirmation(
            attested_root=str(symlink_path.resolve()),
        )
        with _gate_patch_context():
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(
                    activation_id,
                    confirmation_id=confirmation_id,
                    pipeline_root=str(symlink_path),
                ),
            )
        self.assertFalse(assessment.dry_run_ready)
        self.assertIn(BLOCK_MIRROR_ROOT_NOT_TRUSTED, assessment.blocking_reasons)

    def test_wrong_ticket_denied(self) -> None:
        activation_id = self._armed_id(
            scope_type=ACTIVATION_SCOPE_TICKET_SCOPED,
            ticket_id="scoped-ticket-a",
        )
        confirmation_id = self._write_confirmation(ticket_id="scoped-ticket-a")
        with _gate_patch_context():
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(
                    activation_id,
                    ticket_id="wrong-ticket",
                    confirmation_id=confirmation_id,
                ),
            )
        self.assertIn(BLOCK_TICKET_SCOPE_MISMATCH, assessment.blocking_reasons)

    def test_confirmation_mismatch_denied(self) -> None:
        activation_id = self._armed_id()
        other_mirror = self.tmp_path / "other-mirror"
        other_mirror.mkdir()
        confirmation_id = self._write_confirmation(
            attested_root=str(other_mirror.resolve()),
        )
        with _gate_patch_context():
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(
                    activation_id,
                    confirmation_id=confirmation_id,
                ),
            )
        self.assertIn(BLOCK_CONFIRMATION_SCOPE_MISMATCH, assessment.blocking_reasons)

    def test_consumed_confirmation_denied(self) -> None:
        activation_id = self._armed_id()
        confirmation_id = self._write_confirmation(consumed=True)
        with _gate_patch_context():
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(
                    activation_id,
                    confirmation_id=confirmation_id,
                ),
            )
        self.assertIn(BLOCK_CONFIRMATION_SCOPE_MISMATCH, assessment.blocking_reasons)

    def test_publish_intent_denied(self) -> None:
        activation_id = self._armed_id()
        self.assertTrue(
            _snapshot_has_publish_intent({"workflow": {"publish": True}}),
        )
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_dry_run._probe_publish_intent",
            return_value=True,
        ):
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id),
            )
        self.assertIn(BLOCK_PUBLISH_NOT_ALLOWED, assessment.blocking_reasons)
        self.assertFalse(assessment.draft_only)

    def test_armed_ttl_expired_denied(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        expired_now = datetime.fromisoformat(
            loaded.armed_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        with _gate_patch_context():
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id, now=expired_now),
            )
        self.assertIn(BLOCK_ARMED_EXPIRED, assessment.blocking_reasons)

    def test_active_gate_false_denied(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_regression_clear",
            return_value=False,
        ):
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id),
            )
        self.assertIn(BLOCK_ACTIVE_GATE_NOT_READY, assessment.blocking_reasons)
        self.assertIn(BLOCK_REGRESSION_BLOCKED, assessment.blocking_reasons)

    def test_recovery_required_denied(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_recovery_required",
            return_value=True,
        ):
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id),
            )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, assessment.blocking_reasons)

    def test_repair_lock_denied(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_repair_lock_held",
            return_value=True,
        ):
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id),
            )
        self.assertIn(BLOCK_REPAIR_LOCK_HELD, assessment.blocking_reasons)

    def test_regression_fail_denied(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_regression_clear",
            return_value=False,
        ):
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id),
            )
        self.assertIn(BLOCK_REGRESSION_BLOCKED, assessment.blocking_reasons)

    def test_signoff_not_ready_denied(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_signoff_ready",
            return_value=False,
        ):
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id),
            )
        self.assertIn(BLOCK_SIGNOFF_NOT_READY, assessment.blocking_reasons)

    def test_cutover_not_ready_denied(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate._probe_cutover_ready",
            return_value=False,
        ):
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id),
            )
        self.assertIn(BLOCK_CUTOVER_NOT_READY, assessment.blocking_reasons)

    def test_kill_switch_unavailable_denied(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_active_gate.is_kill_switch_available",
            return_value=False,
        ):
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id),
            )
        self.assertIn(BLOCK_KILL_SWITCH_UNAVAILABLE, assessment.blocking_reasons)

    def test_audit_persistence_failure_fail_closed(self) -> None:
        activation_id = self._armed_id()
        kwargs = self._dry_run_kwargs(activation_id)
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_dry_run._atomic_append_dry_run_record",
            side_effect=ProductionActivationDryRunError("Dry-run audit persistence failed."),
        ):
            with self.assertRaises(ProductionActivationDryRunError):
                run_production_activation_dry_run(**kwargs)

    def test_audit_store_unavailable_blocks_ready(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_dry_run.probe_dry_run_audit_store_available",
            return_value=False,
        ):
            assessment = evaluate_production_dry_run(
                load_activation_request(activation_id, store_dir=self.store_dir),
                **{
                    key: value
                    for key, value in self._dry_run_kwargs(activation_id).items()
                    if key != "activation_request_id"
                },
            )
        self.assertIn(BLOCK_AUDIT_STORE_UNAVAILABLE, assessment.blocking_reasons)

    def test_duplicate_dry_run_idempotent(self) -> None:
        activation_id = self._armed_id()
        kwargs = self._dry_run_kwargs(activation_id)
        with _gate_patch_context():
            first, recorded_first = run_production_activation_dry_run(**kwargs)
            second, recorded_second = run_production_activation_dry_run(**kwargs)
        self.assertTrue(recorded_first)
        self.assertFalse(recorded_second)
        self.assertTrue(second.already_evaluated)
        self.assertEqual(second.recommended_action, ACTION_ALREADY_EVALUATED)
        records = _load_dry_run_records(
            activation_id,
            history_dir=self.history_dir,
        )
        self.assertEqual(len(records), 1)

    def test_dry_run_audit_append_only(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context():
            run_production_activation_dry_run(**self._dry_run_kwargs(activation_id))
        history_path = self.history_dir / f"{activation_id}.json"
        self.assertTrue(history_path.is_file())
        payload = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["records"]), 1)
        record = payload["records"][0]
        self.assertEqual(record["result"], "ready")
        self.assertFalse(record["repository2_execution_attempted"])

    def test_activation_artifact_and_control_history_unchanged(self) -> None:
        activation_id = self._armed_id()
        path = self._artifact_path(activation_id)
        before = load_activation_request(activation_id, store_dir=self.store_dir)
        digest_before = _artifact_digest(path)
        control_len_before = len(before.control_history)
        with _gate_patch_context():
            run_production_activation_dry_run(**self._dry_run_kwargs(activation_id))
        after = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(_artifact_digest(path), digest_before)
        self.assertEqual(len(after.control_history), control_len_before)
        self.assertEqual(after.state, ACTIVATION_STATE_ARMED)

    def test_non_armed_states_blocked(self) -> None:
        activation_id = self._propose()
        self._approve(activation_id)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_APPROVED)
        with _gate_patch_context():
            assessment = evaluate_production_dry_run(
                loaded,
                **{
                    key: value
                    for key, value in self._dry_run_kwargs(activation_id).items()
                    if key != "activation_request_id"
                },
            )
        self.assertFalse(assessment.dry_run_ready)

    def test_suspended_and_revoked_blocked(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        for state in (ACTIVATION_STATE_SUSPENDED, ACTIVATION_STATE_REVOKED):
            with self.subTest(state=state):
                broken = ActivationRequest(**{**loaded.__dict__, "state": state})
                with _gate_patch_context():
                    assessment = evaluate_production_dry_run(
                        broken,
                        **{
                            key: value
                            for key, value in self._dry_run_kwargs(activation_id).items()
                            if key != "activation_request_id"
                        },
                    )
                self.assertFalse(assessment.dry_run_ready)

    def test_active_state_fail_closed(self) -> None:
        activation_id = self._armed_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        broken = ActivationRequest(**{**loaded.__dict__, "state": ACTIVATION_STATE_ACTIVE})
        with _gate_patch_context():
            assessment = evaluate_production_dry_run(
                broken,
                **{
                    key: value
                    for key, value in self._dry_run_kwargs(activation_id).items()
                    if key != "activation_request_id"
                },
            )
        self.assertFalse(assessment.dry_run_ready)

    def test_maintenance_window_scope_blocked(self) -> None:
        activation_id = self._armed_id(
            scope_type=ACTIVATION_SCOPE_MAINTENANCE_WINDOW,
            maintenance_window_start="2026-07-13T10:00:00+00:00",
            maintenance_window_end="2026-07-13T11:00:00+00:00",
        )
        with _gate_patch_context():
            assessment, _ = run_production_activation_dry_run(
                **self._dry_run_kwargs(activation_id),
            )
        self.assertIn(BLOCK_TICKET_SCOPE_MISMATCH, assessment.blocking_reasons)

    def test_safe_output(self) -> None:
        activation_id = self._armed_id()
        with _gate_patch_context():
            output, _ = run_activation_dry_run(**self._dry_run_kwargs(activation_id))
        sanitized = output
        for allowed in (
            "repository2_execution_attempted: false",
            "production_execution_allowed: false",
            "pipeline_root_trusted:",
            "isolated_mirror_only:",
        ):
            sanitized = sanitized.replace(allowed, "")
        lowered = sanitized.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_no_subprocess_or_bounded_runner(self) -> None:
        activation_id = self._armed_id()
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
            run_activation_dry_run(**self._dry_run_kwargs(activation_id))

    def test_cli_parser_dry_run(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "production",
                "activation",
                "dry-run",
                "--activation-request-id",
                "req-1",
                "--ticket-id",
                _TICKET_ID,
                "--confirmation-id",
                "conf-1",
                "--pipeline-root",
                str(self.mirror_root),
            ]
        )
        self.assertEqual(args.coo_dispatch_production_activation_command, "dry-run")


if __name__ == "__main__":
    unittest.main()
