"""Phase 15D tests — production governed runtime session contract."""

from __future__ import annotations

import hashlib
import pickle
import subprocess
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_consume_transaction import (
    CONSUME_STATE_PARTIAL,
    CooDispatchConsumeStatus,
)
from agent.coo.dispatch_gateway_operator_dashboard import (
    build_operator_dashboard_summary,
)
from agent.coo.production_activation_execution_reservation import (
    load_execution_reservation,
)
from agent.coo.production_controlled_window import (
    REASON_INCIDENT_DETECTED,
    REASON_OPERATOR_CLOSE,
    close_production_controlled_window,
    emergency_close_production_controlled_window,
)
from agent.coo.production_governed_cutover import (
    load_governed_cutover_contract,
)
from agent.coo.production_governed_runtime_session import (
    ACTION_START_GOVERNED_RUNTIME_SESSION,
    BLOCK_CONTROLLED_WINDOW_CLOSED,
    BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED,
    BLOCK_CONTROLLED_WINDOW_EXPIRED,
    BLOCK_EMERGENCY_CLOSE_UNAVAILABLE,
    BLOCK_IDENTITY_SEPARATION_INVALID,
    BLOCK_KILL_SWITCH_UNAVAILABLE,
    BLOCK_OPERATOR_IDENTITY_INVALID,
    BLOCK_PERMISSION_EXECUTOR_MISMATCH,
    BLOCK_PRODUCTION_EXECUTION_ENABLED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_RUNTIME_PERMISSION_MISSING,
    BLOCK_SESSION_EXPIRED,
    BLOCK_SESSION_TTL_EXCEEDS_PERMISSION,
    BLOCK_SESSION_TTL_INVALID,
    RELEASE_GOVERNED_RUNTIME_SESSION_STARTED,
    SCOPE_TYPE_ONE_SHOT,
    SESSION_EXPIRED,
    SESSION_READY,
    SESSION_STARTED,
    GovernedRuntimeSessionContext,
    ProductionGovernedRuntimeSessionError,
    build_production_governed_runtime_session_release_summary,
    evaluate_production_governed_runtime_session,
    format_production_governed_runtime_session_status,
    load_governed_runtime_session_events,
    load_governed_runtime_session_record,
    resolve_latest_governed_runtime_session_dashboard_digest,
    start_production_governed_runtime_session,
)
from agent.coo.production_runtime_permission import (
    issue_production_runtime_permission,
    load_runtime_permission_record,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli import test_production_runtime_permission as _prm
from tests.hermes_cli.test_production_controlled_window import _WINDOW_OPERATOR

_RUNTIME_EXECUTOR = _prm._RUNTIME_EXECUTOR
_PERMISSION_OPERATOR = _prm._PERMISSION_OPERATOR
_SESSION_OPERATOR = "session-operator-phase15d"


class TestProductionGovernedRuntimeSession(_prm.TestProductionRuntimePermission):
    def setUp(self) -> None:
        super().setUp()
        self.session_store_dir = (
            self.hermes_home / "coo" / "production-governed-runtime-session"
        )
        self.session_store_dir.mkdir(parents=True, exist_ok=True)

    def _issue_permission(self) -> tuple[str, str]:
        activation_id = self._open_window()
        issue_production_runtime_permission(**self._perm_kwargs(activation_id))
        record = load_runtime_permission_record(
            activation_id, store_dir=self.permission_store_dir
        )
        assert record is not None
        return activation_id, record.permission_id

    def _sess_kwargs(self, activation_id: str, permission_id: str, **overrides):
        base = {
            **self._eval_kwargs(activation_id),
            "session_store_dir": self.session_store_dir,
            "permission_store_dir": self.permission_store_dir,
            "executor_id": _RUNTIME_EXECUTOR,
            "operator_id": _SESSION_OPERATOR,
            "permission_id": permission_id,
            "ttl_seconds": 120,
        }
        base.update(overrides)
        return base

    def _context_kwargs(self, activation_id: str, session_id: str, **overrides):
        base = {
            "session_id": session_id,
            "activation_request_id": activation_id,
            "session_store_dir": self.session_store_dir,
            "permission_store_dir": self.permission_store_dir,
            "governed_cutover_store_dir": self.governed_cutover_store_dir,
            "window_store_dir": self.window_store_dir,
            "final_signoff_store_dir": self.final_signoff_store_dir,
            "signoff_store_dir": self.signoff_store_dir,
            "store_dir": self.store_dir,
            "reservation_dir": self.reservation_dir,
            "runtime_history_dir": self.runtime_history_dir,
            "evidence_dir": self.evidence_dir,
            "audit_dir": self.audit_dir,
            "bundle_dir": self.bundle_dir,
            "confirmation_dir": self.confirmation_dir,
            "transaction_dir": self.transaction_dir,
            "e2e_history_dir": self.e2e_history_dir,
            "validation_store_dir": self.validation_store_dir,
            "preflight_history_dir": self.preflight_history_dir,
            "repo_root": self.repo_root,
            "merged_config": {},
            "now": self._now,
        }
        base.update(overrides)
        return base

    def _digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _correlated_digests(self, activation_id: str) -> tuple[str, str, str, str]:
        contract_path = self.governed_cutover_store_dir / f"{activation_id}.json"
        window_path = self.window_store_dir / f"{activation_id}.json"
        permission_path = self.permission_store_dir / f"{activation_id}.json"
        activation_path = self.store_dir / f"{activation_id}.json"
        return (
            self._digest(contract_path),
            self._digest(window_path),
            self._digest(permission_path),
            self._digest(activation_path),
        )

    # -- 1. check readiness --------------------------------------------------

    def test_check_ready_when_permission_issued_and_window_open(self) -> None:
        activation_id, permission_id = self._issue_permission()
        summary = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id, operator_id="")
        )
        self.assertEqual(summary.session_state, SESSION_READY)
        self.assertTrue(summary.session_ready)
        self.assertEqual(
            summary.recommended_action, ACTION_START_GOVERNED_RUNTIME_SESSION
        )
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.permission_consumed)

    # -- 2. read-only digests unchanged --------------------------------------

    def test_check_read_only_digests_unchanged(self) -> None:
        activation_id, permission_id = self._issue_permission()
        before = self._correlated_digests(activation_id)
        evaluate_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id, operator_id="")
        )
        after = self._correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertFalse(list(self.session_store_dir.glob("*.json")))

    # -- 3. start success -----------------------------------------------------

    def test_start_success_append_only(self) -> None:
        activation_id, permission_id = self._issue_permission()
        before = self._correlated_digests(activation_id)
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                summary = start_production_governed_runtime_session(
                    **self._sess_kwargs(activation_id, permission_id)
                )
        after = self._correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertEqual(summary.session_state, SESSION_STARTED)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.permission_consumed)
        self.assertFalse(summary.permission_revoked)
        self.assertFalse(summary.production_execution_allowed)
        events = load_governed_runtime_session_events(
            activation_id, store_dir=self.session_store_dir
        )
        types = [event.event_type for event in events]
        self.assertIn("session_started", types)
        self.assertIn("runtime_boundary_blocked_waiting_phase_15e", types)
        record = load_governed_runtime_session_record(
            activation_id, store_dir=self.session_store_dir
        )
        assert record is not None
        self.assertEqual(record.scope_type, SCOPE_TYPE_ONE_SHOT)

    # -- 4. missing permission -------------------------------------------------

    def test_missing_permission_blocked(self) -> None:
        activation_id = self._open_window()
        summary = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, "")
        )
        self.assertIn(BLOCK_RUNTIME_PERMISSION_MISSING, summary.blocking_items)
        with self.assertRaises(ProductionGovernedRuntimeSessionError):
            start_production_governed_runtime_session(
                **self._sess_kwargs(activation_id, "")
            )

    # -- 5. window not open / closed / emergency-closed / expired -------------

    def test_closed_window_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                reason_code=REASON_OPERATOR_CLOSE,
            )
        )
        summary = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id)
        )
        self.assertIn(BLOCK_CONTROLLED_WINDOW_CLOSED, summary.blocking_items)

    def test_emergency_closed_window_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        emergency_close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                actor_role="operator",
                reason_code=REASON_INCIDENT_DETECTED,
            )
        )
        summary = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id)
        )
        self.assertIn(BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED, summary.blocking_items)

    def test_expired_window_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        later = self._now + timedelta(hours=2)
        summary = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id, now=later)
        )
        self.assertTrue(summary.controlled_window_expired)
        self.assertIn(BLOCK_CONTROLLED_WINDOW_EXPIRED, summary.blocking_items)

    # -- 6. TTL bounds ----------------------------------------------------------

    def test_ttl_bounds_low_and_high_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        low = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id, ttl_seconds=20)
        )
        self.assertIn(BLOCK_SESSION_TTL_INVALID, low.blocking_items)
        high = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id, ttl_seconds=301)
        )
        self.assertIn(BLOCK_SESSION_TTL_INVALID, high.blocking_items)
        with self.assertRaises(ProductionGovernedRuntimeSessionError):
            start_production_governed_runtime_session(
                **self._sess_kwargs(activation_id, permission_id, ttl_seconds=20)
            )

    def test_ttl_exceeds_permission_blocked(self) -> None:
        activation_id = self._open_window()
        issued = issue_production_runtime_permission(
            **self._perm_kwargs(activation_id, ttl_seconds=60)
        )
        summary = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, issued.permission_id, ttl_seconds=120)
        )
        self.assertIn(BLOCK_SESSION_TTL_EXCEEDS_PERMISSION, summary.blocking_items)
        with self.assertRaises(ProductionGovernedRuntimeSessionError):
            start_production_governed_runtime_session(
                **self._sess_kwargs(
                    activation_id, issued.permission_id, ttl_seconds=120
                )
            )

    # -- 7. executor mismatch ----------------------------------------------------

    def test_executor_mismatch_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        summary = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(
                activation_id, permission_id, executor_id="other-runtime-executor"
            )
        )
        self.assertIn(BLOCK_PERMISSION_EXECUTOR_MISMATCH, summary.blocking_items)
        with self.assertRaises(ProductionGovernedRuntimeSessionError):
            start_production_governed_runtime_session(
                **self._sess_kwargs(
                    activation_id, permission_id, executor_id="other-runtime-executor"
                )
            )

    # -- 8. identity separation ---------------------------------------------------

    def test_operator_identity_conflicts(self) -> None:
        activation_id, permission_id = self._issue_permission()
        same_as_executor = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(
                activation_id, permission_id, operator_id=_RUNTIME_EXECUTOR
            )
        )
        self.assertTrue(
            BLOCK_IDENTITY_SEPARATION_INVALID in same_as_executor.blocking_items
            or BLOCK_OPERATOR_IDENTITY_INVALID in same_as_executor.blocking_items
        )
        same_as_issuer = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(
                activation_id, permission_id, operator_id=_PERMISSION_OPERATOR
            )
        )
        self.assertIn(BLOCK_OPERATOR_IDENTITY_INVALID, same_as_issuer.blocking_items)

    # -- 9. recovery required -------------------------------------------------------

    def test_recovery_required_blocked(self) -> None:
        activation_id = self._open_window()
        reservation = load_execution_reservation(
            activation_id, store_dir=self.reservation_dir
        )
        assert reservation is not None
        partial = CooDispatchConsumeStatus(
            consume_state=CONSUME_STATE_PARTIAL,
            transaction_id="tx-partial",
            execution_attempt_id=reservation.execution_attempt_id,
            bundle_consumed=True,
            confirmation_consumed=False,
            recovery_required=True,
        )
        with (
            patch(
                "agent.coo.production_live_operational_signoff.assess_consume_status",
                return_value=partial,
            ),
            patch(
                "agent.coo.production_live_rollback_validation.assess_consume_status",
                return_value=partial,
            ),
        ):
            summary = evaluate_production_governed_runtime_session(
                **self._sess_kwargs(
                    activation_id, "", operator_id="", executor_id=""
                )
            )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, summary.blocking_items)

    # -- 10. force flags never leak into the summary --------------------------------

    def test_force_flags_blocked_summary_stays_false(self) -> None:
        activation_id, permission_id = self._issue_permission()
        forced = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(
                activation_id,
                permission_id,
                force_production_execution_allowed=True,
                force_gateway_enabled=True,
                force_discord_enabled=True,
                force_cutover_started=True,
                force_runtime_invoked=True,
            )
        )
        self.assertIn(BLOCK_PRODUCTION_EXECUTION_ENABLED, forced.blocking_items)
        self.assertFalse(forced.production_execution_allowed)
        self.assertFalse(forced.cutover_started)
        self.assertFalse(forced.runtime_invoked)
        self.assertFalse(forced.permission_consumed)
        self.assertFalse(forced.permission_revoked)

    # -- 11. failsafe availability ---------------------------------------------------

    def test_force_kill_switch_and_emergency_close_unavailable_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        kill = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(
                activation_id, permission_id, force_kill_switch_unavailable=True
            )
        )
        self.assertIn(BLOCK_KILL_SWITCH_UNAVAILABLE, kill.blocking_items)
        emergency = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(
                activation_id, permission_id, force_emergency_close_unavailable=True
            )
        )
        self.assertIn(BLOCK_EMERGENCY_CLOSE_UNAVAILABLE, emergency.blocking_items)

    # -- 12. idempotent duplicate start -----------------------------------------------

    def test_duplicate_same_start_idempotent(self) -> None:
        activation_id, permission_id = self._issue_permission()
        first = start_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id)
        )
        second = start_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id)
        )
        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual(second.session_state, SESSION_STARTED)
        paths = list(self.session_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 13. changed executor/TTL conflicts --------------------------------------------

    def test_duplicate_changed_executor_conflict(self) -> None:
        activation_id, permission_id = self._issue_permission()
        start_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id)
        )
        with self.assertRaises(ProductionGovernedRuntimeSessionError) as ctx:
            start_production_governed_runtime_session(
                **self._sess_kwargs(
                    activation_id, permission_id, executor_id="other-runtime-executor"
                )
            )
        self.assertIn("governed_runtime_session_conflict", str(ctx.exception))

    def test_duplicate_changed_ttl_conflict(self) -> None:
        activation_id, permission_id = self._issue_permission()
        start_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id)
        )
        with self.assertRaises(ProductionGovernedRuntimeSessionError) as ctx:
            start_production_governed_runtime_session(
                **self._sess_kwargs(activation_id, permission_id, ttl_seconds=60)
            )
        self.assertIn("governed_runtime_session_conflict", str(ctx.exception))

    # -- 14. concurrent start ----------------------------------------------------------

    def test_concurrent_start_one_success(self) -> None:
        activation_id, permission_id = self._issue_permission()
        results: list[str] = []
        errors: list[str] = []

        def worker() -> None:
            try:
                summary = start_production_governed_runtime_session(
                    **self._sess_kwargs(activation_id, permission_id)
                )
                results.append(summary.session_id)
            except ProductionGovernedRuntimeSessionError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(results)
        self.assertEqual(len(set(results)), 1)
        paths = list(self.session_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 15. expiry and restart ---------------------------------------------------------

    def test_expired_session_derived_and_restart_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        started = start_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id, ttl_seconds=60)
        )
        self.assertEqual(started.session_state, SESSION_STARTED)
        later = self._now + timedelta(seconds=61)
        expired = evaluate_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id, now=later)
        )
        self.assertEqual(expired.session_state, SESSION_EXPIRED)
        self.assertIn(BLOCK_SESSION_EXPIRED, expired.blocking_items)
        with self.assertRaises(ProductionGovernedRuntimeSessionError) as ctx:
            start_production_governed_runtime_session(
                **self._sess_kwargs(activation_id, permission_id, now=later)
            )
        self.assertIn("session_expired", str(ctx.exception))

    # -- 16. GovernedRuntimeSessionContext ------------------------------------------------

    def test_context_valid_nested_reuse_and_pickle_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        started = start_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id)
        )
        context = GovernedRuntimeSessionContext(
            **self._context_kwargs(activation_id, started.session_id)
        )
        with context as ctx:
            self.assertTrue(ctx.active)
            self.assertEqual(ctx.session_id, started.session_id)
            self.assertEqual(ctx.permission_id, permission_id)
            with self.assertRaises(ProductionGovernedRuntimeSessionError):
                with context:
                    pass
        self.assertFalse(context.active)
        with self.assertRaises(ProductionGovernedRuntimeSessionError):
            with context:
                pass
        with self.assertRaises(ProductionGovernedRuntimeSessionError):
            pickle.dumps(context)

    # -- 17. safe output + CLI parsers --------------------------------------------------

    def test_safe_output_and_cli_parsers(self) -> None:
        activation_id, permission_id = self._issue_permission()
        started = start_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id)
        )
        output = format_production_governed_runtime_session_status(started)
        self.assertIn("session_state:", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertNotIn(_RUNTIME_EXECUTOR, output)
        self.assertNotIn(_SESSION_OPERATOR, output)
        self.assertNotIn(_PERMISSION_OPERATOR, output)
        self.assertNotIn(str(self.hermes_home), output)
        self.assertNotIn("/opt/data/", output)
        parser = build_coo_dispatch_parser()
        for cmd, handler in (
            (
                [
                    "production", "governed-cutover", "session", "status",
                    "--activation-request-id", activation_id,
                ],
                "_cmd_production_governed_runtime_session_status",
            ),
            (
                [
                    "production", "governed-cutover", "session", "check",
                    "--activation-request-id", activation_id,
                    "--permission-id", permission_id,
                    "--executor-id", _RUNTIME_EXECUTOR,
                ],
                "_cmd_production_governed_runtime_session_check",
            ),
            (
                [
                    "production", "governed-cutover", "session", "start",
                    "--activation-request-id", activation_id,
                    "--permission-id", permission_id,
                    "--executor-id", _RUNTIME_EXECUTOR,
                    "--operator-id", _SESSION_OPERATOR,
                    "--ttl-seconds", "120",
                ],
                "_cmd_production_governed_runtime_session_start",
            ),
            (
                [
                    "production", "governed-cutover", "session", "show",
                    "--session-id", started.session_id,
                ],
                "_cmd_production_governed_runtime_session_show",
            ),
            (
                [
                    "production", "governed-cutover", "session", "history",
                    "--activation-request-id", activation_id,
                ],
                "_cmd_production_governed_runtime_session_history",
            ),
        ):
            args = parser.parse_args(cmd)
            self.assertEqual(args.handler.__name__, handler)
        legacy = parser.parse_args(
            ["production", "cutover-check", "--ticket-id", "ticket-x"]
        )
        self.assertEqual(legacy.handler.__name__, "_cmd_production_cutover_check")

    # -- 18. dashboard + release summary --------------------------------------------------

    def test_dashboard_and_release_summary(self) -> None:
        activation_id, permission_id = self._issue_permission()
        started = start_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id)
        )
        digest = resolve_latest_governed_runtime_session_dashboard_digest(
            governed_cutover_store_dir=self.governed_cutover_store_dir,
            window_store_dir=self.window_store_dir,
            permission_store_dir=self.permission_store_dir,
            session_store_dir=self.session_store_dir,
            final_signoff_store_dir=self.final_signoff_store_dir,
            store_dir=self.store_dir,
            reservation_dir=self.reservation_dir,
            runtime_history_dir=self.runtime_history_dir,
            evidence_dir=self.evidence_dir,
            audit_dir=self.audit_dir,
            bundle_dir=self.bundle_dir,
            confirmation_dir=self.confirmation_dir,
            transaction_dir=self.transaction_dir,
            e2e_history_dir=self.e2e_history_dir,
            signoff_store_dir=self.signoff_store_dir,
            validation_store_dir=self.validation_store_dir,
            preflight_history_dir=self.preflight_history_dir,
            repo_root=self.repo_root,
            merged_config={},
            now=self._now,
        )
        self.assertEqual(digest.governed_runtime_session_state, SESSION_STARTED)
        self.assertTrue(digest.governed_runtime_session_present)
        self.assertEqual(digest.governed_runtime_session_id, started.session_id)
        dashboard = build_operator_dashboard_summary(merged_config={})
        self.assertTrue(hasattr(dashboard, "governed_runtime_session_state"))
        release = build_production_governed_runtime_session_release_summary(started)
        self.assertEqual(release.release_status, RELEASE_GOVERNED_RUNTIME_SESSION_STARTED)
        self.assertEqual(release.next_phase, "Phase_15E_runtime_boundary")
        self.assertFalse(release.production_execution_allowed)
        self.assertFalse(release.cutover_started)
        self.assertFalse(release.runtime_invoked)
        self.assertFalse(release.permission_consumed)
        self.assertFalse(release.permission_revoked)
        contract = load_governed_cutover_contract(
            activation_id, store_dir=self.governed_cutover_store_dir
        )
        assert contract is not None
        self.assertFalse(contract.cutover_started)

    # -- 19. no subprocess on start ------------------------------------------------------

    def test_no_subprocess_on_start(self) -> None:
        activation_id, permission_id = self._issue_permission()
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                start_production_governed_runtime_session(
                    **self._sess_kwargs(activation_id, permission_id)
                )


if __name__ == "__main__":
    unittest.main()
