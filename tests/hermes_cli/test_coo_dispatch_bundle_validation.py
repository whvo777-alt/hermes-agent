"""Phase 11E tests — shared dispatch bundle validation core."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import mark_bundle_consumed, read_bundle
from agent.coo.dispatch_cli_bundle_validation import load_validated_dispatch_bundle_for_cli
from agent.coo.dispatch_cli_confirm import validate_confirm_run_bundle_evidence
from agent.coo.dispatch_cli_status import summarize_dispatch_persistence_status
from agent.coo.gateway_execution_dispatch import prepare_dispatch_for_gateway_ticket
from agent.coo.production_executor_confirmation import (
    REQUIRED_CONFIRMATION_PHRASE,
    create_production_executor_confirmation,
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


class _BundleValidationFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.pipeline_root = Path(self.tmp.name) / "fake-pipeline"
        self.pipeline_root.mkdir()
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
            operator_id="op-bundle-core",
            operator_name="Bundle Core Operator",
            confirmation_reason="bundle validation core test",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
            attested_pipeline_root=str(self.pipeline_root.resolve()),
            persist_to_file=True,
            confirmation_dir=self.confirmation_dir,
        )
        return {"ticket": ticket, "prepare": prepare, "confirmation": confirmation}


class TestDispatchBundleValidationCore(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _BundleValidationFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.fixture.write_binding_state("bound")

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_load_validated_bundle_passes(self) -> None:
        ticket = self.seeded["ticket"]
        bundle = load_validated_dispatch_bundle_for_cli(
            ticket_id=ticket.ticket_id,
            bundle_dir=self.fixture.bundle_dir,
        )
        self.assertEqual(bundle.ticket_id, ticket.ticket_id)

    def test_consumed_bundle_rejected(self) -> None:
        ticket = self.seeded["ticket"]
        mark_bundle_consumed(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        with self.assertRaises(ValueError):
            load_validated_dispatch_bundle_for_cli(
                ticket_id=ticket.ticket_id,
                bundle_dir=self.fixture.bundle_dir,
            )

    def test_validation_core_imports_bundle_validation_helper(self) -> None:
        import agent.coo.dispatch_cli_bundle_validation as bundle_validation
        import agent.coo.dispatch_cli_validation_core as validation_core

        self.assertIs(
            validation_core.load_validated_dispatch_bundle_for_cli,
            bundle_validation.load_validated_dispatch_bundle_for_cli,
        )

    def test_confirm_run_delegates_to_bundle_validation_core(self) -> None:
        import agent.coo.dispatch_cli_confirm as confirm_cli

        bundle = read_bundle(
            self.seeded["ticket"].ticket_id,
            bundle_dir=self.fixture.bundle_dir,
            reject_consumed=False,
        )
        prepare = self.seeded["prepare"]
        with patch.object(
            confirm_cli,
            "load_validated_dispatch_bundle_for_cli",
            return_value=bundle,
        ) as bundle_mock:
            validate_confirm_run_bundle_evidence(
                ticket_id=self.seeded["ticket"].ticket_id,
                plan_id=prepare["unlock_token"]["plan_id"],
                unlock_token_id=prepare["unlock_token"]["token_id"],
                dispatch_request_id=prepare["dispatch_request"]["dispatch_request_id"],
                bundle_dir=self.fixture.bundle_dir,
            )
        bundle_mock.assert_called_once()

    def test_status_preflight_reads_bundle_once(self) -> None:
        ticket = self.seeded["ticket"]
        confirmation = self.seeded["confirmation"]
        with patch(
            "agent.coo.dispatch_cli_bundle_validation.read_bundle",
            wraps=read_bundle,
        ) as read_mock:
            summarize_dispatch_persistence_status(
                ticket_id=ticket.ticket_id,
                confirmation_id=confirmation.confirmation_id,
                pipeline_root=str(self.fixture.pipeline_root),
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                merged_config=_enabled_executor_config(self.fixture.pipeline_root),
            )
        self.assertEqual(read_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
