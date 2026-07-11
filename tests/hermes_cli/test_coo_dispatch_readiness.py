"""Phase 10Z tests — dispatch operator readiness CLI."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import (
    build_dispatch_execution_bundle,
    mark_bundle_consumed,
    read_bundle,
    write_bundle,
)
from agent.coo.dispatch_cli_readiness import (
    STEP_BUNDLE_PERSISTENCE,
    STEP_CONFIRMATION_PERSISTENCE,
    STEP_CONSUME_TRANSACTION,
    STEP_DISPATCH_ENABLEMENT,
    STEP_EXECUTOR_CONFIG,
    STEP_PIPELINE_ROOT_TRUST,
    STEP_PIPELINE_ROOT_ATTESTATION,
    STEP_POLICY_PREFLIGHT,
    STEP_RUNNER_BINDING_STATE,
    evaluate_dispatch_operator_readiness,
    format_dispatch_readiness_summary,
)
from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionRequest,
    DispatchUnlockTokenStore,
    create_dispatch_unlock_token,
)
from agent.coo.gateway_execution_dispatch import prepare_dispatch_for_gateway_ticket
from agent.coo.production_executor_confirmation import (
    REQUIRED_CONFIRMATION_PHRASE,
    create_production_executor_confirmation,
    read_confirmation,
    write_confirmation,
)
from agent.coo.tests.test_execution_dispatch_runtime import _approved_unlock_context
from agent.coo.tests.test_gateway_execution_dispatch import _seed_approved_dispatch_pipeline
from hermes_cli.coo_dispatch import build_coo_dispatch_parser, main


_DEFAULT_DISABLED_CONFIG = {
    "coo": {
        "dispatch": {
            "executor": {
                "enabled": False,
                "allowed_pipeline_roots": [],
            }
        }
    }
}


def _enabled_executor_config(pipeline_root: Path) -> dict:
    return {
        "coo": {
            "dispatch": {
                "executor": {
                    "enabled": True,
                    "allowed_pipeline_roots": [str(pipeline_root)],
                }
            }
        }
    }


class _CooDispatchReadinessFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.pipeline_root = Path(self.tmp.name) / "fake-pipeline"
        self.pipeline_root.mkdir(exist_ok=True)
        self._patches = [
            patch(
                "agent.coo.dispatch_bundle_store.get_hermes_home",
                return_value=self.hermes_home,
            ),
            patch(
                "agent.coo.production_executor_confirmation.get_hermes_home",
                return_value=self.hermes_home,
            ),
            patch(
                "agent.coo.dispatch_runner_binding_state.get_hermes_home",
                return_value=self.hermes_home,
            ),
        ]

    def write_binding_state(self, state: str) -> None:
        (self.hermes_home / "coo").mkdir(parents=True, exist_ok=True)
        binding_path = self.hermes_home / "coo" / "dispatch-runner-binding.json"
        binding_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": state,
                    "updated_at": "2026-07-11T00:00:00+00:00",
                    "operator_id": "test-op",
                    "reason": "test",
                }
            ),
            encoding="utf-8",
        )

    def start(self) -> None:
        for item in self._patches:
            item.start()

    def stop(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self.tmp.cleanup()

    def seed_gateway_bundle_and_confirmation(self) -> dict:
        ctx = _seed_approved_dispatch_pipeline()
        ticket = ctx["ticket"]
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            prepare = prepare_dispatch_for_gateway_ticket(
                ticket.ticket_id,
                requester_id=ticket.requester_id,
                ticket_store=ctx["ticket_store"],
                plan_store=ctx["plan_store"],
                run_store=ctx["run_store"],
                dry_run_request_store=ctx["dry_run_request_store"],
                execute_request_store=ctx["execute_request_store"],
                gate_store=ctx["gate_store"],
                token_store=ctx["token_store"],
                dispatch_request_store=ctx["dispatch_request_store"],
                bundle_dir=self.bundle_dir,
            )
        confirmation = create_production_executor_confirmation(
            ticket_id=ticket.ticket_id,
            plan_id=prepare["unlock_token"]["plan_id"],
            unlock_token_id=prepare["unlock_token"]["token_id"],
            dispatch_request_id=prepare["dispatch_request"]["dispatch_request_id"],
            operator_id="op-readiness",
            operator_name="Readiness Operator",
            confirmation_reason="readiness test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            attested_pipeline_root=str(self.pipeline_root.resolve()),
            persist_to_file=True,
            confirmation_dir=self.confirmation_dir,
        )
        self.write_binding_state("bound")
        return {"ticket": ticket, "prepare": prepare, "confirmation": confirmation}


def _seed_manual_bundle_and_confirmation(
    *,
    bundle_dir: Path,
    confirmation_dir: Path,
    remint_pending: bool = False,
    bundle_consumed: bool = False,
    confirmation_consumed: bool = False,
    confirmation_expired: bool = False,
    confirmation_ticket_mismatch: bool = False,
    attested_pipeline_root: str | None = None,
):
    ticket, plan, dry_run, dry_run_request, execute_request, gate = _approved_unlock_context()
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
        dispatch_request_id="req-readiness-1",
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
    if remint_pending:
        from dataclasses import replace

        snapshot = dict(bundle.snapshot)
        snapshot["_remint_pending_prepare"] = True
        bundle = replace(bundle, snapshot=snapshot)
    write_bundle(bundle, bundle_dir=bundle_dir)
    if bundle_consumed:
        mark_bundle_consumed(ticket.ticket_id, bundle_dir=bundle_dir)

    if attested_pipeline_root is None:
        root = bundle_dir.parent.parent.parent / "fake-pipeline"
        root.mkdir(exist_ok=True)
        attested_pipeline_root = str(root.resolve())

    confirmation_ticket_id = "wrong-ticket" if confirmation_ticket_mismatch else ticket.ticket_id
    confirmation = create_production_executor_confirmation(
        ticket_id=confirmation_ticket_id,
        plan_id=token.plan_id,
        unlock_token_id=token.token_id,
        dispatch_request_id=dispatch_request.dispatch_request_id,
        operator_id="op-readiness",
        operator_name="Readiness Operator",
        confirmation_reason="readiness test",
        confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
        attested_pipeline_root=attested_pipeline_root,
    )
    if confirmation_consumed:
        from dataclasses import replace as dc_replace

        confirmation = dc_replace(
            confirmation,
            consumed=True,
            consumed_at=datetime.now(timezone.utc).isoformat(),
        )
    if confirmation_expired:
        from dataclasses import replace as dc_replace

        confirmation = dc_replace(
            confirmation,
            expires_at="2020-01-01T00:00:00+00:00",
        )
    write_confirmation(confirmation, confirmation_dir=confirmation_dir)
    return ticket, confirmation


class TestDispatchOperatorReadiness(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchReadinessFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_gateway_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def _readiness_kwargs(self, **overrides):
        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        base = dict(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            pipeline_root=str(self.fixture.pipeline_root),
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            merged_config=_enabled_executor_config(self.fixture.pipeline_root),
        )
        base.update(overrides)
        return base

    def test_ready_with_enabled_config_and_valid_persistence(self) -> None:
        summary = evaluate_dispatch_operator_readiness(**self._readiness_kwargs())
        output = format_dispatch_readiness_summary(summary)
        self.assertTrue(summary.ready)
        self.assertTrue(summary.config_valid)
        self.assertTrue(summary.persistence_valid)
        self.assertEqual(summary.preflight, "passed")
        self.assertEqual(summary.checks_failed_count, 0)
        self.assertGreater(summary.checks_passed_count or 0, 0)
        self.assertIn("readiness: ready", output)
        self.assertIn("config_valid: true", output)
        self.assertIn("persistence_valid: true", output)
        self.assertIn("preflight: passed", output)
        self.assertIn("checks_failed_count: 0", output)

    def test_default_disabled_config_not_ready(self) -> None:
        summary = evaluate_dispatch_operator_readiness(
            **self._readiness_kwargs(merged_config=_DEFAULT_DISABLED_CONFIG)
        )
        output = format_dispatch_readiness_summary(summary)
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_DISPATCH_ENABLEMENT,))
        self.assertIn("readiness: not_ready", output)
        self.assertIn(f"failed_steps: {STEP_DISPATCH_ENABLEMENT}", output)

    def test_invalid_executor_config_not_ready(self) -> None:
        summary = evaluate_dispatch_operator_readiness(
            **self._readiness_kwargs(
                merged_config={
                    "coo": {
                        "dispatch": {
                            "executor": {
                                "enabled": True,
                                "allowed_pipeline_roots": [],
                            }
                        }
                    }
                }
            )
        )
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_EXECUTOR_CONFIG,))

    def test_missing_bundle_not_ready(self) -> None:
        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        summary = evaluate_dispatch_operator_readiness(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            pipeline_root=str(self.fixture.pipeline_root),
            bundle_dir=self.fixture.bundle_dir / "missing",
            confirmation_dir=self.fixture.confirmation_dir,
            merged_config=_enabled_executor_config(self.fixture.pipeline_root),
        )
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_BUNDLE_PERSISTENCE,))

    def test_corrupted_bundle_not_ready(self) -> None:
        ticket = self.seeded["ticket"]
        bundle_path = self.fixture.bundle_dir / f"{ticket.ticket_id}.json"
        bundle_path.write_text("{not-json", encoding="utf-8")
        summary = evaluate_dispatch_operator_readiness(**self._readiness_kwargs())
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_BUNDLE_PERSISTENCE,))

    def test_consumed_bundle_not_ready(self) -> None:
        ticket = self.seeded["ticket"]
        mark_bundle_consumed(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        summary = evaluate_dispatch_operator_readiness(**self._readiness_kwargs())
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_CONSUME_TRANSACTION,))

    def test_remint_pending_not_ready(self) -> None:
        ticket = self.seeded["ticket"]
        bundle_path = self.fixture.bundle_dir / f"{ticket.ticket_id}.json"
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        payload["snapshot"]["_remint_pending_prepare"] = True
        bundle_path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_dispatch_operator_readiness(**self._readiness_kwargs())
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_BUNDLE_PERSISTENCE,))

    def test_missing_confirmation_not_ready(self) -> None:
        confirmation = self.seeded["confirmation"]
        confirmation_path = (
            self.fixture.confirmation_dir / f"{confirmation.confirmation_id}.json"
        )
        confirmation_path.unlink()
        summary = evaluate_dispatch_operator_readiness(
            **self._readiness_kwargs(confirmation_id=confirmation.confirmation_id)
        )
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_CONFIRMATION_PERSISTENCE,))

    def test_expired_confirmation_not_ready(self) -> None:
        confirmation = self.seeded["confirmation"]
        confirmation_path = (
            self.fixture.confirmation_dir / f"{confirmation.confirmation_id}.json"
        )
        payload = json.loads(confirmation_path.read_text(encoding="utf-8"))
        payload["expires_at"] = "2020-01-01T00:00:00+00:00"
        confirmation_path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_dispatch_operator_readiness(**self._readiness_kwargs())
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_CONFIRMATION_PERSISTENCE,))

    def test_consumed_confirmation_not_ready(self) -> None:
        confirmation = self.seeded["confirmation"]
        confirmation_path = (
            self.fixture.confirmation_dir / f"{confirmation.confirmation_id}.json"
        )
        payload = json.loads(confirmation_path.read_text(encoding="utf-8"))
        payload["consumed"] = True
        payload["consumed_at"] = datetime.now(timezone.utc).isoformat()
        confirmation_path.write_text(json.dumps(payload), encoding="utf-8")
        summary = evaluate_dispatch_operator_readiness(**self._readiness_kwargs())
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_CONSUME_TRANSACTION,))

    def test_confirmation_bundle_mismatch_not_ready(self) -> None:
        ticket, confirmation = _seed_manual_bundle_and_confirmation(
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            confirmation_ticket_mismatch=True,
        )
        summary = evaluate_dispatch_operator_readiness(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            pipeline_root=str(self.fixture.pipeline_root),
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            merged_config=_enabled_executor_config(self.fixture.pipeline_root),
        )
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_CONFIRMATION_PERSISTENCE,))

    def test_production_root_hard_denied(self) -> None:
        summary = evaluate_dispatch_operator_readiness(
            **self._readiness_kwargs(
                pipeline_root="/opt/data/multi-content-pipeline",
            )
        )
        output = format_dispatch_readiness_summary(summary)
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_PIPELINE_ROOT_TRUST,))
        self.assertNotIn("/opt/data/multi-content-pipeline", output)

    def test_ready_and_not_ready_do_not_consume(self) -> None:
        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.build_pipeline_dispatch_executor",
                side_effect=AssertionError("no factory"),
            ),
            patch(
                "agent.coo.dispatch_cli_run.run_approved_dispatch",
                side_effect=AssertionError("no runner"),
            ),
        ):
            evaluate_dispatch_operator_readiness(**self._readiness_kwargs())
            evaluate_dispatch_operator_readiness(
                **self._readiness_kwargs(merged_config=_DEFAULT_DISABLED_CONFIG)
            )
        bundle = read_bundle(
            ticket.ticket_id,
            bundle_dir=self.fixture.bundle_dir,
            reject_consumed=False,
        )
        self.assertEqual(bundle.consumed_at, "")
        loaded = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
            reject_consumed=False,
        )
        self.assertFalse(loaded.consumed)

    def test_no_factory_runner_or_subprocess_on_ready(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.build_pipeline_dispatch_executor",
                side_effect=AssertionError("no factory"),
            ),
            patch(
                "agent.coo.dispatch_cli_run.run_approved_dispatch",
                side_effect=AssertionError("no runner"),
            ),
        ):
            summary = evaluate_dispatch_operator_readiness(**self._readiness_kwargs())
        self.assertTrue(summary.ready)

    def test_output_does_not_leak_secrets_or_paths(self) -> None:
        prepare = self.seeded["prepare"]
        summary = evaluate_dispatch_operator_readiness(**self._readiness_kwargs())
        output = format_dispatch_readiness_summary(summary)
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE, output)
        self.assertNotIn(prepare["unlock_token"]["token_id"], output)
        self.assertNotIn('"snapshot"', output)
        self.assertNotIn(str(self.fixture.pipeline_root), output)
        self.assertNotIn("/opt/data/multi-content-pipeline", output)

    def test_pipeline_root_mismatch_not_ready(self) -> None:
        other_root = self.fixture.pipeline_root.parent / "other-pipeline"
        other_root.mkdir(exist_ok=True)
        summary = evaluate_dispatch_operator_readiness(
            **self._readiness_kwargs(pipeline_root=str(other_root))
        )
        output = format_dispatch_readiness_summary(summary)
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_PIPELINE_ROOT_ATTESTATION,))
        self.assertIn(f"failed_steps: {STEP_PIPELINE_ROOT_ATTESTATION}", output)
        self.assertNotIn(str(other_root), output)
        self.assertNotIn(str(self.fixture.pipeline_root), output)

    def test_readiness_cli_ready_exit_zero(self) -> None:
        with (
            patch(
                "hermes_cli.config.load_config",
                return_value=_enabled_executor_config(self.fixture.pipeline_root),
            ),
            patch(
                "agent.coo.dispatch_bundle_store.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.production_executor_confirmation.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
        ):
            stdout = io.StringIO()
            with patch.object(sys, "stdout", stdout):
                exit_code = main(
                    [
                        "readiness",
                        "--ticket-id",
                        self.seeded["ticket"].ticket_id,
                        "--confirmation-id",
                        self.seeded["confirmation"].confirmation_id,
                        "--pipeline-root",
                        str(self.fixture.pipeline_root),
                    ]
                )
        self.assertEqual(exit_code, 0)
        self.assertIn("readiness: ready", stdout.getvalue())

    def test_readiness_cli_not_ready_exit_one(self) -> None:
        with (
            patch(
                "hermes_cli.config.load_config",
                return_value=_DEFAULT_DISABLED_CONFIG,
            ),
            patch(
                "agent.coo.dispatch_bundle_store.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
            patch(
                "agent.coo.production_executor_confirmation.get_hermes_home",
                return_value=self.fixture.hermes_home,
            ),
        ):
            stdout = io.StringIO()
            with patch.object(sys, "stdout", stdout):
                exit_code = main(
                    [
                        "readiness",
                        "--ticket-id",
                        self.seeded["ticket"].ticket_id,
                        "--confirmation-id",
                        self.seeded["confirmation"].confirmation_id,
                        "--pipeline-root",
                        str(self.fixture.pipeline_root),
                    ]
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("readiness: not_ready", stdout.getvalue())

    def test_readiness_parser_registered(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "readiness",
                "--ticket-id",
                "t-1",
                "--confirmation-id",
                "c-1",
                "--pipeline-root",
                "/tmp/isolated",
            ]
        )
        self.assertEqual(args.coo_dispatch_command, "readiness")
        self.assertEqual(args.ticket_id, "t-1")
        self.assertEqual(args.confirmation_id, "c-1")
        self.assertEqual(args.pipeline_root, "/tmp/isolated")


class TestDispatchOperatorReadinessManualPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.pipeline_root = Path(self.tmp.name) / "fake-pipeline"
        self.pipeline_root.mkdir(exist_ok=True)
        self.home_patch_bundle = patch(
            "agent.coo.dispatch_bundle_store.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.home_patch_confirm = patch(
            "agent.coo.production_executor_confirmation.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.home_patch_binding = patch(
            "agent.coo.dispatch_runner_binding_state.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.home_patch_bundle.start()
        self.home_patch_confirm.start()
        self.home_patch_binding.start()
        self._write_binding_state("bound")

    def _write_binding_state(self, state: str) -> None:
        (self.hermes_home / "coo").mkdir(parents=True, exist_ok=True)
        binding_path = self.hermes_home / "coo" / "dispatch-runner-binding.json"
        binding_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": state,
                    "updated_at": "2026-07-11T00:00:00+00:00",
                    "operator_id": "test-op",
                    "reason": "test",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.home_patch_binding.stop()
        self.home_patch_confirm.stop()
        self.home_patch_bundle.stop()
        self.tmp.cleanup()

    def test_remint_pending_manual_seed_not_ready(self) -> None:
        ticket, confirmation = _seed_manual_bundle_and_confirmation(
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            remint_pending=True,
        )
        summary = evaluate_dispatch_operator_readiness(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            pipeline_root=str(self.pipeline_root),
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            merged_config=_enabled_executor_config(self.pipeline_root),
        )
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_BUNDLE_PERSISTENCE,))

    def test_consumed_confirmation_manual_seed_not_ready(self) -> None:
        ticket, confirmation = _seed_manual_bundle_and_confirmation(
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            confirmation_consumed=True,
        )
        summary = evaluate_dispatch_operator_readiness(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            pipeline_root=str(self.pipeline_root),
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            merged_config=_enabled_executor_config(self.pipeline_root),
        )
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_CONSUME_TRANSACTION,))

    def test_expired_confirmation_manual_seed_not_ready(self) -> None:
        ticket, confirmation = _seed_manual_bundle_and_confirmation(
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            confirmation_expired=True,
        )
        summary = evaluate_dispatch_operator_readiness(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            pipeline_root=str(self.pipeline_root),
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            merged_config=_enabled_executor_config(self.pipeline_root),
        )
        self.assertFalse(summary.ready)
        self.assertEqual(summary.failed_steps, (STEP_CONFIRMATION_PERSISTENCE,))
