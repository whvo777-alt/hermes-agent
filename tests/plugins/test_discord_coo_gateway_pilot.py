"""Phase 13K tests — Discord approval to gateway pilot bridge."""

from __future__ import annotations

import subprocess
import unittest
from contextlib import contextmanager, ExitStack
from pathlib import Path
from unittest.mock import patch

from agent.coo.approval_session import CEOApprovalSessionStatus
from agent.coo.dispatch_gateway_discord_bridge import (
    ACTION_GATEWAY_PILOT_DRY_RUN,
    ACTION_GATEWAY_PILOT_RUN,
    ACTION_GATEWAY_PILOT_STATUS,
    DISCORD_GATEWAY_PILOT_RESULT_KEY,
    build_discord_gateway_request_id,
    execute_discord_gateway_pilot_action,
    format_discord_gateway_pilot_response,
)
from agent.coo.dispatch_gateway_request_store import read_gateway_request
from agent.coo.dispatch_pilot_history import read_pilot_history_record
from plugins.platforms.discord import coo_approval
from plugins.platforms.discord.coo_approval import (
    build_coo_approval_components,
    execute_coo_approval_button_action,
    parse_coo_approval_custom_id,
)
from tests.hermes_cli.test_coo_dispatch_gateway_pilot import _GatewayPilotFixture
from tests.hermes_cli.test_coo_dispatch_gateway_readiness import (
    _ready_cutover_summary,
)
from tests.hermes_cli.test_coo_dispatch_production_signoff import (
    _successful_attestation,
)
from tests.hermes_cli.test_coo_dispatch_run import (
    _mock_runner_failure,
    _mock_runner_success,
    _mock_runner_timeout,
)


_FORBIDDEN_RESPONSE_TOKENS = (
    "unlock",
    "token",
    "pipeline_root",
    "/opt/data/multi-content-pipeline",
    "argv",
    "cwd",
    "env",
    "stdout",
    "stderr",
    "phrase",
    "secret",
)


def _staged_config(root: Path) -> dict:
    return {
        "coo": {
            "dispatch": {
                "executor": {
                    "enabled": True,
                    "allowed_pipeline_roots": [str(root)],
                },
                "gateway": {"enablement": "staged"},
            },
        },
    }


