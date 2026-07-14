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
    from hermes_cli.coo_dispatch_help_text import (
        mock_pilot,
        read_only,
        repository_read_only,
        write_gated,
    )

    subparsers = parser.add_subparsers(dest="coo_dispatch_command", required=True)

    confirm_parser = subparsers.add_parser(
        "confirm-run",
        help=write_gated(
            "Create a production executor confirmation record (no dispatch run)"
        ),
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
        help=write_gated(
            "Run approved dispatch from persisted bundle + confirmation files"
        ),
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
        help=read_only(
            "Summary of persisted dispatch bundle and confirmation files"
        ),
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
        help=read_only(
            "Operator readiness check before dispatch run (config, persistence, policy)"
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
        help=read_only("Dispatch execution audit commands"),
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
        help=read_only("Dispatch execution evidence commands"),
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
        help="Consume transaction status, recovery, and repair commands",
        description=(
            "Consume operator surface. status/recovery are read-only; repair apply "
            "requires operator confirmation. Production execution remains disabled."
        ),
    )
    consume_subparsers = consume_parser.add_subparsers(
        dest="coo_dispatch_consume_command",
        required=True,
    )
    consume_status_parser = consume_subparsers.add_parser(
        "status",
        help=read_only("Consume status for bundle + confirmation pair"),
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
        help=read_only("Recovery assessment for bundle + confirmation consume pair"),
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
        help=read_only("Evaluate repair eligibility without mutating persisted state"),
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
        help=write_gated(
            "Apply eligible consume repair (prepared cleanup or partial forward-complete)"
        ),
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
        help=read_only("Operator guidance and runbook commands"),
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

    operator_guidance_parser = operator_subparsers.add_parser(
        "guidance",
        help=read_only("Map recommended_action code to in-repo runbook guidance"),
    )
    operator_guidance_parser.add_argument(
        "--recommended-action",
        required=True,
        help="Fixed recommended_action code (no shell commands emitted)",
    )
    operator_guidance_parser.set_defaults(handler=_cmd_operator_guidance)

    repository_parser = subparsers.add_parser(
        "repository",
        help=repository_read_only("Repository2 attestation commands"),
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
        help=read_only("Production readiness review commands"),
        description=read_only(
            "Production sign-off and cutover review. Execution remains disabled."
        ),
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

    production_cutover_parser = production_subparsers.add_parser(
        "cutover-check",
        help="Evaluate read-only production cutover checklist (execution remains disabled)",
    )
    production_cutover_parser.add_argument(
        "--ticket-id",
        action="append",
        default=None,
        dest="ticket_ids",
        help="Ticket id to include in pilot fleet review (repeatable)",
    )
    production_cutover_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for recent pilot history records per ticket",
    )
    production_cutover_parser.set_defaults(handler=_cmd_production_cutover_check)

    production_final_signoff_status_parser = production_subparsers.add_parser(
        "final-signoff-status",
        help=read_only("Read-only final production sign-off assessment"),
    )
    production_final_signoff_status_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for final sign-off status",
    )
    production_final_signoff_status_parser.add_argument(
        "--reservation-id",
        required=True,
        help="Reservation id correlated with finalized live pilot",
    )
    production_final_signoff_status_parser.set_defaults(
        handler=_cmd_production_final_signoff_status
    )

    production_final_signoff_parser = production_subparsers.add_parser(
        "final-signoff",
        help=read_only(
            "Record final production sign-off when release candidate is ready"
        ),
    )
    production_final_signoff_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for final sign-off",
    )
    production_final_signoff_parser.add_argument(
        "--reservation-id",
        required=True,
        help="Reservation id correlated with finalized live pilot",
    )
    production_final_signoff_parser.add_argument(
        "--signer-id",
        required=True,
        help="Release approver id performing final sign-off",
    )
    production_final_signoff_parser.set_defaults(handler=_cmd_production_final_signoff)

    production_governed_cutover_parser = production_subparsers.add_parser(
        "governed-cutover",
        help=read_only(
            "Governed production cutover contract (Phase 15A; no execution)"
        ),
        description=read_only(
            "Evaluate and prepare append-only governed cutover contracts. "
            "Does not open a maintenance window or enable production execution. "
            "Distinct from legacy production cutover-check (Phase 13D)."
        ),
    )
    governed_cutover_subparsers = production_governed_cutover_parser.add_subparsers(
        dest="coo_dispatch_production_governed_cutover_command",
        required=True,
    )
    governed_cutover_status_parser = governed_cutover_subparsers.add_parser(
        "status",
        help=read_only("Read-only governed cutover status summary"),
    )
    governed_cutover_status_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for governed cutover status",
    )
    governed_cutover_status_parser.add_argument(
        "--reservation-id",
        required=True,
        help="Reservation id correlated with final sign-off",
    )
    governed_cutover_status_parser.set_defaults(
        handler=_cmd_production_governed_cutover_status
    )
    governed_cutover_check_parser = governed_cutover_subparsers.add_parser(
        "check",
        help=read_only("Read-only governed cutover checklist"),
    )
    governed_cutover_check_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for governed cutover checklist",
    )
    governed_cutover_check_parser.add_argument(
        "--reservation-id",
        required=True,
        help="Reservation id correlated with final sign-off",
    )
    governed_cutover_check_parser.set_defaults(
        handler=_cmd_production_governed_cutover_check
    )
    governed_cutover_prepare_parser = governed_cutover_subparsers.add_parser(
        "prepare",
        help=read_only(
            "Append-only governed cutover contract when ready "
            "(does not open window or execute)"
        ),
    )
    governed_cutover_prepare_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for cutover contract",
    )
    governed_cutover_prepare_parser.add_argument(
        "--reservation-id",
        required=True,
        help="Reservation id correlated with final sign-off",
    )
    governed_cutover_prepare_parser.add_argument(
        "--operator-id",
        required=True,
        help="Primary cutover operator id (not printed in safe output)",
    )
    governed_cutover_prepare_parser.add_argument(
        "--window-start",
        required=True,
        help="Timezone-aware ISO8601 maintenance window start",
    )
    governed_cutover_prepare_parser.add_argument(
        "--window-end",
        required=True,
        help="Timezone-aware ISO8601 maintenance window end",
    )
    governed_cutover_prepare_parser.set_defaults(
        handler=_cmd_production_governed_cutover_prepare
    )
    governed_cutover_show_parser = governed_cutover_subparsers.add_parser(
        "show",
        help=read_only("Read-only governed cutover contract lookup"),
    )
    governed_cutover_show_parser.add_argument(
        "--cutover-contract-id",
        required=True,
        help="Opaque cutover contract id",
    )
    governed_cutover_show_parser.set_defaults(
        handler=_cmd_production_governed_cutover_show
    )

    production_activation_parser = production_subparsers.add_parser(
        "activation",
        help=read_only("Production activation proposal commands"),
        description=read_only(
            "Production activation governance proposals. Execution remains disabled."
        ),
    )
    production_activation_subparsers = production_activation_parser.add_subparsers(
        dest="coo_dispatch_production_activation_command",
        required=True,
    )
    production_activation_propose_parser = production_activation_subparsers.add_parser(
        "propose",
        help=(
            "Create a proposed production activation artifact "
            "(append-only; no approval or execution)"
        ),
    )
    production_activation_propose_parser.add_argument(
        "--tested-commit-sha",
        required=True,
        help="Git commit SHA under test (must match current repository HEAD)",
    )
    production_activation_propose_parser.add_argument(
        "--release-tag",
        required=True,
        help="Release tag for the tested commit (e.g. v1.0.0-rc.1)",
    )
    production_activation_propose_parser.add_argument(
        "--repository-attestation-hash",
        required=True,
        help="SHA-256 digest of the read-only repository attestation snapshot",
    )
    production_activation_propose_parser.add_argument(
        "--requested-by",
        required=True,
        help="Operator id submitting the activation proposal",
    )
    production_activation_propose_parser.add_argument(
        "--rollback-commit",
        required=True,
        help="Rollback commit SHA if activation must be revoked",
    )
    production_activation_propose_parser.add_argument(
        "--activation-scope",
        required=True,
        dest="scope_type",
        choices=("one_shot", "ticket_scoped", "maintenance_window"),
        help="Activation scope type",
    )
    production_activation_propose_parser.add_argument(
        "--platform",
        default="cli",
        choices=("cli", "gateway"),
        help="Activation platform surface (default: cli)",
    )
    production_activation_propose_parser.add_argument(
        "--ticket-id",
        default="",
        help="Ticket id when activation scope is ticket_scoped",
    )
    production_activation_propose_parser.add_argument(
        "--maintenance-window-start",
        default="",
        help="ISO-8601 maintenance window start (maintenance_window scope only)",
    )
    production_activation_propose_parser.add_argument(
        "--maintenance-window-end",
        default="",
        help="ISO-8601 maintenance window end (maintenance_window scope only)",
    )
    production_activation_propose_parser.set_defaults(
        handler=_cmd_production_activation_propose
    )

    production_activation_approve_parser = production_activation_subparsers.add_parser(
        "approve",
        help="Record one release approver approval for a proposed activation",
    )
    production_activation_approve_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to approve",
    )
    production_activation_approve_parser.add_argument(
        "--approver-id",
        required=True,
        help="Release approver operator id",
    )
    production_activation_approve_parser.add_argument(
        "--approver-role",
        default="release_approver",
        choices=("release_approver",),
        help="Approver role (release_approver only in this phase)",
    )
    production_activation_approve_parser.set_defaults(
        handler=_cmd_production_activation_approve
    )

    production_activation_security_parser = production_activation_subparsers.add_parser(
        "security-review",
        help="Record security reviewer approval for a proposed activation",
    )
    production_activation_security_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to review",
    )
    production_activation_security_parser.add_argument(
        "--reviewer-id",
        required=True,
        help="Security reviewer operator id",
    )
    production_activation_security_parser.set_defaults(
        handler=_cmd_production_activation_security_review
    )

    production_activation_status_parser = production_activation_subparsers.add_parser(
        "status",
        help=read_only("Show safe activation approval status"),
    )
    production_activation_status_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to inspect",
    )
    production_activation_status_parser.set_defaults(
        handler=_cmd_production_activation_status
    )

    production_activation_arm_parser = production_activation_subparsers.add_parser(
        "arm",
        help="Arm an approved activation with production executor confirmation",
    )
    production_activation_arm_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to arm",
    )
    production_activation_arm_parser.add_argument(
        "--executor-id",
        required=True,
        help="Production executor operator id",
    )
    production_activation_arm_parser.add_argument(
        "--phrase",
        required=True,
        help="Arm confirmation phrase (CLI only)",
    )
    production_activation_arm_parser.set_defaults(handler=_cmd_production_activation_arm)

    production_activation_disarm_parser = production_activation_subparsers.add_parser(
        "disarm",
        help="Disarm or cancel an approved/armed activation",
    )
    production_activation_disarm_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to disarm",
    )
    production_activation_disarm_parser.add_argument(
        "--actor-id",
        required=True,
        help="Operator performing the disarm",
    )
    production_activation_disarm_parser.add_argument(
        "--reason-code",
        required=True,
        help="Disarm reason code",
    )
    production_activation_disarm_parser.add_argument(
        "--actor-role",
        default="",
        help="Optional actor role override (operator, production_executor, incident_commander)",
    )
    production_activation_disarm_parser.set_defaults(
        handler=_cmd_production_activation_disarm
    )

    production_activation_gate_parser = production_activation_subparsers.add_parser(
        "gate",
        help=read_only("Evaluate active gate readiness for an armed activation"),
    )
    production_activation_gate_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to evaluate",
    )
    production_activation_gate_parser.set_defaults(
        handler=_cmd_production_activation_gate
    )

    production_activation_suspend_parser = production_activation_subparsers.add_parser(
        "suspend",
        help="Suspend an armed activation via kill switch",
    )
    production_activation_suspend_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to suspend",
    )
    production_activation_suspend_parser.add_argument(
        "--actor-id",
        required=True,
        help="Operator performing the suspend",
    )
    production_activation_suspend_parser.add_argument(
        "--actor-role",
        required=True,
        choices=("operator", "incident_commander"),
        help="Actor role for suspend",
    )
    production_activation_suspend_parser.add_argument(
        "--reason-code",
        required=True,
        help="Suspend reason code",
    )
    production_activation_suspend_parser.set_defaults(
        handler=_cmd_production_activation_suspend
    )

    production_activation_revoke_parser = production_activation_subparsers.add_parser(
        "revoke",
        help="Revoke a suspended activation via kill switch",
    )
    production_activation_revoke_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to revoke",
    )
    production_activation_revoke_parser.add_argument(
        "--actor-id",
        required=True,
        help="Operator performing the revoke",
    )
    production_activation_revoke_parser.add_argument(
        "--actor-role",
        required=True,
        choices=("operator", "incident_commander"),
        help="Actor role for revoke",
    )
    production_activation_revoke_parser.add_argument(
        "--reason-code",
        required=True,
        help="Revoke reason code",
    )
    production_activation_revoke_parser.set_defaults(
        handler=_cmd_production_activation_revoke
    )

    production_activation_dry_run_parser = production_activation_subparsers.add_parser(
        "dry-run",
        help=read_only("Evaluate production dry-run contract without execution"),
    )
    production_activation_dry_run_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to evaluate",
    )
    production_activation_dry_run_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Ticket id for scoped dry-run validation",
    )
    production_activation_dry_run_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Confirmation id for scoped dry-run validation",
    )
    production_activation_dry_run_parser.add_argument(
        "--pipeline-root",
        required=True,
        help="Isolated production mirror root (not production Repository2 root)",
    )
    production_activation_dry_run_parser.set_defaults(
        handler=_cmd_production_activation_dry_run
    )

    production_activation_activate_parser = production_activation_subparsers.add_parser(
        "activate",
        help=read_only("Transition armed activation to active without execution"),
    )
    production_activation_activate_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to activate",
    )
    production_activation_activate_parser.add_argument(
        "--actor-id",
        required=True,
        help="Production executor actor id",
    )
    production_activation_activate_parser.add_argument(
        "--actor-role",
        required=True,
        help="Actor role (must be production_executor)",
    )
    production_activation_activate_parser.add_argument(
        "--phrase",
        required=True,
        help="Activation confirmation phrase",
    )
    production_activation_activate_parser.set_defaults(
        handler=_cmd_production_activation_activate
    )

    production_activation_active_status_parser = production_activation_subparsers.add_parser(
        "active-status",
        help=read_only("Show controlled active transition status"),
    )
    production_activation_active_status_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to inspect",
    )
    production_activation_active_status_parser.set_defaults(
        handler=_cmd_production_activation_active_status
    )

    production_activation_execution_gate_parser = (
        production_activation_subparsers.add_parser(
            "execution-gate",
            help=read_only("Evaluate production execution gate without execution"),
        )
    )
    production_activation_execution_gate_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id to evaluate",
    )
    production_activation_execution_gate_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Ticket id for scoped execution gate validation",
    )
    production_activation_execution_gate_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Confirmation id for scoped execution gate validation",
    )
    production_activation_execution_gate_parser.add_argument(
        "--pipeline-root",
        required=True,
        help="Isolated production mirror root (not production Repository2 root)",
    )
    production_activation_execution_gate_parser.set_defaults(
        handler=_cmd_production_activation_execution_gate
    )

    production_activation_live_pilot_parser = (
        production_activation_subparsers.add_parser(
            "live-pilot",
            help=read_only(
                "Live pilot preflight, reservation, and permit without execution"
            ),
        )
    )
    production_activation_live_pilot_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for live pilot preflight",
    )
    production_activation_live_pilot_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Ticket id for scoped live pilot preflight",
    )
    production_activation_live_pilot_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Confirmation id for scoped live pilot preflight",
    )
    production_activation_live_pilot_parser.add_argument(
        "--unlock-token-id",
        required=True,
        help="Unlock token id for bundle/confirmation correlation",
    )
    production_activation_live_pilot_parser.add_argument(
        "--requester-id",
        required=True,
        help="Dispatch requester id for audit correlation",
    )
    production_activation_live_pilot_parser.add_argument(
        "--pipeline-root",
        required=True,
        help="Isolated production mirror root (not production Repository2 root)",
    )
    production_activation_live_pilot_parser.add_argument(
        "--phrase",
        required=True,
        help="Execution confirmation phrase (CLI only)",
    )
    production_activation_live_pilot_parser.add_argument(
        "--execute-isolated-mirror",
        action="store_true",
        help="Opt in to one bounded isolated-mirror runtime execution",
    )
    production_activation_live_pilot_parser.set_defaults(
        handler=_cmd_production_activation_live_pilot
    )

    production_activation_live_pilot_finalize_parser = (
        production_activation_subparsers.add_parser(
            "live-pilot-finalize",
            help=read_only(
                "Finalize live pilot E2E after isolated mirror runtime success"
            ),
        )
    )
    production_activation_live_pilot_finalize_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for E2E finalize",
    )
    production_activation_live_pilot_finalize_parser.add_argument(
        "--reservation-id",
        required=True,
        help="Reservation id correlated with runtime completion",
    )
    production_activation_live_pilot_finalize_parser.set_defaults(
        handler=_cmd_production_activation_live_pilot_finalize
    )

    production_activation_live_pilot_status_parser = (
        production_activation_subparsers.add_parser(
            "live-pilot-status",
            help=read_only("Read-only live pilot operational status assessment"),
        )
    )
    production_activation_live_pilot_status_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for live pilot status",
    )
    production_activation_live_pilot_status_parser.add_argument(
        "--reservation-id",
        required=True,
        help="Reservation id for live pilot status correlation",
    )
    production_activation_live_pilot_status_parser.set_defaults(
        handler=_cmd_production_activation_live_pilot_status
    )

    production_activation_live_pilot_signoff_parser = (
        production_activation_subparsers.add_parser(
            "live-pilot-signoff",
            help=read_only(
                "Record operator sign-off after successful live pilot validation"
            ),
        )
    )
    production_activation_live_pilot_signoff_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for operator sign-off",
    )
    production_activation_live_pilot_signoff_parser.add_argument(
        "--reservation-id",
        required=True,
        help="Reservation id correlated with finalized live pilot",
    )
    production_activation_live_pilot_signoff_parser.add_argument(
        "--operator-id",
        required=True,
        help="Operator id performing supervised sign-off",
    )
    production_activation_live_pilot_signoff_parser.set_defaults(
        handler=_cmd_production_activation_live_pilot_signoff
    )

    production_activation_rollback_check_parser = (
        production_activation_subparsers.add_parser(
            "rollback-check",
            help=read_only(
                "Read-only rollback readiness validation for live pilot artifacts"
            ),
        )
    )
    production_activation_rollback_check_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for rollback validation",
    )
    production_activation_rollback_check_parser.add_argument(
        "--reservation-id",
        required=True,
        help="Reservation id correlated with finalized live pilot",
    )
    production_activation_rollback_check_parser.set_defaults(
        handler=_cmd_production_activation_rollback_check
    )

    production_activation_rollback_plan_parser = (
        production_activation_subparsers.add_parser(
            "rollback-plan",
            help=read_only("Read-only rollback plan summary without execution"),
        )
    )
    production_activation_rollback_plan_parser.add_argument(
        "--activation-request-id",
        required=True,
        help="Activation request id for rollback plan",
    )
    production_activation_rollback_plan_parser.add_argument(
        "--reservation-id",
        required=True,
        help="Reservation id correlated with finalized live pilot",
    )
    production_activation_rollback_plan_parser.set_defaults(
        handler=_cmd_production_activation_rollback_plan
    )

    pilot_parser = subparsers.add_parser(
        "pilot",
        help="Isolated operational dispatch pilot",
        description=mock_pilot(
            "Isolated pilot surface. Live mock uses injected runner only."
        ),
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
        help=mock_pilot(
            "Run isolated operational pilot dispatch after sign-off and readiness gates"
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

    pilot_regression_parser = pilot_subparsers.add_parser(
        "regression",
        help="Evaluate read-only pilot operations regression from persisted history",
    )
    pilot_regression_parser.add_argument(
        "--ticket-id",
        default=None,
        help="Optional ticket id filter for regression evaluation",
    )
    pilot_regression_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of newest history records to evaluate",
    )
    pilot_regression_parser.set_defaults(handler=_cmd_pilot_regression)

    pilot_runbook_parser = pilot_subparsers.add_parser(
        "runbook",
        help="Show read-only isolated operational pilot drill runbook",
    )
    pilot_runbook_parser.add_argument(
        "--ticket-id",
        default=None,
        help="Optional ticket id to scope history and regression",
    )
    pilot_runbook_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for recent pilot history records",
    )
    pilot_runbook_parser.set_defaults(handler=_cmd_pilot_runbook)

    pilot_fleet_parser = pilot_subparsers.add_parser(
        "fleet",
        help="Show read-only multi-ticket isolated operational pilot fleet view",
    )
    pilot_fleet_parser.add_argument(
        "--ticket-id",
        action="append",
        default=None,
        dest="ticket_ids",
        help="Ticket id to include (repeatable; defaults to recent history tickets)",
    )
    pilot_fleet_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for recent pilot history records per ticket",
    )
    pilot_fleet_parser.set_defaults(handler=_cmd_pilot_fleet)

    pilot_history_parser = pilot_subparsers.add_parser(
        "history",
        help="Read-only isolated operational pilot history commands",
    )
    pilot_history_subparsers = pilot_history_parser.add_subparsers(
        dest="coo_dispatch_pilot_history_command",
        required=True,
    )
    pilot_history_show_parser = pilot_history_subparsers.add_parser(
        "show",
        help="Show one pilot history record by pilot_attempt_id",
    )
    pilot_history_show_parser.add_argument(
        "--pilot-attempt-id",
        required=True,
        help="Pilot attempt id",
    )
    pilot_history_show_parser.set_defaults(handler=_cmd_pilot_history_show)

    pilot_history_list_parser = pilot_history_subparsers.add_parser(
        "list",
        help="List pilot history records newest-first",
    )
    pilot_history_list_parser.set_defaults(handler=_cmd_pilot_history_list)

    pilot_history_find_parser = pilot_history_subparsers.add_parser(
        "find",
        help="Find pilot history records for one ticket id",
    )
    pilot_history_find_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id",
    )
    pilot_history_find_parser.set_defaults(handler=_cmd_pilot_history_find)

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

    gateway_parser = subparsers.add_parser(
        "gateway",
        help="Gateway enablement, observability, and mock pilot",
        description=read_only(
            "Gateway operator surface: status, readiness, audit, correlation, dashboard."
        ),
    )
    gateway_subparsers = gateway_parser.add_subparsers(
        dest="coo_dispatch_gateway_command",
        required=True,
    )
    gateway_status_parser = gateway_subparsers.add_parser(
        "status",
        help=read_only("Gateway enablement state summary"),
    )
    gateway_status_parser.set_defaults(handler=_cmd_gateway_status)

    gateway_readiness_parser = gateway_subparsers.add_parser(
        "readiness",
        help=read_only("Gateway readiness without mutating state"),
    )
    gateway_readiness_parser.add_argument(
        "--ticket-id",
        default=None,
        help="Optional execution ticket id for evidence cross-reference",
    )
    gateway_readiness_parser.add_argument(
        "--confirmation-id",
        default=None,
        help="Optional confirmation id for evidence cross-reference",
    )
    gateway_readiness_parser.add_argument(
        "--pipeline-root",
        default=None,
        help="Optional isolated pipeline root for evidence cross-reference",
    )
    gateway_readiness_parser.set_defaults(handler=_cmd_gateway_readiness)

    gateway_facade_parser = gateway_subparsers.add_parser(
        "facade",
        help="Show safe summary of gateway execution facade scaffold",
    )
    gateway_facade_parser.set_defaults(handler=_cmd_gateway_facade)

    gateway_pilot_parser = gateway_subparsers.add_parser(
        "pilot",
        help=mock_pilot("Gateway pilot mock dispatch (staged only)"),
    )
    gateway_pilot_subparsers = gateway_pilot_parser.add_subparsers(
        dest="coo_dispatch_gateway_pilot_command",
        required=True,
    )
    gateway_pilot_readiness_parser = gateway_pilot_subparsers.add_parser(
        "readiness",
        help="Evaluate gateway pilot readiness without dispatch",
    )
    gateway_pilot_readiness_parser.add_argument(
        "--session-id",
        required=True,
        help="Gateway approval session id",
    )
    gateway_pilot_readiness_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id",
    )
    gateway_pilot_readiness_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    gateway_pilot_readiness_parser.add_argument(
        "--pipeline-root",
        required=True,
        help="Isolated pipeline root (production root hard-denied)",
    )
    gateway_pilot_readiness_parser.set_defaults(handler=_cmd_gateway_pilot_readiness)

    gateway_pilot_run_parser = gateway_pilot_subparsers.add_parser(
        "run",
        help="Run gateway pilot mock dispatch after readiness gates",
    )
    gateway_pilot_run_parser.add_argument(
        "--session-id",
        required=True,
        help="Gateway approval session id",
    )
    gateway_pilot_run_parser.add_argument(
        "--ticket-id",
        required=True,
        help="Execution ticket id",
    )
    gateway_pilot_run_parser.add_argument(
        "--confirmation-id",
        required=True,
        help="Production executor confirmation id",
    )
    gateway_pilot_run_parser.add_argument(
        "--unlock-token-id",
        required=True,
        help="Dispatch unlock token id (must match bundle)",
    )
    gateway_pilot_run_parser.add_argument(
        "--requester-id",
        required=True,
        help="Authorized requester id",
    )
    gateway_pilot_run_parser.add_argument(
        "--pipeline-root",
        required=True,
        help="Isolated pipeline root (production root hard-denied)",
    )
    gateway_pilot_run_parser.add_argument(
        "--gateway-request-id",
        required=True,
        help="Opaque gateway request id for idempotency",
    )
    gateway_pilot_run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preflight only; runner not invoked, nothing consumed",
    )
    gateway_pilot_run_parser.set_defaults(handler=_cmd_gateway_pilot_run)

    gateway_audit_parser = gateway_subparsers.add_parser(
        "audit",
        help="Read-only gateway request audit correlation commands",
    )
    gateway_audit_subparsers = gateway_audit_parser.add_subparsers(
        dest="coo_dispatch_gateway_audit_command",
        required=True,
    )
    gateway_audit_show_parser = gateway_audit_subparsers.add_parser(
        "show",
        help="Show read-only audit correlation for one gateway request id",
    )
    gateway_audit_show_parser.add_argument(
        "--gateway-request-id",
        required=True,
        help="Gateway request id to audit",
    )
    gateway_audit_show_parser.set_defaults(handler=_cmd_gateway_audit_show)

    gateway_correlation_parser = gateway_subparsers.add_parser(
        "correlation",
        help=read_only("Gateway correlation explorer commands"),
    )
    gateway_correlation_subparsers = gateway_correlation_parser.add_subparsers(
        dest="coo_dispatch_gateway_correlation_command",
        required=True,
    )
    gateway_correlation_show_parser = gateway_correlation_subparsers.add_parser(
        "show",
        help="Show read-only gateway correlation chain for one query id",
    )
    gateway_correlation_show_parser.add_argument(
        "--gateway-request-id",
        default="",
        help="Gateway request id query",
    )
    gateway_correlation_show_parser.add_argument(
        "--pilot-attempt-id",
        default="",
        help="Pilot attempt id query",
    )
    gateway_correlation_show_parser.add_argument(
        "--execution-attempt-id",
        default="",
        help="Execution attempt id query",
    )
    gateway_correlation_show_parser.add_argument(
        "--dispatch-run-id",
        default="",
        help="Dispatch run id query",
    )
    gateway_correlation_show_parser.add_argument(
        "--ticket-id",
        default="",
        help="Ticket id query (newest gateway request)",
    )
    gateway_correlation_show_parser.set_defaults(handler=_cmd_gateway_correlation_show)

    gateway_correlation_diff_parser = gateway_correlation_subparsers.add_parser(
        "diff",
        help="Compare read-only correlation chains for two gateway request ids",
    )
    gateway_correlation_diff_parser.add_argument(
        "--left-gateway-request-id",
        required=True,
        help="Left (older) gateway request id",
    )
    gateway_correlation_diff_parser.add_argument(
        "--right-gateway-request-id",
        required=True,
        help="Right (newer) gateway request id",
    )
    gateway_correlation_diff_parser.set_defaults(handler=_cmd_gateway_correlation_diff)

    gateway_dashboard_parser = gateway_subparsers.add_parser(
        "dashboard",
        help=read_only("Gateway operator dashboard"),
    )
    gateway_dashboard_parser.add_argument(
        "--ticket-id",
        default="",
        help="Filter dashboard to one ticket id",
    )
    gateway_dashboard_parser.add_argument(
        "--session-id",
        default="",
        help="Filter dashboard to one session id",
    )
    gateway_dashboard_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum recent gateway requests to include",
    )
    gateway_dashboard_parser.set_defaults(handler=_cmd_gateway_dashboard)

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
    from hermes_cli.coo_dispatch_help_text import DOCS_OPERATOR_INDEX, SAFETY_FOOTER

    parser = argparse.ArgumentParser(
        prog="hermes coo dispatch",
        description=(
            "COO dispatch operator surface: read-only review, gated consume repair, "
            "and isolated mock pilot. Production execution remains disabled."
        ),
        epilog=(
            "Examples:\n"
            "  hermes coo dispatch gateway dashboard\n"
            "  hermes coo dispatch gateway correlation show --gateway-request-id <id>\n"
            "  hermes coo dispatch operator guidance --recommended-action <code>\n"
            "  hermes coo dispatch production sign-off\n"
            "\n"
            f"Operator documentation: {DOCS_OPERATOR_INDEX}\n"
            f"{SAFETY_FOOTER}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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


def _cmd_operator_guidance(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_operator_guidance import run_operator_guidance_show
    from agent.coo.dispatch_operator_guidance import OperatorGuidanceError

    try:
        output, exit_code = run_operator_guidance_show(
            recommended_action=args.recommended_action,
        )
    except OperatorGuidanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


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
        execute_pilot_dispatch_run,
        format_dispatch_pilot_run_footer,
        format_dispatch_pilot_run_outcome,
    )
    from agent.coo.dispatch_cli_preflight import format_dispatch_preflight_summary
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

    outcome = execute_pilot_dispatch_run(
        ticket_id=args.ticket_id,
        confirmation_id=args.confirmation_id,
        unlock_token_id=args.unlock_token_id,
        requester_id=args.requester_id,
        pipeline_root=args.pipeline_root,
        dry_run=bool(args.dry_run),
        pilot_summary=pilot_summary,
        merged_config=merged_config,
        use_runner_provider=not args.dry_run,
    )
    result = outcome.run_result
    if result is not None:
        print(f"ticket_id: {result.ticket_id}")
        print(f"confirmation_id: {result.confirmation_id}")
        print(f"dispatch_request_id: {result.dispatch_request_id}")
        if result.execution_attempt_id:
            print(f"execution_attempt_id: {result.execution_attempt_id}")
        print(f"status: {result.status}")
        print(f"consumed: {result.consumed}")
        if result.preflight is not None:
            print(format_dispatch_preflight_summary(result.preflight))
        if result.dry_run_only:
            print(
                "status: preflight-only (--dry-run; runner not invoked, nothing consumed)"
            )
    elif outcome.run_error:
        print(f"error: {outcome.run_error}", file=sys.stderr)

    print(format_dispatch_pilot_run_outcome(outcome))
    print(format_dispatch_pilot_run_footer(pilot_ready_summary=pilot_summary))
    return int(outcome.exit_code)


def _cmd_pilot_regression(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_pilot_regression import (
        REGRESSION_STATUS_FAIL,
        format_pilot_regression_summary,
        evaluate_pilot_regression,
    )

    try:
        summary = evaluate_pilot_regression(
            ticket_id=args.ticket_id,
            limit=args.limit,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_pilot_regression_summary(summary))
    return 1 if summary.regression_status == REGRESSION_STATUS_FAIL else 0


def _cmd_pilot_runbook(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_pilot_runbook import (
        format_pilot_drill_runbook,
        summarize_pilot_drill_runbook,
    )
    from hermes_cli.config import load_config

    try:
        summary = summarize_pilot_drill_runbook(
            ticket_id=args.ticket_id,
            limit=args.limit,
            merged_config=load_config(),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_pilot_drill_runbook(summary))
    return 0 if summary.pilot_runbook_ready else 1


def _cmd_pilot_fleet(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_pilot_fleet import (
        FLEET_STATUS_NOT_READY,
        format_pilot_fleet_summary,
        summarize_pilot_fleet,
    )
    from hermes_cli.config import load_config

    try:
        summary = summarize_pilot_fleet(
            ticket_ids=args.ticket_ids,
            limit=args.limit,
            merged_config=load_config(),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_pilot_fleet_summary(summary))
    return 1 if summary.fleet_status == FLEET_STATUS_NOT_READY else 0


def _cmd_pilot_history_show(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_pilot_history import (
        format_pilot_history_summary,
        summarize_pilot_history_record,
    )

    try:
        summary = summarize_pilot_history_record(args.pilot_attempt_id)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_pilot_history_summary(summary))
    return 0


def _cmd_pilot_history_list(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_pilot_history import (
        format_pilot_history_list,
        list_pilot_history_summaries,
    )

    try:
        entries = list_pilot_history_summaries()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_pilot_history_list(entries))
    return 0


def _cmd_pilot_history_find(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_pilot_history import (
        format_pilot_history_find,
        find_pilot_history_summaries_for_ticket,
    )

    try:
        entries = find_pilot_history_summaries_for_ticket(args.ticket_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_pilot_history_find(entries))
    return 0


def _cmd_production_signoff(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_production_signoff import (
        format_dispatch_production_signoff,
        evaluate_dispatch_production_signoff,
    )
    from hermes_cli.config import load_config

    summary = evaluate_dispatch_production_signoff(merged_config=load_config())
    print(format_dispatch_production_signoff(summary))
    return 0 if summary.signoff_ready else 1


def _cmd_production_cutover_check(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_production_cutover import (
        format_production_cutover_checklist,
        evaluate_production_cutover_checklist,
    )
    from hermes_cli.config import load_config

    try:
        summary = evaluate_production_cutover_checklist(
            ticket_ids=args.ticket_ids,
            limit=args.limit,
            merged_config=load_config(),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_production_cutover_checklist(summary))
    return 0 if summary.cutover_ready else 1


def _cmd_production_activation_propose(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_production_activation import (
        ProductionActivationCliError,
        run_production_activation_propose,
    )

    try:
        output, exit_code = run_production_activation_propose(
            tested_commit_sha=args.tested_commit_sha,
            release_tag=args.release_tag,
            repository_attestation_hash=args.repository_attestation_hash,
            requested_by=args.requested_by,
            rollback_commit=args.rollback_commit,
            scope_type=args.scope_type,
            platform=args.platform,
            ticket_id=args.ticket_id,
            maintenance_window_start=args.maintenance_window_start,
            maintenance_window_end=args.maintenance_window_end,
        )
    except ProductionActivationCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_approve(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_approval import (
        ProductionActivationApprovalError,
        run_activation_approve,
    )

    try:
        output, exit_code = run_activation_approve(
            activation_request_id=args.activation_request_id,
            approver_id=args.approver_id,
            approver_role=args.approver_role,
        )
    except ProductionActivationApprovalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_security_review(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_approval import (
        ProductionActivationApprovalError,
        run_activation_security_review,
    )

    try:
        output, exit_code = run_activation_security_review(
            activation_request_id=args.activation_request_id,
            reviewer_id=args.reviewer_id,
        )
    except ProductionActivationApprovalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_status(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_arm import (
        ProductionActivationArmError,
        run_activation_status,
    )

    try:
        output, exit_code = run_activation_status(
            activation_request_id=args.activation_request_id,
        )
    except ProductionActivationArmError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_arm(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_arm import (
        ProductionActivationArmError,
        run_activation_arm,
    )

    try:
        output, exit_code = run_activation_arm(
            activation_request_id=args.activation_request_id,
            executor_id=args.executor_id,
            phrase=args.phrase,
        )
    except ProductionActivationArmError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_disarm(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_arm import (
        ProductionActivationArmError,
        run_activation_disarm,
    )

    actor_role = (args.actor_role or "").strip() or None
    try:
        output, exit_code = run_activation_disarm(
            activation_request_id=args.activation_request_id,
            actor_id=args.actor_id,
            reason_code=args.reason_code,
            actor_role=actor_role,
        )
    except ProductionActivationArmError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_gate(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_active_gate import (
        ProductionActivationActiveGateError,
        run_activation_gate,
    )

    try:
        output, exit_code = run_activation_gate(
            activation_request_id=args.activation_request_id,
        )
    except ProductionActivationActiveGateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_suspend(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_kill_switch import (
        ProductionActivationKillSwitchError,
        run_activation_suspend,
    )

    try:
        output, exit_code = run_activation_suspend(
            activation_request_id=args.activation_request_id,
            actor_id=args.actor_id,
            actor_role=args.actor_role,
            reason_code=args.reason_code,
        )
    except ProductionActivationKillSwitchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_revoke(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_kill_switch import (
        ProductionActivationKillSwitchError,
        run_activation_revoke,
    )

    try:
        output, exit_code = run_activation_revoke(
            activation_request_id=args.activation_request_id,
            actor_id=args.actor_id,
            actor_role=args.actor_role,
            reason_code=args.reason_code,
        )
    except ProductionActivationKillSwitchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_dry_run(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_dry_run import (
        ProductionActivationDryRunError,
        run_activation_dry_run,
    )

    try:
        output, exit_code = run_activation_dry_run(
            activation_request_id=args.activation_request_id,
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
            pipeline_root=args.pipeline_root,
        )
    except ProductionActivationDryRunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_activate(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_active import (
        ProductionActivationActiveError,
        run_activation_activate,
    )

    try:
        output, exit_code = run_activation_activate(
            activation_request_id=args.activation_request_id,
            actor_id=args.actor_id,
            actor_role=args.actor_role,
            phrase=args.phrase,
        )
    except ProductionActivationActiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_active_status(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_active import (
        ProductionActivationActiveError,
        run_activation_active_status,
    )

    try:
        output, exit_code = run_activation_active_status(
            activation_request_id=args.activation_request_id,
        )
    except ProductionActivationActiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_execution_gate(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_execution_gate import (
        ProductionActivationExecutionGateError,
        run_activation_execution_gate,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_activation_execution_gate(
            activation_request_id=args.activation_request_id,
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
            pipeline_root=args.pipeline_root,
            merged_config=load_config(),
        )
    except ProductionActivationExecutionGateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_live_pilot(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_live_pilot import (
        ProductionActivationLivePilotError,
        run_activation_live_pilot,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_activation_live_pilot(
            activation_request_id=args.activation_request_id,
            ticket_id=args.ticket_id,
            confirmation_id=args.confirmation_id,
            unlock_token_id=args.unlock_token_id,
            requester_id=args.requester_id,
            pipeline_root=args.pipeline_root,
            phrase=args.phrase,
            merged_config=load_config(),
            execute_isolated_mirror=bool(
                getattr(args, "execute_isolated_mirror", False)
            ),
        )
    except ProductionActivationLivePilotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_live_pilot_finalize(args: argparse.Namespace) -> int:
    from agent.coo.production_activation_live_e2e import (
        ProductionActivationLiveE2EError,
        run_activation_live_pilot_finalize,
    )

    try:
        output, exit_code = run_activation_live_pilot_finalize(
            activation_request_id=args.activation_request_id,
            reservation_id=args.reservation_id,
        )
    except ProductionActivationLiveE2EError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_live_pilot_status(args: argparse.Namespace) -> int:
    from agent.coo.production_live_operational_signoff import (
        ProductionLiveOperationalSignoffError,
        run_activation_live_pilot_status,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_activation_live_pilot_status(
            activation_request_id=args.activation_request_id,
            reservation_id=args.reservation_id,
            merged_config=load_config(),
        )
    except ProductionLiveOperationalSignoffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_live_pilot_signoff(args: argparse.Namespace) -> int:
    from agent.coo.production_live_operational_signoff import (
        ProductionLiveOperationalSignoffError,
        run_activation_live_pilot_signoff,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_activation_live_pilot_signoff(
            activation_request_id=args.activation_request_id,
            reservation_id=args.reservation_id,
            operator_id=args.operator_id,
            merged_config=load_config(),
        )
    except ProductionLiveOperationalSignoffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_rollback_check(args: argparse.Namespace) -> int:
    from agent.coo.production_live_rollback_validation import (
        ProductionLiveRollbackValidationError,
        run_activation_rollback_check,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_activation_rollback_check(
            activation_request_id=args.activation_request_id,
            reservation_id=args.reservation_id,
            merged_config=load_config(),
        )
    except ProductionLiveRollbackValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_activation_rollback_plan(args: argparse.Namespace) -> int:
    from agent.coo.production_live_rollback_validation import (
        ProductionLiveRollbackValidationError,
        run_activation_rollback_plan,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_activation_rollback_plan(
            activation_request_id=args.activation_request_id,
            reservation_id=args.reservation_id,
            merged_config=load_config(),
        )
    except ProductionLiveRollbackValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_final_signoff_status(args: argparse.Namespace) -> int:
    from agent.coo.production_final_signoff import (
        ProductionFinalSignoffError,
        run_production_final_signoff_status,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_production_final_signoff_status(
            activation_request_id=args.activation_request_id,
            reservation_id=args.reservation_id,
            merged_config=load_config(),
        )
    except ProductionFinalSignoffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_governed_cutover_status(args: argparse.Namespace) -> int:
    from agent.coo.production_governed_cutover import (
        ProductionGovernedCutoverError,
        run_production_governed_cutover_status,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_production_governed_cutover_status(
            activation_request_id=args.activation_request_id,
            reservation_id=args.reservation_id,
            merged_config=load_config(),
        )
    except ProductionGovernedCutoverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_governed_cutover_check(args: argparse.Namespace) -> int:
    from agent.coo.production_governed_cutover import (
        ProductionGovernedCutoverError,
        run_production_governed_cutover_check,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_production_governed_cutover_check(
            activation_request_id=args.activation_request_id,
            reservation_id=args.reservation_id,
            merged_config=load_config(),
        )
    except ProductionGovernedCutoverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_governed_cutover_prepare(args: argparse.Namespace) -> int:
    from agent.coo.production_governed_cutover import (
        ProductionGovernedCutoverError,
        run_production_governed_cutover_prepare,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_production_governed_cutover_prepare(
            activation_request_id=args.activation_request_id,
            reservation_id=args.reservation_id,
            operator_id=args.operator_id,
            window_start=args.window_start,
            window_end=args.window_end,
            merged_config=load_config(),
        )
    except ProductionGovernedCutoverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_governed_cutover_show(args: argparse.Namespace) -> int:
    from agent.coo.production_governed_cutover import (
        ProductionGovernedCutoverError,
        run_production_governed_cutover_show,
    )

    try:
        output, exit_code = run_production_governed_cutover_show(
            cutover_contract_id=args.cutover_contract_id,
        )
    except ProductionGovernedCutoverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_production_final_signoff(args: argparse.Namespace) -> int:
    from agent.coo.production_final_signoff import (
        ProductionFinalSignoffError,
        run_production_final_signoff,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_production_final_signoff(
            activation_request_id=args.activation_request_id,
            reservation_id=args.reservation_id,
            signer_id=args.signer_id,
            merged_config=load_config(),
        )
    except ProductionFinalSignoffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


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


def _cmd_gateway_facade(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_gateway_execution_facade import (
        evaluate_gateway_execution_facade,
        format_gateway_execution_facade,
    )
    from hermes_cli.config import load_config

    facade = evaluate_gateway_execution_facade(merged_config=load_config())
    print(format_gateway_execution_facade(facade))
    return 0 if facade.valid and facade.facade_connected else 1


def _cmd_gateway_readiness(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_gateway_readiness import (
        evaluate_dispatch_gateway_readiness,
        format_dispatch_gateway_readiness_summary,
    )
    from hermes_cli.config import load_config

    summary = evaluate_dispatch_gateway_readiness(
        ticket_id=args.ticket_id,
        confirmation_id=args.confirmation_id,
        pipeline_root=args.pipeline_root,
        merged_config=load_config(),
    )
    print(format_dispatch_gateway_readiness_summary(summary))
    return 0 if summary.gateway_readiness_ready else 1


def _cmd_gateway_status(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_gateway_status import (
        format_dispatch_gateway_status_summary,
        summarize_dispatch_gateway_status,
    )
    from hermes_cli.config import load_config

    summary = summarize_dispatch_gateway_status(merged_config=load_config())
    print(format_dispatch_gateway_status_summary(summary))
    return 0


def _cmd_gateway_correlation_show(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_gateway_correlation import run_gateway_correlation_show
    from agent.coo.dispatch_gateway_correlation_explorer import (
        GatewayCorrelationExplorerError,
    )

    try:
        output, exit_code = run_gateway_correlation_show(
            gateway_request_id=args.gateway_request_id,
            pilot_attempt_id=args.pilot_attempt_id,
            execution_attempt_id=args.execution_attempt_id,
            dispatch_run_id=args.dispatch_run_id,
            ticket_id=args.ticket_id,
        )
    except GatewayCorrelationExplorerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_gateway_correlation_diff(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_gateway_dashboard import run_gateway_correlation_diff
    from agent.coo.dispatch_gateway_operator_dashboard import (
        GatewayOperatorDashboardError,
    )

    try:
        output, exit_code = run_gateway_correlation_diff(
            left_gateway_request_id=args.left_gateway_request_id,
            right_gateway_request_id=args.right_gateway_request_id,
        )
    except GatewayOperatorDashboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_gateway_dashboard(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_gateway_dashboard import run_operator_dashboard
    from agent.coo.dispatch_gateway_operator_dashboard import (
        GatewayOperatorDashboardError,
    )
    from hermes_cli.config import load_config

    try:
        output, exit_code = run_operator_dashboard(
            ticket_id=args.ticket_id,
            session_id=args.session_id,
            limit=args.limit,
            merged_config=load_config(),
        )
    except GatewayOperatorDashboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return exit_code


def _cmd_gateway_audit_show(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_gateway_request_audit import (
        run_gateway_request_audit_show,
    )

    try:
        output = run_gateway_request_audit_show(args.gateway_request_id)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(output)
    return 0


def _cmd_gateway_pilot_readiness(args: argparse.Namespace) -> int:
    from agent.coo.dispatch_cli_gateway_pilot import (
        evaluate_gateway_pilot_readiness,
        format_gateway_pilot_readiness,
    )
    from hermes_cli.config import load_config

    summary = evaluate_gateway_pilot_readiness(
        session_id=args.session_id,
        ticket_id=args.ticket_id,
        confirmation_id=args.confirmation_id,
        pipeline_root=args.pipeline_root,
        merged_config=load_config(),
    )
    print(format_gateway_pilot_readiness(summary))
    return 0 if summary.pilot_ready else 1


def _cmd_gateway_pilot_run(args: argparse.Namespace) -> int:
    return run_gateway_pilot_dispatch_from_args(args)


def run_gateway_pilot_dispatch_from_args(
    args: argparse.Namespace,
    *,
    injected_runner=None,
    merged_config=None,
    session_store=None,
    ticket_store=None,
    bundle_dir=None,
    confirmation_dir=None,
    request_dir=None,
    history_dir=None,
) -> int:
    """Execute gateway pilot dispatch from parsed CLI args (runner injectable for tests)."""
    from agent.coo.dispatch_gateway_pilot_service import (
        FAILURE_MOCK_RUNNER_NOT_CONFIGURED,
        execute_gateway_pilot_dispatch,
        format_gateway_pilot_result,
    )

    if merged_config is None:
        from hermes_cli.config import load_config

        merged_config = load_config()

    dry_run = bool(getattr(args, "dry_run", False))
    result = execute_gateway_pilot_dispatch(
        session_id=args.session_id,
        ticket_id=args.ticket_id,
        confirmation_id=args.confirmation_id,
        unlock_token_id=args.unlock_token_id,
        requester_id=args.requester_id,
        pipeline_root=args.pipeline_root,
        gateway_request_id=args.gateway_request_id,
        dry_run=dry_run,
        merged_config=merged_config,
        injected_runner=injected_runner,
        allow_mock_gateway_dispatch=dry_run or injected_runner is not None,
        session_store=session_store,
        ticket_store=ticket_store,
        bundle_dir=bundle_dir,
        confirmation_dir=confirmation_dir,
        request_dir=request_dir,
        history_dir=history_dir,
    )
    print(format_gateway_pilot_result(result))
    if result.accepted:
        return 0
    if (
        not dry_run
        and injected_runner is None
        and result.failure_reason_code == FAILURE_MOCK_RUNNER_NOT_CONFIGURED
    ):
        print(
            "error: gateway mock runner is not configured",
            file=sys.stderr,
        )
    return 1


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
