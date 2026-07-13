"""Phase 14H-2 tests — production activation execution gate."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import (
    build_dispatch_execution_bundle,
    mark_bundle_consumed,
    write_bundle,
)
from agent.coo.dispatch_cli_production_activation import build_production_activation_proposal
from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionRequest,
    DispatchUnlockTokenStore,
    create_dispatch_unlock_token,
)
from agent.coo.production_activation_active import (
    activate_production_activation,
)
from agent.coo.production_activation_approval import (
    record_release_approver_approval,
    record_security_reviewer_approval,
)
from agent.coo.production_activation_arm import (
    CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
    arm_production_activation,
)
from agent.coo.production_activation_dry_run import run_production_activation_dry_run
from agent.coo.production_activation_execution_gate import (
    ACTION_ALREADY_EVALUATED,
    ACTION_EXECUTION_GATE_READY_WAIT_FOR_PHASE_14H_3,
    BLOCK_ACTIVE_EXPIRED,
    BLOCK_ACTIVATION_NOT_ACTIVE,
    BLOCK_BINDING_NOT_BOUND,
    BLOCK_BOUNDED_RUNNER_CONTRACT_MISSING,
    BLOCK_BUNDLE_CONSUMED,
    BLOCK_BUNDLE_MISSING,
    BLOCK_CONFIRMATION_CONSUMED,
    BLOCK_CONFIRMATION_SCOPE_MISMATCH,
    BLOCK_CUTOVER_NOT_READY,
    BLOCK_DRY_RUN_CORRELATION_MISMATCH,
    BLOCK_DRY_RUN_MISSING,
    BLOCK_DRY_RUN_STALE,
    BLOCK_KILL_SWITCH_UNAVAILABLE,
    BLOCK_MIRROR_ROOT_NOT_TRUSTED,
    BLOCK_PRODUCTION_ROOT_DENIED,
    BLOCK_PUBLISH_NOT_ALLOWED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_REGRESSION_BLOCKED,
    BLOCK_REPAIR_LOCK_HELD,
    BLOCK_ROLLBACK_NOT_READY,
    BLOCK_SIGNOFF_NOT_READY,
    BLOCK_TICKET_SCOPE_MISMATCH,
    ProductionActivationExecutionGateError,
    _load_execution_gate_records,
    run_activation_execution_gate,
    run_production_execution_gate,
)
from agent.coo.production_activation_state import (
    ACTIVATION_PLATFORM_CLI,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_ARMED,
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
from agent.coo.tests.test_execution_dispatch_runtime import _approved_unlock_context
from hermes_cli.coo_dispatch import build_coo_dispatch_parser

_TESTED_SHA = "ca269dab24ffceb43ddfeb44c76a5120f987dc46"
_ROLLBACK_SHA = "18a03673739262534847af0296458239511bb7e6"
_ATTESTATION_HASH = "a" * 64
_EXECUTOR_ID = "executor-e"
_TICKET_ID = "ticket-exec-gate-1"
_PRODUCTION_ROOT = "/opt/data/multi-content-pipeline"

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
    "dry_run_key",
)


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    (home / "coo" / "production-activation").mkdir(parents=True)
    (home / "coo" / "production-activation-dry-run").mkdir(parents=True)
    (home / "coo" / "production-execution-gate").mkdir(parents=True)
    (home / "coo" / "dispatch-bundles").mkdir(parents=True)
    (home / "coo" / "confirmations").mkdir(parents=True)
    return home


def _enabled_executor_config(pipeline_root: Path) -> dict:
    return {
        "coo": {
            "dispatch": {
                "executor": {
                    "enabled": False,
                    "allowed_pipeline_roots": [str(pipeline_root.resolve())],
                }
            }
        }
    }


def _bound_config(pipeline_root: Path, node_path: Path) -> dict:
    config = _enabled_executor_config(pipeline_root)
    config["coo"]["dispatch"]["runner_provider"] = {"mode": "bounded"}
    config["coo"]["dispatch"]["runner"] = {
        "node_executable": str(node_path.resolve()),
    }
    return config


def _artifact_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_fake_node(workspace: Path) -> Path:
    bin_dir = workspace / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    node_path = bin_dir / "node"
    node_path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    node_path.chmod(0o755)
    return node_path


def _write_binding_state(hermes_home: Path, state: str = "bound") -> None:
    path = hermes_home / "coo" / "dispatch-runner-binding.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "state": state,
                "updated_at": "2026-07-13T12:00:00+00:00",
                "operator_id": "op-bind",
                "reason": "test",
            }
        ),
        encoding="utf-8",
    )


def _gate_patch_context():
    return patch.multiple(
        "agent.coo.production_activation_execution_gate",
        _probe_signoff_ready=lambda **_: True,
        _probe_cutover_ready=lambda **_: True,
        _probe_regression_clear=lambda: True,
        _probe_recovery_required=lambda request: False,
        _probe_repair_lock_held=lambda request: False,
    )


class TestProductionActivationExecutionGate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.hermes_home = _hermes_home(self.tmp_path)
        self.repo_root = self.tmp_path / "repo"
        self.repo_root.mkdir()
        git_dir = self.repo_root / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True)
        (git_dir / "main").write_text(f"{_TESTED_SHA}\n", encoding="utf-8")
        (self.repo_root / ".git" / "HEAD").write_text(
            "ref: refs/heads/main\n",
            encoding="utf-8",
        )
        self.mirror_root = self.tmp_path / "isolated-mirror"
        self.mirror_root.mkdir()
        self.fake_node = _write_fake_node(self.tmp_path)
        self.store_dir = self.hermes_home / "coo" / "production-activation"
        self.history_dir = self.hermes_home / "coo" / "production-activation-dry-run"
        self.gate_history_dir = self.hermes_home / "coo" / "production-execution-gate"
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.merged_config = _bound_config(self.mirror_root, self.fake_node)
        self.ticket_id = ""
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

    def _seed_bundle_and_confirmation(self) -> str:
        ticket, plan, dry_run, dry_run_request, execute_request, gate = (
            _approved_unlock_context()
        )
        self.ticket_id = ticket.ticket_id
        token_store = DispatchUnlockTokenStore()
        token = create_dispatch_unlock_token(
            ticket,
            plan,
            dry_run,
            dry_run_request,
            execute_request,
            gate,
            requested_by=ticket.requester_id,
            token_store=token_store,
        )
        dispatch_request = DispatchExecutionRequest(
            dispatch_request_id="req-gate-1",
            execute_request_id=token.execute_request_id,
            gate_id=gate.gate_id,
            ticket_id=ticket.ticket_id,
            plan_id=plan.plan_id,
            dry_run_run_id=token.dry_run_run_id,
            unlock_token_id=token.token_id,
            target_skills=list(token.target_skills),
            requested_by=ticket.requester_id,
            requested_at="2026-07-07T00:00:00+00:00",
        )
        bundle = build_dispatch_execution_bundle(
            ticket=ticket,
            plan=plan,
            dry_run=dry_run,
            dry_run_request=dry_run_request,
            execute_request=execute_request,
            gate=gate,
            token=token,
            dispatch_request=dispatch_request,
        )
        write_bundle(bundle, bundle_dir=self.bundle_dir)
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=token.plan_id,
            unlock_token_id=token.token_id,
            dispatch_request_id=dispatch_request.dispatch_request_id,
            operator_id="op-gate",
            operator_name="Gate Operator",
            confirmation_reason="gate test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            attested_pipeline_root=str(self.mirror_root.resolve()),
        )
        from dataclasses import replace

        confirmation = replace(confirmation, confirmation_id="conf-gate-1")
        write_confirmation(confirmation, confirmation_dir=self.confirmation_dir)
        return confirmation.confirmation_id

    def _ready_dry_run(self, activation_id: str, confirmation_id: str) -> None:
        with _gate_patch_context():
            run_production_activation_dry_run(
                activation_request_id=activation_id,
                ticket_id=self.ticket_id,
                confirmation_id=confirmation_id,
                pipeline_root=str(self.mirror_root),
                repo_root=self.repo_root,
                store_dir=self.store_dir,
                history_dir=self.history_dir,
                confirmation_dir=self.confirmation_dir,
                merged_config=self.merged_config,
                now=self._now + timedelta(minutes=5),
            )

    def _active_id(self) -> tuple[str, str]:
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
        confirmation_id = self._seed_bundle_and_confirmation()
        _write_binding_state(self.hermes_home)
        self._ready_dry_run(activation_id, confirmation_id)
        with _gate_patch_context():
            activate_production_activation(
                activation_request_id=activation_id,
                actor_id=_EXECUTOR_ID,
                actor_role="production_executor",
                phrase=CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
                repo_root=self.repo_root,
                store_dir=self.store_dir,
                history_dir=self.history_dir,
                merged_config=self.merged_config,
                now=self._now + timedelta(minutes=6),
            )
        self._align_bundle_timestamp_with_activation(activation_id)
        return activation_id, confirmation_id

    def _align_bundle_timestamp_with_activation(self, activation_id: str) -> None:
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        active_at = (loaded.active_at or "").strip()
        if not active_at or not self.ticket_id:
            return
        bundle_path = self.bundle_dir / f"{self.ticket_id}.json"
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        payload["updated_at"] = active_at
        bundle_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _gate_kwargs(self, activation_id: str, confirmation_id: str, **overrides):
        base = {
            "activation_request_id": activation_id,
            "ticket_id": self.ticket_id,
            "confirmation_id": confirmation_id,
            "pipeline_root": str(self.mirror_root),
            "repo_root": self.repo_root,
            "store_dir": self.store_dir,
            "history_dir": self.gate_history_dir,
            "dry_run_history_dir": self.history_dir,
            "bundle_dir": self.bundle_dir,
            "confirmation_dir": self.confirmation_dir,
            "merged_config": self.merged_config,
            "now": self._now + timedelta(minutes=7),
        }
        base.update(overrides)
        return base

    def _artifact_path(self, activation_id: str) -> Path:
        return self.store_dir / f"{activation_id}.json"

    def test_active_fresh_inputs_execution_gate_ready(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context():
            assessment, recorded = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertTrue(recorded)
        self.assertTrue(assessment.execution_gate_ready)
        self.assertTrue(assessment.dry_run_verified)
        self.assertTrue(assessment.dry_run_fresh)
        self.assertTrue(assessment.execution_runtime_disabled)

    def test_ready_does_not_call_execution(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context(), patch(
            "agent.coo.dispatch_cli_run.execute_coo_dispatch_run",
            side_effect=AssertionError("execution must not run"),
        ):
            run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )

    def test_safety_flags(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context():
            output, exit_code = run_activation_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("repository2_execution_attempted: false", output)
        self.assertIn("execution_runtime_disabled: true", output)

    def test_active_expired_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        expired_now = datetime.fromisoformat(
            loaded.active_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(
                    activation_id,
                    confirmation_id,
                    now=expired_now,
                )
            )
        self.assertIn(BLOCK_ACTIVE_EXPIRED, assessment.blocking_reasons)

    def test_armed_state_blocked(self) -> None:
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
        confirmation_id = self._seed_bundle_and_confirmation()
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_ACTIVATION_NOT_ACTIVE, assessment.blocking_reasons)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_ARMED)

    def test_dry_run_missing_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        path = self.history_dir / f"{activation_id}.json"
        if path.is_file():
            path.unlink()
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_DRY_RUN_MISSING, assessment.blocking_reasons)

    def test_dry_run_stale_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        path = self.history_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["records"][-1]["timestamp"] = (
            self._now - timedelta(hours=2)
        ).isoformat()
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_DRY_RUN_STALE, assessment.blocking_reasons)

    def test_dry_run_key_mismatch_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        from agent.coo.production_activation_state import ActivationRequest

        broken = ActivationRequest(
            **{
                **loaded.__dict__,
                "dry_run_key": "b" * 64,
            }
        )
        from agent.coo.production_activation_store import save_activation_request

        save_activation_request(broken, store_dir=self.store_dir)
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_DRY_RUN_CORRELATION_MISMATCH, assessment.blocking_reasons)

    def test_wrong_ticket_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(
                    activation_id,
                    confirmation_id,
                    ticket_id="wrong-ticket",
                )
            )
        self.assertIn(BLOCK_DRY_RUN_CORRELATION_MISMATCH, assessment.blocking_reasons)

    def test_bundle_missing_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        bundle_path = self.bundle_dir / f"{self.ticket_id}.json"
        bundle_path.unlink()
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_BUNDLE_MISSING, assessment.blocking_reasons)

    def test_bundle_consumed_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        mark_bundle_consumed(self.ticket_id, bundle_dir=self.bundle_dir)
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_BUNDLE_CONSUMED, assessment.blocking_reasons)

    def test_confirmation_consumed_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        from agent.coo.production_executor_confirmation import mark_confirmation_consumed_file

        mark_confirmation_consumed_file(
            confirmation_id,
            confirmation_dir=self.confirmation_dir,
        )
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_CONFIRMATION_CONSUMED, assessment.blocking_reasons)

    def test_confirmation_root_mismatch_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        other_mirror = self.tmp_path / "other-mirror"
        other_mirror.mkdir()
        confirmation = create_production_executor_confirmation(
            ticket_id=self.ticket_id,
            plan_id="plan-1",
            unlock_token_id="token-x",
            dispatch_request_id="req-x",
            operator_id="op-gate",
            operator_name="Gate Operator",
            confirmation_reason="gate test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            attested_pipeline_root=str(other_mirror.resolve()),
        )
        from dataclasses import replace

        confirmation = replace(confirmation, confirmation_id=confirmation_id)
        write_confirmation(confirmation, confirmation_dir=self.confirmation_dir)
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_CONFIRMATION_SCOPE_MISMATCH, assessment.blocking_reasons)

    def test_production_root_denied(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(
                    activation_id,
                    confirmation_id,
                    pipeline_root=_PRODUCTION_ROOT,
                )
            )
        self.assertIn(BLOCK_PRODUCTION_ROOT_DENIED, assessment.blocking_reasons)

    def test_mirror_symlink_escape_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        escape_target = self.tmp_path / "outside-mirror"
        escape_target.mkdir()
        symlink_path = self.tmp_path / "mirror-link"
        symlink_path.symlink_to(escape_target, target_is_directory=True)
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(
                    activation_id,
                    confirmation_id,
                    pipeline_root=str(symlink_path),
                )
            )
        self.assertIn(BLOCK_MIRROR_ROOT_NOT_TRUSTED, assessment.blocking_reasons)

    def test_publish_intent_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        bundle_path = self.bundle_dir / f"{self.ticket_id}.json"
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        snapshot = dict(payload.get("snapshot", {}))
        snapshot["publish"] = True
        payload["snapshot"] = snapshot
        bundle_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_PUBLISH_NOT_ALLOWED, assessment.blocking_reasons)

    def test_binding_unbound_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        _write_binding_state(self.hermes_home, state="staged")
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_BINDING_NOT_BOUND, assessment.blocking_reasons)

    def test_runner_contract_missing_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        config = _bound_config(self.mirror_root, self.fake_node)
        config["coo"]["dispatch"]["runner"] = {}
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(
                    activation_id,
                    confirmation_id,
                    merged_config=config,
                )
            )
        self.assertIn(BLOCK_BOUNDED_RUNNER_CONTRACT_MISSING, assessment.blocking_reasons)

    def test_recovery_required_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_execution_gate._probe_recovery_required",
            return_value=True,
        ):
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, assessment.blocking_reasons)

    def test_repair_lock_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_execution_gate._probe_repair_lock_held",
            return_value=True,
        ):
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_REPAIR_LOCK_HELD, assessment.blocking_reasons)

    def test_regression_fail_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_execution_gate._probe_regression_clear",
            return_value=False,
        ):
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_REGRESSION_BLOCKED, assessment.blocking_reasons)

    def test_signoff_not_ready_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_execution_gate._probe_signoff_ready",
            return_value=False,
        ):
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_SIGNOFF_NOT_READY, assessment.blocking_reasons)

    def test_cutover_not_ready_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_execution_gate._probe_cutover_ready",
            return_value=False,
        ):
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_CUTOVER_NOT_READY, assessment.blocking_reasons)

    def test_kill_switch_unavailable_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_execution_gate.is_kill_switch_available",
            return_value=False,
        ):
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_KILL_SWITCH_UNAVAILABLE, assessment.blocking_reasons)

    def test_rollback_missing_blocked(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_execution_gate._rollback_present",
            return_value=False,
        ):
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(BLOCK_ROLLBACK_NOT_READY, assessment.blocking_reasons)

    def test_duplicate_evaluation_idempotent(self) -> None:
        activation_id, confirmation_id = self._active_id()
        kwargs = self._gate_kwargs(activation_id, confirmation_id)
        with _gate_patch_context():
            first, recorded_first = run_production_execution_gate(**kwargs)
            second, recorded_second = run_production_execution_gate(**kwargs)
        self.assertTrue(recorded_first)
        self.assertFalse(recorded_second)
        self.assertTrue(second.already_evaluated)
        self.assertEqual(len(_load_execution_gate_records(activation_id, history_dir=self.gate_history_dir)), 1)

    def test_artifact_unchanged(self) -> None:
        activation_id, confirmation_id = self._active_id()
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        loaded_before = load_activation_request(activation_id, store_dir=self.store_dir)
        control_len = len(loaded_before.control_history)
        with _gate_patch_context():
            run_production_execution_gate(**self._gate_kwargs(activation_id, confirmation_id))
        loaded_after = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(_artifact_digest(path), digest_before)
        self.assertEqual(len(loaded_after.control_history), control_len)
        self.assertEqual(loaded_after.state, ACTIVATION_STATE_ACTIVE)

    def test_safe_output(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context():
            output, _ = run_activation_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        sanitized = output
        for allowed in (
            "repository2_execution_attempted: false",
            "production_execution_allowed: false",
            "execution_runtime_disabled: true",
            "mirror_root_trusted:",
            "isolated_mirror_only:",
            "dry_run_verified:",
            "dry_run_fresh:",
            "executor_binding_ready:",
            "bounded_runner_contract_available:",
        ):
            sanitized = sanitized.replace(allowed, "")
        lowered = sanitized.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_no_subprocess_bounded_runner_or_execute(self) -> None:
        activation_id, confirmation_id = self._active_id()
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
        ), patch(
            "agent.coo.dispatch_cli_run.execute_coo_dispatch_run",
            side_effect=AssertionError("no execute"),
        ), _gate_patch_context():
            run_activation_execution_gate(**self._gate_kwargs(activation_id, confirmation_id))

    def test_cli_parser_execution_gate(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "production",
                "activation",
                "execution-gate",
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
        self.assertEqual(
            args.coo_dispatch_production_activation_command,
            "execution-gate",
        )

    def test_success_recommended_action(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context():
            assessment, _ = run_production_execution_gate(
                **self._gate_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(
            assessment.recommended_action,
            ACTION_EXECUTION_GATE_READY_WAIT_FOR_PHASE_14H_3,
        )

    def test_audit_persistence_failure_fail_closed(self) -> None:
        activation_id, confirmation_id = self._active_id()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_execution_gate._atomic_append_execution_gate_record",
            side_effect=ProductionActivationExecutionGateError(
                "Execution gate audit persistence failed."
            ),
        ), self.assertRaises(ProductionActivationExecutionGateError):
            run_production_execution_gate(**self._gate_kwargs(activation_id, confirmation_id))


if __name__ == "__main__":
    unittest.main()
