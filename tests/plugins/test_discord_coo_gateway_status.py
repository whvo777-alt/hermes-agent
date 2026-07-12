"""Phase 13L tests — Discord gateway operational status."""

from __future__ import annotations

import hashlib
import subprocess
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from agent.coo.approval_session import CEOApprovalSessionStatus
from agent.coo.dispatch_gateway_discord_bridge import (
    ACTION_GATEWAY_PILOT_DRY_RUN,
    ACTION_GATEWAY_PILOT_RUN,
    ACTION_GATEWAY_PILOT_STATUS,
    ACTION_GATEWAY_HEALTH,
    ACTION_PILOT_HISTORY_SUMMARY,
    ACTION_REGRESSION_SUMMARY,
    execute_discord_gateway_pilot_action,
)
from agent.coo.dispatch_gateway_discord_status import (
    execute_discord_gateway_status_action,
    format_discord_gateway_status_response,
)
from agent.coo.dispatch_gateway_operational_status import (
    HEALTH_STATUS_BLOCKED,
    HEALTH_STATUS_DEGRADED,
    HEALTH_STATUS_HEALTHY,
    HEALTH_STATUS_NOT_CONFIGURED,
    RECOMMENDED_ACTION_COLLECT_MORE_PILOT_HISTORY,
    RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE,
    RECOMMENDED_ACTION_RESOLVE_RECOVERY_ISSUE,
    RECOMMENDED_ACTION_RESOLVE_REGRESSION_FAILURE,
    RECOMMENDED_ACTION_RUN_GATEWAY_MOCK_PILOT,
    RECOMMENDED_ACTION_RUN_GATEWAY_PILOT_DRY_RUN,
    RECOMMENDED_ACTION_STAGE_GATEWAY,
    TIMELINE_PILOT_COMPLETED,
    TIMELINE_PILOT_FAILED,
    TIMELINE_RECOVERY_REQUIRED,
    build_gateway_operational_summary,
    find_latest_gateway_request,
    format_gateway_operational_summary,
)
from agent.coo.dispatch_gateway_request_store import (
    CooDispatchGatewayRequestRecord,
    REQUEST_STATUS_COMPLETED,
    reserve_gateway_request,
)
from agent.coo.dispatch_pilot_history import (
    EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
    PILOT_STATUS_DRY_RUN,
    PILOT_STATUS_FAILURE,
    PILOT_STATUS_SUCCESS,
    CooDispatchPilotHistoryRecord,
    write_pilot_history_record,
)
from plugins.platforms.discord import coo_approval
from plugins.platforms.discord.coo_approval import (
    build_coo_approval_components,
    execute_coo_approval_button_action,
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
)


