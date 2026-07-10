"""Phase 10V tests — confirm-run bundle cross-validation."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_bundle_store import mark_bundle_consumed, read_bundle
from agent.coo.dispatch_cli_confirm import (
    execute_coo_dispatch_confirm_run,
    validate_confirm_run_bundle_evidence,
)
from agent.coo.gateway_execution_dispatch import prepare_dispatch_for_gateway_ticket
from agent.coo.production_executor_confirmation import REQUIRED_CONFIRMATION_PHRASE
from agent.coo.tests.test_gateway_execution_dispatch import _seed_approved_dispatch_pipeline
from hermes_cli.coo_dispatch import main


class _CooDispatchConfirmFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
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

    def seed_bundle(self) -> dict:
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
        return {"ticket": ticket, "prepare": prepare}

    def bundle_path(self, ticket_id: str) -> Path:
        return self.bundle_dir / f"{ticket_id}.json"

    def confirm_args(self, seeded: dict) -> dict:
        ticket = seeded["ticket"]
        prepare = seeded["prepare"]
        return {
            "ticket_id": ticket.ticket_id,
            "plan_id": prepare["unlock_token"]["plan_id"],
            "unlock_token_id": prepare["unlock_token"]["token_id"],
            "dispatch_request_id": prepare["dispatch_request"]["dispatch_request_id"],
            "operator_id": "op-confirm",
            "operator_name": "Confirm Operator",
            "confirmation_reason": "confirm-run validation test",
            "confirmation_phrase": REQUIRED_CONFIRMATION_PHRASE,
            "bundle_dir": self.bundle_dir,
            "confirmation_dir": self.confirmation_dir,
        }


class TestConfirmRunBundleCrossValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _CooDispatchConfirmFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle()

    def tearDown(self) -> None:
        self.fixture.stop()

    def _confirm_kwargs(self, **overrides):
        base = self.fixture.confirm_args(self.seeded)
        base.update(overrides)
        return base

    def _bundle_validate_kwargs(self, **overrides):
        full = self._confirm_kwargs(**overrides)
        return {
            key: full[key]
            for key in (
                "ticket_id",
                "plan_id",
                "unlock_token_id",
                "dispatch_request_id",
                "bundle_dir",
            )
        }

    def _assert_no_confirmation_files(self) -> None:
        self.assertEqual(list(self.fixture.confirmation_dir.glob("*.json")), [])

    def test_valid_bundle_and_matching_ids_create_confirmation(self) -> None:
        confirmation = execute_coo_dispatch_confirm_run(**self._confirm_kwargs())
        self.assertTrue(confirmation.confirmation_id)
        files = list(self.fixture.confirmation_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["ticket_id"], self.seeded["ticket"].ticket_id)

    def test_missing_bundle_rejected(self) -> None:
        with self.assertRaises(KeyError):
            validate_confirm_run_bundle_evidence(
                ticket_id="missing-ticket",
                plan_id="plan-1",
                unlock_token_id="token-1",
                dispatch_request_id="req-1",
                bundle_dir=self.fixture.bundle_dir,
            )
        self._assert_no_confirmation_files()

    def test_corrupted_bundle_rejected(self) -> None:
        ticket = self.seeded["ticket"]
        self.fixture.bundle_path(ticket.ticket_id).write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            validate_confirm_run_bundle_evidence(
                ticket_id=ticket.ticket_id,
                plan_id=self.seeded["prepare"]["unlock_token"]["plan_id"],
                unlock_token_id=self.seeded["prepare"]["unlock_token"]["token_id"],
                dispatch_request_id=self.seeded["prepare"]["dispatch_request"]["dispatch_request_id"],
                bundle_dir=self.fixture.bundle_dir,
            )
        self.assertIn("corrupted", str(exc.exception).lower())
        self._assert_no_confirmation_files()

    def test_consumed_bundle_rejected(self) -> None:
        ticket = self.seeded["ticket"]
        mark_bundle_consumed(ticket.ticket_id, bundle_dir=self.fixture.bundle_dir)
        with self.assertRaises(ValueError) as exc:
            validate_confirm_run_bundle_evidence(**self._bundle_validate_kwargs())
        self.assertIn("consumed", str(exc.exception).lower())
        self._assert_no_confirmation_files()

    def test_remint_pending_rejected(self) -> None:
        ticket = self.seeded["ticket"]
        path = self.fixture.bundle_path(ticket.ticket_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["snapshot"]["_remint_pending_prepare"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            validate_confirm_run_bundle_evidence(**self._bundle_validate_kwargs())
        self.assertIn("pending prepare", str(exc.exception).lower())
        self._assert_no_confirmation_files()

    def test_ticket_id_mismatch_rejected(self) -> None:
        with self.assertRaises(KeyError):
            validate_confirm_run_bundle_evidence(
                **self._bundle_validate_kwargs(ticket_id="wrong-ticket"),
            )
        self._assert_no_confirmation_files()

    def test_plan_id_mismatch_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            validate_confirm_run_bundle_evidence(
                **self._bundle_validate_kwargs(plan_id="wrong-plan"),
            )
        self.assertIn("plan_id", str(exc.exception))
        self._assert_no_confirmation_files()

    def test_unlock_token_id_mismatch_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            validate_confirm_run_bundle_evidence(
                **self._bundle_validate_kwargs(unlock_token_id="wrong-token"),
            )
        self.assertIn("unlock_token_id", str(exc.exception))
        self._assert_no_confirmation_files()

    def test_dispatch_request_id_mismatch_rejected(self) -> None:
        with self.assertRaises(ValueError) as exc:
            validate_confirm_run_bundle_evidence(
                **self._bundle_validate_kwargs(dispatch_request_id="wrong-request"),
            )
        self.assertIn("dispatch_request_id", str(exc.exception))
        self._assert_no_confirmation_files()

    def test_validation_failure_does_not_create_confirmation_file(self) -> None:
        with self.assertRaises(ValueError):
            execute_coo_dispatch_confirm_run(
                **self._confirm_kwargs(plan_id="wrong-plan"),
            )
        self._assert_no_confirmation_files()

    def test_wrong_phrase_rejected_without_confirmation_file(self) -> None:
        with self.assertRaises(ValueError) as exc:
            execute_coo_dispatch_confirm_run(
                **self._confirm_kwargs(confirmation_phrase="WRONG-PHRASE"),
            )
        self.assertIn("confirmation_phrase", str(exc.exception))
        self._assert_no_confirmation_files()

    def test_cli_rejects_missing_bundle(self) -> None:
        kwargs = self._confirm_kwargs()
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            exit_code = main(
                [
                    "confirm-run",
                    "--ticket-id",
                    "missing-ticket",
                    "--plan-id",
                    kwargs["plan_id"],
                    "--unlock-token-id",
                    kwargs["unlock_token_id"],
                    "--dispatch-request-id",
                    kwargs["dispatch_request_id"],
                    "--operator-id",
                    kwargs["operator_id"],
                    "--operator-name",
                    kwargs["operator_name"],
                    "--reason",
                    kwargs["confirmation_reason"],
                    "--phrase",
                    kwargs["confirmation_phrase"],
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("not found", stderr.getvalue().lower())
        self._assert_no_confirmation_files()

    def test_subprocess_not_used(self) -> None:
        kwargs = self._confirm_kwargs()
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            execute_coo_dispatch_confirm_run(**kwargs)
        bundle = read_bundle(
            self.seeded["ticket"].ticket_id,
            bundle_dir=self.fixture.bundle_dir,
        )
        self.assertEqual(bundle.consumed_at, "")

    def test_no_repository2_access(self) -> None:
        import os

        kwargs = self._confirm_kwargs()
        execute_coo_dispatch_confirm_run(**kwargs)
        bundle_path = os.path.realpath(
            str(self.fixture.bundle_path(self.seeded["ticket"].ticket_id))
        )
        production_root = os.path.realpath("/opt/data/multi-content-pipeline")
        try:
            is_inside = os.path.commonpath([bundle_path, production_root]) == production_root
        except ValueError:
            is_inside = False
        self.assertFalse(is_inside)
