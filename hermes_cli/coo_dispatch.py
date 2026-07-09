"""CLI skeleton: `hermes coo dispatch confirm-run` (Phase 10L).

Creates production executor confirmation records only — no dispatch execution.
"""

from __future__ import annotations

import argparse


def register_cli(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="coo_dispatch_command", required=True)

    confirm_parser = subparsers.add_parser(
        "confirm-run",
        help="Create a production executor confirmation record (no dispatch run)",
    )
    confirm_parser.add_argument("--ticket-id", required=True, help="Execution ticket id")
    confirm_parser.add_argument("--plan-id", required=True, help="Dispatch plan id")
    confirm_parser.add_argument(
        "--unlock-token-id",
        required=True,
        help="Dispatch unlock token id",
    )
    confirm_parser.add_argument(
        "--dispatch-request-id",
        required=True,
        help="Dispatch execution request id",
    )
    confirm_parser.add_argument("--operator-id", required=True, help="Operator identity id")
    confirm_parser.add_argument("--operator-name", required=True, help="Operator display name")
    confirm_parser.add_argument(
        "--reason",
        required=True,
        help="Human-readable reason for confirming real execution",
    )
    confirm_parser.add_argument(
        "--phrase",
        default="CONFIRM-REPOSITORY2-EXECUTION",
        help="Required confirmation phrase",
    )
    confirm_parser.set_defaults(handler=_cmd_confirm_run)


def build_coo_dispatch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes coo dispatch")
    register_cli(parser)
    return parser


def _cmd_confirm_run(args: argparse.Namespace) -> int:
    from agent.coo.production_executor_confirmation import (
        create_production_executor_confirmation,
    )

    confirmation = create_production_executor_confirmation(
        ticket_id=args.ticket_id,
        plan_id=args.plan_id,
        unlock_token_id=args.unlock_token_id,
        dispatch_request_id=args.dispatch_request_id,
        operator_id=args.operator_id,
        operator_name=args.operator_name,
        confirmation_reason=args.reason,
        confirmation_phrase=args.phrase,
    )
    print(f"confirmation_id: {confirmation.confirmation_id}")
    print(f"expires_at: {confirmation.expires_at}")
    print("Dispatch run is NOT executed by this command.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_coo_dispatch_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))
