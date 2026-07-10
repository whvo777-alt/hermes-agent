"""CLI: `hermes coo dispatch` (Phase 10L / 10N / 10Q).

confirm-run creates production executor confirmation records.
run loads persisted bundle + confirmation and dispatches via injected runner only.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Optional

from agent.coo.dispatch_pipeline_root_trust import (
    PRODUCTION_ROOT_HARD_DENY,
    assert_pipeline_root_allowed,
    assert_pipeline_root_trusted,
)


def assert_pipeline_root_allowed_for_cli(pipeline_root: str) -> None:
    """Reject production Repository2 roots and any path inside them."""
    resolved = os.path.realpath(os.path.expanduser(pipeline_root))
    try:
        assert_pipeline_root_allowed(resolved)
    except ValueError as exc:
        raise ValueError(
            f"pipeline_root {pipeline_root!r} is hard-denied for CLI dispatch run"
        ) from exc


def assert_cli_pipeline_root_trusted(pipeline_root: str) -> str:
    """Resolve and validate CLI pipeline_root before any filesystem writes.

    Bundle snapshots do not currently carry a trusted pipeline_root field, so
    validation is limited to symlink-resolved hard-deny policy checks.
    """
    return assert_pipeline_root_trusted(pipeline_root)


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
    confirm_parser.add_argument(
        "--pipeline-root",
        required=True,
        help="Isolated pipeline root to attest for later dispatch (production root hard-denied)",
    )
    confirm_parser.set_defaults(handler=_cmd_confirm_run)

    run_parser = subparsers.add_parser(
        "run",
        help="Run approved dispatch from persisted bundle + confirmation files",
    )
    run_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id (bundle file key)",
    )
    run_parser.add_argument(
        "--unlock-token-id",
        required=True,
        help="Dispatch unlock token id (must match bundle)",
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
        help=(
            "Validate-only: load bundle and confirmation files, run fail-closed "
            "checks, and exit without invoking the production runner or consuming "
            "persisted records"
        ),
    )
    run_parser.set_defaults(handler=_cmd_run)

    status_parser = subparsers.add_parser(
        "status",
        help="Read-only summary of persisted dispatch bundle and confirmation files",
    )
    status_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id (bundle file key)",
    )
    status_parser.add_argument(
        "--confirmation-id",
        default=None,
        help=(
            "Production executor confirmation id; requires --pipeline-root for "
            "read-only policy preflight"
        ),
    )
    status_parser.add_argument(
        "--pipeline-root",
        default=None,
        help=(
            "Isolated pipeline root for read-only policy preflight; requires "
            "--confirmation-id (production root hard-denied)"
        ),
    )
    status_parser.set_defaults(handler=_cmd_status)

    readiness_parser = subparsers.add_parser(
        "readiness",
        help=(
            "Read-only operator readiness check before dispatch run "
            "(config, persistence, policy preflight)"
        ),
    )
    readiness_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id (bundle file key)",
    )
    readiness_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    readiness_parser.add_argument(
        "--pipeline-root",
        required=True,
        help="Isolated pipeline root for readiness preflight (production root hard-denied)",
    )
    readiness_parser.set_defaults(handler=_cmd_readiness)

    config_parser = subparsers.add_parser(
        "config",
        help="Read-only dispatch executor config commands",
    )
    config_subparsers = config_parser.add_subparsers(
        dest="coo_dispatch_config_command",
        required=True,
    )
    validate_parser = config_subparsers.add_parser(
        "validate",
        help="Validate coo.dispatch.executor config without dispatch execution",
    )
    validate_parser.set_defaults(handler=_cmd_config_validate)


def build_coo_dispatch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes coo dispatch")
    register_cli(parser)
    return parser


def _cmd_confirm_run(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_confirm import execute_coo_dispatch_confirm_run

    try:
        confirmation = execute_coo_dispatch_confirm_run(
            ticket_id=args.ticket_id,
            plan_id=args.plan_id,
            unlock_token_id=args.unlock_token_id,
            dispatch_request_id=args.dispatch_request_id,
            operator_id=args.operator_id,
            operator_name=args.operator_name,
            confirmation_reason=args.reason,
            confirmation_phrase=args.phrase,
            pipeline_root=args.pipeline_root,
        )
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"confirmation_id: {confirmation.confirmation_id}")
    print(f"expires_at: {confirmation.expires_at}")
    print("Dispatch run is NOT executed by this command.")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    return run_coo_dispatch_from_args(args)


def _cmd_status(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_status import (
        format_dispatch_status_summary,
        summarize_dispatch_persistence_status,
    )

    try:
        if args.pipeline_root is not None:
            assert_cli_pipeline_root_trusted(args.pipeline_root)
        summary = summarize_dispatch_persistence_status(
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
            pipeline_root=args.pipeline_root,
        )
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_status_summary(summary))
    if summary.preflight == "failed":
        return 1
    return 0


def _cmd_readiness(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_readiness import (
        evaluate_dispatch_operator_readiness,
        format_dispatch_readiness_summary,
    )

    summary = evaluate_dispatch_operator_readiness(
        ticket_id=args.ticket_id,
        confirmation_id=args.confirmation_id,
        pipeline_root=args.pipeline_root,
    )
    print(format_dispatch_readiness_summary(summary))
    return 0 if summary.ready else 1


def _cmd_config_validate(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_config_validate import (
        format_dispatch_executor_config_validation_summary,
        validate_dispatch_executor_config,
    )

    try:
        summary = validate_dispatch_executor_config()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_executor_config_validation_summary(summary))
    return 0


def run_coo_dispatch_from_args(
    args: argparse.Namespace,
    *,
    subprocess_runner=None,
) -> int:
    """Execute dispatch run from parsed CLI args (runner injectable for tests)."""
    from agent.coo.dispatch_cli_run import execute_coo_dispatch_run

    try:
        result = execute_coo_dispatch_run(
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
            unlock_token_id=args.unlock_token_id,
            requester_id=args.requester_id,
            pipeline_root=args.pipeline_root,
            dry_run=bool(args.dry_run),
            subprocess_runner=subprocess_runner,
        )
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"ticket_id: {result.ticket_id}")
    print(f"confirmation_id: {result.confirmation_id}")
    print(f"dispatch_request_id: {result.dispatch_request_id}")
    print(f"status: {result.status}")
    print(f"consumed: {result.consumed}")
    if result.preflight is not None:
        from agent.coo.dispatch_cli_preflight import format_dispatch_preflight_summary

        print(format_dispatch_preflight_summary(result.preflight))
    if result.dry_run_only:
        print("status: preflight-only (--dry-run; runner not invoked, nothing consumed)")
        if result.preflight is not None and not result.preflight.all_passed:
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_coo_dispatch_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))
