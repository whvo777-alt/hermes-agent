"""Phase 14H-3B tests — live pilot reservation and ephemeral permit."""

from __future__ import annotations

import hashlib
import json
import pickle
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
from agent.coo.production_activation_active import activate_production_activation
from agent.coo.production_activation_approval import (
    record_release_approver_approval,
    record_security_reviewer_approval,
)
from agent.coo.production_activation_arm import (
    CONFIRM_PRODUCTION_ACTIVATION_PHRASE,
    arm_production_activation,
)
from agent.coo.production_activation_dry_run import run_production_activation_dry_run
from agent.coo.production_activation_execution_gate import run_production_execution_gate
from agent.coo.production_activation_execution_permit import (
    ActivationExecutionPermit,
    ActivationExecutionPermitError,
    build_activation_execution_permit,
)
from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_RESERVED,
    load_execution_reservation,
)
from agent.coo.production_activation_live_pilot import (
    ACTION_CONTINUE_TO_PHASE_14H_3C_2,
    ACTION_CONTINUE_TO_PHASE_14H_3C,
    FAIL_ACTIVATION_NOT_ACTIVE,
    FAIL_ALREADY_COMPLETED,
    FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C_2,
    FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C,
    FAIL_BUNDLE_CONSUMED,
    FAIL_CONFIRMATION_CONSUMED,
    FAIL_EXECUTION_IN_PROGRESS,
    FAIL_INVALID_EXECUTION_PHRASE,
    FAIL_MIRROR_ROOT_NOT_TRUSTED,
    FAIL_PRODUCTION_ROOT_DENIED,
    FAIL_PUBLISH_NOT_ALLOWED,
    FAIL_REQUIRES_NEW_PROPOSAL,
    FAIL_RESERVATION_IN_PROGRESS,
    FAIL_RESERVATION_SCOPE_CONFLICT,
    ProductionActivationLivePilotError,
    load_preflight_records,
    run_activation_live_pilot,
    run_production_activation_live_pilot_preflight,
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
    mark_confirmation_consumed_file,
    write_confirmation,
)
from agent.coo.tests.test_execution_dispatch_runtime import _approved_unlock_context
from hermes_cli.coo_dispatch import build_coo_dispatch_parser

_TESTED_SHA = "ca269dab24ffceb43ddfeb44c76a5120f987dc46"
_ROLLBACK_SHA = "18a03673739262534847af0296458239511bb7e6"
_ATTESTATION_HASH = "a" * 64
_EXECUTOR_ID = "executor-e"
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
    "requester",
)


def _seed_mirror_structure(mirror_root: Path) -> None:
    (mirror_root / "pipeline.js").write_text("// test\n", encoding="utf-8")
    (mirror_root / "package.json").write_text(
        json.dumps({"scripts": {"start": "node pipeline.js"}}),
        encoding="utf-8",
    )
    for name in ("publishers", "prompts", "config"):
        (mirror_root / name).mkdir(parents=True, exist_ok=True)


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    for sub in (
        "production-activation",
        "production-activation-dry-run",
        "production-execution-gate",
        "production-activation-execution-reservation",
        "production-activation-execution-preflight",
        "production-live-harness",
        "dispatch-bundles",
        "confirmations",
    ):
        (home / "coo" / sub).mkdir(parents=True)
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


def _artifact_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _gate_patch_context():
    return patch.multiple(
        "agent.coo.production_activation_execution_gate",
        _probe_signoff_ready=lambda **_: True,
        _probe_cutover_ready=lambda **_: True,
        _probe_regression_clear=lambda: True,
        _probe_recovery_required=lambda request: False,
        _probe_repair_lock_held=lambda request: False,
    )


