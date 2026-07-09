"""CLI skeleton tests for hermes coo dispatch (Phase 10L)."""

from __future__ import annotations

import unittest

from hermes_cli.coo_dispatch import build_coo_dispatch_parser


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
            ]
        )
        self.assertEqual(args.coo_dispatch_command, "confirm-run")
        self.assertEqual(args.ticket_id, "ticket-1")
        self.assertEqual(args.phrase, "CONFIRM-REPOSITORY2-EXECUTION")


if __name__ == "__main__":
    unittest.main()
