"""CLI: `hermes coo dispatch` (Phase 10L / 10N / 10Q).

confirm-run creates production executor confirmation records.
run loads persisted bundle + confirmation and dispatches via injected runner only.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, Optional

from agent.coo.dispatch_pipeline_root_trust import (
    PRODUCTION_ROOT_HARD_DENY,
    assert_cli_pipeline_root_trusted,
    assert_pipeline_root_allowed_for_cli,
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

    audit_parser = subparsers.add_parser(
        "audit",
        help="Read-only dispatch execution audit commands",
    )
    audit_subparsers = audit_parser.add_subparsers(
        dest="coo_dispatch_audit_command",
        required=True,
    )
    audit_show_parser = audit_subparsers.add_parser(
        "show",
        help="Show a safe summary for a persisted dispatch execution audit record",
    )
    audit_show_parser.add_argument(
        "--dispatch-run-id",
        required=True,
        help="Dispatch execution run id (audit file key)",
    )
    audit_show_parser.set_defaults(handler=_cmd_audit_show)

    audit_list_parser = audit_subparsers.add_parser(
        "list",
        help="List safe summaries for persisted dispatch execution audit records",
    )
    audit_list_parser.set_defaults(handler=_cmd_audit_list)

    audit_find_parser = audit_subparsers.add_parser(
        "find",
        help="Find audit records for an execution ticket id",
    )
    audit_find_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id to match against audit snapshot evidence",
    )
    audit_find_parser.set_defaults(handler=_cmd_audit_find)

    evidence_parser = subparsers.add_parser(
        "evidence",
        help="Read-only dispatch execution evidence commands",
    )
    evidence_subparsers = evidence_parser.add_subparsers(
        dest="coo_dispatch_evidence_command",
        required=True,
    )
    evidence_show_parser = evidence_subparsers.add_parser(
        "show",
        help="Show a safe summary for an execution attempt",
    )
    evidence_show_parser.add_argument(
        "--execution-attempt-id",
        required=True,
        help="Execution attempt id (evidence file key)",
    )
    evidence_show_parser.set_defaults(handler=_cmd_evidence_show)

    evidence_find_parser = evidence_subparsers.add_parser(
        "find",
        help="Find execution attempts for an execution ticket id",
    )
    evidence_find_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id to match against audit evidence",
    )
    evidence_find_parser.set_defaults(handler=_cmd_evidence_find)

    consume_parser = subparsers.add_parser(
        "consume",
        help="Read-only dispatch consume transaction commands",
    )
    consume_subparsers = consume_parser.add_subparsers(
        dest="coo_dispatch_consume_command",
        required=True,
    )
    consume_status_parser = consume_subparsers.add_parser(
        "status",
        help="Show safe consume status for bundle + confirmation pair",
    )
    consume_status_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id (bundle file key)",
    )
    consume_status_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    consume_status_parser.set_defaults(handler=_cmd_consume_status)

    consume_recovery_parser = consume_subparsers.add_parser(
        "recovery",
        help="Read-only recovery assessment for bundle + confirmation consume pair",
    )
    consume_recovery_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id (bundle file key)",
    )
    consume_recovery_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    consume_recovery_parser.set_defaults(handler=_cmd_consume_recovery)

    consume_repair_parser = consume_subparsers.add_parser(
        "repair",
        help="Consume repair dry-run and apply commands",
    )
    consume_repair_subparsers = consume_repair_parser.add_subparsers(
        dest="coo_dispatch_consume_repair_command",
        required=True,
    )
    consume_repair_dry_run_parser = consume_repair_subparsers.add_parser(
        "dry-run",
        help="Evaluate repair eligibility without mutating persisted state",
    )
    consume_repair_dry_run_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id (bundle file key)",
    )
    consume_repair_dry_run_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    consume_repair_dry_run_parser.add_argument(
        "--operator-id",
        required=True,
        help="Operator identity id for dry-run eligibility evaluation",
    )
    consume_repair_dry_run_parser.add_argument(
        "--operator-name",
        required=True,
        help="Operator display name for dry-run eligibility evaluation",
    )
    consume_repair_dry_run_parser.add_argument(
        "--reason",
        required=True,
        help="Human-readable reason for evaluating repair eligibility",
    )
    consume_repair_dry_run_parser.set_defaults(handler=_cmd_consume_repair_dry_run)

    consume_repair_apply_parser = consume_repair_subparsers.add_parser(
        "apply",
        help="Apply the eligible consume repair action (prepared cleanup or partial forward-complete)",
    )
    consume_repair_apply_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id (bundle file key)",
    )
    consume_repair_apply_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    consume_repair_apply_parser.add_argument(
        "--operator-id",
        required=True,
        help="Operator identity id for repair apply",
    )
    consume_repair_apply_parser.add_argument(
        "--operator-name",
        required=True,
        help="Operator display name for repair apply",
    )
    consume_repair_apply_parser.add_argument(
        "--reason",
        required=True,
        help="Human-readable reason for repair apply",
    )
    consume_repair_apply_parser.add_argument(
        "--phrase",
        required=True,
        help='Operator repair phrase (must be exactly "CONFIRM-CONSUME-REPAIR")',
    )
    consume_repair_apply_parser.set_defaults(handler=_cmd_consume_repair_apply)

    consume_repair_audit_parser = consume_repair_subparsers.add_parser(
        "audit",
        help="Read-only consume repair audit inspection",
    )
    consume_repair_audit_subparsers = consume_repair_audit_parser.add_subparsers(
        dest="coo_dispatch_consume_repair_audit_command",
        required=True,
    )
    consume_repair_audit_show_parser = consume_repair_audit_subparsers.add_parser(
        "show",
        help="Show one consume repair audit record",
    )
    consume_repair_audit_show_parser.add_argument(
        "--repair-attempt-id",
        required=True,
        help="Consume repair attempt id",
    )
    consume_repair_audit_show_parser.set_defaults(handler=_cmd_consume_repair_audit_show)

    consume_repair_audit_list_parser = consume_repair_audit_subparsers.add_parser(
        "list",
        help="List consume repair audit records newest-first",
    )
    consume_repair_audit_list_parser.add_argument(
        "--ticket-id",
        default="",
        help="Optional ticket id filter",
    )
    consume_repair_audit_list_parser.set_defaults(handler=_cmd_consume_repair_audit_list)

    consume_repair_lock_parser = consume_repair_subparsers.add_parser(
        "lock",
        help="Read-only consume repair lock diagnosis",
    )
    consume_repair_lock_subparsers = consume_repair_lock_parser.add_subparsers(
        dest="coo_dispatch_consume_repair_lock_command",
        required=True,
    )
    consume_repair_lock_status_parser = consume_repair_lock_subparsers.add_parser(
        "status",
        help="Probe consume repair lock state for a consume pair",
    )
    consume_repair_lock_status_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id (bundle file key)",
    )
    consume_repair_lock_status_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    consume_repair_lock_status_parser.set_defaults(handler=_cmd_consume_repair_lock_status)

    operator_parser = subparsers.add_parser(
        "operator",
        help="Read-only operator guidance commands",
    )
    operator_subparsers = operator_parser.add_subparsers(
        dest="coo_dispatch_operator_command",
        required=True,
    )
    operator_runbook_parser = operator_subparsers.add_parser(
        "runbook",
        help="Show read-only operator runbook for a consume pair",
    )
    operator_runbook_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id (bundle file key)",
    )
    operator_runbook_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    operator_runbook_parser.set_defaults(handler=_cmd_operator_runbook)

    repository_parser = subparsers.add_parser(
        "repository",
        help="Read-only Repository2 attestation commands",
    )
    repository_subparsers = repository_parser.add_subparsers(
        dest="coo_dispatch_repository_command",
        required=True,
    )
    repository_attest_parser = repository_subparsers.add_parser(
        "attest",
        help=(
            "Read-only attestation of Repository2 production root identity "
            "and structure (stat/read/hash only)"
        ),
    )
    repository_attest_parser.add_argument(
        "--repository-root",
        required=True,
        help=(
            "Absolute path to the official Repository2 production root "
            "(read-only; production execution remains hard-denied)"
        ),
    )
    repository_attest_parser.set_defaults(handler=_cmd_repository_attest)

    production_parser = subparsers.add_parser(
        "production",
        help="Read-only production readiness review commands",
    )
    production_subparsers = production_parser.add_subparsers(
        dest="coo_dispatch_production_command",
        required=True,
    )
    production_readiness_parser = production_subparsers.add_parser(
        "readiness",
        help="Evaluate dispatch production readiness without mutating state",
    )
    production_readiness_parser.set_defaults(handler=_cmd_production_readiness)

    production_signoff_parser = production_subparsers.add_parser(
        "sign-off",
        help="Evaluate read-only production dispatch sign-off readiness",
    )
    production_signoff_parser.set_defaults(handler=_cmd_production_signoff)

    pilot_parser = subparsers.add_parser(
        "pilot",
        help="Isolated operational dispatch pilot (production root hard-denied)",
    )
    pilot_subparsers = pilot_parser.add_subparsers(
        dest="coo_dispatch_pilot_command",
        required=True,
    )
    pilot_readiness_parser = pilot_subparsers.add_parser(
        "readiness",
        help="Evaluate isolated operational pilot readiness without dispatch",
    )
    pilot_readiness_parser.add_argument(
        "--pipeline-root",
        default=None,
        help="Isolated pipeline root to trust-check (production root hard-denied)",
    )
    pilot_readiness_parser.add_argument(
        "--ticket-id",
        default=None,
        help="Execution ticket id for operator readiness cross-check",
    )
    pilot_readiness_parser.add_argument(
        "--confirmation-id",
        default=None,
        help="Production executor confirmation id for operator readiness cross-check",
    )
    pilot_readiness_parser.set_defaults(handler=_cmd_pilot_readiness)

    pilot_run_parser = pilot_subparsers.add_parser(
        "run",
        help=(
            "Run isolated operational pilot dispatch after sign-off and "
            "readiness gates (production root hard-denied)"
        ),
    )
    pilot_run_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id (bundle file key)",
    )
    pilot_run_parser.add_argument(
        "--unlock-token-id",
        required=True,
        help="Dispatch unlock token id (must match bundle)",
    )
    pilot_run_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    pilot_run_parser.add_argument(
        "--requester-id",
        required=True,
        help="Ticket requester id authorized for dispatch",
    )
    pilot_run_parser.add_argument(
        "--pipeline-root",
        required=True,
        help="Isolated pipeline root for pilot dispatch (production root hard-denied)",
    )
    pilot_run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate pilot gates and preflight only; do not invoke runner",
    )
    pilot_run_parser.set_defaults(handler=_cmd_pilot_run)

    enablement_parser = subparsers.add_parser(
        "enablement",
        help="Read-only production runner enablement checks",
    )
    enablement_subparsers = enablement_parser.add_subparsers(
        dest="coo_dispatch_enablement_command",
        required=True,
    )
    enablement_check_parser = enablement_subparsers.add_parser(
        "check",
        help="Assess whether dispatch executor enablement can proceed without binding a runner",
    )
    enablement_check_parser.set_defaults(handler=_cmd_enablement_check)

    binding_parser = subparsers.add_parser(
        "binding",
        help="Read-only and operator-controlled runner binding state commands",
    )
    binding_subparsers = binding_parser.add_subparsers(
        dest="coo_dispatch_binding_command",
        required=True,
    )
    binding_status_parser = binding_subparsers.add_parser(
        "status",
        help="Show safe summary of persisted runner binding state",
    )
    binding_status_parser.set_defaults(handler=_cmd_binding_status)

    binding_stage_parser = binding_subparsers.add_parser(
        "stage",
        help="Transition runner binding from unbound to staged",
    )
    binding_stage_parser.add_argument("--operator-id", required=True, help="Operator identity id")
    binding_stage_parser.add_argument(
        "--reason",
        required=True,
        help="Human-readable reason for staging runner binding",
    )
    binding_stage_parser.set_defaults(handler=_cmd_binding_stage)

    binding_reset_parser = binding_subparsers.add_parser(
        "reset",
        help="Transition runner binding from staged to unbound",
    )
    binding_reset_parser.add_argument("--operator-id", required=True, help="Operator identity id")
    binding_reset_parser.add_argument(
        "--reason",
        required=True,
        help="Human-readable reason for resetting runner binding",
    )
    binding_reset_parser.set_defaults(handler=_cmd_binding_reset)

    binding_bind_parser = binding_subparsers.add_parser(
        "bind",
        help="Transition runner binding from staged to bound (state record only)",
    )
    binding_bind_parser.add_argument("--operator-id", required=True, help="Operator identity id")
    binding_bind_parser.add_argument(
        "--reason",
        required=True,
        help="Human-readable reason for binding runner state",
    )
    binding_bind_parser.set_defaults(handler=_cmd_binding_bind)


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


def _cmd_audit_show(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_audit import (
        format_dispatch_audit_summary,
        summarize_dispatch_execution_audit,
    )

    try:
        summary = summarize_dispatch_execution_audit(args.dispatch_run_id)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_audit_summary(summary))
    return 0


def _cmd_audit_list(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_audit import (
        format_dispatch_audit_list,
        list_dispatch_execution_audits,
    )

    try:
        entries = list_dispatch_execution_audits()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_audit_list(entries))
    return 0


def _cmd_audit_find(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_audit import (
        find_dispatch_execution_audits_for_ticket,
        format_dispatch_audit_find,
    )

    try:
        entries = find_dispatch_execution_audits_for_ticket(args.ticket_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_audit_find(args.ticket_id, entries))
    return 0


def _cmd_evidence_show(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_evidence import (
        format_dispatch_evidence_summary,
        summarize_dispatch_evidence_attempt,
    )

    try:
        summary = summarize_dispatch_evidence_attempt(args.execution_attempt_id)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_evidence_summary(summary))
    return 0


def _cmd_evidence_find(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_evidence import (
        find_dispatch_evidence_attempts_for_ticket,
        format_dispatch_evidence_find,
    )

    try:
        entries = find_dispatch_evidence_attempts_for_ticket(args.ticket_id)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_evidence_find(args.ticket_id, entries))
    return 0


def _cmd_consume_status(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_consume_status import (
        format_dispatch_consume_status_summary,
        summarize_dispatch_consume_status,
    )

    try:
        summary = summarize_dispatch_consume_status(
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_consume_status_summary(summary))
    return 0


def _cmd_consume_recovery(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_consume_recovery import (
        assess_dispatch_consume_recovery,
        format_dispatch_consume_recovery_assessment,
    )

    try:
        assessment = assess_dispatch_consume_recovery(
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_consume_recovery_assessment(assessment))
    return 0


def _cmd_consume_repair_dry_run(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_consume_repair import (
        format_dispatch_consume_repair_eligibility,
        run_dispatch_consume_repair_dry_run,
    )

    try:
        eligibility, exit_code = run_dispatch_consume_repair_dry_run(
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
            operator_id=args.operator_id,
            operator_name=args.operator_name,
            reason=args.reason,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_consume_repair_eligibility(eligibility))
    return exit_code


def _cmd_consume_repair_apply(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_consume_repair import (
        format_dispatch_consume_repair_apply_result,
        run_dispatch_consume_repair_apply,
    )

    try:
        result, exit_code = run_dispatch_consume_repair_apply(
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
            operator_id=args.operator_id,
            operator_name=args.operator_name,
            reason=args.reason,
            phrase=args.phrase,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_consume_repair_apply_result(result))
    return exit_code


def _cmd_consume_repair_audit_show(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_consume_repair_audit import (
        format_dispatch_consume_repair_audit_summary,
        summarize_consume_repair_audit,
    )

    try:
        summary = summarize_consume_repair_audit(
            repair_attempt_id=args.repair_attempt_id,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_consume_repair_audit_summary(summary))
    return 0


def _cmd_consume_repair_audit_list(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_consume_repair_audit import (
        format_dispatch_consume_repair_audit_summary,
        list_consume_repair_audit_summaries,
    )

    ticket_id = args.ticket_id or None
    summaries = list_consume_repair_audit_summaries(ticket_id=ticket_id)
    if not summaries:
        print("(none)")
        return 0
    rendered = [
        format_dispatch_consume_repair_audit_summary(summary)
        for summary in summaries
    ]
    print("\n---\n".join(rendered))
    return 0


def _cmd_consume_repair_lock_status(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_consume_repair_lock import (
        format_dispatch_consume_repair_lock_status,
        summarize_consume_repair_lock_status,
    )

    try:
        status = summarize_consume_repair_lock_status(
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_consume_repair_lock_status(status))
    return 0


def _cmd_operator_runbook(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_operator_runbook import (
        format_dispatch_operator_runbook,
        summarize_dispatch_operator_runbook,
    )
    from hermes_cli.config import load_config

    try:
        summary = summarize_dispatch_operator_runbook(
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
            merged_config=load_config(),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_operator_runbook(summary))
    return 0


def _cmd_repository_attest(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_repository_attestation import (
        attest_repository2_production_root,
        format_dispatch_repository_attestation,
    )

    try:
        summary = attest_repository2_production_root(
            repository_root=args.repository_root,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_repository_attestation(summary))
    return 0 if summary.repository_attested else 1


def _cmd_pilot_readiness(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_pilot import (
        format_dispatch_pilot_readiness,
        evaluate_pilot_readiness,
    )
    from hermes_cli.config import load_config

    summary = evaluate_pilot_readiness(
        ticket_id=args.ticket_id,
        confirmation_id=args.confirmation_id,
        pipeline_root=args.pipeline_root,
        merged_config=load_config(),
    )
    print(format_dispatch_pilot_readiness(summary))
    return 0 if summary.pilot_ready else 1


def _cmd_pilot_run(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_pilot import (
        assert_pilot_dispatch_allowed,
        format_dispatch_pilot_run_footer,
    )
    from hermes_cli.config import load_config

    merged_config = load_config()
    try:
        pilot_summary = assert_pilot_dispatch_allowed(
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
            pipeline_root=args.pipeline_root,
            merged_config=merged_config,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    exit_code = run_coo_dispatch_from_args(
        args,
        use_runner_provider=not args.dry_run,
        merged_config=merged_config,
    )
    print(format_dispatch_pilot_run_footer(pilot_ready_summary=pilot_summary))
    return exit_code


def _cmd_production_signoff(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_production_signoff import (
        format_dispatch_production_signoff,
        evaluate_dispatch_production_signoff,
    )
    from hermes_cli.config import load_config

    summary = evaluate_dispatch_production_signoff(merged_config=load_config())
    print(format_dispatch_production_signoff(summary))
    return 0 if summary.signoff_ready else 1


def _cmd_production_readiness(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_production_readiness import (
        OVERALL_NOT_READY,
        format_dispatch_production_readiness,
        evaluate_dispatch_production_readiness,
    )
    from hermes_cli.config import load_config

    summary = evaluate_dispatch_production_readiness(merged_config=load_config())
    print(format_dispatch_production_readiness(summary))
    return 0 if summary.overall != OVERALL_NOT_READY else 1


def _cmd_enablement_check(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_enablement import (
        evaluate_dispatch_enablement,
        format_dispatch_enablement_summary,
    )
    from hermes_cli.config import load_config

    summary = evaluate_dispatch_enablement(load_config())
    print(format_dispatch_enablement_summary(summary))
    return 0 if summary.enablement_ready else 1


def _cmd_binding_status(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_binding import (
        format_runner_binding_state_summary,
        summarize_dispatch_runner_binding,
    )

    try:
        binding = summarize_dispatch_runner_binding()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_runner_binding_state_summary(binding))
    return 0


def _cmd_binding_stage(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_binding import (
        execute_dispatch_binding_stage,
        format_dispatch_binding_transition_summary,
    )

    try:
        summary = execute_dispatch_binding_stage(
            operator_id=args.operator_id,
            reason=args.reason,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_binding_transition_summary(summary))
    return 0


def _cmd_binding_reset(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_binding import (
        execute_dispatch_binding_reset,
        format_dispatch_binding_transition_summary,
    )

    try:
        summary = execute_dispatch_binding_reset(
            operator_id=args.operator_id,
            reason=args.reason,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_binding_transition_summary(summary))
    return 0


def _cmd_binding_bind(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_binding import (
        execute_dispatch_binding_bind,
        format_dispatch_binding_transition_summary,
    )
    from hermes_cli.config import load_config

    try:
        summary = execute_dispatch_binding_bind(
            operator_id=args.operator_id,
            reason=args.reason,
            merged_config=load_config(),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_dispatch_binding_transition_summary(summary))
    return 0


def run_coo_dispatch_from_args(
    args: argparse.Namespace,
    *,
    subprocess_runner=None,
    injected_runner=None,
    use_runner_provider: bool = False,
    use_real_bounded_runner: bool = False,
    merged_config=None,
    binding_state=None,
    harness_profile=None,
    node_executable=None,
    node_path=None,
    harness_max_output_bytes=None,
    harness_max_timeout_seconds=None,
) -> int:
    """Execute dispatch run from parsed CLI args (runner injectable for tests)."""
    from agent.coo.bounded_subprocess_runner import (
        RUNNER_PROFILE_RESTRICTED,
    )
    from agent.coo.dispatch_cli_run import execute_coo_dispatch_run
    from agent.coo.dispatch_cli_runner_injection import (
        DEFAULT_REAL_HARNESS_MAX_OUTPUT_BYTES,
        DEFAULT_REAL_HARNESS_MAX_TIMEOUT_SECONDS,
        resolve_dispatch_run_subprocess_runner,
    )

    dry_run = bool(args.dry_run)
    resolved_config = merged_config
    if use_runner_provider and resolved_config is None and not dry_run:
        from hermes_cli.config import load_config

        resolved_config = load_config()

    harness_kwargs = {}
    if harness_max_output_bytes is not None:
        harness_kwargs["harness_max_output_bytes"] = harness_max_output_bytes
    if harness_max_timeout_seconds is not None:
        harness_kwargs["harness_max_timeout_seconds"] = harness_max_timeout_seconds

    resolved_harness_profile = (
        harness_profile if harness_profile is not None else RUNNER_PROFILE_RESTRICTED
    )
    resolved_node_path = node_path if node_path is not None else node_executable

    try:
        resolved_runner = resolve_dispatch_run_subprocess_runner(
            subprocess_runner=subprocess_runner,
            injected_runner=injected_runner,
            use_runner_provider=use_runner_provider,
            use_real_bounded_runner=use_real_bounded_runner,
            dry_run=dry_run,
            merged_config=resolved_config,
            binding_state=binding_state,
            harness_profile=resolved_harness_profile,
            node_executable=node_executable,
            harness_max_output_bytes=harness_kwargs.get(
                "harness_max_output_bytes",
                DEFAULT_REAL_HARNESS_MAX_OUTPUT_BYTES,
            ),
            harness_max_timeout_seconds=harness_kwargs.get(
                "harness_max_timeout_seconds",
                DEFAULT_REAL_HARNESS_MAX_TIMEOUT_SECONDS,
            ),
        )
        result = execute_coo_dispatch_run(
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
            unlock_token_id=args.unlock_token_id,
            requester_id=args.requester_id,
            pipeline_root=args.pipeline_root,
            dry_run=dry_run,
            subprocess_runner=resolved_runner,
            merged_config=resolved_config if use_runner_provider else merged_config,
            node_path=resolved_node_path,
        )
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"ticket_id: {result.ticket_id}")
    print(f"confirmation_id: {result.confirmation_id}")
    print(f"dispatch_request_id: {result.dispatch_request_id}")
    if result.execution_attempt_id:
        print(f"execution_attempt_id: {result.execution_attempt_id}")
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
