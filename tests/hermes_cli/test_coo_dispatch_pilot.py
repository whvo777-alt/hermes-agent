"""Phase 13A tests — isolated operational dispatch pilot CLI."""

from __future__ import annotations

import hashlib
import io
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_pilot import (
    EXECUTION_SCOPE_ISOLATED_CLONE,
    OPERATOR_ACTION_APPROVE_ISOLATED_DRILL,
    OPERATOR_ACTION_RESOLVE_FAILED,
    assert_pilot_dispatch_allowed,
    evaluate_pilot_readiness,
    format_dispatch_pilot_readiness,
)
from agent.coo.dispatch_cli_repository_attestation import (
    CooDispatchRepositoryAttestationSummary,
)
from agent.coo.execution_dispatch_runtime import DispatchExecutionRunStatus
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli.coo_dispatch_isolated_clone_fixture import (
    CLONE_BEHAVIOR_SUCCESS,
    CooDispatchIsolatedCloneFixture,
    bounded_dispatch_config,
    run_clone_full_path_execute,
    write_clone_fake_node,
)
from tests.hermes_cli.test_coo_dispatch_run import _DEFAULT_DISABLED_EXECUTOR_CONFIG

_FORBIDDEN_OUTPUT_TOKENS = (
    "argv",
    "cwd",
    "env",
    "stdout",
    "stderr",
    "snapshot",
    "token",
    "SECRET",
    "PASSWORD",
    "phrase",
    "dependencies",
    "node pipeline.js",
    "npm",
    "npx",
    "/opt/data/multi-content-pipeline",
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


def _attestation_success_summary() -> CooDispatchRepositoryAttestationSummary:
    return CooDispatchRepositoryAttestationSummary(
        repository_attested=True,
        root_matches_expected=True,
        root_is_symlink=False,
        pipeline_entrypoint_present=True,
        package_manifest_present=True,
        required_directories_present_count=3,
        required_directories_missing="(none)",
        optional_directories_present="outputs",
        pipeline_sha256="a" * 64,
        package_sha256="b" * 64,
        git_metadata_present=True,
        git_head_kind="symbolic_ref",
        git_head_value="master@abcdef012345",
        execution_allowed=False,
        production_root_hard_deny=True,
        recommended_next_phase="Phase 13A Isolated Operational Dispatch Pilot",
    )


@contextmanager
def _successful_signoff():
    with patch(
        "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
        return_value=_attestation_success_summary(),
    ):
        yield


class TestPilotReadinessGlobal(unittest.TestCase):
    def test_signoff_and_binding_ready_without_pair(self) -> None:
        with (
            _successful_signoff(),
            patch(
                "agent.coo.dispatch_cli_pilot.load_dispatch_runner_binding_state",
            ) as mock_binding,
            patch(
                "agent.coo.dispatch_cli_pilot.runner_binding_state_is_bound",
                return_value=True,
            ),
            patch(
                "agent.coo.dispatch_cli_pilot.evaluate_dispatch_runtime_enablement",
            ) as mock_runtime,
        ):
            from agent.coo.dispatch_cli_enablement import CooDispatchEnablementSummary

            mock_binding.return_value.state = "bound"
            mock_runtime.return_value = CooDispatchEnablementSummary(
                enablement_ready=True,
                runner_bound=True,
                runner_binding_state="bound",
            )
            summary = evaluate_pilot_readiness()
        self.assertTrue(summary.pilot_ready)
        self.assertTrue(summary.signoff_ready)
        self.assertTrue(summary.runner_bound)
        self.assertTrue(summary.runtime_enablement_ready)
        self.assertEqual(summary.operator_ready, "not_evaluated")
        self.assertEqual(summary.pipeline_root_trusted, "not_evaluated")
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.gateway_enabled)
        self.assertTrue(summary.production_root_hard_deny)
        self.assertEqual(summary.execution_scope, EXECUTION_SCOPE_ISOLATED_CLONE)
        self.assertEqual(summary.operator_action, OPERATOR_ACTION_APPROVE_ISOLATED_DRILL)

    def test_signoff_not_ready_blocks_pilot(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
            side_effect=ValueError("attestation failed"),
        ):
            summary = evaluate_pilot_readiness()
        self.assertFalse(summary.pilot_ready)
        self.assertIn("production_signoff", summary.failed_checks)
        self.assertEqual(summary.operator_action, OPERATOR_ACTION_RESOLVE_FAILED)

    def test_runner_unbound_blocks_pilot(self) -> None:
        from agent.coo.dispatch_runner_binding_state import (
            DispatchRunnerBindingStateError,
        )

        with (
            _successful_signoff(),
            patch(
                "agent.coo.dispatch_cli_pilot.load_dispatch_runner_binding_state",
                side_effect=DispatchRunnerBindingStateError("invalid"),
            ),
        ):
            summary = evaluate_pilot_readiness()
        self.assertFalse(summary.pilot_ready)
        self.assertIn("runner_binding", summary.failed_checks)

    def test_gateway_enabled_blocks_pilot(self) -> None:
        with (
            _successful_signoff(),
            patch(
                "agent.coo.dispatch_cli_pilot.evaluate_dispatch_production_signoff",
            ) as mock_signoff,
        ):
            from agent.coo.dispatch_cli_production_signoff import (
                CooDispatchProductionSignoffSummary,
            )

            mock_signoff.return_value = CooDispatchProductionSignoffSummary(
                signoff_ready=True,
                overall_status="SIGNOFF_READY",
                checks_passed_count=11,
                checks_blocked_count=3,
                checks_failed_count=0,
                failed_checks="(none)",
                blocked_checks="gateway_disabled",
                repository_attested=True,
                production_root_hard_deny=True,
                execution_allowed=False,
                gateway_enabled=True,
                recommended_next_phase="x",
                operator_action="x",
            )
            summary = evaluate_pilot_readiness()
        self.assertFalse(summary.pilot_ready)
        self.assertIn("gateway_enabled", summary.failed_checks)


class TestPilotReadinessWithFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CooDispatchIsolatedCloneFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_operator_and_pipeline_checks_pass(self) -> None:
        with _successful_signoff():
            summary = evaluate_pilot_readiness(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                pipeline_root=str(self.fixture.pipeline_root),
                merged_config=bounded_dispatch_config(self.fixture.pipeline_root),
            )
        self.assertTrue(summary.pilot_ready)
        self.assertEqual(summary.operator_ready, "true")
        self.assertEqual(summary.pipeline_root_trusted, "true")

    def test_production_root_rejected(self) -> None:
        with _successful_signoff():
            summary = evaluate_pilot_readiness(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                pipeline_root="/opt/data/multi-content-pipeline",
                merged_config=bounded_dispatch_config(self.fixture.pipeline_root),
            )
        self.assertFalse(summary.pilot_ready)
        self.assertEqual(summary.pipeline_root_trusted, "false")

    def test_outside_allowlist_rejected(self) -> None:
        other_root = self.fixture.pipeline_root.parent / "other-clone"
        other_root.mkdir()
        with _successful_signoff():
            summary = evaluate_pilot_readiness(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                pipeline_root=str(other_root),
                merged_config=bounded_dispatch_config(self.fixture.pipeline_root),
            )
        self.assertFalse(summary.pilot_ready)
        self.assertIn("pipeline_root_allowlist", summary.failed_checks)


