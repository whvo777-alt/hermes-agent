"""Phase 14H-3C-2 tests — isolated mirror bounded runtime execution."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
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
from agent.coo.production_activation_execution_reservation import (
    RESERVATION_STATE_COMPLETED,
    RESERVATION_STATE_FAILED,
    RESERVATION_STATE_RESERVED,
    load_execution_reservation,
)
from agent.coo.production_activation_live_pilot import (
    FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C_2,
    format_live_pilot_runtime_result,
    run_activation_live_pilot,
    run_production_activation_live_pilot_preflight,
)
from agent.coo.production_activation_live_runtime import (
    FAIL_RUNTIME_NONZERO,
    FAIL_RUNTIME_PUBLISH_ATTEMPT,
    FAIL_RUNTIME_SOURCE_MUTATION,
    FAIL_RUNTIME_TIMEOUT,
    load_runtime_records,
)
from agent.coo.production_activation_state import (
    ACTIVATION_PLATFORM_CLI,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_SUSPENDED,
)
from agent.coo.production_activation_store import load_activation_request
from agent.coo.production_executor_confirmation import (
    REQUIRED_CONFIRMATION_PHRASE,
    create_production_executor_confirmation,
    write_confirmation,
)
from agent.coo.production_executor_factory import _TIMEOUT_EXIT_CODE
from agent.coo.tests.test_execution_dispatch_runtime import _approved_unlock_context

_TESTED_SHA = "ca269dab24ffceb43ddfeb44c76a5120f987dc46"
_ROLLBACK_SHA = "18a03673739262534847af0296458239511bb7e6"
_ATTESTATION_HASH = "a" * 64
_EXECUTOR_ID = "executor-e"
_PRODUCTION_ROOT = "/opt/data/multi-content-pipeline"


def _seed_mirror_structure(mirror_root: Path) -> None:
    (mirror_root / "pipeline.js").write_text("// test\n", encoding="utf-8")
    (mirror_root / "package.json").write_text(
        json.dumps({"scripts": {"start": "node pipeline.js"}}),
        encoding="utf-8",
    )
    for name in ("publishers", "prompts", "config"):
        (mirror_root / name).mkdir(parents=True, exist_ok=True)


def _write_fake_node(workspace: Path, *, script_body: str) -> Path:
    bin_dir = workspace / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    node_path = bin_dir / "node"
    node_path.write_text(
        f"#!{sys.executable}\n{textwrap.dedent(script_body).lstrip()}",
        encoding="utf-8",
    )
    node_path.chmod(0o755)
    return node_path


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    for sub in (
        "production-activation",
        "production-activation-dry-run",
        "production-execution-gate",
        "production-activation-execution-reservation",
        "production-activation-execution-preflight",
        "production-live-harness",
        "production-live-runtime",
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


class TestProductionActivationLiveRuntime(unittest.TestCase):
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
        self.fake_node = _write_fake_node(
            self.tmp_path,
            script_body=textwrap.dedent(
                """\
                from pathlib import Path
                out = Path("outputs")
                out.mkdir(exist_ok=True)
                (out / "draft.txt").write_text("ok\\n", encoding="utf-8")
                import sys
                sys.exit(0)
                """
            ),
        )
        self.store_dir = self.hermes_home / "coo" / "production-activation"
        self.history_dir = self.hermes_home / "coo" / "production-activation-dry-run"
        self.gate_history_dir = self.hermes_home / "coo" / "production-execution-gate"
        self.reservation_dir = (
            self.hermes_home / "coo" / "production-activation-execution-reservation"
        )
        self.preflight_history_dir = (
            self.hermes_home / "coo" / "production-activation-execution-preflight"
        )
        self.runtime_history_dir = self.hermes_home / "coo" / "production-live-runtime"
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.merged_config = _bound_config(self.mirror_root, self.fake_node)
        _write_binding_state(self.hermes_home)
        self.env_patch = patch.dict("os.environ", {"HERMES_HOME": str(self.hermes_home)})
        self.env_patch.start()
        self._now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        self.ticket_id = ""
        self.unlock_token_id = ""

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
            dispatch_request_id="req-runtime-1",
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
            operator_id="op-runtime",
            operator_name="Runtime Operator",
            confirmation_reason="runtime test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            attested_pipeline_root=str(self.mirror_root.resolve()),
        )
        from dataclasses import replace

        confirmation = replace(confirmation, confirmation_id="conf-runtime-1")
        write_confirmation(confirmation, confirmation_dir=self.confirmation_dir)
        return confirmation.confirmation_id

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

    def _pilot_kwargs(self, activation_id: str, confirmation_id: str, **overrides):
        base = {
            "activation_request_id": activation_id,
            "ticket_id": self.ticket_id,
            "confirmation_id": confirmation_id,
            "unlock_token_id": self.unlock_token_id,
            "requester_id": "discord-user-1",
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

    def test_default_without_execute_flag_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            result = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        self.assertEqual(result.failure_reason_code, FAIL_BLOCKED_WAIT_FOR_PHASE_14H_3C_2)
        self.assertFalse(result.execution_runtime_invoked)

    def test_execute_flag_success_runtime(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context(), patch(
            "agent.coo.dispatch_cli_run.execute_coo_dispatch_run",
            side_effect=AssertionError("consume path blocked"),
        ):
            outcome = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    execute_isolated_mirror=True,
                )
            )
        from agent.coo.production_activation_live_runtime import (
            ProductionActivationLiveRuntimeResult,
        )

        self.assertIsInstance(outcome, ProductionActivationLiveRuntimeResult)
        self.assertTrue(outcome.runtime_invoked)
        self.assertTrue(outcome.isolated_mirror_runtime_invoked)
        self.assertFalse(outcome.original_repository2_execution_attempted)
        self.assertFalse(outcome.production_execution_allowed)
        self.assertTrue(outcome.completed)
        self.assertEqual(outcome.exit_code, 0)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        self.assertEqual(reservation.state, RESERVATION_STATE_COMPLETED)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_SUSPENDED)
        output = format_live_pilot_runtime_result(outcome)
        self.assertIn("consume_attempted: false", output)
        records = load_runtime_records(
            activation_id,
            history_dir=self.runtime_history_dir,
        )
        self.assertTrue(any(r.event_type == "runtime_completed" for r in records))

    def test_nonzero_exit_failed_and_suspended(self) -> None:
        self.fake_node = _write_fake_node(
            self.tmp_path,
            script_body="import sys\nsys.exit(2)\n",
        )
        self.merged_config = _bound_config(self.mirror_root, self.fake_node)
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            outcome = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    execute_isolated_mirror=True,
                )
            )
        from agent.coo.production_activation_live_runtime import (
            ProductionActivationLiveRuntimeResult,
        )

        self.assertIsInstance(outcome, ProductionActivationLiveRuntimeResult)
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure_reason_code, FAIL_RUNTIME_NONZERO)
        reservation = load_execution_reservation(
            activation_id,
            store_dir=self.reservation_dir,
        )
        assert reservation is not None
        self.assertEqual(reservation.state, RESERVATION_STATE_FAILED)
        loaded = load_activation_request(activation_id, store_dir=self.store_dir)
        self.assertEqual(loaded.state, ACTIVATION_STATE_SUSPENDED)

    def test_timeout_exit_code_124(self) -> None:
        self.fake_node = _write_fake_node(
            self.tmp_path,
            script_body="import time\ntime.sleep(5)\n",
        )
        self.merged_config = _bound_config(self.mirror_root, self.fake_node)
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            outcome = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    execute_isolated_mirror=True,
                    runtime_timeout_seconds=1,
                )
            )
        from agent.coo.production_activation_live_runtime import (
            ProductionActivationLiveRuntimeResult,
        )

        self.assertIsInstance(outcome, ProductionActivationLiveRuntimeResult)
        self.assertTrue(outcome.timed_out)
        self.assertEqual(outcome.exit_code, _TIMEOUT_EXIT_CODE)
        self.assertEqual(outcome.failure_reason_code, FAIL_RUNTIME_TIMEOUT)

    def test_publish_output_detected(self) -> None:
        self.fake_node = _write_fake_node(
            self.tmp_path,
            script_body='import sys\nprint("publish")\nsys.exit(0)\n',
        )
        self.merged_config = _bound_config(self.mirror_root, self.fake_node)
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            outcome = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    execute_isolated_mirror=True,
                )
            )
        from agent.coo.production_activation_live_runtime import (
            ProductionActivationLiveRuntimeResult,
        )

        self.assertIsInstance(outcome, ProductionActivationLiveRuntimeResult)
        self.assertEqual(outcome.failure_reason_code, FAIL_RUNTIME_PUBLISH_ATTEMPT)

    def test_source_mutation_failed(self) -> None:
        self.fake_node = _write_fake_node(
            self.tmp_path,
            script_body=textwrap.dedent(
                """\
                from pathlib import Path
                Path("pipeline.js").write_text("// mutated\\n", encoding="utf-8")
                import sys
                sys.exit(0)
                """
            ),
        )
        self.merged_config = _bound_config(self.mirror_root, self.fake_node)
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            outcome = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    execute_isolated_mirror=True,
                )
            )
        from agent.coo.production_activation_live_runtime import (
            ProductionActivationLiveRuntimeResult,
        )

        self.assertIsInstance(outcome, ProductionActivationLiveRuntimeResult)
        self.assertEqual(outcome.failure_reason_code, FAIL_RUNTIME_SOURCE_MUTATION)

    def test_duplicate_execute_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    execute_isolated_mirror=True,
                )
            )
            blocked = run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    execute_isolated_mirror=True,
                )
            )
        from agent.coo.production_activation_live_pilot import (
            ProductionActivationLivePilotPreflightResult,
        )

        self.assertIsInstance(blocked, ProductionActivationLivePilotPreflightResult)
        self.assertIn(
            blocked.failure_reason_code,
            {"activation_execution_already_completed", "execution_in_progress"},
        )

    def test_production_root_blocked(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        kwargs = self._pilot_kwargs(activation_id, confirmation_id)
        kwargs["pipeline_root"] = _PRODUCTION_ROOT
        kwargs["execute_isolated_mirror"] = True
        with _gate_patch_context():
            outcome = run_production_activation_live_pilot_preflight(**kwargs)
        from agent.coo.production_activation_live_pilot import (
            ProductionActivationLivePilotPreflightResult,
        )

        self.assertIsInstance(outcome, ProductionActivationLivePilotPreflightResult)

    def test_wrong_phrase_zero_mutation(self) -> None:
        activation_id, confirmation_id = self._active_setup()
        path = self.reservation_dir / f"{activation_id}.json"
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(activation_id, confirmation_id)
            )
        digest_before = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
        with _gate_patch_context():
            run_production_activation_live_pilot_preflight(
                **self._pilot_kwargs(
                    activation_id,
                    confirmation_id,
                    phrase="wrong-phrase",
                    execute_isolated_mirror=True,
                )
            )
        if path.exists():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest_before)
