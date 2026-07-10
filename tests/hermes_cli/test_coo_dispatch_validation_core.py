"""Phase 11B tests — shared dispatch pre-run validation core."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.coo.dispatch_bundle_store import mark_bundle_consumed, read_bundle
from agent.coo.dispatch_cli_readiness import evaluate_dispatch_operator_readiness
from agent.coo.dispatch_cli_run import execute_coo_dispatch_run
from agent.coo.dispatch_cli_status import summarize_dispatch_persistence_status
from agent.coo.dispatch_cli_validation_core import (
    STEP_BUNDLE_PERSISTENCE,
    STEP_CONFIRMATION_PERSISTENCE,
    STEP_EXECUTOR_CONFIG,
    STEP_PIPELINE_ROOT_ATTESTATION,
    DispatchPreRunValidationFailure,
    validate_dispatch_pre_run,
)
from agent.coo.gateway_execution_dispatch import prepare_dispatch_for_gateway_ticket
from agent.coo.production_executor_confirmation import (
    REQUIRED_CONFIRMATION_PHRASE,
    create_production_executor_confirmation,
    read_confirmation,
)
from agent.coo.tests.test_gateway_execution_dispatch import _seed_approved_dispatch_pipeline


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


class _ValidationCoreFixture:
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
        ]

    def start(self) -> None:
        for item in self._patches:
            item.start()

    def stop(self) -> None:
        for item in reversed(self._patches):
            item.stop()
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
            operator_id="op-core",
            operator_name="Core Operator",
            confirmation_reason="validation core test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            attested_pipeline_root=str(self.pipeline_root.resolve()),
            persist_to_file=True,
            confirmation_dir=self.confirmation_dir,
        )
        return {"ticket": ticket, "prepare": prepare, "confirmation": confirmation}

    def validation_kwargs(self, seeded: dict, **overrides) -> dict:
        ticket = seeded["ticket"]
        confirmation = seeded["confirmation"]
        base = dict(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            pipeline_root=str(self.pipeline_root),
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            merged_config=_enabled_executor_config(self.pipeline_root),
        )
        base.update(overrides)
        return base


class TestDispatchPreRunValidationCore(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _ValidationCoreFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_validate_dispatch_pre_run_passes(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = validate_dispatch_pre_run(**self.fixture.validation_kwargs(self.seeded))
        self.assertTrue(result.preflight.all_passed)
        self.assertEqual(result.bundle.ticket_id, self.seeded["ticket"].ticket_id)
        self.assertEqual(
            result.confirmation.confirmation_id,
            self.seeded["confirmation"].confirmation_id,
        )
        self.assertEqual(result.trusted_pipeline_root, str(self.fixture.pipeline_root.resolve()))

    def test_config_failure(self) -> None:
        with self.assertRaises(DispatchPreRunValidationFailure) as exc:
            validate_dispatch_pre_run(
                **self.fixture.validation_kwargs(
                    self.seeded,
                    merged_config={
                        "coo": {
                            "dispatch": {
                                "executor": {
                                    "enabled": True,
                                    "allowed_pipeline_roots": [],
                                }
                            }
                        }
                    },
                )
            )
        self.assertEqual(exc.exception.step, STEP_EXECUTOR_CONFIG)

    def test_bundle_failure(self) -> None:
        ticket = self.seeded["ticket"]
        mark_bundle_consumed(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        with self.assertRaises(DispatchPreRunValidationFailure) as exc:
            validate_dispatch_pre_run(**self.fixture.validation_kwargs(self.seeded))
        self.assertEqual(exc.exception.step, STEP_BUNDLE_PERSISTENCE)
        self.assertIsInstance(exc.exception.cause_exc, ValueError)

    def test_confirmation_failure(self) -> None:
        confirmation = self.seeded["confirmation"]
        confirmation_path = (
            self.fixture.confirmation_dir / f"{confirmation.confirmation_id}.json"
        )
        payload = json.loads(confirmation_path.read_text(encoding="utf-8"))
        payload["expires_at"] = "2020-01-01T00:00:00+00:00"
        confirmation_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(DispatchPreRunValidationFailure) as exc:
            validate_dispatch_pre_run(**self.fixture.validation_kwargs(self.seeded))
        self.assertEqual(exc.exception.step, STEP_CONFIRMATION_PERSISTENCE)

    def test_attestation_mismatch(self) -> None:
        other_root = self.fixture.pipeline_root.parent / "other-pipeline"
        other_root.mkdir()
        with self.assertRaises(DispatchPreRunValidationFailure) as exc:
            validate_dispatch_pre_run(
                **self.fixture.validation_kwargs(
                    self.seeded,
                    pipeline_root=str(other_root),
                )
            )
        self.assertEqual(exc.exception.step, STEP_PIPELINE_ROOT_ATTESTATION)

    def test_policy_preflight_failure_returns_failed_summary(self) -> None:
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = validate_dispatch_pre_run(
                **self.fixture.validation_kwargs(
                    self.seeded,
                    merged_config=_DEFAULT_DISABLED_CONFIG,
                )
            )
        self.assertFalse(result.preflight.all_passed)
        self.assertIn("policy_enabled", result.preflight.failed_check_names)


class TestDispatchCliPathsUseValidationCore(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _ValidationCoreFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def _mock_validation_result(self) -> MagicMock:
        mock_result = MagicMock()
        mock_result.trusted_pipeline_root = str(self.fixture.pipeline_root.resolve())
        mock_result.bundle = read_bundle(
            self.seeded["ticket"].ticket_id,
            bundle_dir=self.fixture.bundle_dir,
            reject_consumed=False,
        )
        mock_result.confirmation = read_confirmation(
            self.seeded["confirmation"].confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
            reject_consumed=False,
        )
        mock_preflight = MagicMock()
        mock_preflight.all_passed = True
        mock_preflight.passed_check_names = ("policy_enabled",)
        mock_preflight.failed_check_names = ()
        mock_result.preflight = mock_preflight
        return mock_result

    def test_readiness_delegates_to_validation_core(self) -> None:
        mock_result = self._mock_validation_result()
        with patch(
            "agent.coo.dispatch_cli_readiness.validate_dispatch_pre_run",
            return_value=mock_result,
        ) as core_mock:
            summary = evaluate_dispatch_operator_readiness(
                **self.fixture.validation_kwargs(self.seeded)
            )
        core_mock.assert_called_once()
        self.assertTrue(summary.ready)

    def test_run_dry_run_delegates_to_validation_core(self) -> None:
        mock_result = self._mock_validation_result()
        prepare = self.seeded["prepare"]
        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        with (
            patch(
                "agent.coo.dispatch_cli_validation_core.validate_dispatch_pre_run",
                return_value=mock_result,
            ) as core_mock,
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            result = execute_coo_dispatch_run(
                ticket_id=ticket.ticket_id,
                confirmation_id=confirmation.confirmation_id,
                unlock_token_id=prepare["unlock_token"]["token_id"],
                requester_id=ticket.requester_id,
                pipeline_root=str(self.fixture.pipeline_root),
                dry_run=True,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                merged_config=_enabled_executor_config(self.fixture.pipeline_root),
            )
        core_mock.assert_called_once()
        self.assertEqual(result.status, "preflight_passed")

    def test_run_non_dry_delegates_to_validation_core(self) -> None:
        mock_result = self._mock_validation_result()
        prepare = self.seeded["prepare"]
        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]

        def mock_runner(*args, **kwargs):
            return 0, "ok", ""

        with (
            patch(
                "agent.coo.dispatch_cli_validation_core.validate_dispatch_pre_run",
                return_value=mock_result,
            ) as core_mock,
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
                merged_config=_enabled_executor_config(self.fixture.pipeline_root),
                subprocess_runner=mock_runner,
            )
        core_mock.assert_called_once()
        self.assertTrue(result.consumed)

    def test_status_preflight_delegates_to_validation_core(self) -> None:
        mock_result = self._mock_validation_result()
        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        with patch(
            "agent.coo.dispatch_cli_status.validate_dispatch_pre_run",
            return_value=mock_result,
        ) as core_mock:
            summary = summarize_dispatch_persistence_status(
                ticket_id=ticket.ticket_id,
                confirmation_id=confirmation.confirmation_id,
                pipeline_root=str(self.fixture.pipeline_root),
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                merged_config=_enabled_executor_config(self.fixture.pipeline_root),
            )
        core_mock.assert_called_once()
        self.assertEqual(summary.preflight, "passed")

    def test_non_dry_preflight_failure_rejects_before_runner(self) -> None:
        ticket = self.seeded["ticket"]
        prepare = self.seeded["prepare"]
        confirmation = self.seeded["confirmation"]
        runner_calls = {"count": 0}

        def counting_runner(*args, **kwargs):
            runner_calls["count"] += 1
            return 0, "ok", ""

        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch(
                "agent.coo.dispatch_cli_run.run_approved_dispatch",
                side_effect=AssertionError("no runner"),
            ),
            patch(
                "agent.coo.dispatch_cli_run.build_pipeline_dispatch_executor",
                side_effect=AssertionError("no factory"),
            ),
        ):
            with self.assertRaises(ValueError) as exc:
                execute_coo_dispatch_run(
                    ticket_id=ticket.ticket_id,
                    confirmation_id=confirmation.confirmation_id,
                    unlock_token_id=prepare["unlock_token"]["token_id"],
                    requester_id=ticket.requester_id,
                    pipeline_root=str(self.fixture.pipeline_root),
                    bundle_dir=self.fixture.bundle_dir,
                    confirmation_dir=self.fixture.confirmation_dir,
                    merged_config=_DEFAULT_DISABLED_CONFIG,
                    subprocess_runner=counting_runner,
                )
        self.assertIn("disabled", str(exc.exception).lower())
        self.assertEqual(runner_calls["count"], 0)
        bundle = read_bundle(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        self.assertEqual(bundle.consumed_at, "")
        loaded = read_confirmation(
            confirmation.confirmation_id,
            confirmation_dir=self.fixture.confirmation_dir,
        )
        self.assertFalse(loaded.consumed)


if __name__ == "__main__":
    unittest.main()
