"""Phase 13G tests — dispatch gateway readiness CLI."""

from __future__ import annotations

import hashlib
import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_gateway_readiness import (
    EVIDENCE_CONTEXT_AMBIGUOUS,
    EVIDENCE_CONTEXT_FULL,
    EVIDENCE_CONTEXT_NONE,
    READINESS_LEVEL_NOT_READY,
    READINESS_LEVEL_NOT_READY_FOR_EXECUTION,
    READINESS_LEVEL_READY_FOR_MOCK_WIRING,
    RECOMMENDED_ACTION_IMPLEMENT_FACADE,
    RECOMMENDED_ACTION_RESOLVE_FAILED_CHECKS,
    RECOMMENDED_ACTION_STAGE_GATEWAY,
    evaluate_dispatch_gateway_readiness,
    format_dispatch_gateway_readiness_summary,
)
from agent.coo.dispatch_cli_production_cutover import (
    CooDispatchProductionCutoverSummary,
)
from agent.coo.dispatch_cli_production_readiness import CHECK_BLOCKED, CHECK_FAIL
from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_PARTIAL,
    CONSUME_STATE_RECOVERY_REQUIRED,
)
from agent.coo.dispatch_gateway_enablement import GATEWAY_STATE_DISABLED
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.coo_dispatch_isolated_clone_fixture import (
    CooDispatchIsolatedCloneFixture,
    bounded_dispatch_config,
)
from tests.hermes_cli.test_coo_dispatch_production_signoff import (
    _successful_attestation,
)

_FORBIDDEN_OUTPUT_TOKENS = (
    "argv",
    "cwd",
    "env",
    "stdout",
    "stderr",
    "snapshot",
    "unlock",
    "token",
    "SECRET",
    "PASSWORD",
    "phrase",
    "pipeline.js",
    "/opt/data/multi-content-pipeline",
    "config.yaml",
    "HERMES_",
    "channel",
    "user_id",
)


def _gateway_config(state: str) -> dict:
    return {
        "coo": {
            "dispatch": {
                "gateway": {
                    "enablement": state,
                },
            },
        },
    }


def _ready_cutover_summary() -> CooDispatchProductionCutoverSummary:
    return CooDispatchProductionCutoverSummary(
        cutover_ready=True,
        overall_status="READY",
        checks_passed_count=12,
        checks_blocked_count=3,
        checks_failed_count=0,
        failed_checks="(none)",
        blocked_checks="production_root_hard_deny,execution_disabled,gateway_disabled",
        fleet_status="READY",
        ticket_count=1,
        ready_ticket_count=1,
        failed_ticket_count=0,
        production_execution_allowed=False,
        gateway_enabled=False,
        production_root_hard_deny=True,
        recommended_action="continue_isolated_pilot",
        recommended_next_phase="Phase 13A",
    )


