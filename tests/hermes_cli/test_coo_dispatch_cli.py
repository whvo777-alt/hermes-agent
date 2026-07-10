"""CLI skeleton tests for hermes coo dispatch (Phase 10L)."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from hermes_cli import coo_dispatch
from hermes_cli.coo_dispatch import (
    PRODUCTION_ROOT_HARD_DENY,
    assert_pipeline_root_allowed_for_cli,
    build_coo_dispatch_parser,
)


class TestCooDispatchCli(unittest.TestCase):
    def test_confirm_run_parser_accepts_required_args(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "confirm-run",
                "--ticket-id",
                "ticket-1",
                "--plan-id",
                "plan-1",
                "--unlock-token-id",
                "token-1",
                "--dispatch-request-id",
                "req-1",
                "--operator-id",
                "op-1",
                "--operator-name",
                "Operator",
                "--reason",
                "staging validation",
                "--phrase",
                "CONFIRM-REPOSITORY2-EXECUTION",
            ]
        )
        self.assertEqual(args.coo_dispatch_command, "confirm-run")
        self.assertEqual(args.ticket_id, "ticket-1")
        self.assertEqual(args.phrase, "CONFIRM-REPOSITORY2-EXECUTION")

    def test_confirm_run_parser_requires_phrase(self) -> None:
        parser = build_coo_dispatch_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "confirm-run",
                    "--ticket-id",
                    "ticket-1",
                    "--plan-id",
                    "plan-1",
                    "--unlock-token-id",
                    "token-1",
                    "--dispatch-request-id",
                    "req-1",
                    "--operator-id",
                    "op-1",
                    "--operator-name",
                    "Operator",
                    "--reason",
                    "staging validation",
                ]
            )


    def test_run_subcommand_parser_accepts_required_args(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
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
        self.assertEqual(args.coo_dispatch_command, "run")
        self.assertEqual(args.unlock_token_id, "token-1")
        self.assertFalse(args.dry_run)

    def test_production_root_hard_deny_helper(self) -> None:
        self.assertIn("/opt/data/multi-content-pipeline", PRODUCTION_ROOT_HARD_DENY)
        with self.assertRaises(ValueError) as exc:
            assert_pipeline_root_allowed_for_cli("/opt/data/multi-content-pipeline")
        self.assertIn("hard-denied", str(exc.exception))

    def test_production_root_outputs_subdirectory_denied(self) -> None:
        with self.assertRaises(ValueError):
            assert_pipeline_root_allowed_for_cli(
                "/opt/data/multi-content-pipeline/outputs"
            )

    def test_production_root_outputs_audit_subdirectory_denied(self) -> None:
        with self.assertRaises(ValueError):
            assert_pipeline_root_allowed_for_cli(
                "/opt/data/multi-content-pipeline/outputs/audit"
            )

    def test_production_root_internal_symlink_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            denied_root = os.path.join(tmp, "denied-root")
            internal_dir = os.path.join(denied_root, "outputs")
            os.makedirs(internal_dir)
            link_path = os.path.join(tmp, "escape-link")
            os.symlink(internal_dir, link_path)
            with patch.object(
                coo_dispatch,
                "PRODUCTION_ROOT_HARD_DENY",
                (denied_root,),
            ):
                with self.assertRaises(ValueError):
                    assert_pipeline_root_allowed_for_cli(link_path)

    def test_external_pipeline_root_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            external_root = os.path.join(tmp, "fake-pipeline")
            os.makedirs(external_root)
            assert_pipeline_root_allowed_for_cli(external_root)


if __name__ == "__main__":
    unittest.main()
