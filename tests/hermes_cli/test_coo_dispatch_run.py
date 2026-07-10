"""Phase 10Q tests — CLI dispatch run persistence wiring."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import (
    build_dispatch_execution_bundle,
    mark_bundle_consumed,
    read_bundle,
    validate_bundle_for_cli_execution,
    write_bundle,
)
from agent.coo.dispatch_cli_run import (
    execute_coo_dispatch_run,
    hydrate_dispatch_stores_from_bundle,
)
from agent.coo.execution_dispatch_runtime import DispatchExecutionRunStatus
from agent.coo.gateway_execution_dispatch import prepare_dispatch_for_gateway_ticket
from agent.coo.production_executor_confirmation import (
    REQUIRED_CONFIRMATION_PHRASE,
    create_production_executor_confirmation,
    read_confirmation,
    validate_confirmation_for_cli_execution,
)
from agent.coo.production_executor_factory import _TIMEOUT_EXIT_CODE
from agent.coo.tests.test_gateway_execution_dispatch import _seed_approved_dispatch_pipeline
from hermes_cli.coo_dispatch import build_coo_dispatch_parser, run_coo_dispatch_from_args


def _mock_runner_success(argv, cwd, env, timeout):
    return 0, "ok", ""


def _mock_runner_failure(argv, cwd, env, timeout):
    return 1, "", "fake failure"


def _mock_runner_timeout(argv, cwd, env, timeout):
    return _TIMEOUT_EXIT_CODE, "", "timeout"


class _CooDispatchRunFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.pipeline_root = Path(self.tmp.name) / "fake-pipeline"
        self.pipeline_root.mkdir()
        self.home_patch = patch(
            "agent.coo.dispatch_bundle_store.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.confirm_home_patch = patch(
            "agent.coo.production_executor_confirmation.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.cli_home_patch = patch(
            "agent.coo.dispatch_cli_run.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.factory_home_patch = patch(
            "agent.coo.production_executor_factory.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.audit_home_patch = patch(
            "agent.coo.dispatch_execution_audit.get_hermes_home",
            return_value=self.hermes_home,
        )

    def start(self) -> None:
        self.home_patch.start()
        self.confirm_home_patch.start()
        self.cli_home_patch.start()
        self.factory_home_patch.start()
        self.audit_home_patch.start()

    def stop(self) -> None:
        self.audit_home_patch.stop()
        self.factory_home_patch.stop()
        self.cli_home_patch.stop()
        self.confirm_home_patch.stop()
        self.home_patch.stop()
        self.tmp.cleanup()

    def seed_bundle_and_confirmation(self) -> dict:
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
            operator_id="op-cli",
            operator_name="CLI Operator",
            confirmation_reason="cli run test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            persist_to_file=True,
            confirmation_dir=self.confirmation_dir,
        )
        return {
            "ticket": ticket,
            "prepare": prepare,
            "confirmation": confirmation,
        }


class _CooDispatchRunTestBase(unittest.TestCase):
    def _run_kwargs(self, **overrides):
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        base = dict(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        base.update(overrides)
        return base


class TestCooDispatchRunHappyPath(_CooDispatchRunTestBase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_execute_success_consumes_files(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_coo_dispatch_run(
                ticket_id=ticket.ticket_id,
                confirmation_id=confirmation.confirmation_id,
                unlock_token_id=prepare["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                pipeline_root=str(self.fixture.pipeline_root),
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                subprocess_runner=_mock_runner_success,
            )
        self.assertTrue(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.COMPLETED.value)
        with self.assertRaises(ValueError):
            read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        with self.assertRaises(ValueError):
            read_confirmation(
                confirmation.confirmation_id,
                confirmation_dir=self.fixture.confirmation_dir,
            )

    def test_cli_run_success_output_minimal(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        args = argparse.Namespace(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            dry_run=False,
        )
        stdout = io.StringIO()
        with (
            patch.object(sys, "stdout", stdout),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = run_coo_dispatch_from_args(
                args,
                subprocess_runner=_mock_runner_success,
            )
        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("ticket_id:", output)
        self.assertIn("consumed: True", output)
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE, output)
        self.assertNotIn(prepare["unlock_token"]["token_id"], output)

    def test_dry_run_validates_without_consume(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        result = execute_coo_dispatch_run(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            dry_run=True,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            merged_config={"coo": {"dispatch": {"executor": {"enabled": False}}}},
        )
        self.assertFalse(result.consumed)
        self.assertEqual(result.status, "preflight_failed")
        self.assertIsNotNone(result.preflight)
        self.assertFalse(result.preflight.all_passed)
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        self.assertEqual(bundle.consumed_at, "")
        loaded = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        self.assertFalse(loaded.consumed)

    def test_dry_run_preflight_pass_with_enabled_allowlist(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        enabled_config = {
            "coo": {
                "dispatch": {
                    "executor": {
                        "enabled": True,
                        "allowed_pipeline_roots": [str(self.fixture.pipeline_root)],
                    }
                }
            }
        }
        result = execute_coo_dispatch_run(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            dry_run=True,
            bundle_dir=self.fixture.bundle_dir,
            confirmation_dir=self.fixture.confirmation_dir,
            merged_config=enabled_config,
        )
        self.assertEqual(result.status, "preflight_passed")
        self.assertTrue(result.preflight is not None and result.preflight.all_passed)
        self.assertFalse(result.consumed)

    def test_dry_run_cli_output_states_preflight_summary(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        args = argparse.Namespace(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            dry_run=True,
        )
        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout):
            exit_code = run_coo_dispatch_from_args(args)
        self.assertEqual(exit_code, 1)
        output = stdout.getvalue()
        self.assertIn("preflight: failed", output)
        self.assertIn("preflight-only", output)
        self.assertIn("consumed: False", output)
        self.assertIn("failed_checks: policy_enabled", output)

    def test_hydrate_preserves_snapshot_ids_and_status(self) -> None:
        ticket = self.seeded["ticket"]
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        snap = bundle.snapshot
        stores = hydrate_dispatch_stores_from_bundle(bundle)
        self.assertEqual(stores["ticket"].ticket_id, snap["ticket"]["ticket_id"])
        self.assertEqual(stores["ticket"].status.value, snap["ticket"]["status"])
        self.assertEqual(stores["token"].token_id, snap["unlock_token"]["token_id"])
        self.assertEqual(
            stores["dispatch_request"].dispatch_request_id,
            snap["dispatch_request"]["dispatch_request_id"],
        )
        self.assertEqual(stores["gate"].status.value, snap["gate"]["status"])
        self.assertEqual(
            stores["ticket_store"].get(bundle.ticket_id).ticket_id,
            bundle.ticket_id,
        )

    def test_output_excludes_secrets_and_raw_persistence(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        confirmation_path = (
            self.fixture.confirmation_dir / f"{confirmation.confirmation_id}.json"
        )
        confirmation_json = confirmation_path.read_text(encoding="utf-8")
        args = argparse.Namespace(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            dry_run=True,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(sys, "stdout", stdout),
            patch.object(sys, "stderr", stderr),
        ):
            run_coo_dispatch_from_args(args)
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE, combined)
        self.assertNotIn(prepare["unlock_token"]["token_id"], combined)
        self.assertNotIn(json.dumps(bundle.snapshot), combined)
        self.assertNotIn(confirmation_json, combined)
        self.assertNotIn(str(self.fixture.pipeline_root), combined)
        self.assertIn("preflight:", combined)
        for secret_key in ("API_KEY", "PASSWORD", "SECRET", "TOKEN="):
            self.assertNotIn(secret_key, combined)

    def test_partial_bundle_consume_failure_is_fail_closed(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        runner_calls = {"count": 0}

        def counting_runner(*args, **kwargs):
            runner_calls["count"] += 1
            return _mock_runner_success(*args, **kwargs)

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.mark_bundle_consumed",
                side_effect=ValueError("bundle consume failed"),
            ),
        ):
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=counting_runner),
                )
        self.assertIn("bundle consume failed", str(exc.exception))
        self.assertEqual(runner_calls["count"], 1)
        loaded_confirmation = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
            reject_consumed=False,
        )
        self.assertTrue(loaded_confirmation.consumed)
        bundle = read_bundle(
            ticket.ticket_id,
            bundle_dir=self.fixture.bundle_dir,
            reject_consumed=False,
        )
        self.assertEqual(bundle.consumed_at, "")
        runner_calls["count"] = 0
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            with self.assertRaises(ValueError) as replay_exc:
                execute_coo_dispatch_run(
                    **self._run_kwargs(subprocess_runner=counting_runner),
                )
        self.assertIn("consumed", str(replay_exc.exception).lower())
        self.assertEqual(runner_calls["count"], 0)

    def test_partial_bundle_consume_failure_cli_returns_error(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        args = argparse.Namespace(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            unlock_token_id=prepare["unlock_token"]["token_id"],
            requester_id=ticket.requester_id,
            pipeline_root=str(self.fixture.pipeline_root),
            dry_run=False,
        )
        stderr = io.StringIO()
        with (
            patch.object(sys, "stderr", stderr),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.mark_bundle_consumed",
                side_effect=ValueError("bundle consume failed"),
            ),
        ):
            exit_code = run_coo_dispatch_from_args(
                args,
                subprocess_runner=_mock_runner_success,
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("bundle consume failed", stderr.getvalue())

    def test_replay_rejected_after_success(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        runner_calls = {"count": 0}

        def counting_runner(*args, **kwargs):
            runner_calls["count"] += 1
            return _mock_runner_success(*args, **kwargs)

        kwargs = self._run_kwargs(subprocess_runner=counting_runner)
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            execute_coo_dispatch_run(**kwargs)
            with self.assertRaises(ValueError):
                execute_coo_dispatch_run(**kwargs)
        self.assertEqual(runner_calls["count"], 1)


class TestCooDispatchRunFailures(_CooDispatchRunTestBase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def _assert_not_consumed(self) -> None:
        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        self.assertEqual(bundle.consumed_at, "")
        loaded = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        self.assertFalse(loaded.consumed)
        self.assertEqual(loaded.consumed_at, "")

    def test_runner_failure_does_not_consume(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=_mock_runner_failure),
            )
        self.assertFalse(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.FAILED.value)
        self._assert_not_consumed()

    def test_timeout_does_not_consume(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_coo_dispatch_run(
                **self._run_kwargs(subprocess_runner=_mock_runner_timeout),
            )
        self.assertFalse(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.FAILED.value)
        self._assert_not_consumed()

    def test_no_runner_configured_fail_closed(self) -> None:
        with self.assertRaises(ValueError) as exc:
            execute_coo_dispatch_run(**self._run_kwargs(subprocess_runner=None))
        self.assertIn("production runner is not configured", str(exc.exception))
        self._assert_not_consumed()

    def test_factory_failure_does_not_consume(self) -> None:
        with self.assertRaises(ValueError) as exc:
            execute_coo_dispatch_run(
                **self._run_kwargs(
                    pipeline_root="/opt/data/multi-content-pipeline/evil",
                    subprocess_runner=_mock_runner_success,
                ),
            )
        self.assertIn("hard-denied", str(exc.exception))
        self._assert_not_consumed()


class TestCooDispatchBundleRejection(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def _bundle_path(self) -> Path:
        ticket = self.seeded["ticket"]
        return self.fixture.bundle_dir / f"{ticket.ticket_id}.json"

    def test_remint_pending_rejected(self) -> None:
        path = self._bundle_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["snapshot"]["_remint_pending_prepare"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        bundle = read_bundle(
            self.seeded["ticket"].ticket_id,
            bundle_dir=self.fixture.bundle_dir,
            reject_consumed=False,
        )
        with self.assertRaises(ValueError) as exc:
            validate_bundle_for_cli_execution(bundle)
        self.assertIn("pending prepare", str(exc.exception))

    def test_snapshot_missing_block_rejected(self) -> None:
        path = self._bundle_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["snapshot"]["dispatch_request"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            read_bundle(
                self.seeded["ticket"].ticket_id,
                bundle_dir=self.fixture.bundle_dir,
                reject_consumed=False,
            )
        self.assertIn("dispatch_request", str(exc.exception))

    def test_top_level_ticket_mismatch_rejected(self) -> None:
        path = self._bundle_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["ticket_id"] = "wrong-ticket"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_bundle(
                self.seeded["ticket"].ticket_id,
                bundle_dir=self.fixture.bundle_dir,
                reject_consumed=False,
            )

    def test_consumed_bundle_rejected(self) -> None:
        ticket = self.seeded["ticket"]
        mark_bundle_consumed(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        with self.assertRaises(ValueError):
            read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)

    def test_corrupted_json_rejected(self) -> None:
        self._bundle_path().write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_bundle(
                self.seeded["ticket"].ticket_id,
                bundle_dir=self.fixture.bundle_dir,
            )

    def test_outside_hermes_path_rejected(self) -> None:
        outside = Path(self.fixture.tmp.name) / "outside" / "bundles"
        ticket = self.seeded["ticket"]
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        with self.assertRaises(ValueError):
            write_bundle(bundle, bundle_dir=outside)
        self.assertFalse(outside.exists())


class TestCooDispatchConfirmationRejection(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchRunFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def _confirmation_path(self) -> Path:
        confirmation = self.seeded["confirmation"]
        return self.fixture.confirmation_dir / f"{confirmation.confirmation_id}.json"

    def test_ticket_id_mismatch_rejected(self) -> None:
        bundle = read_bundle(
            self.seeded["ticket"].ticket_id,
            bundle_dir=self.fixture.bundle_dir,
        )
        confirmation = self.seeded["confirmation"]
        bad = type(confirmation)(
            **{**confirmation.__dict__, "ticket_id": "wrong-ticket"}
        )
        with self.assertRaises(ValueError):
            validate_confirmation_for_cli_execution(
                bad,
                bundle=bundle,
                expected_confirmation_id=confirmation.confirmation_id,
            )

    def test_dispatch_request_mismatch_rejected(self) -> None:
        bundle = read_bundle(
            self.seeded["ticket"].ticket_id,
            bundle_dir=self.fixture.bundle_dir,
        )
        confirmation = self.seeded["confirmation"]
        bad = type(confirmation)(
            **{**confirmation.__dict__, "dispatch_request_id": "wrong-req"}
        )
        with self.assertRaises(ValueError):
            validate_confirmation_for_cli_execution(
                bad,
                bundle=bundle,
                expected_confirmation_id=confirmation.confirmation_id,
            )

    def test_stored_phrase_in_json_rejected(self) -> None:
        path = self._confirmation_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["confirmation_phrase"] = REQUIRED_CONFIRMATION_PHRASE
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_confirmation(
                self.seeded["confirmation"].confirmation_id,
                confirmation_dir=self.fixture.confirmation_dir,
            )

    def test_phrase_verified_false_rejected(self) -> None:
        path = self._confirmation_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["phrase_verified"] = False
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_confirmation(
                self.seeded["confirmation"].confirmation_id,
                confirmation_dir=self.fixture.confirmation_dir,
            )


class TestCooDispatchCliParser(unittest.TestCase):
    def test_run_parser_requires_ticket_id(self) -> None:
        parser = build_coo_dispatch_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "run",
                    "--unlock-token-id",
                    "token-1",
                    "--confirmation-id",
                    "confirm-1",
                    "--requester-id",
                    "requester-1",
                    "--pipeline-root",
                    "/tmp/fake-pipeline",
                ]
            )

    def test_cli_without_runner_fail_closed(self) -> None:
        fixture = _CooDispatchRunFixture()
        fixture.start()
        try:
            seeded = fixture.seed_bundle_and_confirmation()
            ticket = seeded["ticket"]
            prepare = seeded["prepare"]
            confirmation = seeded["confirmation"]
            stderr = io.StringIO()
            args = argparse.Namespace(
                ticket_id=ticket.ticket_id,
                confirmation_id=confirmation.confirmation_id,
                unlock_token_id=prepare["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                pipeline_root=str(fixture.pipeline_root),
                dry_run=False,
            )
            with patch.object(sys, "stderr", stderr):
                exit_code = run_coo_dispatch_from_args(args)
            self.assertEqual(exit_code, 1)
            self.assertIn("production runner is not configured", stderr.getvalue())
        finally:
            fixture.stop()


if __name__ == "__main__":
    unittest.main()