def _hermes_digest(root: Path) -> str:
    if not root.exists():
        return ""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            parts.append(f"{rel}:{digest}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


class TestGatewayReadinessStates(unittest.TestCase):
    def test_missing_config_disabled_not_ready_for_execution(self) -> None:
        summary = evaluate_dispatch_gateway_readiness(merged_config={})
        self.assertEqual(summary.gateway_state, GATEWAY_STATE_DISABLED)
        self.assertEqual(summary.readiness_level, READINESS_LEVEL_NOT_READY_FOR_EXECUTION)
        self.assertFalse(summary.gateway_readiness_ready)
        self.assertEqual(summary.recommended_action, RECOMMENDED_ACTION_STAGE_GATEWAY)
        self.assertIn("gateway_enablement_state", summary.blocked_checks)
        self.assertFalse(summary.gateway_execution_allowed)
        self.assertFalse(summary.production_execution_allowed)

    def test_disabled_capability_normal_intentional_blocked(self) -> None:
        summary = evaluate_dispatch_gateway_readiness(
            merged_config=_gateway_config("disabled"),
        )
        self.assertEqual(summary.readiness_level, READINESS_LEVEL_NOT_READY_FOR_EXECUTION)
        self.assertFalse(summary.gateway_readiness_ready)
        self.assertTrue(summary.gateway_ui_surface_available)
        self.assertTrue(summary.gateway_session_model_available)
        self.assertTrue(summary.gateway_prepare_surface_available)
        self.assertIn("gateway_enablement_state", summary.blocked_checks)
        self.assertIn("gateway_execution_facade_connected", summary.blocked_checks)

    def test_staged_ready_for_mock_wiring(self) -> None:
        with (
            _successful_attestation(),
            patch(
                "agent.coo.dispatch_cli_gateway_readiness.evaluate_production_cutover_checklist",
                return_value=_ready_cutover_summary(),
            ),
        ):
            summary = evaluate_dispatch_gateway_readiness(
                merged_config=_gateway_config("staged"),
            )
        self.assertEqual(summary.readiness_level, READINESS_LEVEL_READY_FOR_MOCK_WIRING)
        self.assertTrue(summary.gateway_readiness_ready)
        self.assertEqual(summary.recommended_action, RECOMMENDED_ACTION_IMPLEMENT_FACADE)
        self.assertIn("gateway_execution_facade_connected", summary.blocked_checks)

    def test_enabled_facade_false_fails(self) -> None:
        summary = evaluate_dispatch_gateway_readiness(
            merged_config=_gateway_config("enabled"),
        )
        self.assertEqual(summary.readiness_level, READINESS_LEVEL_NOT_READY)
        self.assertFalse(summary.gateway_readiness_ready)
        self.assertIn("gateway_execution_facade_connected", summary.failed_checks)

    def test_invalid_gateway_config_fail_closed(self) -> None:
        config = _gateway_config("enabled")
        config["coo"]["dispatch"]["gateway"]["enablement"] = "bogus"
        summary = evaluate_dispatch_gateway_readiness(merged_config=config)
        self.assertFalse(summary.gateway_readiness_ready)
        self.assertIn("gateway_enablement_state", summary.failed_checks)


class TestGatewayReadinessCapabilityProbes(unittest.TestCase):
    def test_ui_capability_missing_fails(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_gateway_readiness._probe_gateway_ui_surface_available",
            return_value=False,
        ):
            summary = evaluate_dispatch_gateway_readiness(
                merged_config=_gateway_config("staged"),
            )
        self.assertIn("gateway_ui_surface_available", summary.failed_checks)

    def test_session_model_missing_fails(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_gateway_readiness._probe_gateway_session_model_available",
            return_value=False,
        ):
            summary = evaluate_dispatch_gateway_readiness(
                merged_config=_gateway_config("staged"),
            )
        self.assertIn("gateway_session_model_available", summary.failed_checks)

    def test_prepare_surface_missing_fails(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_gateway_readiness._probe_gateway_prepare_surface_available",
            return_value=False,
        ):
            summary = evaluate_dispatch_gateway_readiness(
                merged_config=_gateway_config("staged"),
            )
        self.assertIn("gateway_prepare_surface_available", summary.failed_checks)


class TestGatewayReadinessEvidenceContext(unittest.TestCase):
    def test_capability_only_without_evidence_context(self) -> None:
        summary = evaluate_dispatch_gateway_readiness(merged_config={})
        self.assertEqual(summary.evidence_context_requested, EVIDENCE_CONTEXT_NONE)
        self.assertEqual(summary.operator_readiness_status, "not_evaluated")
        self.assertEqual(summary.consume_state, "not_evaluated")
        statuses = {check.name: check.status for check in summary.checks}
        self.assertEqual(statuses["operator_readiness"], "NOT_APPLICABLE")
        self.assertEqual(statuses["file_backed_bundle_available"], "NOT_APPLICABLE")

    def test_partial_evidence_args_rejected(self) -> None:
        summary = evaluate_dispatch_gateway_readiness(
            ticket_id="ticket-1",
            merged_config={},
        )
        self.assertEqual(summary.evidence_context_requested, EVIDENCE_CONTEXT_AMBIGUOUS)
        self.assertFalse(summary.gateway_readiness_ready)
        self.assertIn("evidence_context", summary.failed_checks)

    def test_full_evidence_context_cross_reference(self) -> None:
        fixture = CooDispatchIsolatedCloneFixture()
        fixture.start()
        try:
            seeded = fixture.seed_bundle_and_confirmation()
            ticket_id = seeded["ticket"].ticket_id
            confirmation_id = seeded["confirmation"].confirmation_id
            fixture.write_binding_state("bound")
            config = bounded_dispatch_config(fixture.pipeline_root)
            with (
                _successful_attestation(),
                patch(
                    "agent.coo.dispatch_cli_gateway_readiness.evaluate_production_cutover_checklist",
                    return_value=_ready_cutover_summary(),
                ),
            ):
                summary = evaluate_dispatch_gateway_readiness(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    pipeline_root=str(fixture.pipeline_root),
                    merged_config=config,
                )
            self.assertEqual(summary.evidence_context_requested, EVIDENCE_CONTEXT_FULL)
            self.assertEqual(summary.operator_readiness_status, "ready")
            statuses = {check.name: check.status for check in summary.checks}
            self.assertEqual(statuses["file_backed_bundle_available"], "PASS")
            self.assertEqual(statuses["file_backed_confirmation_available"], "PASS")
            self.assertEqual(statuses["consume_state_clear"], "PASS")
            self.assertEqual(statuses["repair_lock_clear"], "PASS")
            self.assertEqual(statuses["operator_readiness"], "PASS")
        finally:
            fixture.stop()


class TestGatewayReadinessFailClosed(unittest.TestCase):
    def test_consume_partial_not_ready(self) -> None:
        fixture = CooDispatchIsolatedCloneFixture()
        fixture.start()
        try:
            seeded = fixture.seed_bundle_and_confirmation()
            ticket_id = seeded["ticket"].ticket_id
            confirmation_id = seeded["confirmation"].confirmation_id
            with patch(
                "agent.coo.dispatch_cli_gateway_readiness.assess_consume_status",
            ) as mock_consume:
                from agent.coo.dispatch_consume_transaction import CooDispatchConsumeStatus

                mock_consume.return_value = CooDispatchConsumeStatus(
                    consume_state=CONSUME_STATE_PARTIAL,
                    transaction_id="tx-1",
                    execution_attempt_id="attempt-1",
                    bundle_consumed=True,
                    confirmation_consumed=False,
                    recovery_required=True,
                    repair_attempt_id="",
                )
                summary = evaluate_dispatch_gateway_readiness(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    pipeline_root=str(fixture.pipeline_root),
                    merged_config=bounded_dispatch_config(fixture.pipeline_root),
                )
            self.assertIn("consume_state_clear", summary.failed_checks)
            self.assertFalse(summary.gateway_readiness_ready)
        finally:
            fixture.stop()

    def test_recovery_required_not_ready(self) -> None:
        fixture = CooDispatchIsolatedCloneFixture()
        fixture.start()
        try:
            seeded = fixture.seed_bundle_and_confirmation()
            with patch(
                "agent.coo.dispatch_cli_gateway_readiness.assess_consume_status",
            ) as mock_consume:
                from agent.coo.dispatch_consume_transaction import CooDispatchConsumeStatus

                mock_consume.return_value = CooDispatchConsumeStatus(
                    consume_state=CONSUME_STATE_RECOVERY_REQUIRED,
                    transaction_id="tx-1",
                    execution_attempt_id="attempt-1",
                    bundle_consumed=False,
                    confirmation_consumed=False,
                    recovery_required=True,
                    repair_attempt_id="repair-1",
                )
                summary = evaluate_dispatch_gateway_readiness(
                    ticket_id=seeded["ticket"].ticket_id,
                    confirmation_id=seeded["confirmation"].confirmation_id,
                    pipeline_root=str(fixture.pipeline_root),
                    merged_config=bounded_dispatch_config(fixture.pipeline_root),
                )
            self.assertIn("consume_state_clear", summary.failed_checks)
        finally:
            fixture.stop()

    def test_repair_lock_held_fails(self) -> None:
        fixture = CooDispatchIsolatedCloneFixture()
        fixture.start()
        try:
            seeded = fixture.seed_bundle_and_confirmation()
            ticket_id = seeded["ticket"].ticket_id
            confirmation_id = seeded["confirmation"].confirmation_id
            from agent.coo.dispatch_consume_repair_lock import (
                CooDispatchConsumeRepairLockStatus,
            )

            with patch(
                "agent.coo.dispatch_cli_consume_repair_lock.summarize_consume_repair_lock_status",
            ) as mock_lock:
                mock_lock.return_value = CooDispatchConsumeRepairLockStatus(
                    lock_present=True,
                    lock_acquirable=False,
                    repair_in_progress=True,
                    stale_unknown=False,
                )
                summary = evaluate_dispatch_gateway_readiness(
                    ticket_id=ticket_id,
                    confirmation_id=confirmation_id,
                    pipeline_root=str(fixture.pipeline_root),
                    merged_config=bounded_dispatch_config(fixture.pipeline_root),
                )
            self.assertIn("repair_lock_clear", summary.failed_checks)
            self.assertTrue(summary.repair_in_progress)
        finally:
            fixture.stop()

    def test_production_signoff_not_ready_fails(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_gateway_readiness.evaluate_dispatch_production_signoff",
        ) as mock_signoff:
            from agent.coo.dispatch_cli_production_signoff import (
                CooDispatchProductionSignoffSummary,
            )

            mock_signoff.return_value = CooDispatchProductionSignoffSummary(
                signoff_ready=False,
                overall_status="SIGNOFF_NOT_READY",
                checks_passed_count=0,
                checks_blocked_count=0,
                checks_failed_count=1,
                failed_checks="repository_attestation_valid",
                blocked_checks="(none)",
                repository_attested=False,
                production_root_hard_deny=True,
                execution_allowed=False,
                gateway_enabled=False,
                recommended_next_phase="x",
                operator_action="x",
            )
            summary = evaluate_dispatch_gateway_readiness(merged_config={})
        self.assertIn("production_signoff_ready", summary.failed_checks)

    def test_production_cutover_not_ready_fails(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_gateway_readiness.evaluate_production_cutover_checklist",
            return_value=CooDispatchProductionCutoverSummary(
                cutover_ready=False,
                overall_status="NOT_READY",
                checks_passed_count=0,
                checks_blocked_count=0,
                checks_failed_count=1,
                failed_checks="production_signoff_ready",
                blocked_checks="(none)",
                fleet_status="NOT_READY",
                ticket_count=0,
                ready_ticket_count=0,
                failed_ticket_count=0,
                production_execution_allowed=False,
                gateway_enabled=False,
                production_root_hard_deny=True,
                recommended_action="x",
                recommended_next_phase="x",
            ),
        ):
            summary = evaluate_dispatch_gateway_readiness(merged_config={})
        self.assertIn("production_cutover_ready", summary.failed_checks)

    def test_production_policy_violation_fails(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_gateway_readiness._production_root_hard_deny_active",
            return_value=False,
        ):
            summary = evaluate_dispatch_gateway_readiness(merged_config={})
        self.assertIn("production_root_hard_deny", summary.failed_checks)


class TestGatewayReadinessSafeOutput(unittest.TestCase):
    def test_format_includes_required_fields(self) -> None:
        summary = evaluate_dispatch_gateway_readiness(merged_config={})
        output = format_dispatch_gateway_readiness_summary(summary)
        self.assertIn("Gateway Readiness", output)
        self.assertIn("gateway_state:", output)
        self.assertIn("readiness_level:", output)
        self.assertIn("gateway_readiness_ready:", output)
        self.assertIn("gateway_execution_allowed: false", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("recommended_action:", output)
        self.assertIn("recommended_next_phase:", output)

    def test_safe_output_has_no_forbidden_tokens(self) -> None:
        summary = evaluate_dispatch_gateway_readiness(merged_config={})
        output = format_dispatch_gateway_readiness_summary(summary).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)

    def test_cli_gateway_readiness(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["gateway", "readiness"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 1)
        self.assertIn("gateway_state: disabled", buf.getvalue())


class TestGatewayReadinessReadOnly(unittest.TestCase):
    def test_no_subprocess_invocation(self) -> None:
        with (
            patch("subprocess.run", side_effect=AssertionError("subprocess.run called")),
            patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen called")),
        ):
            evaluate_dispatch_gateway_readiness(merged_config=_gateway_config("staged"))

    def test_repository2_digest_unchanged(self) -> None:
        repo_root = Path("/opt/data/multi-content-pipeline")
        before = _hermes_digest(repo_root) if repo_root.exists() else ""
        evaluate_dispatch_gateway_readiness(merged_config={})
        if repo_root.exists():
            self.assertEqual(before, _hermes_digest(repo_root))

    def test_hermes_home_digest_unchanged(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            before = _hermes_digest(hermes_home)
            with patch.dict("os.environ", {"HERMES_HOME": str(hermes_home)}):
                evaluate_dispatch_gateway_readiness(merged_config={})
            self.assertEqual(before, _hermes_digest(hermes_home))


class TestGatewayReadinessRegression(unittest.TestCase):
    def test_gateway_status_cli_still_works(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(["gateway", "status"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        self.assertIn("gateway_state:", buf.getvalue())

    def test_production_readiness_gateway_still_blocked(self) -> None:
        from agent.coo.dispatch_cli_production_readiness import (
            evaluate_dispatch_production_readiness,
        )

        summary = evaluate_dispatch_production_readiness(merged_config={})
        gateway = next(check for check in summary.checks if check.name == "gateway")
        self.assertEqual(gateway.status, CHECK_BLOCKED)

    def test_enabled_gateway_check_still_fail(self) -> None:
        from agent.coo.dispatch_cli_production_readiness import (
            evaluate_dispatch_production_readiness,
        )

        summary = evaluate_dispatch_production_readiness(
            merged_config=_gateway_config("enabled"),
        )
        gateway = next(check for check in summary.checks if check.name == "gateway")
        self.assertEqual(gateway.status, CHECK_FAIL)


if __name__ == "__main__":
    unittest.main()