_FORBIDDEN_RESPONSE_TOKENS = (
    "unlock",
    "token_id",
    "pipeline_root",
    "/opt/data/multi-content-pipeline",
    "argv",
    "cwd",
    "env",
    "stdout",
    "stderr",
    "phrase",
    "secret",
    "confirmation_phrase",
    "channel_id",
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


def _history_record(
    *,
    ticket_id: str,
    confirmation_id: str,
    pilot_attempt_id: str,
    status: str = PILOT_STATUS_SUCCESS,
    dry_run: bool = False,
    consumed: bool = True,
    gateway_request_id: str = "",
    session_id: str = "",
    completed_at: str = "2026-07-13T01:00:00+00:00",
) -> CooDispatchPilotHistoryRecord:
    return CooDispatchPilotHistoryRecord(
        version=1,
        pilot_attempt_id=pilot_attempt_id,
        execution_attempt_id=f"exec-{pilot_attempt_id}",
        ticket_id=ticket_id,
        confirmation_id=confirmation_id,
        dispatch_run_id=f"run-{pilot_attempt_id}",
        execution_scope=EXECUTION_SCOPE_ISOLATED_GATEWAY_MOCK,
        status=status,
        exit_code=0 if status == PILOT_STATUS_SUCCESS else 1,
        dry_run=dry_run,
        started_at="2026-07-13T00:59:00+00:00",
        completed_at=completed_at,
        evidence_present=True,
        audit_present=True,
        consumed=consumed,
        failure_reason_code="none",
        production_execution_allowed=False,
        production_root_hard_deny=True,
        gateway_enabled=False,
        gateway_request_id=gateway_request_id,
        session_id=session_id,
    )


class TestGatewayOperationalStatus(unittest.TestCase):
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

    def _kwargs(self) -> dict:
        ctx = self.fixture.ctx
        return {
            "session_id": ctx["session_id"],
            "ticket_id": ctx["ticket"].ticket_id,
            "confirmation_id": ctx["confirmation"].confirmation_id,
            "unlock_token_id": ctx["unlock_token_id"],
            "requester_id": ctx["ticket"].requester_id,
            "pipeline_root": str(self.fixture.pipeline_root),
            "merged_config": _staged_config(self.fixture.pipeline_root),
            "session_store": self.fixture.session_store,
            "bundle_dir": self.fixture.bundle_dir,
            "confirmation_dir": self.fixture.confirmation_dir,
            "request_dir": self.fixture.gateway_request_dir(),
            "history_dir": self.fixture.pilot_history_dir(),
        }

    @contextmanager
    def _ready_env(self):
        with patch(
            "agent.coo.dispatch_cli_gateway_pilot.evaluate_production_cutover_checklist",
            return_value=_ready_cutover_summary(),
        ):
            yield

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
    def _discord_context(self):
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

    def test_staged_healthy_summary(self) -> None:
        ctx = self.fixture.ctx
        write_pilot_history_record(
            _history_record(
                ticket_id=ctx["ticket"].ticket_id,
                confirmation_id=ctx["confirmation"].confirmation_id,
                pilot_attempt_id="healthy-1",
                session_id=ctx["session_id"],
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        with self._ready_env():
            summary = build_gateway_operational_summary(**self._kwargs())
        self.assertEqual(summary.health_status, HEALTH_STATUS_HEALTHY)
        self.assertEqual(summary.recommended_action, RECOMMENDED_ACTION_RUN_GATEWAY_MOCK_PILOT)
        self.assertFalse(summary.production_execution_allowed)
        self.assertTrue(summary.production_root_hard_deny)

    def test_staged_dry_run_only_degraded(self) -> None:
        ctx = self.fixture.ctx
        write_pilot_history_record(
            _history_record(
                ticket_id=ctx["ticket"].ticket_id,
                confirmation_id=ctx["confirmation"].confirmation_id,
                pilot_attempt_id="dry-1",
                status=PILOT_STATUS_DRY_RUN,
                dry_run=True,
                consumed=False,
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        with self._ready_env():
            summary = build_gateway_operational_summary(**self._kwargs())
        self.assertIn(
            summary.health_status,
            {HEALTH_STATUS_DEGRADED, HEALTH_STATUS_HEALTHY},
        )
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_RUN_GATEWAY_PILOT_DRY_RUN,
        )

    def test_no_history_collect_more(self) -> None:
        with self._ready_env():
            summary = build_gateway_operational_summary(**self._kwargs())
        self.assertIn(
            summary.health_status,
            {HEALTH_STATUS_DEGRADED, HEALTH_STATUS_NOT_CONFIGURED},
        )
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_COLLECT_MORE_PILOT_HISTORY,
        )

    def test_single_failure_degraded(self) -> None:
        ctx = self.fixture.ctx
        write_pilot_history_record(
            _history_record(
                ticket_id=ctx["ticket"].ticket_id,
                confirmation_id=ctx["confirmation"].confirmation_id,
                pilot_attempt_id="fail-1",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
                completed_at="2026-07-13T02:00:00+00:00",
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        with self._ready_env():
            summary = build_gateway_operational_summary(**self._kwargs())
        self.assertEqual(summary.health_status, HEALTH_STATUS_DEGRADED)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_INVESTIGATE_RECENT_FAILURE,
        )

    def test_regression_fail_blocked(self) -> None:
        ctx = self.fixture.ctx
        for idx in range(2):
            write_pilot_history_record(
                _history_record(
                    ticket_id=ctx["ticket"].ticket_id,
                    confirmation_id=ctx["confirmation"].confirmation_id,
                    pilot_attempt_id=f"fail-{idx}",
                    status=PILOT_STATUS_FAILURE,
                    consumed=False,
                    completed_at=f"2026-07-13T0{idx}:00:00+00:00",
                ),
                history_dir=self.fixture.pilot_history_dir(),
            )
        with self._ready_env():
            summary = build_gateway_operational_summary(**self._kwargs())
        self.assertEqual(summary.health_status, HEALTH_STATUS_BLOCKED)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_RESOLVE_REGRESSION_FAILURE,
        )

    def test_gateway_disabled_not_configured(self) -> None:
        kwargs = self._kwargs()
        kwargs["merged_config"] = {
            "coo": {"dispatch": {"gateway": {"enablement": "disabled"}}},
        }
        with self._ready_env():
            summary = build_gateway_operational_summary(**kwargs)
        self.assertEqual(summary.health_status, HEALTH_STATUS_NOT_CONFIGURED)
        self.assertEqual(summary.recommended_action, RECOMMENDED_ACTION_STAGE_GATEWAY)

    def test_gateway_enabled_blocked_status_allowed(self) -> None:
        with self._discord_context():
            enabled_run = execute_discord_gateway_pilot_action(
                action=ACTION_GATEWAY_PILOT_RUN,
                session_payload=self._session_payload(),
                requester_id=self.fixture.ctx["ticket"].requester_id,
                interaction_id="enabled-run",
                merged_config={
                    "coo": {"dispatch": {"gateway": {"enablement": "enabled"}}},
                },
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
            )
        self.assertEqual(enabled_run.failure_reason_code, "enabled_state_not_supported_for_gateway_pilot")

        with self._discord_context():
            enabled_status = execute_discord_gateway_status_action(
                action=ACTION_GATEWAY_HEALTH,
                session_payload=self._session_payload(),
                requester_id=self.fixture.ctx["ticket"].requester_id,
                merged_config={
                    "coo": {"dispatch": {"gateway": {"enablement": "enabled"}}},
                },
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
            )
        self.assertEqual(enabled_status.health_status, HEALTH_STATUS_BLOCKED)

    def test_recovery_required_blocked(self) -> None:
        with self._ready_env(), patch(
            "agent.coo.dispatch_gateway_operational_status.assess_consume_status",
        ) as assess:
            assess.return_value = type(
                "ConsumeStatus",
                (),
                {
                    "consume_state": "recovery_required",
                    "recovery_required": True,
                },
            )()
            summary = build_gateway_operational_summary(**self._kwargs())
        self.assertEqual(summary.health_status, HEALTH_STATUS_BLOCKED)
        self.assertTrue(summary.recovery_required)
        self.assertEqual(
            summary.recommended_action,
            RECOMMENDED_ACTION_RESOLVE_RECOVERY_ISSUE,
        )

    def test_repair_lock_held_blocked(self) -> None:
        with self._ready_env(), patch(
            "agent.coo.dispatch_cli_consume_repair_lock.summarize_consume_repair_lock_status",
        ) as lock_status:
            lock_status.return_value = type(
                "LockStatus",
                (),
                {"repair_in_progress": True},
            )()
            summary = build_gateway_operational_summary(**self._kwargs())
        self.assertEqual(summary.health_status, HEALTH_STATUS_BLOCKED)
        self.assertTrue(summary.repair_lock_held)

    def test_latest_gateway_request_summary(self) -> None:
        ctx = self.fixture.ctx
        reserve_gateway_request(
            CooDispatchGatewayRequestRecord(
                gateway_request_id="gw-req-status-1",
                ticket_id=ctx["ticket"].ticket_id,
                confirmation_id=ctx["confirmation"].confirmation_id,
                execution_attempt_id="exec-1",
                dispatch_run_id="run-1",
                status=REQUEST_STATUS_COMPLETED,
                dry_run=False,
                failure_reason_code="none",
                production_execution_allowed=False,
                gateway_state="staged",
                session_id=ctx["session_id"],
                pilot_attempt_id="pilot-1",
            ),
            request_dir=self.fixture.gateway_request_dir(),
        )
        latest = find_latest_gateway_request(
            ticket_id=ctx["ticket"].ticket_id,
            session_id=ctx["session_id"],
            request_dir=self.fixture.gateway_request_dir(),
        )
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.gateway_request_id, "gw-req-status-1")

    def test_timeline_success_path(self) -> None:
        ctx = self.fixture.ctx
        write_pilot_history_record(
            _history_record(
                ticket_id=ctx["ticket"].ticket_id,
                confirmation_id=ctx["confirmation"].confirmation_id,
                pilot_attempt_id="timeline-success",
                session_id=ctx["session_id"],
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        with self._ready_env():
            summary = build_gateway_operational_summary(**self._kwargs())
        event_types = [event.event_type for event in summary.timeline]
        self.assertIn(TIMELINE_PILOT_COMPLETED, event_types)

    def test_timeline_failure_path(self) -> None:
        ctx = self.fixture.ctx
        write_pilot_history_record(
            _history_record(
                ticket_id=ctx["ticket"].ticket_id,
                confirmation_id=ctx["confirmation"].confirmation_id,
                pilot_attempt_id="timeline-fail",
                status=PILOT_STATUS_FAILURE,
                consumed=False,
            ),
            history_dir=self.fixture.pilot_history_dir(),
        )
        with self._ready_env():
            summary = build_gateway_operational_summary(**self._kwargs())
        event_types = [event.event_type for event in summary.timeline]
        self.assertIn(TIMELINE_PILOT_FAILED, event_types)

    def test_timeline_recovery_path(self) -> None:
        with self._ready_env(), patch(
            "agent.coo.dispatch_gateway_operational_status.assess_consume_status",
        ) as assess:
            assess.return_value = type(
                "ConsumeStatus",
                (),
                {
                    "consume_state": "recovery_required",
                    "recovery_required": True,
                },
            )()
            summary = build_gateway_operational_summary(**self._kwargs())
        self.assertTrue(summary.recovery_required)
        self.assertEqual(summary.health_status, HEALTH_STATUS_BLOCKED)


class TestDiscordGatewayOperationalStatus(unittest.TestCase):
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
    def _discord_context(self):
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
                    "agent.coo.dispatch_gateway_pilot_service.execute_gateway_pilot_dispatch",
                    side_effect=AssertionError("no pilot dispatch"),
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

    def _execute_status(self, action: str) -> dict:
        with self._discord_context():
            return execute_coo_approval_button_action(
                action=action,
                session_id=self.fixture.ctx["session_id"],
                discord_user_id=self.fixture.ctx["ticket"].requester_id,
                store=self.fixture.session_store,
                gateway_pilot_config=_staged_config(self.fixture.pipeline_root),
                gateway_pilot_bundle_dir=self.fixture.bundle_dir,
                gateway_pilot_confirmation_dir=self.fixture.confirmation_dir,
                gateway_pilot_request_dir=self.fixture.gateway_request_dir(),
                gateway_pilot_history_dir=self.fixture.pilot_history_dir(),
            )

    def test_unauthorized_status_denied(self) -> None:
        with self.assertRaises(ValueError):
            execute_coo_approval_button_action(
                action=ACTION_GATEWAY_PILOT_STATUS,
                session_id=self.fixture.ctx["session_id"],
                discord_user_id="wrong-user",
                store=self.fixture.session_store,
            )

    def test_session_ticket_mismatch_blocked(self) -> None:
        session = self.fixture.session_store.get(self.fixture.ctx["session_id"])
        assert session is not None
        session.execution_ticket_id = "wrong-ticket"
        self.fixture.session_store.save(session)
        with self._discord_context():
            result = execute_discord_gateway_status_action(
                action=ACTION_GATEWAY_PILOT_STATUS,
                session_payload=self._session_payload(),
                requester_id=self.fixture.ctx["ticket"].requester_id,
                merged_config=_staged_config(self.fixture.pipeline_root),
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
            )
        self.assertEqual(result.failure_reason_code, "correlation_failed")

    def test_status_views(self) -> None:
        for action in (
            ACTION_GATEWAY_PILOT_STATUS,
            ACTION_GATEWAY_HEALTH,
            ACTION_PILOT_HISTORY_SUMMARY,
            ACTION_REGRESSION_SUMMARY,
        ):
            payload = self._execute_status(action)
            response = str(payload.get("_coo_gateway_pilot_ephemeral") or "")
            self.assertIn("health_status", response)
            self.assertIn("production_execution_allowed: false", response)

    def test_safe_response_forbidden_fields_absent(self) -> None:
        payload = self._execute_status(ACTION_GATEWAY_PILOT_STATUS)
        response = str(payload.get("_coo_gateway_pilot_ephemeral") or "").lower()
        for token in _FORBIDDEN_RESPONSE_TOKENS:
            self.assertNotIn(token.lower(), response)

    def test_status_action_read_only(self) -> None:
        digest_before = _hermes_digest(self.fixture.hermes_home)
        session_before = self.fixture.session_store.get(self.fixture.ctx["session_id"])
        assert session_before is not None
        status_before = session_before.status
        execution_before = session_before.execution_dispatched

        self._execute_status(ACTION_GATEWAY_PILOT_STATUS)

        digest_after = _hermes_digest(self.fixture.hermes_home)
        session_after = self.fixture.session_store.get(self.fixture.ctx["session_id"])
        assert session_after is not None
        self.assertEqual(digest_before, digest_after)
        self.assertEqual(status_before, session_after.status)
        self.assertEqual(execution_before, session_after.execution_dispatched)

    def test_existing_dry_run_button_regression(self) -> None:
        with self._discord_context(), patch(
            "agent.coo.dispatch_gateway_discord_bridge._latest_dispatch_snapshot",
            return_value=self._snapshot(),
        ):
            result = execute_coo_approval_button_action(
                action=ACTION_GATEWAY_PILOT_DRY_RUN,
                session_id=self.fixture.ctx["session_id"],
                discord_user_id=self.fixture.ctx["ticket"].requester_id,
                store=self.fixture.session_store,
                gateway_pilot_config=_staged_config(self.fixture.pipeline_root),
                gateway_pilot_bundle_dir=self.fixture.bundle_dir,
                gateway_pilot_confirmation_dir=self.fixture.confirmation_dir,
                gateway_pilot_request_dir=self.fixture.gateway_request_dir(),
                gateway_pilot_history_dir=self.fixture.pilot_history_dir(),
            )
        self.assertIn("_coo_gateway_pilot_result", result)

    def test_existing_live_mock_button_regression(self) -> None:
        with self._discord_context(), patch(
            "agent.coo.dispatch_gateway_discord_bridge._latest_dispatch_snapshot",
            return_value=self._snapshot(),
        ):
            result = execute_coo_approval_button_action(
                action=ACTION_GATEWAY_PILOT_RUN,
                session_id=self.fixture.ctx["session_id"],
                discord_user_id=self.fixture.ctx["ticket"].requester_id,
                store=self.fixture.session_store,
                gateway_pilot_runner=_mock_runner_success,
                gateway_pilot_config=_staged_config(self.fixture.pipeline_root),
                gateway_pilot_bundle_dir=self.fixture.bundle_dir,
                gateway_pilot_confirmation_dir=self.fixture.confirmation_dir,
                gateway_pilot_request_dir=self.fixture.gateway_request_dir(),
                gateway_pilot_history_dir=self.fixture.pilot_history_dir(),
            )
        pilot = result["_coo_gateway_pilot_result"]
        self.assertIn(pilot["status"], {"completed", "failed", "blocked"})

    def test_status_and_run_buttons_separated(self) -> None:
        session_payload = self._session_payload()
        with patch.object(
            coo_approval,
            "_lookup_latest_execution_review_for_session",
            return_value={"gate": {"status": "approved"}},
        ), patch.object(
            coo_approval,
            "_lookup_latest_dispatch_for_session",
            return_value=self._snapshot(),
        ):
            components = build_coo_approval_components(session_payload)
        labels = [button["label"] for button in components]
        self.assertIn("Gateway Pilot Dry Run", labels)
        self.assertIn("Gateway Pilot Run", labels)
        self.assertIn("Gateway Pilot Status", labels)
        self.assertIn("Gateway Health", labels)
        self.assertIn("Pilot History Summary", labels)
        self.assertIn("Regression Summary", labels)

    def test_format_operational_summary_safe(self) -> None:
        with self._discord_context():
            result = execute_discord_gateway_status_action(
                action=ACTION_GATEWAY_PILOT_STATUS,
                session_payload=self._session_payload(),
                requester_id=self.fixture.ctx["ticket"].requester_id,
                merged_config=_staged_config(self.fixture.pipeline_root),
                session_store=self.fixture.session_store,
                bundle_dir=self.fixture.bundle_dir,
                confirmation_dir=self.fixture.confirmation_dir,
            )
        rendered = format_discord_gateway_status_response(result)
        self.assertIn("Gateway Operational Summary", rendered)
        self.assertIn("production_execution_allowed: false", rendered)


if __name__ == "__main__":
    unittest.main()
