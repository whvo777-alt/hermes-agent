"""CLI skeleton: `hermes coo dispatch` (Phase 10L / 10N).

confirm-run creates production executor confirmation records only.
run is parser/validation skeleton only — no dispatch execution.
"""

from __future__ import annotations

import argparse
import os

PRODUCTION_ROOT_HARD_DENY = (
    "/opt/data/multi-content-pipeline",
)


def assert_pipeline_root_allowed_for_cli(pipeline_root: str) -> None:
    """Reject production Repository2 roots and any path inside them."""
    candidate = os.path.realpath(os.path.expanduser(pipeline_root))
    for denied in PRODUCTION_ROOT_HARD_DENY:
        production_root = os.path.realpath(denied)
        try:
            is_inside = os.path.commonpath([candidate, production_root]) == production_root
        except ValueError:
            is_inside = False
        if is_inside:
            raise ValueError(
                f"pipeline_root {pipeline_root!r} is hard-denied for CLI dispatch run"
            )


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
        required=True,
        help='Operator-typed confirmation phrase (must be exactly "CONFIRM-REPOSITORY2-EXECUTION")',
    )
    confirm_parser.set_defaults(handler=_cmd_confirm_run)

    run_parser = subparsers.add_parser(
        "run",
        help="Dispatch run skeleton (validation only — execution not implemented)",
    )
    run_parser.add_argument(
        "--unlock-token-id",
        required=True,
        help="Dispatch unlock token id",
    )
    run_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    run_parser.add_argument(
        "--requester-id",
        required=True,
        help="Ticket requester id authorized for dispatch",
    )
    run_parser.add_argument(
        "--pipeline-root",
        required=True,
        help="Isolated pipeline root for dispatch (production root hard-denied)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs only; do not execute dispatch",
    )
    run_parser.set_defaults(handler=_cmd_run)


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


def _cmd_run(args: argparse.Namespace) -> int:
    assert_pipeline_root_allowed_for_cli(args.pipeline_root)
    print("Dispatch run is NOT executed by this command (skeleton only).")
    print(f"unlock_token_id: {args.unlock_token_id}")
    print(f"confirmation_id: {args.confirmation_id}")
    print(f"requester_id: {args.requester_id}")
    print(f"pipeline_root: {args.pipeline_root}")
    print(f"dry_run: {args.dry_run}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_coo_dispatch_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))
