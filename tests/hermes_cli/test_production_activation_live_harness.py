"""Phase 14H-3C-1 tests — live harness wiring contract."""

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
from agent.coo.production_activation_execution_reservation import load_execution_reservation
from agent.coo.production_activation_live_harness import (
    BLOCK_RESERVATION_NOT_RESERVED,
    BLOCK_RUNNER_FACTORY_UNAVAILABLE,
    BLOCK_RUNTIME_INVOKER_DISABLED,
    BLOCK_TIMEOUT_INVALID,
    ConfigBoundRunnerFactoryAvailability,
    DisabledProductionLiveRuntimeInvoker,
    ProductionActivationLiveHarnessError,
    ProductionLiveRunnerFactory,
    build_live_harness_request,
    compute_harness_key,
    compute_pipeline_root_token,
    evaluate_live_harness_plan,
    load_harness_records,
    probe_harness_audit_store_available,
    run_live_harness_wiring,
    validate_live_harness_argv_contract,
    validate_live_harness_cwd_contract,
    validate_live_harness_env_contract,
)
from agent.coo.production_activation_live_pilot import (
    ACTION_CONTINUE_TO_PHASE_14H_3C_2,
    FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C_2,
    format_live_pilot_preflight_result,
    run_production_activation_live_pilot_preflight,
)
from agent.coo.production_activation_state import (
    ACTIVATION_PLATFORM_CLI,
    ACTIVATION_SCOPE_ONE_SHOT,
)
from agent.coo.production_activation_store import load_activation_request
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


def _gate_patch_context():
    return patch.multiple(
        "agent.coo.production_activation_execution_gate",
        _probe_signoff_ready=lambda **_: True,
        _probe_cutover_ready=lambda **_: True,
        _probe_regression_clear=lambda: True,
        _probe_recovery_required=lambda request: False,
        _probe_repair_lock_held=lambda request: False,
    )


class _EnabledRuntimeInvoker:
    def is_enabled(self) -> bool:
        return True

    def invoke(self, **_: object) -> None:
        raise AssertionError("invoke must not be called in Phase 14H-3C-1")


class _UnavailableRunnerFactory:
    def is_available(self, **_: object) -> bool:
        return False