class TestProductionActivationExecutionReservation(unittest.TestCase):
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
        _seed_mirror_structure(self.mirror_root)
        self.fake_node = _write_fake_node(self.tmp_path)
        self.store_dir = self.hermes_home / "coo" / "production-activation"
        self.history_dir = self.hermes_home / "coo" / "production-activation-dry-run"
        self.gate_history_dir = self.hermes_home / "coo" / "production-execution-gate"
        self.reservation_dir = (
            self.hermes_home / "coo" / "production-activation-execution-reservation"
        )
        self.preflight_history_dir = (
            self.hermes_home / "coo" / "production-activation-execution-preflight"
        )
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.merged_config = _bound_config(self.mirror_root, self.fake_node)
        _write_binding_state(self.hermes_home)
        self.env_patch = patch.dict("os.environ", {"HERMES_HOME": str(self.hermes_home)})
        self.env_patch.start()
        self._now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        self.ticket_id = ""
        self.unlock_token_id = ""
        self.requester_id = "discord-user-1"

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
        self.requester_id = ticket.requester_id
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
        self.unlock_token_id = token.token_id
        dispatch_request = DispatchExecutionRequest(
            dispatch_request_id="req-live-pilot-1",
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
            operator_id="op-live",
            operator_name="Live Pilot Operator",
            confirmation_reason="live pilot test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            attested_pipeline_root=str(self.mirror_root.resolve()),
        )
        from dataclasses import replace

        confirmation = replace(confirmation, confirmation_id="conf-live-1")
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

    def _ready_gate(self, activation_id: str, confirmation_id: str) -> None:
        with _gate_patch_context():
            run_production_execution_gate(
                activation_request_id=activation_id,
                ticket_id=self.ticket_id,
                confirmation_id=confirmation_id,
                pipeline_root=str(self.mirror_root),
                repo_root=self.repo_root,
                store_dir=self.store_dir,
                history_dir=self.gate_history_dir,
                dry_run_history_dir=self.history_dir,
                bundle_dir=self.bundle_dir,
                confirmation_dir=self.confirmation_dir,
                merged_config=self.merged_config,
                now=self._now + timedelta(minutes=7),
            )

    def _active_setup(self) -> tuple[str, str]:
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
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        bundle_path = self.bundle_dir / f"{self.ticket_id}.json"
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        payload["updated_at"] = loaded.active_at
        bundle_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._ready_gate(activation_id, confirmation_id)
        return activation_id, confirmation_id

    def _pilot_kwargs(self, activation_id: str, confirmation_id: str, **overrides):
        base = {
            "activation_request_id": activation_id,
            "ticket_id": self.ticket_id,
            "confirmation_id": confirmation_id,
            "unlock_token_id": self.unlock_token_id,
            "requester_id": self.requester_id,
            "pipeline_root": str(self.mirror_root),
            "phrase": REQUIRED_CONFIRMATION_PHRASE,
            "repo_root": self.repo_root,
            "store_dir": self.store_dir,
            "gate_history_dir": self.gate_history_dir,
            "dry_run_history_dir": self.history_dir,
            "reservation_dir": self.reservation_dir,
            "preflight_history_dir": self.preflight_history_dir,
            "bundle_dir": self.bundle_dir,
            "confirmation_dir": self.confirmation_dir,
            "merged_config": self.merged_config,
            "now": self._now + timedelta(minutes=8),
        }
        base.update(overrides)
        return base

    def _artifact_path(self, activation_id: str) -> Path:
        return self.store_dir / f"{activation_id}.json"

    def test_active_ready_gate_valid_phrase_creates_reservation(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertTrue(result.preflight_ready)
        self.assertTrue(result.permit_ready)
        self.assertEqual(result.state, RESERVATION_STATE_RESERVED)
        self.assertEqual(result.failure_reason_code, FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C_2)
        self.assertEqual(result.recommended_action, ACTION_CONTINUE_TO_PHASE_14H_3C_2)
        self.assertTrue(result.harness_ready)
        self.assertTrue(result.runtime_invocation_planned)
        self.assertFalse(result.execution_runtime_invoked)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        self.assertIsNotNone(reservation)
        assert reservation is not None
        self.assertEqual(reservation.state, RESERVATION_STATE_RESERVED)
        self.assertEqual(reservation.execution_count, 0)
        self.assertEqual(reservation.max_executions, 1)

    def test_cli_exit_code_one_and_runtime_not_invoked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            output, exit_code = run_activation_live_pilot(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("execution_runtime_invoked: false", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("repository2_execution_attempted: false", output)

    def test_wrong_phrase_zero_mutation(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        artifact_before = _artifact_digest(self._artifact_path(activation_id))
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    phrase="WRONG-PHRASE",
                )
            )
        self.assertEqual(result.failure_reason_code, FAIL_INVALID_EXECUTION_PHRASE)
        self.assertIsNone(
            load_execution_reservation(activation_id, store_dir=self.reservation_dir)
        )
        self.assertEqual(load_preflight_records(activation_id, history_dir=self.preflight_history_dir), [])
        self.assertEqual(_artifact_digest(self._artifact_path(activation_id)), artifact_before)

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
        _write_binding_state(self.hermes_home)
        self._ready_gate(activation_id, confirmation_id)
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_ACTIVATION_NOT_ACTIVE)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_ARMED)

    def test_duplicate_reserved_idempotent(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        kwargs = self._pilot_kwargs(activation_id, confirmation_id)
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(**kwargs)
            second = run_production_activation_live_pilot_preflight(**kwargs)
        self.assertEqual(second.failure_reason_code, FAIL_RESERVATION_IN_PROGRESS)

    def test_existing_started_execution_in_progress(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        path = self.reservation_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["reservation"]["state"] = "started"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_EXECUTION_IN_PROGRESS)

    def test_existing_completed_already_completed(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        path = self.reservation_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["reservation"]["state"] = "completed"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_ALREADY_COMPLETED)

    def test_existing_failed_requires_new_proposal(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        path = self.reservation_dir / f"{activation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["reservation"]["state"] = "failed"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_REQUIRES_NEW_PROPOSAL)

    def test_scope_conflict_different_confirmation(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        kwargs = self._pilot_kwargs(activation_id, confirmation_id)
        kwargs["confirmation_id"] = "other-confirmation"
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(**kwargs)
        self.assertEqual(result.failure_reason_code, FAIL_RESERVATION_SCOPE_CONFLICT)

    def test_bundle_consumed_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        mark_bundle_consumed(self.ticket_id, bundle_dir=self.bundle_dir)
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_BUNDLE_CONSUMED)

    def test_confirmation_consumed_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        mark_confirmation_consumed_file(
            confirmation_id,
            confirmation_dir=self.confirmation_dir,
        )
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertIn(
            result.failure_reason_code,
            {FAIL_CONFIRMATION_CONSUMED, FAIL_BUNDLE_CONSUMED},
        )

    def test_production_root_denied(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    pipeline_root=_PRODUCTION_ROOT,
                )
            )
        self.assertEqual(result.failure_reason_code, FAIL_PRODUCTION_ROOT_DENIED)

    def test_publish_intent_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        bundle_path = self.bundle_dir / f"{self.ticket_id}.json"
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        payload["snapshot"]["publish"] = True
        bundle_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_PUBLISH_NOT_ALLOWED)

    def test_permit_not_reusable_after_context(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        permit = build_activation_execution_permit(
            reservation,
            pipeline_root=str(self.mirror_root),
            store_dir=self.store_dir,
            reservation_dir=self.reservation_dir,
            gate_history_dir=self.gate_history_dir,
            dry_run_history_dir=self.history_dir,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            merged_config=self.merged_config,
            now=self._now + timedelta(minutes=8),
        )
        with _gate_patch_context(), permit:
            self.assertTrue(permit.granted)
        self.assertTrue(permit.consumed)
        with _gate_patch_context(), self.assertRaises(ActivationExecutionPermitError):
            with permit:
                pass

    def test_nested_permit_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        permit = build_activation_execution_permit(
            reservation,
            pipeline_root=str(self.mirror_root),
            store_dir=self.store_dir,
            reservation_dir=self.reservation_dir,
            gate_history_dir=self.gate_history_dir,
            dry_run_history_dir=self.history_dir,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            merged_config=self.merged_config,
            now=self._now + timedelta(minutes=8),
        )
        with _gate_patch_context(), permit, self.assertRaises(ActivationExecutionPermitError):
            with build_activation_execution_permit(
                reservation,
                pipeline_root=str(self.mirror_root),
                store_dir=self.store_dir,
                reservation_dir=self.reservation_dir,
                gate_history_dir=self.gate_history_dir,
                dry_run_history_dir=self.history_dir,
                bundle_dir=self.bundle_dir,
                confirmation_dir=self.confirmation_dir,
                merged_config=self.merged_config,
                now=self._now + timedelta(minutes=8),
            ):
                pass

    def test_permit_not_serializable(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        permit = build_activation_execution_permit(
            reservation,
            pipeline_root=str(self.mirror_root),
        )
        with self.assertRaises(TypeError):
            pickle.dumps(permit)

    def test_activation_artifact_unchanged(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        path = self._artifact_path(activation_id)
        digest_before = _artifact_digest(path)
        loaded_before = load_activation_request(activation_id, store_dir=self.store_dir)
        control_len = len(loaded_before.control_history)
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        loaded_after = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(_artifact_digest(path), digest_before)
        self.assertEqual(len(loaded_after.control_history), control_len)
        self.assertEqual(loaded_after.state, ACTIVATION_STATE_ACTIVE)

    def test_preflight_audit_append_only(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        records = load_preflight_records(
            activation_id,
            history_dir=self.preflight_history_dir,
        )
        self.assertGreaterEqual(len(records), 2)
        event_types = {record.event_type for record in records}
        self.assertIn("reservation_created", event_types)
        self.assertIn("execution_blocked_waiting_phase_14h_3c_2", event_types)

    def test_safe_output(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            output, _ = run_activation_live_pilot(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        sanitized = output
        for allowed in (
            "repository2_execution_attempted: false",
            "production_execution_allowed: false",
            "execution_runtime_invoked: false",
            "runtime_invoked: false",
            "phrase_verified:",
            "harness_ready:",
            "runtime_invocation_planned:",
            "harness_request_valid:",
            "harness_reservation_valid:",
            "harness_permit_valid:",
            "harness_active_valid:",
            "harness_gate_valid:",
            "harness_mirror_valid:",
            "harness_runner_profile_valid:",
            "harness_argv_contract_valid:",
            "harness_cwd_contract_valid:",
            "harness_env_contract_valid:",
            "harness_timeout_valid:",
        ):
            sanitized = sanitized.replace(allowed, "")
        lowered = sanitized.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_no_subprocess_or_execute(self) -> None:
        activation_id, confirmation_id = self._active_setup()
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
            run_activation_live_pilot(**self._pilot_kwargs(activation_id, confirmation_id))

    def test_cli_parser_live_pilot(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "production",
                "activation",
                "live-pilot",
                "--activation-request-id",
                "req-1",
                "--ticket-id",
                "ticket-1",
                "--confirmation-id",
                "conf-1",
                "--unlock-token-id",
                "token-1",
                "--requester-id",
                "req-user",
                "--pipeline-root",
                str(self.mirror_root),
                "--phrase",
                REQUIRED_CONFIRMATION_PHRASE,
            ]
        )
        self.assertEqual(args.coo_dispatch_production_activation_command, "live-pilot")

    def test_reservation_write_failure_no_artifact(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context(), patch(
            "agent.coo.production_activation_execution_reservation._atomic_create_reservation",
            side_effect=Exception("write failed"),
        ):
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(result.failure_reason_code, "reservation_write_failed")
        self.assertIsNone(
            load_execution_reservation(activation_id, store_dir=self.reservation_dir)
        )


if __name__ == "__main__":
    unittest.main()
