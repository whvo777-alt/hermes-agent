"""Phase 15E tests — production runtime boundary contract."""

from __future__ import annotations

import pickle
import subprocess
import threading
import unittest
from datetime import timedelta
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
from agent.coo.production_runtime_boundary import (
    BLOCK_BOUNDARY_EXPIRED,
    BLOCK_BOUNDARY_TTL_EXCEEDS_SESSION,
    BLOCK_BOUNDARY_TTL_INVALID,
    BLOCK_CONTROLLED_WINDOW_CLOSED,
    BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED,
    BLOCK_CONTROLLED_WINDOW_EXPIRED,
    BLOCK_EMERGENCY_CLOSE_UNAVAILABLE,
    BLOCK_GOVERNED_RUNTIME_SESSION_MISSING,
    BLOCK_IDENTITY_SEPARATION_INVALID,
    BLOCK_KILL_SWITCH_UNAVAILABLE,
    BLOCK_OPERATOR_IDENTITY_INVALID,
    BLOCK_PERMISSION_EXECUTOR_MISMATCH,
    BLOCK_PRODUCTION_EXECUTION_ENABLED,
    BLOCK_RECOVERY_REQUIRED,
    BLOCK_RUNTIME_FACTORY_UNAVAILABLE,
    BLOCK_RUNTIME_INVOKER_ENABLED,
    BLOCK_RUNTIME_PERMISSION_EXPIRED,
    BLOCK_RUNTIME_PERMISSION_MISSING,
    BLOCK_SESSION_EXECUTOR_MISMATCH,
    ACTION_PREPARE_RUNTIME_BOUNDARY,
    BOUNDARY_EXPIRED,
    BOUNDARY_READY,
    BOUNDARY_RESERVED,
    RELEASE_RUNTIME_BOUNDARY_RESERVED,
    EVENT_BOUNDARY_RESERVED,
    EVENT_CUTOVER_START_REQUESTED,
    EVENT_RUNTIME_BOUNDARY_BLOCKED,
    RuntimeBoundaryContext,
    RuntimeBoundaryError,
    build_production_runtime_boundary_release_summary,
    evaluate_production_runtime_boundary,
    format_production_runtime_boundary_status,
    load_runtime_boundary_events,
    prepare_production_runtime_boundary,
    resolve_latest_runtime_boundary_dashboard_digest,
)
from agent.coo.production_governed_runtime_session import (
    start_production_governed_runtime_session,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser
from tests.hermes_cli import test_production_governed_runtime_session as _sess
from tests.hermes_cli.test_production_runtime_permission import (
    _RUNTIME_EXECUTOR,
    _PERMISSION_OPERATOR,
)
from tests.hermes_cli.test_production_controlled_window import _WINDOW_OPERATOR

# session test file defines _SESSION_OPERATOR
_SESSION_OPERATOR = getattr(_sess, "_SESSION_OPERATOR", "session-operator-phase15d")
_BOUNDARY_OPERATOR = "boundary-operator-phase15e"


class TestProductionRuntimeBoundary(
    _sess.TestProductionGovernedRuntimeSession
):
    def setUp(self) -> None:
        super().setUp()
        self.boundary_store_dir = (
            self.hermes_home / "coo" / "production-runtime-boundary"
        )
        self.boundary_store_dir.mkdir(parents=True, exist_ok=True)

    def _start_session(self) -> tuple[str, str, str]:
        activation_id, permission_id = self._issue_permission()
        started = start_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id)
        )
        return activation_id, permission_id, started.session_id

    def _bound_kwargs(
        self,
        activation_id: str,
        permission_id: str,
        session_id: str,
        **overrides,
    ):
        base = {
            **self._eval_kwargs(activation_id),
            "boundary_store_dir": self.boundary_store_dir,
            "permission_store_dir": self.permission_store_dir,
            "session_store_dir": self.session_store_dir,
            "executor_id": _RUNTIME_EXECUTOR,
            "operator_id": _BOUNDARY_OPERATOR,
            "permission_id": permission_id,
            "session_id": session_id,
            "ttl_seconds": 60,
        }
        base.update(overrides)
        return base

    def _boundary_context_kwargs(self, activation_id: str, **overrides):
        base = {
            "activation_request_id": activation_id,
            "boundary_store_dir": self.boundary_store_dir,
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

    def _boundary_correlated_digests(
        self, activation_id: str
    ) -> tuple[str, str, str, str, str]:
        contract_path = self.governed_cutover_store_dir / f"{activation_id}.json"
        window_path = self.window_store_dir / f"{activation_id}.json"
        permission_path = self.permission_store_dir / f"{activation_id}.json"
        session_path = self.session_store_dir / f"{activation_id}.json"
        activation_path = self.store_dir / f"{activation_id}.json"
        return (
            self._digest(contract_path),
            self._digest(window_path),
            self._digest(permission_path),
            self._digest(session_path),
            self._digest(activation_path),
        )

    # -- 1. check readiness ---------------------------------------------------

    def test_check_ready_when_session_started_and_permission_issued(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        summary = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id, permission_id, session_id, operator_id=""
            )
        )
        self.assertEqual(summary.boundary_state, BOUNDARY_READY)
        self.assertTrue(summary.boundary_ready)
        self.assertEqual(
            summary.recommended_action, ACTION_PREPARE_RUNTIME_BOUNDARY
        )
        self.assertFalse(summary.production_execution_allowed)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.permission_consumed)
        self.assertFalse(summary.permission_revoked)

    # -- 2. read-only digests unchanged ---------------------------------------

    def test_check_read_only_digests_unchanged(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        before = self._boundary_correlated_digests(activation_id)
        evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id, permission_id, session_id, operator_id=""
            )
        )
        after = self._boundary_correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertFalse(list(self.boundary_store_dir.glob("*.json")))

    # -- 3. reserve success -----------------------------------------------------

    def test_reserve_success_append_only(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        before = self._boundary_correlated_digests(activation_id)
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                summary = prepare_production_runtime_boundary(
                    **self._bound_kwargs(activation_id, permission_id, session_id)
                )
        after = self._boundary_correlated_digests(activation_id)
        self.assertEqual(before, after)
        self.assertEqual(summary.boundary_state, BOUNDARY_RESERVED)
        self.assertTrue(summary.boundary_id)
        self.assertTrue(summary.invocation_id)
        self.assertFalse(summary.runtime_invoked)
        self.assertFalse(summary.cutover_started)
        self.assertFalse(summary.permission_consumed)
        self.assertTrue(summary.cutover_start_event_recorded)
        events = load_runtime_boundary_events(
            activation_id, store_dir=self.boundary_store_dir
        )
        types = [event.event_type for event in events]
        self.assertIn(EVENT_BOUNDARY_RESERVED, types)
        self.assertIn(EVENT_CUTOVER_START_REQUESTED, types)
        self.assertIn(EVENT_RUNTIME_BOUNDARY_BLOCKED, types)

    # -- 4. missing session blocked -------------------------------------------

    def test_missing_session_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        summary = evaluate_production_runtime_boundary(
            **self._bound_kwargs(activation_id, permission_id, "")
        )
        self.assertIn(
            BLOCK_GOVERNED_RUNTIME_SESSION_MISSING, summary.blocking_items
        )
        with self.assertRaises(RuntimeBoundaryError):
            prepare_production_runtime_boundary(
                **self._bound_kwargs(activation_id, permission_id, "")
            )

    # -- 5. missing / expired permission blocked ------------------------------

    def test_missing_and_expired_permission_blocked(self) -> None:
        activation_id = self._open_window()
        missing = evaluate_production_runtime_boundary(
            **self._bound_kwargs(activation_id, "", "")
        )
        self.assertIn(BLOCK_RUNTIME_PERMISSION_MISSING, missing.blocking_items)
        with self.assertRaises(RuntimeBoundaryError):
            prepare_production_runtime_boundary(
                **self._bound_kwargs(activation_id, "", "")
            )

        activation_id2, permission_id2, session_id2 = self._start_session()
        later = self._now + timedelta(seconds=301)
        expired = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id2, permission_id2, session_id2, now=later
            )
        )
        self.assertIn(BLOCK_RUNTIME_PERMISSION_EXPIRED, expired.blocking_items)
        with self.assertRaises(RuntimeBoundaryError):
            prepare_production_runtime_boundary(
                **self._bound_kwargs(
                    activation_id2, permission_id2, session_id2, now=later
                )
            )

    # -- 6. controlled window closed / emergency / expired blocked ------------

    def test_window_closed_emergency_expired_blocked(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        close_production_controlled_window(
            **self._eval_kwargs(
                activation_id,
                operator_id=_WINDOW_OPERATOR,
                reason_code=REASON_OPERATOR_CLOSE,
            )
        )
        closed = evaluate_production_runtime_boundary(
            **self._bound_kwargs(activation_id, permission_id, session_id)
        )
        self.assertIn(BLOCK_CONTROLLED_WINDOW_CLOSED, closed.blocking_items)

        activation_id2, permission_id2, session_id2 = self._start_session()
        emergency_close_production_controlled_window(
            **self._eval_kwargs(
                activation_id2,
                operator_id=_WINDOW_OPERATOR,
                actor_role="operator",
                reason_code=REASON_INCIDENT_DETECTED,
            )
        )
        emergency = evaluate_production_runtime_boundary(
            **self._bound_kwargs(activation_id2, permission_id2, session_id2)
        )
        self.assertIn(
            BLOCK_CONTROLLED_WINDOW_EMERGENCY_CLOSED, emergency.blocking_items
        )

        activation_id3, permission_id3, session_id3 = self._start_session()
        later = self._now + timedelta(hours=2)
        expired = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id3, permission_id3, session_id3, now=later
            )
        )
        self.assertTrue(expired.controlled_window_expired)
        self.assertIn(BLOCK_CONTROLLED_WINDOW_EXPIRED, expired.blocking_items)

    # -- 7. TTL bounds ----------------------------------------------------------

    def test_ttl_bounds_low_and_high_blocked(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        low = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id, permission_id, session_id, ttl_seconds=10
            )
        )
        self.assertIn(BLOCK_BOUNDARY_TTL_INVALID, low.blocking_items)
        high = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id, permission_id, session_id, ttl_seconds=121
            )
        )
        self.assertIn(BLOCK_BOUNDARY_TTL_INVALID, high.blocking_items)
        with self.assertRaises(RuntimeBoundaryError):
            prepare_production_runtime_boundary(
                **self._bound_kwargs(
                    activation_id, permission_id, session_id, ttl_seconds=10
                )
            )

    # -- 8. TTL exceeds session -------------------------------------------------

    def test_ttl_exceeds_session_blocked(self) -> None:
        activation_id, permission_id = self._issue_permission()
        started = start_production_governed_runtime_session(
            **self._sess_kwargs(activation_id, permission_id, ttl_seconds=60)
        )
        summary = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id, permission_id, started.session_id, ttl_seconds=90
            )
        )
        self.assertIn(BLOCK_BOUNDARY_TTL_EXCEEDS_SESSION, summary.blocking_items)
        with self.assertRaises(RuntimeBoundaryError):
            prepare_production_runtime_boundary(
                **self._bound_kwargs(
                    activation_id, permission_id, started.session_id, ttl_seconds=90
                )
            )

    # -- 9. identity conflicts ---------------------------------------------------

    def test_identity_conflicts_blocked(self) -> None:
        activation_id, permission_id, session_id = self._start_session()

        mismatch = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id,
                permission_id,
                session_id,
                executor_id="other-runtime-executor",
            )
        )
        self.assertTrue(
            BLOCK_PERMISSION_EXECUTOR_MISMATCH in mismatch.blocking_items
            or BLOCK_SESSION_EXECUTOR_MISMATCH in mismatch.blocking_items
        )

        same_as_executor = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id, permission_id, session_id, operator_id=_RUNTIME_EXECUTOR
            )
        )
        self.assertTrue(
            BLOCK_IDENTITY_SEPARATION_INVALID in same_as_executor.blocking_items
            or BLOCK_OPERATOR_IDENTITY_INVALID in same_as_executor.blocking_items
        )

        same_as_session_starter = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id, permission_id, session_id, operator_id=_SESSION_OPERATOR
            )
        )
        self.assertIn(
            BLOCK_OPERATOR_IDENTITY_INVALID, same_as_session_starter.blocking_items
        )

        same_as_permission_issuer = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id,
                permission_id,
                session_id,
                operator_id=_PERMISSION_OPERATOR,
            )
        )
        self.assertIn(
            BLOCK_OPERATOR_IDENTITY_INVALID, same_as_permission_issuer.blocking_items
        )

    # -- 10. recovery required ------------------------------------------------------

    def test_recovery_required_blocked(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
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
            summary = evaluate_production_runtime_boundary(
                **self._bound_kwargs(
                    activation_id, permission_id, session_id, operator_id="", executor_id=""
                )
            )
        self.assertIn(BLOCK_RECOVERY_REQUIRED, summary.blocking_items)

    # -- 11. force flags never leak into the summary --------------------------------

    def test_force_flags_blocked_summary_stays_false(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        forced = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id,
                permission_id,
                session_id,
                force_production_execution_allowed=True,
                force_gateway_enabled=True,
                force_discord_enabled=True,
                force_cutover_started=True,
                force_runtime_invoked=True,
                force_permission_consumed=True,
                force_permission_revoked=True,
            )
        )
        self.assertIn(BLOCK_PRODUCTION_EXECUTION_ENABLED, forced.blocking_items)
        self.assertFalse(forced.production_execution_allowed)
        self.assertFalse(forced.cutover_started)
        self.assertFalse(forced.runtime_invoked)
        self.assertFalse(forced.permission_consumed)
        self.assertFalse(forced.permission_revoked)

    # -- 12. failsafe / factory / invoker availability -------------------------------

    def test_force_kill_emergency_factory_invoker_blocked(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        kill = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id,
                permission_id,
                session_id,
                force_kill_switch_unavailable=True,
            )
        )
        self.assertIn(BLOCK_KILL_SWITCH_UNAVAILABLE, kill.blocking_items)
        emergency = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id,
                permission_id,
                session_id,
                force_emergency_close_unavailable=True,
            )
        )
        self.assertIn(BLOCK_EMERGENCY_CLOSE_UNAVAILABLE, emergency.blocking_items)
        factory = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id,
                permission_id,
                session_id,
                force_runtime_factory_unavailable=True,
            )
        )
        self.assertIn(BLOCK_RUNTIME_FACTORY_UNAVAILABLE, factory.blocking_items)
        invoker = evaluate_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id,
                permission_id,
                session_id,
                force_runtime_invoker_enabled=True,
            )
        )
        self.assertIn(BLOCK_RUNTIME_INVOKER_ENABLED, invoker.blocking_items)

    # -- 13. idempotent duplicate reserve -----------------------------------------

    def test_duplicate_same_reserve_idempotent(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        first = prepare_production_runtime_boundary(
            **self._bound_kwargs(activation_id, permission_id, session_id)
        )
        second = prepare_production_runtime_boundary(
            **self._bound_kwargs(activation_id, permission_id, session_id)
        )
        self.assertEqual(first.boundary_id, second.boundary_id)
        self.assertEqual(second.boundary_state, BOUNDARY_RESERVED)
        paths = list(self.boundary_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 14. changed executor / TTL conflicts --------------------------------------

    def test_duplicate_changed_executor_and_ttl_conflict(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        prepare_production_runtime_boundary(
            **self._bound_kwargs(activation_id, permission_id, session_id)
        )
        with self.assertRaises(RuntimeBoundaryError) as ctx:
            prepare_production_runtime_boundary(
                **self._bound_kwargs(
                    activation_id,
                    permission_id,
                    session_id,
                    executor_id="other-runtime-executor",
                )
            )
        self.assertIn("runtime_boundary_conflict", str(ctx.exception))

        with self.assertRaises(RuntimeBoundaryError) as ctx2:
            prepare_production_runtime_boundary(
                **self._bound_kwargs(
                    activation_id, permission_id, session_id, ttl_seconds=45
                )
            )
        self.assertIn("runtime_boundary_conflict", str(ctx2.exception))

    # -- 15. concurrent reserve ----------------------------------------------------

    def test_concurrent_reserve_one_success(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        results: list[str] = []
        errors: list[str] = []

        def worker() -> None:
            try:
                summary = prepare_production_runtime_boundary(
                    **self._bound_kwargs(activation_id, permission_id, session_id)
                )
                results.append(summary.boundary_id)
            except RuntimeBoundaryError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(results)
        self.assertEqual(len(set(results)), 1)
        paths = list(self.boundary_store_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)

    # -- 16. expiry and re-reserve ---------------------------------------------------

    def test_expired_boundary_derived_and_re_reserve_blocked(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        reserved = prepare_production_runtime_boundary(
            **self._bound_kwargs(
                activation_id, permission_id, session_id, ttl_seconds=15
            )
        )
        self.assertEqual(reserved.boundary_state, BOUNDARY_RESERVED)
        later = self._now + timedelta(seconds=16)
        expired = evaluate_production_runtime_boundary(
            **self._bound_kwargs(activation_id, permission_id, session_id, now=later)
        )
        self.assertEqual(expired.boundary_state, BOUNDARY_EXPIRED)
        self.assertIn(BLOCK_BOUNDARY_EXPIRED, expired.blocking_items)
        with self.assertRaises(RuntimeBoundaryError) as ctx:
            prepare_production_runtime_boundary(
                **self._bound_kwargs(
                    activation_id, permission_id, session_id, now=later
                )
            )
        self.assertIn("boundary_expired", str(ctx.exception))

    # -- 17. RuntimeBoundaryContext ------------------------------------------

    def test_context_valid_nested_reuse_and_pickle_blocked(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        reserved = prepare_production_runtime_boundary(
            **self._bound_kwargs(activation_id, permission_id, session_id)
        )
        context = RuntimeBoundaryContext(
            **self._boundary_context_kwargs(activation_id)
        )
        with context as ctx:
            self.assertTrue(ctx.active)
            self.assertEqual(ctx.boundary_id, reserved.boundary_id)
            self.assertEqual(ctx.session_id, session_id)
            self.assertEqual(ctx.permission_id, permission_id)
            with self.assertRaises(RuntimeBoundaryError):
                with context:
                    pass
        self.assertFalse(context.active)
        with self.assertRaises(RuntimeBoundaryError):
            with context:
                pass
        with self.assertRaises(RuntimeBoundaryError):
            pickle.dumps(context)

    # -- 18. safe output + CLI parsers --------------------------------------------------

    def test_safe_output_and_cli_parsers(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        reserved = prepare_production_runtime_boundary(
            **self._bound_kwargs(activation_id, permission_id, session_id)
        )
        output = format_production_runtime_boundary_status(reserved)
        self.assertIn("boundary_state:", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertNotIn(_RUNTIME_EXECUTOR, output)
        self.assertNotIn(_BOUNDARY_OPERATOR, output)
        self.assertNotIn(_SESSION_OPERATOR, output)
        self.assertNotIn(_PERMISSION_OPERATOR, output)
        self.assertNotIn(str(self.hermes_home), output)
        self.assertNotIn("/opt/data/", output)
        parser = build_coo_dispatch_parser()
        for cmd, handler in (
            (
                [
                    "production", "governed-cutover", "runtime-boundary", "status",
                    "--activation-request-id", activation_id,
                ],
                "_cmd_production_runtime_boundary_status",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-boundary", "check",
                    "--activation-request-id", activation_id,
                    "--session-id", session_id,
                    "--permission-id", permission_id,
                ],
                "_cmd_production_runtime_boundary_check",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-boundary", "prepare",
                    "--activation-request-id", activation_id,
                    "--session-id", session_id,
                    "--permission-id", permission_id,
                    "--executor-id", _RUNTIME_EXECUTOR,
                    "--operator-id", _BOUNDARY_OPERATOR,
                    "--ttl-seconds", "60",
                ],
                "_cmd_production_runtime_boundary_prepare",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-boundary", "show",
                    "--boundary-id", reserved.boundary_id,
                ],
                "_cmd_production_runtime_boundary_show",
            ),
            (
                [
                    "production", "governed-cutover", "runtime-boundary", "history",
                    "--activation-request-id", activation_id,
                ],
                "_cmd_production_runtime_boundary_history",
            ),
        ):
            args = parser.parse_args(cmd)
            self.assertEqual(args.handler.__name__, handler)
        legacy = parser.parse_args(
            ["production", "cutover-check", "--ticket-id", "ticket-x"]
        )
        self.assertEqual(legacy.handler.__name__, "_cmd_production_cutover_check")

    # -- 19. dashboard + release summary --------------------------------------------------

    def test_dashboard_and_release_summary(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        reserved = prepare_production_runtime_boundary(
            **self._bound_kwargs(activation_id, permission_id, session_id)
        )
        digest = resolve_latest_runtime_boundary_dashboard_digest(
            governed_cutover_store_dir=self.governed_cutover_store_dir,
            window_store_dir=self.window_store_dir,
            permission_store_dir=self.permission_store_dir,
            session_store_dir=self.session_store_dir,
            boundary_store_dir=self.boundary_store_dir,
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
        self.assertEqual(digest.runtime_boundary_state, BOUNDARY_RESERVED)
        self.assertTrue(digest.runtime_boundary_present)
        self.assertEqual(digest.runtime_boundary_id, reserved.boundary_id)
        dashboard = build_operator_dashboard_summary(merged_config={})
        self.assertTrue(hasattr(dashboard, "runtime_boundary_state"))
        release = build_production_runtime_boundary_release_summary(
            reserved
        )
        self.assertEqual(
            release.release_status, RELEASE_RUNTIME_BOUNDARY_RESERVED
        )
        self.assertEqual(release.next_phase, "Phase_15F_governed_runtime_invocation")
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

    # -- 20. no subprocess on reserve ------------------------------------------------------

    def test_no_subprocess_on_reserve(self) -> None:
        activation_id, permission_id, session_id = self._start_session()
        with patch.object(
            subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("no popen")
            ):
                prepare_production_runtime_boundary(
                    **self._bound_kwargs(activation_id, permission_id, session_id)
                )


if __name__ == "__main__":
    unittest.main()
