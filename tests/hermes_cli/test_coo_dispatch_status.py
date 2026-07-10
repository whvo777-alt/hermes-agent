"""Phase 10S tests — read-only dispatch persistence status CLI."""

from __future__ import annotations

import io
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
    write_bundle,
)
from agent.coo.execution_dispatch_runtime import (
    DispatchExecutionRequest,
    DispatchUnlockTokenStore,
    create_dispatch_unlock_token,
)
from agent.coo.production_executor_confirmation import (
    REQUIRED_CONFIRMATION_PHRASE,
    create_production_executor_confirmation,
    write_confirmation,
)
from agent.coo.tests.test_execution_dispatch_runtime import _approved_unlock_context
from agent.coo.dispatch_cli_status import (
    format_dispatch_status_summary,
    summarize_dispatch_persistence_status,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser, main


def _seed_bundle_and_confirmation(
    *,
    bundle_dir: Path,
    confirmation_dir: Path,
    remint_pending: bool = False,
    bundle_consumed: bool = False,
    confirmation_consumed: bool = False,
    confirmation_expired: bool = False,
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
        dispatch_request_id="req-status-1",
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

    confirmation = create_production_executor_confirmation(
        ticket_id=ticket.ticket_id,
        plan_id=token.plan_id,
        unlock_token_id=token.token_id,
        dispatch_request_id=dispatch_request.dispatch_request_id,
        operator_id="op-status",
        operator_name="Status Operator",
        confirmation_reason="status test",
        confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
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
    return ticket, token, dispatch_request, confirmation


class TestDispatchPersistenceStatus(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.bundle_dir = self.hermes_home / "coo" / "dispatch-bundles"
        self.confirmation_dir = self.hermes_home / "coo" / "confirmations"
        self.home_patch_bundle = patch(
            "agent.coo.dispatch_bundle_store.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.home_patch_confirmation = patch(
            "agent.coo.production_executor_confirmation.get_hermes_home",
            return_value=self.hermes_home,
        )
        self.home_patch_bundle.start()
        self.home_patch_confirmation.start()

    def tearDown(self) -> None:
        self.home_patch_confirmation.stop()
        self.home_patch_bundle.stop()
        self.tmp.cleanup()

    def test_bundle_only_summary(self) -> None:
        ticket, _token, _dispatch_request, _confirmation = _seed_bundle_and_confirmation(
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        summary = summarize_dispatch_persistence_status(
            ticket_id=ticket.ticket_id,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        output = format_dispatch_status_summary(summary)
        self.assertIn(f"ticket_id: {ticket.ticket_id}", output)
        self.assertIn("bundle_consumed: false", output)
        self.assertIn("remint_pending_prepare: false", output)
        self.assertNotIn("confirmation_id:", output)

    def test_bundle_and_confirmation_summary(self) -> None:
        ticket, _token, _dispatch_request, confirmation = _seed_bundle_and_confirmation(
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        summary = summarize_dispatch_persistence_status(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        output = format_dispatch_status_summary(summary)
        self.assertIn(f"confirmation_id: {confirmation.confirmation_id}", output)
        self.assertIn("confirmation_consumed: false", output)
        self.assertIn("confirmation_expired: false", output)

    def test_missing_bundle_rejected(self) -> None:
        with self.assertRaises(KeyError):
            summarize_dispatch_persistence_status(
                ticket_id="missing-ticket",
                bundle_dir=self.bundle_dir,
                confirmation_dir=self.confirmation_dir,
            )

    def test_corrupted_bundle_rejected(self) -> None:
        ticket, _token, _dispatch_request, _confirmation = _seed_bundle_and_confirmation(
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        path = self.bundle_dir / f"{ticket.ticket_id}.json"
        path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError) as exc:
            summarize_dispatch_persistence_status(
                ticket_id=ticket.ticket_id,
                bundle_dir=self.bundle_dir,
                confirmation_dir=self.confirmation_dir,
            )
        self.assertIn("corrupted", str(exc.exception))

    def test_confirmation_id_mismatch_rejected(self) -> None:
        ticket, _token, _dispatch_request, confirmation = _seed_bundle_and_confirmation(
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        other = create_production_executor_confirmation(
            ticket_id="other-ticket",
            plan_id=confirmation.plan_id,
            unlock_token_id=confirmation.unlock_token_id,
            dispatch_request_id=confirmation.dispatch_request_id,
            operator_id="op-2",
            operator_name="Other",
            confirmation_reason="mismatch",
            confirmation_phrase=REQUIRED_CONFIRMATION_PHRASE,
        )
        write_confirmation(other, confirmation_dir=self.confirmation_dir)
        with self.assertRaises(ValueError) as exc:
            summarize_dispatch_persistence_status(
                ticket_id=ticket.ticket_id,
                confirmation_id=other.confirmation_id,
                bundle_dir=self.bundle_dir,
                confirmation_dir=self.confirmation_dir,
            )
        self.assertIn("ticket_id", str(exc.exception))

    def test_consumed_expired_and_remint_pending_summary(self) -> None:
        ticket, _token, _dispatch_request, confirmation = _seed_bundle_and_confirmation(
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            remint_pending=True,
            bundle_consumed=True,
            confirmation_consumed=True,
            confirmation_expired=True,
        )
        summary = summarize_dispatch_persistence_status(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        output = format_dispatch_status_summary(summary)
        self.assertIn("bundle_consumed: true", output)
        self.assertIn("remint_pending_prepare: true", output)
        self.assertIn("confirmation_consumed: true", output)
        self.assertIn("confirmation_expired: true", output)

    def test_output_excludes_secrets_and_snapshot(self) -> None:
        ticket, token, _dispatch_request, confirmation = _seed_bundle_and_confirmation(
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        summary = summarize_dispatch_persistence_status(
            ticket_id=ticket.ticket_id,
            confirmation_id=confirmation.confirmation_id,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        output = format_dispatch_status_summary(summary)
        self.assertNotIn(REQUIRED_CONFIRMATION_PHRASE, output)
        self.assertNotIn(token.token_id, output)
        self.assertNotIn('"snapshot"', output)
        self.assertNotIn("unlock_token", output.lower())

    def test_subprocess_not_used_by_status_cli(self) -> None:
        ticket, _token, _dispatch_request, _confirmation = _seed_bundle_and_confirmation(
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
        )
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            with patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")):
                exit_code = main(
                    [
                        "status",
                        "--ticket-id",
                        ticket.ticket_id,
                    ]
                )
        self.assertEqual(exit_code, 0)

    def test_status_parser_accepts_optional_confirmation(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "status",
                "--ticket-id",
                "ticket-1",
                "--confirmation-id",
                "confirm-1",
            ]
        )
        self.assertEqual(args.coo_dispatch_command, "status")
        self.assertEqual(args.ticket_id, "ticket-1")
        self.assertEqual(args.confirmation_id, "confirm-1")

    def test_status_cli_error_on_missing_bundle(self) -> None:
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            exit_code = main(
                [
                    "status",
                    "--ticket-id",
                    "missing-ticket",
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("error:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