class TestDiscordGatewayPilotBridge(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _GatewayPilotFixture()
        self.fixture.start()
        self.fixture.write_binding_state("bound")
        self.fixture.pipeline_root.mkdir(parents=True, exist_ok=True)
        self.fixture.ctx = self.fixture.seed_pilot_context()
        self.attestation = _successful_attestation()
        self.attestation.__enter__()

    def tearDown(self) -> None:
        self.attestation.__exit__(None, None, None)
        self.fixture.stop()

    def _session_payload(self) -> dict:
        session = self.fixture.session_store.get(self.fixture.ctx["session_id"])
        assert session is not None
        return session.to_dict()

    def _snapshot(self) -> dict:
        return {
            "can_run": True,
            "unlock_token": {"token_id": self.fixture.ctx["unlock_token_id"]},
            "dispatch_request": {"dispatch_request_id": "dispatch-request-1"},
        }

    @contextmanager
    def _pilot_context(self, *, interaction_id: str = "interaction-1"):
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(subprocess, "run", side_effect=AssertionError("no subprocess"))
            )
            stack.enter_context(
                patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess"))
            )
            stack.enter_context(
                patch(
                    "agent.coo.bounded_subprocess_runner.create_bounded_subprocess_runner",
                    side_effect=AssertionError("no bounded runner"),
                )
            )
            stack.enter_context(
                patch(
                    "agent.coo.dispatch_gateway_discord_bridge._latest_dispatch_snapshot",
                    return_value=self._snapshot(),
                )
            )
            stack.enter_context(
                patch.object(
                    coo_approval,
                    "_lookup_latest_execution_review_for_session",
                    return_value={"gate": {"status": "approved"}},
                )
            )
            stack.enter_context(
                patch.object(
                    coo_approval,
                    "_lookup_latest_dispatch_for_session",
                    return_value=self._snapshot(),
                )
            )
            stack.enter_context(
                patch(
                    "agent.coo.dispatch_cli_gateway_pilot.evaluate_production_cutover_checklist",
                    return_value=_ready_cutover_summary(),
                )
            )
            yield

    def _execute_action(
        self,
        *,
        action: str,
        interaction_id: str = "interaction-1",
        runner=None,
    ) -> dict:
        with self._pilot_context(interaction_id=interaction_id):
            return execute_coo_approval_button_action(
                action=action,
                session_id=self.fixture.ctx["session_id"],
                discord_user_id=self.fixture.ctx["ticket"].requester_id,
                store=self.fixture.session_store,
                discord_interaction_id=interaction_id,
                gateway_pilot_runner=runner,
                gateway_pilot_config=_staged_config(self.fixture.pipeline_root),
                gateway_pilot_bundle_dir=self.fixture.bundle_dir,
                gateway_pilot_confirmation_dir=self.fixture.confirmation_dir,
                gateway_pilot_request_dir=self.fixture.gateway_request_dir(),
                gateway_pilot_history_dir=self.fixture.pilot_history_dir(),
            )

    def test_authorized_staged_dry_run_button_success_no_runner(self) -> None:
        runner_calls = {"count": 0}

        def counting_runner(*_args, **_kwargs):
            runner_calls["count"] += 1
            return 0, "", ""

        before = self.fixture.session_store.get(self.fixture.ctx["session_id"])
        assert before is not None
        self.assertFalse(before.execution_dispatched)

        result = self._execute_action(
            action=ACTION_GATEWAY_PILOT_DRY_RUN,
            runner=counting_runner,
        )

        pilot = result[DISCORD_GATEWAY_PILOT_RESULT_KEY]
        self.assertEqual(pilot["status"], "completed")
        self.assertTrue(pilot["dry_run"])
        self.assertEqual(runner_calls["count"], 0)
        after = self.fixture.session_store.get(self.fixture.ctx["session_id"])
        assert after is not None
        self.assertFalse(after.execution_dispatched)

    def test_authorized_staged_live_mock_success(self) -> None:
        result = self._execute_action(
            action=ACTION_GATEWAY_PILOT_RUN,
            runner=_mock_runner_success,
        )
        pilot = result[DISCORD_GATEWAY_PILOT_RESULT_KEY]
        self.assertEqual(pilot["status"], "completed")
        self.assertFalse(pilot["dry_run"])
        self.assertTrue(pilot["dispatch_run_id"])
        record = read_gateway_request(
            pilot["gateway_request_id"],
            request_dir=self.fixture.gateway_request_dir(),
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.session_id, self.fixture.ctx["session_id"])
        history = read_pilot_history_record(
            pilot["pilot_attempt_id"],
            history_dir=self.fixture.pilot_history_dir(),
        )
        self.assertEqual(history.gateway_request_id, pilot["gateway_request_id"])

    def test_non_zero_failure_no_consume(self) -> None:
        failed = self._execute_action(
            action=ACTION_GATEWAY_PILOT_RUN,
            interaction_id="interaction-fail",
            runner=_mock_runner_failure,
        )[DISCORD_GATEWAY_PILOT_RESULT_KEY]

        self.assertEqual(failed["status"], "failed")

    def test_timeout_failure_no_consume(self) -> None:
        timed_out = self._execute_action(
            action=ACTION_GATEWAY_PILOT_RUN,
            interaction_id="interaction-timeout",
            runner=_mock_runner_timeout,
        )[DISCORD_GATEWAY_PILOT_RESULT_KEY]

        self.assertEqual(timed_out["status"], "failed")

    def test_unauthorized_requester_denied(self) -> None:
        with self.assertRaises(ValueError):
            execute_coo_approval_button_action(
                action=ACTION_GATEWAY_PILOT_DRY_RUN,
                session_id=self.fixture.ctx["session_id"],
                discord_user_id="wrong-user",
                store=self.fixture.session_store,
            )

    def test_missing_and_rejected_sessions_blocked(self) -> None:
        with self.assertRaises(KeyError):
            execute_coo_approval_button_action(
                action=ACTION_GATEWAY_PILOT_DRY_RUN,
                session_id="missing-session",
                discord_user_id=self.fixture.ctx["ticket"].requester_id,
                store=self.fixture.session_store,
            )
        session = self.fixture.session_store.get(self.fixture.ctx["session_id"])
        assert session is not None
        session.status = CEOApprovalSessionStatus.REJECTED
        self.fixture.session_store.save(session)
        with self.assertRaises(ValueError):
            self._execute_action(action=ACTION_GATEWAY_PILOT_DRY_RUN)

    def test_session_ticket_and_confirmation_mismatches_blocked(self) -> None:
        session = self.fixture.session_store.get(self.fixture.ctx["session_id"])
        assert session is not None
        session.execution_ticket_id = "wrong-ticket"
        self.fixture.session_store.save(session)
        result = self._execute_action(action=ACTION_GATEWAY_PILOT_DRY_RUN)
        self.assertEqual(
            result[DISCORD_GATEWAY_PILOT_RESULT_KEY]["failure_reason_code"],
            "bundle_mismatch",
        )

        session.execution_ticket_id = self.fixture.ctx["ticket"].ticket_id
        self.fixture.session_store.save(session)
        with self._pilot_context(), patch(
            "agent.coo.dispatch_gateway_discord_bridge._find_confirmation_for_unlock_token",
            return_value=None,
        ):
            bridge = execute_discord_gateway_pilot_action(
                action=ACTION_GATEWAY_PILOT_DRY_RUN,
                session_payload=self._session_payload(),
                requester_id=self.fixture.ctx["ticket"].requester_id,
                interaction_id="interaction-missing-confirm",
                merged_config=_staged_config(self.fixture.pipeline_root),
                session_store=self.fixture.session_store,
            )
        self.assertEqual(bridge.failure_reason_code, "confirmation_missing")

    def test_disabled_and_enabled_blocked(self) -> None:
        with self._pilot_context():
            disabled = execute_discord_gateway_pilot_action(
                action=ACTION_GATEWAY_PILOT_DRY_RUN,
                session_payload=self._session_payload(),
                requester_id=self.fixture.ctx["ticket"].requester_id,
                interaction_id="interaction-disabled",
                merged_config={"coo": {"dispatch": {"gateway": {"enablement": "disabled"}}}},
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
            )
        self.assertEqual(disabled.failure_reason_code, "gateway_disabled")

        with self._pilot_context():
            enabled = execute_discord_gateway_pilot_action(
                action=ACTION_GATEWAY_PILOT_DRY_RUN,
                session_payload=self._session_payload(),
                requester_id=self.fixture.ctx["ticket"].requester_id,
                interaction_id="interaction-enabled",
                merged_config={"coo": {"dispatch": {"gateway": {"enablement": "enabled"}}}},
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                request_dir=self.fixture.gateway_request_dir(),
                history_dir=self.fixture.pilot_history_dir(),
            )
        self.assertEqual(
            enabled.failure_reason_code,
            "enabled_state_not_supported_for_gateway_pilot",
        )

    def test_regression_fail_blocks_before_runner(self) -> None:
        calls = {"count": 0}

        def runner(*_args, **_kwargs):
            calls["count"] += 1
            return 0, "", ""

        with self._pilot_context(), patch(
            "agent.coo.dispatch_cli_pilot_regression_gate.evaluate_pilot_regression_gate",
        ) as mock_gate:
            from agent.coo.dispatch_cli_pilot_regression_gate import (
                CooDispatchPilotRegressionGateSummary,
            )

            mock_gate.return_value = CooDispatchPilotRegressionGateSummary(
                regression_status="FAIL",
                regression_gate="blocked_for_live",
                live_pilot_allowed=False,
                dry_run_allowed=True,
                consecutive_failures=2,
                total_attempts=2,
                latest_status="failure",
                latest_pilot_attempt_id="pilot-1",
                production_policy_violations=0,
            )
            result = execute_discord_gateway_pilot_action(
                action=ACTION_GATEWAY_PILOT_RUN,
                session_payload=self._session_payload(),
                requester_id=self.fixture.ctx["ticket"].requester_id,
                interaction_id="interaction-regression",
                merged_config=_staged_config(self.fixture.pipeline_root),
                injected_runner=runner,
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
                request_dir=self.fixture.gateway_request_dir(),
                history_dir=self.fixture.pilot_history_dir(),
            )
        self.assertEqual(result.failure_reason_code, "gateway_pilot_readiness_failed")
        self.assertEqual(calls["count"], 0)

    def test_duplicate_discord_interaction_idempotent(self) -> None:
        first = self._execute_action(
            action=ACTION_GATEWAY_PILOT_DRY_RUN,
            interaction_id="same-interaction",
        )[DISCORD_GATEWAY_PILOT_RESULT_KEY]
        second = self._execute_action(
            action=ACTION_GATEWAY_PILOT_DRY_RUN,
            interaction_id="same-interaction",
        )[DISCORD_GATEWAY_PILOT_RESULT_KEY]
        self.assertEqual(first["gateway_request_id"], second["gateway_request_id"])
        self.assertEqual(second["status"], "already_completed")

    def test_live_runner_missing_blocked(self) -> None:
        result = self._execute_action(
            action=ACTION_GATEWAY_PILOT_RUN,
            interaction_id="interaction-no-runner",
            runner=None,
        )[DISCORD_GATEWAY_PILOT_RESULT_KEY]
        self.assertEqual(result["failure_reason_code"], "gateway_mock_runner_not_configured")

    def test_safe_response_forbidden_fields_absent(self) -> None:
        result = self._execute_action(
            action=ACTION_GATEWAY_PILOT_RUN,
            interaction_id="interaction-safe",
            runner=_mock_runner_success,
        )[DISCORD_GATEWAY_PILOT_RESULT_KEY]
        response = format_discord_gateway_pilot_response(
            execute_discord_gateway_pilot_action(
                action=ACTION_GATEWAY_PILOT_STATUS,
                session_payload=self._session_payload(),
                requester_id=self.fixture.ctx["ticket"].requester_id,
                interaction_id="interaction-status",
                merged_config=_staged_config(self.fixture.pipeline_root),
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
            )
        ).lower()
        response += "\n".join(f"{key}: {value}" for key, value in result.items()).lower()
        for token in _FORBIDDEN_RESPONSE_TOKENS:
            self.assertNotIn(token.lower(), response)

    def test_components_include_gateway_pilot_buttons_when_dispatch_ready(self) -> None:
        session_payload = self._session_payload()
        with patch.object(coo_approval, "_lookup_latest_execution_review_for_session") as review, patch.object(
            coo_approval,
            "_lookup_latest_dispatch_for_session",
            return_value=self._snapshot(),
        ):
            review.return_value = {"gate": {"status": "approved"}}
            components = build_coo_approval_components(session_payload)
        labels = [button["label"] for button in components]
        self.assertIn("Gateway Pilot Dry Run", labels)
        self.assertIn("Gateway Pilot Run", labels)
        self.assertIn("Gateway Pilot Status", labels)

    def test_parse_gateway_pilot_custom_ids(self) -> None:
        parsed = parse_coo_approval_custom_id(
            f"coo_approval:{ACTION_GATEWAY_PILOT_DRY_RUN}:{self.fixture.ctx['session_id']}"
        )
        self.assertEqual(parsed["action"], ACTION_GATEWAY_PILOT_DRY_RUN)

    def test_gateway_request_id_is_opaque_and_stable(self) -> None:
        request_id = build_discord_gateway_request_id(
            session_id=self.fixture.ctx["session_id"],
            action=ACTION_GATEWAY_PILOT_DRY_RUN,
            interaction_id="interaction-opaque",
        )
        self.assertTrue(request_id.startswith("discord-"))
        self.assertNotIn("/", request_id)
        self.assertNotIn("\\", request_id)


if __name__ == "__main__":
    unittest.main()