class TestPilotRun(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CooDispatchIsolatedCloneFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()
        self.workspace = self.fixture.pipeline_root.parent

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_pilot_run_success_isolated_clone(self) -> None:
        fake_node = write_clone_fake_node(self.workspace, CLONE_BEHAVIOR_SUCCESS)
        with (
            _successful_signoff(),
            patch.object(subprocess, "run", wraps=subprocess.run),
        ):
            result = run_clone_full_path_execute(
                self.fixture,
                self.seeded,
                fake_node=fake_node,
            )
        self.assertTrue(result.consumed)
        self.assertEqual(result.status, DispatchExecutionRunStatus.COMPLETED.value)

    def test_pilot_gate_blocks_production_root_before_subprocess(self) -> None:
        with (
            _successful_signoff(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            with self.assertRaises(ValueError) as exc:
                assert_pilot_dispatch_allowed(
                    ticket_id=self.seeded["ticket"].ticket_id,
                    confirmation_id=self.seeded["confirmation"].confirmation_id,
                    pipeline_root="/opt/data/multi-content-pipeline",
                    merged_config=bounded_dispatch_config(self.fixture.pipeline_root),
                )
        self.assertIn("pilot is not ready", str(exc.exception))

    def test_pilot_gate_blocks_signoff_not_ready(self) -> None:
        with (
            patch(
                "agent.coo.dispatch_cli_production_signoff.attest_repository2_production_root",
                side_effect=ValueError("attestation failed"),
            ),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
        ):
            with self.assertRaises(ValueError):
                assert_pilot_dispatch_allowed(
                    ticket_id=self.seeded["ticket"].ticket_id,
                    confirmation_id=self.seeded["confirmation"].confirmation_id,
                    pipeline_root=str(self.fixture.pipeline_root),
                    merged_config=bounded_dispatch_config(self.fixture.pipeline_root),
                )


class TestPilotCli(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CooDispatchIsolatedCloneFixture()
        self.fixture.start()
        self.seeded = self.fixture.seed_bundle_and_confirmation()

    def tearDown(self) -> None:
        self.fixture.stop()

    def test_cli_readiness_exit_zero_when_ready(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "pilot",
                "readiness",
                "--pipeline-root",
                str(self.fixture.pipeline_root),
                "--ticket-id",
                self.seeded["ticket"].ticket_id,
                "--confirmation-id",
                self.seeded["confirmation"].confirmation_id,
            ],
        )
        buf = io.StringIO()
        with (
            _successful_signoff(),
            patch("sys.stdout", buf),
            patch(
                "hermes_cli.config.load_config",
                return_value=bounded_dispatch_config(self.fixture.pipeline_root),
            ),
        ):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        self.assertIn("pilot_ready: true", buf.getvalue())

    def test_cli_run_dry_run_exit_zero(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "pilot",
                "run",
                "--ticket-id",
                self.seeded["ticket"].ticket_id,
                "--unlock-token-id",
                self.seeded["prepare"]["unlock_token"]["token_id"],
                "--confirmation-id",
                self.seeded["confirmation"].confirmation_id,
                "--requester-id",
                self.seeded["ticket"].requester_id,
                "--pipeline-root",
                str(self.fixture.pipeline_root),
                "--dry-run",
            ],
        )
        buf = io.StringIO()
        with (
            _successful_signoff(),
            patch("sys.stdout", buf),
            patch(
                "hermes_cli.config.load_config",
                return_value=bounded_dispatch_config(self.fixture.pipeline_root),
            ),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        output = buf.getvalue()
        self.assertIn("execution_scope: isolated_clone", output)
        self.assertIn("production_execution_allowed: false", output)

    def test_safe_output_has_no_forbidden_tokens(self) -> None:
        with _successful_signoff():
            summary = evaluate_pilot_readiness(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                pipeline_root=str(self.fixture.pipeline_root),
                merged_config=bounded_dispatch_config(self.fixture.pipeline_root),
            )
        output = format_dispatch_pilot_readiness(summary).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)

    def test_readiness_read_only_no_writes(self) -> None:
        before = _hermes_digest(self.fixture.hermes_home)
        with (
            _successful_signoff(),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            evaluate_pilot_readiness(
                ticket_id=self.seeded["ticket"].ticket_id,
                confirmation_id=self.seeded["confirmation"].confirmation_id,
                pipeline_root=str(self.fixture.pipeline_root),
                merged_config=bounded_dispatch_config(self.fixture.pipeline_root),
            )
        self.assertEqual(_hermes_digest(self.fixture.hermes_home), before)


class TestPilotRegression(unittest.TestCase):
    def test_production_signoff_still_denies_execution(self) -> None:
        with _successful_signoff():
            summary = evaluate_pilot_readiness()
        self.assertFalse(summary.production_execution_allowed)

    def test_executor_disabled_blocks_runtime_enablement(self) -> None:
        with _successful_signoff():
            summary = evaluate_pilot_readiness(
                merged_config=_DEFAULT_DISABLED_EXECUTOR_CONFIG,
            )
        self.assertFalse(summary.runtime_enablement_ready)
        self.assertFalse(summary.pilot_ready)