class TestProductionActivationLiveHarness(unittest.TestCase):
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
        self.harness_history_dir = self.hermes_home / "coo" / "production-live-harness"
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
            from agent.coo.production_activation_store import append_activation_proposal

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
            dispatch_request_id="req-harness-1",
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
            operator_id="op-harness",
            operator_name="Harness Operator",
            confirmation_reason="harness test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            attested_pipeline_root=str(self.mirror_root.resolve()),
        )
        from dataclasses import replace

        confirmation = replace(confirmation, confirmation_id="conf-harness-1")
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

    def _run_live_pilot(self, activation_id: str, confirmation_id: str):
        with _gate_patch_context():
            return run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )

    def _assert_safe_output(self, output: str) -> None:
        sanitized = output
        for allowed in (
            "repository2_execution_attempted: false",
            "production_execution_allowed: false",
            "execution_runtime_invoked: false",
            "runtime_invoked: false",
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
            self.assertNotIn(token, lowered, msg=f"unsafe token {token!r}")

    def test_pipeline_root_token_is_opaque_hash(self) -> None:
        resolved = str(self.mirror_root.resolve())
        token = compute_pipeline_root_token(resolved)
        self.assertEqual(len(token), 64)
        self.assertNotIn("/", token)
        self.assertNotIn("isolated-mirror", token)

    def test_argv_contract_valid_shape(self) -> None:
        argv = [str(self.fake_node), "pipeline.js", "--run-date", "2026-07-13"]
        self.assertTrue(
            validate_live_harness_argv_contract(
                argv,
                node_executable=str(self.fake_node),
            )
        )

    def test_argv_contract_blocks_npm_executable(self) -> None:
        npm = self.tmp_path / "bin" / "npm"
        npm.write_text("#!/bin/sh\n", encoding="utf-8")
        npm.chmod(0o755)
        argv = [str(npm), "pipeline.js", "--run-date", "2026-07-13"]
        self.assertFalse(
            validate_live_harness_argv_contract(
                argv,
                node_executable=str(self.fake_node),
            )
        )

    def test_argv_contract_blocks_extra_args(self) -> None:
        argv = [
            str(self.fake_node),
            "pipeline.js",
            "--run-date",
            "2026-07-13",
            "--publish",
        ]
        self.assertFalse(
            validate_live_harness_argv_contract(
                argv,
                node_executable=str(self.fake_node),
            )
        )

    def test_env_contract_blocks_forbidden_keys(self) -> None:
        self.assertFalse(
            validate_live_harness_env_contract({"DISCORD_TOKEN": "x"})
        )
        self.assertTrue(validate_live_harness_env_contract({"PATH": "/usr/bin"}))

    def test_cwd_contract_blocks_production_root(self) -> None:
        self.assertFalse(
            validate_live_harness_cwd_contract(
                _PRODUCTION_ROOT,
                resolved_mirror=_PRODUCTION_ROOT,
                merged_config=self.merged_config,
            )
        )

    def test_cwd_contract_blocks_missing_structure(self) -> None:
        empty = self.tmp_path / "empty-mirror"
        empty.mkdir()
        self.assertFalse(
            validate_live_harness_cwd_contract(
                str(empty),
                resolved_mirror=str(empty.resolve()),
                merged_config=_enabled_executor_config(empty),
            )
        )

    def test_cwd_contract_blocks_symlink_escape(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir()
        link = self.mirror_root / "escape-link"
        link.symlink_to(outside)
        self.assertFalse(
            validate_live_harness_cwd_contract(
                str(link),
                resolved_mirror=str(self.mirror_root.resolve()),
                merged_config=self.merged_config,
            )
        )

    def test_valid_flow_harness_plan_ready_runtime_not_invoked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context(), patch(
            "subprocess.run", side_effect=AssertionError("subprocess.run blocked")
        ), patch(
            "subprocess.Popen", side_effect=AssertionError("subprocess.Popen blocked")
        ), patch(
            "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
            side_effect=AssertionError("create_bounded_subprocess_runner blocked"),
        ), patch(
            "agent.coo.dispatch_cli_run.execute_coo_dispatch_run",
            side_effect=AssertionError("execute_coo_dispatch_run blocked"),
        ):
            result = self._run_live_pilot(activation_id, confirmation_id)
            output = format_live_pilot_preflight_result(result)
            exit_code = 1
        self.assertEqual(exit_code, 1)
        self.assertTrue(result.harness_ready)
        self.assertTrue(result.runtime_invocation_planned)
        self.assertFalse(result.execution_runtime_invoked)
        self.assertFalse(result.production_execution_allowed)
        self.assertFalse(result.repository2_execution_attempted)
        self.assertEqual(result.failure_reason_code, FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C_2)
        self.assertEqual(result.recommended_action, ACTION_CONTINUE_TO_PHASE_14H_3C_2)
        self.assertTrue(result.harness_argv_contract_valid)
        self.assertTrue(result.harness_cwd_contract_valid)
        self._assert_safe_output(output)
        records = load_harness_records(
            activation_id,
            history_dir=self.harness_history_dir,
        )
        event_types = [record.event_type for record in records]
        self.assertIn("harness_plan_evaluated", event_types)
        self.assertIn("harness_plan_ready", event_types)
        self.assertIn("runtime_blocked_waiting_phase_14h_3c_2", event_types)
        for record in records:
            blob = json.dumps(record.__dict__).lower()
            for token in ("pipeline_root", "argv", "cwd", "env", "phrase"):
                self.assertNotIn(token, blob)

    def test_harness_audit_append_only_idempotent(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            self._run_live_pilot(activation_id, confirmation_id)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        kwargs = {
            "request": request,
            "reservation": reservation,
            "ticket_id": self.ticket_id,
            "confirmation_id": confirmation_id,
            "pipeline_root": str(self.mirror_root),
            "merged_config": self.merged_config,
            "gate_history_dir": self.gate_history_dir,
            "dry_run_history_dir": self.history_dir,
            "harness_history_dir": self.harness_history_dir,
            "now": self._now + timedelta(minutes=8),
        }
        first = run_live_harness_wiring(**kwargs)
        first_count = len(
            load_harness_records(activation_id, history_dir=self.harness_history_dir)
        )
        second = run_live_harness_wiring(**kwargs)
        self.assertEqual(
            len(load_harness_records(activation_id, history_dir=self.harness_history_dir)),
            first_count,
        )
        self.assertTrue(first.harness_ready)
        self.assertTrue(second.harness_ready)
        self.assertTrue(second.already_evaluated)

    def test_wrong_runner_profile_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            self._run_live_pilot(activation_id, confirmation_id)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        with self.assertRaises(ProductionActivationLiveHarnessError):
            build_live_harness_request(
                activation_request_id=activation_id,
                reservation=reservation,
                ticket_id=self.ticket_id,
                confirmation_id=confirmation_id,
                pipeline_root_resolved=str(self.mirror_root.resolve()),
                runner_profile="restricted",
            )

    def test_factory_unavailable_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            self._run_live_pilot(activation_id, confirmation_id)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        result = run_live_harness_wiring(
            request=request,
            reservation=reservation,
            ticket_id=self.ticket_id,
            confirmation_id=confirmation_id,
            pipeline_root=str(self.mirror_root),
            merged_config=self.merged_config,
            gate_history_dir=self.gate_history_dir,
            dry_run_history_dir=self.history_dir,
            harness_history_dir=self.hermes_home / "coo" / "production-live-harness-alt",
            runner_factory=_UnavailableRunnerFactory(),
            now=self._now + timedelta(minutes=8),
        )
        self.assertFalse(result.harness_ready)
        self.assertIn(BLOCK_RUNNER_FACTORY_UNAVAILABLE, result.plan.blocking_reasons)

    def test_runtime_invoker_enabled_unexpectedly_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            self._run_live_pilot(activation_id, confirmation_id)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        result = run_live_harness_wiring(
            request=request,
            reservation=reservation,
            ticket_id=self.ticket_id,
            confirmation_id=confirmation_id,
            pipeline_root=str(self.mirror_root),
            merged_config=self.merged_config,
            gate_history_dir=self.gate_history_dir,
            dry_run_history_dir=self.history_dir,
            harness_history_dir=self.hermes_home / "coo" / "production-live-harness-invoker",
            runtime_invoker=_EnabledRuntimeInvoker(),
            now=self._now + timedelta(minutes=8),
        )
        self.assertFalse(result.harness_ready)
        self.assertIn(BLOCK_RUNTIME_INVOKER_DISABLED, result.plan.blocking_reasons)

    def test_timeout_invalid_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            self._run_live_pilot(activation_id, confirmation_id)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        result = run_live_harness_wiring(
            request=request,
            reservation=reservation,
            ticket_id=self.ticket_id,
            confirmation_id=confirmation_id,
            pipeline_root=str(self.mirror_root),
            merged_config=self.merged_config,
            gate_history_dir=self.gate_history_dir,
            dry_run_history_dir=self.history_dir,
            harness_history_dir=self.hermes_home / "coo" / "production-live-harness-timeout",
            timeout_seconds=4000,
            now=self._now + timedelta(minutes=8),
        )
        self.assertFalse(result.harness_ready)
        self.assertIn(BLOCK_TIMEOUT_INVALID, result.plan.blocking_reasons)

    def test_corrupted_harness_audit_fail_closed(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            self._run_live_pilot(activation_id, confirmation_id)
        path = self.harness_history_dir / f"{activation_id}.json"
        path.write_text("{not-json", encoding="utf-8")
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        with self.assertRaises(ProductionActivationLiveHarnessError):
            run_live_harness_wiring(
                request=request,
                reservation=reservation,
                ticket_id=self.ticket_id,
                confirmation_id=confirmation_id,
                pipeline_root=str(self.mirror_root),
                merged_config=self.merged_config,
                gate_history_dir=self.gate_history_dir,
                dry_run_history_dir=self.history_dir,
                harness_history_dir=self.harness_history_dir,
                now=self._now + timedelta(minutes=9),
            )

    def test_activation_and_reservation_artifacts_unchanged(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        activation_path = self.store_dir / f"{activation_id}.json"
        before_activation = hashlib.sha256(activation_path.read_bytes()).hexdigest()
        with _gate_patch_context():
            self._run_live_pilot(activation_id, confirmation_id)
        reservation_path = self.reservation_dir / f"{activation_id}.json"
        before_reservation = hashlib.sha256(reservation_path.read_bytes()).hexdigest()
        with _gate_patch_context():
            self._run_live_pilot(activation_id, confirmation_id)
        self.assertEqual(
            hashlib.sha256(activation_path.read_bytes()).hexdigest(),
            before_activation,
        )
        self.assertEqual(
            hashlib.sha256(reservation_path.read_bytes()).hexdigest(),
            before_reservation,
        )

    def test_reservation_not_reserved_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            self._run_live_pilot(activation_id, confirmation_id)
        reservation_path = self.reservation_dir / f"{activation_id}.json"
        payload = json.loads(reservation_path.read_text(encoding="utf-8"))
        payload["reservation"]["state"] = "started"
        reservation_path.write_text(json.dumps(payload), encoding="utf-8")
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        request = load_activation_request(activation_id, store_dir=self.store_dir)
        plan = evaluate_live_harness_plan(
            request=request,
            harness_request=build_live_harness_request(
                activation_request_id=activation_id,
                reservation=reservation,
                ticket_id=self.ticket_id,
                confirmation_id=confirmation_id,
                pipeline_root_resolved=str(self.mirror_root.resolve()),
            ),
            reservation=reservation,
            pipeline_root=str(self.mirror_root),
            merged_config=self.merged_config,
            gate_history_dir=self.gate_history_dir,
            dry_run_history_dir=self.history_dir,
            now=self._now + timedelta(minutes=8),
        )
        self.assertIn(BLOCK_RESERVATION_NOT_RESERVED, plan.blocking_reasons)

    def test_default_runtime_invoker_disabled(self) -> None:
        invoker = DisabledProductionLiveRuntimeInvoker()
        self.assertFalse(invoker.is_enabled())

    def test_runner_factory_protocol_contract(self) -> None:
        factory = ConfigBoundRunnerFactoryAvailability()
        self.assertTrue(isinstance(factory, ProductionLiveRunnerFactory))
        self.assertTrue(
            factory.is_available(
                pipeline_root=str(self.mirror_root),
                runner_profile="dispatch",
                timeout_seconds=300,
                merged_config=self.merged_config,
            )
        )

    def test_probe_harness_audit_store_available(self) -> None:
        self.assertTrue(
            probe_harness_audit_store_available(
                history_dir=self.harness_history_dir,
            )
        )

    def test_compute_harness_key_stable(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            result = self._run_live_pilot(activation_id, confirmation_id)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        request = build_live_harness_request(
            activation_request_id=activation_id,
            reservation=reservation,
            ticket_id=self.ticket_id,
            confirmation_id=confirmation_id,
            pipeline_root_resolved=str(self.mirror_root.resolve()),
        )
        key_a = compute_harness_key(request)
        key_b = compute_harness_key(request)
        self.assertEqual(key_a, key_b)
        self.assertTrue(result.harness_ready)

    def test_cli_live_pilot_parser_still_registers(self) -> None:
        parser = build_coo_dispatch_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["live-pilot", "--help"])
